"""SectionStreamParser — byte-offset fuzzing and edge cases (criterion 8).

The central invariant: feeding the whole fixture in one call produces the same
concatenated per-section output as feeding it split at *any* byte/character
offset.  This is parameterized over every split offset of a 5-section fixture,
plus a byte-by-byte feed and the empty / no-markers edge cases.
"""

from __future__ import annotations

import pytest

from app.services.gemini_stream import SectionStreamParser, reconstruct

# A 5-section fixture with realistic radiology content.  Markers are split at
# every offset below (including mid-marker offsets) to exercise the look-back.
FIXTURE = (
    "<<SECTION:Findings>>The liver is normal in size and echogenicity.\n"
    "No focal lesions are seen.\n"
    "<<SECTION:Impression>>No acute intrathoracic findings.\n"
    "<<SECTION:Technique>>Helical CT of the chest was performed without IV contrast.\n"
    "<<SECTION:Comparison>>No prior studies are available for comparison.\n"
    "<<SECTION:Recommendation>>No further imaging is indicated at this time.\n"
)


def _parse_whole(text: str) -> list[tuple[str, str]]:
    parser = SectionStreamParser()
    deltas = list(parser.feed(text))
    deltas.extend(parser.flush())
    return deltas


def _parse_split(text: str, offset: int) -> list[tuple[str, str]]:
    parser = SectionStreamParser()
    deltas = list(parser.feed(text[:offset]))
    deltas.extend(parser.feed(text[offset:]))
    deltas.extend(parser.flush())
    return deltas


def _parse_byte_by_byte(text: str) -> list[tuple[str, str]]:
    parser = SectionStreamParser()
    deltas: list[tuple[str, str]] = []
    for ch in text:
        deltas.extend(parser.feed(ch))
    deltas.extend(parser.flush())
    return deltas


_EXPECTED = reconstruct(_parse_whole(FIXTURE))


def test_whole_feed_has_five_sections() -> None:
    """Sanity: the whole-feed parse yields exactly the five expected sections."""
    assert [title for title, _body in _EXPECTED] == [
        "Findings",
        "Impression",
        "Technique",
        "Comparison",
        "Recommendation",
    ]
    bodies = dict(_EXPECTED)
    assert "liver is normal" in bodies["Findings"]
    assert "No acute" in bodies["Impression"]


@pytest.mark.parametrize("offset", list(range(len(FIXTURE) + 1)))
def test_byte_offset_fuzzing(offset: int) -> None:
    """Split the fixture at this offset — reconstructed output must match the whole feed."""
    deltas = _parse_split(FIXTURE, offset)
    assert reconstruct(deltas) == _EXPECTED, f"Mismatch at split offset {offset}"


def test_byte_by_byte_feed_matches_whole() -> None:
    """The strongest split — one character at a time — still reconstructs identically."""
    assert reconstruct(_parse_byte_by_byte(FIXTURE)) == _EXPECTED


def test_byte_offset_fragment_concatenation() -> None:
    """Per-section fragment concatenation is identical across split offsets (criterion 8)."""
    whole_bodies = dict(_EXPECTED)
    for offset in range(0, len(FIXTURE) + 1, 7):
        split_bodies = dict(reconstruct(_parse_split(FIXTURE, offset)))
        assert set(split_bodies) == set(whole_bodies)
        for section in whole_bodies:
            assert split_bodies[section] == whole_bodies[section], (
                f"Section {section!r} body differs at offset {offset}"
            )


def test_empty_input() -> None:
    """An empty feed yields no deltas and no sections."""
    parser = SectionStreamParser()
    assert list(parser.feed("")) == []
    assert list(parser.flush()) == []
    assert parser.current_section is None


def test_no_section_markers() -> None:
    """Text with no markers yields no deltas — everything is preamble/discarded."""
    parser = SectionStreamParser()
    deltas = list(parser.feed("Just some narrative text with no markers at all."))
    deltas.extend(parser.flush())
    assert deltas == []
    assert parser.current_section is None


def test_preamble_before_first_marker_is_discarded() -> None:
    """Text before the first marker is preamble — never emitted as a body fragment."""
    parser = SectionStreamParser()
    deltas = list(parser.feed("preamble to discard<<SECTION:Only>>body text here"))
    deltas.extend(parser.flush())
    assert reconstruct(deltas) == [("Only", "body text here")]


def test_marker_split_across_feeds() -> None:
    """A marker straddling a feed boundary is buffered, not leaked as a fragment."""
    text = "<<SECTION:Findings>>the body"
    # Split right inside the marker prefix: "...<<SECTI" | "ON:Findings>>the body"
    cut = text.index("ON:Findings>>")
    parser = SectionStreamParser()
    deltas = list(parser.feed(text[:cut]))
    deltas.extend(parser.feed(text[cut:]))
    deltas.extend(parser.flush())
    assert reconstruct(deltas) == [("Findings", "the body")]


def test_repeated_section_title() -> None:
    """Two markers with the same title produce two deltas; reconstruct merges the run."""
    text = "<<SECTION:Note>>first<<SECTION:Note>>second"
    deltas = _parse_whole(text)
    # Two separate section transitions → two deltas.
    assert len(deltas) == 2
    assert deltas[0] == ("Note", "first")
    assert deltas[1] == ("Note", "second")
    # reconstruct groups consecutive same-title runs.
    assert reconstruct(deltas) == [("Note", "firstsecond")]
    # The byte-offset invariant still holds for repeated titles.
    assert reconstruct(_parse_split(text, len(text) // 2)) == [("Note", "firstsecond")]
