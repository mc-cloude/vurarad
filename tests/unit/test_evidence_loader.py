"""Rule-set loader — citation + reviewedBy enforcement, hash-verify (WP13 §3.16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import NotFoundError
from app.evidence.loader import (
    MAX_STATEMENT_LENGTH,
    RULESET_DIR,
    RuleSetLoadError,
    RuleSetRegistry,
    compute_hash,
    default_registry,
    load_rule_set_from_dict,
    load_rule_sets_from_directory,
)

BUNDLED_IDS = {"acr-incidental-adrenal", "birads-5e", "fleischner-2017", "lirads-2018"}


def _valid_data() -> dict[str, object]:
    """Return a minimal valid rule-set dict (safe to mutate per test)."""
    return {
        "id": "test-rs",
        "name": "Test rule set",
        "version": "1.0",
        "findingType": "pulmonary_nodule",
        "reviewedBy": "Dr. Review",
        "reviewedAt": "2024-01-01",
        "citation": {"doi": "10.0/0", "url": "https://example.org/x", "title": "T"},
        "rules": [
            {
                "id": "r1",
                "statement": "A short statement.",
                "logic": 'nodule_type == "solid" and diameter_mm >= 8',
                "recommendation": "Do something.",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestLoadValid:
    def test_load_returns_rule_set_with_hash(self) -> None:
        rs = load_rule_set_from_dict(_valid_data())
        assert rs.id == "test-rs"
        assert rs.hash
        assert len(rs.hash) == 64  # SHA-256 hex

    def test_compute_hash_is_deterministic(self) -> None:
        from app.evidence.engine import RuleSet

        rs = load_rule_set_from_dict(_valid_data())
        # Recompute over a fresh model (without the loader-set hash) — must match.
        again = RuleSet.model_validate(_valid_data())
        assert compute_hash(again) == rs.hash

    def test_load_strips_and_verifies_expected_hash(self) -> None:
        data = _valid_data()
        rs = load_rule_set_from_dict(data)
        data["expectedHash"] = rs.hash
        # Re-loading with the correct pinned hash must succeed.
        rs2 = load_rule_set_from_dict(data)
        assert rs2.hash == rs.hash


# ---------------------------------------------------------------------------
# Citation + reviewedBy enforcement (criterion 3)
# ---------------------------------------------------------------------------
class TestCitationAndReviewEnforcement:
    def test_missing_citation_refused(self) -> None:
        data = _valid_data()
        data["citation"] = {"doi": None, "url": None, "title": "T"}
        with pytest.raises(RuleSetLoadError, match="resolvable"):
            load_rule_set_from_dict(data)

    def test_doi_only_is_resolvable(self) -> None:
        data = _valid_data()
        data["citation"] = {"doi": "10.0/0", "url": None, "title": "T"}
        rs = load_rule_set_from_dict(data)
        assert rs.citation.doi == "10.0/0"

    def test_url_only_is_resolvable(self) -> None:
        data = _valid_data()
        data["citation"] = {"doi": None, "url": "https://example.org/x", "title": "T"}
        rs = load_rule_set_from_dict(data)
        assert rs.citation.url == "https://example.org/x"

    def test_missing_reviewed_by_refused(self) -> None:
        data = _valid_data()
        data["reviewedBy"] = ""
        with pytest.raises(RuleSetLoadError, match="reviewedBy"):
            load_rule_set_from_dict(data)

    def test_missing_reviewed_at_refused(self) -> None:
        data = _valid_data()
        data["reviewedAt"] = None
        with pytest.raises(RuleSetLoadError, match="reviewedAt"):
            load_rule_set_from_dict(data)

    def test_omitted_reviewed_at_refused(self) -> None:
        data = _valid_data()
        del data["reviewedAt"]
        with pytest.raises(RuleSetLoadError, match="reviewedAt"):
            load_rule_set_from_dict(data)


# ---------------------------------------------------------------------------
# Statement cap (criterion 5)
# ---------------------------------------------------------------------------
class TestStatementCap:
    def test_overlong_statement_refused(self) -> None:
        data = _valid_data()
        data["rules"] = [
            {
                "id": "r1",
                "statement": "x" * (MAX_STATEMENT_LENGTH + 1),
                "logic": 'nodule_type == "solid"',
                "recommendation": "r",
            }
        ]
        with pytest.raises(RuleSetLoadError, match="300"):
            load_rule_set_from_dict(data)

    def test_statement_at_cap_accepted(self) -> None:
        data = _valid_data()
        data["rules"] = [
            {
                "id": "r1",
                "statement": "x" * MAX_STATEMENT_LENGTH,
                "logic": 'nodule_type == "solid"',
                "recommendation": "r",
            }
        ]
        rs = load_rule_set_from_dict(data)
        assert len(rs.rules[0].statement) == MAX_STATEMENT_LENGTH


# ---------------------------------------------------------------------------
# Logic sanity (criterion 7)
# ---------------------------------------------------------------------------
class TestLogicValidation:
    def test_invalid_logic_syntax_refused(self) -> None:
        data = _valid_data()
        data["rules"][0]["logic"] = "nodule_type =="
        with pytest.raises(RuleSetLoadError, match="invalid logic"):
            load_rule_set_from_dict(data)

    def test_disallowed_logic_node_refused(self) -> None:
        data = _valid_data()
        data["rules"][0]["logic"] = "foo()"
        with pytest.raises(RuleSetLoadError, match="invalid logic"):
            load_rule_set_from_dict(data)

    def test_unknown_attribute_refused(self) -> None:
        data = _valid_data()
        data["rules"][0]["logic"] = "not_a_real_field == 1"
        with pytest.raises(RuleSetLoadError, match="unknown"):
            load_rule_set_from_dict(data)

    def test_unknown_attribute_in_not_applicable_when_refused(self) -> None:
        data = _valid_data()
        data["rules"][0]["notApplicableWhen"] = "bogus_field == 1"
        with pytest.raises(RuleSetLoadError, match="unknown"):
            load_rule_set_from_dict(data)


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------
class TestStructuralChecks:
    def test_unknown_finding_type_refused(self) -> None:
        data = _valid_data()
        data["findingType"] = "no_such_type"
        with pytest.raises(RuleSetLoadError, match="findingType"):
            load_rule_set_from_dict(data)

    def test_no_rules_refused(self) -> None:
        data = _valid_data()
        data["rules"] = []
        with pytest.raises(RuleSetLoadError, match="no rules"):
            load_rule_set_from_dict(data)

    def test_duplicate_rule_id_refused(self) -> None:
        data = _valid_data()
        dup = {
            "id": "dup",
            "statement": "s",
            "logic": 'nodule_type == "solid"',
            "recommendation": "r",
        }
        data["rules"] = [dup, dict(dup)]
        with pytest.raises(RuleSetLoadError, match="duplicate"):
            load_rule_set_from_dict(data)

    def test_rule_without_id_refused(self) -> None:
        data = _valid_data()
        data["rules"] = [
            {"id": "", "statement": "s", "logic": 'nodule_type == "solid"', "recommendation": "r"}
        ]
        with pytest.raises(RuleSetLoadError, match="without an id"):
            load_rule_set_from_dict(data)


# ---------------------------------------------------------------------------
# Hash-verify
# ---------------------------------------------------------------------------
class TestHashVerify:
    def test_mismatched_expected_hash_refused(self) -> None:
        data = _valid_data()
        data["expectedHash"] = "0" * 64
        with pytest.raises(RuleSetLoadError, match="expectedHash"):
            load_rule_set_from_dict(data)


# ---------------------------------------------------------------------------
# Directory loading + registry
# ---------------------------------------------------------------------------
class TestDirectoryLoading:
    def test_bundled_directory_loads_all_four(self) -> None:
        reg = load_rule_sets_from_directory(RULESET_DIR)
        assert set(reg.ids()) == BUNDLED_IDS
        for rs in reg.all():
            assert rs.hash
            assert rs.citation.resolvable()
            assert rs.reviewed_by
            assert rs.reviewed_at

    def test_default_registry_is_cached(self) -> None:
        assert default_registry() is default_registry()

    def test_registry_get_unknown_raises_not_found(self) -> None:
        reg = RuleSetRegistry({})
        with pytest.raises(NotFoundError):
            reg.get("nope")

    def test_duplicate_rule_set_id_in_directory_refused(self, tmp_path: Path) -> None:
        import yaml

        data = _valid_data()
        (tmp_path / "a.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        (tmp_path / "b.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(RuleSetLoadError, match="duplicate rule set id"):
            load_rule_sets_from_directory(tmp_path)

    def test_non_mapping_yaml_refused(self, tmp_path: Path) -> None:
        (tmp_path / "bad.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
        with pytest.raises(RuleSetLoadError, match="mapping"):
            load_rule_sets_from_directory(tmp_path)


# ---------------------------------------------------------------------------
# No Radiopaedia anywhere in the bundled rule sets (criterion 4)
# ---------------------------------------------------------------------------
class TestNoRadiopaedia:
    def test_no_radiopaedia_in_ruleset_files(self) -> None:
        offenders: list[str] = []
        for path in sorted(RULESET_DIR.glob("*.yaml")):
            if "radiopaedia" in path.read_text(encoding="utf-8").lower():
                offenders.append(str(path))
        assert offenders == [], f"radiopaedia references found: {offenders}"
