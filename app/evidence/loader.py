"""Rule-set loader — load, validate, and hash-verify rule sets at startup (WP13).

The loader reads YAML rule-set files, validates each one against the
:class:`RuleSet` model and a set of structural rules, computes a content hash,
and registers the result.  It is the single gate that keeps unreviewed or
uncited rules out of the system:

- **Citation required** — a rule set without a resolvable DOI *or* URL is
  refused (criterion 3).
- **Review required** — a rule set without ``reviewedBy`` *and* ``reviewedAt``
  is refused (criterion 3).
- **Statement cap** — every rule ``statement`` is ≤ 300 characters; no
  reproduced article text enters the system (criterion 5).
- **Logic sanity** — every ``logic`` (and optional ``notApplicableWhen``) must
  parse as a valid restricted expression and may only reference attributes
  defined for the rule set's ``findingType`` (criterion 7).
- **Hash-verify** — when a rule set pins an ``expectedHash``, the loader refuses
  it if the computed content hash does not match (tamper / drift detection).

Validation failures raise :class:`RuleSetLoadError` so a bad rule set aborts
startup rather than silently shipping broken evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from app.core.errors import NotFoundError
from app.evidence.attributes import FINDING_TYPE_ATTRIBUTES, attribute_names
from app.evidence.engine import (
    RuleLogicError,
    RuleSet,
    logic_names,
)

# Maximum length of a rule ``statement`` (criterion 5).
MAX_STATEMENT_LENGTH = 300

# Directory containing the bundled rule-set YAML files.
RULESET_DIR = Path(__file__).resolve().parent / "rulesets"


class RuleSetLoadError(ValueError):
    """A rule set failed validation and must not be loaded."""


class RuleSetRegistry:
    """An immutable, id-keyed collection of loaded rule sets."""

    def __init__(self, rule_sets: dict[str, RuleSet]) -> None:
        self._rule_sets: dict[str, RuleSet] = dict(rule_sets)

    def get(self, rule_set_id: str) -> RuleSet:
        """Return the rule set, or raise 404 :class:`NotFoundError`."""
        rule_set = self._rule_sets.get(rule_set_id)
        if rule_set is None:
            raise NotFoundError(f"Rule set {rule_set_id!r} not found")
        return rule_set

    def ids(self) -> list[str]:
        return sorted(self._rule_sets.keys())

    def all(self) -> list[RuleSet]:
        return [self._rule_sets[k] for k in self.ids()]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def compute_hash(rule_set: RuleSet) -> str:
    """Compute the SHA-256 of the rule set's canonical JSON (excluding hashes)."""
    payload = rule_set.model_dump(mode="json", by_alias=True, exclude={"hash", "expected_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_rule_set(data: dict[str, Any], *, source: str) -> RuleSet:
    """Validate a raw dict into a :class:`RuleSet`, enforcing all criteria."""
    try:
        rule_set = RuleSet.model_validate(data)
    except Exception as exc:  # ValidationError or PydanticCustomError
        raise RuleSetLoadError(f"{source}: invalid rule set structure: {exc}") from exc

    # -- citation required (criterion 3) -------------------------------------
    if not rule_set.citation.resolvable():
        raise RuleSetLoadError(
            f"{source}: rule set {rule_set.id!r} lacks a resolvable DOI or URL citation"
        )

    # -- review required (criterion 3) ---------------------------------------
    if not rule_set.reviewed_by or not rule_set.reviewed_at:
        raise RuleSetLoadError(
            f"{source}: rule set {rule_set.id!r} lacks reviewedBy or reviewedAt"
        )

    # -- finding type known --------------------------------------------------
    if rule_set.finding_type not in FINDING_TYPE_ATTRIBUTES:
        raise RuleSetLoadError(
            f"{source}: rule set {rule_set.id!r} has unknown findingType "
            f"{rule_set.finding_type!r}"
        )
    known_attrs = attribute_names(rule_set.finding_type)

    # -- rules ---------------------------------------------------------------
    if not rule_set.rules:
        raise RuleSetLoadError(f"{source}: rule set {rule_set.id!r} has no rules")

    seen_ids: set[str] = set()
    for rule in rule_set.rules:
        if not rule.id:
            raise RuleSetLoadError(f"{source}: rule set {rule_set.id!r} has a rule without an id")
        if rule.id in seen_ids:
            raise RuleSetLoadError(
                f"{source}: duplicate rule id {rule.id!r} in rule set {rule_set.id!r}"
            )
        seen_ids.add(rule.id)

        # statement cap (criterion 5)
        if len(rule.statement) > MAX_STATEMENT_LENGTH:
            raise RuleSetLoadError(
                f"{source}: rule {rule.id!r} statement exceeds {MAX_STATEMENT_LENGTH} characters"
            )

        # logic parses and references only known attributes (criterion 7)
        _validate_logic(rule.logic, known_attrs, rule.id, rule_set.id, source)
        if rule.not_applicable_when:
            _validate_logic(
                rule.not_applicable_when,
                known_attrs,
                rule.id,
                rule_set.id,
                source,
            )

    return rule_set


def _validate_logic(
    expr: str,
    known_attrs: set[str],
    rule_id: str,
    rule_set_id: str,
    source: str,
) -> None:
    """Ensure ``expr`` parses and references only known attribute names."""
    try:
        names = logic_names(expr)
    except RuleLogicError as exc:
        raise RuleSetLoadError(
            f"{source}: rule {rule_id!r} in {rule_set_id!r} has invalid logic: {exc}"
        ) from exc
    unknown = names - known_attrs
    if unknown:
        raise RuleSetLoadError(
            f"{source}: rule {rule_id!r} in {rule_set_id!r} references unknown "
            f"attributes: {sorted(unknown)}"
        )


def load_rule_set_from_dict(data: dict[str, Any], *, source: str = "<inline>") -> RuleSet:
    """Validate, hash, and verify a rule set from an in-memory dict."""
    rule_set = _validate_rule_set(data, source=source)
    computed = compute_hash(rule_set)
    if rule_set.expected_hash is not None and rule_set.expected_hash != computed:
        raise RuleSetLoadError(
            f"{source}: rule set {rule_set.id!r} expectedHash "
            f"{rule_set.expected_hash[:12]}... does not match computed {computed[:12]}..."
        )
    rule_set.hash = computed
    return rule_set


def load_rule_sets_from_directory(directory: Path) -> RuleSetRegistry:
    """Load and validate every ``*.yaml`` / ``*.yml`` rule set in ``directory``."""
    registry: dict[str, RuleSet] = {}
    paths = sorted(
        [*directory.glob("*.yaml"), *directory.glob("*.yml")],
    )
    for path in paths:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise RuleSetLoadError(f"{path}: top-level YAML must be a mapping")
        rule_set = load_rule_set_from_dict(data, source=str(path))
        if rule_set.id in registry:
            raise RuleSetLoadError(f"{path}: duplicate rule set id {rule_set.id!r}")
        registry[rule_set.id] = rule_set
    return RuleSetRegistry(registry)


_default_registry: RuleSetRegistry | None = None


def default_registry() -> RuleSetRegistry:
    """Return the registry of bundled rule sets (loaded once, cached)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = load_rule_sets_from_directory(RULESET_DIR)
    return _default_registry


__all__ = [
    "MAX_STATEMENT_LENGTH",
    "RULESET_DIR",
    "RuleSetLoadError",
    "RuleSetRegistry",
    "compute_hash",
    "default_registry",
    "load_rule_set_from_dict",
    "load_rule_sets_from_directory",
]
