"""Cohort service — CRUD, IRB reference capture, retention (WP17).

The single place cohorts are created, listed, and archived.  Enforces:

- **IRB capture** (criterion 7): ``irbReference`` and ``irbDetermination`` are
  required — de-identification reduces but does not automatically remove IRB
  obligations, so the determination is captured, not assumed away.  The model
  validator is the structural guard; the service double-checks.
- **Retention**: a cohort past its ``retentionDays`` may be marked
  ``RETENTION_EXPIRED``.
- Every returned :class:`Cohort` is de-identified and RUO — no ``studyId`` /
  ``patientKey``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import ConflictError, NotFoundError
from app.models.cohort import Cohort, CohortStatus, IrbDetermination
from app.repositories.cohort_repo import CohortRepository
from app.services.audit_service import AuditService

logger = logging.getLogger("vurarad.cohort")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CohortService:
    """Cohort CRUD + IRB capture + retention."""

    def __init__(self, repo: CohortRepository, audit: AuditService) -> None:
        self._repo = repo
        self._audit = audit

    # -- create (route 60) --------------------------------------------------
    async def create_cohort(
        self,
        user: AuthenticatedUser,
        *,
        name: str,
        description: str,
        irb_reference: str,
        irb_determination: IrbDetermination,
        retention_days: int = 365,
    ) -> Cohort:
        """Create a cohort.  IRB reference + determination are required."""
        if not irb_reference.strip() or not irb_determination.strip():
            raise ConflictError("irbReference and irbDetermination are required")
        now = _now()
        cohort = Cohort(
            cohort_id=f"co_{ULID()}",
            name=name,
            description=description,
            irb_reference=irb_reference,
            irb_determination=irb_determination,
            status=CohortStatus.ACTIVE,
            retention_days=retention_days,
            created_by=user.operator_id or user.uid,
            created_at=now,
            updated_at=now,
        )
        created = await self._repo.create_cohort(cohort)
        if not created:
            raise ConflictError("Cohort already exists")
        await self._audit.record(
            event_type="COHORT_CREATED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"cohortId": cohort.cohort_id, "irbReference": irb_reference},
        )
        return cohort

    # -- read (route 61) ----------------------------------------------------
    async def get_cohort(self, cohort_id: str) -> Cohort:
        cohort = await self._repo.get_cohort(cohort_id)
        if cohort is None:
            raise NotFoundError(f"Cohort {cohort_id} not found")
        return cohort

    async def list_cohorts(self) -> list[Cohort]:
        return await self._repo.list_cohorts()

    # -- archive ------------------------------------------------------------
    async def archive_cohort(self, user: AuthenticatedUser, cohort_id: str) -> Cohort:
        cohort = await self.get_cohort(cohort_id)
        if cohort.status == CohortStatus.ARCHIVED:
            return cohort
        await self._repo.update_cohort(
            cohort_id, {"status": CohortStatus.ARCHIVED.value, "updatedAt": _now()}
        )
        await self._audit.record(
            event_type="COHORT_ARCHIVED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"cohortId": cohort_id},
        )
        cohort.status = CohortStatus.ARCHIVED
        return cohort

    # -- retention ----------------------------------------------------------
    async def expire_retention(self, user: AuthenticatedUser, cohort_id: str) -> Cohort:
        """Mark a cohort ``RETENTION_EXPIRED`` once its retention window has passed."""
        cohort = await self.get_cohort(cohort_id)
        # A cohort is retention-due when its creation + retentionDays is in the
        # past.  ``created_at`` is an ISO-8601 ``Z`` string.
        created = datetime.fromisoformat(cohort.created_at.replace("Z", "+00:00"))
        due = created.timestamp() + cohort.retention_days * 86400
        if datetime.now(UTC).timestamp() < due:
            raise ConflictError("Cohort retention window has not yet elapsed")
        await self._repo.update_cohort(
            cohort_id,
            {"status": CohortStatus.RETENTION_EXPIRED.value, "updatedAt": _now()},
        )
        await self._audit.record(
            event_type="COHORT_RETENTION_EXPIRED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"cohortId": cohort_id, "retentionDays": cohort.retention_days},
        )
        cohort.status = CohortStatus.RETENTION_EXPIRED
        return cohort


__all__ = ["CohortService"]
