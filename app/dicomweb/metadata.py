"""DICOM metadata store — Firestore-backed, tenant-scoped.

Queries are always tenant-scoped so that a cross-tenant study UID resolves to
``None`` (→ 404), never to a 403 that would confirm the existence of a study
the caller does not own.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.core.config import Settings
from app.dicomweb.models import InstanceRecord, SeriesRecord, StudyRecord

logger = logging.getLogger("vurarad.dicomweb.metadata")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class DicomMetadataStore(Protocol):
    """Metadata persistence for DICOMweb study/series/instance records."""

    async def put_study(self, study: StudyRecord) -> None: ...
    async def put_series(self, series: SeriesRecord) -> None: ...
    async def put_instance(self, instance: InstanceRecord) -> None: ...

    async def get_study(self, study_uid: str, tenant_id: str) -> StudyRecord | None: ...
    async def get_series(
        self, study_uid: str, series_uid: str, tenant_id: str
    ) -> SeriesRecord | None: ...
    async def get_instance(
        self, study_uid: str, series_uid: str, sop_uid: str, tenant_id: str
    ) -> InstanceRecord | None: ...

    async def query_studies(
        self, tenant_id: str, limit: int, offset: str | None
    ) -> list[StudyRecord]: ...
    async def query_series(
        self, study_uid: str, tenant_id: str, limit: int, offset: str | None
    ) -> list[SeriesRecord]: ...
    async def query_instances(
        self,
        study_uid: str,
        series_uid: str,
        tenant_id: str,
        limit: int,
        offset: str | None,
    ) -> list[InstanceRecord]: ...


# ---------------------------------------------------------------------------
# Firestore implementation
# ---------------------------------------------------------------------------
class FirestoreDicomMetadataStore:
    """Firestore-backed metadata store with tenant-scoped queries."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._studies = db.collection("studies")
        self._series = db.collection("series")
        self._instances = db.collection("instances")

    @classmethod
    def from_settings(cls, settings: Settings) -> FirestoreDicomMetadataStore:
        """Build from application settings."""
        import google.cloud.firestore as firestore

        if settings.firestore_emulator_host:
            db = firestore.Client(
                project=settings.gcp_project_id,
                database=settings.firestore_database,
            )
        else:
            db = firestore.Client(
                project=settings.gcp_project_id,
                database=settings.firestore_database,
            )
        return cls(db)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _study_doc_id(study_uid: str) -> str:
        return study_uid

    @staticmethod
    def _series_doc_id(study_uid: str, series_uid: str) -> str:
        return f"{study_uid}_{series_uid}"

    @staticmethod
    def _instance_doc_id(study_uid: str, series_uid: str, sop_uid: str) -> str:
        return f"{study_uid}_{series_uid}_{sop_uid}"

    # -- writes --------------------------------------------------------------
    async def put_study(self, study: StudyRecord) -> None:
        import asyncio

        doc_data = {
            "studyUid": study.study_uid,
            "patientId": study.patient_id,
            "patientName": study.patient_name,
            "studyDate": study.study_date,
            "studyTime": study.study_time,
            "accessionNumber": study.accession_number,
            "modalitiesInStudy": study.modalities_in_study,
            "studyDescription": study.study_description,
            "tenantId": study.tenant_id,
            "numSeries": study.num_series,
            "numInstances": study.num_instances,
        }
        await asyncio.to_thread(
            self._studies.document(self._study_doc_id(study.study_uid)).set, doc_data
        )

    async def put_series(self, series: SeriesRecord) -> None:
        import asyncio

        doc_data = {
            "studyUid": series.study_uid,
            "seriesUid": series.series_uid,
            "modality": series.modality,
            "seriesNumber": series.series_number,
            "seriesDescription": series.series_description,
            "tenantId": series.tenant_id,
            "numInstances": series.num_instances,
        }
        await asyncio.to_thread(
            self._series.document(
                self._series_doc_id(series.study_uid, series.series_uid)
            ).set,
            doc_data,
        )

    async def put_instance(self, instance: InstanceRecord) -> None:
        import asyncio

        doc_data = {
            "studyUid": instance.study_uid,
            "seriesUid": instance.series_uid,
            "sopUid": instance.sop_uid,
            "sopClassUid": instance.sop_class_uid,
            "instanceNumber": instance.instance_number,
            "rows": instance.rows,
            "columns": instance.columns,
            "numFrames": instance.num_frames,
            "tenantId": instance.tenant_id,
            "objectRef": instance.object_ref,
            "pixelDataOffset": instance.pixel_data_offset,
            "frameOffsets": instance.frame_offsets,
        }
        await asyncio.to_thread(
            self._instances.document(
                self._instance_doc_id(
                    instance.study_uid, instance.series_uid, instance.sop_uid
                )
            ).set,
            doc_data,
        )

    # -- reads (tenant-scoped: cross-tenant → None, never 403) ---------------
    async def get_study(self, study_uid: str, tenant_id: str) -> StudyRecord | None:
        import asyncio

        doc = await asyncio.to_thread(
            self._studies.document(self._study_doc_id(study_uid)).get
        )
        if not doc.exists:
            return None
        data = doc.to_dict()
        if not data or data.get("tenantId") != tenant_id:
            return None
        return StudyRecord(
            study_uid=data["studyUid"],
            patient_id=data.get("patientId", ""),
            patient_name=data.get("patientName", ""),
            study_date=data.get("studyDate", ""),
            study_time=data.get("studyTime", ""),
            accession_number=data.get("accessionNumber", ""),
            modalities_in_study=data.get("modalitiesInStudy", []),
            study_description=data.get("studyDescription", ""),
            tenant_id=data["tenantId"],
            num_series=data.get("numSeries", 0),
            num_instances=data.get("numInstances", 0),
        )

    async def get_series(
        self, study_uid: str, series_uid: str, tenant_id: str
    ) -> SeriesRecord | None:
        import asyncio

        doc = await asyncio.to_thread(
            self._series.document(
                self._series_doc_id(study_uid, series_uid)
            ).get
        )
        if not doc.exists:
            return None
        data = doc.to_dict()
        if not data or data.get("tenantId") != tenant_id:
            return None
        return SeriesRecord(
            study_uid=data["studyUid"],
            series_uid=data["seriesUid"],
            modality=data.get("modality", ""),
            series_number=data.get("seriesNumber", 0),
            series_description=data.get("seriesDescription", ""),
            tenant_id=data["tenantId"],
            num_instances=data.get("numInstances", 0),
        )

    async def get_instance(
        self, study_uid: str, series_uid: str, sop_uid: str, tenant_id: str
    ) -> InstanceRecord | None:
        import asyncio

        doc = await asyncio.to_thread(
            self._instances.document(
                self._instance_doc_id(study_uid, series_uid, sop_uid)
            ).get
        )
        if not doc.exists:
            return None
        data = doc.to_dict()
        if not data or data.get("tenantId") != tenant_id:
            return None
        return InstanceRecord(
            study_uid=data["studyUid"],
            series_uid=data["seriesUid"],
            sop_uid=data["sopUid"],
            sop_class_uid=data.get("sopClassUid", ""),
            instance_number=data.get("instanceNumber", 0),
            rows=data.get("rows", 0),
            columns=data.get("columns", 0),
            num_frames=data.get("numFrames", 0),
            tenant_id=data["tenantId"],
            object_ref=data.get("objectRef", ""),
            pixel_data_offset=data.get("pixelDataOffset", 0),
            frame_offsets=data.get("frameOffsets", []),
        )

    # -- queries (tenant-scoped) ---------------------------------------------
    async def query_studies(
        self, tenant_id: str, limit: int, offset: str | None
    ) -> list[StudyRecord]:
        import asyncio

        def _query() -> list[StudyRecord]:
            q = self._studies.where("tenantId", "==", tenant_id).limit(limit)
            docs = q.stream()
            return [
                StudyRecord(
                    study_uid=d.to_dict()["studyUid"],
                    patient_id=d.to_dict().get("patientId", ""),
                    patient_name=d.to_dict().get("patientName", ""),
                    study_date=d.to_dict().get("studyDate", ""),
                    study_time=d.to_dict().get("studyTime", ""),
                    accession_number=d.to_dict().get("accessionNumber", ""),
                    modalities_in_study=d.to_dict().get("modalitiesInStudy", []),
                    study_description=d.to_dict().get("studyDescription", ""),
                    tenant_id=d.to_dict()["tenantId"],
                    num_series=d.to_dict().get("numSeries", 0),
                    num_instances=d.to_dict().get("numInstances", 0),
                )
                for d in docs
            ]

        return await asyncio.to_thread(_query)

    async def query_series(
        self, study_uid: str, tenant_id: str, limit: int, offset: str | None
    ) -> list[SeriesRecord]:
        import asyncio

        def _query() -> list[SeriesRecord]:
            q = (
                self._series.where("tenantId", "==", tenant_id)
                .where("studyUid", "==", study_uid)
                .limit(limit)
            )
            return [
                SeriesRecord(
                    study_uid=d.to_dict()["studyUid"],
                    series_uid=d.to_dict()["seriesUid"],
                    modality=d.to_dict().get("modality", ""),
                    series_number=d.to_dict().get("seriesNumber", 0),
                    series_description=d.to_dict().get("seriesDescription", ""),
                    tenant_id=d.to_dict()["tenantId"],
                    num_instances=d.to_dict().get("numInstances", 0),
                )
                for d in q.stream()
            ]

        return await asyncio.to_thread(_query)

    async def query_instances(
        self,
        study_uid: str,
        series_uid: str,
        tenant_id: str,
        limit: int,
        offset: str | None,
    ) -> list[InstanceRecord]:
        import asyncio

        def _query() -> list[InstanceRecord]:
            q = (
                self._instances.where("tenantId", "==", tenant_id)
                .where("studyUid", "==", study_uid)
                .where("seriesUid", "==", series_uid)
                .limit(limit)
            )
            return [
                InstanceRecord(
                    study_uid=d.to_dict()["studyUid"],
                    series_uid=d.to_dict()["seriesUid"],
                    sop_uid=d.to_dict()["sopUid"],
                    sop_class_uid=d.to_dict().get("sopClassUid", ""),
                    instance_number=d.to_dict().get("instanceNumber", 0),
                    rows=d.to_dict().get("rows", 0),
                    columns=d.to_dict().get("columns", 0),
                    num_frames=d.to_dict().get("numFrames", 0),
                    tenant_id=d.to_dict()["tenantId"],
                    object_ref=d.to_dict().get("objectRef", ""),
                    pixel_data_offset=d.to_dict().get("pixelDataOffset", 0),
                    frame_offsets=d.to_dict().get("frameOffsets", []),
                )
                for d in q.stream()
            ]

        return await asyncio.to_thread(_query)
