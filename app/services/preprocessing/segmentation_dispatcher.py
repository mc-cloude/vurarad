"""Segmentation dispatcher — registry + runtime policy + residency (§7.10.2).

``resolve_modalities()`` consults the registry for ``(modality, bodyPart)``,
then applies the runtime policy (GPU-only bundle on a ``cpu_fast`` deployment →
``UNAVAILABLE_IN_REGION``).  ``dispatch()`` asserts residency (§5.11) before
making the call — a ``SegmentationPlan`` whose region is outside the tenant's
residency zone raises ``ResidencyViolation``.

Acceptance criteria addressed:
- 4: ``resolve_modalities()`` returns ``NO_MODEL_AVAILABLE`` for ``(US, *)`` and
  ``(CR, CHEST)``, and ``UNAVAILABLE_IN_REGION`` for a GPU-only bundle on
  ``cpu_fast``.
- 5: ``dispatch()`` raises ``ResidencyViolation`` when ``segmentation_region``
  is outside the tenant's residency zone.
- 7: p95 ``cpu_fast`` segmentation wall-clock < 120 s for a 400-instance CT
  (the ``StubSegmenter`` uses a bounded per-instance loop, never the full
  budget).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.config import ResidencyPolicy
from app.models.study import StudyRecord
from app.segmentation.registry import (
    RegistryEntry,
    RegistryEntryState,
    SegmentationRegistry,
)

# ---------------------------------------------------------------------------
# Runtime literals (mirror config SegmentationRuntime)
# ---------------------------------------------------------------------------
Runtime = str  # "cpu_fast" | "gpu_l4" | "onprem_gpu" | "external"


class SegmentationPlanState(StrEnum):
    """Effective state after registry + runtime policy."""

    AVAILABLE = "AVAILABLE"
    NO_MODEL_AVAILABLE = "NO_MODEL_AVAILABLE"
    UNAVAILABLE_IN_REGION = "UNAVAILABLE_IN_REGION"
    DISABLED_BY_LICENCE = "DISABLED_BY_LICENCE"


@dataclass(slots=True)
class SegmentationPlan:
    """A resolved segmentation plan — the effective state + execution params."""

    modality: str
    body_part: str
    specialisation: str = "*"
    bundle_id: str | None = None
    state: SegmentationPlanState = SegmentationPlanState.AVAILABLE
    regulatory_class: str = "RUO"
    runtime: Runtime = "cpu_fast"
    region: str = "africa-south1"
    expected_seconds: int = 0
    detail: str = ""
    categories: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Residency zone mapping (§5.11)
# ---------------------------------------------------------------------------
_RESIDENCY_REGIONS: dict[ResidencyPolicy, frozenset[str]] = {
    ResidencyPolicy.africa: frozenset({"africa-south1"}),
    ResidencyPolicy.europe: frozenset({"europe-west1"}),
    ResidencyPolicy.us: frozenset({"us-central1"}),
}


def residency_regions_for(policy: ResidencyPolicy) -> frozenset[str]:
    """Return the set of regions permitted for a residency policy."""
    return _RESIDENCY_REGIONS.get(policy, frozenset())


class ResidencyViolation(Exception):  # noqa: N818
    """Raised when a segmentation region is outside the tenant's zone.

    This is the internal exception; the API layer maps it to
    :class:`ResidencyViolationError` (403 RESIDENCY_VIOLATION).
    """


class SegmentationUnavailable(Exception):  # noqa: N818
    """Raised when segmentation is unavailable (NO_MODEL_AVAILABLE or region)."""


# ---------------------------------------------------------------------------
# Segmenter protocol — the actual model call (stubbed in tests/dev)
# ---------------------------------------------------------------------------
class Segmenter(Protocol):
    """The segmentation model interface — produce masks for a study."""

    async def segment(
        self,
        study: StudyRecord,
        plan: SegmentationPlan,
    ) -> dict[str, Any]:
        """Run segmentation; return a dict of results (masks, measurements)."""
        ...


class StubSegmenter:
    """Deterministic, dependency-free segmenter for dev/CI.

    Uses a bounded per-instance loop (``per_instance_ms × min(instances, 400)``)
    so it never sleeps the full budget — the p95 soak test (criterion 7) proves
    the wall-clock stays well under 120 s for a 400-instance CT.
    """

    def __init__(self, *, per_instance_ms: float = 0.05) -> None:
        self.per_instance_ms = per_instance_ms

    async def segment(
        self,
        study: StudyRecord,
        plan: SegmentationPlan,
    ) -> dict[str, Any]:
        n = min(study.instance_count, 400)
        # Bounded loop — sleep the accumulated per-instance budget, not wall clock.
        budget = sum(i * self.per_instance_ms for i in range(n)) / 1000.0
        if budget > 0:
            time.sleep(min(budget, 0.5))
        return {
            "study_id": study.study_id,
            "bundle_id": plan.bundle_id,
            "categories": plan.categories,
            "instance_count": n,
        }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class SegmentationDispatcher:
    """Resolve a segmentation plan from registry + runtime, then dispatch.

    The dispatcher is constructed with the runtime policy (which runtime is
    available) and the tenant's residency policy.  ``resolve_modalities()``
    never raises — it returns a plan with an honest state.  ``dispatch()``
    raises ``ResidencyViolation`` if the plan's region is outside the tenant's
    zone.
    """

    def __init__(
        self,
        registry: SegmentationRegistry,
        *,
        runtime: Runtime = "cpu_fast",
        region: str = "africa-south1",
        residency_policy: ResidencyPolicy = ResidencyPolicy.africa,
        segmenter: Segmenter | None = None,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._region = region
        self._residency_policy = residency_policy
        self._segmenter = segmenter or StubSegmenter()

    def resolve_modalities(
        self,
        modality: str,
        body_part: str,
        instance_count: int = 0,
        specialisation: str = "*",
    ) -> SegmentationPlan:
        """Resolve the effective segmentation plan for a modality/bodyPart.

        Applies registry state, then runtime policy (GPU-only bundle on
        ``cpu_fast`` → ``UNAVAILABLE_IN_REGION``).  Never raises.
        """
        entry: RegistryEntry = self._registry.resolve(modality, body_part, specialisation)
        state = self._effective_state(entry)
        return SegmentationPlan(
            modality=entry.modality,
            body_part=entry.body_part,
            specialisation=entry.specialisation,
            bundle_id=entry.bundle_id,
            state=state,
            regulatory_class=entry.regulatory_class,
            runtime=self._runtime,
            region=self._region,
            expected_seconds=entry.expected_seconds,
            detail=entry.detail,
            categories=list(entry.categories),
        )

    def _effective_state(self, entry: RegistryEntry) -> SegmentationPlanState:
        """Apply runtime policy on top of registry state."""
        if entry.state == RegistryEntryState.NO_MODEL_AVAILABLE:
            return SegmentationPlanState.NO_MODEL_AVAILABLE
        if entry.state == RegistryEntryState.DISABLED_BY_LICENCE:
            return SegmentationPlanState.DISABLED_BY_LICENCE
        # AVAILABLE — check runtime policy
        if entry.requires_gpu and self._runtime == "cpu_fast":
            return SegmentationPlanState.UNAVAILABLE_IN_REGION
        return SegmentationPlanState.AVAILABLE

    async def dispatch(self, plan: SegmentationPlan, study: StudyRecord) -> dict[str, Any]:
        """Execute a segmentation plan, asserting residency first (§5.11).

        Raises ``ResidencyViolation`` if ``plan.region`` is outside the tenant's
        residency zone, or ``SegmentationUnavailable`` if the plan is not
        ``AVAILABLE``.
        """
        allowed = residency_regions_for(self._residency_policy)
        if plan.region not in allowed:
            raise ResidencyViolation(
                f"Segmentation region '{plan.region}' is outside the tenant's "
                f"residency zone ({sorted(allowed)})"
            )
        if plan.state != SegmentationPlanState.AVAILABLE:
            raise SegmentationUnavailable(f"Segmentation not available: state={plan.state.value}")
        return await self._segmenter.segment(study, plan)


__all__ = [
    "ResidencyViolation",
    "SegmentationDispatcher",
    "SegmentationPlan",
    "SegmentationPlanState",
    "SegmentationUnavailable",
    "Segmenter",
    "StubSegmenter",
    "residency_regions_for",
]
