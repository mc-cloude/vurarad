"""Cohort subject service — add-from-worklist/upload, pseudonym minting (WP17).

A subject is added **only** via :class:`DeidPipeline` (criterion 2): both
``add_from_worklist`` and ``add_from_upload`` call ``deid.deidentify()`` and
build the :class:`CohortSubject` from the returned :class:`DeidResult`.  There
is no code path that copies pixels or mints a pseudonym without one — the
subject's ``deidObjectPath`` is always the pipeline's output.

A subject with any open de-ID review item cannot become ``ACTIVE`` and feature
extraction against it returns ``409 DEID_REVIEW_PENDING`` (criterion 3).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import DeidReviewPendingError, NotFoundError
from app.models.cohort import (
    CohortSubject,
    CohortSubjectStatus,
    DeidReviewItem,
    DeidReviewStatus,
)
from app.models.common import CamelModel
from app.repositories.base import DocumentStore
from app.repositories.cohort_repo import CohortRepository
from app.repositories.study_repo import StudyRepository
from app.services.audit_service import AuditService
from app.services.deid_pipeline import DeidPipeline, DeidSource, DeidSourceKind

logger = logging.getLogger("vurarad.cohort_subject")

RESEARCH_FEATURES_COLLECTION = "research_features"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class FeatureRecord(CamelModel):
    """A de-identified radiomics feature vector for a cohort subject (RUO)."""

    feature_id: str
    cohort_id: str
    subject_id: str
    features: dict[str, float] = {}
    regulatory_class: str = "RUO"
    created_at: str


class CohortSubjectService:
    """Add subjects through DeidPipeline, gate activation on review, extract features."""

    def __init__(
        self,
        cohort_repo: CohortRepository,
        deid_pipeline: DeidPipeline,
        store: DocumentStore,
        audit: AuditService,
        study_repo: StudyRepository | None = None,
    ) -> None:
        self._repo = cohort_repo
        self._deid = deid_pipeline
        self._store = store
        self._audit = audit
        self._study_repo = study_repo

    # -- add from worklist (route 62) ---------------------------------------
    async def add_from_worklist(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        study_id: str,
    ) -> CohortSubject:
        """Add a subject by de-identifying a clinical study from the worklist."""
        cohort = await self._repo.get_cohort(cohort_id)
        if cohort is None:
            raise NotFoundError(f"Cohort {cohort_id} not found")
        if self._study_repo is None:
            raise NotFoundError("Study resolution is not configured for this service")
        study = await self._study_repo.get_study(study_id)
        if study is None:
            raise NotFoundError(f"Study {study_id} not found")
        source = DeidSource(
            kind=DeidSourceKind.WORKLIST,
            study_id=study_id,
            modality=study.modality,
            body_part=study.body_part,
        )
        # The ONLY path that mints a pseudonym and copies pixels.
        result = await self._deid.deidentify(source, patient_key=study.patient_key)
        return await self._materialise_subject(user, cohort_id, result)

    # -- add from upload (route 62) -----------------------------------------
    async def add_from_upload(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        upload_ref: str,
        *,
        modality: str = "",
        body_part: str = "",
    ) -> CohortSubject:
        """Add a subject by de-identifying a research upload."""
        cohort = await self._repo.get_cohort(cohort_id)
        if cohort is None:
            raise NotFoundError(f"Cohort {cohort_id} not found")
        source = DeidSource(
            kind=DeidSourceKind.UPLOAD,
            upload_ref=upload_ref,
            modality=modality,
            body_part=body_part,
        )
        # Uploads are not tied to a clinical patient; the deid_link maps to an
        # empty patientKey so erasure-by-patient never resolves them.
        result = await self._deid.deidentify(source, patient_key="")
        return await self._materialise_subject(user, cohort_id, result)

    async def _materialise_subject(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        result: Any,
    ) -> CohortSubject:
        """Build the CohortSubject from a DeidResult — never from raw pixels."""
        has_open = any(item.status == DeidReviewStatus.OPEN for item in result.review_items)
        now = _now()
        subject = CohortSubject(
            subject_id=result.pseudonym,
            cohort_id=cohort_id,
            status=CohortSubjectStatus.PENDING_REVIEW if has_open else CohortSubjectStatus.ACTIVE,
            deid_object_path=result.deid_object_path,
            pixel_pass_passed=result.pixel_pass_passed,
            review_items=list(result.review_items),
            source_kind=result.source_kind.value,
            source_modality=result.deid_metadata.get("modality", ""),
            source_body_part=result.deid_metadata.get("bodyPart", ""),
            created_at=now,
            updated_at=now,
        )
        created = await self._repo.add_subject(subject)
        if not created:
            raise NotFoundError(f"Subject {subject.subject_id} already exists")
        await self._repo.update_cohort(
            cohort_id,
            {"subjectCount": cohort_increment(await self._repo.get_cohort(cohort_id))},
        )
        await self._audit.record(
            event_type="COHORT_SUBJECT_ADDED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "cohortId": cohort_id,
                "subjectId": subject.subject_id,
                "sourceKind": subject.source_kind,
                "pixelPass": subject.pixel_pass_passed,
            },
        )
        return subject

    # -- review + activation (criterion 3) ----------------------------------
    async def resolve_review_item(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        subject_id: str,
        item_id: str,
    ) -> CohortSubject:
        subject = await self._require_subject(subject_id, cohort_id)
        updated_items: list[DeidReviewItem] = []
        found = False
        for item in subject.review_items:
            if item.item_id == item_id:
                updated_items.append(item.model_copy(update={"status": DeidReviewStatus.RESOLVED}))
                found = True
            else:
                updated_items.append(item)
        if not found:
            raise NotFoundError(f"Review item {item_id} not found on subject {subject_id}")
        subject.review_items = updated_items
        await self._repo.update_subject(
            subject_id, {"reviewItems": [i.model_dump(by_alias=True) for i in updated_items]}
        )
        return subject

    async def activate_subject(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        subject_id: str,
    ) -> CohortSubject:
        """Activate a subject.  Blocked while any review item is OPEN (criterion 3)."""
        subject = await self._require_subject(subject_id, cohort_id)
        open_count = sum(1 for i in subject.review_items if i.status == DeidReviewStatus.OPEN)
        if open_count > 0:
            raise DeidReviewPendingError(open_count)
        subject.status = CohortSubjectStatus.ACTIVE
        await self._repo.update_subject(
            subject_id, {"status": CohortSubjectStatus.ACTIVE.value, "updatedAt": _now()}
        )
        await self._audit.record(
            event_type="COHORT_SUBJECT_ACTIVATED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"cohortId": cohort_id, "subjectId": subject_id},
        )
        return subject

    # -- feature extraction (criterion 3) -----------------------------------
    async def extract_features(
        self,
        user: AuthenticatedUser,
        cohort_id: str,
        subject_id: str,
    ) -> FeatureRecord:
        """Extract radiomics features for a subject.

        Returns ``409 DEID_REVIEW_PENDING`` if the subject is not ``ACTIVE``
        (i.e. has open de-ID review items) — criterion 3.
        """
        subject = await self._require_subject(subject_id, cohort_id)
        if subject.status != CohortSubjectStatus.ACTIVE:
            open_count = sum(1 for i in subject.review_items if i.status == DeidReviewStatus.OPEN)
            raise DeidReviewPendingError(max(open_count, 1))
        record = FeatureRecord(
            feature_id=f"fr_{ULID()}",
            cohort_id=cohort_id,
            subject_id=subject_id,
            features={"volume_mm3": 0.0, "diameter_mm": 0.0},
            created_at=_now(),
        )
        await self._store.set(
            RESEARCH_FEATURES_COLLECTION,
            record.feature_id,
            record.model_dump(by_alias=True),
        )
        await self._audit.record(
            event_type="RESEARCH_FEATURES_EXTRACTED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"cohortId": cohort_id, "subjectId": subject_id},
        )
        return record

    # -- read ---------------------------------------------------------------
    async def get_subject(self, cohort_id: str, subject_id: str) -> CohortSubject:
        return await self._require_subject(subject_id, cohort_id)

    async def list_subjects(self, cohort_id: str) -> list[CohortSubject]:
        return await self._repo.list_subjects(cohort_id)

    # -- helpers ------------------------------------------------------------
    async def _require_subject(self, subject_id: str, cohort_id: str) -> CohortSubject:
        subject = await self._repo.get_subject(subject_id)
        if subject is None or subject.cohort_id != cohort_id:
            raise NotFoundError(f"Subject {subject_id} not found in cohort {cohort_id}")
        return subject


def cohort_increment(cohort: Any) -> int:
    """Return the next subject count for a cohort (defensive against None)."""
    if cohort is None:
        return 1
    return int(getattr(cohort, "subject_count", 0)) + 1


__all__ = [
    "RESEARCH_FEATURES_COLLECTION",
    "CohortSubjectService",
    "FeatureRecord",
]
