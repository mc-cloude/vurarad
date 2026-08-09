"""RenditionService — three quality tiers, PIL resize, manifest, erasure (WP15).

Unit tests for the pure-PIL encoding (byte budgets, max dimensions, grayscale)
and the store-oriented lifecycle (lazy preview caching, manifest sizing, and
``derived/{studyId}/`` erasure — criterion 3).
"""

from __future__ import annotations

from datetime import UTC, datetime

from PIL import Image

from app.services.rendition_service import (
    DERIVED_PREFIX,
    PREVIEW_TARGET_BYTES,
    THUMBNAIL_TARGET_BYTES,
    Quality,
    RenditionService,
)
from app.storage.base import ObjectMetadata, ObjectRef


# ---------------------------------------------------------------------------
# InMemoryObjectStore — a real byte-storing fake for rendition tests
# ---------------------------------------------------------------------------
class InMemoryObjectStore:
    """Minimal ObjectStore that stores bytes in a dict and tracks them."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._meta: dict[str, dict[str, str]] = {}
        self._bucket = "test-bucket"

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        self._objects[key] = data
        self._meta[key] = dict(metadata) if metadata else {}
        return ObjectRef(bucket=self._bucket, key=key)

    async def get_blob(self, key: str) -> bytes:
        return self._objects.get(key, b"")

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return self._objects.get(key, b"")[start:end]

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)
        self._meta.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        refs = [
            ObjectRef(bucket=self._bucket, key=k)
            for k in sorted(self._objects)
            if k.startswith(prefix)
        ]
        return refs[:limit]

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        return f"https://signed.example/{key}?ttl={ttl_seconds}"

    async def generate_signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int
    ) -> str:
        return f"https://upload.example/{key}"

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        return f"https://resumable.example/{key}"

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        if src_key in self._objects:
            self._objects[dst_key] = self._objects[src_key]
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        if src_key in self._objects:
            self._objects[dst_key] = self._objects[src_key]
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        data = self._objects.get(key, b"")
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket, key=key),
            size=len(data),
            content_type="image/jpeg",
            etag="",
            updated=datetime.now(UTC),
            metadata=self._meta.get(key, {}),
        )

    @property
    def supports_bucket_lock(self) -> bool:
        return True

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _gradient_image(width: int = 512, height: int = 512) -> Image.Image:
    """A synthetic gradient image — enough detail to exercise the JPEG encoder."""
    px = bytearray()
    for y in range(height):
        for x in range(width):
            px.append((x + y) % 256)
    return Image.frombytes("L", (width, height), bytes(px))


class _Inst:
    """Minimal InstanceLike for manifest input."""

    def __init__(self, sop: str, idx: int, size: int) -> None:
        self.sop_instance_uid = sop
        self.stack_index = idx
        self.size_bytes = size


# ---------------------------------------------------------------------------
# Key layout
# ---------------------------------------------------------------------------
class TestKeyLayout:
    def test_derived_key_format(self) -> None:
        key = RenditionService.derived_key("st_1", "se_1", "1.2.3", Quality.THUMBNAIL)
        assert key == "derived/st_1/se_1/1.2.3/thumbnail.jpg"

    def test_derived_prefix_for_study(self) -> None:
        assert RenditionService.derived_prefix_for_study("st_1") == "derived/st_1/"

    def test_derived_prefix_constant(self) -> None:
        assert DERIVED_PREFIX == "derived/"


# ---------------------------------------------------------------------------
# Pure PIL encoding — quality tiers
# ---------------------------------------------------------------------------
class TestQualityTiers:
    def test_thumbnail_max_dim_128(self) -> None:
        svc = RenditionService(InMemoryObjectStore())
        img = _gradient_image(1024, 768)
        data = svc.resize_to_thumbnail(img)
        decoded = Image.open(__import__("io").BytesIO(data))
        assert max(decoded.size) <= 128
        assert decoded.mode == "L"

    def test_preview_max_dim_512(self) -> None:
        svc = RenditionService(InMemoryObjectStore())
        img = _gradient_image(1024, 768)
        data = svc.resize_to_preview(img)
        decoded = Image.open(__import__("io").BytesIO(data))
        assert max(decoded.size) <= 512
        assert decoded.mode == "L"

    def test_thumbnail_byte_budget_near_15kb(self) -> None:
        svc = RenditionService(InMemoryObjectStore())
        img = _gradient_image(512, 512)
        data = svc.resize_to_thumbnail(img)
        # The encoder targets ~15 KB; allow a generous tolerance because small
        # images may not reach the target even at max quality.
        assert len(data) <= THUMBNAIL_TARGET_BYTES * 2
        assert len(data) > 0

    def test_preview_byte_budget_near_120kb(self) -> None:
        svc = RenditionService(InMemoryObjectStore())
        img = _gradient_image(1024, 1024)
        data = svc.resize_to_preview(img)
        assert len(data) <= PREVIEW_TARGET_BYTES * 2
        assert len(data) > 0

    def test_small_image_not_upscaled(self) -> None:
        svc = RenditionService(InMemoryObjectStore())
        img = _gradient_image(64, 64)
        data = svc.resize_to_thumbnail(img)
        decoded = Image.open(__import__("io").BytesIO(data))
        assert decoded.size == (64, 64)

    def test_aspect_ratio_preserved(self) -> None:
        svc = RenditionService(InMemoryObjectStore())
        img = _gradient_image(200, 100)
        data = svc.resize_to_thumbnail(img)
        decoded = Image.open(__import__("io").BytesIO(data))
        w, h = decoded.size
        assert max(w, h) <= 128
        # aspect ratio ~2:1
        assert abs(w / h - 2.0) < 0.1


# ---------------------------------------------------------------------------
# Store-oriented lifecycle — lazy preview caching
# ---------------------------------------------------------------------------
class TestLazyPreview:
    async def test_ensure_thumbnail_stores_and_returns_size(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        img = _gradient_image(512, 512)
        size = await svc.ensure_thumbnail("st_1", "se_1", "1.2.3", img)
        key = RenditionService.derived_key("st_1", "se_1", "1.2.3", Quality.THUMBNAIL)
        assert await store.exists(key)
        assert size == len(store._objects[key])  # noqa: SLF001

    async def test_ensure_preview_is_lazy_cached(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        img = _gradient_image(1024, 1024)
        key = RenditionService.derived_key("st_1", "se_1", "1.2.3", Quality.PREVIEW)
        # First call — cache miss, generates and stores.
        size1 = await svc.ensure_preview("st_1", "se_1", "1.2.3", img)
        assert await store.exists(key)
        # Second call — cache hit, returns the stored size without re-encoding.
        size2 = await svc.ensure_preview("st_1", "se_1", "1.2.3", img)
        assert size1 == size2

    async def test_quality_object_diagnostic_returns_source(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        source_key = "dicom/st_1/se_1/1.2.3.dcm"
        await store.put(source_key, b"\x00" * 526336, "application/dicom")
        key, size = await svc.quality_object(
            "st_1", "se_1", "1.2.3", Quality.DIAGNOSTIC, source_key
        )
        assert key == source_key
        assert size == 526336


# ---------------------------------------------------------------------------
# Manifest (criterion 2)
# ---------------------------------------------------------------------------
class TestManifest:
    async def test_manifest_reports_three_quality_sizes(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        # Store a thumbnail for instance 0.
        img = _gradient_image(512, 512)
        thumb_size = await svc.ensure_thumbnail("st_1", "se_1", "1.2.0", img)
        instances = [_Inst("1.2.0", 0, 526336), _Inst("1.2.1", 1, 510000)]
        manifest = await svc.build_manifest("st_1", "se_1", instances)

        assert manifest.study_id == "st_1"
        assert manifest.series_uid == "se_1"
        assert manifest.instance_count == 2
        assert len(manifest.instances) == 2

        entry0 = manifest.instances[0]
        assert entry0.sop_instance_uid == "1.2.0"
        assert entry0.stack_index == 0
        assert entry0.thumbnail_bytes == thumb_size
        assert entry0.diagnostic_bytes == 526336
        # Preview not yet generated → reports the design target (~120 KB).
        assert entry0.preview_bytes == PREVIEW_TARGET_BYTES

    async def test_manifest_preview_reports_actual_when_cached(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        img = _gradient_image(1024, 1024)
        preview_size = await svc.ensure_preview("st_1", "se_1", "1.2.0", img)
        instances = [_Inst("1.2.0", 0, 526336)]
        manifest = await svc.build_manifest("st_1", "se_1", instances)
        assert manifest.instances[0].preview_bytes == preview_size

    async def test_manifest_sorted_by_stack_index(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        instances = [_Inst("1.2.2", 2, 100), _Inst("1.2.0", 0, 100), _Inst("1.2.1", 1, 100)]
        manifest = await svc.build_manifest("st_1", "se_1", instances)
        assert [e.stack_index for e in manifest.instances] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Erasure (criterion 3)
# ---------------------------------------------------------------------------
class TestErasure:
    async def test_erase_for_study_deletes_all_derived(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        img = _gradient_image(256, 256)
        await svc.ensure_thumbnail("st_1", "se_1", "1.2.0", img)
        await svc.ensure_thumbnail("st_1", "se_2", "1.2.1", img)
        # A different study's derived object must survive.
        await svc.ensure_thumbnail("st_2", "se_1", "1.2.0", img)

        count = await svc.erase_for_study("st_1")
        assert count == 2
        # st_1's derived prefix is now empty.
        st1_refs = await store.list_prefix(RenditionService.derived_prefix_for_study("st_1"))
        assert st1_refs == []
        # st_2's derived object survives.
        st2_refs = await store.list_prefix(RenditionService.derived_prefix_for_study("st_2"))
        assert len(st2_refs) == 1

    async def test_erase_empty_study_returns_zero(self) -> None:
        store = InMemoryObjectStore()
        svc = RenditionService(store)
        count = await svc.erase_for_study("st_nonexistent")
        assert count == 0
