"""Cohort repository — ``cohorts``, ``cohort_subjects``, ``cohort_segmentations``.

The three research collections are de-identified: no document here stores a
``studyId`` or ``patientKey``.  The re-identification mapping lives only in the
write-restricted ``deid_links`` collection (see :mod:`app.repositories.deid_link_repo`),
which this repository never reads.

Erasure (``ErasureService``) deletes a subject's cohort subject and segmentation
documents here; the features/labels/analyses cascade lives in the erasure
service because those collections span more than cohorts.
"""

from __future__ import annotations

from app.models.cohort import Cohort, CohortSegmentation, CohortSubject
from app.repositories.base import DocumentStore

COHORTS_COLLECTION = "cohorts"
COHORT_SUBJECTS_COLLECTION = "cohort_subjects"
COHORT_SEGMENTATIONS_COLLECTION = "cohort_segmentations"


class CohortRepository:
    """CRUD over the three de-identified cohort collections."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    # -- cohorts ------------------------------------------------------------
    async def create_cohort(self, cohort: Cohort) -> bool:
        """Atomically create a cohort document; ``False`` if it already exists."""
        return await self._store.create(
            COHORTS_COLLECTION, cohort.cohort_id, cohort.model_dump(by_alias=True)
        )

    async def get_cohort(self, cohort_id: str) -> Cohort | None:
        doc = await self._store.get(COHORTS_COLLECTION, cohort_id)
        if doc is None:
            return None
        return Cohort.model_validate(doc)

    async def list_cohorts(self, *, limit: int = 100) -> list[Cohort]:
        rows = await self._store.query(COHORTS_COLLECTION, where=None, limit=limit)
        return [Cohort.model_validate(doc) for _doc_id, doc in rows]

    async def update_cohort(self, cohort_id: str, data: dict[str, object]) -> None:
        await self._store.update(COHORTS_COLLECTION, cohort_id, data)

    # -- subjects -----------------------------------------------------------
    async def add_subject(self, subject: CohortSubject) -> bool:
        """Atomically create a subject document; ``False`` if it already exists."""
        return await self._store.create(
            COHORT_SUBJECTS_COLLECTION,
            subject.subject_id,
            subject.model_dump(by_alias=True),
        )

    async def get_subject(self, subject_id: str) -> CohortSubject | None:
        doc = await self._store.get(COHORT_SUBJECTS_COLLECTION, subject_id)
        if doc is None:
            return None
        return CohortSubject.model_validate(doc)

    async def list_subjects(self, cohort_id: str) -> list[CohortSubject]:
        rows = await self._store.query(
            COHORT_SUBJECTS_COLLECTION, where=[("cohortId", "==", cohort_id)]
        )
        return [CohortSubject.model_validate(doc) for _doc_id, doc in rows]

    async def update_subject(self, subject_id: str, data: dict[str, object]) -> None:
        await self._store.update(COHORT_SUBJECTS_COLLECTION, subject_id, data)

    async def delete_subject(self, subject_id: str) -> None:
        await self._store.delete(COHORT_SUBJECTS_COLLECTION, subject_id)

    # -- segmentation -------------------------------------------------------
    async def upsert_segmentation(self, seg: CohortSegmentation) -> None:
        await self._store.set(
            COHORT_SEGMENTATIONS_COLLECTION,
            seg.segmentation_id,
            seg.model_dump(by_alias=True),
        )

    async def get_segmentation(self, segmentation_id: str) -> CohortSegmentation | None:
        doc = await self._store.get(COHORT_SEGMENTATIONS_COLLECTION, segmentation_id)
        if doc is None:
            return None
        return CohortSegmentation.model_validate(doc)

    async def find_segmentation_for_subject(
        self, subject_id: str
    ) -> CohortSegmentation | None:
        """Return the segmentation aggregate for a subject, if any."""
        rows = await self._store.query(
            COHORT_SEGMENTATIONS_COLLECTION, where=[("subjectId", "==", subject_id)]
        )
        if not rows:
            return None
        _doc_id, doc = rows[0]
        return CohortSegmentation.model_validate(doc)

    async def delete_segmentation(self, segmentation_id: str) -> None:
        await self._store.delete(COHORT_SEGMENTATIONS_COLLECTION, segmentation_id)


__all__ = [
    "COHORTS_COLLECTION",
    "COHORT_SEGMENTATIONS_COLLECTION",
    "COHORT_SUBJECTS_COLLECTION",
    "CohortRepository",
]
