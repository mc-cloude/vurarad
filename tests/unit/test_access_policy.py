"""StudyAccessPolicy — full role × ownership × status cross-product (§6.3.3).

Enumerated cell by cell: 3 roles × every ownership state × every action × every
study status.  The table in §6.3.2 and this test cannot diverge — if someone
changes a predicate, the corresponding cell fails with a message pointing at
the section.
"""

from __future__ import annotations

import pytest

from app.core.auth import AuthenticatedUser
from app.core.capabilities import Role
from app.core.errors import NotAssignedError, PermissionDeniedError
from app.models.study import AssignedTo, StudyRecord, StudyStatus, ViewerScope
from app.services.access_policy import StudyAccessPolicy
from tests.conftest import make_user

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
policy = StudyAccessPolicy()

RAD_UID = "rad-alice"
RAD_OTHER = "rad-bob"
VIEWER_UID = "viewer-carol"


def _study(
    *,
    status: StudyStatus = StudyStatus.UNREAD,
    assigned_to: AssignedTo | None = None,
    study_id: str = "st_test",
    referring_physician: str = "",
) -> StudyRecord:
    return StudyRecord(
        study_id=study_id,
        status=status,
        assigned_to=assigned_to,
        referring_physician=referring_physician,
    )


def _rad(uid: str = RAD_UID) -> AuthenticatedUser:
    return make_user(uid=uid, role=Role.RADIOLOGIST)


def _viewer(uid: str = VIEWER_UID) -> AuthenticatedUser:
    return make_user(uid=uid, role=Role.VIEWER)


def _admin() -> AuthenticatedUser:
    return make_user(uid="admin-1", role=Role.ADMIN)


def _assigned(uid: str) -> AssignedTo:
    return AssignedTo(uid=uid, operator_id="RAD-0001", display_name="Test")


ALL_STATUSES = list(StudyStatus)


# ---------------------------------------------------------------------------
# assert_can_read — radiologist
# ---------------------------------------------------------------------------
class TestRadiologistRead:
    """radiologist: own OR unassigned-UNREAD; else NOT_ASSIGNED."""

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_assigned_to_self_reads_any_status(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=_assigned(RAD_UID))
        policy.assert_can_read(_rad(), study)  # no raise

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_assigned_to_other_is_not_assigned(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=_assigned(RAD_OTHER))
        with pytest.raises(NotAssignedError):
            policy.assert_can_read(_rad(), study)

    def test_unassigned_unread_succeeds(self) -> None:
        study = _study(status=StudyStatus.UNREAD, assigned_to=None)
        policy.assert_can_read(_rad(), study)  # no raise

    @pytest.mark.parametrize(
        "status",
        [s for s in ALL_STATUSES if s != StudyStatus.UNREAD],
    )
    def test_unassigned_non_unread_is_not_assigned(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=None)
        with pytest.raises(NotAssignedError):
            policy.assert_can_read(_rad(), study)


# ---------------------------------------------------------------------------
# assert_can_read — viewer
# ---------------------------------------------------------------------------
class TestViewerRead:
    """viewer: SIGNED AND in viewerScope; else PERMISSION_DENIED."""

    def test_signed_in_scope_succeeds(self) -> None:
        study = _study(status=StudyStatus.SIGNED, study_id="st_123")
        scope = ViewerScope(study_ids={"st_123"})
        policy.assert_can_read(_viewer(), study, scope)  # no raise

    def test_signed_matching_referring_physician_succeeds(self) -> None:
        study = _study(status=StudyStatus.SIGNED, referring_physician="Dr. X")
        scope = ViewerScope(referring_physicians={"Dr. X"})
        policy.assert_can_read(_viewer(), study, scope)  # no raise

    @pytest.mark.parametrize(
        "status",
        [s for s in ALL_STATUSES if s != StudyStatus.SIGNED],
    )
    def test_non_signed_is_denied(self, status: StudyStatus) -> None:
        study = _study(status=status, study_id="st_123")
        scope = ViewerScope(study_ids={"st_123"})
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_read(_viewer(), study, scope)

    def test_signed_outside_scope_is_denied(self) -> None:
        study = _study(status=StudyStatus.SIGNED, study_id="st_999")
        scope = ViewerScope(study_ids={"st_123"})
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_read(_viewer(), study, scope)

    def test_signed_empty_scope_is_denied(self) -> None:
        """Freshly created viewer with empty scope can read nothing."""
        study = _study(status=StudyStatus.SIGNED, study_id="st_123")
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_read(_viewer(), study, ViewerScope())

    def test_signed_no_scope_provided_is_denied(self) -> None:
        study = _study(status=StudyStatus.SIGNED, study_id="st_123")
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_read(_viewer(), study)


# ---------------------------------------------------------------------------
# assert_can_write — radiologist only, assigned-to-self
# ---------------------------------------------------------------------------
class TestWrite:
    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_radiologist_assigned_to_self_can_write(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=_assigned(RAD_UID))
        policy.assert_can_write(_rad(), study)  # no raise

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_radiologist_assigned_to_other_cannot_write(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=_assigned(RAD_OTHER))
        with pytest.raises(NotAssignedError):
            policy.assert_can_write(_rad(), study)

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_radiologist_unassigned_cannot_write(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=None)
        with pytest.raises(NotAssignedError):
            policy.assert_can_write(_rad(), study)

    def test_viewer_cannot_write(self) -> None:
        study = _study(status=StudyStatus.UNREAD, assigned_to=_assigned(VIEWER_UID))
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_write(_viewer(), study)

    def test_admin_cannot_write(self) -> None:
        study = _study(status=StudyStatus.UNREAD, assigned_to=_assigned("admin-1"))
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_write(_admin(), study)


# ---------------------------------------------------------------------------
# assert_can_sign — radiologist only, assigned-to-self
# ---------------------------------------------------------------------------
class TestSign:
    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_radiologist_assigned_to_self_can_sign(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=_assigned(RAD_UID))
        policy.assert_can_sign(_rad(), study)  # no raise

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_radiologist_assigned_to_other_cannot_sign(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=_assigned(RAD_OTHER))
        with pytest.raises(NotAssignedError):
            policy.assert_can_sign(_rad(), study)

    @pytest.mark.parametrize("status", ALL_STATUSES)
    def test_radiologist_unassigned_cannot_sign(self, status: StudyStatus) -> None:
        study = _study(status=status, assigned_to=None)
        with pytest.raises(NotAssignedError):
            policy.assert_can_sign(_rad(), study)

    def test_viewer_cannot_sign(self) -> None:
        study = _study(status=StudyStatus.SIGNED, assigned_to=_assigned(VIEWER_UID))
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_sign(_viewer(), study)

    def test_admin_cannot_sign(self) -> None:
        study = _study(status=StudyStatus.SIGNED, assigned_to=_assigned("admin-1"))
        with pytest.raises(PermissionDeniedError):
            policy.assert_can_sign(_admin(), study)
