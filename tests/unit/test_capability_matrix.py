"""The single RBAC authority — admin has zero PHI capabilities, matrix is correct."""

from __future__ import annotations

from app.core.capabilities import (
    PHI_CAPABILITIES,
    ROLE_CAPABILITIES,
    Capability,
    Role,
    get_role_capabilities,
    has_capability,
    is_phi_capability,
)


# ---------------------------------------------------------------------------
# Structural invariant — separation of duties (acceptance criterion #3)
# ---------------------------------------------------------------------------
def test_admin_has_zero_phi_capabilities() -> None:
    """admin ∩ PHI == ∅ — asserted at import time AND here."""
    assert ROLE_CAPABILITIES[Role.ADMIN] & PHI_CAPABILITIES == frozenset()


def test_admin_cannot_read_studies_or_reports() -> None:
    admin = ROLE_CAPABILITIES[Role.ADMIN]
    assert Capability.STUDY_READ not in admin
    assert Capability.REPORT_READ not in admin
    assert Capability.IMAGING_ACCESS not in admin


# ---------------------------------------------------------------------------
# Exact capability sets per role
# ---------------------------------------------------------------------------
def test_admin_capabilities() -> None:
    assert ROLE_CAPABILITIES[Role.ADMIN] == frozenset(
        {
            Capability.AUDIT_READ,
            Capability.AUDIT_EXPORT,
            Capability.ANALYTICS_READ,
            Capability.USER_MANAGE,
        }
    )


def test_viewer_capabilities() -> None:
    assert ROLE_CAPABILITIES[Role.VIEWER] == frozenset(
        {
            Capability.STUDY_READ,
            Capability.STUDY_SEARCH,
            Capability.REPORT_READ,
        }
    )


def test_radiologist_capabilities() -> None:
    assert ROLE_CAPABILITIES[Role.RADIOLOGIST] == frozenset(
        {
            Capability.STUDY_READ,
            Capability.STUDY_WRITE,
            Capability.STUDY_IMPORT,
            Capability.STUDY_SEARCH,
            Capability.STUDY_ANNOTATE,
            Capability.REPORT_READ,
            Capability.REPORT_WRITE,
            Capability.REPORT_SIGN,
            Capability.REPORT_ADDENDUM,
            Capability.IMAGING_ACCESS,
            Capability.AI_DRAFT,
            Capability.AI_FULL,
            Capability.BREAK_GLASS,
            Capability.RESEARCH_COHORT_CREATE,
            Capability.RESEARCH_FEATURES_READ,
            Capability.RESEARCH_EXPORT,
        }
    )


# ---------------------------------------------------------------------------
# Matrix completeness
# ---------------------------------------------------------------------------
def test_every_role_has_an_entry() -> None:
    for role in Role:
        assert role in ROLE_CAPABILITIES
        assert isinstance(ROLE_CAPABILITIES[role], frozenset)


def test_every_phi_capability_is_a_capability_member() -> None:
    for cap in PHI_CAPABILITIES:
        assert isinstance(cap, Capability)


def test_all_capability_values_are_unique() -> None:
    values = [c.value for c in Capability]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_get_role_capabilities_matches_matrix() -> None:
    for role in Role:
        assert get_role_capabilities(role) is ROLE_CAPABILITIES[role]


def test_has_capability_true_false() -> None:
    assert has_capability(Role.RADIOLOGIST, Capability.REPORT_SIGN) is True
    assert has_capability(Role.VIEWER, Capability.REPORT_SIGN) is False
    assert has_capability(Role.ADMIN, Capability.STUDY_READ) is False


def test_is_phi_capability() -> None:
    assert is_phi_capability(Capability.STUDY_READ) is True
    assert is_phi_capability(Capability.AUDIT_READ) is False


# ---------------------------------------------------------------------------:
# AI capabilities (WP6) — ai:draft / ai:full are PHI capabilities
# ---------------------------------------------------------------------------:
def test_ai_capabilities_are_phi() -> None:
    """Both AI capabilities are PHI capabilities → admin gets 403 on AI routes."""
    assert is_phi_capability(Capability.AI_DRAFT) is True
    assert is_phi_capability(Capability.AI_FULL) is True


def test_radiologist_has_ai_draft() -> None:
    """Radiologists may draft reports with AI; viewers and admins may not."""
    assert has_capability(Role.RADIOLOGIST, Capability.AI_DRAFT) is True
    assert has_capability(Role.VIEWER, Capability.AI_DRAFT) is False


def test_radiologist_has_ai_full() -> None:
    """Radiologists may ask study Q&A with AI; viewers and admins may not."""
    assert has_capability(Role.RADIOLOGIST, Capability.AI_FULL) is True
    assert has_capability(Role.VIEWER, Capability.AI_FULL) is False


def test_admin_has_no_ai_capabilities() -> None:
    """Admin holds zero AI capabilities (separation of duties → 403 PHI_ACCESS_FORBIDDEN)."""
    assert has_capability(Role.ADMIN, Capability.AI_DRAFT) is False
    assert has_capability(Role.ADMIN, Capability.AI_FULL) is False
    assert ROLE_CAPABILITIES[Role.ADMIN] & {Capability.AI_DRAFT, Capability.AI_FULL} == frozenset()
