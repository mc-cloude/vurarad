"""Unit tests for the OCR engines — genuine localisation, not transcription.

The :class:`ThresholdOcrEngine` is the dependency-free detector used in dev/CI
and under the validation corpus.  It performs *genuine* region localisation
(luminance threshold + connected components) but never transcribes, so its
regions route downstream to review (fail-closed).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from app.services.deid.ocr import (
    DEFAULT_INK_THRESHOLD,
    BBox,
    OcrRegion,
    ThresholdOcrEngine,
    detect_text_regions,
)

# A small dark-on-light image: a solid ink rectangle in the top-left.
_INK_IMAGE = np.full((32, 64), 220, dtype=np.uint8)
_INK_IMAGE[2:8, 4:20] = 30  # one dark blob


# ---------------------------------------------------------------------------
# BBox
# ---------------------------------------------------------------------------
class TestBBox:
    def test_width_height_area(self) -> None:
        b = BBox(1, 2, 6, 10)
        assert b.width == 5
        assert b.height == 8
        assert b.area == 40

    def test_zero_area_when_degenerate(self) -> None:
        assert BBox(5, 5, 5, 5).area == 0

    def test_iou_identical(self) -> None:
        b = BBox(0, 0, 10, 10)
        assert b.iou(b) == 1.0

    def test_iou_disjoint(self) -> None:
        assert BBox(0, 0, 5, 5).iou(BBox(10, 10, 15, 15)) == 0.0

    def test_iou_partial(self) -> None:
        a = BBox(0, 0, 10, 10)
        b = BBox(5, 5, 15, 15)
        # intersection 25, union 175
        assert a.iou(b) == pytest.approx(25 / 175)

    def test_frozen(self) -> None:
        b = BBox(0, 0, 1, 1)
        with pytest.raises(FrozenInstanceError):
            b.x0 = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ThresholdOcrEngine — genuine localisation
# ---------------------------------------------------------------------------
class TestThresholdOcrEngine:
    def test_version_is_pinned(self) -> None:
        assert ThresholdOcrEngine().version == "threshold-v1"

    def test_detects_ink_region(self) -> None:
        regions = ThresholdOcrEngine().detect(_INK_IMAGE)
        assert len(regions) >= 1
        # The detected box overlaps the ink rectangle.
        ink = BBox(4, 2, 20, 8)
        assert any(r.bbox.iou(ink) > 0.4 for r in regions)

    def test_detect_returns_empty_text(self) -> None:
        # No recogniser — text is always empty → downstream treats as
        # unclassified → review (fail-closed).
        regions = ThresholdOcrEngine().detect(_INK_IMAGE)
        for r in regions:
            assert r.text == ""

    def test_blank_image_yields_no_regions(self) -> None:
        blank = np.full((32, 64), 220, dtype=np.uint8)
        assert ThresholdOcrEngine().detect(blank) == []

    def test_min_area_filters_noise(self) -> None:
        img = np.full((32, 64), 220, dtype=np.uint8)
        img[5, 5] = 30  # a single dark pixel (area 1) below min_area
        regions = ThresholdOcrEngine(min_area=8).detect(img)
        assert regions == []

    def test_ink_threshold_controls_detection(self) -> None:
        # A mid-gray blob (value 150) is ink only when the threshold is high.
        img = np.full((32, 64), 220, dtype=np.uint8)
        img[2:10, 4:20] = 150
        assert ThresholdOcrEngine(ink_threshold=128).detect(img) == []
        assert len(ThresholdOcrEngine(ink_threshold=200).detect(img)) >= 1


# ---------------------------------------------------------------------------
# detect_text_regions — the free function
# ---------------------------------------------------------------------------
class TestDetectTextRegions:
    def test_finds_blob_bbox(self) -> None:
        boxes = detect_text_regions(_INK_IMAGE)
        assert len(boxes) >= 1
        ink = BBox(4, 2, 20, 8)
        assert any(b.iou(ink) > 0.4 for b in boxes)

    def test_handles_grayscale_2d(self) -> None:
        boxes = detect_text_regions(np.full((16, 16), 220, dtype=np.uint8))
        assert boxes == []

    def test_handles_rgb_3d(self) -> None:
        img = np.full((32, 64, 3), 220, dtype=np.uint8)
        img[2:8, 4:20, :] = 30
        boxes = detect_text_regions(img)
        assert len(boxes) >= 1


# ---------------------------------------------------------------------------
# OcrRegion
# ---------------------------------------------------------------------------
def test_ocr_region_defaults() -> None:
    r = OcrRegion(BBox(0, 0, 5, 5), "hi", 0.9)
    assert r.frame_index == 0
    assert r.confidence == 0.9


def test_default_ink_threshold_is_128() -> None:
    assert DEFAULT_INK_THRESHOLD == 128
