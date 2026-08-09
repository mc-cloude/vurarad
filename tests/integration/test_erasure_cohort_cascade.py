"""Erasure cascade across the research workbench (WP17 — criterion 4, 5).

``ErasureService.erase_patient`` removes a patient's cohort subjects, de-ID
objects (``deid_links``), features, and labels, and marks affected analyses
``STALE_SUBJECT_REMOVED``.  The cascade is exercised end-to-end against the real
services and an in-memory document store.  The ``patient:erase`` capability gate
on ``deid_links`` reads (criterion 4) is asserted: a caller lacking it is
rejected with ``403 PERMISSION_DENIED``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.capabilities import Capability, Role
from app.core.errors import PermissionDeniedError
from app.models.cohort import Cohort, CohortSubjectStatus, SegmentationSource
from app.repositories.base import InMemoryDocumentStore
from app.repositories.cohort_repo import CohortRepository
from app.repositories.deid_link_repo import DEID_LINKS_COLLECTION, DeidLinkRepository
from app.repositories.study_repo import StudyRepository
from app.services.audit_service import AuditService
from app.services.cohort_segmentation_service import (
    CohortSegmentationService,
    StubResearchSegmenter,
)
from app.services.cohort_subject_service import RESEARCH_FEATURES_COLLECTION, CohortSubjectService
from app.services.deid_pipeline import StubDeidPipeline
from app.services.erasure_service import (
    ANALYSES_COLLECTION,
    RESEARCH_LABELS_COLLECTION,
    AnalysisStatus,
    ErasureService,
)
from tests.conftest import make_user

PATIENT_KEY = "pk_test"


def _study_doc() -> dict[str, object]:
    return {
        "studyId": "st_test",
        "patientKey": PATIENT_KEY,
        "patientRef": "PT-1",
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "accession": "ACC-1",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:14:00Z",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "seriesCount": 1,
        "instanceCount": 100,
        "studyBytes": 1000,
        "hasReport": False,
        "priorStudies": [],
        "seriesIds": [],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


def _seed_cohort(doc_store: InMemoryDocumentStore) -> str:
    cohort = Cohort(
        cohort_id="co_1",
        name="NSC-Lung",
        irb_reference="IRB-2026-0142",
        irb_determination="EXEMPT",
        created_by="op_r",
        created_at="2026-08-09T00:00:00Z",
        updated_at="2026-08-09T00:00:00Z",
    )
    asyncio.run(CohortRepository(doc_store).create_cohort(cohort))
    asyncio.run(doc_store.set("studies", "st_test", _study_doc()))
    return "co_1"


def _build_subject_service(
    doc_store: InMemoryDocumentStore, audit: AuditService
) -> CohortSubjectService:
    return CohortSubjectService(
        CohortRepository(doc_store),
        StubDeidPipeline(DeidLinkRepository(doc_store)),
        doc_store,
        audit,
        study_repo=StudyRepository(doc_store),
    )


def _seed_research_artifacts(
    doc_store: InMemoryDocumentStore, subject_id: str
) -> None:
    # A label for the subject (research labels collection).
    asyncio.run(
        doc_store.set(
            RESEARCH_LABELS_COLLECTION,
            "ls_1",
            {"labelId": "ls_1", "subjectId": subject_id, "outcome": "EGFR_NEGATIVE_REF"},
        )
    )
    # An active analysis referencing the subject.
    asyncio.run(
        doc_store.set(
            ANALYSES_COLLECTION,
            "an_1",
            {
                "analysisId": "an_1",
                "subjectId": subject_id,
                "status": AnalysisStatus.ACTIVE.value,
            },
        )
    )


class TestErasureCascade:
    def test_erase_patient_removes_subject_features_labels_and_marks_analyses(
        self, doc_store: InMemoryDocumentStore
    ) -> None:
        audit = AuditService(InMemoryAuditMirror())
        cohort_id = _seed_cohort(doc_store)
        subject_service = _build_subject_service(doc_store, audit)
        user = make_user(role=Role.RESEARCHER, operator_id="op_r")

        # Add a subject through DeidPipeline (writes cohort subject + deid_link).
        subject = asyncio.run(subject_service.add_from_worklist(user, cohort_id, "st_test"))
        subject_id = subject.subject_id
        assert subject.status == CohortSubjectStatus.ACTIVE

        # Extract features (writes a research_features doc).
        feature = asyncio.run(subject_service.extract_features(user, cohort_id, subject_id))
        assert feature.subject_id == subject_id

        # Create a segmentation for the subject.
        seg_service = CohortSegmentationService(
            CohortRepository(doc_store),
            StubResearchSegmenter(),
            audit,
        )
        asyncio.run(
            seg_service.create_or_edit_segmentation(
                user, cohort_id, subject_id, source=SegmentationSource.MONAI,
            )
        )

        # Seed a label + an active analysis referencing the subject.
        _seed_research_artifacts(doc_store, subject_id)

        # Sanity: artifacts exist before erasure.
        assert asyncio.run(doc_store.get("cohort_subjects", subject_id)) is not None
        assert asyncio.run(doc_store.get(DEID_LINKS_COLLECTION, subject_id)) is not None
        feature_doc = asyncio.run(
            doc_store.get(RESEARCH_FEATURES_COLLECTION, feature.feature_id)
        )
        assert feature_doc is not None
        assert asyncio.run(doc_store.get(RESEARCH_LABELS_COLLECTION, "ls_1")) is not None
        analysis_before = asyncio.run(doc_store.get(ANALYSES_COLLECTION, "an_1"))
        assert analysis_before is not None
        assert analysis_before["status"] == AnalysisStatus.ACTIVE.value

        # Erase the patient — requires patient:erase.
        erasure = ErasureService(
            CohortRepository(doc_store),
            DeidLinkRepository(doc_store),
            doc_store,
            audit,
        )
        result = asyncio.run(
            erasure.erase_patient(
                PATIENT_KEY, capabilities=frozenset({Capability.PATIENT_ERASE}), actor=user
            )
        )

        # Cohort subject + deid_link removed.
        assert result.subjects_removed == 1
        assert result.links_removed == 1
        assert result.features_removed == 1
        assert result.labels_removed == 1
        assert result.segmentations_removed == 1
        assert result.analyses_marked_stale == 1
        assert asyncio.run(doc_store.get("cohort_subjects", subject_id)) is None
        assert asyncio.run(doc_store.get(DEID_LINKS_COLLECTION, subject_id)) is None
        # Features + labels removed.
        assert asyncio.run(doc_store.get(RESEARCH_FEATURES_COLLECTION, feature.feature_id)) is None
        assert asyncio.run(doc_store.get(RESEARCH_LABELS_COLLECTION, "ls_1")) is None
        # Analysis marked stale (not deleted — auditability).
        analysis_after = asyncio.run(doc_store.get(ANALYSES_COLLECTION, "an_1"))
        assert analysis_after is not None
        assert analysis_after["status"] == AnalysisStatus.STALE_SUBJECT_REMOVED.value
        # Segmentation removed.
        assert (
            asyncio.run(CohortRepository(doc_store).find_segmentation_for_subject(subject_id))
            is None
        )

    def test_erase_without_patient_erase_is_denied(
        self, doc_store: InMemoryDocumentStore
    ) -> None:
        audit = AuditService(InMemoryAuditMirror())
        cohort_id = _seed_cohort(doc_store)
        subject_service = _build_subject_service(doc_store, audit)
        user = make_user(role=Role.RESEARCHER, operator_id="op_r")
        subject = asyncio.run(subject_service.add_from_worklist(user, cohort_id, "st_test"))

        erasure = ErasureService(
            CohortRepository(doc_store),
            DeidLinkRepository(doc_store),
            doc_store,
            audit,
        )
        # A caller holding only cohort:* capabilities cannot erase (deid_links
        # read requires patient:erase — criterion 4).
        cohort_only = frozenset(
            {
                Capability.COHORT_CREATE,
                Capability.COHORT_READ,
                Capability.COHORT_WRITE,
                Capability.COHORT_SUBJECT_ADD,
                Capability.COHORT_SEGMENTATION,
            }
        )
        with pytest.raises(PermissionDeniedError):
            asyncio.run(
                erasure.erase_patient(PATIENT_KEY, capabilities=cohort_only, actor=user)
            )
        # Nothing was removed.
        assert asyncio.run(doc_store.get("cohort_subjects", subject.subject_id)) is not None

    def test_erase_unknown_patient_is_a_noop(
        self, doc_store: InMemoryDocumentStore
    ) -> None:
        audit = AuditService(InMemoryAuditMirror())
        erasure = ErasureService(
            CohortRepository(doc_store),
            DeidLinkRepository(doc_store),
            doc_store,
            audit,
        )
        result = asyncio.run(
            erasure.erase_patient(
                "pk_nobody", capabilities=frozenset({Capability.PATIENT_ERASE})
            )
        )
        assert result.subjects_removed == 0
        assert result.links_removed == 0


# Defensive: importing AnalysisStatus must not pull clinical PHI models.
def test_analysis_status_values() -> None:
    assert AnalysisStatus.ACTIVE.value == "ACTIVE"
    assert AnalysisStatus.STALE_SUBJECT_REMOVED.value == "STALE_SUBJECT_REMOVED"
