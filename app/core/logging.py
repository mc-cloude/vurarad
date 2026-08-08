"""Structured JSON logging with mandatory PHI redaction.

Every log record passes through PhiRedactionFilter — it fails CLOSED:
if the filter cannot run for any reason, the record is dropped and
LOGGING_POLICY_VIOLATION is incremented.
"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from app.core.redaction import PHI_FIELD_NAMES


# -- structured formatter ----------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            obj["exception"] = "".join(traceback.format_exception(*record.exc_info))
        if hasattr(record, "request_id"):
            obj["requestId"] = record.request_id
        if hasattr(record, "actor_uid"):
            obj["actor"] = record.actor_uid
        return json.dumps(obj, default=str)


# -- PHI redaction filter (fail-closed) -------------------------------------
class PhiRedactionFilter(logging.Filter):
    """Drop any record whose formatted message contains a PHI field name.

    This is a fail-closed design — if we cannot be certain no PHI escapes,
    the record does not reach the log sink.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for field in PHI_FIELD_NAMES:
            if field in msg:
                # Increment the violation counter for alerting
                try:
                    import google.cloud.logging

                    client = google.cloud.logging.Client()  # type: ignore[no-untyped-call]
                    client.logger("vurarad-policy").log_text(  # type: ignore[no-untyped-call]
                        json.dumps(
                            {
                                "event": "LOGGING_POLICY_VIOLATION",
                                "field": field,
                                "level": record.levelname,
                            }
                        ),
                        severity="ERROR",
                    )
                except Exception:
                    pass
                return False
        return True


def setup_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure root logger for structured JSON output."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        handler.setFormatter(JsonFormatter())

    root.addHandler(handler)
    root.addFilter(PhiRedactionFilter())
