# ruff: noqa: B008
"""Integration tests for the WP8 migration (§5.6.2 acceptance criteria).

The migration runs end to end against a seeded legacy fixture: legacy
``studies/{StudyInstanceUID}`` documents carrying fabricated ``ai_triage`` /
``accuracy_tier`` maps, legacy reports (one SIGNED, one DRAFT), and legacy DICOM
objects.  Pixel data is re-ingested through the **real** acquisition path
(``POST /api/v1/uploads`` + ``/complete``) driven over an in-process ASGI
transport, so every migrated study passes the same five validations and gets a
server-computed ``stackIndex``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydicom import Dataset, dcmwrite
from pydicom.dataset import FileMetaDataset
from pydicom.uid import UID, ExplicitVRLittleEndian, generate_uid

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.main import create_app
from app.models.series import StackOrderConfidence
from app.repositories.base import InMemoryDocumentStore
from app.repositories.series_repo import SeriesRepository
from app.services.audit_service import AuditService
from app.storage.base import ObjectStore
from app.tools.migrate_v1 import (
    FABRICATED_FIELD_NAMES,
    MIGRATION_MAP_COLLECTION,
    PATIENTS_COLLECTION,
    REPORT_VERSIONS_COLLECTION,
    REPORTS_COLLECTION,
    HttpIngestClient,
    LegacyObject,
    LegacyReport,
    LegacyStudy,
    MigrationRunner,
    patient_ref_for,
)
from app.tools.verify_migration import MigrationVerifier
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user
from tests.integration.test_ingest_api import FakeObjectStore

CT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.2"
_AXIAL_IOP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

STUDY_A = "1.2.840.113619.2.55.3.604688119.971"
STUDY_B = "1.2.840.113619.2.55.3.604688119.972"
STUDY_C = "1.2.840.113619.2.55.3.604688119.973"
STUDY_D = "1.2.840.113619.2.55.3.604688119.974"  # DISCARD — no objects
SERIES_A = f"{STUDY_A}.series.1"
SERIES_B = f"{STUDY_B}.series.1"
SERIES_C = f"{STUDY_C}.series.1"


# ---------------------------------------------------------------------------
# DICOM helpers
# ---------------------------------------------------------------------------
def build_ct_instance(
    sop_uid: str,
    *,
    study_uid: str,
    series_uid: str,
    z: float,
    instance_number: int,
    with_position: bool = True,
) -> bytes:
    """Build a minimal valid CT Part 10 dataset (>= 1 KB)."""
    ds = Dataset()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = CT_SOP_CLASS
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "TEST123"
    ds.Modality = "CT"
    ds.InstanceNumber = instance_number
    if with_position:
        ds.ImagePositionPatient = [0.0, 0.0, z]
    ds.ImageOrientationPatient = list(_AXIAL_IOP)
    ds.Rows = 64
    ds.Columns = 64
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x00" * (64 * 64)

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = UID(CT_SOP_CLASS)
    file_meta.MediaStorageSOPInstanceUID = UID(sop_uid)
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()
    ds.file_meta = file_meta
    ds.preamble = b"\x00" * 128

    buf = BytesIO()
    dcmwrite(buf, ds)
    return buf.getvalue()


def _stack(uid: str, series_uid: str, n: int, *, with_position: bool) -> list[LegacyObject]:
    return [
        LegacyObject(
            study_instance_uid=uid,
            series_instance_uid=series_uid,
            sop_instance_uid=f"{uid}.sop.{i + 1}",
            data=build_ct_instance(
                f"{uid}.sop.{i + 1}",
                study_uid=uid,
                series_uid=series_uid,
                z=i * 5.0,
                instance_number=i + 1,
                with_position=with_position,
            ),
            has_position=with_position,
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Seeded legacy fixture
# ---------------------------------------------------------------------------
def _seeded_studies(obj_bytes: dict[str, int]) -> list[LegacyStudy]:
    return [
        LegacyStudy(
            legacy_study_id=STUDY_A,
            study_instance_uid=STUDY_A,
            patient_id="MRN-4471",
            patient_name="Doe^John",
            patient_sex="M",
            patient_age_sex="41 M",
            accession="ACC-A",
            modality="CT",
            study_date="2026-07-01",
            object_count=5,
            object_bytes=obj_bytes[STUDY_A],
            series_count=1,
            has_report=True,
            legacy_report_id="legacy-rp-A",
            source_lacks_position=False,
            fabricated_fields=["ai_triage", "ai_confidence", "accuracy_tier"],
        ),
        LegacyStudy(
            legacy_study_id=STUDY_B,
            study_instance_uid=STUDY_B,
            patient_id="MRN-4471",  # same patient as A
            patient_name="Doe^John",
            patient_sex="M",
            patient_age_sex="41 M",
            accession="ACC-B",
            modality="CT",
            study_date="2026-07-02",
            object_count=3,
            object_bytes=obj_bytes[STUDY_B],
            series_count=1,
            has_report=True,
            legacy_report_id="legacy-rp-B",
            source_lacks_position=False,
            fabricated_fields=["ai_triage"],
        ),
        LegacyStudy(
            legacy_study_id=STUDY_C,
            study_instance_uid=STUDY_C,
            patient_id="MRN-9999",
            patient_name="Roe^Jane",
            patient_sex="F",
            patient_age_sex="33 F",
            accession="ACC-C",
            modality="CT",
            study_date="2026-07-03",
            object_count=2,
            object_bytes=obj_bytes[STUDY_C],
            series_count=1,
            has_report=False,
            legacy_report_id=None,
            source_lacks_position=True,  # UNVERIFIED is legitimate for this study
            fabricated_fields=["accuracy_tier", "radiogenomics"],
        ),
        LegacyStudy(
            legacy_study_id=STUDY_D,
            study_instance_uid=STUDY_D,
            patient_id="MRN-DISCARD",
            patient_name="Ghost^Patient",
            patient_sex="",
            patient_age_sex=None,
            accession="ACC-D",
            modality="CT",
            study_date="2026-07-04",
            object_count=0,
            object_bytes=0,
            series_count=0,
            has_report=False,
            legacy_report_id=None,
            source_lacks_position=False,
            fabricated_fields=["ai_triage"],
        ),
    ]


def _seeded_reports() -> list[LegacyReport]:
    return [
        LegacyReport(
            legacy_report_id="legacy-rp-A",
            legacy_study_id=STUDY_A,
            status="SIGNED",
            version=2,
            sections={
                "findings": "No focal consolidation. 6 mm RLL nodule.",
                "impression": "6 mm RLL nodule; recommend follow-up CT.",
            },
            signed_by_uid="rad-1",
            signed_by_name="R. Chen",
            signed_by_operator_id="RAD-0114",
            signed_at="2026-07-01T10:00:00Z",
            fabricated_fields=["ai_confidence"],
        ),
        LegacyReport(
            legacy_report_id="legacy-rp-B",
            legacy_study_id=STUDY_B,
            status="DRAFT",
            version=1,
            sections={"findings": "Pending review."},
            signed_by_uid=None,
            signed_by_name=None,
            signed_by_operator_id=None,
            signed_at=None,
            fabricated_fields=["ai_triage"],
        ),
    ]


class SeededLegacySource:
    """In-memory LegacySource backed by the seeded fixture above."""

    def __init__(self) -> None:
        self._objects: dict[str, list[LegacyObject]] = {
            STUDY_A: _stack(STUDY_A, SERIES_A, 5, with_position=True),
            STUDY_B: _stack(STUDY_B, SERIES_B, 3, with_position=True),
            STUDY_C: _stack(STUDY_C, SERIES_C, 2, with_position=False),
        }
        obj_bytes = {uid: sum(len(o.data) for o in objs) for uid, objs in self._objects.items()}
        self._studies = _seeded_studies(obj_bytes)
        self._reports = _seeded_reports()

    async def list_studies(self) -> list[LegacyStudy]:
        return list(self._studies)

    async def list_reports(self) -> list[LegacyReport]:
        return list(self._reports)

    async def get_objects(self, study_instance_uid: str) -> list[LegacyObject]:
        return list(self._objects.get(study_instance_uid, []))


# ---------------------------------------------------------------------------
# Shared migration environment
# ---------------------------------------------------------------------------
@dataclass
class MigrationEnv:
    legacy: SeededLegacySource
    object_store: ObjectStore
    doc_store: InMemoryDocumentStore
    audit_mirror: InMemoryAuditMirror
    audit_service: AuditService
    api_client: HttpIngestClient
    _http: httpx.AsyncClient

    def runner(self, checkpoint: Path, manifest: Path, *, dry_run: bool = False) -> MigrationRunner:
        return MigrationRunner(
            legacy_source=self.legacy,
            api_client=self.api_client,
            object_store=self.object_store,
            doc_store=self.doc_store,
            audit_service=self.audit_service,
            checkpoint_path=checkpoint,
            manifest_path=manifest,
            dry_run=dry_run,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


@pytest.fixture
async def env() -> AsyncIterator[MigrationEnv]:
    object_store = FakeObjectStore()
    doc_store = InMemoryDocumentStore()
    audit_mirror = InMemoryAuditMirror()
    legacy = SeededLegacySource()

    application = create_app()
    application.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    application.state.audit_object_store = StubAuditStore(locked=True)
    application.state.object_store = object_store
    application.state.document_store = doc_store
    application.state.audit_mirror = audit_mirror

    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )
    built = MigrationEnv(
        legacy=legacy,
        object_store=cast(ObjectStore, object_store),
        doc_store=doc_store,
        audit_mirror=audit_mirror,
        audit_service=AuditService(audit_mirror),
        api_client=HttpIngestClient(http_client, VALID_TOKEN),
        _http=http_client,
    )
    try:
        yield built
    finally:
        await built.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _docs(doc_store: InMemoryDocumentStore, collection: str) -> list[dict[str, Any]]:
    rows = await doc_store.query(collection, limit=10_000)
    return [doc for _id, doc in rows]


def _find_fabricated(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str) and key in FABRICATED_FIELD_NAMES:
                found.add(key)
            found.update(_find_fabricated(sub))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_fabricated(item))
    return found


async def _scan_fabricated(doc_store: InMemoryDocumentStore) -> set[str]:
    leaked: set[str] = set()
    for collection in (
        PATIENTS_COLLECTION,
        REPORTS_COLLECTION,
        REPORT_VERSIONS_COLLECTION,
        MIGRATION_MAP_COLLECTION,
        "series",
    ):
        for doc in await _docs(doc_store, collection):
            leaked.update(_find_fabricated(doc))
    return leaked


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_full_migration_end_to_end(env: MigrationEnv, tmp_path: Path) -> None:
    runner = env.runner(tmp_path / "ckpt.json", tmp_path / "manifest.json")
    ok = await runner.run()
    assert ok, "internal verify must pass after a full migration"

    # Three migratable studies (D is DISCARD) → three permanent migration_map entries.
    maps = dict(await env.doc_store.query(MIGRATION_MAP_COLLECTION, limit=100))
    assert set(maps) == {STUDY_A, STUDY_B, STUDY_C}

    # Criterion 2: re-ingested through the real path → server-computed stackIndex,
    # RELIABLE confidence for studies with position data.
    repo = SeriesRepository(env.doc_store)
    series_a = await repo.get_series_for_study(maps[STUDY_A]["newStudyId"])
    assert len(series_a) == 1
    assert [i.stack_index for i in series_a[0].instances] == [0, 1, 2, 3, 4]
    assert series_a[0].stack_order_confidence == StackOrderConfidence.RELIABLE

    # A study whose source genuinely lacks position data is UNVERIFIED — allowed.
    series_c = await repo.get_series_for_study(maps[STUDY_C]["newStudyId"])
    assert series_c[0].stack_order_confidence == StackOrderConfidence.UNVERIFIED

    # Criterion 3: no fabricated field name survives anywhere in migrated Firestore.
    leaked = await _scan_fabricated(env.doc_store)
    assert not leaked, f"fabricated fields leaked: {leaked}"

    # Criterion 4: patients/{patientKey} with a deterministic patientRef; A and B
    # (same MRN) collapse into one patient doc carrying both studyIds.
    patients = await _docs(env.doc_store, PATIENTS_COLLECTION)
    assert len(patients) == 2  # MRN-4471 (A+B) and MRN-9999 (C)
    ref_4471 = patient_ref_for("MRN-4471")
    pt_4471 = next(p for p in patients if p["patientRef"] == ref_4471)
    assert set(pt_4471["studyIds"]) == {maps[STUDY_A]["newStudyId"], maps[STUDY_B]["newStudyId"]}
    assert pt_4471["patientRef"] == ref_4471
    assert pt_4471["mrnHash"]

    # Criterion 6: reports migrate with preserved status; SIGNED → contentHash +
    # version snapshot + signatureOrigin: MIGRATED.
    reports = {doc["migratedFrom"]: doc for doc in await _docs(env.doc_store, REPORTS_COLLECTION)}
    ra = reports["legacy-rp-A"]
    assert ra["status"] == "SIGNED"
    assert ra["signatureOrigin"] == "MIGRATED"
    assert str(ra["signature"]["contentHash"]).startswith("sha256:")
    versions = await _docs(env.doc_store, REPORT_VERSIONS_COLLECTION)
    assert any(
        v["reportId"] == ra["reportId"] and v["signatureOrigin"] == "MIGRATED" for v in versions
    )
    rb = reports["legacy-rp-B"]
    assert rb["status"] == "DRAFT"
    assert rb["signature"] is None

    # Migration provenance: one STUDY_MIGRATED audit event per migratable study.
    migrated_events = [e for e in env.audit_mirror._events if e.event_type == "STUDY_MIGRATED"]
    assert len(migrated_events) == 3

    assert (tmp_path / "manifest.json").exists()


async def test_resumable_interrupt_after_ingest_continues(
    env: MigrationEnv, tmp_path: Path
) -> None:
    ckpt, manifest = tmp_path / "ckpt.json", tmp_path / "manifest.json"
    # Run only inventory + ingest, then "interrupt".
    r1 = env.runner(ckpt, manifest)
    await r1._step_inventory()
    await r1._step_ingest()

    # A fresh runner resumes from the checkpoint — patients/reports/verify/manifest.
    r2 = env.runner(ckpt, manifest)
    ok = await r2.run()
    assert ok

    # No duplication: exactly one patient doc per MRN, two reports, three map entries.
    assert len(await _docs(env.doc_store, PATIENTS_COLLECTION)) == 2
    assert len(await _docs(env.doc_store, REPORTS_COLLECTION)) == 2
    assert len(await env.doc_store.query(MIGRATION_MAP_COLLECTION, limit=100)) == 3


async def test_rerun_is_idempotent(env: MigrationEnv, tmp_path: Path) -> None:
    ckpt, manifest = tmp_path / "ckpt.json", tmp_path / "manifest.json"
    await env.runner(ckpt, manifest).run()

    maps_before = dict(await env.doc_store.query(MIGRATION_MAP_COLLECTION, limit=100))
    study_ids_before = sorted(doc["newStudyId"] for doc in maps_before.values())
    patient_before = await _docs(env.doc_store, PATIENTS_COLLECTION)
    refs_before = sorted(doc["patientRef"] for doc in patient_before)

    ok = await env.runner(ckpt, manifest).run()
    assert ok

    maps_after = dict(await env.doc_store.query(MIGRATION_MAP_COLLECTION, limit=100))
    study_ids_after = sorted(doc["newStudyId"] for doc in maps_after.values())
    patient_after = await _docs(env.doc_store, PATIENTS_COLLECTION)
    refs_after = sorted(doc["patientRef"] for doc in patient_after)
    assert study_ids_before == study_ids_after  # no duplicate re-ingest
    assert refs_before == refs_after  # deterministic patientRef across re-runs


async def test_dry_run_writes_no_documents(env: MigrationEnv, tmp_path: Path) -> None:
    ckpt, manifest = tmp_path / "ckpt.json", tmp_path / "manifest.json"
    ok = await env.runner(ckpt, manifest, dry_run=True).run()
    assert ok

    for collection in (
        PATIENTS_COLLECTION,
        REPORTS_COLLECTION,
        REPORT_VERSIONS_COLLECTION,
        MIGRATION_MAP_COLLECTION,
        "series",
        "ingest_jobs",
        "uploads",
    ):
        assert not await _docs(env.doc_store, collection), f"dry-run wrote to {collection}"

    # The inventory checkpoint is the only artefact.
    assert ckpt.exists()


async def test_verify_migration_gate_passes_then_blocks_on_tamper(
    env: MigrationEnv, tmp_path: Path
) -> None:
    ckpt, manifest = tmp_path / "ckpt.json", tmp_path / "manifest.json"
    await env.runner(ckpt, manifest).run()

    verifier = MigrationVerifier(manifest, env.doc_store, env.object_store)
    result = await verifier.verify()
    assert result.passed
    assert result.exit_code() == 0

    # Tamper: launder a fabricated field back into a migrated patient document.
    patients = await env.doc_store.query(PATIENTS_COLLECTION, limit=10)
    pid, pd = patients[0]
    pd["ai_triage"] = {"priority": "CRITICAL", "confidence": 0.99}
    await env.doc_store.set(PATIENTS_COLLECTION, pid, pd)

    tampered = await MigrationVerifier(manifest, env.doc_store, env.object_store).verify()
    assert not tampered.passed
    assert tampered.exit_code() == 1
    assert any(c.name == "no_fabricated_fields" and not c.passed for c in tampered.checks)


async def test_verify_migration_blocks_on_count_mismatch(env: MigrationEnv, tmp_path: Path) -> None:
    ckpt, manifest = tmp_path / "ckpt.json", tmp_path / "manifest.json"
    await env.runner(ckpt, manifest).run()

    # Tamper: delete a migrated report so the report count drifts from the manifest.
    reports = await env.doc_store.query(REPORTS_COLLECTION, limit=10)
    await env.doc_store.delete(REPORTS_COLLECTION, reports[0][0])

    result = await MigrationVerifier(manifest, env.doc_store, env.object_store).verify()
    assert not result.passed
    assert result.exit_code() == 1
    assert any(c.name == "report_count" and not c.passed for c in result.checks)


async def test_verify_migration_gate_scans_migration_map(env: MigrationEnv, tmp_path: Path) -> None:
    """The operator-facing gate scans every collection, including the permanent
    migration_map — a fabricated field laundered into a map entry must block
    cutover, not slip past the gate."""
    ckpt, manifest = tmp_path / "ckpt.json", tmp_path / "manifest.json"
    await env.runner(ckpt, manifest).run()

    result = await MigrationVerifier(manifest, env.doc_store, env.object_store).verify()
    assert result.passed  # baseline: clean migration passes the gate

    # Tamper: launder a fabricated field into a permanent migration_map document.
    maps = await env.doc_store.query(MIGRATION_MAP_COLLECTION, limit=10)
    mid, md = maps[0]
    md["ai_triage"] = {"priority": "CRITICAL", "confidence": 0.99}
    await env.doc_store.set(MIGRATION_MAP_COLLECTION, mid, md)

    tampered = await MigrationVerifier(manifest, env.doc_store, env.object_store).verify()
    assert not tampered.passed
    assert tampered.exit_code() == 1
    assert any(c.name == "no_fabricated_fields" and not c.passed for c in tampered.checks)
