#!/usr/bin/env python3
"""Regenerate ``docs/deid-validation.md`` — the measured de-ID validation report.

Run::

    python -m app.tools.deid_validation_report           # write docs/deid-validation.md
    python -m app.tools.deid_validation_report --check    # exit non-zero if docs are stale

The report is the release artefact for acceptance criterion 9 (WP11).  It records
the tag profile version, OCR engine version, PHI model identity, per-modality PHI
recall measured against the synthetic corpus, the ``(modality, manufacturer)``
coverage section (pairs **not** validated), and a residual-risk assessment.

The output is deterministic — no timestamps — so ``--check`` is stable across
runs: the report changes only when the code (versions, corpus, floors, or
detection logic) changes.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

# Settings() is constructed at module-import time in app.core.config and requires
# several env vars with no defaults.  Set them before the first app import so the
# tool is self-contained in dev/CI (mirrors tests/conftest.py).  setdefault keeps
# any real env values already present.
os.environ.setdefault("GCP_PROJECT_ID", "vurarad-report")
os.environ.setdefault("GCP_REGION", "us-central1")
os.environ.setdefault("PIXEL_BUCKET_NAME", "vurarad-report-pixels")
os.environ.setdefault("AUDIT_BUCKET_NAME", "vurarad-report-audit")
os.environ.setdefault("FIREBASE_PROJECT_ID", "vurarad-report")
os.environ.pop("FIRESTORE_EMULATOR_HOST", None)

from app.core.config import settings
from app.services.deid.ocr import BBox, ThresholdOcrEngine
from app.services.deid.phi_ner import OPENMED_MODEL_ID, OPENMED_MODEL_REVISION
from app.services.deid.tags import TAG_PROFILE_VERSION
from tests.data.deid.corpus import covered_pairs, render_corpus

DOCS_PATH = Path(__file__).resolve().parents[2] / "docs" / "deid-validation.md"

# Modalities in floor-group order (matches the validation test).
_MODALITIES = ["US", "SC", "OT", "XC", "CR", "DX", "MG", "CT", "MR"]

_FLOOR_GROUPS: dict[float, frozenset[str]] = {
    settings.deid_recall_floor_us_sc_ot_xc: frozenset({"US", "SC", "OT", "XC"}),
    settings.deid_recall_floor_cr_dx_mg: frozenset({"CR", "DX", "MG"}),
    settings.deid_recall_floor_ct_mr: frozenset({"CT", "MR"}),
}


def _floor_for(modality: str) -> float:
    for floor, modalities in _FLOOR_GROUPS.items():
        if modality in modalities:
            return floor
    raise KeyError(f"no recall floor defined for modality {modality}")


def _covers(det: BBox, gt: BBox, *, threshold: float = 0.5) -> bool:
    """A ground-truth region is *recalled* when a detected box covers >= 50% of it."""
    ix0, iy0 = max(det.x0, gt.x0), max(det.y0, gt.y0)
    ix1, iy1 = min(det.x1, gt.x1), min(det.y1, gt.y1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    return inter / max(1, gt.area) >= threshold


def _modality_recall() -> dict[str, tuple[int, int, float]]:
    """Render the corpus, run the detector, return per-modality ``(recalled, total, recall)``."""
    engine = ThresholdOcrEngine()
    recalled: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for img in render_corpus():
        detected = engine.detect(img.array)
        for gt in img.regions:
            if not gt.is_phi:
                continue
            total[img.modality] += 1
            if any(_covers(d.bbox, gt.bbox) for d in detected):
                recalled[img.modality] += 1
    return {
        mod: (recalled[mod], total[mod], recalled[mod] / total[mod] if total[mod] else 1.0)
        for mod in total
    }


def generate_report() -> str:
    """Produce the full markdown validation report as a deterministic string."""
    recall = _modality_recall()
    pairs = sorted(covered_pairs())

    lines: list[str] = []
    lines.append("# De-identification Validation Report")
    lines.append("")
    lines.append(
        "This report is the measured validation artefact for the de-identification"
        " pipeline (WP11, §3.17).  It is regenerated and committed on every release."
        "  Recall on PHI is the reported metric — a missed identifier is a breach;"
        " an over-redaction is an annoyance."
    )
    lines.append("")

    # -- Configuration -------------------------------------------------------
    lines.append("## Configuration")
    lines.append("")
    lines.append("| Property | Value |")
    lines.append("|---|---|")
    lines.append(f"| Tag profile | `{TAG_PROFILE_VERSION}` |")
    lines.append(f"| OCR engine (default) | `threshold` (`{ThresholdOcrEngine().version}`) |")
    lines.append(f"| PHI classifier (default) | `{settings.deid_phi_classifier}` |")
    lines.append(f"| PHI model ID | `{OPENMED_MODEL_ID}` |")
    lines.append(f"| PHI model revision | `{OPENMED_MODEL_REVISION}` |")
    lines.append(f"| Confidence threshold | {settings.deid_confidence_threshold} |")
    lines.append(f"| Require pixel pass | {settings.deid_require_pixel_pass} |")
    lines.append("")

    # -- Per-modality recall -------------------------------------------------
    lines.append("## Per-modality PHI recall")
    lines.append("")
    lines.append(
        "Measured against the synthetic overlay corpus"
        " (``tests/data/deid/``) using the default"
        " ``ThresholdOcrEngine``.  A ground-truth PHI region is *recalled* when a"
        " detected box covers ≥ 50% of its area.  Recall must meet the per-modality"
        " floor or the build fails (``tests/validation/test_deid_recall.py``)."
    )
    lines.append("")
    lines.append("| Modality | Floor | Recalled | Total | Recall | Status |")
    lines.append("|---|---|---|---|---|---|")
    for modality in _MODALITIES:
        recalled_n, total_n, value = recall[modality]
        floor = _floor_for(modality)
        status = "PASS" if value >= floor else "FAIL"
        lines.append(
            f"| {modality} | {floor:.2f} | {recalled_n} | {total_n} | {value:.4f} | {status} |"
        )
    lines.append("")

    # -- Coverage ------------------------------------------------------------
    lines.append("## Coverage — validated and unvalidated sources")
    lines.append("")
    lines.append("### Validated (modality, manufacturer) pairs")
    lines.append("")
    lines.append(
        "The following synthetic ``(modality, manufacturer)`` pairs are exercised"
        " by the corpus and measured against the recall floors above:"
    )
    lines.append("")
    lines.append("| Modality | Manufacturer |")
    lines.append("|---|---|")
    for modality, manufacturer in pairs:
        lines.append(f"| {modality} | {manufacturer} |")
    lines.append("")

    lines.append("### Unvalidated sources")
    lines.append("")
    lines.append(
        "Every real-world ``(modality, manufacturer)`` pair **not** listed above is"
        " unvalidated.  An unvalidated source emits a ``DEID_UNVALIDATED_SOURCE``"
        " audit event and forces **all** detected text regions to human review"
        " regardless of confidence (criterion 10).  A pair is only removed from the"
        " unvalidated set after it has been measured against the recall floors and"
        " added to ``DEID_VALIDATED_SOURCES``."
    )
    lines.append("")

    # -- Residual-risk assessment -------------------------------------------
    lines.append("## Residual-risk assessment")
    lines.append("")
    lines.append(
        "1. **Synthetic corpus limitation.**  The corpus uses PIL-rendered overlays"
        " (DejaVuSans-Bold, dark-on-light) that model the *shape* of burned-in PHI"
        " but not the full diversity of real scanner overlays (anti-aliased text,"
        " semi-transparent banners, multi-column layouts).  Real-world recall may"
        " differ; per-manufacturer measurement is required before clearing a source."
    )
    lines.append(
        "2. **Default engines.**  The default OCR engine (``threshold``) and PHI"
        " classifier (``deterministic``) are dependency-free stand-ins for dev/CI."
        "  Production deployments must configure ``DEID_OCR_ENGINE=paddle`` (or"
        " ``tesseract``) and ``DEID_PHI_CLASSIFIER=openmed`` and re-measure recall"
        " against real-world overlays."
    )
    lines.append(
        "3. **Unvalidated sources.**  Until a ``(modality, manufacturer)`` pair is"
        " measured and added to ``DEID_VALIDATED_SOURCES``, all detected regions"
        " route to human review.  This is fail-closed: no automated redaction"
        " occurs for an unvalidated source."
    )
    lines.append(
        "4. **Clinical-annotation preservation.**  The clinical-annotation allowlist"
        " (``LEFT``, ``RIGHT``, ``SUPINE``, ``PRONE``, laterality, measurements, scale"
        " bars) is preserved by the OpenMed classifier path and destroyed by a"
        " rules-only redactor.  The deterministic stand-in classifies clinical"
        " annotations correctly for the synthetic corpus; production must verify"
        " the OpenMed model preserves real-world clinical annotations."
    )
    lines.append(
        "5. **Tag scrubbing is not sufficient.**  Tag scrubbing (PS3.15 Annex E)"
        " cannot touch burned-in pixel text.  The pixel pass (OCR + classification)"
        " runs even when tag scrubbing reports no PHI tags (criterion 1)."
    )
    lines.append(
        "6. **Fail-closed review.**  A classifier error, an OCR-text-with-no-"
        "classification case, or a low-confidence region routes to **review**, never"
        " to keep (criterion 5).  ``DEID_REQUIRE_PIXEL_PASS`` cannot be ``False`` in"
        " production (criterion 6)."
    )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate the de-ID validation report (docs/deid-validation.md).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if docs/deid-validation.md is stale.",
    )
    args = parser.parse_args()

    report = generate_report()

    if args.check:
        if not DOCS_PATH.exists():
            print(
                f"{DOCS_PATH} does not exist — run without --check to generate it.",
                file=sys.stderr,
            )
            sys.exit(1)
        existing = DOCS_PATH.read_text()
        if existing != report:
            print(
                f"{DOCS_PATH} is stale — run without --check to regenerate it.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"{DOCS_PATH} is up to date.")
        return

    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(report)
    print(f"wrote {DOCS_PATH}")


if __name__ == "__main__":
    main()
