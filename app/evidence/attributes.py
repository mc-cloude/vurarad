"""Strict per-findingType attribute models for evidence evaluation (WP13 §3.16).

Each finding type has exactly one :class:`CamelModel` that validates the
attribute payload a reader submits with an evidence lookup.  The models are
intentionally strict (``extra="forbid"`` from :class:`CamelModel`, constrained
literals and ranges) so a malformed or partial payload is rejected before the
pure rule engine ever sees it — the engine never has to guess at missing or
ill-typed attributes.

``validate_attributes`` is the single entry point: given a ``findingType`` and a
raw (camelCase) payload, it returns a snake_case ``dict`` ready for
:meth:`app.evidence.engine.RuleEngine.evaluate`, or raises ``ValidationError``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError

from app.models.common import CamelModel


# ---------------------------------------------------------------------------
# Per-findingType attribute models
# ---------------------------------------------------------------------------
class PulmonaryNoduleAttributes(CamelModel):
    """Attributes for the Fleischner 2017 incidental pulmonary nodule algorithm."""

    finding_type: Literal["pulmonary_nodule"] = "pulmonary_nodule"
    nodule_type: Literal["solid", "part_solid", "ground_glass"]
    diameter_mm: float = Field(ge=0, description="Average diameter in millimetres")
    patient_risk: Literal["low", "high"]
    nodule_count: Literal["single", "multiple"] = "single"


class LiverObservationAttributes(CamelModel):
    """Attributes for the LI-RADS 2018 liver observation categories."""

    finding_type: Literal["liver_observation"] = "liver_observation"
    category: Literal[
        "LR-1",
        "LR-2",
        "LR-3",
        "LR-4",
        "LR-5",
        "LR-M",
        "LR-NC",
        "LR-TIV",
    ]
    size_mm: float = Field(default=0, ge=0, description="Long-axis dimension in millimetres")
    cirrhosis: bool = False


class BreastAssessmentAttributes(CamelModel):
    """Attributes for the BI-RADS 5th edition assessment-to-management mapping."""

    finding_type: Literal["breast_assessment"] = "breast_assessment"
    category: Literal["0", "1", "2", "3", "4A", "4B", "4C", "5", "6"]


class AdrenalIncidentalAttributes(CamelModel):
    """Attributes for the ACR incidental adrenal nodule management guidance."""

    finding_type: Literal["adrenal_incidental"] = "adrenal_incidental"
    diameter_mm: float = Field(ge=0, description="Max diameter in millimetres")
    unenhanced_hu: float | None = Field(
        default=None, description="Unenhanced CT attenuation in Hounsfield units"
    )
    has_heterogeneous: bool = False
    has_historical_growth: bool = False


# ---------------------------------------------------------------------------
# Registry — findingType → attribute model
# ---------------------------------------------------------------------------
FINDING_TYPE_ATTRIBUTES: dict[str, type[CamelModel]] = {
    "pulmonary_nodule": PulmonaryNoduleAttributes,
    "liver_observation": LiverObservationAttributes,
    "breast_assessment": BreastAssessmentAttributes,
    "adrenal_incidental": AdrenalIncidentalAttributes,
}


def attribute_names(finding_type: str) -> set[str]:
    """Return the snake_case field names defined for ``finding_type``."""
    model = FINDING_TYPE_ATTRIBUTES.get(finding_type)
    if model is None:
        return set()
    return set(model.model_fields.keys())


def validate_attributes(finding_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a raw camelCase payload against the model for ``finding_type``.

    Returns a snake_case ``dict`` (via ``model_dump()``) suitable for the rule
    engine.  Raises :class:`ValidationError` if ``finding_type`` is unknown or
    the payload does not satisfy the model.
    """
    model = FINDING_TYPE_ATTRIBUTES.get(finding_type)
    if model is None:
        raise ValueError(f"unknown findingType: {finding_type!r}")
    instance = model.model_validate(raw)
    return instance.model_dump()


__all__ = [
    "AdrenalIncidentalAttributes",
    "BreastAssessmentAttributes",
    "FINDING_TYPE_ATTRIBUTES",
    "LiverObservationAttributes",
    "PulmonaryNoduleAttributes",
    "attribute_names",
    "validate_attributes",
    "ValidationError",
]
