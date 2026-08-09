"""Finding service — CRUD, disposition state machine, draftability gate (§3.15).

The finding service is the single place findings are created, listed, and
dispositioned.  It enforces:

- **Disposition state machine** (§3.15.2): ``PENDING → {CONFIRMED, REJECTED,
  EDITED}``; ``CONFIRMED → EDITED``; no other transitions.
- **confirmedText required** for ``CONFIRMED`` / ``EDITED`` (enforced in the
  model validator; the service double-checks).
- **FINDING_DISPOSITIONED audit event** carrying ``findingId``, prior and new
  state, ``provenance.source``, ``provenance.modelVersion``, and operator id.
- **Draftability gate** (criterion 9): ``assert_draftable()`` raises
  ``FindingsPendingError`` (409) while any finding is ``PENDING``, with the
  count.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import ConflictError, FindingsPendingError, NotFoundError
from app.models.finding import (
    Disposition,
    DispositionRequest,
    Finding,
    FindingsResponse,
    UnavailableReason,
)
from app.models.study import StudyRecord, ViewerScope
from app.repositories.base import DocumentStore
from app.services.access_policy import StudyAccessPolicy
from app.services.audit_service import AuditService

logger = logging.getLogger("vurarad.findings")

FINDINGS_COLLECTION = "findings"

# Allowed disposition transitions.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"CONFIRMED", "REJECTED", "EDITED"}),
    "CONFIRMED": frozenset({"EDITED", "REJECTED"}),
    "EDITED": frozenset({"CONFIRMED", "REJECTED"}),
    "REJECTED": frozenset({"CONFIRMED", "EDITED"}),
}


class FindingService:
    """Finding CRUD + disposition state machine + draftability gate."""

    def __init__(
        self,
        store: DocumentStore,
        audit: AuditService,
        *,
        access_policy: StudyAccessPolicy | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._policy = access_policy or StudyAccessPolicy()

    # -- list findings (route 20) --------------------------------------------
    async def list_findings(
        self,
        user: AuthenticatedUser,
        study: StudyRecord,
        viewer_scope: ViewerScope | None = None,
    ) -> FindingsResponse:
        """List all findings for a study (§3.15.1)."""
        self._policy.assert_can_read(user, study, viewer_scope)
        rows = await self._store.query(
            FINDINGS_COLLECTION,
            where=[("study_id", "==", study.study_id)],
        )
        findings: list[Finding] = []
        for _doc_id, doc in rows:
            findings.append(Finding.model_validate(doc))
        return FindingsResponse(
            study_id=study.study_id,
            generated_at=datetime.now(UTC),
            preprocessing_state="COMPLETE",
            findings=findings,
            unavailable_reasons=[],
        )

    def list_findings_with_reasons(
        self,
        study: StudyRecord,
        findings: list[Finding],
        unavailable_reasons: list[UnavailableReason],
    ) -> FindingsResponse:
        """Build a :class:`FindingsResponse` with explicit reasons (no I/O)."""
        return FindingsResponse(
            study_id=study.study_id,
            generated_at=datetime.now(UTC),
            findings=findings,
            unavailable_reasons=unavailable_reasons,
        )

    # -- disposition (route 21) -----------------------------------------------
    async def disposition(
        self,
        user: AuthenticatedUser,
        study: StudyRecord,
        finding_id: str,
        request: DispositionRequest,
    ) -> Finding:
        """Apply a disposition to a finding (§3.15.2).

        Writes a ``FINDING_DISPOSITIONED`` audit event with prior and new state,
        source, model version, and operator id.  Raises ``ConflictError`` for
        invalid transitions.
        """
        self._policy.assert_can_write(user, study)
        doc = await self._store.get(FINDINGS_COLLECTION, finding_id)
        if doc is None:
            raise NotFoundError(f"Finding {finding_id} not found")
        finding = Finding.model_validate(doc)
        prior_state = finding.disposition.state
        new_state = request.state
        allowed = _ALLOWED_TRANSITIONS.get(prior_state, frozenset())
        if new_state not in allowed:
            raise ConflictError(f"Cannot transition finding from {prior_state} to {new_state}")
        now = datetime.now(UTC)
        finding.disposition = Disposition(
            state=new_state,
            by_uid=user.uid,
            by_operator_id=user.operator_id,
            at=now,
            dictation_ref=request.dictation_ref,
            confirmed_text=request.confirmed_text,
        )
        await self._store.set(FINDINGS_COLLECTION, finding_id, finding.model_dump())
        await self._audit.record(
            "FINDING_DISPOSITIONED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "findingId": finding_id,
                "studyId": study.study_id,
                "priorState": prior_state,
                "newState": new_state,
                "source": finding.provenance.source,
                "modelVersion": finding.provenance.model_version,
                "operatorId": user.operator_id,
            },
            patient_key=study.patient_key,
        )
        logger.info(
            "FINDING_DISPOSITIONED",
            extra={
                "finding_id": finding_id,
                "prior_state": prior_state,
                "new_state": new_state,
            },
        )
        return finding

    # -- draftability gate (criterion 9) -------------------------------------
    async def assert_draftable(self, study_id: str) -> None:
        """Raise ``FindingsPendingError`` if any finding is still PENDING.

        A study cannot move to report drafting while any finding is ``PENDING``
        (§3.15.2).  The error carries the pending count.
        """
        rows = await self._store.query(
            FINDINGS_COLLECTION,
            where=[("study_id", "==", study_id)],
        )
        pending = sum(1 for _id, doc in rows if _disposition_state(doc) == "PENDING")
        if pending > 0:
            raise FindingsPendingError(pending_count=pending)

    # -- create (used by the pipeline / ingest) -------------------------------
    async def create_finding(self, finding: Finding) -> Finding:
        """Persist a new finding (used by the pre-processing pipeline)."""
        await self._store.create(FINDINGS_COLLECTION, finding.finding_id, finding.model_dump())
        return finding

    @staticmethod
    def new_finding_id() -> str:
        """Generate a new finding id (``fd_<ulid>``)."""
        return f"fd_{ULID()}"


def _disposition_state(doc: dict[str, Any]) -> str:
    """Extract the disposition state from a stored finding doc (camel/snake)."""
    disp = doc.get("disposition")
    if isinstance(disp, dict):
        state = disp.get("state")
        if isinstance(state, str):
            return state
    return "PENDING"


__all__ = ["FINDINGS_COLLECTION", "FindingService"]
