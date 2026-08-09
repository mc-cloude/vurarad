"""Patient identity repository — the ONLY reader of patient names.

``patients/{patientKey}`` holds the identity index: ``patientRef``, MRN,
``patientName``, ``patientBirthDate``, and ``studyIds``.  This repository is
the single place in the codebase that reads those PHI fields, which is what
makes the structural PHI separation enforceable — a grep for ``patient_name``
in the repository layer finds exactly one reader.
"""

from __future__ import annotations

from app.models.study import PatientIdentity
from app.repositories.base import DocumentStore

PATIENTS_COLLECTION = "patients"


class PatientRepository:
    """Read patient identity from ``patients/{patientKey}``."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def get_identity(self, patient_key: str) -> PatientIdentity | None:
        """Return the patient identity, or ``None`` if the document is absent."""
        doc = await self._store.get(PATIENTS_COLLECTION, patient_key)
        if doc is None:
            return None
        return PatientIdentity(
            patient_name=doc.get("patientName", ""),
            patient_birth_date=doc.get("patientBirthDate", ""),
            mrn=doc.get("mrn", ""),
        )
