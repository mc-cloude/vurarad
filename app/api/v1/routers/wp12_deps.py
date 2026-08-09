# ruff: noqa: B008
"""Dependency providers for the WP12 routers (findings, dictation, preprocessing).

Composition root for the pre-processing pipeline and the finding/dictation
services.  Tests override via ``app.state``; production lazy-builds from
settings.  Re-uses the shared ``require_phi_capability`` and ``AuditServiceDep``
from :mod:`app.api.v1.routers.studies_deps`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.api.v1.routers.studies_deps import (
    AuditServiceDep,
    DocumentStoreDep,
    SettingsDep,
    StudyRepoDep,
    require_mfa,
)
from app.core.auth import AuthenticatedUser
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models.study import StudyRecord
from app.segmentation.registry import SegmentationRegistry
from app.services.dictation_service import DictationService
from app.services.finding_service import FindingService
from app.services.preprocessing.orchestrator import PreprocessingOrchestrator
from app.services.preprocessing.segmentation_dispatcher import (
    SegmentationDispatcher,
)

__all__ = [
    "CurrentMfaUserDep",
    "DispatcherDep",
    "DictationServiceDep",
    "FindingServiceDep",
    "OrchestratorDep",
    "PreprocessingRegistryDep",
    "StudyRecordDep",
    "resolve_study",
]


# ---------------------------------------------------------------------------
# Runtime selection — africa → cpu_fast, GPU region → gpu_l4
# ---------------------------------------------------------------------------
def runtime_for_settings(settings: Settings) -> str:
    """Select the segmentation runtime from settings."""
    if settings.residency_policy.value == "africa":
        return "cpu_fast"
    if settings.is_cloud_run_gpu_available:
        return "gpu_l4"
    return "cpu_fast"


# ---------------------------------------------------------------------------
# Segmentation registry
# ---------------------------------------------------------------------------
async def get_segmentation_registry(request: Request) -> SegmentationRegistry:
    reg = getattr(request.app.state, "segmentation_registry", None)
    if reg is None:
        reg = SegmentationRegistry()
        request.app.state.segmentation_registry = reg
    return reg


PreprocessingRegistryDep = Annotated[SegmentationRegistry, Depends(get_segmentation_registry)]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
async def get_dispatcher(
    registry: PreprocessingRegistryDep,
    settings: SettingsDep,
) -> SegmentationDispatcher:
    return SegmentationDispatcher(
        registry,
        runtime=runtime_for_settings(settings),
        region=settings.segmentation_region,
        residency_policy=settings.residency_policy,
    )


DispatcherDep = Annotated[SegmentationDispatcher, Depends(get_dispatcher)]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def get_orchestrator(
    dispatcher: DispatcherDep,
    doc_store: DocumentStoreDep,
) -> PreprocessingOrchestrator:
    return PreprocessingOrchestrator(doc_store, dispatcher)


OrchestratorDep = Annotated[PreprocessingOrchestrator, Depends(get_orchestrator)]


# ---------------------------------------------------------------------------
# Finding service
# ---------------------------------------------------------------------------
async def get_finding_service(
    doc_store: DocumentStoreDep,
    audit_service: AuditServiceDep,
) -> FindingService:
    return FindingService(doc_store, audit_service)


FindingServiceDep = Annotated[FindingService, Depends(get_finding_service)]


# ---------------------------------------------------------------------------
# Dictation service
# ---------------------------------------------------------------------------
async def get_dictation_service(
    doc_store: DocumentStoreDep,
    study_repo: StudyRepoDep,
    audit_service: AuditServiceDep,
) -> DictationService:
    return DictationService(doc_store, study_repo, audit_service)


DictationServiceDep = Annotated[DictationService, Depends(get_dictation_service)]


# ---------------------------------------------------------------------------
# Study record resolver (404 if absent)
# ---------------------------------------------------------------------------
async def resolve_study(
    study_id: str,
    study_repo: StudyRepoDep,
) -> StudyRecord:
    study = await study_repo.get_study(study_id)
    if study is None:
        raise NotFoundError(f"Study {study_id} not found")
    return study


StudyRecordDep = Annotated[StudyRecord, Depends(resolve_study)]


# ---------------------------------------------------------------------------
# MFA-verified current user (convenience alias)
# ---------------------------------------------------------------------------
CurrentMfaUserDep = Annotated[AuthenticatedUser, Depends(require_mfa)]
