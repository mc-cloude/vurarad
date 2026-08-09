"""Erasure service — patient erasure cascades through the research workbench (WP17).

``erase_patient`` removes a patient's footprint across both the clinical and
research sides: cohort subjects, de-ID objects (the ``deid_links`` mapping),
features, and labels are deleted, and affected analyses are marked
``STALE_SUBJECT_REMOVED`` (criterion 5).  It is the compliance/destruction path
and requires the ``patient:erase`` capability — enforced by the
:class:`DeidLinkRepository` reads that resolve pseudonyms from a ``patientKey``
(criterion 4: ``cohort:*`` grants no access).
"""

from __future__ import annotations

import logging
from enum import StrEnum

from app.core.auth import AuthenticatedUser
from app.core.capabilities import Capability
from app.models.common import CamelModel
from app.repositories.base import DocumentStore
from app.repositories.cohort_repo import CohortRepository
from app.repositories.deid_link_repo import DeidLinkRepository
from app.services.audit_service import AuditService
from app.services.cohort_subject_service import RESEARCH_FEATURES_COLLECTION

logger = logging.getLogger("vurarad.erasure")

RESEARCH_LABELS_COLLECTION = "research_labels"
ANALYSES_COLLECTION = "analyses"


class AnalysisStatus(StrEnum):
    """Lifecycle of a research analysis.

    ``STALE_SUBJECT_REMOVED`` is the terminal marker set when a subject is
    erased out from under an analysis (criterion 5).
    """

    ACTIVE = "ACTIVE"
    STALE_SUBJECT_REMOVED = "STALE_SUBJECT_REMOVED"


class ErasureResult(CamelModel):
    """Summary of a patient erasure cascade."""

    patient_key: str
    subjects_removed: int = 0
    segmentations_removed: int = 0
    features_removed: int = 0
    labels_removed: int = 0
    analyses_marked_stale: int = 0
    links_removed: int = 0


class ErasureService:
    """Erase a patient across cohort subjects, de-ID objects, features, labels."""

    def __init__(
        self,
        cohort_repo: CohortRepository,
        deid_link_repo: DeidLinkRepository,
        store: DocumentStore,
        audit: AuditService,
    ) -> None:
        self._cohort_repo = cohort_repo
        self._deid_link_repo = deid_link_repo
        self._store = store
        self._audit = audit

    async def erase_patient(
        self,
        patient_key: str,
        *,
        capabilities: frozenset[Capability],
        actor: AuthenticatedUser | None = None,
    ) -> ErasureResult:
        """Erase ``patient_key`` across the research workbench.

        Requires ``patient:erase`` (enforced by the deid_link read).  Deletes
        cohort subjects, segmentations, features, labels, and deid_links, and
        marks analyses ``STALE_SUBJECT_REMOVED``.
        """
        # Resolve every pseudonym linked to this patient — the read is gated by
        # patient:erase (raises PermissionDeniedError if absent).
        links = await self._deid_link_repo.query_by_patient_key(
            patient_key, capabilities=capabilities
        )

        result = ErasureResult(patient_key=patient_key)

        for pseudonym, _doc in links:
            # Cohort subject
            await self._cohort_repo.delete_subject(pseudonym)
            result.subjects_removed += 1

            # Segmentation aggregate for the subject
            seg = await self._cohort_repo.find_segmentation_for_subject(pseudonym)
            if seg is not None:
                await self._cohort_repo.delete_segmentation(seg.segmentation_id)
                result.segmentations_removed += 1

            # Features
            result.features_removed += await self._delete_by_subject(
                RESEARCH_FEATURES_COLLECTION, pseudonym
            )
            # Labels
            result.labels_removed += await self._delete_by_subject(
                RESEARCH_LABELS_COLLECTION, pseudonym
            )
            # Analyses — mark stale, do not delete (auditability)
            result.analyses_marked_stale += await self._mark_analyses_stale(pseudonym)

            # The deid_link itself
            await self._deid_link_repo.delete_link(pseudonym, capabilities=capabilities)
            result.links_removed += 1

        await self._audit.record(
            event_type="PATIENT_ERASED",
            actor=actor.uid if actor else "system",
            second_factor=actor.is_mfa_verified if actor else False,
            detail={
                "patientKey": patient_key,
                "subjectsRemoved": result.subjects_removed,
                "featuresRemoved": result.features_removed,
                "labelsRemoved": result.labels_removed,
                "analysesMarkedStale": result.analyses_marked_stale,
            },
            patient_key=patient_key,
        )
        return result

    async def _delete_by_subject(self, collection: str, subject_id: str) -> int:
        """Delete every document in ``collection`` whose ``subjectId`` matches."""
        rows = await self._store.query(collection, where=[("subjectId", "==", subject_id)])
        for doc_id, _doc in rows:
            await self._store.delete(collection, doc_id)
        return len(rows)

    async def _mark_analyses_stale(self, subject_id: str) -> int:
        """Mark every analysis for ``subject_id`` ``STALE_SUBJECT_REMOVED``."""
        rows = await self._store.query(
            ANALYSES_COLLECTION, where=[("subjectId", "==", subject_id)]
        )
        for doc_id, doc in rows:
            doc["status"] = AnalysisStatus.STALE_SUBJECT_REMOVED.value
            await self._store.set(ANALYSES_COLLECTION, doc_id, doc)
        return len(rows)


__all__ = [
    "ANALYSES_COLLECTION",
    "RESEARCH_LABELS_COLLECTION",
    "AnalysisStatus",
    "ErasureResult",
    "ErasureService",
]
