"""Unit tests for the keep/redact/review decision layer.

Covers acceptance criteria:
* 3 — forced-modality modalities route every detected region to review.
* 4 — the clinical-annotation allowlist keeps LEFT/RIGHT/SUPINE/PRONE,
  measurements, and scale bars; a rules-only redactor destroys them.
* 5 — a classifier error or an OCR-text-with-no-classification routes to
  REVIEW, never KEEP (fail-closed).
* 10 — an unvalidated (modality, manufacturer) pair forces review.
"""

from __future__ import annotations

import pytest

from app.services.deid.decision import (
    CLINICAL_ANNOTATION_ALLOWLIST,
    Decider,
    Decision,
    DecisionContext,
    ReviewReason,
    is_clinical_annotation,
)
from app.services.deid.phi_ner import (
    DeterministicPhiClassifier,
    PhiClassification,
    PhiLabel,
    RulesOnlyRedactor,
)

# A standard, non-forced, validated context.
_CTX = DecisionContext(
    modality="CR",
    forced_review=False,
    unvalidated_source=False,
    confidence_threshold=0.9,
)


def _ctx(
    *,
    modality: str = "CR",
    forced: bool = False,
    unvalidated: bool = False,
    threshold: float = 0.9,
) -> DecisionContext:
    return DecisionContext(
        modality=modality,
        forced_review=forced,
        unvalidated_source=unvalidated,
        confidence_threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Clinical-annotation allowlist (criterion 4)
# ---------------------------------------------------------------------------
class TestClinicalAnnotationAllowlist:
    @pytest.mark.parametrize(
        "token", ["LEFT", "RIGHT", "SUPINE", "PRONE", "LATERAL", "DECUBITUS", "ERECT"]
    )
    def test_laterality_and_position_kept(self, token: str) -> None:
        assert is_clinical_annotation(token) is True

    @pytest.mark.parametrize(
        "text", ["15.2 cm", "3.5 mm", "120 ml", "10 kg", "5 deg", "scale", "10 mm"]
    )
    def test_measurements_and_scale_bars_kept(self, text: str) -> None:
        assert is_clinical_annotation(text) is True

    @pytest.mark.parametrize("text", ["John Doe", "MRN-12345", "01/02/1980", "ACC123"])
    def test_phi_text_not_clinical(self, text: str) -> None:
        assert is_clinical_annotation(text) is False

    def test_allowlist_surfaces_categories(self) -> None:
        assert "LEFT" in CLINICAL_ANNOTATION_ALLOWLIST
        assert "RIGHT" in CLINICAL_ANNOTATION_ALLOWLIST
        assert "SUPINE" in CLINICAL_ANNOTATION_ALLOWLIST
        assert "MEASUREMENT" in CLINICAL_ANNOTATION_ALLOWLIST
        assert "SCALE_BAR" in CLINICAL_ANNOTATION_ALLOWLIST


# ---------------------------------------------------------------------------
# Rules-only vs OpenMed/deterministic contrast (criterion 4)
# ---------------------------------------------------------------------------
class TestRulesOnlyVsClassifier:
    """A rules-only redactor destroys clinical annotations; the deterministic
    stand-in (which mirrors the OpenMed PHI-vs-clinical distinction) does not."""

    @pytest.mark.parametrize("annotation", ["LEFT", "RIGHT", "SUPINE", "PRONE", "15.2 cm"])
    def test_rules_only_destroys_clinical_annotations(self, annotation: str) -> None:
        rules = RulesOnlyRedactor()
        verdict = rules.classify(annotation)
        assert verdict.label == PhiLabel.PHI  # wrongly flagged as PHI → redacted

    @pytest.mark.parametrize("annotation", ["LEFT", "RIGHT", "SUPINE", "PRONE", "15.2 cm"])
    def test_deterministic_classifier_keeps_clinical_annotations(self, annotation: str) -> None:
        clf = DeterministicPhiClassifier()
        verdict = clf.classify(annotation)
        assert verdict.label == PhiLabel.CLINICAL

    @pytest.mark.parametrize("phi_text", ["John Doe", "MRN:12345", "1980-01-02", "+1 555 0100"])
    def test_both_flag_phi_as_phi(self, phi_text: str) -> None:
        rules = RulesOnlyRedactor()
        det = DeterministicPhiClassifier()
        assert rules.classify(phi_text).label == PhiLabel.PHI
        assert det.classify(phi_text).label == PhiLabel.PHI


# ---------------------------------------------------------------------------
# Decider — fail-closed policy (criteria 3, 5, 10)
# ---------------------------------------------------------------------------
class TestDecider:
    def test_high_confidence_phi_is_redacted(self) -> None:
        decider = Decider()
        cls = PhiClassification(PhiLabel.PHI, 0.95, "PATIENT")
        decision, reason = decider.decide(cls, _ctx())
        assert decision == Decision.REDACT
        assert reason == ""

    def test_high_confidence_clinical_is_kept(self) -> None:
        decider = Decider()
        cls = PhiClassification(PhiLabel.CLINICAL, 0.95, "LATERALITY")
        decision, _reason = decider.decide(cls, _ctx())
        assert decision == Decision.KEEP

    def test_low_confidence_routes_to_review(self) -> None:
        decider = Decider()
        cls = PhiClassification(PhiLabel.PHI, 0.5, "PATIENT")
        decision, reason = decider.decide(cls, _ctx(threshold=0.9))
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.LOW_CONFIDENCE.value

    def test_classifier_error_routes_to_review_never_keep(self) -> None:
        decider = Decider()
        cls = PhiClassification(PhiLabel.UNKNOWN, 0.0, "CLASSIFIER_ERROR")
        decision, reason = decider.decide(cls, _ctx())
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.CLASSIFIER_ERROR.value

    def test_no_classification_routes_to_review_never_keep(self) -> None:
        # OCR returned text but the classifier recognised no entity.
        decider = Decider()
        cls = PhiClassification(PhiLabel.UNKNOWN, 0.0, "NO_ENTITY")
        decision, reason = decider.decide(cls, _ctx())
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.NO_CLASSIFICATION.value

    def test_empty_text_routes_to_review(self) -> None:
        # ThresholdOcrEngine returns empty text → classifier → UNKNOWN/EMPTY.
        decider = Decider()
        cls = PhiClassification(PhiLabel.UNKNOWN, 0.0, "EMPTY")
        decision, reason = decider.decide(cls, _ctx())
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.NO_CLASSIFICATION.value

    def test_forced_modality_routes_every_region_to_review(self) -> None:
        # Criterion 3 — even a high-confidence clinical annotation is reviewed.
        decider = Decider()
        cls = PhiClassification(PhiLabel.CLINICAL, 0.99, "LATERALITY")
        decision, reason = decider.decide(cls, _ctx(forced=True))
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.FORCED_MODALITY.value

    def test_forced_modality_overrides_high_confidence_phi(self) -> None:
        decider = Decider()
        cls = PhiClassification(PhiLabel.PHI, 0.99, "PATIENT")
        decision, reason = decider.decide(cls, _ctx(forced=True))
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.FORCED_MODALITY.value

    def test_unvalidated_source_forces_review(self) -> None:
        # Criterion 10 — unvalidated (modality, manufacturer) → review.
        decider = Decider()
        cls = PhiClassification(PhiLabel.CLINICAL, 0.99, "LATERALITY")
        decision, reason = decider.decide(cls, _ctx(unvalidated=True))
        assert decision == Decision.REVIEW
        assert reason == ReviewReason.UNVALIDATED_SOURCE.value

    def test_forced_review_takes_precedence_over_unvalidated(self) -> None:
        decider = Decider()
        cls = PhiClassification(PhiLabel.PHI, 0.99, "PATIENT")
        decision, reason = decider.decide(cls, _ctx(forced=True, unvalidated=True))
        assert reason == ReviewReason.FORCED_MODALITY.value
