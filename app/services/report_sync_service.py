"""Report sync service — offline draft sync with a mutation ledger (§3.21.4).

A radiologist drafting offline accumulates mutations (section edits and
dictation appends) and pushes them with ``POST /reports/{id}/sync`` once
connectivity returns.  The service is the conflict-resolution authority:

- **Idempotent by ``mutationId``** (criterion 4): every applied mutation is
  recorded in the ``report_sync_mutations`` ledger, so a re-send over a flaky
  link is a no-op that still appears in ``applied``.
- **Section conflicts** (criterion 6): a ``SET_SECTION`` whose section was
  modified by a *different* user after the mutation's ``baseVersion`` conflicts
  — the response carries both the server's and the client's text and the mutation
  is **not** applied.  When the *same* user edited after ``baseVersion``, the
  later ``at`` wins (no conflict).
- **``APPEND_DICTATION`` never conflicts** (criterion 7): segments are ordered by
  ``at`` so out-of-order offline delivery still reconstructs the narrative.
- **One sync = exactly one new version document** (criterion 8) when at least one
  mutation actually changes the report; a pure replay sync bumps nothing.
- **Every sync writes ``REPORT_SYNCED``** (criterion 9) with the applied and
  conflicted mutation IDs.
- A ``SIGNED`` report is immutable — sync returns ``409 REPORT_SIGNED``
  (criterion 5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import model_validator
from pydantic_core import PydanticCustomError
from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import NotFoundError, ReportSignedError
from app.models.common import CamelModel
from app.models.study import StudyRecord
from app.repositories.base import DocumentStore
from app.repositories.study_repo import StudyRepository
from app.repositories.sync_repo import SyncRepository
from app.services.access_policy import StudyAccessPolicy
from app.services.audit_service import AuditService

logger = logging.getLogger("vurarad.report_sync")


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------
class ReportStatus(StrEnum):
    DRAFT = "DRAFT"
    SIGNED = "SIGNED"


class MutationType(StrEnum):
    SET_SECTION = "SET_SECTION"
    APPEND_DICTATION = "APPEND_DICTATION"


class ReportSection(CamelModel):
    """One report section — carries the conflict-detection provenance."""

    section_id: str
    text: str
    modified_by: str  # uid of the last writer
    modified_at: datetime  # timestamp of the last write — "later at wins"
    version: int  # report version when last modified


class DictationSyncSegment(CamelModel):
    """One appended dictation segment, ordered by ``at``."""

    mutation_id: str
    at: datetime
    by: str
    text: str


class Report(CamelModel):
    """The report document at ``reports/{reportId}`` — internal (carries PHI text)."""

    report_id: str
    study_id: str
    status: ReportStatus = ReportStatus.DRAFT
    sections: dict[str, ReportSection] = {}
    dictation_segments: list[DictationSyncSegment] = []
    version: int = 0
    signed_at: datetime | None = None
    signed_by: str | None = None
    tenant_id: str = "default"


class SyncMutation(CamelModel):
    """One client mutation in a sync request.

    ``by`` is NOT client-supplied — the server stamps every mutation with the
    authenticated user's uid, so a cross-user conflict is always "the section was
    edited by someone else server-side".
    """

    mutation_id: str  # idempotency key
    type: MutationType
    section_id: str | None = None  # required for SET_SECTION
    text: str
    base_version: int = 0  # the report version this edit was based on
    at: datetime  # edit / capture timestamp — ordering + "later at wins"

    @model_validator(mode="after")
    def _validate_section_id(self) -> Self:
        if self.type == MutationType.SET_SECTION and not self.section_id:
            raise PydanticCustomError(
                "section_id_required",
                "sectionId is required for SET_SECTION mutations",
            )
        return self


class SyncConflict(CamelModel):
    """A cross-user section conflict — carries both texts (criterion 6)."""

    mutation_id: str
    section_id: str
    server_text: str
    client_text: str
    server_by: str
    client_by: str
    server_version: int


class SyncRequest(CamelModel):
    """``POST /reports/{id}/sync`` request body."""

    mutations: list[SyncMutation]


class CreateReportRequest(CamelModel):
    """``POST /reports`` request body — create a fresh DRAFT report."""

    study_id: str
    report_id: str | None = None  # optional client-supplied id


class SyncResult(CamelModel):
    """``POST /reports/{id}/sync`` response — applied + conflicted mutations."""

    report_id: str
    version: int
    applied: list[str]  # mutation IDs processed without conflict (incl. replays)
    conflicted: list[SyncConflict]


class ReportResponse(CamelModel):
    """``GET /reports/{id}`` response — the report state for the client."""

    report_id: str
    study_id: str
    status: ReportStatus
    version: int
    sections: list[ReportSection]
    dictation_segments: list[DictationSyncSegment]  # ordered by ``at``
    dictation_text: str  # segments joined in ``at`` order


# ---------------------------------------------------------------------------
# Internal outcome of one SET_SECTION mutation
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _SetOutcome:
    conflict: SyncConflict | None = None
    changed: bool = False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class ReportSyncService:
    """Mutation ledger + conflict resolution for offline draft sync."""

    def __init__(
        self,
        doc_store: DocumentStore,
        sync_repo: SyncRepository,
        study_repo: StudyRepository,
        audit: AuditService,
        *,
        access_policy: StudyAccessPolicy | None = None,
    ) -> None:
        self._store = doc_store
        self._sync_repo = sync_repo
        self._study_repo = study_repo
        self._audit = audit
        self._policy = access_policy or StudyAccessPolicy()

    # -- create (seeding / first draft) --------------------------------------
    async def create_report(
        self,
        user: AuthenticatedUser,
        study_id: str,
        report_id: str | None = None,
    ) -> ReportResponse:
        """Create a fresh DRAFT report scoped to a study (asserts write access)."""
        study = await self._require_study(study_id)
        self._policy.assert_can_write(user, study)
        rid = report_id or f"rp_{ULID()}"
        report = Report(
            report_id=rid,
            study_id=study_id,
            status=ReportStatus.DRAFT,
            tenant_id=study.tenant_id,
        )
        await self._sync_repo.save_report(rid, report.model_dump())
        await self._audit.record(
            "REPORT_CREATED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"reportId": rid, "studyId": study_id},
            patient_key=study.patient_key,
        )
        return self._to_response(report)

    # -- read ----------------------------------------------------------------
    async def get_report(self, user: AuthenticatedUser, report_id: str) -> ReportResponse:
        """Return the report state (asserts read access)."""
        report = await self._load_report(report_id)
        study = await self._require_study(report.study_id)
        self._policy.assert_can_read(user, study)
        return self._to_response(report)

    # -- sync (criterion 4-9) ------------------------------------------------
    async def sync(
        self,
        user: AuthenticatedUser,
        report_id: str,
        request: SyncRequest,
    ) -> SyncResult:
        """Apply a batch of mutations with idempotency and conflict resolution."""
        report = await self._load_report(report_id)
        if report.status == ReportStatus.SIGNED:
            raise ReportSignedError()
        study = await self._require_study(report.study_id)
        self._policy.assert_can_write(user, study)

        already_applied = await self._sync_repo.get_applied_mutation_ids(report_id)
        applied: list[str] = []
        conflicts: list[SyncConflict] = []
        seen_this_sync: set[str] = set()
        changed = False

        for mut in request.mutations:
            # Idempotency — a mutation already applied (ledger) or already
            # processed earlier in this same request is a no-op replay.
            if mut.mutation_id in already_applied or mut.mutation_id in seen_this_sync:
                applied.append(mut.mutation_id)
                continue
            seen_this_sync.add(mut.mutation_id)

            if mut.type == MutationType.SET_SECTION:
                outcome = self._apply_set_section(report, mut, user.uid)
                if outcome.conflict is not None:
                    conflicts.append(outcome.conflict)
                    # Conflicts are NOT recorded in the ledger: a re-send
                    # re-evaluates against the current server state.
                    continue
                applied.append(mut.mutation_id)
                if outcome.changed:
                    changed = True
                await self._sync_repo.record_mutation(
                    report_id, mut.mutation_id, mut.type.value, mut.at.isoformat()
                )
            else:  # APPEND_DICTATION — never conflicts (criterion 7)
                self._apply_append_dictation(report, mut, user.uid)
                applied.append(mut.mutation_id)
                changed = True
                await self._sync_repo.record_mutation(
                    report_id, mut.mutation_id, mut.type.value, mut.at.isoformat()
                )

        if changed:
            report.version += 1
            await self._sync_repo.save_report(report_id, report.model_dump())
            # One sync = exactly one new version document (criterion 8).
            await self._sync_repo.record_version(
                report_id,
                report.version,
                applied,
                [c.mutation_id for c in conflicts],
                user.uid,
            )

        # Every sync writes REPORT_SYNCED with applied + conflicted IDs (criterion 9).
        await self._audit.record(
            "REPORT_SYNCED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "reportId": report_id,
                "applied": applied,
                "conflicted": [c.mutation_id for c in conflicts],
                "version": report.version,
            },
            patient_key=study.patient_key,
        )

        return SyncResult(
            report_id=report_id,
            version=report.version,
            applied=applied,
            conflicted=conflicts,
        )

    # -- erasure (criterion 3 — PHI erased with the study) -------------------
    async def erase_for_study(self, study_id: str) -> int:
        """Erase every report, version, and mutation ledger entry for a study."""
        return await self._sync_repo.erase_for_study(study_id)

    # -- conflict resolution -------------------------------------------------
    def _apply_set_section(self, report: Report, mut: SyncMutation, user_uid: str) -> _SetOutcome:
        """Apply one SET_SECTION mutation; return the outcome.

        - section absent or last modified at/below ``baseVersion`` → apply.
        - modified after ``baseVersion`` by a *different* user → conflict.
        - modified after ``baseVersion`` by the *same* user → later ``at`` wins.
        """
        assert mut.section_id is not None  # validated by SyncMutation
        current = report.sections.get(mut.section_id)
        next_version = report.version + 1

        if current is None:
            report.sections[mut.section_id] = ReportSection(
                section_id=mut.section_id,
                text=mut.text,
                modified_by=user_uid,
                modified_at=mut.at,
                version=next_version,
            )
            return _SetOutcome(changed=True)

        modified_after_base = current.version > mut.base_version

        if modified_after_base and current.modified_by != user_uid:
            # Cross-user conflict — both texts, not applied (criterion 6).
            return _SetOutcome(
                conflict=SyncConflict(
                    mutation_id=mut.mutation_id,
                    section_id=mut.section_id,
                    server_text=current.text,
                    client_text=mut.text,
                    server_by=current.modified_by,
                    client_by=user_uid,
                    server_version=current.version,
                )
            )

        if modified_after_base and current.modified_by == user_uid:
            # Same user — later ``at`` wins (criterion 6).
            if mut.at >= current.modified_at:
                report.sections[mut.section_id] = ReportSection(
                    section_id=mut.section_id,
                    text=mut.text,
                    modified_by=user_uid,
                    modified_at=mut.at,
                    version=next_version,
                )
                return _SetOutcome(changed=True)
            # Superseded by a later same-user edit — no change, no conflict.
            return _SetOutcome(changed=False)

        # No modification after base_version — safe to apply.
        report.sections[mut.section_id] = ReportSection(
            section_id=mut.section_id,
            text=mut.text,
            modified_by=user_uid,
            modified_at=mut.at,
            version=next_version,
        )
        return _SetOutcome(changed=True)

    @staticmethod
    def _apply_append_dictation(report: Report, mut: SyncMutation, user_uid: str) -> None:
        """Append one dictation segment and re-order by ``at`` (criterion 7)."""
        report.dictation_segments.append(
            DictationSyncSegment(
                mutation_id=mut.mutation_id,
                at=mut.at,
                by=user_uid,
                text=mut.text,
            )
        )
        report.dictation_segments.sort(key=lambda s: (s.at, s.mutation_id))

    # -- helpers -------------------------------------------------------------
    async def _load_report(self, report_id: str) -> Report:
        doc = await self._sync_repo.get_report(report_id)
        if doc is None:
            raise NotFoundError(f"Report {report_id} not found")
        return self._report_from_doc(doc)

    async def _require_study(self, study_id: str) -> StudyRecord:
        study = await self._study_repo.get_study(study_id)
        if study is None:
            raise NotFoundError(f"Study {study_id} not found")
        return study

    @staticmethod
    def _report_from_doc(doc: dict[str, Any]) -> Report:
        """Reconstruct a Report from a stored doc (snake_case or camelCase)."""
        return Report.model_validate(doc)

    @staticmethod
    def _to_response(report: Report) -> ReportResponse:
        sections = [report.sections[k] for k in sorted(report.sections)]
        segments = list(report.dictation_segments)  # already ordered by ``at``
        return ReportResponse(
            report_id=report.report_id,
            study_id=report.study_id,
            status=report.status,
            version=report.version,
            sections=sections,
            dictation_segments=segments,
            dictation_text="\n".join(s.text for s in segments),
        )


__all__ = [
    "CreateReportRequest",
    "DictationSyncSegment",
    "MutationType",
    "Report",
    "ReportResponse",
    "ReportSection",
    "ReportSignedError",
    "ReportStatus",
    "ReportSyncService",
    "SyncConflict",
    "SyncMutation",
    "SyncRequest",
    "SyncResult",
]
