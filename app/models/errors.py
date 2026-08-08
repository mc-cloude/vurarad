"""Canonical error code enum — every API error resolves to a member here."""

from enum import StrEnum


class ErrorCode(StrEnum):
    # -- auth ----------------------------------------------------------------
    MISSING_TOKEN = "MISSING_TOKEN"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    FORBIDDEN = "FORBIDDEN"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    MFA_REQUIRED = "MFA_REQUIRED"
    MFA_ENROLMENT_REQUIRED = "MFA_ENROLMENT_REQUIRED"
    MFA_CHALLENGE_FAILED = "MFA_CHALLENGE_FAILED"
    MFA_EXPIRED = "MFA_EXPIRED"

    # -- validation ----------------------------------------------------------
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"
    PHI_ACCESS_FORBIDDEN = "PHI_ACCESS_FORBIDDEN"

    # -- domain --------------------------------------------------------------
    INSTANCE_CHUNK_TOO_LARGE = "INSTANCE_CHUNK_TOO_LARGE"
    FINDING_NOT_CONFIRMED = "FINDING_NOT_CONFIRMED"
    REPORT_ALREADY_SIGNED = "REPORT_ALREADY_SIGNED"
    REPORT_NOT_MODIFIABLE = "REPORT_NOT_MODIFIABLE"
    SIGNING_REQUIRES_FRESH_MFA = "SIGNING_REQUIRES_FRESH_MFA"

    # -- upstream ------------------------------------------------------------
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_BUDGET_EXCEEDED = "AI_BUDGET_EXCEEDED"
    AI_DISABLED = "AI_DISABLED"

    # -- de-identification ---------------------------------------------------
    DEID_PIXEL_PASS_FAILED = "DEID_PIXEL_PASS_FAILED"
    DEID_CONFIDENCE_LOW = "DEID_CONFIDENCE_LOW"

    # -- metering ------------------------------------------------------------
    SPEND_WARNING = "SPEND_WARNING"
    SPEND_OVERAGE = "SPEND_OVERAGE"
    SPEND_SUSPENDED = "SPEND_SUSPENDED"
    SPEND_CEILING_AI_DISABLED = "SPEND_CEILING_AI_DISABLED"
    LICENCE_EXPIRED = "LICENCE_EXPIRED"
    LICENCE_INVALID = "LICENCE_INVALID"
    LICENCE_GRACE_EXPIRED = "LICENCE_GRACE_EXPIRED"

    # -- audit ---------------------------------------------------------------
    AUDIT_CHAIN_BROKEN = "AUDIT_CHAIN_BROKEN"
