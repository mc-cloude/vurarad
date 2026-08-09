"""Ingest and upload models — resumable sessions, durable jobs, leases.

The acquisition path is three stages: authorise an upload (mint resumable
session URLs scoped to quarantine), upload directly to GCS, then signal
completion.  Bytes never pass through Cloud Run — no route accepts a file body.
Completion creates a durable :class:`IngestJob` that is checkpointed, leased, and
resumable, so a Cloud Run instance dying mid-ingest is a resumable interruption,
not data loss.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.common import CamelModel


class UploadStatus(StrEnum):
    """Lifecycle of an upload session."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


class IngestJobStatus(StrEnum):
    """Lifecycle of an ingest job.

    ``DUPLICATE`` — the ``StudyInstanceUID`` already maps to an existing study;
    only genuinely new instances were added and ``duplicateOf`` is set.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


class ResumableSessionUrl(CamelModel):
    """One GCS resumable upload session URL and the object name it targets."""

    object_name: str
    session_url: str


class UploadCreate(CamelModel):
    """Request body for ``POST /uploads``."""

    source_label: str
    expected_object_count: int
    expected_total_bytes: int


class UploadSession(CamelModel):
    """A resumable upload session scoped to the quarantine prefix.

    Session URLs are minted 100 at a time; ``nextObjectIndex`` is where the next
    batch begins.  Objects land under ``quarantinePrefix`` and are never
    viewer-reachable until they pass admission and are rewritten into the DICOM
    prefix.
    """

    upload_id: str
    tenant: str
    source_label: str
    expected_object_count: int
    expected_total_bytes: int
    quarantine_prefix: str
    resumable_session_urls: list[ResumableSessionUrl]
    next_object_index: int
    status: UploadStatus = UploadStatus.ACTIVE
    idempotency_key: str
    actor: str
    created_at: str
    expires_at: str


class IngestJobError(CamelModel):
    """One per-object failure recorded on a FAILED job."""

    object_name: str
    reason: str
    detail: str = ""


class IngestJob(CamelModel):
    """A durable ingest job document at ``ingest_jobs/{jobId}``.

    ``lastCheckpointIndex`` is the number of objects fully processed and
    persisted; a retry with the same ``Idempotency-Key`` resumes from it.
    ``ownerToken`` + ``leaseExpireAt`` form the lease on
    ``ingest_locks/{studyInstanceUidHash}`` that prevents two concurrent jobs for
    the same study.
    """

    job_id: str
    upload_id: str
    status: IngestJobStatus
    study_id: str | None = None
    study_instance_uid: str | None = None
    objects_total: int = 0
    objects_processed: int = 0
    objects_failed: int = 0
    series_created: int = 0
    started_at: str
    completed_at: str | None = None
    last_checkpoint_index: int = 0
    duplicate_of: str | None = None
    owner_token: str = ""
    lease_expire_at: float = 0.0
    idempotency_key: str
    actor: str
    errors: list[IngestJobError] = []
