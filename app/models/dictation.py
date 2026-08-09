"""Dictation capture — sessions and ordered segments (§3.21.3, §4.8).

Dictation is the **sole clinical input** to report drafting (D5): the drafting
prompt takes dictation + confirmed findings + template + priors, never image
bytes.  Segments are the radiologist's clinical narrative about a named patient
— they are ePHI, stored in the study's residency region, redacted from logs, and
erased with the study (acceptance criterion 10).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from app.models.common import CamelModel


class DictationSource(StrEnum):
    """How a dictation segment was captured."""

    SPEECH = "SPEECH"
    TYPED = "TYPED"


class DictationSession(CamelModel):
    """A dictation session header at ``dictation_sessions/{sessionId}``.

    ``sessionId`` is ``dc_<ulid>``.  A session is scoped to one study and one
    report; segments are ordered by ``at`` and idempotent on ``mutationId`` so a
    flaky-link retry never duplicates or reorders narrative.
    """

    session_id: str  # dc_<ulid>
    study_id: str
    report_id: str | None = None
    uid: str
    operator_id: str = ""
    device: str = ""
    started_at: datetime
    ended_at: datetime | None = None
    tenant_id: str = "default"


class DictationSegment(CamelModel):
    """One ordered dictation segment.

    Segments are ordered by ``at`` (the capture timestamp), not by insertion
    order, so out-of-order delivery over a flaky link still reconstructs the
    correct narrative.  ``mutationId`` is the idempotency key: re-sending an
    applied segment is a no-op that returns the stored segment.
    """

    session_id: str
    mutation_id: str  # idempotency key — re-send is a no-op
    seq: int  # zero-padded storage ordinal
    at: datetime  # capture timestamp — segments are ordered by this
    source: DictationSource
    text: str  # ePHI — never logged


class DictationSessionCreate(CamelModel):
    """Request body for ``POST /dictation/sessions``."""

    study_id: str
    report_id: str | None = None
    device: str = ""


class DictationSegmentCreate(CamelModel):
    """Request body for ``POST /dictation/sessions/{sessionId}/segments``."""

    mutation_id: str  # idempotency key (required)
    at: datetime  # capture timestamp — segments ordered by this
    source: DictationSource = DictationSource.SPEECH
    text: str


class DictationSessionResponse(CamelModel):
    """Response for ``GET /dictation/sessions/{sessionId}`` — session + ordered segments."""

    session: DictationSession
    segments: list[DictationSegment]  # ordered by ``at``, then mutationId


__all__ = [
    "DictationSegment",
    "DictationSegmentCreate",
    "DictationSession",
    "DictationSessionCreate",
    "DictationSessionResponse",
    "DictationSource",
]
