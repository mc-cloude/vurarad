"""Integration-test-local pytest configuration.

Registers the ``slow`` marker used by the ingest benchmark without touching the
shared ``pyproject.toml`` (parallel-work safe).
"""

from __future__ import annotations


def pytest_configure(config: object) -> None:
    # Registers the marker so ``-m slow`` does not emit PytestUnknownMarkWarning.
    config.addinivalue_line(  # type: ignore[attr-defined]
        "markers", "slow: slow benchmark / large-dataset integration tests"
    )
