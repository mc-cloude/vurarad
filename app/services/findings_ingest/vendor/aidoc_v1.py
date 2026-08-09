"""aidoc_v1 — Aidoc JSON adapter.

Aidoc delivers findings as a ``results`` array.  Each result carries a
``type`` (the finding label), ``bodyPart``, a ``boundingBox``
(``{x, y, width, height}`` → a 2D bbox ``[x, y, x+width, y+height]``), optional
``measurements``, and a free-text ``description``.

Aidoc payloads carry CADt-style triage/urgency flags (``urgency`` /
``triage``).  These are read onto :class:`AdapterFinding` so the normalizer can
count and drop them — storing them would make vuraRAD the CADt device
(§3.15.3).  The adapter does NOT read any FDA K-number from the payload;
clearance is resolved from the registry by the caller.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.findings_ingest.base import (
    AdapterFinding,
    AdapterGeometry,
    geometry_from_parts,
    measurements_from_mapping,
    opt_float,
    opt_float_list,
    opt_str,
    opt_str_list,
)


def _bbox_from_box(d: dict[str, Any]) -> list[float] | None:
    box = d.get("boundingBox") or d.get("bbox")
    if not isinstance(box, dict):
        return None
    x = opt_float(box, "x")
    y = opt_float(box, "y")
    w = opt_float(box, "width")
    h = opt_float(box, "height")
    if x is None or y is None or w is None or h is None:
        # Fall back to an explicit bbox list if the vendor used that shape.
        return opt_float_list(box, "bbox")
    return [x, y, x + w, y + h]


def _geometry(d: dict[str, Any]) -> AdapterGeometry | None:
    bbox = _bbox_from_box(d)
    if bbox is None:
        return None
    return geometry_from_parts(bbox=bbox)


class AidocV1Adapter:
    """Parse an Aidoc v1 JSON payload into adapter findings."""

    name = "aidoc_v1"
    version = "1"
    content_type = "application/json"

    def parse(self, payload: bytes) -> list[AdapterFinding]:
        doc: Any = json.loads(payload.decode("utf-8"))
        if not isinstance(doc, dict):
            return []
        study_uid = opt_str(doc, "studyInstanceUid") or ""
        series_uid = opt_str(doc, "seriesInstanceUid")
        sop_uids = opt_str_list(doc, "sopInstanceUids")
        raw_results = doc.get("results")
        if not isinstance(raw_results, list):
            return []
        findings: list[AdapterFinding] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            per_study = opt_str(item, "studyInstanceUid") or study_uid
            if not per_study:
                continue
            findings.append(
                AdapterFinding(
                    study_instance_uid=per_study,
                    series_instance_uid=opt_str(item, "seriesInstanceUid") or series_uid,
                    sop_instance_uids=opt_str_list(item, "sopInstanceUids") or sop_uids,
                    label=opt_str(item, "type") or opt_str(item, "label") or "",
                    body_site=opt_str(item, "bodyPart") or opt_str(item, "bodySite"),
                    measurements=measurements_from_mapping(item),
                    geometry=_geometry(item),
                    free_text=opt_str(item, "description") or opt_str(item, "freeText"),
                    suspicion=opt_float(item, "suspicion"),
                    urgency=opt_str(item, "urgency"),
                    triage=opt_str(item, "triage"),
                    priority=opt_str(item, "priority"),
                )
            )
        return findings


__all__ = ["AidocV1Adapter"]
