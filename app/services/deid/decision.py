"""Keep / redact / review policy — the fail-closed decision layer.

For each OCR text region the :class:`Decider` resolves one of three outcomes:

* **REDACT** — high-confidence PHI; box-fill the region in the pixel data.
* **KEEP** — high-confidence clinical annotation; leave the pixels untouched.
* **REVIEW** — anything uncertain.  The region is redacted (fail-closed: no PHI
  leaks) *and* routed to the human review queue so a radiologist can restore it
  if it was actually clinical.

The policy is fail-closed by construction:

* A classifier error, an empty/unmatched text region, or a low-confidence
  classification all resolve to **REVIEW**, never **KEEP**.
* Modalities in ``deid_modalities_forced_review`` and unvalidated
  ``(modality, manufacturer)`` sources force **REVIEW** on *every* detected
  region regardless of confidence.

The **clinical-annotation allowlist** (``CLINICAL_ANNOTATION_ALLOWLIST``) is the
set of burned-in annotations that carry clinical meaning and must survive
de-identification: laterality, positioning, measurement burn-ins, and scale
bars.  A rules-only redactor destroys them; the OpenMed path (which classifies
them as CLINICAL) does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.services.deid.phi_ner import PhiClassification, PhiLabel


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
class Decision(StrEnum):
    """Per-region outcome."""

    KEEP = "KEEP"
    REDACT = "REDACT"
    REVIEW = "REVIEW"


# ---------------------------------------------------------------------------
# Review reasons — recorded on every review item for triage
# ---------------------------------------------------------------------------
class ReviewReason(StrEnum):
    """Why a region was routed to human review."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    FORCED_MODALITY = "FORCED_MODALITY"
    UNVALIDATED_SOURCE = "UNVALIDATED_SOURCE"
    CLASSIFIER_ERROR = "CLASSIFIER_ERROR"
    NO_CLASSIFICATION = "NO_CLASSIFICATION"


# ---------------------------------------------------------------------------
# Clinical-annotation allowlist — burned-in text that MUST be kept
# ---------------------------------------------------------------------------
# Laterality and positioning are short, upper-case tokens burned into the image.
CLINICAL_LATERALITY: frozenset[str] = frozenset({"LEFT", "RIGHT"})
CLINICAL_POSITION: frozenset[str] = frozenset({"SUPINE", "PRONE", "LATERAL", "DECUBITUS", "ERECT"})

# Measurement burn-ins and scale bars are numeric-with-unit phrases.  These are
# matched by pattern (not an exhaustive token set) via :func:`is_clinical_annotation`.
_MEASUREMENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|g|ml|cc|hz|deg)\b", re.IGNORECASE)
_SCALE_RE = re.compile(r"\bscale\b|\b\d+\s*mm\b", re.IGNORECASE)

# The canonical allowlist surfaced to operators / the validation report.  The
# pattern-matched categories (measurements, scale bars) are listed by name.
CLINICAL_ANNOTATION_ALLOWLIST: frozenset[str] = frozenset(
    CLINICAL_LATERALITY | CLINICAL_POSITION | {"MEASUREMENT", "SCALE_BAR"}
)


def is_clinical_annotation(text: str) -> bool:
    """True if ``text`` is a clinical annotation that must survive de-identification.

    Matches laterality/positioning tokens exactly and measurement / scale-bar
    phrases by pattern.  Used by the deterministic classifier stand-in and
    asserted by the rules-only-vs-OpenMed contrast test.
    """
    upper = text.strip().upper()
    if upper in CLINICAL_LATERALITY or upper in CLINICAL_POSITION:
        return True
    return bool(_MEASUREMENT_RE.search(text) or _SCALE_RE.search(text))


# ---------------------------------------------------------------------------
# Decision context
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Per-run inputs that modulate the keep/redact/review policy."""

    modality: str
    forced_review: bool  # modality in deid_modalities_forced_review
    unvalidated_source: bool  # (modality, manufacturer) not in validated set
    confidence_threshold: float


# ---------------------------------------------------------------------------
# Decider
# ---------------------------------------------------------------------------
class Decider:
    """Resolve a per-region :class:`Decision` from a classifier verdict + context.

    The order of checks encodes the fail-closed contract: forced/unvalidated
    sources and any uncertainty win over a KEEP, so PHI never leaks.
    """

    def decide(
        self, classification: PhiClassification, ctx: DecisionContext
    ) -> tuple[Decision, str]:
        """Return ``(decision, reason)`` where ``reason`` is empty unless REVIEW."""
        # 1. Forced-modality / unvalidated source → review every region.
        if ctx.forced_review:
            return Decision.REVIEW, ReviewReason.FORCED_MODALITY.value
        if ctx.unvalidated_source:
            return Decision.REVIEW, ReviewReason.UNVALIDATED_SOURCE.value

        # 2. Classifier error or no classification → review, never keep.
        if classification.label == PhiLabel.UNKNOWN:
            reason = (
                ReviewReason.CLASSIFIER_ERROR.value
                if classification.entity_type == "CLASSIFIER_ERROR"
                else ReviewReason.NO_CLASSIFICATION.value
            )
            return Decision.REVIEW, reason

        # 3. Low confidence → review (fail-closed).
        if classification.confidence < ctx.confidence_threshold:
            return Decision.REVIEW, ReviewReason.LOW_CONFIDENCE.value

        # 4. High-confidence verdict.
        if classification.label == PhiLabel.PHI:
            return Decision.REDACT, ""
        return Decision.KEEP, ""
