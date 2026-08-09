"""Build-failing per-modality PHI recall floors (§3.17.2).

This is the only place in the product that owes a *real measurement*.  Recall on
PHI is the reported metric — a missed identifier is a breach; an over-redaction
is an annoyance.  The synthetic corpus (``tests/data/deid/``) supplies images
with known PHI locations; the :class:`ThresholdOcrEngine` performs genuine
region localisation (luminance threshold + connected components); the manifest
supplies the ground-truth labels.  Recall = detected PHI regions / total PHI
regions, per modality, and must meet the per-modality floor or the build fails.

Floors (from config):
* US, SC, OT, XC — 0.99  (worst overlay burden)
* CR, DX, MG    — 0.98  (common annotation burn-in)
* CT, MR        — 0.97  (burned-in text rarer)
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from app.core.config import settings
from app.services.deid.ocr import BBox, ThresholdOcrEngine
from tests.data.deid.corpus import render_corpus

# ---------------------------------------------------------------------------
# Per-modality recall floors (sourced from config so they stay in sync)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Matching — a ground-truth PHI region is "recalled" when a detected box covers
# >= 50% of its area.  The threshold engine splits/merges connected components,
# so pure IoU is too strict; area-coverage is the right criterion for detection.
# ---------------------------------------------------------------------------
def _covers(det: BBox, gt: BBox, *, threshold: float = 0.5) -> bool:
    ix0, iy0 = max(det.x0, gt.x0), max(det.y0, gt.y0)
    ix1, iy1 = min(det.x1, gt.x1), min(det.y1, gt.y1)
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    return inter / max(1, gt.area) >= threshold


def _modality_recall() -> dict[str, tuple[int, int, float]]:
    """Render the corpus, run the detector, and return per-modality recall."""
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


# ---------------------------------------------------------------------------
# Tests — one parametrised case per modality; any sub-floor modality fails build
# ---------------------------------------------------------------------------
_MODALITIES = ["US", "SC", "OT", "XC", "CR", "DX", "MG", "CT", "MR"]


@pytest.mark.parametrize("modality", _MODALITIES)
def test_phi_recall_meets_floor(modality: str) -> None:
    recalled, total, recall = _modality_recall()[modality]
    floor = _floor_for(modality)
    assert recall >= floor, (
        f"PHI recall for {modality} ({recall:.4f}, {recalled}/{total}) "
        f"is below the build-failing floor {floor}"
    )


def test_corpus_covers_every_floored_modality() -> None:
    """The corpus must exercise every modality that has a recall floor."""
    measured = set(_modality_recall())
    for modalities in _FLOOR_GROUPS.values():
        assert modalities <= measured, (
            f"corpus missing modalities with a recall floor: {modalities - measured}"
        )


def test_corpus_has_phi_regions_in_every_modality() -> None:
    """Every measured modality must have at least one PHI ground-truth region."""
    for modality, (_recalled, total, _recall) in _modality_recall().items():
        assert total > 0, f"modality {modality} has no PHI ground-truth regions"
