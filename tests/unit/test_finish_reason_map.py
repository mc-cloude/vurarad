"""FinishReason mapping — every installed-SDK member maps to a wire string (criterion 9).

Iterates ``types.FinishReason`` from the *installed* google-genai SDK and
asserts each member is present in :data:`FINISH_REASON_MAP`.  An SDK upgrade
that adds a new ``FinishReason`` member will fail this test (and the build)
until someone explicitly maps the new member — the safety property of criterion 9.
"""

from __future__ import annotations

from google.genai import types

from app.services.gemini_service import FINISH_REASON_MAP, map_finish_reason


def test_every_finish_reason_member_is_mapped() -> None:
    """Every ``types.FinishReason`` member from the installed SDK has a map entry."""
    mapped = set(FINISH_REASON_MAP)
    members = set(types.FinishReason)
    missing = members - mapped
    assert not missing, (
        "Unmapped FinishReason member(s) — SDK upgrade? "
        f"Map them: {sorted(m.value for m in missing)}"
    )


def test_finish_reason_map_values_are_nonempty_strings() -> None:
    """Every mapped wire string is a non-empty ``str``."""
    for reason, wire in FINISH_REASON_MAP.items():
        assert isinstance(wire, str), f"{reason!r} maps to non-str {wire!r}"
        assert wire, f"{reason!r} maps to empty wire string"


def test_map_finish_reason_known_member() -> None:
    """A known member maps to its enum value string."""
    assert map_finish_reason(types.FinishReason.STOP) == "STOP"
    assert map_finish_reason(types.FinishReason.MAX_TOKENS) == "MAX_TOKENS"


def test_map_finish_reason_none_is_none() -> None:
    """``None`` (no finish reason produced) maps to ``None``."""
    assert map_finish_reason(None) is None


def test_finish_reason_map_matches_enum_values() -> None:
    """The wire string for each member equals the member's enum value."""
    for reason in FINISH_REASON_MAP:
        assert FINISH_REASON_MAP[reason] == reason.value
