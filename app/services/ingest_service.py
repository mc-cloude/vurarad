"""Ingest service — header parse, admission, quarantine→DICOM rewrite, geometry.

The production path by which studies enter the system.  Bytes never pass through
Cloud Run: headers are read with a bounded ``Range`` request and parsed with
``pydicom.dcmread(stop_before_pixels=True)``, and the object body is promoted
from quarantine to the DICOM prefix by a server-side rewrite that sets
``Cache-Control: private, no-store`` — no pixel data is ever loaded into the
application.

A job is durable, checkpointed every 50 objects, and leased per study, so a
Cloud Run instance dying mid-ingest is a resumable interruption, not data loss.
A single unparseable object fails the whole job — a partially-ingested study is a
clinical-safety hazard, not a partial success.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from pydicom import dcmread
from ulid import ULID

from app.core.errors import IngestInProgressError, NotFoundError
from app.models.ingest import IngestJob, IngestJobError, IngestJobStatus
from app.models.series import Instance, Series
from app.repositories.ingest_job_repo import IngestJobRepository
from app.repositories.series_repo import SeriesRepository
from app.services.audit_service import AuditService
from app.services.stack_order import compute_stack_order
from app.services.upload_service import UploadService
from app.storage.base import ObjectStore

logger = logging.getLogger("vurarad.ingest")

# -- tunables (mirror §3.7 config: ingest_checkpoint_every / ingest_lease_seconds)
CHECKPOINT_EVERY = 50
LEASE_SECONDS = 600
HEADER_BYTES = 65_536  # first 64 KB carries the file meta + dataset headers

# -- size bounds (acceptance criterion 2)
MIN_OBJECT_BYTES = 1024  # 1 KB
MAX_OBJECT_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # 4 GB
BYTE_TOLERANCE = 0.01  # 1% byte tolerance on the declared total

# -- SOP class allow-list: CT/MR/CR/DX/US/MG/PT/NM image storage + Enhanced CT/MR
ALLOWED_SOP_CLASSES: frozenset[str] = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.1",  # CR Image Storage
        "1.2.840.10008.5.1.4.1.1.1.1",  # Digital X-Ray Image Storage - For Presentation
        "1.2.840.10008.5.1.4.1.1.1.2",  # MG Image Storage
        "1.2.840.10008.5.1.4.1.1.2",  # CT Image Storage
        "1.2.840.10008.5.1.4.1.1.2.1",  # Enhanced CT Image Storage
        "1.2.840.10008.5.1.4.1.1.4",  # MR Image Storage
        "1.2.840.10008.5.1.4.1.1.4.1",  # Enhanced MR Image Storage
        "1.2.840.10008.5.1.4.1.1.20",  # NM Image Storage
        "1.2.840.10008.5.1.4.1.1.6.1",  # US Image Storage
        "1.2.840.10008.5.1.4.1.1.128",  # PET Image Storage
    }
)

# -- transfer-syntax allow-list: what the Cornerstone loader supports
ALLOWED_TRANSFER_SYNTAXES: frozenset[str] = frozenset(
    {
        "1.2.840.10008.1.2",  # Implicit VR Little Endian
        "1.2.840.10008.1.2.1",  # Explicit VR Little Endian
        "1.2.840.10008.1.2.4.50",  # JPEG Baseline
        "1.2.840.10008.1.2.4.80",  # JPEG-LS Lossless
        "1.2.840.10008.1.2.4.81",  # JPEG-LS
        "1.2.840.10008.1.2.4.90",  # JPEG 2000 Lossless
        "1.2.840.10008.1.2.4.91",  # JPEG 2000
        "1.2.840.10008.1.2.5",  # RLE Lossless
    }
)

NO_STORE = "private, no-store"
REPLAY_DONE: frozenset[IngestJobStatus] = frozenset(
    {IngestJobStatus.SUCCEEDED, IngestJobStatus.DUPLICATE}
)


# ---------------------------------------------------------------------------
# Parsed header
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ParsedObject:
    object_name: str
    sop_class_uid: str
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    modality: str
    transfer_syntax_uid: str
    instance_number: int | None
    image_position_patient: list[float] | None
    image_orientation_patient: list[float] | None
    slice_location: float | None
    number_of_frames: int
    pixel_spacing: list[float] | None
    spacing_between_slices_mm: float | None
    window_center: float | None
    window_width: float | None
    rescale_slope: float | None
    rescale_intercept: float | None
    size_bytes: int


@dataclass(slots=True)
class ParseResult:
    object_name: str
    parsed: ParsedObject | None
    error: str | None


@dataclass
class SeriesAccum:
    """In-memory accumulation of instances for one series during a job."""

    series_instance_uid: str
    modality: str
    sop_class_uid: str
    is_multi_frame: bool = False
    instances: list[Instance] = field(default_factory=list)
    series_id: str | None = None  # stable across checkpoints once minted


# ---------------------------------------------------------------------------
# Header parsing helpers
# ---------------------------------------------------------------------------
def _str_attr(ds: Any, name: str) -> str:
    val = getattr(ds, name, None)
    return str(val) if val is not None else ""


def _float_list(ds: Any, name: str) -> list[float] | None:
    val = getattr(ds, name, None)
    if val is None:
        return None
    try:
        return [float(v) for v in val]
    except (TypeError, ValueError):
        return None


def _float_attr(ds: Any, name: str) -> float | None:
    val = getattr(ds, name, None)
    if val is None:
        return None
    try:
        if hasattr(val, "__iter__") and not isinstance(val, str):
            return float(val[0])
        return float(val)
    except (TypeError, ValueError, IndexError):
        return None


def _int_attr(ds: Any, name: str) -> int | None:
    val = getattr(ds, name, None)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def parse_dicom_headers(chunk: bytes, object_name: str, size_bytes: int) -> ParsedObject | None:
    """Parse the header bytes of one DICOM object (``stop_before_pixels=True``).

    Returns ``None`` when the bytes are not a parseable DICOM Part 10 dataset.
    """
    try:
        ds = dcmread(BytesIO(chunk), stop_before_pixels=True)
    except Exception:  # noqa: BLE001 — any parse failure is an admission failure
        return None

    ts = ""
    file_meta = getattr(ds, "file_meta", None)
    if file_meta is not None:
        ts = str(getattr(file_meta, "TransferSyntaxUID", "") or "")

    frames = _int_attr(ds, "NumberOfFrames") or 1
    return ParsedObject(
        object_name=object_name,
        sop_class_uid=_str_attr(ds, "SOPClassUID"),
        study_instance_uid=_str_attr(ds, "StudyInstanceUID"),
        series_instance_uid=_str_attr(ds, "SeriesInstanceUID"),
        sop_instance_uid=_str_attr(ds, "SOPInstanceUID"),
        modality=_str_attr(ds, "Modality"),
        transfer_syntax_uid=ts,
        instance_number=_int_attr(ds, "InstanceNumber"),
        image_position_patient=_float_list(ds, "ImagePositionPatient"),
        image_orientation_patient=_float_list(ds, "ImageOrientationPatient"),
        slice_location=_float_attr(ds, "SliceLocation"),
        number_of_frames=frames,
        pixel_spacing=_float_list(ds, "PixelSpacing"),
        spacing_between_slices_mm=_float_attr(ds, "SpacingBetweenSlices"),
        window_center=_float_attr(ds, "WindowCenter"),
        window_width=_float_attr(ds, "WindowWidth"),
        rescale_slope=_float_attr(ds, "RescaleSlope"),
        rescale_intercept=_float_attr(ds, "RescaleIntercept"),
        size_bytes=size_bytes,
    )


def _validate_parsed(parsed: ParsedObject) -> str | None:
    """Run admission validations 2-5 on a parsed object; return an error reason or None."""
    if not (
        parsed.sop_class_uid
        and parsed.study_instance_uid
        and parsed.series_instance_uid
        and parsed.sop_instance_uid
        and parsed.modality
    ):
        return "MISSING_REQUIRED_UIDS"
    if parsed.sop_class_uid not in ALLOWED_SOP_CLASSES:
        return f"SOP_CLASS_NOT_ALLOWED:{parsed.sop_class_uid}"
    if parsed.transfer_syntax_uid not in ALLOWED_TRANSFER_SYNTAXES:
        return f"TRANSFER_SYNTAX_NOT_ALLOWED:{parsed.transfer_syntax_uid}"
    if not (MIN_OBJECT_BYTES <= parsed.size_bytes <= MAX_OBJECT_BYTES):
        return f"SIZE_OUT_OF_BOUNDS:{parsed.size_bytes}"
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dicom_key(parsed: ParsedObject) -> str:
    return (
        f"dicom/{parsed.study_instance_uid}/{parsed.series_instance_uid}/"
        f"{parsed.sop_instance_uid}.dcm"
    )


def _build_instance(parsed: ParsedObject, object_path: str) -> Instance:
    return Instance(
        sop_instance_uid=parsed.sop_instance_uid,
        stack_index=0,  # assigned by compute_stack_order at checkpoint/finalize
        instance_number=parsed.instance_number,
        number_of_frames=parsed.number_of_frames,
        image_position_patient=parsed.image_position_patient,
        image_orientation_patient=parsed.image_orientation_patient,
        slice_location=parsed.slice_location,
        pixel_spacing=parsed.pixel_spacing,
        spacing_between_slices_mm=parsed.spacing_between_slices_mm,
        window_center=parsed.window_center,
        window_width=parsed.window_width,
        rescale_slope=parsed.rescale_slope,
        rescale_intercept=parsed.rescale_intercept,
        size_bytes=parsed.size_bytes,
        object_path=object_path,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class IngestService:
    """Orchestrate the quarantine→DICOM ingest for one upload."""

    def __init__(
        self,
        upload_service: UploadService,
        store: ObjectStore,
        series_repo: SeriesRepository,
        job_repo: IngestJobRepository,
        audit_service: AuditService,
    ) -> None:
        self._upload = upload_service
        self._store = store
        self._series_repo = series_repo
        self._jobs = job_repo
        self._audit = audit_service

    # -- public API ----------------------------------------------------------
    async def complete_upload(
        self,
        upload_id: str,
        *,
        idempotency_key: str,
        actor: str,
        second_factor: bool,
        max_objects: int | None = None,
    ) -> IngestJob:
        """Validate the declaration, then create/resume the ingest job and run it."""
        session = await self._upload.get_upload(upload_id)
        if session is None:
            raise NotFoundError(f"Upload session {upload_id} not found")

        objects = sorted(
            o.key for o in await self._store.list_prefix(session.quarantine_prefix, limit=200_000)
        )
        size_map = await self._fetch_sizes(objects)
        self._validate_declaration(
            session.expected_object_count, session.expected_total_bytes, size_map
        )

        existing = await self._jobs.find_by_upload(upload_id)
        if existing is not None and existing.status in REPLAY_DONE:
            return existing

        if existing is not None:
            job = existing
        else:
            job = self._new_job(upload_id, idempotency_key, actor, len(objects))
            await self._jobs.create(job)

        return await self._run_job(job, objects, size_map, actor, second_factor, max_objects)

    async def get_job(self, job_id: str) -> IngestJob | None:
        return await self._jobs.get(job_id)

    async def list_jobs(
        self, status: IngestJobStatus | None = None, limit: int = 100
    ) -> list[IngestJob]:
        return await self._jobs.list_by_status(status, limit)

    # -- declaration validation (route-level 422) ---------------------------
    async def _fetch_sizes(self, objects: list[str]) -> dict[str, int]:
        if not objects:
            return {}
        metas = await asyncio.gather(*(self._store.object_metadata(k) for k in objects))
        return {k: m.size for k, m in zip(objects, metas, strict=True)}

    @staticmethod
    def _validate_declaration(
        expected_count: int, expected_total: int, size_map: dict[str, int]
    ) -> None:
        from fastapi import HTTPException

        actual_count = len(size_map)
        actual_total = sum(size_map.values())
        if actual_count != expected_count:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": (
                            f"object count mismatch: expected {expected_count}, got {actual_count}"
                        ),
                    }
                },
            )
        if expected_total > MAX_TOTAL_BYTES or actual_total > MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": (
                            f"total bytes exceed the 4 GB bound: declared "
                            f"{expected_total}, actual {actual_total}"
                        ),
                    }
                },
            )
        if expected_total > 0:
            deviation = abs(actual_total - expected_total) / expected_total
            if deviation > BYTE_TOLERANCE:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": (
                                f"total bytes outside 1% tolerance: expected "
                                f"{expected_total}, got {actual_total}"
                            ),
                        }
                    },
                )

    # -- job lifecycle -------------------------------------------------------
    def _new_job(self, upload_id: str, idempotency_key: str, actor: str, total: int) -> IngestJob:
        return IngestJob(
            job_id=f"job_{ULID()}",
            upload_id=upload_id,
            status=IngestJobStatus.PENDING,
            objects_total=total,
            started_at=_now_iso(),
            idempotency_key=idempotency_key,
            actor=actor,
        )

    async def _run_job(
        self,
        job: IngestJob,
        objects: list[str],
        size_map: dict[str, int],
        actor: str,
        second_factor: bool,
        max_objects: int | None,
    ) -> IngestJob:
        await self._jobs.update(job.job_id, {"status": IngestJobStatus.RUNNING.value})

        if not objects:
            await self._fail_job(job, "NO_OBJECTS", "upload contained no objects")
            return await self._jobs.get(job.job_id)  # type: ignore[return-value]

        # Resolve the StudyInstanceUID (parse object 0 on the first run).
        cache: dict[str, ParseResult] = {}
        if job.study_instance_uid is None:
            first = await self._parse_and_validate(objects[0], size_map.get(objects[0], 0))
            cache[objects[0]] = first
            if first.error is not None or first.parsed is None:
                await self._fail_job(job, first.error or "UNPARSEABLE", objects[0])
                return await self._jobs.get(job.job_id)  # type: ignore[return-value]
            study_uid = first.parsed.study_instance_uid
            await self._jobs.update(job.job_id, {"study_instance_uid": study_uid})
        else:
            study_uid = job.study_instance_uid

        # Lease — refresh our own, reclaim an expired one, 409 if held by another.
        owner_token = job.owner_token or f"lease_{ULID()}"
        if not await self._jobs.acquire_lease(study_uid, owner_token, LEASE_SECONDS):
            raise IngestInProgressError()
        await self._jobs.update(job.job_id, {"owner_token": owner_token})

        # Duplicate detection — only on the first run; recorded on the job.
        if job.study_id is None:
            existing_series = await self._series_repo.find_by_study_instance_uid(study_uid)
            if existing_series:
                study_id = existing_series[0].study_id
                duplicate_of: str | None = study_id
            else:
                study_id = f"st_{ULID()}"
                duplicate_of = None
            await self._jobs.update(
                job.job_id, {"study_id": study_id, "duplicate_of": duplicate_of}
            )
        else:
            study_id = job.study_id
            duplicate_of = job.duplicate_of
        is_duplicate = duplicate_of is not None

        # Restore the accumulation from prior checkpoints (resume / duplicate).
        accumulation, seen = await self._load_accumulation(study_id)

        start = job.last_checkpoint_index
        end = len(objects) if max_objects is None else min(start + max_objects, len(objects))

        for batch_start in range(start, end, CHECKPOINT_EVERY):
            batch_end = min(batch_start + CHECKPOINT_EVERY, end)
            batch_keys = objects[batch_start:batch_end]
            results = await asyncio.gather(
                *(self._parse_and_validate_cached(k, size_map.get(k, 0), cache) for k in batch_keys)
            )
            # A single bad object fails the whole job.
            for key, result in zip(batch_keys, results, strict=True):
                if result.error is not None or result.parsed is None:
                    await self._fail_job(job, result.error or "UNPARSEABLE", key)
                    return await self._jobs.get(job.job_id)  # type: ignore[return-value]
            # All valid — promote to the DICOM prefix (server-side, no bytes through the app).
            parsed_objs = [r.parsed for r in results if r.parsed is not None]
            await asyncio.gather(*(self._rewrite_one(p, study_id) for p in parsed_objs))
            for parsed in parsed_objs:
                if parsed.sop_instance_uid in seen:
                    continue  # no-op keyed on SOPInstanceUID
                seen.add(parsed.sop_instance_uid)
                accum = accumulation.get(parsed.series_instance_uid)
                if accum is None:
                    accum = SeriesAccum(
                        series_instance_uid=parsed.series_instance_uid,
                        modality=parsed.modality,
                        sop_class_uid=parsed.sop_class_uid,
                        is_multi_frame=parsed.number_of_frames > 1,
                    )
                    accumulation[parsed.series_instance_uid] = accum
                accum.instances.append(_build_instance(parsed, _dicom_key(parsed)))
            await self._checkpoint(job, study_id, study_uid, accumulation, batch_end)

        if end < len(objects):
            # Aborted by max_objects — leave RUNNING and leased for resumption.
            await self._jobs.update(
                job.job_id,
                {"status": IngestJobStatus.RUNNING.value, "objects_processed": end},
            )
            return await self._jobs.get(job.job_id)  # type: ignore[return-value]

        await self._finalize(
            job, study_id, study_uid, accumulation, is_duplicate, duplicate_of, actor, second_factor
        )
        return await self._jobs.get(job.job_id)  # type: ignore[return-value]

    # -- per-object helpers --------------------------------------------------
    async def _parse_and_validate_cached(
        self, key: str, size: int, cache: dict[str, ParseResult]
    ) -> ParseResult:
        if key in cache:
            return cache[key]
        return await self._parse_and_validate(key, size)

    async def _parse_and_validate(self, key: str, size: int) -> ParseResult:
        try:
            chunk = await self._store.get_range(key, 0, HEADER_BYTES)
        except Exception:  # noqa: BLE001
            return ParseResult(object_name=key, parsed=None, error="OBJECT_UNREADABLE")
        parsed = parse_dicom_headers(chunk, key, size)
        if parsed is None:
            return ParseResult(object_name=key, parsed=None, error="UNPARSEABLE_DICOM")
        return ParseResult(object_name=key, parsed=parsed, error=_validate_parsed(parsed))

    async def _rewrite_one(self, parsed: ParsedObject, study_id: str) -> None:
        dst = _dicom_key(parsed)
        await self._store.rewrite(
            parsed.object_name,
            dst,
            cache_control=NO_STORE,
            metadata={
                "study-id": study_id,
                "study-instance-uid": parsed.study_instance_uid,
                "sop-instance-uid": parsed.sop_instance_uid,
            },
        )

    # -- checkpoint / finalize -----------------------------------------------
    async def _checkpoint(
        self,
        job: IngestJob,
        study_id: str,
        study_uid: str,
        accumulation: dict[str, SeriesAccum],
        index: int,
    ) -> None:
        series_count = await self._save_series(study_id, study_uid, accumulation)
        await self._jobs.update(
            job.job_id,
            {
                "last_checkpoint_index": index,
                "objects_processed": index,
                "series_created": series_count,
                "status": IngestJobStatus.RUNNING.value,
            },
        )

    async def _save_series(
        self, study_id: str, study_uid: str, accumulation: dict[str, SeriesAccum]
    ) -> int:
        now = _now_iso()
        for accum in accumulation.values():
            instances = list(accum.instances)
            basis, confidence, ordered = compute_stack_order(instances)
            series_id = accum.series_id or f"se_{ULID()}"
            accum.series_id = series_id
            series = Series(
                series_id=series_id,
                study_id=study_id,
                study_instance_uid=study_uid,
                series_instance_uid=accum.series_instance_uid,
                modality=accum.modality,
                sop_class_uid=accum.sop_class_uid,
                stack_order_basis=basis,
                stack_order_confidence=confidence,
                instance_count=len(ordered),
                frame_count=sum(i.number_of_frames for i in ordered),
                is_multi_frame=accum.is_multi_frame,
                instances=ordered,
                created_at=now,
            )
            await self._series_repo.save(series)
        return len(accumulation)

    async def _finalize(
        self,
        job: IngestJob,
        study_id: str,
        study_uid: str,
        accumulation: dict[str, SeriesAccum],
        is_duplicate: bool,
        duplicate_of: str | None,
        actor: str,
        second_factor: bool,
    ) -> None:
        series_count = await self._save_series(study_id, study_uid, accumulation)
        status = IngestJobStatus.DUPLICATE if is_duplicate else IngestJobStatus.SUCCEEDED
        await self._audit.record(
            "STUDY_INGESTED",
            actor=actor,
            second_factor=second_factor,
            detail={
                "studyId": study_id,
                "studyInstanceUid": study_uid,
                "objectsIngested": job.objects_total,
                "seriesCreated": series_count,
                "status": status.value,
                "duplicateOf": duplicate_of,
            },
            patient_key=hashlib.sha256(study_id.encode()).hexdigest(),
        )
        await self._jobs.update(
            job.job_id,
            {
                "status": status.value,
                "completed_at": _now_iso(),
                "objects_processed": job.objects_total,
                "series_created": series_count,
                "last_checkpoint_index": job.objects_total,
                "duplicate_of": duplicate_of,
            },
        )
        await self._jobs.release_lease(study_uid)
        await self._upload.mark_completed(job.upload_id)

    async def _fail_job(self, job: IngestJob, reason: str, object_name: str) -> None:
        error = IngestJobError(object_name=object_name, reason=reason)
        await self._jobs.update(
            job.job_id,
            {
                "status": IngestJobStatus.FAILED.value,
                "completed_at": _now_iso(),
                "objects_failed": 1,
                "errors": [error.model_dump()],
            },
        )
        if job.study_instance_uid:
            await self._jobs.release_lease(job.study_instance_uid)
        logger.warning("ingest job %s failed: %s on %s", job.job_id, reason, object_name)

    # -- accumulation restore ------------------------------------------------
    async def _load_accumulation(self, study_id: str) -> tuple[dict[str, SeriesAccum], set[str]]:
        accumulation: dict[str, SeriesAccum] = {}
        seen: set[str] = set()
        for series in await self._series_repo.get_series_for_study(study_id):
            accum = SeriesAccum(
                series_instance_uid=series.series_instance_uid,
                modality=series.modality,
                sop_class_uid=series.sop_class_uid,
                is_multi_frame=series.is_multi_frame,
            )
            accum.series_id = series.series_id
            accum.instances = list(series.instances)
            accumulation[series.series_instance_uid] = accum
            seen.update(i.sop_instance_uid for i in series.instances)
        return accumulation, seen
