"""Server-computed stack order — projection of ``ImagePositionPatient`` ``[B6]``.

``instanceNumber`` is NOT the ordering key.  The server projects each
``ImagePositionPatient`` onto the slice normal — the cross product of the
``ImageOrientationPatient`` row and column vectors — and sorts by that scalar.
Geometric order wins even when ``InstanceNumber`` disagrees, and
``stackOrderBasis`` records which rule was used so the viewer can flag anything
other than ``RELIABLE`` ordering before a radiologist measures on it.
"""

from __future__ import annotations

import math

from app.models.series import Instance, StackOrderBasis, StackOrderConfidence

# Even-spacing band for RELIABLE confidence (§3.5 / §3.7 acceptance criterion 6).
SPACING_TOLERANCE = 0.05


def _cross(a: list[float], b: list[float]) -> list[float]:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _dot(a: list[float], b: list[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(v: list[float]) -> float:
    return math.sqrt(_dot(v, v))


def _spacing_confidence(scalars: list[float]) -> StackOrderConfidence:
    """Classify the spacing of an ordered sequence of projection scalars."""
    if len(scalars) < 2:
        return StackOrderConfidence.RELIABLE
    diffs = [scalars[i + 1] - scalars[i] for i in range(len(scalars) - 1)]
    mean = sum(diffs) / len(diffs)
    if mean == 0:
        # All slices at the same location — no meaningful spacing/ordering.
        return StackOrderConfidence.IRREGULAR_SPACING
    for d in diffs:
        if abs(d - mean) / abs(mean) > SPACING_TOLERANCE:
            return StackOrderConfidence.IRREGULAR_SPACING
    return StackOrderConfidence.RELIABLE


def _assign_dense(ordered: list[Instance]) -> None:
    """Assign dense, gapless, zero-based ``stackIndex`` in the given order."""
    for idx, inst in enumerate(ordered):
        inst.stack_index = idx


def _order_by_instance_number(instances: list[Instance]) -> list[Instance]:
    if instances and all(i.instance_number is not None for i in instances):
        return sorted(instances, key=lambda i: i.instance_number or 0)
    return list(instances)


def compute_stack_order(
    instances: list[Instance],
) -> tuple[StackOrderBasis, StackOrderConfidence, list[Instance]]:
    """Assign dense, gapless, zero-based ``stackIndex`` to ``instances``.

    Returns ``(basis, confidence, ordered_instances)`` where the returned
    instances are sorted and carry their final ``stackIndex``.  The input
    instances are mutated in place (their ``stack_index`` is set).

    Basis priority:

    1. ``IMAGE_POSITION_PATIENT_PROJECTED`` — every instance has
       ``imagePositionPatient`` and a consistent ``imageOrientationPatient``.
       Geometric order wins even when ``InstanceNumber`` disagrees.
    2. ``FRAME_INDEX`` — any instance is multi-frame; the object's own frame
       order is authoritative and the server does not flatten it.
    3. ``SLICE_LOCATION`` — positions absent but ``sliceLocation`` present.
    4. ``INSTANCE_NUMBER`` — last-resort fallback; confidence ``UNVERIFIED``.
    """
    if not instances:
        return StackOrderBasis.INSTANCE_NUMBER, StackOrderConfidence.UNVERIFIED, []

    # Multi-frame: the object's own frame order is authoritative.
    if any(i.number_of_frames > 1 for i in instances):
        ordered = _order_by_instance_number(instances)
        _assign_dense(ordered)
        return StackOrderBasis.FRAME_INDEX, StackOrderConfidence.RELIABLE, ordered

    projected = _projected_order(instances)
    if projected is not None:
        basis, confidence, ordered = projected
        _assign_dense(ordered)
        return basis, confidence, ordered

    if instances and all(i.slice_location is not None for i in instances):
        ordered = sorted(
            instances,
            key=lambda i: i.slice_location if i.slice_location is not None else 0.0,
        )
        confidence = _spacing_confidence(
            [i.slice_location if i.slice_location is not None else 0.0 for i in ordered]
        )
        _assign_dense(ordered)
        return StackOrderBasis.SLICE_LOCATION, confidence, ordered

    ordered = _order_by_instance_number(instances)
    _assign_dense(ordered)
    return StackOrderBasis.INSTANCE_NUMBER, StackOrderConfidence.UNVERIFIED, ordered


def _projected_order(
    instances: list[Instance],
) -> tuple[StackOrderBasis, StackOrderConfidence, list[Instance]] | None:
    """Try the ``IMAGE_POSITION_PATIENT_PROJECTED`` basis; ``None`` if not applicable."""
    positions = [i.image_position_patient for i in instances]
    if not all(p is not None for p in positions):
        return None
    iop = instances[0].image_orientation_patient
    if not iop or len(iop) < 6:
        return None
    row = [iop[0], iop[1], iop[2]]
    col = [iop[3], iop[4], iop[5]]
    normal = _cross(row, col)
    nn = _norm(normal)
    if nn == 0:
        return None
    unit = [c / nn for c in normal]
    scaled: list[tuple[float, Instance]] = []
    for inst, pos in zip(instances, positions, strict=True):
        if pos is None:
            return None  # unreachable: guarded by the all() above
        scaled.append((_dot(pos, unit), inst))
    scaled.sort(key=lambda p: p[0])
    ordered = [inst for _scalar, inst in scaled]
    scalars = [scalar for scalar, _inst in scaled]
    return (
        StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED,
        _spacing_confidence(scalars),
        ordered,
    )
