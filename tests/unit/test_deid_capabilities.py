"""Unit tests for the deid:review capability — criterion 8.

``deid:review`` is a PHI capability held by no default role (granted out-of-band
to designated reviewers), so admin never holds it — separation of duties.
"""

from __future__ import annotations

from app.core.capabilities import (
    PHI_CAPABILITIES,
    ROLE_CAPABILITIES,
    Capability,
    Role,
    is_phi_capability,
)


def test_deid_review_exists() -> None:
    assert Capability.DEID_REVIEW == "deid:review"


def test_deid_review_is_a_phi_capability() -> None:
    assert Capability.DEID_REVIEW in PHI_CAPABILITIES
    assert is_phi_capability(Capability.DEID_REVIEW) is True


def test_no_default_role_holds_deid_review() -> None:
    """deid:review is granted out-of-band, never by a default role."""
    for role in Role:
        assert Capability.DEID_REVIEW not in ROLE_CAPABILITIES[role], (
            f"role {role} must not hold deid:review by default"
        )


def test_admin_does_not_hold_deid_review() -> None:
    assert Capability.DEID_REVIEW not in ROLE_CAPABILITIES[Role.ADMIN]
    # The broader separation-of-duties invariant still holds.
    assert ROLE_CAPABILITIES[Role.ADMIN] & PHI_CAPABILITIES == frozenset()
