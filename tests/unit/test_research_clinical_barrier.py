# ruff: noqa: B008
"""The research/clinical barrier (WP17 — acceptance criterion 1, 2, 4, 8, 9).

This is the single most important test in WP17.  It asserts the structural
separation between the de-identified research workbench and the clinical side:

1. ``RESEARCH_ID_PREFIXES`` are rejected by ``ReportService.attach()`` and
   ``set_section()`` with ``409 RESEARCH_OUTPUT_NOT_PERMITTED`` — a research
   pseudonym can never be attached to a signed clinical report.
2. No cohort response model (``Cohort``, ``CohortSubject``,
   ``CohortSegmentation``, ``SegmentationVersion``) carries a ``studyId`` or
   ``patientKey`` field — neither snake_case nor the camelCase wire alias.
3. An import-graph test asserts ``app/services/report_service.py`` has no
   static import path to ``app/repositories/cohort_repo.py`` — the clinical
   report path can never reach the cohort collection.
4. ``deid_links`` reads require ``patient:erase``; ``cohort:*`` grants no
   access (the capability matrix and the repository enforce this).
5. The ``researcher`` role holds zero clinical capabilities.
6. The radiogenomics code is gone: ``grep -rn "EGFR\\|KRAS" app/`` returns
   nothing.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from app.core.capabilities import (
    CLINICAL_CAPABILITIES,
    PHI_CAPABILITIES,
    ROLE_CAPABILITIES,
    Capability,
    Role,
)
from app.core.errors import (
    PermissionDeniedError,
    ResearchOutputNotPermittedError,
)
from app.models.cohort import Cohort, CohortSegmentation, CohortSubject, SegmentationVersion
from app.repositories.base import InMemoryDocumentStore
from app.repositories.deid_link_repo import DEID_LINKS_COLLECTION, DeidLinkRepository
from app.services.report_service import RESEARCH_ID_PREFIXES, ReportService

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. RESEARCH_ID_PREFIXES — the frozen prefix set
# ---------------------------------------------------------------------------
def test_research_id_prefixes_are_the_expected_frozen_set() -> None:
    """The eight research identifier prefixes are frozen and complete."""
    assert (
        frozenset({"co_", "cs_", "sg_", "fr_", "ls_", "an_", "ex_", "rd_"}) == RESEARCH_ID_PREFIXES
    )


# ---------------------------------------------------------------------------
# 2. ReportService rejects research identifiers with 409
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("prefix", sorted(RESEARCH_ID_PREFIXES))
async def test_report_attach_rejects_research_identifiers(prefix: str) -> None:
    """``attach()`` rejects any identifier bearing a RESEARCH_ID_PREFIX."""
    service = ReportService(InMemoryDocumentStore())
    with pytest.raises(ResearchOutputNotPermittedError) as exc_info:
        await service.attach(report_id="rep_123", attachment_ref=f"{prefix}abc123")
    assert exc_info.value.status_code == 409
    assert exc_info.value.code.value == "RESEARCH_OUTPUT_NOT_PERMITTED"


@pytest.mark.parametrize("prefix", sorted(RESEARCH_ID_PREFIXES))
async def test_report_set_section_rejects_research_source(prefix: str) -> None:
    """``set_section()`` rejects a research-derived source reference."""
    service = ReportService(InMemoryDocumentStore())
    with pytest.raises(ResearchOutputNotPermittedError):
        await service.set_section(
            report_id="rep_123", section_id="findings", source_ref=f"{prefix}xyz"
        )


async def test_report_attach_accepts_clinical_identifiers() -> None:
    """A clinical (non-prefixed) identifier is accepted — the barrier is one-way."""
    service = ReportService(InMemoryDocumentStore())
    # No exception raised for a clinical attachment reference.
    result = await service.attach(
        report_id="rep_123", attachment_ref="study/st_test/series/se_0000"
    )
    assert result is True


async def test_report_set_section_accepts_clinical_source() -> None:
    service = ReportService(InMemoryDocumentStore())
    result = await service.set_section(
        report_id="rep_123", section_id="findings", source_ref="finding/fd_1"
    )
    assert result is True


# ---------------------------------------------------------------------------
# 3. No cohort response model carries studyId / patientKey
# ---------------------------------------------------------------------------
_COHORT_RESPONSE_MODELS = [Cohort, CohortSubject, CohortSegmentation, SegmentationVersion]


@pytest.mark.parametrize("model_cls", _COHORT_RESPONSE_MODELS)
def test_no_cohort_model_has_study_id_field(model_cls: type) -> None:
    """No cohort response model declares ``study_id`` (snake) or ``studyId``."""
    field_names = set(model_cls.model_fields.keys())
    assert "study_id" not in field_names, f"{model_cls.__name__} declares study_id"
    aliases = {info.alias for info in model_cls.model_fields.values() if info.alias is not None}
    assert "studyId" not in aliases, f"{model_cls.__name__} aliases studyId"


@pytest.mark.parametrize("model_cls", _COHORT_RESPONSE_MODELS)
def test_no_cohort_model_has_patient_key_field(model_cls: type) -> None:
    """No cohort response model declares ``patient_key`` or ``patientKey``."""
    field_names = set(model_cls.model_fields.keys())
    assert "patient_key" not in field_names, f"{model_cls.__name__} declares patient_key"
    aliases = {info.alias for info in model_cls.model_fields.values() if info.alias is not None}
    assert "patientKey" not in aliases, f"{model_cls.__name__} aliases patientKey"


def test_cohort_model_serialisation_omits_study_and_patient_keys() -> None:
    """Serialising a Cohort never produces studyId/patientKey keys."""
    cohort = Cohort(
        cohort_id="co_test",
        name="NSC-Lung",
        irb_reference="IRB-2026-0142",
        irb_determination="EXEMPT",
        created_by="op_1",
        created_at="2026-08-09T00:00:00Z",
        updated_at="2026-08-09T00:00:00Z",
    )
    dumped = cohort.model_dump(by_alias=True)
    assert "studyId" not in dumped
    assert "patientKey" not in dumped
    assert "study_id" not in dumped
    assert "patient_key" not in dumped


# ---------------------------------------------------------------------------
# 4. Import-graph: report_service.py has no path to cohort_repo.py
# ---------------------------------------------------------------------------
def _app_module_path(module_name: str) -> Path | None:
    """Resolve an ``app.*`` module name to its source file, or None."""
    if not module_name.startswith("app."):
        return None
    parts = module_name.split(".")
    base = REPO_ROOT.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    # Package __init__
    init = base / "__init__.py"
    if init.is_file():
        return init
    return None


def _imported_app_modules(file_path: Path, current_module: str) -> set[str]:
    """Return the set of ``app.*`` module names imported by ``file_path``."""
    try:
        tree = ast.parse(file_path.read_text())
    except SyntaxError:
        return set()

    package_parts = current_module.split(".")[:-1]
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("app."):
                    base_module: str = node.module
                    # Names may be submodules of base_module.
                    for alias in node.names:
                        found.add(f"{base_module}.{alias.name}")
                    found.add(base_module)
            else:
                # Relative import — resolve against the current package.
                if not package_parts:
                    continue
                drop = node.level - 1
                if drop > len(package_parts):
                    continue
                base_parts = package_parts[: len(package_parts) - drop]
                base = ".".join(base_parts)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
                    found.add(base)
                    for alias in node.names:
                        found.add(f"{base}.{alias.name}")
                else:
                    for alias in node.names:
                        found.add(f"{base}.{alias.name}" if base else alias.name)

    return {m for m in found if m.startswith("app.")}


def _import_closure(start_module: str) -> set[str]:
    """Compute the static transitive import closure of ``start_module``."""
    closure: set[str] = set()
    stack: list[str] = [start_module]
    while stack:
        module_name = stack.pop()
        if module_name in closure:
            continue
        closure.add(module_name)
        path = _app_module_path(module_name)
        if path is None:
            continue
        for imported in _imported_app_modules(path, module_name):
            if imported not in closure:
                stack.append(imported)
    return closure


def test_report_service_has_no_import_path_to_cohort_repo() -> None:
    """``app.services.report_service`` must not transitively import
    ``app.repositories.cohort_repo`` — the clinical report path can never reach
    the cohort collection.
    """
    closure = _import_closure("app.services.report_service")
    assert "app.repositories.cohort_repo" not in closure, (
        f"report_service reaches cohort_repo; closure={sorted(closure)}"
    )
    # And it must not reach the de-id link repository either.
    assert "app.repositories.deid_link_repo" not in closure


def test_report_service_does_not_import_cohort_models() -> None:
    """The clinical report path must not import the cohort model module."""
    closure = _import_closure("app.services.report_service")
    assert "app.models.cohort" not in closure


# ---------------------------------------------------------------------------
# 5. deid_links reads require patient:erase; cohort:* grants no access
# ---------------------------------------------------------------------------
async def _seed_deid_link(store: InMemoryDocumentStore) -> None:
    await store.set(
        DEID_LINKS_COLLECTION,
        "cs_subject1",
        {"pseudonym": "cs_subject1", "patientKey": "pk_test"},
    )


async def test_deid_link_read_requires_patient_erase() -> None:
    """A caller holding only cohort:* capabilities cannot read a deid_link."""
    store = InMemoryDocumentStore()
    await _seed_deid_link(store)
    repo = DeidLinkRepository(store)

    cohort_only: frozenset[Capability] = frozenset(
        {
            Capability.COHORT_CREATE,
            Capability.COHORT_READ,
            Capability.COHORT_WRITE,
            Capability.COHORT_SUBJECT_ADD,
            Capability.COHORT_SEGMENTATION,
        }
    )
    with pytest.raises(PermissionDeniedError):
        await repo.get_link("cs_subject1", capabilities=cohort_only)


async def test_deid_link_read_succeeds_with_patient_erase() -> None:
    """A caller holding patient:erase can read a deid_link."""
    store = InMemoryDocumentStore()
    await _seed_deid_link(store)
    repo = DeidLinkRepository(store)

    link = await repo.get_link("cs_subject1", capabilities=frozenset({Capability.PATIENT_ERASE}))
    assert link is not None
    assert link["patientKey"] == "pk_test"


def test_cohort_capabilities_do_not_grant_patient_erase() -> None:
    """The researcher role (all cohort:*) does NOT hold patient:erase."""
    researcher_caps = ROLE_CAPABILITIES[Role.RESEARCHER]
    assert Capability.PATIENT_ERASE not in researcher_caps
    # Every cohort:* capability is present, but patient:erase is not.
    assert {
        Capability.COHORT_CREATE,
        Capability.COHORT_READ,
        Capability.COHORT_WRITE,
        Capability.COHORT_SUBJECT_ADD,
        Capability.COHORT_SEGMENTATION,
    } <= researcher_caps


# ---------------------------------------------------------------------------
# 6. researcher role holds zero clinical capabilities
# ---------------------------------------------------------------------------
def test_researcher_has_zero_clinical_capabilities() -> None:
    """researcher ∩ CLINICAL == ∅ — the barrier is structural."""
    assert ROLE_CAPABILITIES[Role.RESEARCHER] & CLINICAL_CAPABILITIES == frozenset()


def test_researcher_is_not_granted_phi_via_cohort_capabilities() -> None:
    """cohort:* capabilities are NOT PHI; patient:erase IS PHI and is withheld."""
    for cap in (
        Capability.COHORT_CREATE,
        Capability.COHORT_READ,
        Capability.COHORT_WRITE,
        Capability.COHORT_SUBJECT_ADD,
        Capability.COHORT_SEGMENTATION,
    ):
        assert cap not in PHI_CAPABILITIES
    assert Capability.PATIENT_ERASE in PHI_CAPABILITIES


# ---------------------------------------------------------------------------
# 7. Radiogenomics deleted — grep app/ for EGFR|KRAS returns nothing
# ---------------------------------------------------------------------------
def test_no_radiogenomics_markers_in_app() -> None:
    """``grep -rn "EGFR\\|KRAS" app/`` returns nothing (criterion 9)."""
    result = subprocess.run(
        ["grep", "-rn", "EGFR\\|KRAS", "app/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # grep exits 1 when nothing matched — that is the desired outcome.
    assert result.returncode != 0, f"EGFR/KRAS markers still present in app/:\n{result.stdout}"
    assert result.stdout == ""
