"""Segmentation package — registry loader and resolver (§3.21.4)."""

from app.segmentation.registry import (
    RegistryEntry,
    RegistryEntryState,
    SegmentationRegistry,
)

__all__ = [
    "RegistryEntry",
    "RegistryEntryState",
    "SegmentationRegistry",
]
