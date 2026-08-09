"""Report template catalogue — static, versioned, PHI-free (WP9).

Templates are standalone RSNA-style report structures keyed by modality and
body part.  They carry NO patient data and have NO dependency on the report
model — they feed WP6's AI drafting as static scaffolding (criterion 5).

Each template is version-stamped (catalogue ``schemaVersion`` + per-template
``version``) so an AI-drafted report generated against template vN can be
re-generated against the same template version even after the catalogue
evolves.  The catalogue is deterministic and read-only: the same template id
returns byte-identical content for every caller, so the "identical response
across permitted roles" guarantee (criterion 3) is structural — there is no
per-role data variation to test away.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.errors import TemplateNotFoundError
from app.models.common import CamelModel

# Catalogue wire-shape version — bump when TemplateSummary/ReportTemplate
# change.  Independent of individual template ``version``.
SCHEMA_VERSION: int = 1

_TEMPLATES_DIR: Path = Path(__file__).resolve().parent.parent / "prompts" / "templates"


# ---------------------------------------------------------------------------
# Wire models — standalone (no report-model dependency)
# ---------------------------------------------------------------------------
class TemplateSummary(CamelModel):
    """One catalogue entry — metadata only, no template body."""

    template_id: str
    modality: str
    body_part: str
    title: str
    version: int
    schema_version: int


class ReportTemplate(CamelModel):
    """A single report template — metadata plus the markdown body."""

    template_id: str
    modality: str
    body_part: str
    title: str
    version: int
    schema_version: int
    content: str


class TemplateCatalogue(CamelModel):
    """The ``GET /report-templates`` response — PHI-free, version-stamped."""

    schema_version: int
    templates: list[TemplateSummary]


# ---------------------------------------------------------------------------
# Static catalogue definition — the source of truth for available templates.
# Each entry points at a markdown file under app/prompts/templates/.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _TemplateDef:
    template_id: str
    modality: str
    body_part: str
    title: str
    version: int
    file: str


_TEMPLATE_DEFS: tuple[_TemplateDef, ...] = (
    _TemplateDef(
        template_id="chest_ct",
        modality="CT",
        body_part="CHEST",
        title="RSNA Chest CT Report Template",
        version=1,
        file="chest_ct.md",
    ),
    _TemplateDef(
        template_id="abdomen_ct",
        modality="CT",
        body_part="ABDOMEN",
        title="RSNA Abdomen CT Report Template",
        version=1,
        file="abdomen_ct.md",
    ),
)


def _summary(defn: _TemplateDef) -> TemplateSummary:
    return TemplateSummary(
        template_id=defn.template_id,
        modality=defn.modality,
        body_part=defn.body_part,
        title=defn.title,
        version=defn.version,
        schema_version=SCHEMA_VERSION,
    )


class TemplateService:
    """Serve the static, versioned report-template catalogue."""

    def __init__(self, templates_dir: Path | None = None) -> None:
        self._dir = templates_dir if templates_dir is not None else _TEMPLATES_DIR
        self._by_id: dict[str, _TemplateDef] = {d.template_id: d for d in _TEMPLATE_DEFS}
        self._content_cache: dict[str, str] = {}

    async def list_templates(
        self,
        *,
        modality: str | None = None,
        body_part: str | None = None,
    ) -> TemplateCatalogue:
        """Return the catalogue, optionally filtered by modality/bodyPart."""
        summaries: list[TemplateSummary] = []
        for defn in _TEMPLATE_DEFS:
            if modality is not None and defn.modality.upper() != modality.upper():
                continue
            if body_part is not None and defn.body_part.upper() != body_part.upper():
                continue
            summaries.append(_summary(defn))
        return TemplateCatalogue(schema_version=SCHEMA_VERSION, templates=summaries)

    async def get_template(self, template_id: str) -> ReportTemplate:
        """Return a single template by id — 404 TEMPLATE_NOT_FOUND if absent."""
        defn = self._by_id.get(template_id)
        if defn is None:
            raise TemplateNotFoundError(f"Report template '{template_id}' not found")
        return ReportTemplate(
            template_id=defn.template_id,
            modality=defn.modality,
            body_part=defn.body_part,
            title=defn.title,
            version=defn.version,
            schema_version=SCHEMA_VERSION,
            content=self._load_content(defn.file),
        )

    def _load_content(self, filename: str) -> str:
        cached = self._content_cache.get(filename)
        if cached is not None:
            return cached
        content = (self._dir / filename).read_text(encoding="utf-8")
        self._content_cache[filename] = content
        return content


__all__ = [
    "SCHEMA_VERSION",
    "ReportTemplate",
    "TemplateCatalogue",
    "TemplateService",
    "TemplateSummary",
]
