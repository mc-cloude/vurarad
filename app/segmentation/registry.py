"""Segmentation registry — data-file-driven model lookup (§3.21.4).

Loads ``app/segmentation/registry.yaml`` at construction time and validates it.
``resolve()`` maps ``(modality, bodyPart, specialisation)`` to a
:class:`RegistryEntry`, falling back to the ``"*"`` wildcard and finally to
``NO_MODEL_AVAILABLE`` when no entry matches — so the default is honest.

Our own segmentation produces only the categories in ``own_segmentation_categories()``
— ``{ANATOMICAL_MEASUREMENT, PRIOR_COMPARISON_CHANGE}`` — never
``EXTERNAL_DETECTION`` (acceptance criterion 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml  # type: ignore[import-untyped]

REGISTRY_PATH = Path(__file__).resolve().parent / "registry.yaml"


class RegistryEntryState(StrEnum):
    """Registry-level state — before runtime policy is applied."""

    AVAILABLE = "AVAILABLE"
    NO_MODEL_AVAILABLE = "NO_MODEL_AVAILABLE"
    UNAVAILABLE_IN_REGION = "UNAVAILABLE_IN_REGION"
    DISABLED_BY_LICENCE = "DISABLED_BY_LICENCE"


@dataclass(slots=True)
class RegistryEntry:
    """One registry row — a (modality, bodyPart, specialisation) → bundle map."""

    modality: str
    body_part: str
    specialisation: str = "*"
    bundle_id: str | None = None
    state: RegistryEntryState = RegistryEntryState.AVAILABLE
    regulatory_class: str = "RUO"
    requires_gpu: bool = False
    expected_seconds: int = 0
    detail: str = ""
    categories: list[str] = field(default_factory=list)


class SegmentationRegistry:
    """Validated segmentation registry loaded from ``registry.yaml``."""

    def __init__(self, path: Path = REGISTRY_PATH) -> None:
        self._entries: list[RegistryEntry] = []
        self._load(path)

    def _load(self, path: Path) -> None:
        with path.open() as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return
        raw_entries = data.get("entries", [])
        if not isinstance(raw_entries, list):
            raise ValueError("registry.yaml 'entries' must be a list")
        for row in raw_entries:
            self._entries.append(self._parse_entry(row))

    @staticmethod
    def _parse_entry(row: dict[str, object]) -> RegistryEntry:
        modality_raw = row.get("modality")
        body_part_raw = row.get("bodyPart")
        if not isinstance(modality_raw, str) or not isinstance(body_part_raw, str):
            raise ValueError("registry entry requires string 'modality' and 'bodyPart'")
        spec_raw = row.get("specialisation")
        specialisation = spec_raw if isinstance(spec_raw, str) else "*"
        bundle_id_raw = row.get("bundleId")
        bundle_id = bundle_id_raw if isinstance(bundle_id_raw, str) else None
        state_raw = row.get("state")
        try:
            state = (
                RegistryEntryState(state_raw)
                if isinstance(state_raw, str)
                else (RegistryEntryState.AVAILABLE)
            )
        except ValueError:
            state = RegistryEntryState.AVAILABLE
        reg_raw = row.get("regulatoryClass")
        regulatory_class = reg_raw if isinstance(reg_raw, str) else "RUO"
        requires_gpu = bool(row.get("requiresGpu", False))
        expected_seconds_raw = row.get("expectedSeconds")
        expected_seconds = int(expected_seconds_raw) if isinstance(expected_seconds_raw, int) else 0
        detail_raw = row.get("detail")
        detail = detail_raw if isinstance(detail_raw, str) else ""
        cats_raw = row.get("categories")
        categories: list[str] = []
        if isinstance(cats_raw, list):
            categories = [str(c) for c in cats_raw]
        return RegistryEntry(
            modality=modality_raw.upper(),
            body_part=body_part_raw.upper(),
            specialisation=specialisation,
            bundle_id=bundle_id,
            state=state,
            regulatory_class=regulatory_class,
            requires_gpu=requires_gpu,
            expected_seconds=expected_seconds,
            detail=detail,
            categories=categories,
        )

    def entries(self) -> list[RegistryEntry]:
        """Return all registry entries (a copy)."""
        return list(self._entries)

    def resolve(
        self,
        modality: str,
        body_part: str,
        specialisation: str = "*",
    ) -> RegistryEntry:
        """Resolve ``(modality, bodyPart, specialisation)`` to an entry.

        Exact match first, then ``"*"`` wildcard on specialisation, then
        ``"*"`` wildcard on body part.  If nothing matches, return a synthetic
        ``NO_MODEL_AVAILABLE`` entry — the default is honest (§3.21.4).
        """
        mod = modality.upper()
        bp = body_part.upper()
        spec = specialisation

        # 1. exact match
        for e in self._entries:
            if e.modality == mod and e.body_part == bp and e.specialisation == spec:
                return e
        # 2. wildcard specialisation
        for e in self._entries:
            if e.modality == mod and e.body_part == bp and e.specialisation == "*":
                return e
        # 3. wildcard body part
        for e in self._entries:
            if e.modality == mod and e.body_part == "*" and e.specialisation == "*":
                return e
        # 4. no match — honest default
        return RegistryEntry(
            modality=mod,
            body_part=bp,
            state=RegistryEntryState.NO_MODEL_AVAILABLE,
            detail=f"No segmentation bundle for modality {mod}.",
        )

    def own_segmentation_categories(self) -> frozenset[str]:
        """The set of categories our own segmentation may produce.

        Our own segmentation never produces ``EXTERNAL_DETECTION`` — that label
        is reserved for external cleared-AI sources (acceptance criterion 2,
        §3.15.1 invariant 3).
        """
        cats: set[str] = set()
        for e in self._entries:
            cats.update(e.categories)
        # PRIOR_COMPARISON_CHANGE is produced by the priors stage, not a
        # registry bundle, but it is still an own-segmentation category.
        cats.add("PRIOR_COMPARISON_CHANGE")
        cats.discard("EXTERNAL_DETECTION")
        return frozenset(cats)


__all__ = [
    "REGISTRY_PATH",
    "RegistryEntry",
    "RegistryEntryState",
    "SegmentationRegistry",
]
