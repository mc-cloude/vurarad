"""Erasure service — right-to-be-forgotten with full audit retention.

``erase_patient`` deletes every PHI-bearing record for a patient across
Firestore collections and the pixel object store, **retains all audit records**
(the audit mirror is append-only and never deleted from), and writes a
``PATIENT_ERASED`` audit event proving the erasure happened.

Idempotency: a tombstone (``erasure_tombstones/{patientKey}``) is written after
a successful erasure.  A second call for the same patient returns ``200`` with
zero counts rather than ``404`` — the operator can safely retry.

The legacy implementation returned ``500``; this one returns ``200`` with a
per-collection deletion tally.
"""

from __future__ import annotations

import time
from typing import Any

from app.models.admin import ErasureResponse
from app.repositories.base import DocumentStore
from app.services.audit_service import AuditService
from app.storage.base import ObjectStore

# Firestore collections touched by erasure.
PATIENTS_COLLECTION = "patients"
STUDIES_COLLECTION = "studies"
SERIES_COLLECTION = "series"
REPORTS_COLLECTION = "reports"
REPORT_VERSIONS_COLLECTION = "report_versions"
WORKLIST_COLLECTION = "worklist_index"
TOMBSTONES_COLLECTION = "erasure_tombstones"

# GCS object prefix for DICOM objects of a study.
_GCS_STUDY_PREFIX = "studies/"


def _empty_tally() -> dict[str, int]:
    return {
        "patients": 0,
        "studies": 0,
        "series": 0,
        "reports": 0,
        "report_versions": 0,
        "gcs_objects": 0,
        "worklist_index": 0,
    }


class ErasureService:
    """Deletes all PHI for a patient while retaining the audit chain."""

    def __init__(
        self,
        doc_store: DocumentStore,
        object_store: ObjectStore,
        audit_service: AuditService,
    ) -> None:
        self._store = doc_store
        self._objects = object_store
        self._audit = audit_service

    async def erase_patient(
        self,
        patient_key: str,
        confirm_patient_ref: str,
        *,
        actor: str,
        second_factor: bool,
    ) -> ErasureResponse:
        patient = await self._store.get(PATIENTS_COLLECTION, patient_key)

        if patient is None:
            # Idempotent path — a prior erasure left a tombstone.
            tombstone = await self._store.get(TOMBSTONES_COLLECTION, patient_key)
            if tombstone is not None:
                return ErasureResponse(patient_key=patient_key, deleted=_empty_tally())
            from app.core.errors import PatientNotFoundError

            raise PatientNotFoundError(f"Patient {patient_key} not found")

        # Confirmation — the opaque ref must match the stored patient ref.
        stored_ref = str(patient.get("patientRef", ""))
        if stored_ref != confirm_patient_ref:
            from app.core.errors import ErasureConfirmationMismatchError

            raise ErasureConfirmationMismatchError(
                "confirmPatientRef does not match the stored patient reference"
            )

        deleted = _empty_tally()

        # 1. Studies for this patient → collect ids for cascading deletes.
        study_rows = await self._store.query(
            STUDIES_COLLECTION, where=[("patientKey", "==", patient_key)]
        )
        study_ids: list[str] = [doc_id for doc_id, _doc in study_rows]
        deleted["patients"] = 0  # patient deleted last

        # 2. Series + GCS objects per study.
        for study_id in study_ids:
            series_rows = await self._store.query(
                SERIES_COLLECTION, where=[("study_id", "==", study_id)]
            )
            for series_id, _doc in series_rows:
                await self._store.delete(SERIES_COLLECTION, series_id)
                deleted["series"] += 1

            # GCS DICOM objects for the study (prefix delete is robust).
            objs = await self._objects.list_prefix(f"{_GCS_STUDY_PREFIX}{study_id}/")
            for ref in objs:
                await self._objects.delete(ref.key)
                deleted["gcs_objects"] += 1

            await self._store.delete(STUDIES_COLLECTION, study_id)
            deleted["studies"] += 1

        # 3. Reports + report versions (keyed by patientKey).
        report_rows = await self._store.query(
            REPORTS_COLLECTION, where=[("patientKey", "==", patient_key)]
        )
        for report_id, _doc in report_rows:
            await self._store.delete(REPORTS_COLLECTION, report_id)
            deleted["reports"] += 1

        version_rows = await self._store.query(
            REPORT_VERSIONS_COLLECTION, where=[("patientKey", "==", patient_key)]
        )
        for version_id, _doc in version_rows:
            await self._store.delete(REPORT_VERSIONS_COLLECTION, version_id)
            deleted["report_versions"] += 1

        # 4. Worklist index — remove rows referencing the erased studies.
        worklist = await self._store.get(WORKLIST_COLLECTION, "current")
        if worklist is not None:
            items: list[dict[str, Any]] = list(worklist.get("items", []))
            erased_set = set(study_ids)
            kept = [item for item in items if item.get("studyId") not in erased_set]
            removed = len(items) - len(kept)
            if removed:
                worklist["items"] = kept
                worklist["count"] = len(kept)
                if "totalKnown" in worklist:
                    worklist["totalKnown"] = max(0, int(worklist["totalKnown"]) - removed)
                await self._store.set(WORKLIST_COLLECTION, "current", worklist)
                deleted["worklist_index"] = removed

        # 5. Patient identity document (the single PHI-bearing patient record).
        await self._store.delete(PATIENTS_COLLECTION, patient_key)
        deleted["patients"] = 1

        # 6. Tombstone — makes a second call idempotent (200, zero counts).
        await self._store.set(
            TOMBSTONES_COLLECTION,
            patient_key,
            {"patientKey": patient_key, "erasedAt": int(time.time())},
        )

        # 7. Audit — retains every prior record; appends PATIENT_ERASED.
        await self._audit.record(
            "PATIENT_ERASED",
            actor=actor,
            second_factor=second_factor,
            detail={"patientKey": patient_key, "deleted": dict(deleted)},
            patient_key=patient_key,
        )

        return ErasureResponse(patient_key=patient_key, deleted=deleted)
