"""Incremental section-stream parser — byte-offset-safe JSON-section scanner.

The Gemini model is prompted to emit a report as a sequence of sections, each
introduced by a ``<<SECTION:Title>>`` marker, with the section body as the text
between one marker and the next::

    <<SECTION:Findings>>
    The liver is normal in size...
    <<SECTION:Impression>>
    No acute findings.

:class:`SectionStreamParser` consumes the streamed text incrementally via
:py:meth:`feed` and emits ``(section, fragment)`` deltas — one per chunk of
confirmed body text.  It is a **state machine with look-back**: when a chunk
ends with a partial marker prefix (e.g. ``"<<SECTION"``), that tail is held
back rather than emitted as a body fragment, so a marker split across two
``feed`` calls is never leaked as text.

The central invariant (enforced by ``test_byte_offset_fuzzing``): **feeding the
whole text in one call produces the same concatenated per-section output as
feeding it split at any byte/character offset.**  Every body character is
emitted exactly once; every marker character is consumed as a section
transition and never emitted.

This parser only ever sees the model's text.  It never touches
``chunk.parsed`` — the streaming path in :class:`GeminiService` feeds
``chunk.text`` here, never the SDK's parsed structured output.
"""

from __future__ import annotations

from collections.abc import Iterator

# The marker that introduces a section.  Once this prefix is seen, the parser
# scans forward for the closing ``>>`` and reads the title between them.
_SENTINEL = "<<SECTION:"
_SENTINEL_LEN = len(_SENTINEL)  # 10
_CLOSE = ">>"
# The longest possible partial-sentinel prefix is ``_SENTINEL_LEN - 1`` chars
# (the sentinel minus its final ``:``).  Holding back that many chars guarantees
# a partial marker straddling a feed boundary is never emitted as a fragment.
_TAIL_KEEP = _SENTINEL_LEN - 1


class SectionStreamParser:
    """Incremental, byte-offset-safe scanner for ``<<SECTION:Title>>`` streams."""

    def __init__(self) -> None:
        self._section: str | None = None  # current section title; None = pre-first-marker
        self._buf: str = ""

    # -- public API ----------------------------------------------------------
    def feed(self, text: str) -> Iterator[tuple[str, str]]:
        """Consume one chunk of streamed text; yield ``(section, fragment)`` deltas.

        Fragments are only the *confirmed-safe* body text — any tail that could
        be the start of a marker is held back until the next call (or
        :py:meth:`flush`).
        """
        if not text:
            return
        self._buf += text
        yield from self._drain()

    def flush(self) -> Iterator[tuple[str, str]]:
        """Emit any held-back body for the current section; reset the parser.

        Must be called once after the last ``feed`` so the final section's
        trailing text (held back as a potential partial marker) is not lost.
        """
        if self._section is not None and self._buf:
            yield self._section, self._buf
        self._buf = ""
        self._section = None

    @property
    def current_section(self) -> str | None:
        """The section the parser is currently inside (``None`` before the first marker)."""
        return self._section

    # -- internals -----------------------------------------------------------
    def _drain(self) -> Iterator[tuple[str, str]]:
        """Process ``_buf`` as far as possible, yielding confirmed body fragments.

        Always leaves at most ``_TAIL_KEEP`` chars in ``_buf`` when no full
        marker is present, so a marker straddling a feed boundary is buffered
        rather than emitted.
        """
        while True:
            if self._section is None:
                # Looking for the first (or next) marker.  Text before it is
                # preamble / inter-marker whitespace and is discarded, never
                # emitted as a body fragment.
                idx = self._buf.find(_SENTINEL)
                if idx == -1:
                    # No marker: discard everything except a tail that could be
                    # the start of a partial sentinel.
                    keep = min(len(self._buf), _TAIL_KEEP)
                    self._buf = self._buf[len(self._buf) - keep :] if keep else ""
                    return
                # Drop the preamble up to the marker.
                self._buf = self._buf[idx:]
                # ``_buf`` now starts with the sentinel; find the closing ``>>``.
                close = self._buf.find(_CLOSE, _SENTINEL_LEN)
                if close == -1:
                    # Incomplete marker — wait for more text.
                    return
                title = self._buf[_SENTINEL_LEN:close]
                self._section = title
                self._buf = self._buf[close + len(_CLOSE) :]
                # Loop to process the body that follows the marker.
                continue

            # Inside a section — look for the next marker.
            idx = self._buf.find(_SENTINEL)
            if idx == -1:
                # No marker yet: emit the safe prefix, hold the ambiguous tail.
                safe_end = len(self._buf) - _TAIL_KEEP
                if safe_end > 0:
                    yield self._section, self._buf[:safe_end]
                    self._buf = self._buf[safe_end:]
                return
            # A full marker is present — all of ``_buf[:idx]`` is confirmed body.
            if idx > 0:
                yield self._section, self._buf[:idx]
            self._buf = self._buf[idx:]
            self._section = None  # re-parse the marker on the next loop turn
            continue


def reconstruct(deltas: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Group consecutive same-section deltas into ``(section, full_body)`` runs.

    Used by the byte-offset fuzzing test to compare split-fed output against
    whole-fed output: two delta streams are equivalent iff their reconstructions
    are equal.
    """
    runs: list[tuple[str, str]] = []
    for section, fragment in deltas:
        if runs and runs[-1][0] == section:
            prev_section, prev_body = runs[-1]
            runs[-1] = (prev_section, prev_body + fragment)
        else:
            runs.append((section, fragment))
    return runs


__all__ = ["SectionStreamParser", "reconstruct"]
