"""De-identification service — the API-facing layer over :class:`DeidPipeline`.

Fetches the source DICOM object from the object store, parses it, runs the
pipeline, and exposes the review-queue read/resolve operations.  The service is
the only place that connects the pipeline to I/O (object store, repositories,
audit); the pipeline itself stays pure-ish (one ``run_instance`` entrypoint with
optional storage).
"""

from __future__ import annotations

import logging
from io import BytesIO

from pydicom import dcmread
from ulid import ULID

from app.core.config import Settings
from app.repositories.deid_repo import DeidReview, ReviewStatus
from app.services.deid.pipeline import DeidPipeline, DeidResult, DeidRunConfig
from app.services.deid.review_queue import ReviewQueue
from app.storage.base import ObjectStore

logger = logging.getLogger("vurarad.deid.service")


class DeidService:
    """API-facing de-ID operations."""

    def __init__(
        self,
        pipeline: DeidPipeline,
        review_queue: ReviewQueue,
        object_store: ObjectStore,
        settings: Settings,
    ) -> None:
        self._pipeline = pipeline
        self._review_queue = review_queue
        self._object_store = object_store
        self._settings = settings

    def _run_config(self) -> DeidRunConfig:
        return DeidRunConfig(
            confidence_threshold=self._settings.deid_confidence_threshold,
            forced_review_modalities=self._settings.deid_forced_review_modalities,
            validated_sources=self._settings.deid_validated_source_pairs,
            require_pixel_pass=self._settings.deid_require_pixel_pass,
            deid_bucket_name=self._settings.deid_bucket_name,
        )

    async def run_instance(
        self,
        *,
        study_id: str,
        series_id: str,
        source_object_path: str,
        original_sop_instance_uid: str,
        modality: str,
        manufacturer: str,
        actor: str,
        second_factor: bool,
    ) -> DeidResult:
        blob = await self._object_store.get_blob(source_object_path)
        ds = dcmread(BytesIO(blob))
        run_id = str(ULID())
        return await self._pipeline.run_instance(
            ds,
            run_id=run_id,
            study_id=study_id,
            series_id=series_id,
            source_object_path=source_object_path,
            original_sop_instance_uid=original_sop_instance_uid,
            modality=modality,
            manufacturer=manufacturer,
            config=self._run_config(),
            actor=actor,
            second_factor=second_factor,
            object_store=self._object_store,
        )

    async def list_reviews(self, limit: int = 100) -> list[DeidReview]:
        return await self._review_queue.list_pending(limit)

    async def list_run_reviews(self, run_id: str, limit: int = 1000) -> list[DeidReview]:
        return await self._review_queue.list_by_run(run_id, limit)

    async def resolve_review(
        self,
        review_id: str,
        status: ReviewStatus,
        actor: str,
        note: str = "",
    ) -> DeidReview:
        return await self._review_queue.resolve(review_id, status, actor, note)
