"""PHI field detection, hash_identifier, and recursive redact()."""

from __future__ import annotations

from app.core.redaction import (
    PHI_FIELD_NAMES,
    hash_identifier,
    is_phi_key,
    redact,
)


# ---------------------------------------------------------------------------
# Field-name set
# ---------------------------------------------------------------------------
def test_known_phi_fields_present() -> None:
    for field in ("patient_name", "patientName", "patient_mrn", "email", "address"):
        assert field in PHI_FIELD_NAMES


def test_is_phi_key() -> None:
    assert is_phi_key("patient_name") is True
    assert is_phi_key("patientName") is True
    assert is_phi_key("study_id") is False
    assert is_phi_key("diagnosis") is False


# ---------------------------------------------------------------------------
# hash_identifier
# ---------------------------------------------------------------------------
def test_hash_identifier_is_deterministic() -> None:
    assert hash_identifier("ABC123") == hash_identifier("ABC123")


def test_hash_identifier_is_truncated() -> None:
    assert len(hash_identifier("ABC123")) == 16


def test_hash_identifier_salt_changes_output() -> None:
    assert hash_identifier("ABC123") != hash_identifier("ABC123", salt="salty")


# ---------------------------------------------------------------------------
# redact() — recursion
# ---------------------------------------------------------------------------
def test_redact_top_level() -> None:
    record = {"patient_name": "John Doe", "study_id": "S1"}
    out = redact(record)
    assert out["patient_name"] == "[REDACTED]"
    assert out["study_id"] == "S1"


def test_redact_nested_dict() -> None:
    record = {"meta": {"patient_name": "Jane", "ok": 1}}
    out = redact(record)
    assert out["meta"]["patient_name"] == "[REDACTED]"
    assert out["meta"]["ok"] == 1


def test_redact_list_of_dicts() -> None:
    record = {"items": [{"patient_name": "A"}, {"study_id": "S"}]}
    out = redact(record)
    assert out["items"][0]["patient_name"] == "[REDACTED]"
    assert out["items"][1]["study_id"] == "S"


def test_redact_custom_replacement() -> None:
    out = redact({"patient_name": "X"}, replacement="<HIDDEN>")
    assert out["patient_name"] == "<HIDDEN>"


def test_redact_does_not_mutate_input() -> None:
    record = {"patient_name": "John", "nested": {"email": "a@b.c"}}
    redact(record)
    assert record["patient_name"] == "John"
    assert record["nested"]["email"] == "a@b.c"


def test_redact_camel_case_keys() -> None:
    out = redact({"patientBirthDate": "1970-01-01", "id": 7})
    assert out["patientBirthDate"] == "[REDACTED]"
    assert out["id"] == 7
