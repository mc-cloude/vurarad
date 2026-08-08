"""QIDO-RS — query studies, series, and instances against metadata store."""

from __future__ import annotations

from typing import Any

from app.core.auth import AuthenticatedUser
from app.dicomweb.deps import MetadataStoreDep, StudyAccessPolicy
from app.dicomweb.models import (
    instance_to_dicom_json,
    series_to_dicom_json,
    study_to_dicom_json,
)

QIDO_DEFAULT_LIMIT = 100
QIDO_MAX_LIMIT = 500

# DICOM attribute names extractable from the query string for study-level QIDO
_STUDY_FILTERS = frozenset(
    {
        "PatientID",
        "PatientName",
        "AccessionNumber",
        "StudyDate",
        "ModalitiesInStudy",
        "StudyDescription",
    }
)


def _clamp_limit(raw_limit: int | None) -> int:
    if raw_limit is None or raw_limit <= 0:
        return QIDO_DEFAULT_LIMIT
    return min(raw_limit, QIDO_MAX_LIMIT)


def _extract_filters(
    query_params: dict[str, str], allowed: frozenset[str]
) -> dict[str, str]:
    """Extract DICOM attribute filters from query params."""
    return {k: v for k, v in query_params.items() if k in allowed}


async def query_studies(
    store: MetadataStoreDep,
    user: AuthenticatedUser,
    limit: int | None = None,
    offset: str | None = None,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """QIDO-RS: query studies for the caller's tenant."""
    clamped = _clamp_limit(limit)
    studies = await store.query_studies(user.tenant_id, clamped, offset)
    return [study_to_dicom_json(s) for s in studies]


async def query_series(
    study: StudyAccessPolicy,
    store: MetadataStoreDep,
    user: AuthenticatedUser,
    limit: int | None = None,
    offset: str | None = None,
) -> list[dict[str, Any]]:
    """QIDO-RS: query series within a study."""
    clamped = _clamp_limit(limit)
    series_list = await store.query_series(
        study.study_uid, user.tenant_id, clamped, offset
    )
    return [series_to_dicom_json(s) for s in series_list]


async def query_instances(
    study: StudyAccessPolicy,
    series_uid: str,
    store: MetadataStoreDep,
    user: AuthenticatedUser,
    limit: int | None = None,
    offset: str | None = None,
) -> list[dict[str, Any]]:
    """QIDO-RS: query instances within a series."""
    clamped = _clamp_limit(limit)
    instances = await store.query_instances(
        study.study_uid, series_uid, user.tenant_id, clamped, offset
    )
    return [instance_to_dicom_json(i) for i in instances]
