"""Unit tests for the de-identification pipeline orchestration.

Covers acceptance criteria:
* 1 — the pixel pass (OCR) runs even when tag scrubbing reports no PHI tags.
* 3 — forced-modality modalities route every detected region to review.
* 5 — a classifier error / no-classification routes to review, never keep.
* 10 — an unvalidated (modality, manufacturer) source emits
  DEID_UNVALIDATED_SOURCE and forces review on every detected region.
* 11 — every run writes DEID_COMPLETED with the tag profile version, OCR engine
  version, PHI model id + revision, and region counts.
"""

from __future__ import annotations

import numpy as np
from pydicom import Dataset
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.repositories.base import InMemoryDocumentStore
from app.repositories.deid_repo import DeidLinkRepository, DeidReviewRepository
from app.services.audit_service import AuditService
from app.services.deid.decision import Decider
from app.services.deid.ocr import BBox
from app.services.deid.phi_ner import DeterministicPhiClassifier
from app.services.deid.pipeline import DeidPipeline, DeidRunConfig
from app.services.deid.redact import Redactor
from app.services.deid.review_queue import ReviewQueue
from app.services.deid.tags import TAG_PROFILE_VERSION, TagScrubber, UidRemapper
from tests.unit.deid_factory import FakeOcrEngine, InMemoryObjectStore, make_dataset


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------
class _Harness:
    """A fully-wired pipeline + its supporting doubles."""

    def __init__(self, *, ocr: FakeOcrEngine | None = None) -> None:
        self.remapper = UidRemapper("test-salt")
        self.doc_store = InMemoryDocumentStore()
        self.review_repo = DeidReviewRepository(self.doc_store)
        self.link_repo = DeidLinkRepository(self.doc_store)
        self.audit_mirror = InMemoryAuditMirror()
        self.object_store = InMemoryObjectStore()
        self.pipeline = DeidPipeline(
            scrubber=TagScrubber(self.remapper),
            ocr_engine=ocr or FakeOcrEngine(),
            classifier=DeterministicPhiClassifier(),
            decider=Decider(),
            redactor=Redactor(self.remapper),
            review_queue=ReviewQueue(self.review_repo),
            link_repo=self.link_repo,
            audit_service=AuditService(self.audit_mirror),
        )

    @property
    def audit_events(self) -> list:
        return self.audit_mirror._events  # type: ignore[attr-defined]


def _config(**overrides: object) -> DeidRunConfig:
    base: dict[str, object] = {
        "confidence_threshold": 0.9,
        "forced_review_modalities": frozenset(),
        "validated_sources": frozenset({("SC", "TestImaging")}),
        "require_pixel_pass": True,
        "deid_bucket_name": "deid-bucket",
    }
    base.update(overrides)
    return DeidRunConfig(**base)  # type: ignore[arg-type]


def _no_phi_dataset() -> Dataset:
    """A pixel-decodable dataset with NO PHI attributes (only Modality + pixels)."""
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5.1.1"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = "1.2.3.4.5.1.1"
    ds.Modality = "SC"
    ds.Rows = 16
    ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((16, 32), 220, dtype=np.uint8).tobytes()
    return ds


# ---------------------------------------------------------------------------
# Criterion 1 — pixel pass runs even when no PHI tags are found
# ---------------------------------------------------------------------------
class TestPixelPassAlwaysRuns:
    def test_ocr_runs_with_no_phi_tags(self) -> None:
        ds = _no_phi_dataset()
        ocr = FakeOcrEngine(regions={0: [("John Doe", BBox(0, 0, 20, 8))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds, modality="SC", manufacturer="TestImaging", config=_config()
        )
        # Tag scrubbing found no PHI attributes…
        assert outcome.phi_tag_found is False
        # …but the pixel pass still detected and processed the region.
        assert outcome.regions_total == 1
        assert ocr._call_count == 1

    def test_ocr_skipped_when_require_pixel_pass_false(self) -> None:
        ds = _no_phi_dataset()
        ocr = FakeOcrEngine(regions={0: [("x", BBox(0, 0, 5, 5))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds,
            modality="SC",
            manufacturer="TestImaging",
            config=_config(require_pixel_pass=False),
        )
        assert outcome.regions_total == 0
        assert ocr._call_count == 0


# ---------------------------------------------------------------------------
# Criterion 5 — fail-closed: classifier error / no classification → review
# ---------------------------------------------------------------------------
class TestFailClosed:
    def test_empty_text_region_routes_to_review(self) -> None:
        # The threshold engine returns empty text → UNKNOWN → review, never keep.
        ds = make_dataset()
        ocr = FakeOcrEngine(regions={0: [("", BBox(0, 0, 10, 6))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds, modality="SC", manufacturer="TestImaging", config=_config()
        )
        assert outcome.regions_review == 1
        assert outcome.regions_kept == 0
        # Fail-closed: the review region is also redacted (box-filled).
        assert outcome.regions_redacted == 1
        assert len(outcome.redact_boxes) == 1

    def test_unmatched_text_routes_to_review(self) -> None:
        ds = make_dataset()
        ocr = FakeOcrEngine(regions={0: [("zzz qqq", BBox(0, 0, 10, 6))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds, modality="SC", manufacturer="TestImaging", config=_config()
        )
        assert outcome.regions_review == 1
        assert outcome.regions_kept == 0


# ---------------------------------------------------------------------------
# Criterion 3 — forced modality routes every region to review
# ---------------------------------------------------------------------------
class TestForcedModality:
    def test_forced_modality_reviews_clinical_annotation(self) -> None:
        ds = make_dataset(modality="US")
        ocr = FakeOcrEngine(regions={0: [("LEFT", BBox(0, 0, 10, 6))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds,
            modality="US",
            manufacturer="TestImaging",
            config=_config(forced_review_modalities=frozenset({"US"})),
        )
        # LEFT is clinical, but US is forced → review, not keep.
        assert outcome.regions_review == 1
        assert outcome.regions_kept == 0
        assert outcome.region_results[0].reason == "FORCED_MODALITY"

    def test_non_forced_modality_keeps_clinical(self) -> None:
        ds = make_dataset(modality="CR")
        ocr = FakeOcrEngine(regions={0: [("LEFT", BBox(0, 0, 10, 6))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds,
            modality="CR",
            manufacturer="TestImaging",
            config=_config(
                forced_review_modalities=frozenset({"US"}),
                validated_sources=frozenset({("CR", "TestImaging")}),
            ),
        )
        assert outcome.regions_kept == 1
        assert outcome.regions_review == 0


# ---------------------------------------------------------------------------
# Criterion 10 — unvalidated source forces review + emits audit event
# ---------------------------------------------------------------------------
class TestUnvalidatedSource:
    async def test_unvalidated_source_reviews_all_regions(self) -> None:
        ds = make_dataset(modality="US", manufacturer="UnknownVendor")
        ocr = FakeOcrEngine(
            regions={
                0: [
                    ("LEFT", BBox(0, 0, 10, 6)),
                    ("John Doe", BBox(12, 0, 24, 6)),
                ]
            }
        )
        h = _Harness(ocr=ocr)
        result = await h.pipeline.run_instance(
            ds,
            run_id="run-1",
            study_id="s1",
            series_id="se1",
            source_object_path="studies/s1/i.dcm",
            original_sop_instance_uid="1.2.3.4.5.1.1",
            modality="US",
            manufacturer="UnknownVendor",
            config=_config(validated_sources=frozenset({("SC", "TestImaging")})),
            actor="rev1",
            second_factor=True,
            object_store=h.object_store,
        )
        assert result.unvalidated_source is True
        # Both regions reviewed (unvalidated forces review on every region).
        assert result.regions_review == 2
        assert result.regions_kept == 0
        # The unvalidated-source audit event was emitted.
        event_types = [e.event_type for e in h.audit_events]
        assert "DEID_UNVALIDATED_SOURCE" in event_types

    async def test_validated_source_does_not_force_review(self) -> None:
        ds = make_dataset(modality="SC", manufacturer="TestImaging")
        ocr = FakeOcrEngine(regions={0: [("LEFT", BBox(0, 0, 10, 6))]})
        h = _Harness(ocr=ocr)
        result = await h.pipeline.run_instance(
            ds,
            run_id="run-2",
            study_id="s1",
            series_id="se1",
            source_object_path="studies/s1/i.dcm",
            original_sop_instance_uid="1.2.3.4.5.1.1",
            modality="SC",
            manufacturer="TestImaging",
            config=_config(),
            actor="rev1",
            second_factor=True,
            object_store=h.object_store,
        )
        assert result.unvalidated_source is False
        assert result.regions_kept == 1
        event_types = [e.event_type for e in h.audit_events]
        assert "DEID_UNVALIDATED_SOURCE" not in event_types


# ---------------------------------------------------------------------------
# Criterion 11 — DEID_COMPLETED carries the audit fields
# ---------------------------------------------------------------------------
class TestAuditCompleted:
    async def test_emits_deid_completed_with_audit_fields(self) -> None:
        ds = make_dataset()
        ocr = FakeOcrEngine(
            regions={0: [("John Doe", BBox(0, 0, 20, 8)), ("LEFT", BBox(22, 0, 30, 8))]}
        )
        h = _Harness(ocr=ocr)
        result = await h.pipeline.run_instance(
            ds,
            run_id="run-3",
            study_id="s1",
            series_id="se1",
            source_object_path="studies/s1/i.dcm",
            original_sop_instance_uid="1.2.3.4.5.1.1",
            modality="SC",
            manufacturer="TestImaging",
            config=_config(),
            actor="rev1",
            second_factor=True,
            object_store=h.object_store,
        )
        completed = [e for e in h.audit_events if e.event_type == "DEID_COMPLETED"]
        assert len(completed) == 1
        detail = completed[0].detail
        assert detail["tag_profile_version"] == TAG_PROFILE_VERSION
        assert detail["ocr_engine_version"] == ocr.version
        assert detail["phi_model_id"] == result.phi_model_id
        assert detail["phi_model_revision"] == result.phi_model_revision
        assert detail["regions_total"] == 2
        assert detail["regions_redacted"] == result.regions_redacted
        assert detail["regions_kept"] == result.regions_kept
        assert detail["regions_review"] == result.regions_review
        assert detail["unvalidated_source"] is False

    async def test_completed_result_carries_audit_fields(self) -> None:
        ds = make_dataset()
        ocr = FakeOcrEngine(regions={0: [("John Doe", BBox(0, 0, 20, 8))]})
        h = _Harness(ocr=ocr)
        result = await h.pipeline.run_instance(
            ds,
            run_id="run-4",
            study_id="s1",
            series_id="se1",
            source_object_path="studies/s1/i.dcm",
            original_sop_instance_uid="1.2.3.4.5.1.1",
            modality="SC",
            manufacturer="TestImaging",
            config=_config(),
            actor="rev1",
            second_factor=True,
            object_store=h.object_store,
        )
        assert result.completed is True
        assert result.tag_profile_version == TAG_PROFILE_VERSION
        assert result.ocr_engine_version == ocr.version
        assert result.phi_model_id
        assert result.phi_model_revision
        assert result.new_sop_instance_uid is not None
        assert result.redacted_object_path is not None
        # The de-id link was persisted.
        link = await h.link_repo.get("studies/s1/i.dcm")
        assert link is not None
        assert link.new_sop_instance_uid == result.new_sop_instance_uid


# ---------------------------------------------------------------------------
# Redaction — REDACT and REVIEW boxes are both filled (fail-closed)
# ---------------------------------------------------------------------------
class TestRedaction:
    def test_phi_region_is_redacted(self) -> None:
        ds = make_dataset()
        ocr = FakeOcrEngine(regions={0: [("John Doe", BBox(0, 0, 20, 8))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds, modality="SC", manufacturer="TestImaging", config=_config()
        )
        assert outcome.regions_redacted == 1
        # The box region in the redacted dataset is blacked out.
        arr = ds.pixel_array
        assert int(arr[0:8, 0:20].max()) == 0

    def test_kept_region_pixels_preserved(self) -> None:
        ds = make_dataset(modality="CR")
        ocr = FakeOcrEngine(regions={0: [("LEFT", BBox(0, 0, 10, 6))]})
        h = _Harness(ocr=ocr)
        outcome = h.pipeline.process_instance(
            ds,
            modality="CR",
            manufacturer="TestImaging",
            config=_config(validated_sources=frozenset({("CR", "TestImaging")})),
        )
        assert outcome.regions_kept == 1
        assert outcome.regions_redacted == 0
        assert len(outcome.redact_boxes) == 0


# ---------------------------------------------------------------------------
# Processing failure — fail-closed result
# ---------------------------------------------------------------------------
async def test_processing_failure_yields_incomplete_result() -> None:
    ds = make_dataset()
    # An OCR engine that raises → process_instance raises → fail-closed result.
    ocr = FakeOcrEngine()

    def _boom(_image: np.ndarray) -> list:  # noqa: ARG001
        raise RuntimeError("ocr down")

    ocr.detect = _boom  # type: ignore[method-assign]
    h = _Harness(ocr=ocr)
    result = await h.pipeline.run_instance(
        ds,
        run_id="run-5",
        study_id="s1",
        series_id="se1",
        source_object_path="studies/s1/i.dcm",
        original_sop_instance_uid="1.2.3.4.5.1.1",
        modality="SC",
        manufacturer="TestImaging",
        config=_config(),
        actor="rev1",
        second_factor=True,
        object_store=h.object_store,
    )
    assert result.completed is False
    assert result.error is not None
    # Fail-closed: an un-redactable object is never released.
    assert result.new_sop_instance_uid is None
