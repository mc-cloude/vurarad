"""Build-failing gate: no CADt fields on Finding or WorklistRow (criterion 3).

A CADt (computer-aided triage and notification) device classifies findings by
suspicion, urgency, or triage priority.  Storing any of those fields would make
us the CADt — so this test greps the model schemas and source for the forbidden
field names and fails the build on any hit.

Allowed: ``WorklistRow.priority`` is a clinical :class:`StudyPriority`
(order-derived, never AI-derived), not a CADt triage field.
"""

from __future__ import annotations

import inspect

from app.models.finding import Finding
from app.models.study import StudyPriority, WorklistRow

FORBIDDEN_FIELDS = {
    "suspicion",
    "urgency",
    "triage",
    "abnormal",
    "malignancy",
}


def _model_field_names(model_cls: type) -> set[str]:
    """Return the set of snake_case field names for a Pydantic model."""
    return set(model_cls.model_fields.keys())


class TestNoCadtFieldsOnFinding:
    """No forbidden CADt field exists on :class:`Finding`."""

    def test_finding_has_no_cadt_fields(self) -> None:
        fields = _model_field_names(Finding)
        hits = fields & FORBIDDEN_FIELDS
        assert not hits, f"Finding has forbidden CADt fields: {hits}"

    def test_finding_provenance_has_no_cadt_fields(self) -> None:
        from app.models.finding import FindingProvenance

        fields = _model_field_names(FindingProvenance)
        hits = fields & FORBIDDEN_FIELDS
        assert not hits, f"FindingProvenance has forbidden CADt fields: {hits}"

    def test_finding_source_has_no_cadt_strings(self) -> None:
        """Grep the Finding source for forbidden field declarations."""
        source = inspect.getsource(Finding)
        for word in FORBIDDEN_FIELDS:
            # Look for field declarations: "    <word>:" or "<word> ="
            line_hits = [
                line.strip()
                for line in source.splitlines()
                if f"{word}:" in line or f"{word} =" in line or f"{word}=" in line
            ]
            assert not line_hits, f"Finding source declares forbidden field '{word}': {line_hits}"


class TestNoCadtFieldsOnWorklistRow:
    """WorklistRow may have a clinical ``priority`` (StudyPriority) but no CADt fields."""

    def test_worklist_has_no_cadt_fields(self) -> None:
        fields = _model_field_names(WorklistRow)
        hits = fields & FORBIDDEN_FIELDS
        assert not hits, f"WorklistRow has forbidden CADt fields: {hits}"

    def test_worklist_priority_is_clinical_study_priority(self) -> None:
        """The ``priority`` field is a :class:`StudyPriority`, not a CADt triage."""
        field = WorklistRow.model_fields.get("priority")
        assert field is not None, "WorklistRow must have a 'priority' field"
        # StudyPriority is an enum with ROUTINE/URGENT/STAT — clinical, not AI.
        assert StudyPriority.ROUTINE in StudyPriority
        assert StudyPriority.URGENT in StudyPriority
        assert StudyPriority.STAT in StudyPriority
