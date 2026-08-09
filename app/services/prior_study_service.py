"""Prior-study resolution — same ``patientKey``, per-prior authorization (WP9).

Resolves the comparison priors for a study by matching ``patientKey``, then
authorizes EACH prior independently through :class:`StudyAccessPolicy`.  A
prior the caller may not read (e.g. assigned to another radiologist) is
**omitted** from the response — the request never fails with 403 for the
whole list.  Only the primary study's own authorization can fail the request
(criterion 1).

Priors carry ``patientRef`` only — never a patient name, DOB, or MRN
(criterion 2).  This service has no dependency on the report model.
"""

from __future__ import annotations

from app.core.auth import AuthenticatedUser
from app.core.errors import NotAssignedError, NotFoundError, PermissionDeniedError
from app.models.study import PriorStudy, PriorStudyListResponse, StudyRecord, ViewerScope
from app.repositories.study_repo import StudyRepository
from app.services.access_policy import StudyAccessPolicy

# Safety cap on the number of same-patient studies scanned for priors.
_PRIOR_SCAN_LIMIT: int = 500


class PriorStudyService:
    """Resolve and authorize prior studies for the compare-prior viewer."""

    def __init__(
        self,
        study_repo: StudyRepository,
        policy: StudyAccessPolicy | None = None,
    ) -> None:
        self._study_repo = study_repo
        self._policy = policy or StudyAccessPolicy()

    async def get_priors(
        self,
        user: AuthenticatedUser,
        study_id: str,
        viewer_scope: ViewerScope | None = None,
    ) -> PriorStudyListResponse:
        """Return the authorized priors for ``study_id``.

        - 404 NOT_FOUND if the primary study does not exist.
        - The primary study is authorized normally (may 403 NOT_ASSIGNED /
          PERMISSION_DENIED and fail the whole request).
        - Each prior is authorized independently; unreadable priors are
          omitted and counted in ``omittedCount``.
        """
        primary = await self._study_repo.get_study(study_id)
        if primary is None:
            raise NotFoundError(f"Study {study_id} not found")
        # Primary-study authorization CAN fail the whole request.
        self._policy.assert_can_read(user, primary, viewer_scope)

        if not primary.patient_key:
            return PriorStudyListResponse(
                study_id=study_id,
                patient_ref=primary.patient_ref,
                priors=[],
                omitted_count=0,
            )

        records = await self._study_repo.get_studies_by_patient_key(
            primary.patient_key,
            exclude_study_id=study_id,
            limit=_PRIOR_SCAN_LIMIT,
        )

        priors: list[PriorStudy] = []
        omitted = 0
        for rec in records:
            try:
                self._policy.assert_can_read(user, rec, viewer_scope)
            except (NotAssignedError, PermissionDeniedError):
                # Omit, do not fail the request (criterion 1).
                omitted += 1
                continue
            priors.append(_to_prior(rec))

        # Most recent first — stable on equal dates by study_id.
        priors.sort(key=lambda p: (p.study_date, p.study_id), reverse=True)
        return PriorStudyListResponse(
            study_id=study_id,
            patient_ref=primary.patient_ref,
            priors=priors,
            omitted_count=omitted,
        )


def _to_prior(rec: StudyRecord) -> PriorStudy:
    """Map a prior :class:`StudyRecord` to a PHI-free :class:`PriorStudy`."""
    return PriorStudy(
        study_id=rec.study_id,
        patient_ref=rec.patient_ref,
        study_date=rec.study_date,
        modality=rec.modality,
        body_part=rec.body_part,
        description=rec.description,
        status=rec.status,
    )


__all__ = ["PriorStudyService"]
