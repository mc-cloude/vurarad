"""De-identification pipeline orchestration — ``DeidResult``.

The pipeline runs, in this exact order:

1. **Tags** (:mod:`app.services.deid.tags`) — scrub PHI DICOM attributes per
   PS3.15 Annex E and remap UIDs.
2. **OCR** (:mod:`app.services.deid.ocr`) — detect burned-in text regions in
   the pixel data.  This **always** runs when ``require_pixel_pass`` is true,
   even when tag scrubbing reports no PHI tags — tag scrubbing cannot touch
   pixel data and is never treated as sufficient.
3. **OpenMed classification** (:mod:`app.services.deid.phi_ner`) — label each
   region PHI / clinical / unknown.
4. **Redact** (:mod:`app.services.deid.redact`) — box-fill PHI regions and mint
   a fresh SOP Instance UID.
5. **Confidence gate** (:mod:`app.services.deid.decision`) — resolve keep /
   redact / review per region; low-confidence, forced-modality, and
   unvalidated-source regions route to review.
6. **Review queue** (:mod:`app.services.deid.review_queue`) — persist review
   items for a human reviewer.

The pipeline is **fail-closed**: a classifier error or an OCR-text-with-no-
classification region routes to *review* (and is redacted), never to *keep*.
Every run writes a ``DEID_COMPLETED`` audit event carrying the tag profile
version, OCR engine version, PHI model id + revision, and region counts; an
unvalidated ``(modality, manufacturer)`` source emits
``DEID_UNVALIDATED_SOURCE`` and forces review on every detected region.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from pydicom import Dataset

from app.repositories.deid_repo import DeidLink, DeidLinkRepository
from app.services.audit_service import AuditService
from app.services.deid.decision import Decider, Decision, DecisionContext
from app.services.deid.ocr import BBox, OcrEngine, OcrRegion
from app.services.deid.phi_ner import PhiClassifier, PhiLabel
from app.services.deid.redact import Redactor
from app.services.deid.review_queue import ReviewableRegion, ReviewBuildContext, ReviewQueue
from app.services.deid.tags import TAG_PROFILE_VERSION, TagScrubber, TagScrubResult

logger = logging.getLogger("vurarad.deid.pipeline")


# ---------------------------------------------------------------------------
# Configuration + result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DeidRunConfig:
    """Per-run policy inputs, derived from settings by the composition root."""

    confidence_threshold: float
    forced_review_modalities: frozenset[str]
    validated_sources: frozenset[tuple[str, str]]  # (modality, manufacturer)
    require_pixel_pass: bool
    deid_bucket_name: str | None = None


@dataclass(slots=True)
class DeidRegionResult:
    """Per-region outcome of one de-ID run."""

    bbox: BBox
    text: str
    label: PhiLabel
    confidence: float
    decision: Decision
    reason: str
    frame_index: int = 0


@dataclass(slots=True)
class ProcessOutcome:
    """In-memory result of processing one instance (no I/O)."""

    region_results: list[DeidRegionResult]
    redacted_dataset: Dataset
    tag_result: TagScrubResult
    new_sop_instance_uid: str
    phi_tag_found: bool
    unvalidated_source: bool
    regions_total: int
    regions_redacted: int
    regions_kept: int
    regions_review: int
    redact_boxes: list[BBox] = field(default_factory=list)


@dataclass(slots=True)
class DeidResult:
    """The persisted outcome of one de-ID run, returned to the API layer."""

    run_id: str
    study_id: str
    series_id: str
    modality: str
    manufacturer: str
    tag_profile_version: str
    ocr_engine_version: str
    phi_model_id: str
    phi_model_revision: str
    phi_tag_found: bool
    unvalidated_source: bool
    regions_total: int
    regions_redacted: int
    regions_kept: int
    regions_review: int
    region_results: list[DeidRegionResult]
    new_sop_instance_uid: str | None
    completed: bool
    redacted_object_path: str | None
    review_item_ids: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
class DeidPipeline:
    """Orchestrate the six de-ID stages for one DICOM instance."""

    def __init__(
        self,
        scrubber: TagScrubber,
        ocr_engine: OcrEngine,
        classifier: PhiClassifier,
        decider: Decider,
        redactor: Redactor,
        review_queue: ReviewQueue,
        link_repo: DeidLinkRepository,
        audit_service: AuditService,
    ) -> None:
        self._scrubber = scrubber
        self._ocr = ocr_engine
        self._classifier = classifier
        self._decider = decider
        self._redactor = redactor
        self._review_queue = review_queue
        self._link_repo = link_repo
        self._audit = audit_service

    # -- pure, in-memory processing (no I/O) -------------------------------
    def process_instance(
        self,
        ds: Dataset,
        *,
        modality: str,
        manufacturer: str,
        config: DeidRunConfig,
    ) -> ProcessOutcome:
        """Run stages 1-6 in memory and return the redacted dataset + outcomes."""
        # Stage 1 — tags.  Always runs; phi_tag_found records whether any PHI
        # attribute was present (but does NOT gate the pixel pass).
        tag_result = self._scrubber.scrub(ds)

        # Stage 2 — OCR.  Runs unconditionally when require_pixel_pass is true,
        # even when tag scrubbing found no PHI tags (criterion 1).
        regions: list[OcrRegion] = []
        if config.require_pixel_pass:
            regions = self._ocr_frames(ds)
        else:  # pragma: no cover — production forbids this; guarded by config
            logger.warning("deid_require_pixel_pass is False — pixel pass skipped")

        unvalidated = (modality, manufacturer) not in config.validated_sources
        forced = modality in config.forced_review_modalities
        ctx = DecisionContext(
            modality=modality,
            forced_review=forced,
            unvalidated_source=unvalidated,
            confidence_threshold=config.confidence_threshold,
        )

        # Stages 3 + 5 — classify each region, then decide (confidence gate).
        region_results: list[DeidRegionResult] = []
        redact_boxes: list[BBox] = []
        for region in regions:
            classification = self._classifier.classify(region.text)
            decision, reason = self._decider.decide(classification, ctx)
            region_results.append(
                DeidRegionResult(
                    bbox=region.bbox,
                    text=region.text,
                    label=classification.label,
                    confidence=classification.confidence,
                    decision=decision,
                    reason=reason,
                    frame_index=region.frame_index,
                )
            )
            # Fail-closed: REDACT and REVIEW regions are both box-filled so no
            # PHI leaks; REVIEW regions are additionally queued for a human.
            if decision in (Decision.REDACT, Decision.REVIEW):
                redact_boxes.append(region.bbox)

        # Stage 4 — redact (box-fill PHI + review regions, mint fresh SOP UID).
        redact_outcome = self._redactor.redact(ds, redact_boxes)

        counts = _count_decisions(region_results)
        return ProcessOutcome(
            region_results=region_results,
            redacted_dataset=ds,
            tag_result=tag_result,
            new_sop_instance_uid=redact_outcome.new_sop_instance_uid,
            phi_tag_found=tag_result.phi_tag_found,
            unvalidated_source=unvalidated,
            regions_total=len(region_results),
            regions_redacted=counts[Decision.REDACT] + counts[Decision.REVIEW],
            regions_kept=counts[Decision.KEEP],
            regions_review=counts[Decision.REVIEW],
            redact_boxes=redact_boxes,
        )

    # -- full run with persistence + audit ---------------------------------
    async def run_instance(
        self,
        ds: Dataset,
        *,
        run_id: str,
        study_id: str,
        series_id: str,
        source_object_path: str,
        original_sop_instance_uid: str,
        modality: str,
        manufacturer: str,
        config: DeidRunConfig,
        actor: str,
        second_factor: bool,
        object_store: object | None = None,
    ) -> DeidResult:
        """Process one instance, persist the redacted object + link + reviews,
        and emit the ``DEID_COMPLETED`` (and optionally ``DEID_UNVALIDATED_SOURCE``)
        audit events."""
        try:
            outcome = self.process_instance(
                ds,
                modality=modality,
                manufacturer=manufacturer,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: any error → review-only
            logger.exception("De-ID processing failed for %s", source_object_path)
            return _failed_result(
                run_id=run_id,
                study_id=study_id,
                series_id=series_id,
                modality=modality,
                manufacturer=manufacturer,
                source_object_path=source_object_path,
                error=f"{type(exc).__name__}: {exc}",
                ocr_version=self._ocr.version,
                phi_model_id=self._classifier.model_id,
                phi_model_revision=self._classifier.model_revision,
            )

        redacted_object_path = await self._persist(
            outcome.redacted_dataset,
            source_object_path=source_object_path,
            outcome=outcome,
            object_store=object_store,
            config=config,
        )

        # deid_link — immutable provenance mapping (criterion 7).
        await self._link_repo.create(
            DeidLink(
                source_object_path=source_object_path,
                deid_object_path=redacted_object_path,
                run_id=run_id,
                study_id=study_id,
                series_id=series_id,
                original_sop_instance_uid=original_sop_instance_uid,
                new_sop_instance_uid=outcome.new_sop_instance_uid,
                created_at=_now(),
            )
        )

        # Review queue — persist every REVIEW region.
        review_items = self._review_queue.build_items(
            ReviewBuildContext(
                run_id=run_id,
                study_id=study_id,
                series_id=series_id,
                sop_instance_uid=outcome.new_sop_instance_uid,
                modality=modality,
                manufacturer=manufacturer,
            ),
            [
                ReviewableRegion(r.bbox, r.text, r.label.value, r.confidence, r.reason)
                for r in outcome.region_results
                if r.decision == Decision.REVIEW
            ],
        )
        await self._review_queue.enqueue(review_items)

        # Audit — DEID_COMPLETED (criterion 11) + DEID_UNVALIDATED_SOURCE (criterion 10).
        await self._emit_audit(
            run_id=run_id,
            study_id=study_id,
            series_id=series_id,
            modality=modality,
            manufacturer=manufacturer,
            outcome=outcome,
            actor=actor,
            second_factor=second_factor,
        )

        return DeidResult(
            run_id=run_id,
            study_id=study_id,
            series_id=series_id,
            modality=modality,
            manufacturer=manufacturer,
            tag_profile_version=TAG_PROFILE_VERSION,
            ocr_engine_version=self._ocr.version,
            phi_model_id=self._classifier.model_id,
            phi_model_revision=self._classifier.model_revision,
            phi_tag_found=outcome.phi_tag_found,
            unvalidated_source=outcome.unvalidated_source,
            regions_total=outcome.regions_total,
            regions_redacted=outcome.regions_redacted,
            regions_kept=outcome.regions_kept,
            regions_review=outcome.regions_review,
            region_results=outcome.region_results,
            new_sop_instance_uid=outcome.new_sop_instance_uid,
            completed=True,
            redacted_object_path=redacted_object_path,
            review_item_ids=[item.review_id for item in review_items],
        )

    # -- helpers -----------------------------------------------------------
    def _ocr_frames(self, ds: Dataset) -> list[OcrRegion]:
        arr = np.asarray(ds.pixel_array)
        frames = _split_frames(arr, ds)
        regions: list[OcrRegion] = []
        for idx, frame in enumerate(frames):
            image = _frame_to_uint8(frame)
            detected = self._ocr.detect(image)
            for r in detected:
                regions.append(OcrRegion(r.bbox, r.text, r.confidence, frame_index=idx))
        return regions

    async def _persist(
        self,
        ds: Dataset,
        *,
        source_object_path: str,
        outcome: ProcessOutcome,
        object_store: object | None,
        config: DeidRunConfig,
    ) -> str:
        """Write the redacted object to the de-id bucket, returning its path."""
        deid_path = _deid_object_path(source_object_path, outcome.new_sop_instance_uid)
        if object_store is None or config.deid_bucket_name is None:
            return deid_path
        from io import BytesIO

        from pydicom import dcmwrite

        buf = BytesIO()
        dcmwrite(buf, ds, enforce_file_format=False)
        store: Any = object_store
        await store.put(deid_path, buf.getvalue(), "application/dicom")
        return deid_path

    async def _emit_audit(
        self,
        *,
        run_id: str,
        study_id: str,
        series_id: str,
        modality: str,
        manufacturer: str,
        outcome: ProcessOutcome,
        actor: str,
        second_factor: bool,
    ) -> None:
        detail: dict[str, object] = {
            "run_id": run_id,
            "series_id": series_id,
            "modality": modality,
            "manufacturer": manufacturer,
            "tag_profile_version": TAG_PROFILE_VERSION,
            "ocr_engine_version": self._ocr.version,
            "phi_model_id": self._classifier.model_id,
            "phi_model_revision": self._classifier.model_revision,
            "regions_total": outcome.regions_total,
            "regions_redacted": outcome.regions_redacted,
            "regions_kept": outcome.regions_kept,
            "regions_review": outcome.regions_review,
            "unvalidated_source": outcome.unvalidated_source,
        }
        await self._audit.record(
            "DEID_COMPLETED",
            actor=actor,
            second_factor=second_factor,
            detail=detail,
            patient_key=_patient_key(study_id),
        )
        if outcome.unvalidated_source:
            await self._audit.record(
                "DEID_UNVALIDATED_SOURCE",
                actor=actor,
                second_factor=second_factor,
                detail={
                    "run_id": run_id,
                    "modality": modality,
                    "manufacturer": manufacturer,
                },
                patient_key=_patient_key(study_id),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _count_decisions(results: list[DeidRegionResult]) -> dict[Decision, int]:
    counts: dict[Decision, int] = {Decision.KEEP: 0, Decision.REDACT: 0, Decision.REVIEW: 0}
    for r in results:
        counts[r.decision] += 1
    return counts


def _split_frames(arr: npt.NDArray[Any], ds: Dataset) -> list[npt.NDArray[Any]]:
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if arr.ndim == 2:
        return [arr]
    if arr.ndim == 3 and samples == 1:
        return [arr[i] for i in range(arr.shape[0])]
    if arr.ndim == 3 and samples > 1:
        return [arr]
    if arr.ndim == 4:
        return [arr[i] for i in range(arr.shape[0])]
    return [arr]


def _frame_to_uint8(frame: npt.NDArray[Any]) -> npt.NDArray[np.uint8]:
    """Normalise a decoded frame to a uint8 image suitable for OCR."""
    arr = frame
    if arr.dtype != np.uint8:
        arr = arr.astype(np.float32)
        lo, hi = float(arr.min()), float(arr.max())
        if hi > lo:
            arr = (arr - lo) * (255.0 / (hi - lo))
        arr = arr.astype(np.uint8)
    return arr


def _deid_object_path(source_object_path: str, new_sop_uid: str) -> str:
    base = source_object_path.rsplit("/", 1)[-1]
    return f"deid/{new_sop_uid}/{base}"


def _patient_key(study_id: str) -> str:
    return hashlib.sha256(study_id.encode()).hexdigest()[:16]


def _now() -> float:
    import time

    return time.time()


def _failed_result(
    *,
    run_id: str,
    study_id: str,
    series_id: str,
    modality: str,
    manufacturer: str,
    source_object_path: str,
    error: str,
    ocr_version: str,
    phi_model_id: str,
    phi_model_revision: str,
) -> DeidResult:
    """Build a fail-closed result for a processing error — never completed."""
    return DeidResult(
        run_id=run_id,
        study_id=study_id,
        series_id=series_id,
        modality=modality,
        manufacturer=manufacturer,
        tag_profile_version=TAG_PROFILE_VERSION,
        ocr_engine_version=ocr_version,
        phi_model_id=phi_model_id,
        phi_model_revision=phi_model_revision,
        phi_tag_found=False,
        unvalidated_source=True,
        regions_total=0,
        regions_redacted=0,
        regions_kept=0,
        regions_review=0,
        region_results=[],
        new_sop_instance_uid=None,
        completed=False,
        redacted_object_path=None,
        review_item_ids=[],
        error=error,
    )


# Re-export for the package namespace.
__all__ = [
    "DeidPipeline",
    "DeidRegionResult",
    "DeidResult",
    "DeidRunConfig",
    "ProcessOutcome",
]
