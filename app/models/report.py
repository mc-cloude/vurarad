"""Report lifecycle models — drafts, versions, signatures, addenda (WP5).

A report progresses ``DRAFT → PENDING_SIGNATURE → SIGNED``.  Signing is a
transactional, fresh-second-factor-gated, attested operation that freezes the
report content under a **plain SHA-256 content hash** (never HMAC — see
``test_no_hmac.py``).  An addendum is a separate ``ADDENDUM``-typed report that
amends a ``SIGNED`` parent **without mutating the parent's sections, signature,
or content hash**.  Version history is append-only and ordered ascending.

All wire models use ``CamelModel`` so the JSON is camelCase with
``populate_by_name=True``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from app.models.common import CamelModel, UlidStr


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class ReportStatus(StrEnum):
    """Lifecycle of a report — only DRAFT→PENDING_SIGNATURE→SIGNED is legal."""

    DRAFT = "DRAFT"
    PENDING_SIGNATURE = "PENDING_SIGNATURE"
    SIGNED = "SIGNED"


class ReportType(StrEnum):
    """ORIGINAL reports are the primary read; ADDENDA amend a signed parent."""

    ORIGINAL = "ORIGINAL"
    ADDENDUM = "ADDENDUM"


class SignatureOrigin(StrEnum):
    """Whether the signature was freshly asserted or migrated from a legacy system."""

    FRESH = "FRESH"
    MIGRATED = "MIGRATED"


# ---------------------------------------------------------------------------
# Content sub-models
# ---------------------------------------------------------------------------
class ReportSection(CamelModel):
    """One narrative section of a report (e.g. Findings, Impression)."""

    title: str
    body: str


class ReportSections(CamelModel):
    """Ordered container of report sections — the unit that is content-hashed."""

    sections: list[ReportSection] = []


class Measurement(CamelModel):
    """One clinical measurement on a report."""

    label: str
    value: float
    unit: str
    reference_range: str | None = None


# ---------------------------------------------------------------------------
# Signature + version history
# ---------------------------------------------------------------------------
class ReportSignature(CamelModel):
    """The cryptographic attestation that freezes a report at sign time.

    ``contentHash`` is a plain SHA-256 of the canonical JSON sections — NOT an
    HMAC.  ``attestationId`` and ``secondFactorAssertionId`` tie the signature
    to the fresh second-factor assertion and the radiologist's attestation.
    """

    signed_by: str
    signed_at: datetime
    content_hash: str
    origin: SignatureOrigin = SignatureOrigin.FRESH
    attestation_id: str
    second_factor_assertion_id: str


class ReportVersion(CamelModel):
    """An immutable snapshot of a report at a point in its lifecycle.

    Stored at ``reports/{reportId}/versions/{version}`` (modelled as the flat
    ``report_versions`` collection with doc id ``{reportId}__v{version}``).
    """

    version: int
    report_id: UlidStr
    sections: ReportSections
    measurements: list[Measurement] = []
    content_hash: str
    author: str
    created_at: datetime
    status: ReportStatus
    signature: ReportSignature | None = None


class Addendum(CamelModel):
    """An addendum report — amends a SIGNED parent without mutating it."""

    report_id: UlidStr  # the addendum's own id
    amends: UlidStr  # the parent report id being amended
    sections: ReportSections
    author: str
    created_at: datetime
    signature: ReportSignature | None = None


# ---------------------------------------------------------------------------
# The draft — the mutable working document at reports/{reportId}
# ---------------------------------------------------------------------------
class ReportDraft(CamelModel):
    """The mutable report document.

    ``compute_content_hash`` produces a plain SHA-256 over the canonical JSON of
    the sections — the value frozen into :class:`ReportSignature` at sign time.
    ``to_version`` snapshots the current state into an immutable
    :class:`ReportVersion`.
    """

    report_id: UlidStr
    study_id: str
    patient_key: str = ""
    report_type: ReportType = ReportType.ORIGINAL
    status: ReportStatus = ReportStatus.DRAFT
    sections: ReportSections = ReportSections()
    measurements: list[Measurement] = []
    author: str = ""
    created_at: datetime
    updated_at: datetime
    signed_at: datetime | None = None
    signature: ReportSignature | None = None
    amends: UlidStr | None = None
    audit_event_id: str | None = None
    version: int = 1  # current version ordinal; incremented on each saved state

    def compute_content_hash(self) -> str:
        """Plain SHA-256 of the canonical JSON of the sections (NOT HMAC)."""
        payload = json.dumps(
            [s.model_dump() for s in self.sections.sections],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_version(self, version_number: int) -> ReportVersion:
        """Snapshot the current draft state into an immutable version."""
        return ReportVersion(
            version=version_number,
            report_id=self.report_id,
            sections=self.sections,
            measurements=self.measurements,
            content_hash=self.compute_content_hash(),
            author=self.author,
            created_at=self.updated_at,
            status=self.status,
            signature=self.signature,
        )


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------
class CreateReportRequest(CamelModel):
    """Body for ``POST /studies/{studyId}/reports``."""

    sections: list[ReportSection] = []
    measurements: list[Measurement] = []


class UpdateReportRequest(CamelModel):
    """Body for ``PATCH /reports/{reportId}`` — partial update of a draft.

    A non-null ``status`` requests a state transition (e.g. DRAFT→
    PENDING_SIGNATURE); any other transition raises 409.
    """

    sections: list[ReportSection] | None = None
    measurements: list[Measurement] | None = None
    status: ReportStatus | None = None


class SignReportRequest(CamelModel):
    """Body for ``POST /reports/{reportId}/sign``.

    ``attestation`` must be true — a missing/false attestation yields 422
    ATTESTATION_REQUIRED.  The fresh second-factor assertion travels in the
    ``X-Second-Factor-Assertion`` header; the Idempotency-Key header is required.
    """

    attestation: bool = False


class AddendumRequest(CamelModel):
    """Body for ``POST /reports/{reportId}/addenda``.

    Addenda require the same fresh second-factor assertion and attestation as
    signing.  The addendum never mutates the parent report.
    """

    sections: list[ReportSection]
    measurements: list[Measurement] = []
    attestation: bool = False


class ReportResponse(CamelModel):
    """Response for GET / PATCH / sign / addendum report routes."""

    report_id: UlidStr
    study_id: str
    report_type: ReportType
    status: ReportStatus
    sections: list[ReportSection] = []
    measurements: list[Measurement] = []
    content_hash: str
    author: str
    created_at: datetime
    updated_at: datetime
    signed_at: datetime | None = None
    signature: ReportSignature | None = None
    amends: UlidStr | None = None
    version: int = 1


class ReportVersionResponse(CamelModel):
    """Response for ``GET /reports/{reportId}/versions[/{version}]``."""

    report_id: UlidStr
    version: int
    sections: list[ReportSection] = []
    measurements: list[Measurement] = []
    content_hash: str
    author: str
    created_at: datetime
    status: ReportStatus
    signature: ReportSignature | None = None


__all__ = [
    "Addendum",
    "AddendumRequest",
    "CreateReportRequest",
    "Measurement",
    "ReportDraft",
    "ReportResponse",
    "ReportSection",
    "ReportSections",
    "ReportSignature",
    "ReportStatus",
    "ReportType",
    "ReportVersion",
    "ReportVersionResponse",
    "SignatureOrigin",
    "SignReportRequest",
    "UpdateReportRequest",
]
