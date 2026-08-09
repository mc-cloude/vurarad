"""Single RBAC authority — the frozen ROLE_CAPABILITIES mapping.

This is the ONE place that defines who can do what.  Every route reads its
required capability from here; there is no hardcoded role check anywhere else.
"""

from enum import StrEnum


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
class Capability(StrEnum):
    # -- clinical ------------------------------------------------------------
    STUDY_READ = "study:read"
    STUDY_WRITE = "study:write"
    STUDY_IMPORT = "study:import"
    STUDY_DELETE = "study:delete"
    STUDY_SEARCH = "study:search"
    STUDY_ANNOTATE = "study:annotate"

    REPORT_READ = "report:read"
    REPORT_WRITE = "report:write"
    REPORT_SIGN = "report:sign"
    REPORT_ADDENDUM = "report:addendum"
    REPORT_DELETE = "report:delete"

    IMAGING_ACCESS = "imaging:access"
    AI_DRAFT = "ai:draft"
    AI_FULL = "ai:full"

    # -- evidence (WP13) -----------------------------------------------------
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_ACCEPT = "evidence:accept"

    # -- administration ------------------------------------------------------
    AUDIT_READ = "audit:read"
    AUDIT_EXPORT = "audit:export"
    ANALYTICS_READ = "analytics:read"
    COMPLIANCE_PURGE = "compliance:purge"
    USER_MANAGE = "user:manage"
    BREAK_GLASS = "break_glass"

    # -- research ------------------------------------------------------------
    RESEARCH_COHORT_CREATE = "research:cohort:create"
    RESEARCH_FEATURES_READ = "research:features:read"
    RESEARCH_EXPORT = "research:export"


# ---------------------------------------------------------------------------
PHI_CAPABILITIES: frozenset[Capability] = frozenset(
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
        Capability.EVIDENCE_READ,
        Capability.EVIDENCE_ACCEPT,
        Capability.COMPLIANCE_PURGE,
        Capability.BREAK_GLASS,
        Capability.RESEARCH_COHORT_CREATE,
        Capability.RESEARCH_FEATURES_READ,
        Capability.RESEARCH_EXPORT,
    }
)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"
    RADIOLOGIST = "radiologist"


# ---------------------------------------------------------------------------
# The single mapping — frozen, structurally enforced
# ---------------------------------------------------------------------------
ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.ADMIN: frozenset(
        {
            Capability.AUDIT_READ,
            Capability.AUDIT_EXPORT,
            Capability.ANALYTICS_READ,
            Capability.USER_MANAGE,
        }
    ),
    Role.VIEWER: frozenset(
        {
            Capability.STUDY_READ,
            Capability.STUDY_SEARCH,
            Capability.REPORT_READ,
        }
    ),
    Role.RADIOLOGIST: frozenset(
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
            Capability.EVIDENCE_READ,
            Capability.EVIDENCE_ACCEPT,
            Capability.BREAK_GLASS,
            Capability.RESEARCH_COHORT_CREATE,
            Capability.RESEARCH_FEATURES_READ,
            Capability.RESEARCH_EXPORT,
        }
    ),
}

# ---------------------------------------------------------------------------
# Structural invariants — always true, enforced by tests
# ---------------------------------------------------------------------------

# B2: admin has zero PHI capabilities  (separation of duties)
assert ROLE_CAPABILITIES[Role.ADMIN] & PHI_CAPABILITIES == frozenset(), (
    "admin must have zero PHI capabilities — separation of duties is structural"
)

# Admin must not be able to read reports
assert Capability.REPORT_READ not in ROLE_CAPABILITIES[Role.ADMIN]

# Admin must not be able to read studies
assert Capability.STUDY_READ not in ROLE_CAPABILITIES[Role.ADMIN]

# Viewer must NOT be able to write or sign
assert Capability.REPORT_WRITE not in ROLE_CAPABILITIES[Role.VIEWER]
assert Capability.REPORT_SIGN not in ROLE_CAPABILITIES[Role.VIEWER]


def get_role_capabilities(role: Role) -> frozenset[Capability]:
    return ROLE_CAPABILITIES[role]


def has_capability(role: Role, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def is_phi_capability(capability: Capability) -> bool:
    return capability in PHI_CAPABILITIES
