"""Report service — lifecycle, transactional signing, addenda, version history.

The single place report state is mutated.  Enforces:

- **State machine** (criterion 1): only ``DRAFT → PENDING_SIGNATURE → SIGNED``.
  Any other transition raises ``409 INVALID_REPORT_TRANSITION``.
- **Immutability** (criterion 2): a ``PATCH`` on a ``SIGNED`` report raises
  ``409 REPORT_IMMUTABLE`` (not 403).
- **Signing gates** (criteria 3-6): a non-empty ``Idempotency-Key`` (400), a
  fresh ``X-Second-Factor-Assertion`` (403, with replay → 403), and an explicit
  attestation (422).
- **Idempotent replay** (criterion 4): the same ``Idempotency-Key`` returns the
  verbatim original response with no second audit event.
- **Plain SHA-256 content hash** (criterion 7): ``compute_content_hash`` — never
  HMAC.
- **One transaction** (criterion 8): the report write, version write, worklist
  update, audit mirror write, and analytics counter increment all happen inside
  ``ReportRepo.sign_transaction``; a failure rolls back every write.
- **Addenda** (criteria 9-10): only on a ``SIGNED`` parent, same 2FA +
  attestation, and the parent is never mutated.
- **Access control** (criteria 12-13): :class:`StudyAccessPolicy` from WP4 —
  viewer denied on mutations, admin rejected at the router, and a radiologist
  cannot sign an unassigned study (``403 NOT_ASSIGNED``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import (
    AttestationRequiredError,
    ConflictError,
    InvalidReportTransitionError,
    NotFoundError,
    ReportImmutableError,
    SecondFactorAssertionReplayedError,
    SecondFactorReassertionRequiredError,
)
from app.core.redaction import redact
from app.models.report import (
    AddendumRequest,
    CreateReportRequest,
    ReportDraft,
    ReportResponse,
    ReportSections,
    ReportSignature,
    ReportStatus,
    ReportType,
    ReportVersion,
    ReportVersionResponse,
    SignatureOrigin,
    SignReportRequest,
    UpdateReportRequest,
)
from app.models.study import StudyRecord, ViewerScope
from app.repositories.report_repo import REPORTS_COLLECTION, ReportRepo, ReportTxn
from app.repositories.study_repo import (
    STUDIES_COLLECTION,
    WORKLIST_COLLECTION,
    WORKLIST_DOC_ID,
    StudyRepository,
)
from app.repositories.version_repo import VersionRepo
from app.services.access_policy import StudyAccessPolicy
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import AuditService

logger = logging.getLogger("vurarad.reports")

# ---------------------------------------------------------------------------
# State machine — the ONLY legal report status transitions
# ---------------------------------------------------------------------------
_ALLOWED_REPORT_TRANSITIONS: dict[ReportStatus, frozenset[ReportStatus]] = {
    ReportStatus.DRAFT: frozenset({ReportStatus.PENDING_SIGNATURE}),
    ReportStatus.PENDING_SIGNATURE: frozenset({ReportStatus.SIGNED}),
    ReportStatus.SIGNED: frozenset(),  # terminal — no transitions out
}


def validate_report_transition(current: ReportStatus, target: ReportStatus) -> None:
    """Raise ``InvalidReportTransitionError`` unless ``current → target`` is legal."""
    allowed = _ALLOWED_REPORT_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidReportTransitionError(
            f"Report cannot transition from {current.value} to {target.value}"
        )


# ---------------------------------------------------------------------------
# In-memory stores for signing idempotency + second-factor replay detection
# ---------------------------------------------------------------------------
class _CachedResponse:
    """A cached sign/addendum response for verbatim replay."""

    __slots__ = ("request_hash", "response_body", "status_code")

    def __init__(self, request_hash: str, response_body: dict[str, Any], status_code: int) -> None:
        self.request_hash = request_hash
        self.response_body = response_body
        self.status_code = status_code


class SignIdempotencyCache:
    """Caches sign/addendum responses keyed by ``(uid, idempotencyKey)``."""

    def __init__(self) -> None:
        self._records: dict[str, _CachedResponse] = {}

    @staticmethod
    def _key(uid: str, idem_key: str) -> str:
        return f"{uid}:{idem_key}"

    def get(self, uid: str, idem_key: str) -> _CachedResponse | None:
        return self._records.get(self._key(uid, idem_key))

    def record(
        self,
        uid: str,
        idem_key: str,
        request_hash: str,
        response_body: dict[str, Any],
        status_code: int,
    ) -> None:
        self._records[self._key(uid, idem_key)] = _CachedResponse(
            request_hash, response_body, status_code
        )


class SecondFactorAssertionStore:
    """Records second-factor assertion ids that have been consumed by a sign."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def is_used(self, assertion_id: str) -> bool:
        return assertion_id in self._used

    def mark_used(self, assertion_id: str) -> None:
        self._used.add(assertion_id)


class InMemoryCounterStore:
    """Minimal :class:`AnalyticsCounterStore` for tests / local wiring."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    async def increment(self, counter_name: str, amount: int = 1) -> int:
        self._counters[counter_name] = self._counters.get(counter_name, 0) + amount
        return self._counters[counter_name]

    async def read(self, counter_name: str) -> int:
        return self._counters.get(counter_name, 0)


class ReportService:
    """Report lifecycle, transactional signing, addenda, and version history."""

    def __init__(
        self,
        report_repo: ReportRepo,
        version_repo: VersionRepo,
        study_repo: StudyRepository,
        audit: AuditService,
        analytics: AnalyticsService,
        idempotency_cache: SignIdempotencyCache,
        assertion_store: SecondFactorAssertionStore,
        *,
        access_policy: StudyAccessPolicy | None = None,
        mfa_freshness_seconds: int = 300,
    ) -> None:
        self._repo = report_repo
        self._version_repo = version_repo
        self._study_repo = study_repo
        self._audit = audit
        self._analytics = analytics
        self._idempotency = idempotency_cache
        self._assertions = assertion_store
        self._policy = access_policy or StudyAccessPolicy()
        self._mfa_freshness_seconds = mfa_freshness_seconds

    # ------------------------------------------------------------------
    # create draft — POST /studies/{studyId}/reports
    # ------------------------------------------------------------------
    async def create_draft(
        self,
        user: AuthenticatedUser,
        study: StudyRecord,
        request: CreateReportRequest,
    ) -> ReportResponse:
        self._policy.assert_can_write(user, study)
        now = datetime.now(UTC)
        report_id = f"rp_{ULID()}"
        draft = ReportDraft(
            report_id=report_id,
            study_id=study.study_id,
            patient_key=study.patient_key,
            report_type=ReportType.ORIGINAL,
            status=ReportStatus.DRAFT,
            sections=ReportSections(sections=list(request.sections)),
            measurements=list(request.measurements),
            author=user.uid,
            created_at=now,
            updated_at=now,
            version=1,
        )
        await self._repo.create(draft)
        await self._version_repo.create_version(draft.to_version(1))
        await self._audit.record(
            "REPORT_DRAFT_CREATED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"reportId": report_id, "studyId": study.study_id},
            patient_key=study.patient_key,
        )
        await self._analytics.report_created()
        logger.info(
            "REPORT_DRAFT_CREATED",
            extra=redact({"report_id": report_id, "study_id": study.study_id}),
        )
        return self._to_response(draft)

    # ------------------------------------------------------------------
    # update draft — PATCH /reports/{reportId}
    # ------------------------------------------------------------------
    async def update_draft(
        self,
        user: AuthenticatedUser,
        report_id: str,
        request: UpdateReportRequest,
    ) -> ReportResponse:
        draft = await self._require_report(report_id)
        study = await self._require_study(draft.study_id)
        self._policy.assert_can_write(user, study)
        # Criterion 2: PATCH on SIGNED → 409 REPORT_IMMUTABLE (not 403).
        if draft.status == ReportStatus.SIGNED:
            raise ReportImmutableError("A signed report cannot be modified")
        if request.status is not None:
            validate_report_transition(draft.status, request.status)
            draft.status = request.status
        if request.sections is not None:
            draft.sections = ReportSections(sections=list(request.sections))
        if request.measurements is not None:
            draft.measurements = list(request.measurements)
        draft.updated_at = datetime.now(UTC)
        await self._repo.update(draft)
        await self._audit.record(
            "REPORT_DRAFT_UPDATED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"reportId": report_id, "studyId": draft.study_id, "status": draft.status.value},
            patient_key=study.patient_key,
        )
        logger.info(
            "REPORT_DRAFT_UPDATED",
            extra=redact({"report_id": report_id, "status": draft.status.value}),
        )
        return self._to_response(draft)

    # ------------------------------------------------------------------
    # sign — POST /reports/{reportId}/sign
    # ------------------------------------------------------------------
    async def sign(
        self,
        user: AuthenticatedUser,
        report_id: str,
        request: SignReportRequest,
        idem_key: str,
        assertion_header: str | None,
    ) -> tuple[dict[str, Any], int]:
        """Sign a report; returns ``(response_body, status_code)``.

        On an idempotency replay the original response is returned verbatim with
        no second audit event and no second 2FA check.
        """
        request_hash = self._hash_sign_request(report_id, request)
        cached = self._idempotency.get(user.uid, idem_key)
        if cached is not None:
            if cached.request_hash != request_hash:
                raise ConflictError("Idempotency-Key was used with a different request body")
            return dict(cached.response_body), cached.status_code

        # Criterion 5: fresh second-factor assertion.
        assertion_id = self._verify_assertion(assertion_header)
        if self._assertions.is_used(assertion_id):
            raise SecondFactorAssertionReplayedError()
        # Criterion 6: explicit attestation.
        if not request.attestation:
            raise AttestationRequiredError()

        draft = await self._require_report(report_id)
        study = await self._require_study(draft.study_id)
        # Criterion 13: only the assigned radiologist may sign.
        self._policy.assert_can_sign(user, study)
        # Criterion 1: state machine — DRAFT→SIGNED is forbidden.
        if draft.status == ReportStatus.SIGNED:
            raise ReportImmutableError("Report is already signed")
        validate_report_transition(draft.status, ReportStatus.SIGNED)

        now = datetime.now(UTC)
        content_hash = draft.compute_content_hash()
        signature = ReportSignature(
            signed_by=user.uid,
            signed_at=now,
            content_hash=content_hash,
            origin=SignatureOrigin.FRESH,
            attestation_id=assertion_id,
            second_factor_assertion_id=assertion_id,
        )
        draft.status = ReportStatus.SIGNED
        draft.signed_at = now
        draft.updated_at = now
        draft.signature = signature
        draft.version += 1
        signed_version = draft.to_version(draft.version)

        async def work(_txn: ReportTxn) -> ReportDraft:
            # ONE transaction: audit → report → version → worklist → analytics.
            event = await self._audit.record(
                "REPORT_SIGNED",
                actor=user.uid,
                second_factor=True,
                detail={
                    "reportId": report_id,
                    "studyId": draft.study_id,
                    "version": draft.version,
                    "contentHash": content_hash,
                    "reportType": draft.report_type.value,
                },
                patient_key=study.patient_key,
            )
            draft.audit_event_id = event.hash
            await _txn.set(REPORTS_COLLECTION, report_id, draft.model_dump(by_alias=True))
            await self._version_repo.create_version(signed_version)
            await self._update_worklist_and_study(_txn, study, draft)
            await self._analytics.report_signed()
            return draft

        signed = await self._repo.sign_transaction(work)
        self._assertions.mark_used(assertion_id)
        response = self._to_response(signed)
        body = response.model_dump(by_alias=True, mode="json")
        self._idempotency.record(user.uid, idem_key, request_hash, body, 200)
        logger.info(
            "REPORT_SIGNED",
            extra=redact({"report_id": report_id, "version": signed.version}),
        )
        return dict(body), 200

    # ------------------------------------------------------------------
    # addendum — POST /reports/{reportId}/addenda
    # ------------------------------------------------------------------
    async def create_addendum(
        self,
        user: AuthenticatedUser,
        parent_report_id: str,
        request: AddendumRequest,
        idem_key: str,
        assertion_header: str | None,
    ) -> tuple[dict[str, Any], int]:
        """Create a signed addendum to a SIGNED parent; never mutates the parent."""
        request_hash = self._hash_addendum_request(parent_report_id, request)
        cached = self._idempotency.get(user.uid, idem_key)
        if cached is not None:
            if cached.request_hash != request_hash:
                raise ConflictError("Idempotency-Key was used with a different request body")
            return dict(cached.response_body), cached.status_code

        # Criterion 9: same 2FA + attestation as signing.
        assertion_id = self._verify_assertion(assertion_header)
        if self._assertions.is_used(assertion_id):
            raise SecondFactorAssertionReplayedError()
        if not request.attestation:
            raise AttestationRequiredError()

        parent = await self._require_report(parent_report_id)
        # Criterion 9: addendum only on a SIGNED report.
        if parent.status != ReportStatus.SIGNED:
            raise InvalidReportTransitionError("An addendum may only amend a SIGNED report")
        study = await self._require_study(parent.study_id)
        self._policy.assert_can_write(user, study)

        now = datetime.now(UTC)
        addendum_id = f"rp_{ULID()}"
        addendum = ReportDraft(
            report_id=addendum_id,
            study_id=parent.study_id,
            patient_key=parent.patient_key,
            report_type=ReportType.ADDENDUM,
            status=ReportStatus.SIGNED,
            sections=ReportSections(sections=list(request.sections)),
            measurements=list(request.measurements),
            author=user.uid,
            created_at=now,
            updated_at=now,
            signed_at=now,
            amends=parent_report_id,
            version=1,
        )
        content_hash = addendum.compute_content_hash()
        addendum.signature = ReportSignature(
            signed_by=user.uid,
            signed_at=now,
            content_hash=content_hash,
            origin=SignatureOrigin.FRESH,
            attestation_id=assertion_id,
            second_factor_assertion_id=assertion_id,
        )
        addendum_version = addendum.to_version(1)

        async def work(_txn: ReportTxn) -> ReportDraft:
            event = await self._audit.record(
                "REPORT_ADDENDUM",
                actor=user.uid,
                second_factor=True,
                detail={
                    "reportId": addendum_id,
                    "amends": parent_report_id,
                    "studyId": parent.study_id,
                    "contentHash": content_hash,
                },
                patient_key=parent.patient_key,
            )
            addendum.audit_event_id = event.hash
            # The addendum is written — the parent is NEVER touched.
            await _txn.set(REPORTS_COLLECTION, addendum_id, addendum.model_dump(by_alias=True))
            await self._version_repo.create_version(addendum_version)
            await self._analytics.report_signed()
            return addendum

        signed_addendum = await self._repo.sign_transaction(work)
        self._assertions.mark_used(assertion_id)
        response = self._to_response(signed_addendum)
        body = response.model_dump(by_alias=True, mode="json")
        self._idempotency.record(user.uid, idem_key, request_hash, body, 201)
        logger.info(
            "REPORT_ADDENDUM",
            extra=redact({"report_id": addendum_id, "amends": parent_report_id}),
        )
        return dict(body), 201

    # ------------------------------------------------------------------
    # reads — GET report / versions
    # ------------------------------------------------------------------
    async def get_report(
        self,
        user: AuthenticatedUser,
        report_id: str,
        viewer_scope: ViewerScope | None = None,
    ) -> ReportResponse:
        draft = await self._require_report(report_id)
        study = await self._require_study(draft.study_id)
        self._policy.assert_can_read(user, study, viewer_scope)
        return self._to_response(draft)

    async def get_versions(
        self,
        user: AuthenticatedUser,
        report_id: str,
        viewer_scope: ViewerScope | None = None,
    ) -> list[ReportVersionResponse]:
        draft = await self._require_report(report_id)
        study = await self._require_study(draft.study_id)
        self._policy.assert_can_read(user, study, viewer_scope)
        versions = await self._repo.list_versions(report_id)
        return [self._to_version_response(v) for v in versions]

    async def get_version(
        self,
        user: AuthenticatedUser,
        report_id: str,
        version: int,
        viewer_scope: ViewerScope | None = None,
    ) -> ReportVersionResponse:
        draft = await self._require_report(report_id)
        study = await self._require_study(draft.study_id)
        self._policy.assert_can_read(user, study, viewer_scope)
        record = await self._repo.get_version(report_id, version)
        if record is None:
            raise NotFoundError(f"Version {version} not found for report {report_id}")
        return self._to_version_response(record)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _require_report(self, report_id: str) -> ReportDraft:
        draft = await self._repo.get(report_id)
        if draft is None:
            raise NotFoundError(f"Report {report_id} not found")
        return draft

    async def _require_study(self, study_id: str) -> StudyRecord:
        study = await self._study_repo.get_study(study_id)
        if study is None:
            raise NotFoundError(f"Study {study_id} not found")
        return study

    def _verify_assertion(self, assertion_header: str | None) -> str:
        """Validate the ``X-Second-Factor-Assertion`` header; return the assertion id.

        Format: ``<assertionId>:<epochSeconds>``.  Missing or stale (> freshness
        window) → ``SecondFactorReassertionRequiredError``.
        """
        if not assertion_header:
            raise SecondFactorReassertionRequiredError()
        parts = assertion_header.split(":", 1)
        if len(parts) != 2 or not parts[0]:
            raise SecondFactorReassertionRequiredError()
        try:
            issued_at = float(parts[1])
        except ValueError as exc:
            raise SecondFactorReassertionRequiredError() from exc
        if time.time() - issued_at > self._mfa_freshness_seconds:
            raise SecondFactorReassertionRequiredError(
                "Second-factor assertion is older than the freshness window"
            )
        return parts[0]

    def _hash_sign_request(self, report_id: str, request: SignReportRequest) -> str:
        payload = json.dumps(
            {"reportId": report_id, "body": request.model_dump(by_alias=True)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _hash_addendum_request(self, parent_report_id: str, request: AddendumRequest) -> str:
        payload = json.dumps(
            {"parentReportId": parent_report_id, "body": request.model_dump(by_alias=True)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    async def _update_worklist_and_study(
        self,
        txn: ReportTxn,
        study: StudyRecord,
        draft: ReportDraft,
    ) -> None:
        """Transactionally mark the study + worklist index as SIGNED (criterion 14)."""
        signed_at = draft.signed_at.isoformat() if draft.signed_at else None
        study_doc = await txn.get(STUDIES_COLLECTION, study.study_id)
        if study_doc is not None:
            updated_study = {
                **study_doc,
                "status": "SIGNED",
                "hasReport": True,
                "reportId": draft.report_id,
                "signedAt": signed_at,
            }
            await txn.set(STUDIES_COLLECTION, study.study_id, updated_study)
        worklist_doc = await txn.get(WORKLIST_COLLECTION, WORKLIST_DOC_ID)
        if worklist_doc is not None:
            items = worklist_doc.get("items", [])
            new_items: list[dict[str, Any]] = []
            for item in items:
                if item.get("studyId") == study.study_id:
                    new_items.append(
                        {
                            **item,
                            "status": "SIGNED",
                            "hasReport": True,
                            "reportId": draft.report_id,
                            "signedAt": signed_at,
                        }
                    )
                else:
                    new_items.append(item)
            await txn.set(
                WORKLIST_COLLECTION,
                WORKLIST_DOC_ID,
                {**worklist_doc, "items": new_items},
            )

    def _to_response(self, draft: ReportDraft) -> ReportResponse:
        return ReportResponse(
            report_id=draft.report_id,
            study_id=draft.study_id,
            report_type=draft.report_type,
            status=draft.status,
            sections=draft.sections.sections,
            measurements=draft.measurements,
            content_hash=draft.compute_content_hash(),
            author=draft.author,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            signed_at=draft.signed_at,
            signature=draft.signature,
            amends=draft.amends,
            version=draft.version,
        )

    def _to_version_response(self, version: ReportVersion) -> ReportVersionResponse:
        return ReportVersionResponse(
            report_id=version.report_id,
            version=version.version,
            sections=version.sections.sections,
            measurements=version.measurements,
            content_hash=version.content_hash,
            author=version.author,
            created_at=version.created_at,
            status=version.status,
            signature=version.signature,
        )


__all__ = [
    "InMemoryCounterStore",
    "ReportService",
    "SecondFactorAssertionStore",
    "SignIdempotencyCache",
    "validate_report_transition",
]
