"""No CADt wording outside ``notApplicableWhen`` in any rule set (criterion 6).

A CADt (computer-aided triage) device classifies findings by suspicion,
likelihood, or probability.  The evidence rule sets must not carry that
vocabulary — ``malignan``/``probab``/``likel``/``suspicious`` — except inside the
``notApplicableWhen`` predicate, which is the designed escape hatch for genuine
clinical-nuance conditions.  This test enforces the rule at both the parsed-model
level and the raw-YAML text level.
"""

from __future__ import annotations

from datetime import date

from app.evidence.engine import Citation, Rule, RuleSet
from app.evidence.loader import RULESET_DIR, default_registry

# Substrings that would make the evidence layer read like a CADt device.
FORBIDDEN: tuple[str, ...] = ("malignan", "probab", "likel", "suspicious")


def _hits(text: str) -> list[str]:
    low = text.lower()
    return [w for w in FORBIDDEN if w in low]


def _checked_strings(rule_set: RuleSet) -> list[tuple[str, str]]:
    """All string values that must stay free of CADt vocabulary.

    ``not_applicable_when`` is deliberately excluded — it is the one field
    permitted to carry clinical-nuance wording.
    """
    items: list[tuple[str, str]] = [
        ("id", rule_set.id),
        ("name", rule_set.name),
        ("version", rule_set.version),
        ("findingType", rule_set.finding_type),
        ("reviewedBy", rule_set.reviewed_by),
        ("citation.title", rule_set.citation.title),
        ("citation.doi", rule_set.citation.doi or ""),
        ("citation.url", rule_set.citation.url or ""),
    ]
    for rule in rule_set.rules:
        items.extend(
            [
                (f"{rule.id}.id", rule.id),
                (f"{rule.id}.statement", rule.statement),
                (f"{rule.id}.logic", rule.logic),
                (f"{rule.id}.recommendation", rule.recommendation),
                (f"{rule.id}.citationId", rule.citation_id),
            ]
        )
    return items


def _cadt_offenders(rule_set: RuleSet) -> list[tuple[str, list[str]]]:
    offenders: list[tuple[str, list[str]]] = []
    for label, value in _checked_strings(rule_set):
        hits = _hits(value)
        if hits:
            offenders.append((label, hits))
    return offenders


# ---------------------------------------------------------------------------
# Bundled rule sets — parsed-model level
# ---------------------------------------------------------------------------
def test_bundled_rule_sets_have_no_cadt_words_outside_not_applicable_when() -> None:
    for rule_set in default_registry().all():
        offenders = _cadt_offenders(rule_set)
        assert offenders == [], (
            f"rule set {rule_set.id} has CADt wording outside notApplicableWhen: {offenders}"
        )


# ---------------------------------------------------------------------------
# Bundled rule sets — raw-YAML text level
# ---------------------------------------------------------------------------
def test_raw_yaml_has_no_cadt_words_outside_not_applicable_when_lines() -> None:
    for path in sorted(RULESET_DIR.glob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            hits = _hits(line)
            if not hits:
                continue
            assert "notApplicableWhen" in line, (
                f"{path.name}: CADt word(s) {hits} outside notApplicableWhen: {line!r}"
            )


# ---------------------------------------------------------------------------
# notApplicableWhen is the exemption — verified with synthetic rules
# ---------------------------------------------------------------------------
def _synthetic_rule_set(rule: Rule) -> RuleSet:
    return RuleSet(
        id="synthetic",
        name="Synthetic",
        version="1",
        finding_type="pulmonary_nodule",
        reviewed_by="Reviewer",
        reviewed_at=date(2024, 1, 1),
        citation=Citation(doi="10.0/0", url="https://example.org/x", title="t"),
        rules=[rule],
        hash="h",
    )


def test_forbidden_word_in_statement_is_flagged() -> None:
    rule = Rule(id="r", statement="a suspicious nodule", logic='nodule_type == "solid"')
    offenders = _cadt_offenders(_synthetic_rule_set(rule))
    assert any(label.endswith(".statement") for label, _ in offenders)


def test_forbidden_word_only_in_not_applicable_when_is_exempt() -> None:
    rule = Rule(
        id="r",
        statement="a nodule",
        logic='nodule_type == "solid"',
        not_applicable_when='patient_risk == "high" and cat == "suspicious"',
    )
    offenders = _cadt_offenders(_synthetic_rule_set(rule))
    assert offenders == []
