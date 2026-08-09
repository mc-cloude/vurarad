"""Rendition service — three quality tiers and the ``derived/`` object lifecycle.

Imaging PHI is delivered at three quality tiers so a viewer on a constrained link
can fetch a ~15 KB thumbnail or a ~120 KB preview instead of the full diagnostic
object (often 500 KB+ per slice).  The tiers are:

- ``thumbnail`` (~15 KB) — generated **at ingest** with a PIL resize, stored once
  under ``derived/{studyId}/{seriesId}/{sopInstanceUid}/thumbnail.jpg``.
- ``preview``    (~120 KB) — **lazy-cached**: generated on first access from the
  diagnostic source, then stored under ``.../preview.jpg`` for subsequent reads.
- ``diagnostic`` (original) — the untouched DICOM object at ``dicom/...``.

All three are PHI and are handled identically (criterion 1): the *same* signed-URL
issuance path, the same ``Cache-Control: private, no-store`` override, the same
``STUDY_IMAGES_ACCESSED`` audit event, and the same erasure — derived renditions
are deleted alongside their source study (criterion 3).  A ``GET .../manifest``
route returns the per-instance byte size at every tier so the client can plan its
bandwidth budget before fetching a single pixel (criterion 2).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from io import BytesIO
from typing import TYPE_CHECKING, Any, Protocol

from app.models.common import CamelModel
from app.storage.base import ObjectStore

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger("vurarad.rendition")


# ---------------------------------------------------------------------------
# Quality tiers
# ---------------------------------------------------------------------------
class Quality:
    """The three delivery qualities (plain strings — used as path segments)."""

    THUMBNAIL = "thumbnail"
    PREVIEW = "preview"
    DIAGNOSTIC = "diagnostic"

    ALL: tuple[str, str, str] = (THUMBNAIL, PREVIEW, DIAGNOSTIC)


# Design targets — the "~15 KB" / "~120 KB" from the spec.  The encoder searches
# JPEG quality to land as close as possible to these byte budgets.
THUMBNAIL_TARGET_BYTES = 15_360  # ~15 KB
PREVIEW_TARGET_BYTES = 122_880  # ~120 KB
THUMBNAIL_MAX_DIM = 128
PREVIEW_MAX_DIM = 512

DERIVED_PREFIX = "derived/"


# ---------------------------------------------------------------------------
# Manifest models — the wire shape for GET .../manifest (criterion 2)
# ---------------------------------------------------------------------------
class ManifestEntry(CamelModel):
    """Per-instance byte sizes at all three qualities."""

    sop_instance_uid: str
    stack_index: int
    thumbnail_bytes: int
    preview_bytes: int
    diagnostic_bytes: int


class QualityManifest(CamelModel):
    """``GET /studies/{studyId}/series/{seriesId}/manifest`` response."""

    study_id: str
    series_uid: str
    instance_count: int
    instances: list[ManifestEntry]


class RenditionService:
    """Generate, cache, size, and erase derived renditions."""

    def __init__(
        self,
        store: ObjectStore,
        *,
        thumbnail_target_bytes: int = THUMBNAIL_TARGET_BYTES,
        preview_target_bytes: int = PREVIEW_TARGET_BYTES,
        thumbnail_max_dim: int = THUMBNAIL_MAX_DIM,
        preview_max_dim: int = PREVIEW_MAX_DIM,
    ) -> None:
        self._store = store
        self._thumbnail_target = thumbnail_target_bytes
        self._preview_target = preview_target_bytes
        self._thumbnail_max_dim = thumbnail_max_dim
        self._preview_max_dim = preview_max_dim

    # -- key layout ----------------------------------------------------------
    @staticmethod
    def derived_key(study_id: str, series_id: str, sop_instance_uid: str, quality: str) -> str:
        """Object key for a derived rendition of one instance."""
        return f"{DERIVED_PREFIX}{study_id}/{series_id}/{sop_instance_uid}/{quality}.jpg"

    @staticmethod
    def derived_prefix_for_study(study_id: str) -> str:
        """The prefix every derived object for a study lives under (erasure unit)."""
        return f"{DERIVED_PREFIX}{study_id}/"

    # -- pure PIL encoding (no I/O — unit-testable) -------------------------
    @staticmethod
    def _resize(image: Image.Image, max_dim: int) -> Image.Image:
        """Resize so the largest side is ``max_dim``, preserving aspect ratio.

        Converted to 8-bit grayscale (``L``) — medical images are single-channel,
        and JPEG has no 16-bit mode.
        """
        img = image.convert("L")
        width, height = img.size
        if max(width, height) <= max_dim:
            return img
        scale = max_dim / max(width, height)
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return img.resize(new_size, _LANCZOS)

    @staticmethod
    def _encode_near_target(image_l: Image.Image, target_bytes: int) -> bytes:
        """JPEG-encode ``image_l`` at the quality whose size is closest to ``target``.

        JPEG size is monotonic in quality, so a binary search over quality in
        ``[10, 95]`` converges on the closest achievable byte count.
        """
        lo, hi = 10, 95
        best: bytes | None = None
        best_dist = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            buf = BytesIO()
            image_l.save(buf, format="JPEG", quality=mid)
            data = buf.getvalue()
            dist = abs(len(data) - target_bytes)
            if best is None or dist < best_dist:
                best, best_dist = data, dist
            if len(data) < target_bytes:
                lo = mid + 1
            elif len(data) > target_bytes:
                hi = mid - 1
            else:
                break
        assert best is not None  # loop runs at least once (lo=10 <= hi=95)
        return best

    def resize_to_thumbnail(self, image: Image.Image) -> bytes:
        """Produce a ~15 KB thumbnail JPEG (the ingest-time rendition)."""
        return self._encode_near_target(
            self._resize(image, self._thumbnail_max_dim), self._thumbnail_target
        )

    def resize_to_preview(self, image: Image.Image) -> bytes:
        """Produce a ~120 KB preview JPEG (the lazy-cached rendition)."""
        return self._encode_near_target(
            self._resize(image, self._preview_max_dim), self._preview_target
        )

    # -- DICOM pixel decode (the one place pixels enter the app) -------------
    @staticmethod
    def decode_dicom_image(dicom_bytes: bytes) -> Image.Image:
        """Decode a DICOM object's pixel data into a PIL image.

        Used at ingest (thumbnail) and on first preview access (lazy).  This is
        the deliberate exception to the ``stop_before_pixels`` rule — a thumbnail
        cannot be produced without the pixels.
        """
        from PIL import Image
        from pydicom import dcmread

        dataset = dcmread(BytesIO(dicom_bytes))
        pixel_array: Any = dataset.pixel_array
        return Image.fromarray(pixel_array).convert("L")

    # -- store-oriented rendition lifecycle ---------------------------------
    async def ensure_thumbnail(
        self, study_id: str, series_id: str, sop_instance_uid: str, image: Image.Image
    ) -> int:
        """Generate and store the thumbnail at ingest. Returns the byte size."""
        key = self.derived_key(study_id, series_id, sop_instance_uid, Quality.THUMBNAIL)
        data = self.resize_to_thumbnail(image)
        await self._store.put(
            key,
            data,
            "image/jpeg",
            metadata={"study-id": study_id, "quality": Quality.THUMBNAIL},
        )
        return len(data)

    async def ensure_preview(
        self, study_id: str, series_id: str, sop_instance_uid: str, image: Image.Image
    ) -> int:
        """Lazy-cached preview — generate once, then serve from cache.

        Returns the byte size.  A second call with the same keys is a cache hit:
        the stored object is read back and no second encode/write happens.
        """
        key = self.derived_key(study_id, series_id, sop_instance_uid, Quality.PREVIEW)
        if await self._store.exists(key):
            return (await self._store.object_metadata(key)).size
        data = self.resize_to_preview(image)
        await self._store.put(
            key,
            data,
            "image/jpeg",
            metadata={"study-id": study_id, "quality": Quality.PREVIEW},
        )
        return len(data)

    async def ensure_preview_from_source(
        self, study_id: str, series_id: str, sop_instance_uid: str, source_key: str
    ) -> tuple[str, int]:
        """Lazy-cached preview generated from the diagnostic DICOM object.

        Returns ``(object_key, size_bytes)``.  On a cache hit the source is never
        read or decoded.
        """
        key = self.derived_key(study_id, series_id, sop_instance_uid, Quality.PREVIEW)
        if await self._store.exists(key):
            return key, (await self._store.object_metadata(key)).size
        image = self.decode_dicom_image(await self._store.get_blob(source_key))
        data = self.resize_to_preview(image)
        await self._store.put(
            key,
            data,
            "image/jpeg",
            metadata={"study-id": study_id, "quality": Quality.PREVIEW},
        )
        return key, len(data)

    async def quality_object(
        self,
        study_id: str,
        series_id: str,
        sop_instance_uid: str,
        quality: str,
        source_key: str,
    ) -> tuple[str, int]:
        """Return ``(object_key, size_bytes)`` for a quality tier.

        ``diagnostic`` resolves to the original object; ``thumbnail`` to the
        stored thumbnail; ``preview`` is lazily generated from the source on a
        cache miss.  This is the single resolution path the access-URL flow uses
        for every tier so that all PHI traverses the same signed-URL pipeline.
        """
        if quality == Quality.DIAGNOSTIC:
            return source_key, (await self._store.object_metadata(source_key)).size
        if quality == Quality.THUMBNAIL:
            key = self.derived_key(study_id, series_id, sop_instance_uid, Quality.THUMBNAIL)
            return key, (await self._store.object_metadata(key)).size
        return await self.ensure_preview_from_source(
            study_id, series_id, sop_instance_uid, source_key
        )

    # -- manifest (criterion 2) ---------------------------------------------
    async def _size_of(self, key: str, fallback: int = 0) -> int:
        """Return the stored object size, or ``fallback`` when it is absent."""
        if not await self._store.exists(key):
            return fallback
        return (await self._store.object_metadata(key)).size

    async def build_manifest(
        self,
        study_id: str,
        series_id: str,
        instances: Sequence[InstanceLike],
    ) -> QualityManifest:
        """Per-instance byte sizes at all three qualities.

        Thumbnail and diagnostic sizes are read from the store / instance record.
        A preview that has not yet been lazily generated reports the design target
        (~120 KB) so the client can budget bandwidth without forcing generation.
        """
        entries: list[ManifestEntry] = []
        for inst in sorted(instances, key=lambda i: i.stack_index):
            thumb_key = self.derived_key(
                study_id, series_id, inst.sop_instance_uid, Quality.THUMBNAIL
            )
            prev_key = self.derived_key(
                study_id, series_id, inst.sop_instance_uid, Quality.PREVIEW
            )
            entries.append(
                ManifestEntry(
                    sop_instance_uid=inst.sop_instance_uid,
                    stack_index=inst.stack_index,
                    thumbnail_bytes=await self._size_of(thumb_key),
                    preview_bytes=await self._size_of(prev_key, fallback=self._preview_target),
                    diagnostic_bytes=inst.size_bytes,
                )
            )
        return QualityManifest(
            study_id=study_id,
            series_uid=series_id,
            instance_count=len(entries),
            instances=entries,
        )

    # -- erasure (criterion 3) ----------------------------------------------
    async def erase_for_study(self, study_id: str) -> int:
        """Delete every derived rendition for a study. Returns the count erased.

        Derived objects live under ``derived/{studyId}/``; listing and deleting
        that prefix empties it, so erasing a patient leaves ``derived/`` empty.
        """
        prefix = self.derived_prefix_for_study(study_id)
        refs = await self._store.list_prefix(prefix, limit=200_000)
        for ref in refs:
            await self._store.delete(ref.key)
        return len(refs)


# ---------------------------------------------------------------------------
# Minimal structural protocol for manifest input — accepts InstanceGeometry or
# Instance without importing either, keeping this module decoupled from the
# study/series model packages.
# ---------------------------------------------------------------------------
# PIL's resampling enum moved to Image.Resampling in Pillow 9.1; the top-level
# alias was removed in Pillow 10.  Resolve it once at import time.
def _resolve_lanczos() -> Any:
    from PIL import Image

    return Image.Resampling.LANCZOS


_LANCZOS: Any = _resolve_lanczos()


class InstanceLike(Protocol):
    """Minimal structural shape for manifest input (Instance or InstanceGeometry)."""

    sop_instance_uid: str
    stack_index: int
    size_bytes: int


__all__ = [
    "DERIVED_PREFIX",
    "InstanceLike",
    "ManifestEntry",
    "Quality",
    "QualityManifest",
    "RenditionService",
    "THUMBNAIL_TARGET_BYTES",
    "PREVIEW_TARGET_BYTES",
]
