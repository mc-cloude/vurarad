"""PHI redaction — field-name sets, hashing, and structured-log redaction.

Every PHI identifier that appears in a log or error must pass through this
module.  The set is exhaustive; a field appearing in a DICOM or FHIR payload
that is NOT here means the redactor cannot protect it.
"""

import hashlib
from typing import Any

# -- field names known to carry PHI -----------------------------------------
PHI_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "patient_name",
        "patientName",
        "patient_birth_date",
        "patientBirthDate",
        "patient_mrn",
        "patientMrn",
        "accession_number",
        "accessionNumber",
        "case_uid",
        "caseUid",
        "referring_physician",
        "referringPhysician",
        "email",
        "phone",
        "address",
        "patient_address",
        "patientAddress",
    }
)

_PHI_VALUE_PATTERNS = frozenset({"patient_name", "patientName"})


def hash_identifier(value: str, salt: str = "") -> str:
    """Deterministic SHA-256 hash for pseudonymisation, not anonymisation."""
    raw = f"{salt}:{value}" if salt else value
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_phi_key(key: str) -> bool:
    return key in PHI_FIELD_NAMES


def redact(record: dict[str, Any], replacement: str = "[REDACTED]") -> dict[str, Any]:
    """Recursively replace values under any key matching PHI_FIELD_NAMES.

    Returns a shallow copy — the input dict is not mutated.
    """
    result: dict[str, Any] = {}
    for k, v in record.items():
        if is_phi_key(k):
            result[k] = replacement
        elif isinstance(v, dict):
            result[k] = redact(v, replacement)
        elif isinstance(v, list):
            result[k] = [
                redact(item, replacement) if isinstance(item, dict) else item for item in v
            ]
        else:
            result[k] = v
    return result
