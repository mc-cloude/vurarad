"""Report state-machine invariants (WP5 — acceptance criterion 1).

Only ``DRAFT → PENDING_SIGNATURE → SIGNED`` is legal.  Every other transition
—including ``DRAFT → SIGNED`` and any transition out of ``SIGNED``— raises
``InvalidReportTransitionError`` (409 INVALID_REPORT_TRANSITION).
"""

from __future__ import annotations

import pytest

from app.core.errors import InvalidReportTransitionError
from app.models.report import ReportStatus
from app.services.report_service import (
    _ALLOWED_REPORT_TRANSITIONS,
    validate_report_transition,
)

_STATUSES = [ReportStatus.DRAFT, ReportStatus.PENDING_SIGNATURE, ReportStatus.SIGNED]

# The complete legal-transition table (criterion 1).
_LEGAL: set[tuple[ReportStatus, ReportStatus]] = {
    (ReportStatus.DRAFT, ReportStatus.PENDING_SIGNATURE),
    (ReportStatus.PENDING_SIGNATURE, ReportStatus.SIGNED),
}


def test_legal_transitions_table_is_exactly_two_edges() -> None:
    """The allowed-transition graph contains exactly the two legal edges."""
    expected = {
        ReportStatus.DRAFT: frozenset({ReportStatus.PENDING_SIGNATURE}),
        ReportStatus.PENDING_SIGNATURE: frozenset({ReportStatus.SIGNED}),
        ReportStatus.SIGNED: frozenset(),
    }
    assert expected == _ALLOWED_REPORT_TRANSITIONS


@pytest.mark.parametrize("current,target", sorted(_LEGAL))
def test_legal_transitions_pass(current: ReportStatus, target: ReportStatus) -> None:
    validate_report_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    "current,target",
    [(c, t) for c in _STATUSES for t in _STATUSES if (c, t) not in _LEGAL],
)
def test_illegal_transitions_raise_409(current: ReportStatus, target: ReportStatus) -> None:
    with pytest.raises(InvalidReportTransitionError) as exc_info:
        validate_report_transition(current, target)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code.value == "INVALID_REPORT_TRANSITION"


def test_draft_to_signed_is_forbidden() -> None:
    """DRAFT → SIGNED is explicitly forbidden (criterion 1)."""
    with pytest.raises(InvalidReportTransitionError):
        validate_report_transition(ReportStatus.DRAFT, ReportStatus.SIGNED)


def test_signed_is_terminal() -> None:
    """No transition out of SIGNED is allowed."""
    for target in _STATUSES:
        with pytest.raises(InvalidReportTransitionError):
            validate_report_transition(ReportStatus.SIGNED, target)


def test_no_self_transitions() -> None:
    """A status cannot transition to itself (no DRAFT→DRAFT etc.)."""
    for status in _STATUSES:
        with pytest.raises(InvalidReportTransitionError):
            validate_report_transition(status, status)


def test_pending_to_draft_is_forbidden() -> None:
    """PENDING_SIGNATURE → DRAFT is a backwards transition and is forbidden."""
    with pytest.raises(InvalidReportTransitionError):
        validate_report_transition(ReportStatus.PENDING_SIGNATURE, ReportStatus.DRAFT)
