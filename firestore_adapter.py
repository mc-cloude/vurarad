"""
firestore_adapter.py
====================
VuraRAD — Firestore Naming Convention Adapter (Improvement I)

Converts between:
  - snake_case  (Python DTOs, BigQuery, TypeScript interfaces)
  - camelCase   (Firestore native document keys)

Usage:
    from firestore_adapter import to_firestore, from_firestore, from_firestore_doc

    # Write to Firestore:
    db.collection("studies").document(uid).set(to_firestore(study_dict))

    # Read from Firestore:
    raw = doc.to_dict()
    dto  = from_firestore(raw)
"""

import re
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSION UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _snake_to_camel(name: str) -> str:
    """Convert snake_case → camelCase. e.g. 'case_uid' → 'caseUid'"""
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def _camel_to_snake(name: str) -> str:
    """Convert camelCase → snake_case. e.g. 'caseUid' → 'case_uid'"""
    # Insert underscore before uppercase letters, then lowercase everything
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _convert_keys(obj: Any, key_fn) -> Any:
    """Recursively apply key_fn to all dict keys. Handles nested dicts and lists."""
    if isinstance(obj, dict):
        return {key_fn(k): _convert_keys(v, key_fn) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_keys(item, key_fn) for item in obj]
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def to_firestore(data: dict) -> dict:
    """
    Convert a snake_case Python dict to camelCase for Firestore storage.

    Example:
        to_firestore({'case_uid': 'R01', 'ai_confidence': 0.94})
        → {'caseUid': 'R01', 'aiConfidence': 0.94}
    """
    return _convert_keys(data, _snake_to_camel)


def from_firestore(data: dict) -> dict:
    """
    Convert a camelCase Firestore document dict to snake_case for Python DTOs.

    Example:
        from_firestore({'caseUid': 'R01', 'aiConfidence': 0.94})
        → {'case_uid': 'R01', 'ai_confidence': 0.94}
    """
    return _convert_keys(data, _camel_to_snake)


def from_firestore_doc(doc) -> dict:
    """
    Convenience wrapper: accepts a Firestore DocumentSnapshot,
    returns snake_case dict with `id` field injected.

    Example:
        doc = db.collection("studies").document("STRESS_01").get()
        study = from_firestore_doc(doc)
        # → {'id': 'STRESS_01', 'study_uid': 'STRESS_01', 'modality': 'CT', ...}
    """
    if not doc.exists:
        return {}
    data = from_firestore(doc.to_dict())
    data["id"] = doc.id
    return data


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN FIRESTORE ↔ DTO FIELD MAP
# (Handles legacy fields that don't follow clean camelCase conventions)
# ─────────────────────────────────────────────────────────────────────────────

# Explicit overrides for fields that were inconsistently named
_FIRESTORE_TO_DTO_OVERRIDES = {
    "studyUid":     "study_uid",
    "patientId":    "patient_id",
    "patientName":  "patient_name",
    "bodyPart":     "body_part",
    "studyDate":    "study_date",
    "aiModel":      "ai_model",
    "scanUrl":      "scan_url",
}

_DTO_TO_FIRESTORE_OVERRIDES = {v: k for k, v in _FIRESTORE_TO_DTO_OVERRIDES.items()}


def from_firestore_study(doc_dict: dict) -> dict:
    """
    Specialized converter for the `studies` collection.
    Uses override map to handle legacy field names, falls back to auto camelCase→snake.

    Output keys match the StudyDTO and TypeScript Study interface exactly.
    """
    result = {}
    
    # Pre-populate defaults to avoid Pydantic validation errors
    result["body_part"] = "UNKNOWN"
    result["critical"] = False
    result["ai_confidence"] = 0.0
    result["date"] = ""

    for k, v in doc_dict.items():
        snake_key = _FIRESTORE_TO_DTO_OVERRIDES.get(k, _camel_to_snake(k))

        # Handle Date stringification (Firestore DatetimeWithNanoseconds -> ISO string)
        if snake_key == "study_date":
            result["date"] = v.isoformat() if hasattr(v, 'isoformat') else str(v)
            continue

        # Flatten ai_triage nested object into top-level fields
        if snake_key == "ai_triage" and isinstance(v, dict):
            prio = v.get("priority", "ROUTINE")
            result["ai_priority"]    = prio
            result["critical"]       = (prio == "CRITICAL")
            result["ai_confidence"]  = v.get("confidence", 0.0)
            result["ai_finding"]     = v.get("finding", "")
            result["ai_model"]       = v.get("model", "VuraAI-V3")
            result["dso_seal"]       = v.get("dso_seal", "")
        else:
            # Direct mapping for other fields (modality, patient_name, etc.)
            # Some fields like 'status' map directly as 'status'
            result[snake_key] = v

    # Final fallback for study_uid/id mapping
    if "study_uid" in result and "id" not in result:
        result["id"] = result["study_uid"]

    return result


def to_firestore_study(dto: dict) -> dict:
    """
    Specialized converter for writing StudyDTO → Firestore studies collection.
    Re-nests ai_* fields back into ai_triage sub-document.
    """
    ai_triage = {}
    result = {}

    for k, v in dto.items():
        if k.startswith("ai_") and k not in ("ai_model",):
            triage_key = k[3:]  # strip "ai_"
            ai_triage[_snake_to_camel(triage_key)] = v
        else:
            fs_key = _DTO_TO_FIRESTORE_OVERRIDES.get(k, _snake_to_camel(k))
            result[fs_key] = v

    if ai_triage:
        result["aiTriage"] = ai_triage

    return result
