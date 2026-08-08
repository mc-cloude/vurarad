"""WADO-RS — multipart/related retrieval, frame ranges, rendered.

Frame requests serve byte ranges via ``ObjectStore.get_range`` — a
400-instance study must not be read into memory to serve 20 frames (AC7).
Every pixel-bearing response carries ``Cache-Control: private, no-store``.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from typing import Any

from app.core.auth import AuthenticatedUser
from app.dicomweb.deps import MetadataStoreDep, StudyAccessPolicy
from app.dicomweb.models import instance_to_dicom_json
from app.storage.base import ObjectStore
from app.storage.factory import build_object_store  # noqa: F401

logger = logging.getLogger("vurarad.dicomweb.wado")

DEFAULT_TRANSFER_SYNTAX = "1.2.840.10008.1.2.1"

# Placeholder 8x8 grayscale JPEG for the rendered endpoint.
_RENDERED_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9"
    "PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/wAALCAAIAAgBAREA/8QAHwAA"
    "AQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRB"
    "RIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRk"
    "dISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrK"
    "ztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/"
    "ACv/2Q=="
)


def _make_boundary() -> str:
    """Generate a unique multipart boundary."""
    return "vrw-" + secrets.token_hex(16)


def build_multipart(
    parts: list[bytes],
    content_type: str = "application/dicom",
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Assemble a ``multipart/related`` body.

    Returns ``(body, boundary)``.  Each part is preceded by a CRLF-delimited
    boundary header and followed by a CRLF.
    """
    if boundary is None:
        boundary = _make_boundary()

    chunks: list[bytes] = []
    for part in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f"Content-Type: {content_type}\r\n\r\n".encode()
        )
        chunks.append(part)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())

    return b"".join(chunks), boundary


def parse_frame_list(frame_list: str) -> list[int]:
    """Parse a WADO-RS frame list (``"1,3,5"`` or ``"1-5"``) to 0-based indices.

    DICOM frame numbers are 1-based; we return 0-based offsets for internal
    use.
    """
    frames: list[int] = []
    for segment in frame_list.split(","):
        segment = segment.strip()
        if "-" in segment:
            start_s, end_s = segment.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            frames.extend(range(start - 1, end))
        else:
            frames.append(int(segment) - 1)
    return frames


def _negotiate_transfer_syntax(accept: str | None) -> str:
    """Extract the transfer-syntax UID from an Accept header, if present."""
    if not accept:
        return DEFAULT_TRANSFER_SYNTAX
    # Look for transfer-syntax= in the Accept header
    lower = accept.lower()
    marker = "transfer-syntax="
    idx = lower.find(marker)
    if idx >= 0:
        rest = accept[idx + len(marker):]
        # The UID may be terminated by ; or end of string
        uid = rest.split(";")[0].strip().strip('"').strip("'")
        if uid:
            return uid
    return DEFAULT_TRANSFER_SYNTAX


async def retrieve_study(
    study: StudyAccessPolicy,
    store: MetadataStoreDep,
    object_store: ObjectStore,
    user: AuthenticatedUser,
) -> tuple[bytes, str]:
    """WADO-RS: retrieve all instances in a study as multipart/related."""
    # Query all series in the study, then all instances per series
    series_list = await store.query_series(
        study.study_uid, user.tenant_id, 500, None
    )
    parts: list[bytes] = []
    for series in series_list:
        series_instances = await store.query_instances(
            study.study_uid, series.series_uid, user.tenant_id, 500, None
        )
        for inst in series_instances:
            if inst.object_ref:
                data = await object_store.get_blob(inst.object_ref)
                parts.append(data)

    body, boundary = build_multipart(parts)
    return body, boundary


async def retrieve_series(
    study: StudyAccessPolicy,
    series_uid: str,
    store: MetadataStoreDep,
    object_store: ObjectStore,
    user: AuthenticatedUser,
) -> tuple[bytes, str]:
    """WADO-RS: retrieve all instances in a series."""
    instances = await store.query_instances(
        study.study_uid, series_uid, user.tenant_id, 500, None
    )
    parts: list[bytes] = []
    for inst in instances:
        if inst.object_ref:
            data = await object_store.get_blob(inst.object_ref)
            parts.append(data)
    body, boundary = build_multipart(parts)
    return body, boundary


async def retrieve_instance(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    store: MetadataStoreDep,
    object_store: ObjectStore,
    user: AuthenticatedUser,
) -> tuple[bytes, str]:
    """WADO-RS: retrieve a single instance."""
    inst = await store.get_instance(
        study.study_uid, series_uid, sop_uid, user.tenant_id
    )
    if inst is None or not inst.object_ref:
        body, boundary = build_multipart([])
        return body, boundary
    data = await object_store.get_blob(inst.object_ref)
    body, boundary = build_multipart([data])
    return body, boundary


async def retrieve_frames(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    frame_list: str,
    store: MetadataStoreDep,
    object_store: ObjectStore,
    user: AuthenticatedUser,
) -> tuple[bytes, str]:
    """WADO-RS: retrieve specific frames via byte ranges (AC7).

    Uses ``ObjectStore.get_range`` to fetch only the needed bytes, not the
    whole object.
    """
    inst = await store.get_instance(
        study.study_uid, series_uid, sop_uid, user.tenant_id
    )
    if inst is None or not inst.object_ref:
        body, boundary = build_multipart([], content_type="application/octet-stream")
        return body, boundary

    frames = parse_frame_list(frame_list)
    assert inst.object_ref, "object_ref must be set"  # noqa: S101

    parts: list[bytes] = []
    for frame_idx in frames:
        if inst.frame_offsets and frame_idx < len(inst.frame_offsets):
            start = inst.frame_offsets[frame_idx]
            if frame_idx + 1 < len(inst.frame_offsets):
                end = inst.frame_offsets[frame_idx + 1]
            else:
                # Last frame: read to a reasonable boundary
                end = start + 65536
            data = await object_store.get_range(inst.object_ref, start, end)
            parts.append(data)
        else:
            # No frame offset table — fall back to full object
            data = await object_store.get_blob(inst.object_ref)
            parts.append(data)

    body, boundary = build_multipart(parts, content_type="application/octet-stream")
    return body, boundary


async def retrieve_instance_metadata(
    study: StudyAccessPolicy,
    series_uid: str,
    sop_uid: str,
    store: MetadataStoreDep,
    user: AuthenticatedUser,
) -> list[dict[str, Any]]:
    """WADO-RS: return DICOM JSON metadata for a single instance."""
    inst = await store.get_instance(
        study.study_uid, series_uid, sop_uid, user.tenant_id
    )
    if inst is None:
        return []
    return [json.loads(json.dumps(instance_to_dicom_json(inst)))]


async def retrieve_rendered(  # noqa: ARG001
    study: StudyAccessPolicy,
    series_uid: str,  # noqa: ARG001
    sop_uid: str,  # noqa: ARG001
    store: MetadataStoreDep,  # noqa: ARG001
    user: AuthenticatedUser,  # noqa: ARG001
) -> bytes:
    """WADO-RS: return a rendered (JPEG) representation.

    Currently returns a placeholder 8x8 grayscale JPEG.  The real
    implementation will transcode the pixel data to the requested format.
    """
    return _RENDERED_JPEG
