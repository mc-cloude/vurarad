"""Study access policy — ownership/status gate per §6.3.3.

Role is the first gate (enforced by ``require_capability`` / ``require_phi_capability``
at the router).  Ownership is a second, independent gate enforced here, in the
service layer, on every study/series/URL access.

Predicates:

- **radiologist**: may read when ``assignedTo.uid == user.uid`` OR when
  ``assignedTo is None`` and ``status == UNREAD`` (an unclaimed item in the
  shared queue).  A study assigned to **another** radiologist is
  ``403 NOT_ASSIGNED``.  Write/sign requires ``assignedTo.uid == user.uid``.
- **viewer**: may read only when ``status == SIGNED`` AND the study is in the
  viewer's ``viewerScope.studyIds`` or matches ``viewerScope.referringPhysicians``.
  ``viewerScope`` defaults to empty, so a freshly created viewer can read nothing.
- **admin**: never consulted — the router rejects with ``403 PHI_ACCESS_FORBIDDEN``
  before the policy is reached, because admin has zero PHI capabilities.
"""

from __future__ import annotations

from app.core.auth import AuthenticatedUser
from app.core.capabilities import Role
from app.core.errors import NotAssignedError, PermissionDeniedError
from app.models.study import StudyRecord, StudyStatus, ViewerScope


class StudyAccessPolicy:
    """Ownership and status predicates — the second authorisation gate."""

    def assert_can_read(
        self,
        user: AuthenticatedUser,
        study: StudyRecord,
        viewer_scope: ViewerScope | None = None,
    ) -> None:
        """Raise if ``user`` may not read ``study``.

        - radiologist: assigned-to-self OR unassigned-UNREAD; else NOT_ASSIGNED.
        - viewer: SIGNED AND in viewerScope; else PERMISSION_DENIED.
        """
        if user.role == Role.RADIOLOGIST:
            self._assert_radiologist_read(user, study)
        elif user.role == Role.VIEWER:
            self._assert_viewer_read(study, viewer_scope or ViewerScope())
        # admin is rejected at the router; this line is unreachable in practice.

    def assert_can_write(self, user: AuthenticatedUser, study: StudyRecord) -> None:
        """Raise if ``user`` may not write ``study``.

        Radiologist-only; requires ``assignedTo.uid == user.uid``.
        """
        if user.role == Role.RADIOLOGIST:
            if study.assigned_to is None or study.assigned_to.uid != user.uid:
                raise NotAssignedError("Only the assigned radiologist may modify this study")
        else:
            raise PermissionDeniedError("Only a radiologist may modify a study")

    def assert_can_sign(self, user: AuthenticatedUser, study: StudyRecord) -> None:
        """Raise if ``user`` may not sign a report on ``study``.

        Radiologist-only; requires ``assignedTo.uid == user.uid``.
        Fresh 2FA is enforced separately by ``require_mfa``.
        """
        if user.role == Role.RADIOLOGIST:
            if study.assigned_to is None or study.assigned_to.uid != user.uid:
                raise NotAssignedError("Only the assigned radiologist may sign this study")
        else:
            raise PermissionDeniedError("Only a radiologist may sign a report")

    # -- radiologist ---------------------------------------------------------
    @staticmethod
    def _assert_radiologist_read(user: AuthenticatedUser, study: StudyRecord) -> None:
        assigned = study.assigned_to
        if assigned is not None:
            if assigned.uid != user.uid:
                raise NotAssignedError("Study is assigned to another reader")
            return
        # Unassigned: readable only when UNREAD (shared queue).
        if study.status != StudyStatus.UNREAD:
            raise NotAssignedError("Unassigned study is not UNREAD; claim it first")

    # -- viewer --------------------------------------------------------------
    @staticmethod
    def _assert_viewer_read(study: StudyRecord, scope: ViewerScope) -> None:
        if study.status != StudyStatus.SIGNED:
            raise PermissionDeniedError("Viewer may only read SIGNED studies")
        if study.study_id in scope.study_ids:
            return
        if study.referring_physician and study.referring_physician in scope.referring_physicians:
            return
        raise PermissionDeniedError("Study is not in the viewer's scope")
