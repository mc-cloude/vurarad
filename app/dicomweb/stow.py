"""STOW-RS — store instances to quarantine, never directly to the DICOM prefix.

Each SOP instance is written to ``quarantine/{tenant}/{uploadId}/{sopUid}.dcm``
so it enters the same validation and ingest path as §3.7.  STOW-RS is never a
direct write into the DICOM store (AC8).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi.responses import JSONResponse

from app.core.auth import AuthenticatedUser
from app.dicomweb.models import (
    FAILED_SOP_SEQUENCE,
    FAILURE_CANNOT_UNDERSTAND,
    FAILURE_REASON,
    REFERENCED_SOP_CLASS_UID,
    REFERENCED_SOP_INSTANCE_UID,
    REFERENCED_SOP_SEQUENCE,
)
from app.services.storage_service import StorageService
from app.storage.base import ObjectStore

logger = logging.getLogger("vurarad.dicomweb.stow")


def _extract_boundary(content_type: str) -> str | None:
    """Extract the boundary parameter from a multipart/related Content-Type."""
    # Look for boundary= in the content type
    marker = "boundary="
    idx = content_type.lower().find(marker)
    if idx < 0:
        return None
    rest = content_type[idx + len(marker):]
    # Boundary may be quoted
    boundary = rest.split(";")[0].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    return boundary or None


def parse_multipart_related(body: bytes, boundary: str) -> list[bytes]:
    """Split a multipart/related body into individual part payloads."""
    sep = f"--{boundary}".encode()
    parts: list[bytes] = []
    segments = body.split(sep)
    for seg in segments[1:]:  # skip preamble
        if seg in (b"--\r\n", b"--\r", b"--"):
            break  # closing boundary
        if seg.startswith(b"--"):
            break  # closing boundary
        # Each segment starts with \r\n, then headers, then \r\n\r\n, then body
        # Strip leading CRLF
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        # Find the header/body separator
        hdr_end = seg.find(b"\r\n\r\n")
        payload = seg[hdr_end + 4:] if hdr_end >= 0 else seg
        # Strip trailing CRLF
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        parts.append(payload)
    return parts


def _extract_uids(
    dicom_bytes: bytes,
) -> tuple[str, str, str, str] | None:
    """Extract (SOPClassUID, StudyUID, SeriesUID, SOPInstanceUID) from DICOM bytes.

    Returns ``None`` if the bytes cannot be parsed as a valid DICOM dataset.
    """
    from io import BytesIO

    from pydicom import dcmread

    try:
        ds = dcmread(BytesIO(dicom_bytes), stop_before_pixels=True, force=True)
    except Exception:  # noqa: BLE001
        return None

    sop_class = str(getattr(ds, "SOPClassUID", "") or "")
    study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
    series_uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
    sop_uid = str(getattr(ds, "SOPInstanceUID", "") or "")

    if not study_uid or not series_uid or not sop_uid:
        return None

    return sop_class, study_uid, series_uid, sop_uid


def build_stow_response(
    stored: list[tuple[str, str, str]],
    failed: list[tuple[str, str, int]],
) -> tuple[dict[str, Any], int]:
    """Build the STOW-RS response dataset and HTTP status.

    ``stored``: list of (sop_class_uid, sop_instance_uid, object_key)
    ``failed``: list of (sop_class_uid, sop_instance_uid, failure_reason)

    Returns ``(dataset_dict, http_status)``:
    - 200 if all instances stored successfully
    - 202 if partial success
    - 409 if all failed
    - 400 if no instances were provided
    """
    if not stored and not failed:
        return {}, 400

    referenced: list[dict[str, Any]] = []
    for sop_class, sop_uid, _key in stored:
        referenced.append(
            {
                REFERENCED_SOP_CLASS_UID: {"vr": "UI", "Value": [sop_class]},
                REFERENCED_SOP_INSTANCE_UID: {"vr": "UI", "Value": [sop_uid]},
            }
        )

    failed_seq: list[dict[str, Any]] = []
    for sop_class, sop_uid, reason in failed:
        failed_seq.append(
            {
                REFERENCED_SOP_CLASS_UID: {"vr": "UI", "Value": [sop_class]},
                REFERENCED_SOP_INSTANCE_UID: {"vr": "UI", "Value": [sop_uid]},
                FAILURE_REASON: {"vr": "US", "Value": [reason]},
            }
        )

    dataset: dict[str, Any] = {}
    if referenced:
        dataset[REFERENCED_SOP_SEQUENCE] = {"vr": "SQ", "Value": referenced}
    if failed_seq:
        dataset[FAILED_SOP_SEQUENCE] = {"vr": "SQ", "Value": failed_seq}

    if failed and not stored:
        status = 409
    elif failed and stored:
        status = 202
    else:
        status = 200

    return dataset, status


async def store_instances(
    object_store: ObjectStore,
    user: AuthenticatedUser,
    body: bytes,
    content_type: str,
    quarantine_bucket: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Handle a STOW-RS request — parse, validate, write to quarantine.

    Returns ``(response_dataset, http_status)``.
    """
    boundary = _extract_boundary(content_type)
    if not boundary:
        return build_stow_response([], [])

    parts = parse_multipart_related(body, boundary)
    if not parts:
        return build_stow_response([], [])

    upload_id = f"stow_{uuid.uuid4().hex}"
    tenant = user.tenant_id
    service = StorageService(object_store, bucket=quarantine_bucket)

    stored: list[tuple[str, str, str]] = []
    failed: list[tuple[str, str, int]] = []

    for part in parts:
        uids = _extract_uids(part)
        if uids is None:
            # Cannot understand the dataset
            failed.append(("", "", FAILURE_CANNOT_UNDERSTAND))
            continue

        sop_class, _study_uid, _series_uid, sop_uid = uids
        try:
            ref = await service.store_quarantine(
                tenant, upload_id, sop_uid, part
            )
            stored.append((sop_class, sop_uid, ref.key))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to store SOP instance %s", sop_uid)
            failed.append((sop_class, sop_uid, FAILURE_CANNOT_UNDERSTAND))

    return build_stow_response(stored, failed)


def stow_json_response(dataset: dict[str, Any], http_status: int) -> JSONResponse:
    """Wrap a STOW-RS response dataset in a ``JSONResponse``.

    The response uses ``application/dicom+json`` media type and
    ``Cache-Control: private, no-store`` as required for pixel-bearing
    responses.
    """
    return JSONResponse(
        content=dataset,
        status_code=http_status,
        media_type="application/dicom+json",
        headers={"Cache-Control": "private, no-store"},
    )
