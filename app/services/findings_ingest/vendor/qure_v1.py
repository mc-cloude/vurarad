"""qure_v1 — Qure.ai JSON adapter.

Qure.ai (e.g. the qER product) delivers findings as a ``findings`` array.  Each
finding carries a ``name`` (the finding label), ``region`` (body site), a
``bbox`` ``[x0, y0, x1, y1]`` or a ``maskRef`` object key, optional
``measurements``, and a free-text ``note``.

Qure payloads carry priority/suspicion signals (``priority`` / ``suspicion``).
These are read onto :class:`AdapterFinding` so the normalizer can count and drop
them — storing them would make vuraRAD the CADt device (§3.15.3).  The adapter
does NOT read any CE mark from the payload; clearance is resolved from the
registry by the caller.
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


def _geometry(d: dict[str, Any]) -> AdapterGeometry | None:
    bbox = opt_float_list(d, "bbox")
    mask_ref = opt_str(d, "maskRef") or opt_str(d, "mask_ref")
    if bbox is None and mask_ref is None:
        return None
    return geometry_from_parts(bbox=bbox, mask_ref=mask_ref)


class QureV1Adapter:
    """Parse a Qure.ai v1 JSON payload into adapter findings."""

    name = "qure_v1"
    version = "1"
    content_type = "application/json"

    def parse(self, payload: bytes) -> list[AdapterFinding]:
        doc: Any = json.loads(payload.decode("utf-8"))
        if not isinstance(doc, dict):
            return []
        study_uid = opt_str(doc, "studyInstanceUid") or ""
        series_uid = opt_str(doc, "seriesInstanceUid")
        sop_uids = opt_str_list(doc, "sopInstanceUids")
        raw_findings = doc.get("findings")
        if not isinstance(raw_findings, list):
            return []
        findings: list[AdapterFinding] = []
        for item in raw_findings:
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
                    label=opt_str(item, "name") or opt_str(item, "label") or "",
                    body_site=opt_str(item, "region") or opt_str(item, "bodyPart"),
                    measurements=measurements_from_mapping(item),
                    geometry=_geometry(item),
                    free_text=opt_str(item, "note") or opt_str(item, "description"),
                    suspicion=opt_float(item, "suspicion"),
                    urgency=opt_str(item, "urgency"),
                    triage=opt_str(item, "triage"),
                    priority=opt_str(item, "priority"),
                )
            )
        return findings


__all__ = ["QureV1Adapter"]
