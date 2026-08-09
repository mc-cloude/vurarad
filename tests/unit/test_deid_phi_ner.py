"""Unit tests for the PHI NER classifier + rules-only contrast."""

from __future__ import annotations

import pytest

from app.services.deid.phi_ner import (
    OPENMED_LANGUAGES,
    OPENMED_MODEL_ID,
    OPENMED_MODEL_REVISION,
    DeterministicPhiClassifier,
    OpenMedPhiClassifier,
    PhiClassification,
    PhiLabel,
    RulesOnlyRedactor,
)


# ---------------------------------------------------------------------------
# Pinned model identity
# ---------------------------------------------------------------------------
def test_openmed_model_id_is_pinned() -> None:
    assert OPENMED_MODEL_ID == "openmed/phl-ner-multilingual"


def test_openmed_revision_is_pinned() -> None:
    # A 40-char hex commit hash.
    assert len(OPENMED_MODEL_REVISION) == 40
    assert all(c in "0123456789abcdef" for c in OPENMED_MODEL_REVISION)


def test_openmed_covers_exactly_16_languages() -> None:
    assert len(OPENMED_LANGUAGES) == 16
    # Africa-first languages are present.
    for lang in ("sw", "am", "yo", "ha"):
        assert lang in OPENMED_LANGUAGES


# ---------------------------------------------------------------------------
# DeterministicPhiClassifier — the CPU stand-in
# ---------------------------------------------------------------------------
class TestDeterministicClassifier:
    def test_dates_are_phi(self) -> None:
        clf = DeterministicPhiClassifier()
        for text in ("1980-01-02", "01/02/1980", "1-2-80"):
            assert clf.classify(text).label == PhiLabel.PHI

    def test_id_tokens_are_phi(self) -> None:
        clf = DeterministicPhiClassifier()
        for text in ("MRN:12345", "acc ACC987", "patient id P-001", "case 42"):
            assert clf.classify(text).label == PhiLabel.PHI

    def test_phone_numbers_are_phi(self) -> None:
        clf = DeterministicPhiClassifier()
        assert clf.classify("+1 555 010 1234").label == PhiLabel.PHI

    def test_names_are_phi(self) -> None:
        clf = DeterministicPhiClassifier()
        assert clf.classify("John Doe").label == PhiLabel.PHI

    def test_clinical_annotations_are_clinical(self) -> None:
        clf = DeterministicPhiClassifier()
        for text in ("LEFT", "RIGHT", "SUPINE", "PRONE", "15.2 cm"):
            assert clf.classify(text).label == PhiLabel.CLINICAL

    def test_empty_text_is_unknown(self) -> None:
        clf = DeterministicPhiClassifier()
        verdict = clf.classify("")
        assert verdict.label == PhiLabel.UNKNOWN
        assert verdict.entity_type == "EMPTY"

    def test_unmatched_text_is_unknown(self) -> None:
        clf = DeterministicPhiClassifier()
        verdict = clf.classify("zzz qqq")
        assert verdict.label == PhiLabel.UNKNOWN
        assert verdict.entity_type == "UNMATCHED"

    def test_clinical_takes_precedence_over_phi_patterns(self) -> None:
        # A measurement like "12 cm" matches the measurement regex but not a
        # PHI pattern; it must be CLINICAL, not UNKNOWN.
        clf = DeterministicPhiClassifier()
        assert clf.classify("12 cm").label == PhiLabel.CLINICAL

    def test_model_id_marks_it_as_stand_in(self) -> None:
        clf = DeterministicPhiClassifier()
        assert "deterministic" in clf.model_id
        assert clf.model_revision == OPENMED_MODEL_REVISION


# ---------------------------------------------------------------------------
# RulesOnlyRedactor — the anti-pattern
# ---------------------------------------------------------------------------
class TestRulesOnlyRedactor:
    def test_labels_all_nonempty_text_as_phi(self) -> None:
        rules = RulesOnlyRedactor()
        assert rules.classify("LEFT").label == PhiLabel.PHI
        assert rules.classify("15.2 cm").label == PhiLabel.PHI
        assert rules.classify("John Doe").label == PhiLabel.PHI

    def test_empty_text_is_unknown(self) -> None:
        rules = RulesOnlyRedactor()
        assert rules.classify("").label == PhiLabel.UNKNOWN

    def test_model_id_is_rules_only(self) -> None:
        rules = RulesOnlyRedactor()
        assert rules.model_id == "rules-only"
        assert rules.model_revision == "naive-redact-all"


# ---------------------------------------------------------------------------
# OpenMedPhiClassifier — fail-closed without transformers (criterion 5)
# ---------------------------------------------------------------------------
class TestOpenMedFailClosed:
    def test_empty_text_is_unknown(self) -> None:
        clf = OpenMedPhiClassifier()
        verdict = clf.classify("")
        assert verdict.label == PhiLabel.UNKNOWN
        assert verdict.entity_type == "EMPTY"

    def test_classifier_error_routes_to_unknown(self) -> None:
        # transformers is not installed → _ensure() raises → UNKNOWN/CLASSIFIER_ERROR.
        clf = OpenMedPhiClassifier()
        verdict = clf.classify("John Doe")
        assert verdict.label == PhiLabel.UNKNOWN
        assert verdict.entity_type == "CLASSIFIER_ERROR"

    def test_pinned_model_identity(self) -> None:
        clf = OpenMedPhiClassifier()
        assert clf.model_id == OPENMED_MODEL_ID
        assert clf.model_revision == OPENMED_MODEL_REVISION


# ---------------------------------------------------------------------------
# PhiClassification value semantics
# ---------------------------------------------------------------------------
def test_phi_classification_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    cls = PhiClassification(PhiLabel.PHI, 0.9, "PATIENT")
    with pytest.raises(FrozenInstanceError):
        cls.label = PhiLabel.CLINICAL  # type: ignore[misc]
