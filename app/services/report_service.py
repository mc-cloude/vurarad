"""Clinical report service — the research/clinical barrier (WP17, criterion 1).

``ReportService`` is the clinical report assembly path.  It enforces a one-way
barrier: any identifier bearing a ``RESEARCH_ID_PREFIX`` (a cohort pseudonym
``cs_…``, a feature id ``fr_…``, a segmentation id ``sg_…``, …) is rejected with
``409 RESEARCH_OUTPUT_NOT_PERMITTED``.  Research output can never reach a signed
clinical report.

**Import isolation** — this module is structurally firewalled from the research
workbench: it imports nothing under ``app.repositories.cohort_repo`` (nor
``app.models.cohort`` / ``app.repositories.deid_link_repo``).  A static
import-graph test (``test_research_clinical_barrier.py``) asserts the closure of
this module's imports never reaches ``app.repositories.cohort_repo``.
"""

from __future__ import annotations

from app.core.errors import ResearchOutputNotPermittedError
from app.repositories.base import DocumentStore

# ---------------------------------------------------------------------------
# The frozen set of research identifier prefixes (§D9 / WP17 criterion 1).
# Every research-derived id is minted with one of these prefixes:
#   co_ cohort        cs_ cohort subject     sg_ segmentation
#   fr_ feature       ls_ label              an_ analysis
#   ex_ export        rd_ research draft
# ---------------------------------------------------------------------------
RESEARCH_ID_PREFIXES: frozenset[str] = frozenset(
    {"co_", "cs_", "sg_", "fr_", "ls_", "an_", "ex_", "rd_"}
)


def _bears_research_prefix(value: str) -> bool:
    """True if ``value`` (or any of its path components) starts with a research prefix."""
    if not value:
        return False
    components = value.replace("\\", "/").split("/")
    return any(
        component.startswith(prefix)
        for component in components
        for prefix in RESEARCH_ID_PREFIXES
    )


def _assert_no_research_output(*identifiers: str) -> None:
    """Raise ``ResearchOutputNotPermittedError`` if any identifier is research-derived."""
    for identifier in identifiers:
        if _bears_research_prefix(identifier):
            raise ResearchOutputNotPermittedError(
                f"Identifier '{identifier}' bears a research prefix and is not "
                f"permitted in the clinical report path"
            )


class ReportService:
    """Clinical report assembly — the barrier against research output.

    The constructor takes a :class:`DocumentStore` for report persistence; it
    deliberately has no dependency on any cohort/research repository.
    """

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def attach(self, report_id: str, attachment_ref: str) -> bool:
        """Attach a clinical artifact to a report.

        Rejects any research-derived ``attachment_ref`` (or ``report_id``) with
        ``409 RESEARCH_OUTPUT_NOT_PERMITTED``.
        """
        _assert_no_research_output(report_id, attachment_ref)
        await self._store.set(
            "report_attachments",
            f"{report_id}/{attachment_ref}",
            {"reportId": report_id, "attachmentRef": attachment_ref},
        )
        return True

    async def set_section(
        self,
        report_id: str,
        section_id: str,
        source_ref: str,
    ) -> bool:
        """Set a report section sourced from a clinical artifact.

        Rejects any research-derived identifier with
        ``409 RESEARCH_OUTPUT_NOT_PERMITTED``.
        """
        _assert_no_research_output(report_id, section_id, source_ref)
        await self._store.set(
            "report_sections",
            f"{report_id}/{section_id}",
            {"reportId": report_id, "sectionId": section_id, "sourceRef": source_ref},
        )
        return True


__all__ = ["RESEARCH_ID_PREFIXES", "ReportService"]
