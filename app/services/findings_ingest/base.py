"""Adapter protocol + neutral intermediate model for cleared-AI ingest (§3.15.4).

Every external cleared-AI source — DICOM SR (TID 1500), FHIR R4, Aidoc JSON,
Qure.ai JSON, or any vendor following the ``generic_v1`` contract — is parsed by
an adapter into a list of :class:`AdapterFinding` records.  The adapter reads
**headers/metadata only**; it never touches pixels (DICOM SR is parsed with
``stop_before_pixels=True``) and makes **no AI calls**.

``AdapterFinding`` is the only shape the :mod:`normalizer` accepts.  It is
deliberately allowed to carry CADt fields (``suspicion``, ``urgency``,
``triage``, ``priority``) that vendors emit, because the normalizer is the one
place that strips them — storing them on :class:`Finding` would make us the CADt
device (§3.15.3).  Clearance references (FDA K-number / CE mark) are NOT carried
here: they come from the registry's vendor-clearance records, never from the
vendor payload ("never upgrade on the vendor's behalf").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from app.models.common import CamelModel


class VendorIdentity(CamelModel):
    """Who/what produced a payload — stamped onto every finding's provenance.

    Clearance (``fdaKNumber`` / ``ceMarkRef``) is sourced from the registry's
    vendor-clearance record, NEVER from the vendor payload, so the system never
    upgrades a finding to ``CLEARED_DEVICE`` on the vendor's behalf.
    """

    vendor_name: str
    producer: str
    model_version: str
    adapter_name: str
    adapter_version: str
    fda_k_number: str | None = None
    ce_mark_ref: str | None = None
    runtime: str = "external"
    produced_at: datetime
    payload_ref: str | None = None  # object key / path of the retained source

    @property
    def has_clearance(self) -> bool:
        """``True`` when an FDA K-number OR a CE mark reference is registered."""
        return self.fda_k_number is not None or self.ce_mark_ref is not None


class AdapterGeometry(CamelModel):
    """Spatial reference for an adapter finding — bbox, point, or mask ref."""

    bbox: list[float] | None = None  # [x0, y0, x1, y1]
    point: list[float] | None = None  # [x, y, z]
    mask_ref: str | None = None


class AdapterMeasurement(CamelModel):
    """One measurement extracted from a vendor payload."""

    name: str = ""
    value: float
    unit: str
    method: str = ""


# CADt field names that vendor payloads may carry and the normalizer strips.
# Storing any of these on a Finding would make vuraRAD the CADt device (§3.15.3).
CADT_FIELDS: frozenset[str] = frozenset({"suspicion", "urgency", "triage", "priority"})


class AdapterFinding(CamelModel):
    """Neutral intermediate finding produced by every adapter.

    Carries the clinical content (label, body site, measurements, geometry,
    free text) plus the DICOM UIDs that tie it to a study.  CADt fields are
    present so the normalizer can count and drop them — they never reach
    :class:`~app.models.finding.Finding`.
    """

    study_instance_uid: str
    series_instance_uid: str | None = None
    sop_instance_uids: list[str] = []
    category: Literal["EXTERNAL_DETECTION"] = "EXTERNAL_DETECTION"
    label: str = ""
    body_site: str | None = None
    measurements: list[AdapterMeasurement] = []
    geometry: AdapterGeometry | None = None
    free_text: str | None = None

    # -- CADt fields — stripped by the normalizer, counted in telemetry -------
    suspicion: float | None = None
    urgency: str | None = None
    triage: str | None = None
    priority: str | None = None

    def cadt_fields_present(self) -> list[str]:
        """Return the names of CADt fields that are set on this finding."""
        present: list[str] = []
        for name in CADT_FIELDS:
            if getattr(self, name, None) is not None:
                present.append(name)
        return present


@runtime_checkable
class FindingAdapter(Protocol):
    """Parse a vendor payload (bytes) into adapter findings — no AI, no pixels.

    The payload is the raw bytes the client sent.  DICOM adapters parse with
    ``stop_before_pixels=True``; JSON adapters decode and walk the document.
    The adapter must NOT read clearance references from the payload — clearance
    is resolved from the registry (:class:`VendorIdentity`) by the caller.
    """

    name: str
    version: str
    content_type: str

    def parse(self, payload: bytes) -> list[AdapterFinding]: ...


# ---------------------------------------------------------------------------
# Shared JSON coercion helpers — used by the JSON vendor adapters.
# Each adapter still owns its format-specific extraction; these cover the
# common "pull a typed scalar/list out of a vendor dict" cases.
# ---------------------------------------------------------------------------
def opt_str(d: dict[str, Any], key: str) -> str | None:
    """Return a non-empty string value for ``key``, else ``None``."""
    v = d.get(key)
    return v if isinstance(v, str) and v else None


def opt_float(d: dict[str, Any], key: str) -> float | None:
    """Return a float for ``key`` (int or float, never bool), else ``None``."""
    v = d.get(key)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    return None


def opt_str_list(d: dict[str, Any], key: str) -> list[str]:
    """Return a list of non-empty strings for ``key``, else ``[]``."""
    v = d.get(key)
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str) and x]


def opt_float_list(d: dict[str, Any], key: str) -> list[float] | None:
    """Return a list of floats for ``key`` (all members numeric), else ``None``."""
    v = d.get(key)
    if not isinstance(v, list):
        return None
    out: list[float] = []
    for x in v:
        if isinstance(x, bool) or x is None or not isinstance(x, int | float):
            return None
        out.append(float(x))
    return out or None


def measurements_from_mapping(d: dict[str, Any]) -> list[AdapterMeasurement]:
    """Build measurements from a ``measurements`` array of ``{name,value,unit}``."""
    raw = d.get("measurements")
    if not isinstance(raw, list):
        return []
    result: list[AdapterMeasurement] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        value = opt_float(item, "value")
        unit = opt_str(item, "unit")
        if value is None or unit is None:
            continue
        result.append(
            AdapterMeasurement(
                name=opt_str(item, "name") or "",
                value=value,
                unit=unit,
                method=opt_str(item, "method") or "",
            )
        )
    return result


def geometry_from_parts(
    bbox: list[float] | None = None,
    point: list[float] | None = None,
    mask_ref: str | None = None,
) -> AdapterGeometry | None:
    """Build an :class:`AdapterGeometry` or ``None`` when all parts are absent."""
    if bbox is None and point is None and mask_ref is None:
        return None
    return AdapterGeometry(bbox=bbox, point=point, mask_ref=mask_ref)


__all__ = [
    "CADT_FIELDS",
    "AdapterFinding",
    "AdapterGeometry",
    "AdapterMeasurement",
    "FindingAdapter",
    "VendorIdentity",
    "geometry_from_parts",
    "measurements_from_mapping",
    "opt_float",
    "opt_float_list",
    "opt_str",
    "opt_str_list",
]
