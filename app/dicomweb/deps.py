# ruff: noqa: B008
"""DICOMweb dependency providers — metadata store and study-access policy."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import Settings
from app.dicomweb.metadata import DicomMetadataStore
from app.dicomweb.models import StudyRecord


async def get_dicom_metadata_store(request: Request) -> DicomMetadataStore:
    """Lazy-build and cache the metadata store on ``app.state``."""
    store = getattr(request.app.state, "dicom_metadata_store", None)
    if store is None:
        from app.dicomweb.metadata import FirestoreDicomMetadataStore

        settings: Settings = request.app.state.settings
        store = FirestoreDicomMetadataStore.from_settings(settings)
        request.app.state.dicom_metadata_store = store
    return store


MetadataStoreDep = Annotated[DicomMetadataStore, Depends(get_dicom_metadata_store)]


async def resolve_study(
    study_uid: str,
    store: DicomMetadataStore = Depends(get_dicom_metadata_store),
    user: AuthenticatedUser = Depends(get_current_user),
) -> StudyRecord:
    """Resolve a study UID to a :class:`StudyRecord`, enforcing tenant scope.

    Cross-tenant access returns **404**, never 403 — a 403 would leak the
    existence of a study the caller does not own.
    """
    study = await store.get_study(study_uid, user.tenant_id)
    if study is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "Study not found",
                }
            },
        )
    return study


# Annotated type for use as a path-operation parameter
StudyAccessPolicy = Annotated[StudyRecord, Depends(resolve_study)]
