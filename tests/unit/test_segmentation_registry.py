"""Unit tests for the segmentation registry (§3.21.4 — criterion 2, 4).

Covers:
- ``own_segmentation_categories()`` == {ANATOMICAL_MEASUREMENT,
  PRIOR_COMPARISON_CHANGE} (criterion 2 — never EXTERNAL_DETECTION).
- ``resolve()`` returns ``NO_MODEL_AVAILABLE`` for US and CR CHEST.
- CT CHEST/ABDOMEN available; MR BRAIN available + requires_gpu.
- Fallback to wildcard specialisation.
"""

from __future__ import annotations

from app.segmentation.registry import (
    RegistryEntryState,
    SegmentationRegistry,
)


class TestOwnSegmentationCategories:
    """Criterion 2 — our segmentation produces only two categories."""

    def test_category_set(self) -> None:
        reg = SegmentationRegistry()
        cats = reg.own_segmentation_categories()
        assert cats == frozenset({"ANATOMICAL_MEASUREMENT", "PRIOR_COMPARISON_CHANGE"})

    def test_no_external_detection(self) -> None:
        reg = SegmentationRegistry()
        cats = reg.own_segmentation_categories()
        assert "EXTERNAL_DETECTION" not in cats


class TestResolve:
    """Criterion 4 — resolve returns honest states."""

    def test_ct_chest_available(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("CT", "CHEST")
        assert entry.state == RegistryEntryState.AVAILABLE
        assert entry.bundle_id is not None

    def test_ct_abdomen_available(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("CT", "ABDOMEN")
        assert entry.state == RegistryEntryState.AVAILABLE
        assert entry.bundle_id is not None

    def test_mr_brain_available_and_requires_gpu(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("MR", "BRAIN")
        assert entry.state == RegistryEntryState.AVAILABLE
        assert entry.requires_gpu is True

    def test_us_returns_no_model_available(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("US", "ABDOMEN")
        assert entry.state == RegistryEntryState.NO_MODEL_AVAILABLE
        assert entry.bundle_id is None

    def test_us_wildcard_body_part(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("US", "BREAST")
        assert entry.state == RegistryEntryState.NO_MODEL_AVAILABLE

    def test_cr_chest_returns_no_model_available(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("CR", "CHEST")
        assert entry.state == RegistryEntryState.NO_MODEL_AVAILABLE
        assert entry.bundle_id is None

    def test_unknown_modality_returns_no_model_available(self) -> None:
        reg = SegmentationRegistry()
        entry = reg.resolve("DX", "CHEST")
        assert entry.state == RegistryEntryState.NO_MODEL_AVAILABLE

    def test_wildcard_specialisation_fallback(self) -> None:
        """resolve() with a specific specialisation falls back to '*'."""
        reg = SegmentationRegistry()
        entry = reg.resolve("CT", "CHEST", specialisation="THORACIC")
        # Falls back to the '*' entry.
        assert entry.state == RegistryEntryState.AVAILABLE
        assert entry.specialisation == "*"

    def test_entries_count(self) -> None:
        reg = SegmentationRegistry()
        entries = reg.entries()
        # CT CHEST, CT ABDOMEN, MR BRAIN, CR CHEST, US *
        assert len(entries) == 5
