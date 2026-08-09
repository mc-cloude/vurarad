"""Administration models — users, audit query/export, erasure, analytics.

Every model in this module is PHI-free.  :class:`AdminUser` deliberately carries
NO patient fields (no name, DOB, MRN) — only operator identity, role, MFA state,
and claims version.  Erasure responses carry a per-collection *count* tally, not
patient identifiers.
"""

from __future__ import annotations

from typing import Any, Literal

from app.models.common import CamelModel


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
class AdminUser(CamelModel):
    """An operator as seen by the admin console — NO PHI fields.

    ``claimsVersion`` is bumped on every role change and is paired with a
    ``revoke_refresh_tokens`` call so that a token minted before the change is
    rejected (401 TOKEN_REVOKED) on its next verification.
    """

    uid: str
    email: str | None = None
    operator_id: str = ""
    role: str = ""
    disabled: bool = False
    mfa_enrolled: bool = False
    last_sign_in_at: int | None = None  # epoch millis
    claims_version: int = 0


class UserListResponse(CamelModel):
    """Paginated user listing."""

    users: list[AdminUser]
    next_page_token: str | None = None


class RoleUpdateRequest(CamelModel):
    """Change a user's role.  Bumps ``claimsVersion`` + revokes tokens."""

    role: Literal["admin", "viewer", "radiologist"]


class DisableUserRequest(CamelModel):
    """Enable/disable a user account."""

    disabled: bool


# ---------------------------------------------------------------------------
# Erasure (compliance:purge)
# ---------------------------------------------------------------------------
class ErasureRequest(CamelModel):
    """Request to erase every record of a patient.

    ``confirmPatientRef`` must match the patient's stored ``patientRef`` — the
    opaque confirmation that the operator is erasing the intended patient.
    ``patientKey`` is taken from the opaque path segment and is optional here.
    """

    patient_key: str = ""
    confirm_patient_ref: str


class ErasureResponse(CamelModel):
    """Per-collection deletion tally for an erasure.

    ``deleted`` maps collection/object names to the number of records removed.
    The second (idempotent) call returns the same shape with zero counts.
    """

    patient_key: str
    deleted: dict[str, int]


# ---------------------------------------------------------------------------
# Audit query
# ---------------------------------------------------------------------------
class AuditFilter(CamelModel):
    """Filter parameters for an audit query.

    ``from_`` / ``to`` are epoch-second bounds (both required).  The window must
    not exceed 92 days.
    """

    from_: int | None = None
    to: int | None = None
    actor: str | None = None
    action: str | None = None
    patient_key: str | None = None
    study_id: str | None = None
    limit: int = 100
    page_token: str | None = None


class AuditEntry(CamelModel):
    """One audit event projected to a PHI-free wire shape."""

    event_id: str
    timestamp: int
    actor: str
    action: str
    resource: str = ""
    patient_key: str = ""
    study_id: str = ""
    details: dict[str, Any] = {}


class AuditQueryResponse(CamelModel):
    """Paginated audit query result with chain-verification status."""

    entries: list[AuditEntry]
    next_page_token: str | None = None
    total_count: int = 0
    chain_verified: bool = True


# ---------------------------------------------------------------------------
# Audit export
# ---------------------------------------------------------------------------
class AuditExportRequest(CamelModel):
    """Export a window of audit records as NDJSON to a locked bucket."""

    from_: int
    to: int
    reason: str
    recipient: str


class AuditExportResponse(CamelModel):
    """Result of an audit export — a 10-minute signed URL to the NDJSON file."""

    export_id: str
    signed_url: str
    expires_at: int  # epoch seconds
    record_count: int
    sha256: str


# ---------------------------------------------------------------------------
# Analytics dashboard
# ---------------------------------------------------------------------------
class AnalyticsDashboard(CamelModel):
    """PHI-free operational dashboard.

    No field carries patient identifiers — only aggregate counts and
    compliance metrics.
    """

    total_studies: int = 0
    total_reports: int = 0
    signed_reports: int = 0
    studies_by_modality: dict[str, int] = {}
    ai_usage: int = 0
    compliance: dict[str, Any] = {}
    monthly_trend: list[dict[str, Any]] = []
