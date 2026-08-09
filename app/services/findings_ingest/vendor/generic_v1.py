"""generic_v1 — the documented minimal contract for ANY cleared-AI vendor.

This is the catch-all adapter for vendors that do not have a dedicated adapter
(``aidoc_v1`` / ``qure_v1``) and do not deliver DICOM SR or FHIR R4.  The
contract is published in ``docs/cleared-ai-ingest.md`` so a customer can wire an
arbitrary vendor without a custom adapter.

The adapter reads JSON only.  It does NOT read clearance references (FDA
K-number / CE mark) from the payload — clearance is resolved from the registry
by the caller, so the system never upgrades a finding on the vendor's behalf.
CADt fields (``suspicion`` / ``urgency`` / ``triage`` / ``priority``) are read
so the normalizer can count and drop them; they never reach a stored Finding.

Contract (``docs/cleared-ai-ingest.md``)::

    {
      "studyInstanceUid": "1.2.840.113619.2.55.3.604688119.971",
      "seriesInstanceUid": "1.2.840.113619.2.55.3.604688119.972",
      "sopInstanceUids": ["1.2.840.113619.2.55.3.604688119.973"],
      "findings": [
        {
          "label": "Pulmonary nodule",
          "bodySite": "LUNG",
          "measurements": [
            {"name": "long-axis diameter", "value": 8.0, "unit": "mm", "method": ""}
          ],
          "geometry": {"bbox": [10.0, 20.0, 110.0, 120.0], "maskRef": null},
          "freeText": "Incidental pulmonary nodule",
          "suspicion": 0.8, "urgency": "high", "triage": "positive", "priority": "urgent"
        }
      ]
    }
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
    raw = d.get("geometry")
    if not isinstance(raw, dict):
        return None
    return geometry_from_parts(
        bbox=opt_float_list(raw, "bbox"),
        point=opt_float_list(raw, "point"),
        mask_ref=opt_str(raw, "maskRef") or opt_str(raw, "mask_ref"),
    )


class GenericV1Adapter:
    """Parse a ``generic_v1`` JSON payload into adapter findings."""

    name = "generic_v1"
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
                    label=opt_str(item, "label") or "",
                    body_site=opt_str(item, "bodySite") or opt_str(item, "body_site"),
                    measurements=measurements_from_mapping(item),
                    geometry=_geometry(item),
                    free_text=opt_str(item, "freeText") or opt_str(item, "free_text"),
                    suspicion=opt_float(item, "suspicion"),
                    urgency=opt_str(item, "urgency"),
                    triage=opt_str(item, "triage"),
                    priority=opt_str(item, "priority"),
                )
            )
        return findings


__all__ = ["GenericV1Adapter"]
