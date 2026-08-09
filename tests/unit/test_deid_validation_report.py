"""Unit tests for the de-ID validation report tool (criterion 9)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services.deid.ocr import ThresholdOcrEngine
from app.services.deid.phi_ner import OPENMED_MODEL_ID, OPENMED_MODEL_REVISION
from app.services.deid.tags import TAG_PROFILE_VERSION
from app.tools import deid_validation_report as tool


# ---------------------------------------------------------------------------
# generate_report — content
# ---------------------------------------------------------------------------
def test_report_contains_tag_profile_version() -> None:
    assert TAG_PROFILE_VERSION in tool.generate_report()


def test_report_contains_ocr_engine_version() -> None:
    assert ThresholdOcrEngine().version in tool.generate_report()


def test_report_contains_phi_model_identity() -> None:
    report = tool.generate_report()
    assert OPENMED_MODEL_ID in report
    assert OPENMED_MODEL_REVISION in report


def test_report_contains_all_floor_modalities() -> None:
    report = tool.generate_report()
    for modality in ["US", "SC", "OT", "XC", "CR", "DX", "MG", "CT", "MR"]:
        assert f"| {modality} |" in report


def test_report_contains_required_sections() -> None:
    report = tool.generate_report()
    assert "## Configuration" in report
    assert "## Per-modality PHI recall" in report
    assert "## Coverage" in report
    assert "## Residual-risk assessment" in report


def test_report_contains_coverage_and_unvalidated_sections() -> None:
    report = tool.generate_report()
    assert "Validated (modality, manufacturer) pairs" in report
    assert "Unvalidated sources" in report
    assert "DEID_UNVALIDATED_SOURCE" in report


def test_report_all_modalities_pass() -> None:
    """Every measured modality must show PASS (the build-failing floors are met)."""
    report = tool.generate_report()
    assert "| FAIL |" not in report
    assert report.count("| PASS |") == 9


# ---------------------------------------------------------------------------
# generate_report — determinism
# ---------------------------------------------------------------------------
def test_report_is_deterministic() -> None:
    assert tool.generate_report() == tool.generate_report()


# ---------------------------------------------------------------------------
# --check CLI behaviour
# ---------------------------------------------------------------------------
def test_check_passes_when_up_to_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "deid-validation.md"
    docs.write_text(tool.generate_report())
    monkeypatch.setattr(tool, "DOCS_PATH", docs)
    monkeypatch.setattr(sys, "argv", ["deid_validation_report", "--check"])
    tool.main()  # returns normally — no SystemExit


def test_check_fails_when_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "deid-validation.md"
    docs.write_text("stale content\n")
    monkeypatch.setattr(tool, "DOCS_PATH", docs)
    monkeypatch.setattr(sys, "argv", ["deid_validation_report", "--check"])
    with pytest.raises(SystemExit) as exc_info:
        tool.main()
    assert exc_info.value.code == 1


def test_check_fails_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "nonexistent.md"
    monkeypatch.setattr(tool, "DOCS_PATH", docs)
    monkeypatch.setattr(sys, "argv", ["deid_validation_report", "--check"])
    with pytest.raises(SystemExit) as exc_info:
        tool.main()
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# write CLI behaviour
# ---------------------------------------------------------------------------
def test_write_creates_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "subdir" / "deid-validation.md"
    monkeypatch.setattr(tool, "DOCS_PATH", docs)
    monkeypatch.setattr(sys, "argv", ["deid_validation_report"])
    tool.main()
    assert docs.exists()
    assert docs.read_text() == tool.generate_report()
