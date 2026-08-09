"""Rule engine — pure, deterministic rule-set evaluation (WP13 §3.16)."""

from __future__ import annotations

from datetime import date

import pytest

from app.evidence.attributes import validate_attributes
from app.evidence.engine import (
    Citation,
    Rule,
    RuleEngine,
    RuleLogicError,
    RuleSet,
    evaluate_logic,
    logic_names,
)
from app.evidence.loader import default_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rule(
    rid: str,
    logic: str,
    *,
    statement: str = "s",
    recommendation: str = "r",
    not_applicable_when: str | None = None,
) -> Rule:
    return Rule(
        id=rid,
        statement=statement,
        logic=logic,
        recommendation=recommendation,
        not_applicable_when=not_applicable_when,
    )


def _rule_set(rules: list[Rule], **kwargs: object) -> RuleSet:
    return RuleSet(
        id=kwargs.get("id", "test"),
        name=kwargs.get("name", "Test"),
        version=kwargs.get("version", "1"),
        finding_type=kwargs.get("finding_type", "pulmonary_nodule"),
        reviewed_by="Reviewer",
        reviewed_at=date(2024, 1, 1),
        citation=Citation(doi="10.0/0", url="https://example.org/x", title="t"),
        rules=rules,
        hash="h",
    )


# ---------------------------------------------------------------------------
# Pure evaluator
# ---------------------------------------------------------------------------
class TestEvaluateLogic:
    def test_equality_and_boolean(self) -> None:
        assert evaluate_logic('a == "solid" and b > 5', {"a": "solid", "b": 6}) is True
        assert evaluate_logic('a == "solid" and b > 5', {"a": "ground_glass", "b": 6}) is False

    def test_or_and_not(self) -> None:
        assert evaluate_logic("a or b", {"a": False, "b": True}) is True
        assert evaluate_logic("not a", {"a": False}) is True
        assert evaluate_logic("not a", {"a": True}) is False

    def test_membership_in_and_not_in(self) -> None:
        assert evaluate_logic('a in ["x", "y"]', {"a": "y"}) is True
        assert evaluate_logic('a in ["x", "y"]', {"a": "z"}) is False
        assert evaluate_logic('a not in ["x", "y"]', {"a": "z"}) is True

    def test_parenthesised_grouping(self) -> None:
        assert evaluate_logic("(a or b) and c", {"a": True, "b": False, "c": True}) is True
        assert evaluate_logic("(a or b) and c", {"a": False, "b": False, "c": True}) is False

    def test_chained_comparison(self) -> None:
        assert evaluate_logic("5 < b < 10", {"b": 7}) is True
        assert evaluate_logic("5 < b < 10", {"b": 11}) is False

    def test_returns_bool(self) -> None:
        assert isinstance(evaluate_logic("a", {"a": True}), bool)


class TestSafeEvaluatorRejection:
    """The evaluator refuses anything outside the expression whitelist."""

    @pytest.mark.parametrize(
        "expr",
        [
            "foo()",
            "x.attr",
            "__import__('os')",
            "1 + 1",
            "a if b else c",
            "lambda x: x",
        ],
    )
    def test_disallowed_nodes_raise(self, expr: str) -> None:
        with pytest.raises(RuleLogicError):
            evaluate_logic(expr, {"a": 1, "b": 1, "x": 1, "c": 1, "foo": 1})

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(RuleLogicError):
            evaluate_logic("a ==", {"a": 1})

    def test_logic_names_extracts_identifiers(self) -> None:
        assert logic_names('a == 1 and b > 2 or c in ["x"]') == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# RuleEngine.evaluate
# ---------------------------------------------------------------------------
class TestRuleEngineEvaluate:
    def test_returns_only_matching_rules(self) -> None:
        rs = _rule_set(
            [
                _rule("r1", 'k == "a"'),
                _rule("r2", 'k == "b"'),
                _rule("r3", 'k == "c"'),
            ]
        )
        matches = RuleEngine.evaluate(rs, {"k": "b"})
        assert [m.rule_id for m in matches] == ["r2"]

    def test_match_carries_rule_set_metadata(self) -> None:
        rs = _rule_set([_rule("r1", "k == 1")], id="rs1", version="2.3")
        matches = RuleEngine.evaluate(rs, {"k": 1})
        assert len(matches) == 1
        m = matches[0]
        assert m.rule_set_id == "rs1"
        assert m.rule_set_version == "2.3"
        assert m.finding_type == "pulmonary_nodule"
        assert m.hash == "h"
        assert m.citation_id  # falls back to the rule-set DOI/URL

    def test_deterministic_round_trip(self) -> None:
        """Criterion 7: the logic string round-trips to the same result."""
        rs = _rule_set(
            [
                _rule("r1", 't == "solid" and d >= 6 and d < 8 and risk == "low"'),
                _rule("r2", 't == "solid" and d >= 8'),
                _rule("r3", 't == "ground_glass" and d >= 6'),
            ]
        )
        attrs = {"t": "solid", "d": 7, "risk": "low"}
        first = RuleEngine.evaluate(rs, attrs)
        for _ in range(5):
            assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == [m.rule_id for m in first]

    def test_not_applicable_when_overrides_match(self) -> None:
        rs = _rule_set(
            [
                _rule("r1", "d >= 8", not_applicable_when='count == "multiple"'),
            ]
        )
        # Matches when single.
        assert [m.rule_id for m in RuleEngine.evaluate(rs, {"d": 10, "count": "single"})] == ["r1"]
        # Suppressed when multiple.
        assert RuleEngine.evaluate(rs, {"d": 10, "count": "multiple"}) == []

    def test_missing_attribute_does_not_match(self) -> None:
        rs = _rule_set([_rule("r1", "missing == 1")])
        assert RuleEngine.evaluate(rs, {}) == []

    def test_incompatible_type_comparison_does_not_match(self) -> None:
        rs = _rule_set([_rule("r1", 'd < "small"')])
        assert RuleEngine.evaluate(rs, {"d": 5}) == []

    def test_empty_rule_set_matches_nothing(self) -> None:
        rs = _rule_set([])
        assert RuleEngine.evaluate(rs, {"k": 1}) == []


# ---------------------------------------------------------------------------
# Bundled rule sets — end-to-end through the pure engine
# ---------------------------------------------------------------------------
class TestBundledRuleSets:
    def test_fleischner_solid_6_to_8_low_single(self) -> None:
        rs = default_registry().get("fleischner-2017")
        attrs = validate_attributes(
            "pulmonary_nodule",
            {"noduleType": "solid", "diameterMm": 7, "patientRisk": "low", "noduleCount": "single"},
        )
        assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == [
            "solid-single-6-to-8-low-risk"
        ]

    def test_fleischner_large_high_risk_review_skipped_for_multiple(self) -> None:
        rs = default_registry().get("fleischner-2017")
        attrs = validate_attributes(
            "pulmonary_nodule",
            {
                "noduleType": "solid",
                "diameterMm": 10,
                "patientRisk": "high",
                "noduleCount": "multiple",
            },
        )
        ids = {m.rule_id for m in RuleEngine.evaluate(rs, attrs)}
        # The high-risk-review rule is suppressed for multiple nodules.
        assert "solid-large-high-risk-review" not in ids

    def test_birads_category_5(self) -> None:
        rs = default_registry().get("birads-5e")
        attrs = validate_attributes("breast_assessment", {"category": "5"})
        assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == [
            "birads-5-suggestive-carcinoma"
        ]

    def test_lirads_lr_5(self) -> None:
        rs = default_registry().get("lirads-2018")
        attrs = validate_attributes("liver_observation", {"category": "LR-5"})
        assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == ["lr-5-definite-hcc"]

    def test_adrenal_none_hu_measures_attenuation(self) -> None:
        rs = default_registry().get("acr-incidental-adrenal")
        attrs = validate_attributes("adrenal_incidental", {"diameterMm": 25, "unenhancedHu": None})
        assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == [
            "adrenal-measure-attenuation"
        ]

    def test_adrenal_lipid_rich_adenoma(self) -> None:
        rs = default_registry().get("acr-incidental-adrenal")
        attrs = validate_attributes("adrenal_incidental", {"diameterMm": 25, "unenhancedHu": 4})
        assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == ["adrenal-lipid-rich-adenoma"]

    def test_adrenal_tiny_skips_large_or_growing(self) -> None:
        rs = default_registry().get("acr-incidental-adrenal")
        attrs = validate_attributes(
            "adrenal_incidental", {"diameterMm": 5, "hasHistoricalGrowth": True}
        )
        ids = {m.rule_id for m in RuleEngine.evaluate(rs, attrs)}
        assert ids == {"adrenal-tiny-no-followup"}

    def test_bundled_evaluation_is_deterministic(self) -> None:
        reg = default_registry()
        rs = reg.get("fleischner-2017")
        attrs = validate_attributes(
            "pulmonary_nodule",
            {"noduleType": "part_solid", "diameterMm": 9, "patientRisk": "high"},
        )
        first = [m.rule_id for m in RuleEngine.evaluate(rs, attrs)]
        assert [m.rule_id for m in RuleEngine.evaluate(rs, attrs)] == first
