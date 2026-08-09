"""Study repository — worklist index, search, study/series reads.

The worklist is a single document read (``worklist_index/current``) — 1
Firestore read, always.  Search runs an equality-filtered query against the
``studies`` collection using composite indexes.  Study and series reads are
direct document gets, not queries: a 4-series study costs 1 + 4 = 5 reads,
never 412.
"""

from __future__ import annotations

from typing import Any

from app.models.series import Instance, Series
from app.models.study import StudyRecord
from app.repositories.base import DocumentStore

STUDIES_COLLECTION = "studies"
WORKLIST_COLLECTION = "worklist_index"
WORKLIST_DOC_ID = "current"
SERIES_COLLECTION = "series"


class StudyRepository:
    """Read worklist, search, study, and series documents."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    # -- worklist (1 read) ---------------------------------------------------
    async def get_worklist_index(self) -> dict[str, Any] | None:
        """Read the ``worklist_index/current`` document — exactly 1 Firestore read."""
        return await self._store.get(WORKLIST_COLLECTION, WORKLIST_DOC_ID)

    # -- search (reads == results) -------------------------------------------
    async def search(
        self,
        *,
        patient_ref: str | None = None,
        accession: str | None = None,
        modality: str | None = None,
        status: str | None = None,
        study_date_from: str | None = None,
        study_date_to: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Run an equality-filtered query against ``studies``.

        Returns matching study documents.  The caller sorts and paginates.
        Read cost equals the number of documents returned.
        """
        where: list[tuple[str, str, Any]] = []
        if patient_ref is not None:
            where.append(("patientRef", "==", patient_ref))
        if accession is not None:
            where.append(("accession", "==", accession))
        if modality is not None:
            where.append(("modality", "==", modality))
        if status is not None:
            where.append(("status", "==", status))
        rows = await self._store.query(
            STUDIES_COLLECTION,
            where=where or None,
            limit=limit,
        )
        # Apply date-range filtering in-memory (Firestore range queries need
        # composite indexes that differ per filter combination; the in-memory
        # store supports equality only, and the service layer sorts).
        results: list[dict[str, Any]] = []
        for _doc_id, doc in rows:
            sd = doc.get("studyDate", doc.get("study_date", ""))
            if study_date_from is not None and sd < study_date_from:
                continue
            if study_date_to is not None and sd > study_date_to:
                continue
            results.append(doc)
        return results

    # -- study detail (1 read) -----------------------------------------------
    async def get_study(self, study_id: str) -> StudyRecord | None:
        """Read ``studies/{studyId}`` — exactly 1 Firestore read."""
        doc = await self._store.get(STUDIES_COLLECTION, study_id)
        if doc is None:
            return None
        return _study_record_from_doc(doc)

    # -- priors (reads == matching studies) ----------------------------------
    async def get_studies_by_patient_key(
        self,
        patient_key: str,
        *,
        exclude_study_id: str | None = None,
        limit: int = 500,
    ) -> list[StudyRecord]:
        """Return every study sharing ``patientKey`` (prior-study resolution).

        Used by :class:`PriorStudyService` to resolve comparison priors for a
        study.  Reads equal the number of matching studies.  The caller
        authorizes each result independently.
        """
        rows = await self._store.query(
            STUDIES_COLLECTION,
            where=[("patientKey", "==", patient_key)],
            limit=limit,
        )
        records: list[StudyRecord] = []
        for _doc_id, doc in rows:
            rec = _study_record_from_doc(doc)
            if exclude_study_id is not None and rec.study_id == exclude_study_id:
                continue
            records.append(rec)
        return records

    # -- series reads (1 read per series) ------------------------------------
    async def get_series_by_id(self, series_id: str) -> Series | None:
        """Read a series document by its internal id — 1 Firestore read.

        For multi-part series (> 2 000 instances), part 0 is read first and
        remaining parts are fetched only when ``part_count > 1``.
        """
        doc = await self._store.get(SERIES_COLLECTION, series_id)
        if doc is None:
            return None
        base_series = Series.model_validate(doc)
        part_count = base_series.part_count
        if part_count <= 1:
            return base_series
        # Merge additional parts
        instances: list[Instance] = list(base_series.instances)
        for p in range(1, part_count):
            part_doc = await self._store.get(SERIES_COLLECTION, f"{series_id}__p{p}")
            if part_doc is not None:
                for inst in part_doc.get("instances", []):
                    instances.append(Instance.model_validate(inst))
        base_series.instances = instances
        base_series.instance_count = len(instances)
        return base_series

    async def get_series_for_study(self, study: StudyRecord) -> list[Series]:
        """Read every series for a study — 1 read per series.

        Uses ``study.series_ids`` for direct document gets, not a query.
        """
        series_list: list[Series] = []
        for series_id in study.series_ids:
            series = await self.get_series_by_id(series_id)
            if series is not None:
                series_list.append(series)
        return series_list


def _study_record_from_doc(doc: dict[str, Any]) -> StudyRecord:
    """Build a :class:`StudyRecord` from a Firestore document dict.

    Handles snake_case and camelCase field names (Firestore stores camelCase
    via ``model_dump`` on :class:`CamelModel``, but tests may use either).
    """
    from app.models.study import AssignedTo, PriorStudyRef, StudyPriority, StudyStatus

    def _get(key_snake: str, key_camel: str) -> Any:
        return doc.get(key_camel, doc.get(key_snake))

    assigned_raw = _get("assigned_to", "assignedTo")
    assigned: AssignedTo | None = None
    if isinstance(assigned_raw, dict):
        assigned = AssignedTo(
            uid=str(assigned_raw.get("uid", "")),
            operator_id=str(assigned_raw.get("operatorId", assigned_raw.get("operator_id", ""))),
            display_name=str(assigned_raw.get("displayName", assigned_raw.get("display_name", ""))),
        )

    prior_raw = _get("prior_studies", "priorStudies")
    prior_studies: list[PriorStudyRef] = []
    if isinstance(prior_raw, list):
        for p in prior_raw:
            if isinstance(p, dict):
                prior_studies.append(
                    PriorStudyRef(
                        study_id=str(p.get("studyId", p.get("study_id", ""))),
                        study_date=str(p.get("studyDate", p.get("study_date", ""))),
                        modality=str(p.get("modality", "")),
                        body_part=str(p.get("bodyPart", p.get("body_part", ""))),
                        description=str(p.get("description", "")),
                    )
                )

    status_raw = _get("status", "status")
    try:
        status = StudyStatus(status_raw) if status_raw else StudyStatus.UNREAD
    except ValueError:
        status = StudyStatus.UNREAD

    priority_raw = _get("priority", "priority")
    try:
        priority = StudyPriority(priority_raw) if priority_raw else StudyPriority.ROUTINE
    except ValueError:
        priority = StudyPriority.ROUTINE

    return StudyRecord(
        study_id=_get("study_id", "studyId") or "",
        patient_key=_get("patient_key", "patientKey") or "",
        patient_ref=_get("patient_ref", "patientRef") or "",
        patient_age_sex=_get("patient_age_sex", "patientAgeSex") or "",
        patient_sex=_get("patient_sex", "patientSex") or "",
        patient_name=_get("patient_name", "patientName") or "",
        patient_birth_date=_get("patient_birth_date", "patientBirthDate") or "",
        mrn=_get("mrn", "mrn") or "",
        accession=_get("accession", "accession") or "",
        modality=_get("modality", "modality") or "",
        body_part=_get("body_part", "bodyPart") or "",
        description=_get("description", "description") or "",
        study_date=_get("study_date", "studyDate") or "",
        referring_physician=_get("referring_physician", "referringPhysician") or "",
        clinical_history=_get("clinical_history", "clinicalHistory") or "",
        status=status,
        priority=priority,
        assigned_to=assigned,
        series_count=_get("series_count", "seriesCount") or 0,
        instance_count=_get("instance_count", "instanceCount") or 0,
        study_bytes=_get("study_bytes", "studyBytes") or 0,
        has_report=_get("has_report", "hasReport") or False,
        report_id=_get("report_id", "reportId"),
        signed_at=_get("signed_at", "signedAt"),
        prior_studies=prior_studies,
        series_ids=_get("series_ids", "seriesIds") or [],
        tenant_id=_get("tenant_id", "tenantId") or "default",
        created_at=_get("created_at", "createdAt") or "",
        updated_at=_get("updated_at", "updatedAt") or "",
        version=_get("version", "version") or 1,
    )
