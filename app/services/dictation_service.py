"""Dictation service — sessions, ordered segments, ``mutationId`` idempotency.

Dictation is the **sole clinical input** to report drafting (D5).  Segments are
the radiologist's clinical narrative about a named patient — ePHI — so this
service:

- stores segments ordered by ``at`` (capture timestamp), so out-of-order
  delivery over a flaky link still reconstructs the correct narrative;
- is idempotent on ``mutationId``: re-sending an applied segment is a no-op that
  returns the stored segment (acceptance criterion 10);
- **never logs segment text** — audit events and structured logs carry only
  ``sessionId`` / ``mutationId`` / ``source``, never the narrative.  The
  ``text`` field is ePHI and is redacted from logs by construction;
- erases sessions and segments with the study (§5.4, criterion 10).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import NotFoundError
from app.models.dictation import (
    DictationSegment,
    DictationSegmentCreate,
    DictationSession,
    DictationSessionCreate,
    DictationSessionResponse,
    DictationSource,
)
from app.models.study import StudyRecord
from app.repositories.base import DocumentStore
from app.repositories.study_repo import StudyRepository
from app.services.access_policy import StudyAccessPolicy
from app.services.audit_service import AuditService

logger = logging.getLogger("vurarad.dictation")

DICTATION_SESSIONS_COLLECTION = "dictation_sessions"
DICTATION_SEGMENTS_COLLECTION = "dictation_segments"


class DictationService:
    """Dictation session + segment capture with idempotent, ordered segments."""

    def __init__(
        self,
        store: DocumentStore,
        study_repo: StudyRepository,
        audit: AuditService,
        *,
        access_policy: StudyAccessPolicy | None = None,
    ) -> None:
        self._store = store
        self._study_repo = study_repo
        self._audit = audit
        self._policy = access_policy or StudyAccessPolicy()

    # -- start session (route 27) -------------------------------------------
    async def start_session(
        self,
        user: AuthenticatedUser,
        request: DictationSessionCreate,
    ) -> DictationSession:
        """Create a dictation session scoped to a study + report."""
        study = await self._require_study(request.study_id)
        self._policy.assert_can_write(user, study)
        now = datetime.now(UTC)
        session = DictationSession(
            session_id=f"dc_{ULID()}",
            study_id=study.study_id,
            report_id=request.report_id,
            uid=user.uid,
            operator_id=user.operator_id,
            device=request.device,
            started_at=now,
            tenant_id=study.tenant_id,
        )
        await self._store.set(
            DICTATION_SESSIONS_COLLECTION, session.session_id, session.model_dump()
        )
        # No text is ever logged — only the session id and study id.
        logger.info(
            "DICTATION_SESSION_STARTED",
            extra={"session_id": session.session_id, "study_id": study.study_id},
        )
        await self._audit.record(
            "DICTATION_SESSION_STARTED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"sessionId": session.session_id, "studyId": study.study_id},
            patient_key=study.patient_key,
        )
        return session

    # -- append segment (route 28) ------------------------------------------
    async def append_segment(
        self,
        user: AuthenticatedUser,
        session_id: str,
        request: DictationSegmentCreate,
    ) -> DictationSegment:
        """Append one segment, idempotent on ``mutationId``.

        Re-sending a segment with an already-applied ``mutationId`` is a no-op
        that returns the stored segment — the property that makes retry-on-flaky-
        link safe (acceptance criterion 10).
        """
        session = await self._require_session(session_id)
        study = await self._require_study(session.study_id)
        self._policy.assert_can_write(user, study)
        doc_id = self._segment_doc_id(session_id, request.mutation_id)
        existing = await self._store.get(DICTATION_SEGMENTS_COLLECTION, doc_id)
        if existing is not None:
            # Idempotent replay — return the stored segment, no duplicate write.
            return self._segment_from_doc(existing)
        seq = await self._next_seq(session_id)
        segment = DictationSegment(
            session_id=session_id,
            mutation_id=request.mutation_id,
            seq=seq,
            at=request.at,
            source=request.source,
            text=request.text,
        )
        # Store with the study id on the segment so erasure can find it by study.
        payload = segment.model_dump()
        payload["study_id"] = study.study_id
        await self._store.set(DICTATION_SEGMENTS_COLLECTION, doc_id, payload)
        # No text is ever logged — only ids and source.
        logger.info(
            "DICTATION_SEGMENT_APPENDED",
            extra={
                "session_id": session_id,
                "mutation_id": request.mutation_id,
                "source": request.source.value,
            },
        )
        await self._audit.record(
            "DICTATION_SEGMENT_APPENDED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "sessionId": session_id,
                "mutationId": request.mutation_id,
                "source": request.source.value,
            },
            patient_key=study.patient_key,
        )
        return segment

    # -- get session (route 29) ---------------------------------------------
    async def get_session(
        self,
        user: AuthenticatedUser,
        session_id: str,
    ) -> DictationSessionResponse:
        """Return a session with its segments ordered by ``at``."""
        session = await self._require_session(session_id)
        study = await self._require_study(session.study_id)
        self._policy.assert_can_read(user, study)
        segments = await self._load_segments(session_id)
        return DictationSessionResponse(session=session, segments=segments)

    # -- erasure (criterion 10) ---------------------------------------------
    async def erase_for_study(self, study_id: str) -> int:
        """Erase every dictation session + segment for a study. Returns the count.

        Dictation segments are ePHI and are erased alongside the study (§5.4).
        """
        sessions = await self._store.query(
            DICTATION_SESSIONS_COLLECTION,
            where=[("study_id", "==", study_id)],
        )
        segments = await self._store.query(
            DICTATION_SEGMENTS_COLLECTION,
            where=[("study_id", "==", study_id)],
        )
        for sid, _doc in sessions:
            await self._store.delete(DICTATION_SESSIONS_COLLECTION, sid)
        for seg_id, _doc in segments:
            await self._store.delete(DICTATION_SEGMENTS_COLLECTION, seg_id)
        return len(sessions) + len(segments)

    # -- helpers -------------------------------------------------------------
    async def _require_study(self, study_id: str) -> StudyRecord:
        study = await self._study_repo.get_study(study_id)
        if study is None:
            raise NotFoundError(f"Study {study_id} not found")
        return study

    async def _require_session(self, session_id: str) -> DictationSession:
        doc = await self._store.get(DICTATION_SESSIONS_COLLECTION, session_id)
        if doc is None:
            raise NotFoundError(f"Dictation session {session_id} not found")
        return DictationSession.model_validate(doc)

    async def _load_segments(self, session_id: str) -> list[DictationSegment]:
        rows = await self._store.query(
            DICTATION_SEGMENTS_COLLECTION,
            where=[("session_id", "==", session_id)],
        )
        segments = [self._segment_from_doc(doc) for _id, doc in rows]
        # Ordered by `at` (capture timestamp), then mutationId for stability.
        segments.sort(key=lambda s: (s.at, s.mutation_id))
        return segments

    async def _next_seq(self, session_id: str) -> int:
        existing = await self._load_segments(session_id)
        return len(existing) + 1

    @staticmethod
    def _segment_doc_id(session_id: str, mutation_id: str) -> str:
        """Per-session scoped doc id — the idempotency key for a segment."""
        return f"{session_id}:{mutation_id}"

    @staticmethod
    def _segment_from_doc(doc: dict[str, Any]) -> DictationSegment:
        """Reconstruct a segment from a stored doc.

        The stored payload carries an erasure-only ``study_id`` key (so
        ``erase_for_study`` can find segments by study) that is not a
        ``DictationSegment`` field — strip it before validation.
        """
        return DictationSegment.model_validate({k: v for k, v in doc.items() if k != "study_id"})


__all__ = [
    "DICTATION_SEGMENTS_COLLECTION",
    "DICTATION_SESSIONS_COLLECTION",
    "DictationService",
    "DictationSource",
]
