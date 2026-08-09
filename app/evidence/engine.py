"""Rule engine — pure, deterministic rule-set evaluation (WP13 §3.16).

The engine matches a rule set against a validated attribute dict and returns the
rules whose ``logic`` predicate holds.  It is a PURE function: no I/O, no LLM,
no network, no global mutable state.  The same ``(rule_set, attributes)`` always
yields the same ``list[RuleMatch]`` (criterion 7 — the ``logic`` string
round-trips through the evaluator to the same result).

The ``logic`` (and optional ``notApplicableWhen``) strings are evaluated by a
restricted Python-expression evaluator built on the :mod:`ast` module.  Only a
whitelist of node types is permitted — boolean ops, comparisons, membership,
constants, and ``Name`` lookups against the attribute dict — so the expression
language cannot call functions, access attributes, import modules, or otherwise
escape its sandbox.  This keeps the engine a pure function with no ambient
authority.
"""

from __future__ import annotations

import ast
from datetime import date
from typing import Any

from app.models.common import CamelModel


# ---------------------------------------------------------------------------
# Rule-set models
# ---------------------------------------------------------------------------
class Citation(CamelModel):
    """A bibliographic citation backing a rule set — a resolvable DOI or URL."""

    doi: str | None = None
    url: str | None = None
    title: str = ""

    def resolvable(self) -> bool:
        """True when at least one resolvable locator (DOI or URL) is present."""
        return bool(self.doi) or bool(self.url)


class Rule(CamelModel):
    """One evidence rule.

    ``statement`` is a short (≤ 300 chars) human-readable summary — never
    reproduced article text.  ``logic`` is the deterministic predicate evaluated
    against the finding's attributes.  ``notApplicableWhen`` is an optional
    override predicate; when it holds the rule is skipped regardless of
    ``logic`` (this is the ONLY field in which clinical-nuance wording such as
    risk-stratification terms is permitted — criterion 6).
    """

    id: str
    statement: str
    logic: str
    recommendation: str = ""
    citation_id: str = ""
    not_applicable_when: str | None = None


class RuleSet(CamelModel):
    """A versioned, reviewed collection of evidence rules for a finding type."""

    id: str
    name: str
    version: str
    finding_type: str
    reviewed_by: str
    reviewed_at: date
    citation: Citation
    rules: list[Rule]
    hash: str = ""  # SHA-256 of canonical content, set by the loader
    expected_hash: str | None = None  # optional pinned hash; loader verifies if set


class RuleMatch(CamelModel):
    """A rule that matched the supplied attributes — returned to the reader."""

    rule_id: str
    rule_set_id: str
    rule_set_version: str
    finding_type: str
    statement: str
    recommendation: str
    citation_id: str
    hash: str


__all__ = ["Citation", "Rule", "RuleMatch", "RuleSet"]


# ---------------------------------------------------------------------------
# Safe expression evaluator
# ---------------------------------------------------------------------------
# Node types permitted in a logic / notApplicableWhen expression.  Anything
# outside this set is rejected at validation time (loader) and at runtime.
_ALLOWED_COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.In: "in",
    ast.NotIn: "not in",
}


class RuleLogicError(ValueError):
    """A logic expression is syntactically invalid or uses a disallowed node."""


def _validate_node(node: ast.AST) -> None:
    """Walk an expression AST and raise on any disallowed node."""
    for child in ast.walk(node):
        cls = type(child)
        if isinstance(
            child,
            (
                ast.Expression,
                ast.BoolOp,
                ast.UnaryOp,
                ast.Compare,
                ast.Constant,
                ast.Name,
                ast.List,
                ast.Tuple,
                ast.Load,
                ast.And,
                ast.Or,
                ast.Not,
            ),
        ):
            continue
        if isinstance(child, ast.cmpop) and cls in _ALLOWED_COMPARE_OPS:
            continue
        raise RuleLogicError(f"disallowed expression node: {cls.__name__}")


def _parse_logic(expr: str) -> ast.Expression:
    """Parse a logic expression in eval mode, raising ``RuleLogicError``."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise RuleLogicError(f"invalid logic expression: {exc.msg}") from exc
    assert isinstance(tree, ast.Expression)  # mode="eval" guarantees this
    _validate_node(tree)
    return tree


def logic_names(expr: str) -> set[str]:
    """Return the set of ``Name`` identifiers referenced by a logic expression.

    Used by the loader to cross-check that a rule only references attributes
    defined for its finding type.  Raises :class:`RuleLogicError` on invalid
    expressions.
    """
    tree = _parse_logic(expr)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _eval_node(node: ast.AST, attributes: dict[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        values = (_eval_node(v, attributes) for v in node.values)
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise RuleLogicError("disallowed boolean operator")
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, attributes)
        raise RuleLogicError("disallowed unary operator")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, attributes)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, attributes)
            if not _apply_compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in attributes:
            raise KeyError(node.id)
        return attributes[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(e, attributes) for e in node.elts]
    raise RuleLogicError(f"disallowed expression node: {type(node).__name__}")


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.Lt):
        return bool(left < right)
    if isinstance(op, ast.LtE):
        return bool(left <= right)
    if isinstance(op, ast.Gt):
        return bool(left > right)
    if isinstance(op, ast.GtE):
        return bool(left >= right)
    if isinstance(op, ast.In):
        return bool(left in right)
    if isinstance(op, ast.NotIn):
        return bool(left not in right)
    raise RuleLogicError(f"disallowed comparison operator: {type(op).__name__}")


def evaluate_logic(expr: str, attributes: dict[str, Any]) -> bool:
    """Evaluate a logic expression against ``attributes``.

    Pure and deterministic.  Raises :class:`RuleLogicError` for malformed
    expressions and :class:`KeyError` for unresolved attribute names — callers
    that want a safe "no-match on error" semantics should use
    :meth:`RuleEngine.evaluate`.
    """
    tree = _parse_logic(expr)
    return bool(_eval_node(tree.body, attributes))


def _safe_eval(expr: str, attributes: dict[str, Any]) -> bool:
    """Evaluate a predicate, returning ``False`` on any error.

    A rule that references a missing attribute, or whose comparison types are
    incompatible, simply does not match — never raises.  This keeps
    :meth:`RuleEngine.evaluate` a total, pure function.
    """
    try:
        return evaluate_logic(expr, attributes)
    except (RuleLogicError, KeyError, TypeError, ValueError):
        return False


def _citation_id(rule: Rule, rule_set: RuleSet) -> str:
    """Resolve the citation id backing a rule (rule-specific, else the set's)."""
    if rule.citation_id:
        return rule.citation_id
    return rule_set.citation.doi or rule_set.citation.url or ""


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------
class RuleEngine:
    """Pure rule-set evaluator — no I/O, no LLM, no network."""

    @staticmethod
    def evaluate(rule_set: RuleSet, attributes: dict[str, Any]) -> list[RuleMatch]:
        """Return the rules in ``rule_set`` whose ``logic`` matches ``attributes``.

        ``notApplicableWhen`` short-circuits a rule to "no match" when it holds.
        The result is deterministic for a given ``(rule_set, attributes)`` pair.
        """
        matches: list[RuleMatch] = []
        for rule in rule_set.rules:
            if rule.not_applicable_when and _safe_eval(rule.not_applicable_when, attributes):
                continue
            if _safe_eval(rule.logic, attributes):
                matches.append(
                    RuleMatch(
                        rule_id=rule.id,
                        rule_set_id=rule_set.id,
                        rule_set_version=rule_set.version,
                        finding_type=rule_set.finding_type,
                        statement=rule.statement,
                        recommendation=rule.recommendation,
                        citation_id=_citation_id(rule, rule_set),
                        hash=rule_set.hash,
                    )
                )
        return matches
