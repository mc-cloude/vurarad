"""Unit tests for server-computed stack order (§3.7 acceptance criteria 5-6).

``instanceNumber`` is NOT the ordering key: geometric order wins even when
``InstanceNumber`` disagrees, ``stackOrderBasis`` records which rule was used,
and ``stackOrderConfidence`` surfaces anything other than ``RELIABLE``.
"""

from __future__ import annotations

from app.models.series import Instance, StackOrderBasis, StackOrderConfidence
from app.services.stack_order import SPACING_TOLERANCE, compute_stack_order

# Axial orientation: row along +x, col along +y → slice normal +z.
_AXIAL_IOP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _inst(
    sop: str,
    *,
    pos: list[float] | None = None,
    iop: list[float] | None = None,
    instance_number: int | None = None,
    slice_location: float | None = None,
    frames: int = 1,
) -> Instance:
    return Instance(
        sop_instance_uid=sop,
        stack_index=0,
        instance_number=instance_number,
        number_of_frames=frames,
        image_position_patient=pos,
        image_orientation_patient=iop,
        slice_location=slice_location,
    )


# ---------------------------------------------------------------------------
# Geometric order wins over InstanceNumber disagreement
# ---------------------------------------------------------------------------
def test_geometric_order_wins_over_instance_number_disagreement() -> None:
    """Slices ordered by projection even when InstanceNumber is reversed."""
    # Geometric order: z = 0, 5, 10, 15 → sop-1..4 in that order.
    # InstanceNumber deliberately reversed: sop-1 has number 4, sop-4 has 1.
    instances = [
        _inst("1", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP, instance_number=4),
        _inst("2", pos=[0.0, 0.0, 5.0], iop=_AXIAL_IOP, instance_number=3),
        _inst("3", pos=[0.0, 0.0, 10.0], iop=_AXIAL_IOP, instance_number=2),
        _inst("4", pos=[0.0, 0.0, 15.0], iop=_AXIAL_IOP, instance_number=1),
    ]
    basis, confidence, ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED
    assert [i.sop_instance_uid for i in ordered] == ["1", "2", "3", "4"]
    # InstanceNumber was reversed but the stack follows geometry.
    assert [i.instance_number for i in ordered] == [4, 3, 2, 1]


def test_regular_spacing_is_reliable() -> None:
    instances = [
        _inst("a", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP),
        _inst("b", pos=[0.0, 0.0, 5.0], iop=_AXIAL_IOP),
        _inst("c", pos=[0.0, 0.0, 10.0], iop=_AXIAL_IOP),
    ]
    _basis, confidence, _ordered = compute_stack_order(instances)
    assert confidence is StackOrderConfidence.RELIABLE


def test_irregular_spacing_is_flagged() -> None:
    """Uneven spacing sets IRREGULAR_SPACING, never swallowed."""
    instances = [
        _inst("a", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP),
        _inst("b", pos=[0.0, 0.0, 5.0], iop=_AXIAL_IOP),
        _inst("c", pos=[0.0, 0.0, 30.0], iop=_AXIAL_IOP),  # 25 mm gap vs 5 mm
    ]
    _basis, confidence, ordered = compute_stack_order(instances)
    assert confidence is StackOrderConfidence.IRREGULAR_SPACING
    # Still ordered geometrically.
    assert [i.sop_instance_uid for i in ordered] == ["a", "b", "c"]


def test_spacing_tolerance_band_is_reliable() -> None:
    """Spacing within SPACING_TOLERANCE (5%) counts as regular."""
    spacing = 5.0
    jitter = spacing * SPACING_TOLERANCE * 0.5  # half the tolerance band
    instances = [
        _inst("a", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP),
        _inst("b", pos=[0.0, 0.0, spacing + jitter], iop=_AXIAL_IOP),
        _inst("c", pos=[0.0, 0.0, 2 * spacing], iop=_AXIAL_IOP),
    ]
    _basis, confidence, _ordered = compute_stack_order(instances)
    assert confidence is StackOrderConfidence.RELIABLE


# ---------------------------------------------------------------------------
# Dense, gapless, zero-based stackIndex
# ---------------------------------------------------------------------------
def test_stack_index_is_dense_gapless_zero_based() -> None:
    instances = [
        _inst("z", pos=[0.0, 0.0, 30.0], iop=_AXIAL_IOP, instance_number=99),
        _inst("y", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP, instance_number=2),
        _inst("x", pos=[0.0, 0.0, 15.0], iop=_AXIAL_IOP, instance_number=7),
    ]
    _basis, _confidence, ordered = compute_stack_order(instances)
    assert [i.stack_index for i in ordered] == [0, 1, 2]
    # Input mutated in place.
    assert sorted(i.stack_index for i in instances) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Multi-frame → FRAME_INDEX
# ---------------------------------------------------------------------------
def test_multiframe_uses_frame_index_basis() -> None:
    """A multi-frame object's own frame order is authoritative."""
    instances = [
        _inst("mf", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP, frames=10, instance_number=1),
        _inst("sf", pos=[0.0, 0.0, 5.0], iop=_AXIAL_IOP, frames=1, instance_number=2),
    ]
    basis, confidence, _ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.FRAME_INDEX
    assert confidence is StackOrderConfidence.RELIABLE


# ---------------------------------------------------------------------------
# SLICE_LOCATION fallback
# ---------------------------------------------------------------------------
def test_slice_location_fallback_when_positions_absent() -> None:
    instances = [
        _inst("a", slice_location=10.0, instance_number=3),
        _inst("b", slice_location=0.0, instance_number=1),
        _inst("c", slice_location=5.0, instance_number=2),
    ]
    basis, _confidence, ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.SLICE_LOCATION
    assert [i.sop_instance_uid for i in ordered] == ["b", "c", "a"]


# ---------------------------------------------------------------------------
# INSTANCE_NUMBER last-resort → UNVERIFIED
# ---------------------------------------------------------------------------
def test_instance_number_fallback_is_unverified() -> None:
    """No geometry at all → INSTANCE_NUMBER basis, UNVERIFIED confidence."""
    instances = [
        _inst("a", instance_number=3),
        _inst("b", instance_number=1),
        _inst("c", instance_number=2),
    ]
    basis, confidence, ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.INSTANCE_NUMBER
    assert confidence is StackOrderConfidence.UNVERIFIED
    assert [i.sop_instance_uid for i in ordered] == ["b", "c", "a"]


def test_partial_positions_fall_back_to_instance_number() -> None:
    """If even one instance lacks a position, projection is not applicable."""
    instances = [
        _inst("a", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP, instance_number=1),
        _inst("b", instance_number=2),  # no position
    ]
    basis, confidence, _ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.INSTANCE_NUMBER
    assert confidence is StackOrderConfidence.UNVERIFIED


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_empty_list_returns_empty() -> None:
    basis, confidence, ordered = compute_stack_order([])
    assert ordered == []
    assert basis is StackOrderBasis.INSTANCE_NUMBER
    assert confidence is StackOrderConfidence.UNVERIFIED


def test_single_instance_is_reliable() -> None:
    instances = [_inst("solo", pos=[0.0, 0.0, 0.0], iop=_AXIAL_IOP)]
    basis, confidence, ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED
    assert confidence is StackOrderConfidence.RELIABLE
    assert ordered[0].stack_index == 0


def test_degenerate_normal_falls_back() -> None:
    """A zero cross-product (parallel row/col) cannot define a normal."""
    iop = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # row == col → zero normal
    instances = [
        _inst("a", pos=[0.0, 0.0, 0.0], iop=iop, instance_number=1),
        _inst("b", pos=[0.0, 0.0, 1.0], iop=iop, instance_number=2),
    ]
    basis, _confidence, _ordered = compute_stack_order(instances)
    assert basis is StackOrderBasis.INSTANCE_NUMBER
