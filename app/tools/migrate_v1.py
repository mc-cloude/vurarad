"""WP8 migration tool — the six-step legacy migration of §5.6.2.

A one-shot, idempotent, **resumable** script run by a human with the
``vurarad-tf@`` service account.  It migrates the legacy vuraRAD deployment
(``studies/{StudyInstanceUID}`` documents keyed by the raw DICOM UID, with
fabricated ``ai_triage`` / ``accuracy_tier`` maps and no series subcollection)
into the new system.

The defining constraint (§5.6.2 step 3, acceptance criterion 2): **pixel data is
re-ingested through the real acquisition path** — ``POST /api/v1/uploads`` then
``POST /api/v1/uploads/{uploadId}/complete`` — never hand-copied into the DICOM
prefix and never hand-written as Firestore documents.  Only the real ingest path
runs the five admission validations, extracts geometry, and computes the
server-side ``stackIndex``.  A migration that hand-writes documents produces
data the ingest contract never validated.

The six steps, in order, each checkpointed so an interrupted re-run continues
rather than duplicating:

1. **Inventory** — export the legacy study/report list and classify every
   legacy study (``MIGRATABLE`` / ``NEEDS_REINGEST`` / ``DISCARD``).  No system
   writes; with ``--dry-run`` the script stops here.
2. **Re-ingest pixels** — push every migratable study's objects through the real
   upload + complete path; record the new ``studyId`` and write the permanent
   ``migration_map/{oldId}`` entry plus a ``STUDY_MIGRATED`` audit event.
3. **Migrate patient identifiers** — write ``patients/{patientKey}`` with a
   deterministic ``patientRef`` pseudonym and the MRN hash.
4. **Migrate reports** — preserve the legacy status; a legacy ``SIGNED`` report
   gets a version snapshot, a ``contentHash`` over its migrated content, and
   ``signatureOrigin: MIGRATED``.  Fabricated fields are discarded.
5. **Verify counts** — internal self-check of migrated vs inventory counts and
   that no fabricated field name survived.
6. **Write manifest** — emit ``migration-manifest.json`` summarising the run.

Fabricated legacy data — ``ai_triage``, ``ai_confidence``, ``accuracy_tier``,
and every radiogenomics / BigQuery artefact — is **discarded, not migrated**.
Migrating a ``TIER_1_HIGH`` label produced by an untrained model would launder
§0.7 into the new system.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from ulid import ULID

from app.models.ingest import IngestJob, IngestJobStatus, UploadSession
from app.models.series import StackOrderConfidence
from app.repositories.base import DocumentStore, FirestoreDocumentStore
from app.repositories.series_repo import SERIES_COLLECTION, SeriesRepository
from app.services.audit_service import AuditService
from app.storage.base import ObjectStore

logger = logging.getLogger("vurarad.migration")

# -- collections written by the migration (not by the ingest path) ------------
PATIENTS_COLLECTION = "patients"
REPORTS_COLLECTION = "reports"
REPORT_VERSIONS_COLLECTION = "report_versions"
MIGRATION_MAP_COLLECTION = "migration_map"

# -- classifications (§5.6.2 step 1) ------------------------------------------
MIGRATABLE = "MIGRATABLE"
NEEDS_REINGEST = "NEEDS_REINGEST"
DISCARD = "DISCARD"

# Ingest statuses that mean "this study's pixels are fully ingested" — a re-run
# skips any study whose ingest landed in one of these.
REPLAY_DONE: frozenset[str] = frozenset(
    {IngestJobStatus.SUCCEEDED.value, IngestJobStatus.DUPLICATE.value}
)

# Fabricated legacy field names that must never appear in migrated Firestore
# data (acceptance criterion 3).  Both snake_case (legacy) and camelCase (wire)
# variants are listed so a discard check is casing-robust.
FABRICATED_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "ai_triage",
        "aiTriage",
        "ai_confidence",
        "aiConfidence",
        "accuracy_tier",
        "accuracyTier",
        "radiogenomics",
        "radiogenomicsReport",
        "egfr",
        "egfr_prediction",
        "egfrPrediction",
        "kras",
        "kras_prediction",
        "krasPrediction",
        "tp53",
        "tp53_prediction",
        "tp53Prediction",
        "genomic_report",
        "genomicReport",
        "bigquery",
        "bigquery_table",
        "bigqueryTable",
        "fhir_diagnostic_report",
        "fhirDiagnosticReport",
        "tier_1_high",
        "tier1High",
        "radiomic_features",
        "radiomicFeatures",
        "triage_history",
        "triageHistory",
    }
)

DICOM_PREFIX = "dicom/"
DICOM_CONTENT_TYPE = "application/dicom"


# ---------------------------------------------------------------------------
# Legacy source shapes
# ---------------------------------------------------------------------------
@dataclass
class LegacyStudy:
    """One legacy ``studies/{StudyInstanceUID}`` document + its objects."""

    legacy_study_id: str  # the raw DICOM StudyInstanceUID used as the legacy key
    study_instance_uid: str
    patient_id: str | None  # MRN
    patient_name: str | None
    patient_sex: str | None
    patient_age_sex: str | None
    accession: str | None
    modality: str
    study_date: str | None
    object_count: int
    object_bytes: int
    series_count: int
    has_report: bool
    legacy_report_id: str | None
    source_lacks_position: bool
    fabricated_fields: list[str]


@dataclass
class LegacyReport:
    """One legacy report document."""

    legacy_report_id: str
    legacy_study_id: str
    status: str  # DRAFT | REPORTED | SIGNED
    version: int
    sections: dict[str, str]
    signed_by_uid: str | None
    signed_by_name: str | None
    signed_by_operator_id: str | None
    signed_at: str | None
    fabricated_fields: list[str]


@dataclass
class LegacyObject:
    """One legacy DICOM object to re-ingest.

    ``has_position`` records whether the object's series carries positional
    geometry (``ImagePositionPatient`` / ``SliceLocation``).  The migration
    derives ``source_lacks_position`` from it so verification can tell a
    legitimately ``UNVERIFIED`` series from one that should have been ordered.
    """

    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    data: bytes
    has_position: bool = True


@runtime_checkable
class LegacySource(Protocol):
    """Read-only view over the legacy deployment being migrated."""

    async def list_studies(self) -> list[LegacyStudy]: ...

    async def list_reports(self) -> list[LegacyReport]: ...

    async def get_objects(self, study_instance_uid: str) -> list[LegacyObject]: ...


# ---------------------------------------------------------------------------
# Ingest API client — drives the REAL acquisition path over HTTP
# ---------------------------------------------------------------------------
@runtime_checkable
class IngestApiClient(Protocol):
    """HTTP client for the real upload + complete ingest path (§3.7)."""

    async def create_upload(
        self,
        *,
        source_label: str,
        expected_object_count: int,
        expected_total_bytes: int,
        idempotency_key: str,
    ) -> UploadSession: ...

    async def complete_upload(
        self,
        *,
        upload_id: str,
        idempotency_key: str,
    ) -> IngestJob: ...

    async def aclose(self) -> None: ...


class HttpIngestClient:
    """Drive the real ingest path over HTTP using an ``httpx.AsyncClient``.

    In production the client points at the deployed API with a real bearer
    token; in tests it is wired with an ``ASGITransport`` against the in-process
    app so the same code exercises the real routes, auth, validation, and
    ``stackIndex`` computation end to end.
    """

    def __init__(self, client: httpx.AsyncClient, auth_token: str) -> None:
        self._client = client
        self._auth = {"Authorization": f"Bearer {auth_token}"}

    @staticmethod
    def _headers(idempotency_key: str, auth: Mapping[str, str]) -> dict[str, str]:
        return {"Idempotency-Key": idempotency_key, **auth}

    async def create_upload(
        self,
        *,
        source_label: str,
        expected_object_count: int,
        expected_total_bytes: int,
        idempotency_key: str,
    ) -> UploadSession:
        resp = await self._client.post(
            "/api/v1/uploads",
            json={
                "sourceLabel": source_label,
                "expectedObjectCount": expected_object_count,
                "expectedTotalBytes": expected_total_bytes,
            },
            headers=self._headers(idempotency_key, self._auth),
        )
        resp.raise_for_status()
        return UploadSession.model_validate(resp.json())

    async def complete_upload(
        self,
        *,
        upload_id: str,
        idempotency_key: str,
    ) -> IngestJob:
        resp = await self._client.post(
            f"/api/v1/uploads/{upload_id}/complete",
            headers=self._headers(idempotency_key, self._auth),
        )
        resp.raise_for_status()
        return IngestJob.model_validate(resp.json())

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Per-item migration state (serialised into the checkpoint)
# ---------------------------------------------------------------------------
@dataclass
class StudyMigration:
    """Resumable per-study migration state."""

    legacy_study_id: str
    study_instance_uid: str
    classification: str
    patient_id: str | None
    patient_name: str | None
    patient_sex: str | None
    patient_age_sex: str | None
    accession: str | None
    modality: str
    study_date: str | None
    object_count: int
    object_bytes: int
    series_count: int
    has_report: bool
    legacy_report_id: str | None
    source_lacks_position: bool
    fabricated_fields: list[str]
    new_study_id: str | None = None
    ingest_job_id: str | None = None
    ingest_status: str | None = None
    patient_key: str | None = None
    patient_ref: str | None = None
    patient_migrated: bool = False
    report_id: str | None = None
    report_migrated: bool = False

    @classmethod
    def from_legacy(cls, study: LegacyStudy, classification: str) -> StudyMigration:
        return cls(
            legacy_study_id=study.legacy_study_id,
            study_instance_uid=study.study_instance_uid,
            classification=classification,
            patient_id=study.patient_id,
            patient_name=study.patient_name,
            patient_sex=study.patient_sex,
            patient_age_sex=study.patient_age_sex,
            accession=study.accession,
            modality=study.modality,
            study_date=study.study_date,
            object_count=study.object_count,
            object_bytes=study.object_bytes,
            series_count=study.series_count,
            has_report=study.has_report,
            legacy_report_id=study.legacy_report_id,
            source_lacks_position=study.source_lacks_position,
            fabricated_fields=list(study.fabricated_fields),
        )


@dataclass
class ReportMigration:
    """Resumable per-report migration state."""

    legacy_report_id: str
    legacy_study_id: str
    status: str
    version: int
    sections: dict[str, str]
    signed_by_uid: str | None
    signed_by_name: str | None
    signed_by_operator_id: str | None
    signed_at: str | None
    fabricated_fields: list[str]
    report_id: str | None = None
    migrated: bool = False

    @classmethod
    def from_legacy(cls, report: LegacyReport) -> ReportMigration:
        return cls(
            legacy_report_id=report.legacy_report_id,
            legacy_study_id=report.legacy_study_id,
            status=report.status,
            version=report.version,
            sections=dict(report.sections),
            signed_by_uid=report.signed_by_uid,
            signed_by_name=report.signed_by_name,
            signed_by_operator_id=report.signed_by_operator_id,
            signed_at=report.signed_at,
            fabricated_fields=list(report.fabricated_fields),
        )


@dataclass
class VerifyCheck:
    """One verification result recorded in the checkpoint / manifest."""

    name: str
    passed: bool
    detail: str


@dataclass
class Checkpoint:
    """Resumable migration state, persisted as JSON between steps."""

    version: int = 1
    started_at: str = ""
    updated_at: str = ""
    steps: dict[str, bool] = field(
        default_factory=lambda: {
            "inventory": False,
            "ingest": False,
            "patients": False,
            "reports": False,
            "verify": False,
            "manifest": False,
        }
    )
    studies: dict[str, StudyMigration] = field(default_factory=dict)
    reports: dict[str, ReportMigration] = field(default_factory=dict)
    discarded: dict[str, int] = field(default_factory=dict)
    verify_passed: bool | None = None
    verify_checks: list[VerifyCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "steps": dict(self.steps),
            "studies": {k: asdict(v) for k, v in self.studies.items()},
            "reports": {k: asdict(v) for k, v in self.reports.items()},
            "discarded": dict(self.discarded),
            "verifyPassed": self.verify_passed,
            "verifyChecks": [asdict(c) for c in self.verify_checks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        studies_raw = data.get("studies", {})
        studies: dict[str, StudyMigration] = {}
        for key, value in studies_raw.items():
            studies[key] = StudyMigration(**value)
        reports_raw = data.get("reports", {})
        reports: dict[str, ReportMigration] = {}
        for key, value in reports_raw.items():
            reports[key] = ReportMigration(**value)
        checks_raw = data.get("verifyChecks", [])
        checks = [VerifyCheck(**c) for c in checks_raw]
        verify_passed = data.get("verifyPassed")
        return cls(
            version=_as_int(data.get("version", 1), 1),
            started_at=str(data.get("startedAt", "")),
            updated_at=str(data.get("updatedAt", "")),
            steps=dict(data.get("steps", {})),
            studies=studies,
            reports=reports,
            discarded=dict(data.get("discarded", {})),
            verify_passed=verify_passed if isinstance(verify_passed, bool) else None,
            verify_checks=checks,
        )


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def patient_ref_for(patient_identity: str) -> str:
    """Deterministic ``PT-`` + 7-digit pseudonym from a patient identifier.

    Stable across re-runs (acceptance criterion 4): the same MRN (or fallback
    identity) always yields the same ``patientRef``.  ``patientRef`` is a
    pseudonym, not de-identification (§4.7, review #17).
    """
    digest = hashlib.sha256(patient_identity.encode()).hexdigest()
    digits = str(int(digest[:16], 16) % 10_000_000).zfill(7)
    return f"PT-{digits}"


def mrn_hash_for(patient_identity: str) -> str:
    """SHA-256 of the MRN — the identity index stored at ``patients/{patientKey}``."""
    return hashlib.sha256(patient_identity.encode()).hexdigest()


def report_content_hash(sections: dict[str, str], signed_by_uid: str, signed_at: str) -> str:
    """SHA-256 over canonicalised sections plus signer identity and timestamp.

    Mirrors §3.8: ``contentHash`` is a SHA-256 over the canonicalised sections
    plus the signer identity and timestamp.  Integrity comes from the
    hash-chained audit trail, not a keyed MAC (the old HMAC scheme is deleted).
    """
    payload = json.dumps(
        {"sections": sections, "signedByUid": signed_by_uid, "signedAt": signed_at},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _patient_identity(study: StudyMigration) -> str:
    """Stable identity input for pseudonym derivation — MRN, else name, else id."""
    if study.patient_id:
        return study.patient_id
    if study.patient_name:
        return study.patient_name
    return study.legacy_study_id


def _classify(study: LegacyStudy) -> str:
    """Classify a legacy study (§5.6.2 step 1)."""
    if not study.study_instance_uid or study.object_count == 0:
        return DISCARD
    if study.source_lacks_position:
        return NEEDS_REINGEST
    return MIGRATABLE


# ---------------------------------------------------------------------------
# Migration runner — the six steps
# ---------------------------------------------------------------------------
class MigrationRunner:
    """Orchestrate the six-step migration, resumable via a JSON checkpoint."""

    def __init__(
        self,
        legacy_source: LegacySource,
        api_client: IngestApiClient,
        object_store: ObjectStore,
        doc_store: DocumentStore,
        audit_service: AuditService,
        checkpoint_path: Path,
        manifest_path: Path,
        *,
        actor: str = "vurarad-tf@migration",
        dry_run: bool = False,
    ) -> None:
        self._legacy = legacy_source
        self._api = api_client
        self._objects = object_store
        self._docs = doc_store
        self._audit = audit_service
        self._checkpoint_path = checkpoint_path
        self._manifest_path = manifest_path
        self._actor = actor
        self._dry_run = dry_run
        self._checkpoint = self._load_checkpoint()

    # -- checkpoint persistence ---------------------------------------------
    def _load_checkpoint(self) -> Checkpoint:
        if self._checkpoint_path.exists():
            data = json.loads(self._checkpoint_path.read_text())
            return Checkpoint.from_dict(data)
        return Checkpoint(started_at=_now_iso())

    def _save_checkpoint(self) -> None:
        self._checkpoint.updated_at = _now_iso()
        self._checkpoint_path.write_text(
            json.dumps(self._checkpoint.to_dict(), indent=2, sort_keys=True)
        )

    # -- public entry point --------------------------------------------------
    async def run(self) -> bool:
        """Run every not-yet-complete step in order; return the verify result."""
        await self._step_inventory()
        if self._dry_run:
            self._save_checkpoint()
            logger.info("dry-run complete — inventory written, no system writes")
            return True
        await self._step_ingest()
        await self._step_patients()
        await self._step_reports()
        await self._step_verify()
        await self._step_manifest()
        return self._checkpoint.verify_passed is True

    async def aclose(self) -> None:
        """Release the ingest API client (HTTP connection pool)."""
        await self._api.aclose()

    # -- step 1: inventory ---------------------------------------------------
    async def _step_inventory(self) -> None:
        if self._checkpoint.steps["inventory"]:
            return
        legacy_studies = await self._legacy.list_studies()
        legacy_reports = await self._legacy.list_reports()
        for study in legacy_studies:
            classification = _classify(study)
            study_entry = StudyMigration.from_legacy(study, classification)
            self._checkpoint.studies[study_entry.legacy_study_id] = study_entry
            for name in study.fabricated_fields:
                self._checkpoint.discarded[name] = self._checkpoint.discarded.get(name, 0) + 1
        for report in legacy_reports:
            entry = ReportMigration.from_legacy(report)
            self._checkpoint.reports[entry.legacy_report_id] = entry
            for name in report.fabricated_fields:
                self._checkpoint.discarded[name] = self._checkpoint.discarded.get(name, 0) + 1
        self._checkpoint.steps["inventory"] = True
        self._save_checkpoint()
        migratable = sum(
            1
            for s in self._checkpoint.studies.values()
            if s.classification in (MIGRATABLE, NEEDS_REINGEST)
        )
        logger.info(
            "inventory: %d studies (%d migratable, %d discard), %d reports",
            len(self._checkpoint.studies),
            migratable,
            sum(1 for s in self._checkpoint.studies.values() if s.classification == DISCARD),
            len(self._checkpoint.reports),
        )

    # -- step 2: re-ingest pixels through the real path ----------------------
    async def _step_ingest(self) -> None:
        if self._checkpoint.steps["ingest"]:
            return
        for study in list(self._checkpoint.studies.values()):
            if study.classification == DISCARD:
                continue
            await self._ingest_one(study)
        self._checkpoint.steps["ingest"] = True
        self._save_checkpoint()

    async def _ingest_one(self, study: StudyMigration) -> None:
        if study.ingest_status in REPLAY_DONE and study.new_study_id:
            return  # already re-ingested — resumable skip
        idem = f"migrate-v1:{study.legacy_study_id}"
        session = await self._api.create_upload(
            source_label=f"migration:{study.legacy_study_id}",
            expected_object_count=study.object_count,
            expected_total_bytes=study.object_bytes,
            idempotency_key=idem,
        )
        # Place the legacy objects under the quarantine prefix — the same thing
        # a real upload client does with the resumable session URLs.  The ingest
        # service then reads only header ranges and rewrites server-side; no
        # Firestore study/series document is hand-written by the migration.
        objects = await self._legacy.get_objects(study.study_instance_uid)
        for index, obj in enumerate(objects, start=1):
            key = f"{session.quarantine_prefix}{index:04d}.dcm"
            await self._objects.put(key, obj.data, DICOM_CONTENT_TYPE)
        job = await self._api.complete_upload(upload_id=session.upload_id, idempotency_key=idem)
        study.new_study_id = job.study_id
        study.ingest_job_id = job.job_id
        study.ingest_status = job.status.value
        # Permanent old → new map (§5.6.2 step 2), retained for historical lookups.
        await self._docs.set(
            MIGRATION_MAP_COLLECTION,
            study.legacy_study_id,
            {
                "legacyStudyId": study.legacy_study_id,
                "studyInstanceUid": study.study_instance_uid,
                "newStudyId": study.new_study_id,
                "ingestJobId": study.ingest_job_id,
                "ingestStatus": study.ingest_status,
                "migratedAt": _now_iso(),
            },
        )
        # Provenance audit event so the trail does not begin with an unexplained
        # population of studies (§5.6.2 step 5).
        await self._audit.record(
            "STUDY_MIGRATED",
            actor=self._actor,
            second_factor=False,
            detail={
                "migratedFrom": study.legacy_study_id,
                "studyId": study.new_study_id,
                "studyInstanceUid": study.study_instance_uid,
                "ingestJobId": study.ingest_job_id,
                "ingestStatus": study.ingest_status,
            },
            patient_key=mrn_hash_for(_patient_identity(study)),
        )
        self._save_checkpoint()
        logger.info(
            "re-ingested %s -> %s (%s, %d objects)",
            study.legacy_study_id,
            study.new_study_id,
            study.ingest_status,
            study.object_count,
        )

    # -- step 3: migrate patient identifiers ---------------------------------
    async def _step_patients(self) -> None:
        if self._checkpoint.steps["patients"]:
            return
        for study in list(self._checkpoint.studies.values()):
            if study.classification == DISCARD or not study.new_study_id:
                continue
            await self._migrate_patient_one(study)
        self._checkpoint.steps["patients"] = True
        self._save_checkpoint()

    async def _migrate_patient_one(self, study: StudyMigration) -> None:
        if study.patient_migrated:
            return  # resumable skip
        new_study_id = study.new_study_id
        if not new_study_id:
            return  # defensive — caller filters, but keep the type narrow
        identity = _patient_identity(study)
        patient_ref = patient_ref_for(identity)
        mrn_hash = mrn_hash_for(identity)
        existing = await self._docs.query(
            PATIENTS_COLLECTION, where=[("mrnHash", "==", mrn_hash)], limit=1
        )
        if existing:
            patient_key = existing[0][1]["patientKey"]
            prior_study_ids: list[str] = list(existing[0][1].get("studyIds", []))
            if new_study_id not in prior_study_ids:
                prior_study_ids.append(new_study_id)
            await self._docs.update(
                PATIENTS_COLLECTION,
                patient_key,
                {"studyIds": prior_study_ids},
            )
        else:
            patient_key = f"pk_{ULID()}"
            await self._docs.set(
                PATIENTS_COLLECTION,
                patient_key,
                {
                    "patientKey": patient_key,
                    "patientRef": patient_ref,
                    "mrnHash": mrn_hash,
                    "studyIds": [new_study_id],
                    "createdAt": _now_iso(),
                    "migratedFrom": study.legacy_study_id,
                },
            )
        study.patient_key = patient_key
        study.patient_ref = patient_ref
        study.patient_migrated = True
        # Backfill the permanent map with the resolved patient identity.
        await self._docs.update(
            MIGRATION_MAP_COLLECTION,
            study.legacy_study_id,
            {"patientKey": patient_key, "patientRef": patient_ref},
        )
        self._save_checkpoint()

    # -- step 4: migrate reports --------------------------------------------
    async def _step_reports(self) -> None:
        if self._checkpoint.steps["reports"]:
            return
        for report in list(self._checkpoint.reports.values()):
            await self._migrate_report_one(report)
        self._checkpoint.steps["reports"] = True
        self._save_checkpoint()

    async def _migrate_report_one(self, report: ReportMigration) -> None:
        if report.migrated:
            return  # resumable skip
        study = self._checkpoint.studies.get(report.legacy_study_id)
        if study is None or not study.new_study_id:
            logger.warning(
                "report %s skipped — parent study %s not migrated",
                report.legacy_report_id,
                report.legacy_study_id,
            )
            return
        new_study_id = study.new_study_id
        assert new_study_id is not None  # guarded above; narrows the type
        report_id = f"rp_{ULID()}"
        now = _now_iso()
        is_signed = report.status == "SIGNED"
        signer = (
            {
                "uid": report.signed_by_uid,
                "operatorId": report.signed_by_operator_id,
                "displayName": report.signed_by_name,
            }
            if is_signed and report.signed_by_uid
            else None
        )
        content_hash: str | None = None
        signature: dict[str, object] | None = None
        if is_signed and report.signed_by_uid and report.signed_at:
            content_hash = report_content_hash(
                report.sections, report.signed_by_uid, report.signed_at
            )
            signature = {
                "signedBy": signer,
                "signedAt": report.signed_at,
                "attestation": True,
                "contentHash": content_hash,
                # Marks this signature as migrated, not produced under the new
                # fresh-2FA rule — nobody mistakes it for a fresh sign (criterion 6).
                "signatureOrigin": "MIGRATED",
            }
        report_doc: dict[str, object] = {
            "reportId": report_id,
            "studyId": new_study_id,
            "status": report.status,
            "reportType": "PRIMARY",
            "amends": None,
            "sections": dict(report.sections),
            "measurements": [],
            "signature": signature,
            "signatureOrigin": "MIGRATED" if is_signed else None,
            "version": report.version,
            "createdAt": now,
            "updatedAt": now,
            "migratedFrom": report.legacy_report_id,
        }
        await self._docs.set(REPORTS_COLLECTION, report_id, report_doc)
        # A signed report gets an immutable version snapshot (§3.8 / criterion 6).
        if is_signed:
            version_id = f"{report_id}_v{report.version:03d}"
            await self._docs.set(
                REPORT_VERSIONS_COLLECTION,
                version_id,
                {
                    "reportId": report_id,
                    "studyId": new_study_id,
                    "version": report.version,
                    "status": report.status,
                    "sections": dict(report.sections),
                    "signature": signature,
                    "contentHash": content_hash,
                    "signatureOrigin": "MIGRATED",
                    "snapshotAt": now,
                    "migratedFrom": report.legacy_report_id,
                },
            )
        report.report_id = report_id
        report.migrated = True
        study.report_id = report_id
        study.report_migrated = True
        self._save_checkpoint()
        logger.info(
            "migrated report %s -> %s (status=%s)",
            report.legacy_report_id,
            report_id,
            report.status,
        )

    # -- step 5: verify counts (internal self-check) -------------------------
    async def _step_verify(self) -> None:
        if self._checkpoint.steps["verify"]:
            return
        checks = await self._collect_verify_checks()
        self._checkpoint.verify_checks = checks
        self._checkpoint.verify_passed = all(c.passed for c in checks)
        self._checkpoint.steps["verify"] = True
        self._save_checkpoint()
        for check in checks:
            level = logger.info if check.passed else logger.warning
            level(
                "verify %s: %s (%s)",
                "PASS" if check.passed else "FAIL",
                check.name,
                check.detail,
            )

    async def _collect_verify_checks(self) -> list[VerifyCheck]:
        checks: list[VerifyCheck] = []
        series_repo = SeriesRepository(self._docs)
        migratable = [
            s
            for s in self._checkpoint.studies.values()
            if s.classification in (MIGRATABLE, NEEDS_REINGEST)
        ]
        legacy_studies = len(migratable)
        legacy_series = sum(s.series_count for s in migratable)
        legacy_instances = sum(s.object_count for s in migratable)
        legacy_reports = sum(
            1
            for r in self._checkpoint.reports.values()
            if any(s.legacy_study_id == r.legacy_study_id for s in migratable)
        )
        legacy_objects = sum(s.object_count for s in migratable)

        # Migrated counts, derived from the real Firestore + object store.
        migrated_studies = 0
        migrated_series = 0
        migrated_instances = 0
        unverified_series: list[str] = []
        for study in migratable:
            if not study.new_study_id:
                continue
            series_list = await series_repo.get_series_for_study(study.new_study_id)
            if series_list:
                migrated_studies += 1
            for series in series_list:
                migrated_series += 1
                migrated_instances += series.instance_count
                if series.stack_order_confidence == StackOrderConfidence.UNVERIFIED and (
                    not study.source_lacks_position
                ):
                    unverified_series.append(series.series_id)

        migrated_reports = sum(1 for r in self._checkpoint.reports.values() if r.migrated)
        dicom_objs = await self._objects.list_prefix(DICOM_PREFIX, limit=200_000)
        migrated_objects = len(dicom_objs)

        checks.append(self._count_check("study_count", legacy_studies, migrated_studies))
        checks.append(self._count_check("series_count", legacy_series, migrated_series))
        checks.append(self._count_check("instance_count", legacy_instances, migrated_instances))
        checks.append(self._count_check("report_count", legacy_reports, migrated_reports))
        checks.append(self._count_check("dicom_object_count", legacy_objects, migrated_objects))

        # Report status distribution must match (criterion 5).
        legacy_dist = self._legacy_report_status_distribution(migratable)
        migrated_dist = await self._migrated_report_status_distribution()
        checks.append(
            VerifyCheck(
                name="report_status_distribution",
                passed=legacy_dist == migrated_dist,
                detail=f"legacy={legacy_dist} migrated={migrated_dist}",
            )
        )

        # No UNVERIFIED series unless the source genuinely lacks position data
        # (criterion 2).
        checks.append(
            VerifyCheck(
                name="no_unverified_without_cause",
                passed=not unverified_series,
                detail=(
                    "ok"
                    if not unverified_series
                    else f"unverified series without positional cause: {unverified_series}"
                ),
            )
        )

        # No fabricated field name anywhere in migrated Firestore (criterion 3).
        leaked = await self._scan_fabricated_fields()
        checks.append(
            VerifyCheck(
                name="no_fabricated_fields",
                passed=not leaked,
                detail="ok" if not leaked else f"leaked fields: {sorted(leaked)}",
            )
        )
        return checks

    @staticmethod
    def _count_check(name: str, expected: int, actual: int) -> VerifyCheck:
        return VerifyCheck(
            name=name,
            passed=expected == actual,
            detail=f"expected={expected} actual={actual}",
        )

    def _legacy_report_status_distribution(
        self, migratable: list[StudyMigration]
    ) -> dict[str, int]:
        dist: dict[str, int] = {}
        migratable_ids = {s.legacy_study_id for s in migratable}
        for report in self._checkpoint.reports.values():
            if report.legacy_study_id not in migratable_ids:
                continue
            dist[report.status] = dist.get(report.status, 0) + 1
        return dist

    async def _migrated_report_status_distribution(self) -> dict[str, int]:
        rows = await self._docs.query(REPORTS_COLLECTION, limit=10_000)
        dist: dict[str, int] = {}
        for _doc_id, doc in rows:
            status = str(doc.get("status", ""))
            dist[status] = dist.get(status, 0) + 1
        return dist

    async def _scan_fabricated_fields(self) -> set[str]:
        """Return any fabricated field names present in migrated Firestore docs."""
        leaked: set[str] = set()
        for collection in (
            PATIENTS_COLLECTION,
            REPORTS_COLLECTION,
            REPORT_VERSIONS_COLLECTION,
            MIGRATION_MAP_COLLECTION,
            SERIES_COLLECTION,
        ):
            rows = await self._docs.query(collection, limit=50_000)
            for _doc_id, doc in rows:
                leaked.update(self._find_fabricated_keys(doc))
        return leaked

    @staticmethod
    def _find_fabricated_keys(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, sub in value.items():
                if isinstance(key, str) and key in FABRICATED_FIELD_NAMES:
                    found.add(key)
                found.update(MigrationRunner._find_fabricated_keys(sub))
        elif isinstance(value, list):
            for item in value:
                found.update(MigrationRunner._find_fabricated_keys(item))
        return found

    # -- step 6: write manifest ---------------------------------------------
    async def _step_manifest(self) -> None:
        if self._checkpoint.steps["manifest"]:
            return
        migratable = [
            s
            for s in self._checkpoint.studies.values()
            if s.classification in (MIGRATABLE, NEEDS_REINGEST)
        ]
        manifest = {
            "version": 1,
            "startedAt": self._checkpoint.started_at,
            "completedAt": _now_iso(),
            "dryRun": self._dry_run,
            "actor": self._actor,
            "legacy": {
                "studies": len(self._checkpoint.studies),
                "migratable": len(migratable),
                "discarded": sum(
                    1 for s in self._checkpoint.studies.values() if s.classification == DISCARD
                ),
                "series": sum(s.series_count for s in migratable),
                "instances": sum(s.object_count for s in migratable),
                "reports": len(self._checkpoint.reports),
                "objects": sum(s.object_count for s in migratable),
                "reportStatusDistribution": self._legacy_report_status_distribution(migratable),
            },
            "discardedFields": dict(self._checkpoint.discarded),
            "verifyPassed": self._checkpoint.verify_passed,
            "verifyChecks": [asdict(c) for c in self._checkpoint.verify_checks],
            "studies": [
                {
                    "legacyStudyId": s.legacy_study_id,
                    "studyInstanceUid": s.study_instance_uid,
                    "classification": s.classification,
                    "newStudyId": s.new_study_id,
                    "ingestStatus": s.ingest_status,
                    "patientKey": s.patient_key,
                    "patientRef": s.patient_ref,
                    "reportId": s.report_id,
                    "reportMigrated": s.report_migrated,
                    "sourceLacksPosition": s.source_lacks_position,
                }
                for s in self._checkpoint.studies.values()
            ],
        }
        self._manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        self._checkpoint.steps["manifest"] = True
        self._save_checkpoint()
        logger.info("manifest written to %s", self._manifest_path)


# ---------------------------------------------------------------------------
# Production legacy source — reads the legacy Firestore + object store
# ---------------------------------------------------------------------------
class FirestoreLegacySource:
    """Read the legacy deployment: ``studies/{StudyInstanceUID}`` documents
    (snake_case, with fabricated maps) and the legacy DICOM objects.

    Legacy objects are grouped by ``StudyInstanceUID`` by parsing each object's
    header range with the same parser the ingest path uses, so the migration
    re-ingests exactly the objects that belonged to each legacy study.  Legacy
    reports are read from a legacy collection (the plan notes the real legacy
    deployment has none, so this is usually empty).
    """

    def __init__(
        self,
        legacy_doc_store: DocumentStore,
        legacy_object_store: ObjectStore,
        *,
        studies_collection: str = "studies",
        reports_collection: str = "reports",
        legacy_prefix: str = "legacy/dicom/",
    ) -> None:
        self._docs = legacy_doc_store
        self._objects = legacy_object_store
        self._studies_collection = studies_collection
        self._reports_collection = reports_collection
        self._prefix = legacy_prefix
        self._objects_by_study: dict[str, list[LegacyObject]] | None = None

    async def list_studies(self) -> list[LegacyStudy]:
        objects_by_study = await self._load_objects()
        report_index = await self._load_report_index()
        rows = await self._docs.query(self._studies_collection, limit=200_000)
        studies: list[LegacyStudy] = []
        for doc_id, doc in rows:
            objs = objects_by_study.get(doc_id, [])
            fabricated = [k for k in doc if isinstance(k, str) and k in FABRICATED_FIELD_NAMES]
            series_uids = {o.series_instance_uid for o in objs}
            lacks_position = bool(objs) and any(not o.has_position for o in objs)
            legacy_report_id = report_index.get(doc_id)
            studies.append(
                LegacyStudy(
                    legacy_study_id=doc_id,
                    study_instance_uid=str(doc.get("study_instance_uid", doc_id)),
                    patient_id=_opt_str(doc, "patient_id", "patientId"),
                    patient_name=_opt_str(doc, "patient_name", "patientName"),
                    patient_sex=_opt_str(doc, "patient_sex", "patientSex"),
                    patient_age_sex=_opt_str(
                        doc, "patient_age_sex", "patientAgeSex", "patient_age"
                    ),
                    accession=_opt_str(doc, "accession", "accession_number", "accessionNumber"),
                    modality=_opt_str(doc, "modality") or "CT",
                    study_date=_opt_str(doc, "study_date", "studyDate"),
                    object_count=len(objs),
                    object_bytes=sum(len(o.data) for o in objs),
                    series_count=len(series_uids),
                    has_report=legacy_report_id is not None,
                    legacy_report_id=legacy_report_id,
                    source_lacks_position=lacks_position,
                    fabricated_fields=fabricated,
                )
            )
        return studies

    async def list_reports(self) -> list[LegacyReport]:
        rows = await self._docs.query(self._reports_collection, limit=200_000)
        reports: list[LegacyReport] = []
        for doc_id, doc in rows:
            fabricated = [k for k in doc if isinstance(k, str) and k in FABRICATED_FIELD_NAMES]
            reports.append(
                LegacyReport(
                    legacy_report_id=doc_id,
                    legacy_study_id=str(doc.get("study_id", doc.get("study_instance_uid", ""))),
                    status=str(doc.get("status", "DRAFT")).upper(),
                    version=_as_int(doc.get("version", 1), 1),
                    sections=_extract_sections(doc),
                    signed_by_uid=_opt_str(doc, "signed_by_uid", "signedByUid"),
                    signed_by_name=_opt_str(doc, "signed_by_name", "signedByName"),
                    signed_by_operator_id=_opt_str(
                        doc, "signed_by_operator_id", "signedByOperatorId"
                    ),
                    signed_at=_opt_str(doc, "signed_at", "signedAt"),
                    fabricated_fields=fabricated,
                )
            )
        return reports

    async def get_objects(self, study_instance_uid: str) -> list[LegacyObject]:
        if self._objects_by_study is None:
            self._objects_by_study = await self._load_objects()
        return self._objects_by_study.get(study_instance_uid, [])

    async def _load_objects(self) -> dict[str, list[LegacyObject]]:
        if self._objects_by_study is not None:
            return self._objects_by_study
        from app.services.ingest_service import HEADER_BYTES, parse_dicom_headers

        refs = await self._objects.list_prefix(self._prefix, limit=200_000)
        grouped: dict[str, list[LegacyObject]] = {}
        for ref in refs:
            meta = await self._objects.object_metadata(ref.key)
            chunk = await self._objects.get_range(ref.key, 0, HEADER_BYTES)
            parsed = parse_dicom_headers(chunk, ref.key, meta.size)
            if parsed is None:
                continue
            grouped.setdefault(parsed.study_instance_uid, []).append(
                LegacyObject(
                    study_instance_uid=parsed.study_instance_uid,
                    series_instance_uid=parsed.series_instance_uid,
                    sop_instance_uid=parsed.sop_instance_uid,
                    data=await self._objects.get_blob(ref.key),
                    has_position=parsed.image_position_patient is not None,
                )
            )
        self._objects_by_study = grouped
        return grouped

    async def _load_report_index(self) -> dict[str, str]:
        reports = await self.list_reports()
        return {r.legacy_study_id: r.legacy_report_id for r in reports}


def _opt_str(doc: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = doc.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _extract_sections(doc: dict[str, object]) -> dict[str, str]:
    sections = doc.get("sections")
    if isinstance(sections, dict):
        return {str(k): str(v) for k, v in sections.items()}
    # Legacy reports often store findings/impression as flat snake_case fields.
    out: dict[str, str] = {}
    for key in ("findings", "impression", "clinical_history", "technique", "recommendations"):
        value = doc.get(key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_v1",
        description="WP8 six-step legacy migration (§5.6.2). Resumable via a JSON checkpoint.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run only step 1 (inventory); write no Firestore documents and ingest nothing",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=Path("migration-checkpoint.json"),
        help="path to the resumable checkpoint file",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("migration-manifest.json"),
        help="path to write the migration manifest",
    )
    parser.add_argument(
        "--api-base-url",
        default="http://localhost:8080",
        help="base URL of the deployed vuraRAD API (the real ingest path)",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="bearer token for the API (defaults to $MIGRATION_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--actor",
        default="vurarad-tf@migration",
        help="actor recorded on migration audit events",
    )
    parser.add_argument(
        "--legacy-prefix",
        default="legacy/dicom/",
        help="object-store prefix holding the legacy DICOM objects",
    )
    parser.add_argument(
        "--legacy-bucket",
        default=None,
        help="bucket holding the legacy objects (defaults to the pixel bucket)",
    )
    parser.add_argument(
        "--legacy-project",
        default=None,
        help="GCP project of the legacy Firestore (defaults to the new project)",
    )
    parser.add_argument(
        "--legacy-database",
        default="(default)",
        help="Firestore database of the legacy studies collection",
    )
    return parser


async def _build_runner(args: argparse.Namespace) -> MigrationRunner:
    from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
    from app.core.config import settings
    from app.storage.factory import build_object_store

    new_doc_store = FirestoreDocumentStore.from_settings(settings)
    new_object_store = build_object_store(settings)

    # Legacy source — a separate read-only view over the legacy deployment.
    legacy_doc_store = _build_legacy_doc_store(args, settings)
    legacy_object_store = build_object_store(settings, bucket_name=args.legacy_bucket)
    legacy_source = FirestoreLegacySource(
        legacy_doc_store,
        legacy_object_store,
        legacy_prefix=args.legacy_prefix,
    )

    audit_service = AuditService(InMemoryAuditMirror())

    if args.dry_run:
        api_client: IngestApiClient = _DryRunIngestClient()
    else:
        token = args.auth_token or _env("MIGRATION_AUTH_TOKEN")
        if not token:
            raise SystemExit(
                "MIGRATION_AUTH_TOKEN is required for a real run (or pass --auth-token)"
            )
        api_client = HttpIngestClient(httpx.AsyncClient(base_url=args.api_base_url), token)

    return MigrationRunner(
        api_client=api_client,
        audit_service=audit_service,
        checkpoint_path=args.checkpoint,
        doc_store=new_doc_store,
        legacy_source=legacy_source,
        manifest_path=args.manifest,
        object_store=new_object_store,
        actor=args.actor,
        dry_run=args.dry_run,
    )


def _build_legacy_doc_store(args: argparse.Namespace, settings: Any) -> DocumentStore:
    project = args.legacy_project or settings.gcp_project_id
    database = None if args.legacy_database == "(default)" else args.legacy_database
    from google.cloud.firestore import AsyncClient

    return FirestoreDocumentStore(AsyncClient(project=project, database=database))


def _env(name: str) -> str | None:
    import os

    return os.environ.get(name)


class _DryRunIngestClient:
    """No-op ingest client used only for ``--dry-run`` (step 1 writes nothing)."""

    async def create_upload(
        self,
        *,
        source_label: str,
        expected_object_count: int,
        expected_total_bytes: int,
        idempotency_key: str,
    ) -> UploadSession:
        raise RuntimeError("dry-run must not call the ingest path")

    async def complete_upload(self, *, upload_id: str, idempotency_key: str) -> IngestJob:
        raise RuntimeError("dry-run must not call the ingest path")

    async def aclose(self) -> None:
        return None


async def _amain(args: argparse.Namespace) -> bool:
    runner = await _build_runner(args)
    try:
        return await runner.run()
    finally:
        await runner.aclose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _build_arg_parser().parse_args(argv)
    ok = asyncio.run(_amain(args))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
