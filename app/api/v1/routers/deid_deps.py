"""Dependency providers for the de-identification router.

Composition root for WP11: builds the UID remapper, tag scrubber, OCR engine,
PHI classifier, decider, redactor, review queue, link repository, audit service,
pipeline, and the API-facing :class:`DeidService`.  Tests override the OCR
engine / classifier via ``app.state.deid_ocr_engine`` /
``app.state.deid_classifier``; production selects the real engines via
``DEID_OCR_ENGINE`` / ``DEID_PHI_CLASSIFIER`` settings.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.api.v1.routers.acquisition_deps import (
    AuditServiceDep,
    DocumentStoreDep,
    ObjectStoreAcqDep,
)
from app.core.config import Settings
from app.repositories.deid_repo import DeidLinkRepository, DeidReviewRepository
from app.services.deid.decision import Decider
from app.services.deid.ocr import OcrEngine, PaddleOcrEngine, TesseractEngine, ThresholdOcrEngine
from app.services.deid.phi_ner import (
    DeterministicPhiClassifier,
    OpenMedPhiClassifier,
    PhiClassifier,
)
from app.services.deid.pipeline import DeidPipeline
from app.services.deid.redact import Redactor
from app.services.deid.review_queue import ReviewQueue
from app.services.deid.tags import TagScrubber, UidRemapper
from app.services.deid_service import DeidService


def _uid_remapper(settings: Settings) -> UidRemapper:
    # Deployment-scoped salt (not a raw secret) for deterministic UID
    # pseudonymisation within a deployment.
    salt = f"{settings.gcp_project_id}:{settings.firebase_project_id}:deid-uid"
    return UidRemapper(salt=salt)


def _build_ocr_engine(request: Request, settings: Settings) -> OcrEngine:
    override = getattr(request.app.state, "deid_ocr_engine", None)
    if override is not None:
        return cast(OcrEngine, override)
    choice = settings.deid_ocr_engine
    if choice == "paddle":
        return PaddleOcrEngine()
    if choice == "tesseract":
        return TesseractEngine()
    return ThresholdOcrEngine()


def _build_classifier(request: Request, settings: Settings) -> PhiClassifier:
    override = getattr(request.app.state, "deid_classifier", None)
    if override is not None:
        return cast(PhiClassifier, override)
    if settings.deid_phi_classifier == "openmed":
        return OpenMedPhiClassifier()
    return DeterministicPhiClassifier()


async def get_deid_service(
    request: Request,
    settings: Settings,
    doc_store: DocumentStoreDep,
    object_store: ObjectStoreAcqDep,
    audit_service: AuditServiceDep,
) -> DeidService:
    remapper = _uid_remapper(settings)
    scrubber = TagScrubber(remapper)
    ocr_engine = _build_ocr_engine(request, settings)
    classifier = _build_classifier(request, settings)
    decider = Decider()
    redactor = Redactor(remapper)
    review_repo = DeidReviewRepository(doc_store)
    link_repo = DeidLinkRepository(doc_store)
    review_queue = ReviewQueue(review_repo)
    pipeline = DeidPipeline(
        scrubber=scrubber,
        ocr_engine=ocr_engine,
        classifier=classifier,
        decider=decider,
        redactor=redactor,
        review_queue=review_queue,
        link_repo=link_repo,
        audit_service=audit_service,
    )
    return DeidService(pipeline, review_queue, object_store, settings)


DeidServiceDep = Annotated[DeidService, Depends(get_deid_service)]


# Settings dependency (local to avoid touching the shared deps module).
async def _get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(_get_settings)]
