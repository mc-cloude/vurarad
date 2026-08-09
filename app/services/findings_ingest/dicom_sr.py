"""dicom_sr — TID 1500 content-tree walk via pydicom, headers only.

The adapter parses a DICOM Structured Report (Comprehensive SR, SOPClassUID
``1.2.840.10008.5.1.4.1.1.88.22``) carrying a TID 1500 "Measurements and
Qualitative Evaluations" content tree.  Parsing uses
``dcmread(..., stop_before_pixels=True)`` so pixel data is NEVER read — the
safety equivalent required for cleared-AI ingest (criterion 7).  Only the
header/metadata and the SR content tree are walked; no AI calls are made.

The walk extracts one :class:`AdapterFinding` per measurement-group CONTAINER:

- ``label``      ← the container's ConceptName ``CodeMeaning``
- ``body_site``  ← a child CODE item concept-named "Finding Site"
- ``measurements`` ← child NUM items (``NumericValue`` + unit)
- ``geometry``   ← child SCOORD items (POLYLINE/CIRCLE/ELLIPSE → bbox, POINT → point)
- ``free_text``  ← child TEXT items (``TextValue``)

The SR object itself is retained by the caller (stored in the object store) and
referenced by each finding's provenance via ``ingest_ref``; this adapter only
parses it.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pydicom import dcmread

from app.services.findings_ingest.base import (
    AdapterFinding,
    AdapterGeometry,
    AdapterMeasurement,
    geometry_from_parts,
)

__all__ = ["DicomSRAdapter"]


# ---------------------------------------------------------------------------
# pydicom attribute helpers (Dataset is treated as Any — pydicom is untyped)
# ---------------------------------------------------------------------------
def _value_type(item: Any) -> str:
    return str(getattr(item, "ValueType", "") or "")


def _concept_meaning(item: Any) -> str:
    seq = getattr(item, "ConceptNameCodeSequence", None)
    if seq and len(seq) > 0:
        return str(getattr(seq[0], "CodeMeaning", "") or "")
    return ""


def _content_sequence(item: Any) -> list[Any]:
    seq = getattr(item, "ContentSequence", None)
    if not seq:
        return []
    return list(seq)


def _numeric_value(num: Any) -> float | None:
    measured = getattr(num, "MeasuredValueSequence", None)
    if not measured or len(measured) == 0:
        return None
    raw = getattr(measured[0], "NumericValue", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _numeric_unit(num: Any) -> str | None:
    measured = getattr(num, "MeasuredValueSequence", None)
    if not measured or len(measured) == 0:
        return None
    units = getattr(measured[0], "MeasurementUnitsCodeSequence", None)
    if not units or len(units) == 0:
        return None
    unit = getattr(units[0], "CodeValue", None) or getattr(units[0], "CodeMeaning", None)
    val = str(unit or "")
    return val or None


def _measurement_from_num(num: Any) -> AdapterMeasurement | None:
    value = _numeric_value(num)
    unit = _numeric_unit(num)
    if value is None or unit is None:
        return None
    return AdapterMeasurement(name=_concept_meaning(num), value=value, unit=unit)


def _code_value_of(code_item: Any) -> str:
    """The display value of a CODE content item (meaning, then code value)."""
    seq = getattr(code_item, "ConceptCodeSequence", None)
    if not seq or len(seq) == 0:
        return ""
    first = seq[0]
    return str(getattr(first, "CodeMeaning", "") or getattr(first, "CodeValue", "") or "")


def _body_site(children: list[Any]) -> str | None:
    """Find a "Finding Site" CODE child and return its coded value."""
    for child in children:
        if _value_type(child) != "CODE":
            continue
        if "finding site" in _concept_meaning(child).lower():
            val = _code_value_of(child)
            if val:
                return val
    return None


def _free_text(children: list[Any]) -> str | None:
    parts: list[str] = []
    for child in children:
        if _value_type(child) != "TEXT":
            continue
        val = getattr(child, "TextValue", None)
        if val is not None:
            text = str(val)
            if text:
                parts.append(text)
    return " ".join(parts) if parts else None


def _scoord_geometry(item: Any) -> AdapterGeometry | None:
    gtype = str(getattr(item, "GraphicType", "") or "")
    gdata = getattr(item, "GraphicData", None)
    if gdata is None:
        return None
    try:
        coords = [float(x) for x in gdata]
    except (TypeError, ValueError):
        return None
    if gtype == "POINT":
        return geometry_from_parts(point=coords)
    xs = coords[0::2]
    ys = coords[1::2]
    if not xs or not ys:
        return None
    return geometry_from_parts(bbox=[min(xs), min(ys), max(xs), max(ys)])


def _geometry(children: list[Any]) -> AdapterGeometry | None:
    for child in children:
        if _value_type(child) == "SCOORD":
            geom = _scoord_geometry(child)
            if geom is not None:
                return geom
    return None


# ---------------------------------------------------------------------------
# Content-tree walk
# ---------------------------------------------------------------------------
def _walk(
    item: Any,
    study_uid: str,
    series_uid: str | None,
    sop_uids: list[str],
    out: list[AdapterFinding],
) -> None:
    if _value_type(item) != "CONTAINER":
        return
    children = _content_sequence(item)
    nums = [c for c in children if _value_type(c) == "NUM"]
    scoords = [c for c in children if _value_type(c) == "SCOORD"]
    if nums or scoords:
        measurements: list[AdapterMeasurement] = []
        for num in nums:
            m = _measurement_from_num(num)
            if m is not None:
                measurements.append(m)
        out.append(
            AdapterFinding(
                study_instance_uid=study_uid,
                series_instance_uid=series_uid,
                sop_instance_uids=sop_uids,
                label=_concept_meaning(item),
                body_site=_body_site(children),
                measurements=measurements,
                geometry=_geometry(children),
                free_text=_free_text(children),
            )
        )
    for child in children:
        _walk(child, study_uid, series_uid, sop_uids, out)


class DicomSRAdapter:
    """Parse a DICOM TID 1500 SR (headers only) into adapter findings."""

    name = "dicom_sr"
    version = "1"
    content_type = "application/dicom"

    def parse(self, payload: bytes) -> list[AdapterFinding]:
        # stop_before_pixels=True guarantees pixel data is never read, even if
        # the object erroneously carries PixelData (criterion 7).
        ds = dcmread(BytesIO(payload), stop_before_pixels=True, force=True)
        study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
        series_uid = getattr(ds, "SeriesInstanceUID", None)
        series_uid = str(series_uid) if series_uid else None
        sop_uid = getattr(ds, "SOPInstanceUID", None)
        sop_uids = [str(sop_uid)] if sop_uid else []

        out: list[AdapterFinding] = []
        for item in _content_sequence(ds):
            _walk(item, study_uid, series_uid, sop_uids, out)
        return out
