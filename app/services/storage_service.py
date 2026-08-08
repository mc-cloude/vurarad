"""Storage service — facade over ``ObjectStore`` for DICOM pixel data.

Depends on ``ObjectStore`` only (injected, never imported directly from a
backend SDK).  The GCS SDK import lives solely inside ``app/storage/gcs.py``;
a CI grep enforces that.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.storage.base import ObjectMetadata, ObjectRef, ObjectStore

DICOM_PREFIX = "dicom/"
QUARANTINE_PREFIX = "quarantine/"


class StorageService:
    """Facade over :class:`ObjectStore` for DICOM pixel data.

    The service knows the prefix layout (``dicom/`` and ``quarantine/``) so
    callers never construct object keys by hand.
    """

    def __init__(self, store: ObjectStore, *, bucket: str | None = None) -> None:
        self._store = store
        self._override_bucket = bucket

    # -- bucket --------------------------------------------------------------
    def _primary_bucket(self) -> str:
        if self._override_bucket:
            return self._override_bucket
        bucket = getattr(self._store, "_bucket_name", "")
        if not bucket:
            raise RuntimeError("StorageService requires a bucket-scoped ObjectStore")
        return bucket

    # -- key helpers ---------------------------------------------------------
    def dicom_ref(self, study_uid: str, series_uid: str, sop_uid: str) -> ObjectRef:
        key = f"{DICOM_PREFIX}{study_uid}/{series_uid}/{sop_uid}.dcm"
        return ObjectRef(bucket=self._primary_bucket(), key=key)

    def quarantine_ref(self, tenant: str, upload_id: str, sop_uid: str) -> ObjectRef:
        key = f"{QUARANTINE_PREFIX}{tenant}/{upload_id}/{sop_uid}.dcm"
        return ObjectRef(bucket=self._primary_bucket(), key=key)

    # -- write paths ---------------------------------------------------------
    async def store_dicom_instance(
        self,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        data: bytes,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.put(
            ref.key, data, content_type="application/dicom", metadata=metadata
        )

    async def store_quarantine(
        self,
        tenant: str,
        upload_id: str,
        sop_uid: str,
        data: bytes,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectRef:
        ref = self.quarantine_ref(tenant, upload_id, sop_uid)
        assert ref.key.startswith(QUARANTINE_PREFIX), (
            "quarantine writes must use the quarantine prefix"
        )
        return await self._store.put(
            ref.key, data, content_type="application/dicom", metadata=metadata
        )

    # -- read paths ----------------------------------------------------------
    async def get_instance(
        self, study_uid: str, series_uid: str, sop_uid: str
    ) -> bytes:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.get_blob(ref.key)

    async def get_instance_range(
        self,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        start: int,
        end: int,
    ) -> bytes:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.get_range(ref.key, start, end)

    async def delete_instance(
        self, study_uid: str, series_uid: str, sop_uid: str
    ) -> None:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        await self._store.delete(ref.key)

    async def instance_exists(
        self, study_uid: str, series_uid: str, sop_uid: str
    ) -> bool:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.exists(ref.key)

    async def instance_metadata(
        self, study_uid: str, series_uid: str, sop_uid: str
    ) -> ObjectMetadata:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.object_metadata(ref.key)

    async def copy_instance(
        self,
        src_study_uid: str,
        src_series_uid: str,
        src_sop_uid: str,
        dst_study_uid: str,
        dst_series_uid: str,
        dst_sop_uid: str,
    ) -> ObjectRef:
        src = self.dicom_ref(src_study_uid, src_series_uid, src_sop_uid)
        dst = self.dicom_ref(dst_study_uid, dst_series_uid, dst_sop_uid)
        return await self._store.copy(src.key, dst.key)

    # -- signed URLs ---------------------------------------------------------
    async def signed_read_url(
        self,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        ttl_seconds: int = 300,
    ) -> str:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.generate_signed_read_url(ref.key, ttl_seconds)

    async def signed_upload_url(
        self,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        ttl_seconds: int = 300,
    ) -> str:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.generate_signed_upload_url(
            ref.key, "application/dicom", ttl_seconds
        )

    async def resumable_upload(
        self,
        study_uid: str,
        series_uid: str,
        sop_uid: str,
        expected_bytes: int,
    ) -> str:
        ref = self.dicom_ref(study_uid, series_uid, sop_uid)
        return await self._store.create_resumable_upload(
            ref.key, "application/dicom", expected_bytes
        )

    # -- bucket lock ---------------------------------------------------------
    @property
    def supports_bucket_lock(self) -> bool:
        return self._store.supports_bucket_lock
