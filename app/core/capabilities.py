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

    # -- cohort workbench (WP17) — de-identified, NOT PHI -------------------
    COHORT_CREATE = "cohort:create"
    COHORT_READ = "cohort:read"
    COHORT_WRITE = "cohort:write"
    COHORT_SUBJECT_ADD = "cohort:subject:add"
    COHORT_SEGMENTATION = "cohort:segmentation"

    # -- compliance ----------------------------------------------------------
    # ``patient:erase`` is the ONLY capability that may read the write-restricted
    # ``deid_links`` collection (the pseudonym->patientKey map).  It re-identifies
    # a subject, so it is PHI-level and is deliberately withheld from the
    # ``researcher`` role (cohort:* grants no access to it).
    PATIENT_ERASE = "patient:erase"


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
        Capability.COMPLIANCE_PURGE,
        Capability.BREAK_GLASS,
        Capability.RESEARCH_COHORT_CREATE,
        Capability.RESEARCH_FEATURES_READ,
        Capability.RESEARCH_EXPORT,
        Capability.PATIENT_ERASE,
    }
)


# ---------------------------------------------------------------------------
# Clinical capabilities — the set the ``researcher`` role must NOT hold.
# ``cohort:*`` capabilities are deliberately absent (they are research, not
# clinical) so the research/clinical barrier is expressible as a set
# intersection.
# ---------------------------------------------------------------------------
CLINICAL_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.STUDY_READ,
        Capability.STUDY_WRITE,
        Capability.STUDY_IMPORT,
        Capability.STUDY_DELETE,
        Capability.STUDY_SEARCH,
        Capability.STUDY_ANNOTATE,
        Capability.REPORT_READ,
        Capability.REPORT_WRITE,
        Capability.REPORT_SIGN,
        Capability.REPORT_ADDENDUM,
        Capability.REPORT_DELETE,
        Capability.IMAGING_ACCESS,
        Capability.AI_DRAFT,
        Capability.AI_FULL,
        Capability.BREAK_GLASS,
    }
)


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"
    RADIOLOGIST = "radiologist"
    RESEARCHER = "researcher"


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
            Capability.BREAK_GLASS,
            Capability.RESEARCH_COHORT_CREATE,
            Capability.RESEARCH_FEATURES_READ,
            Capability.RESEARCH_EXPORT,
        }
    ),
    Role.RESEARCHER: frozenset(
        {
            Capability.COHORT_CREATE,
            Capability.COHORT_READ,
            Capability.COHORT_WRITE,
            Capability.COHORT_SUBJECT_ADD,
            Capability.COHORT_SEGMENTATION,
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

# WP17 — research/clinical barrier (structural):
# The researcher role holds zero clinical capabilities — cohort work is
# firewalled from the clinical reading path.  ``cohort:*`` grants no access to
# the write-restricted ``deid_links`` collection (only ``patient:erase`` may).
assert ROLE_CAPABILITIES[Role.RESEARCHER] & CLINICAL_CAPABILITIES == frozenset(), (
    "researcher must have zero clinical capabilities — the barrier is structural"
)
assert Capability.PATIENT_ERASE not in ROLE_CAPABILITIES[Role.RESEARCHER], (
    "researcher must not hold patient:erase — cohort:* grants no deid_links access"
)


def get_role_capabilities(role: Role) -> frozenset[Capability]:
    return ROLE_CAPABILITIES[role]


def has_capability(role: Role, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def is_phi_capability(capability: Capability) -> bool:
    return capability in PHI_CAPABILITIES
