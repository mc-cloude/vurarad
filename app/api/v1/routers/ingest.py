# ruff: noqa: B008
"""Ingest job routes — read-only views over durable ingest jobs (§3.7).

``GET /ingest/jobs/{jobId}`` returns one job record; ``GET /ingest/jobs`` lists
jobs, optionally filtered by ``status``.  Both require the ``study:import``
capability.  These are the read side of the acquisition path — the write side
(authorise upload, signal completion) lives in ``uploads.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.v1.routers.acquisition_deps import IngestServiceDep
from app.core.auth import AuthenticatedUser, get_current_user, require_capability
from app.core.capabilities import Capability
from app.core.errors import NotFoundError
from app.models.common import CamelModel
from app.models.ingest import IngestJob, IngestJobStatus

router = APIRouter(tags=["ingest"])


class JobListResponse(CamelModel):
    """Paginated list of ingest jobs (§3.7)."""

    items: list[IngestJob]
    next_cursor: str | None = None


@router.get(
    "/ingest/jobs/{job_id}",
    dependencies=[Depends(require_capability(Capability.STUDY_IMPORT))],
)
async def get_job(
    job_id: str,
    ingest_service: IngestServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> IngestJob:
    job = await ingest_service.get_job(job_id)
    if job is None:
        raise NotFoundError(f"Ingest job {job_id} not found")
    return job


@router.get(
    "/ingest/jobs",
    dependencies=[Depends(require_capability(Capability.STUDY_IMPORT))],
)
async def list_jobs(
    ingest_service: IngestServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    status: IngestJobStatus | None = Query(default=None),
) -> JobListResponse:
    jobs = await ingest_service.list_jobs(status=status)
    return JobListResponse(items=jobs, next_cursor=None)
