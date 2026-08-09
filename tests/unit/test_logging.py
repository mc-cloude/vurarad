"""Structured logging — PhiRedactionFilter blocks PHI and reports the violation."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from app.core.logging import JsonFormatter, PhiRedactionFilter, setup_logging


def _record(msg: str, *, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="vurarad.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# PhiRedactionFilter (criterion #13) — drops patient_name + reports violation
# ---------------------------------------------------------------------------
def test_phi_filter_drops_patient_name_and_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.cloud.logging as gcl

    mock_client = MagicMock()
    mock_logger = MagicMock()
    mock_client.logger.return_value = mock_logger
    monkeypatch.setattr(gcl, "Client", lambda: mock_client)

    flt = PhiRedactionFilter()
    record = _record("patient_name=John Doe")
    assert flt.filter(record) is False  # record dropped (fail-closed)

    mock_client.logger.assert_called_once_with("vurarad-policy")
    payload = json.loads(mock_logger.log_text.call_args.args[0])
    assert payload["event"] == "LOGGING_POLICY_VIOLATION"
    assert payload["field"] == "patient_name"


def test_phi_filter_drops_camel_case_patient_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.cloud.logging as gcl

    monkeypatch.setattr(gcl, "Client", lambda: MagicMock())
    flt = PhiRedactionFilter()
    assert flt.filter(_record("patientName appears here")) is False


def test_phi_filter_drops_email_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.cloud.logging as gcl

    monkeypatch.setattr(gcl, "Client", lambda: MagicMock())
    flt = PhiRedactionFilter()
    assert flt.filter(_record("user email=a@b.c")) is False


def test_phi_filter_allows_clean_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.cloud.logging as gcl

    mock_client = MagicMock()
    monkeypatch.setattr(gcl, "Client", lambda: mock_client)
    flt = PhiRedactionFilter()
    assert flt.filter(_record("study S-123 was signed")) is True
    mock_client.logger.assert_not_called()


def test_phi_filter_fail_closed_when_client_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the policy client cannot be built, the record is STILL dropped."""
    import google.cloud.logging as gcl

    def _boom() -> None:
        raise RuntimeError("no creds")

    monkeypatch.setattr(gcl, "Client", _boom)
    flt = PhiRedactionFilter()
    assert flt.filter(_record("patient_name=John")) is False


# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------
def test_json_formatter_basic() -> None:
    formatter = JsonFormatter()
    out = formatter.format(_record("hello world"))
    obj = json.loads(out)
    assert obj["message"] == "hello world"
    assert obj["level"] == "INFO"
    assert obj["logger"] == "vurarad.test"
    assert "timestamp" in obj


def test_json_formatter_with_request_id_and_actor() -> None:
    formatter = JsonFormatter()
    record = _record("event")
    record.request_id = "req-1"  # type: ignore[attr-defined]
    record.actor_uid = "u-1"  # type: ignore[attr-defined]
    obj = json.loads(formatter.format(record))
    assert obj["requestId"] == "req-1"
    assert obj["actor"] == "u-1"


def test_json_formatter_includes_exception() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed", level=logging.ERROR)
        record.exc_info = sys.exc_info()  # type: ignore[attr-defined]
    obj = json.loads(formatter.format(record))
    assert "exception" in obj
    assert "ValueError" in obj["exception"]


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------
def test_setup_logging_adds_handler_and_phi_filter(
    reset_root_logging: logging.Logger,
) -> None:
    setup_logging(level="WARNING")
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    assert any(isinstance(f, PhiRedactionFilter) for f in root.filters)


def test_setup_logging_text_format(
    reset_root_logging: logging.Logger,
) -> None:
    setup_logging(level="INFO", json_format=False)
    root = logging.getLogger()
    handler = next(h for h in root.handlers if isinstance(h, logging.StreamHandler))
    # Non-JSON format uses the default Formatter (not JsonFormatter).
    assert not isinstance(handler.formatter, JsonFormatter)


@pytest.fixture(autouse=True)
def _isolate_root_logger(reset_root_logging: logging.Logger) -> None:
    """Ensure each test starts from a clean root logger."""
    logging.getLogger().handlers.clear()
    logging.getLogger().filters.clear()
    yield
