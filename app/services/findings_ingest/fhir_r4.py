"""fhir_r4 — FHIR R4 Observation + ImagingSelection + DiagnosticReport adapter.

Parses a FHIR R4 ``Bundle`` whose entries are a ``DiagnosticReport``, one or
more ``Observation`` resources (the findings), and the ``ImagingSelection``
resources that carry the DICOM UIDs and image regions.

.. note::
   ``ImagingSelection`` is not part of the original FHIR R4 ballot (it lands in
   R4B/R5), but it is the FHIR resource that carries the DICOM Study/Series/
   Instance UIDs and a 2-D image region — exactly what cleared-AI ingest needs.
   This adapter therefore accepts the ``ImagingSelection`` shape
   (``studyUid`` / ``seriesUid`` / ``instance[].uid`` / ``imageRegion``) that
   R4B+ defines, linked from an Observation via ``derivedFrom``.

Mapping:

- ``label``        ← ``Observation.code`` (text, then coding display)
- ``body_site``    ← ``Observation.bodySite``
- ``measurements`` ← ``Observation.component[]`` (component.code → name,
  component.valueQuantity → value + unit)
- ``free_text``    ← ``Observation.note[].text``
- ``geometry``     ← ``Observation.derivedFrom`` → ``ImagingSelection.imageRegion``
  (rectangle/polygon/ellipse coordinates → bbox ``[min_x, min_y, max_x, max_y]``)
- ``studyInstanceUid`` / ``seriesInstanceUid`` / ``sopInstanceUids`` ←
  the linked ``ImagingSelection``

The adapter reads JSON only and never touches pixels or makes AI calls.  It does
NOT read clearance references from the payload — clearance is resolved from the
registry by the caller.  CADt fields are not part of the FHIR mapping; if a
vendor encodes suspicion/triage in an extension it is ignored here.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.findings_ingest.base import (
    AdapterFinding,
    AdapterGeometry,
    AdapterMeasurement,
    geometry_from_parts,
)


def _ref_id(ref: Any) -> str | None:
    if not isinstance(ref, dict):
        return None
    reference = ref.get("reference")
    if not isinstance(reference, str) or not reference:
        return None
    return reference.rsplit("/", 1)[-1] or None


def _codeable_display(cc: Any) -> str | None:
    if not isinstance(cc, dict):
        return None
    text = cc.get("text")
    if isinstance(text, str) and text:
        return text
    codings = cc.get("coding")
    if isinstance(codings, list):
        for c in codings:
            if isinstance(c, dict):
                d = c.get("display") or c.get("code")
                if isinstance(d, str) and d:
                    return d
    return None


def _quantity(qty: Any) -> tuple[float | None, str | None]:
    if not isinstance(qty, dict):
        return None, None
    value = qty.get("value")
    if isinstance(value, bool) or value is None or not isinstance(value, int | float):
        return None, None
    unit = qty.get("unit") or qty.get("code")
    unit_str = unit if isinstance(unit, str) and unit else None
    return float(value), unit_str


def _measurements_from_obs(obs: dict[str, Any]) -> list[AdapterMeasurement]:
    components = obs.get("component")
    if not isinstance(components, list):
        return []
    result: list[AdapterMeasurement] = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        value, unit = _quantity(comp.get("valueQuantity"))
        if value is None or unit is None:
            continue
        name = _codeable_display(comp.get("code")) or ""
        result.append(AdapterMeasurement(name=name, value=value, unit=unit))
    return result


def _region_bbox(region: Any) -> list[float] | None:
    if not isinstance(region, dict):
        return None
    coords = region.get("coordinate")
    if not isinstance(coords, list):
        return None
    xs: list[float] = []
    ys: list[float] = []
    for point in coords:
        if not isinstance(point, list) or len(point) < 2:
            return None
        try:
            x = float(point[0])
            y = float(point[1])
        except (TypeError, ValueError):
            return None
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _imaging_selection_fields(
    sel: dict[str, Any],
) -> tuple[str, str | None, list[str], AdapterGeometry | None]:
    study_uid = sel.get("studyUid")
    study = study_uid if isinstance(study_uid, str) and study_uid else ""
    series_uid = sel.get("seriesUid")
    series = series_uid if isinstance(series_uid, str) and series_uid else None
    sop_uids: list[str] = []
    instances = sel.get("instance")
    if isinstance(instances, list):
        for inst in instances:
            if isinstance(inst, dict):
                uid = inst.get("uid")
                if isinstance(uid, str) and uid:
                    sop_uids.append(uid)
    bbox = _region_bbox(sel.get("imageRegion") or sel.get("region"))
    geometry = geometry_from_parts(bbox=bbox) if bbox is not None else None
    return study, series, sop_uids, geometry


class FhirR4Adapter:
    """Parse a FHIR R4 Bundle (DiagnosticReport + Observation + ImagingSelection)."""

    name = "fhir_r4"
    version = "1"
    content_type = "application/fhir+json"

    def parse(self, payload: bytes) -> list[AdapterFinding]:
        doc: Any = json.loads(payload.decode("utf-8"))
        resources = self._extract_resources(doc)
        by_id: dict[str, dict[str, Any]] = {
            r["id"]: r for r in resources if isinstance(r.get("id"), str)
        }
        selections = {r["id"]: r for r in resources if r.get("resourceType") == "ImagingSelection"}

        observation_ids = self._observation_ids(resources, by_id)
        findings: list[AdapterFinding] = []
        for oid in observation_ids:
            obs = by_id.get(oid)
            if not isinstance(obs, dict) or obs.get("resourceType") != "Observation":
                continue
            findings.append(self._observation_to_finding(obs, by_id, selections))
        return findings

    @staticmethod
    def _extract_resources(doc: Any) -> list[dict[str, Any]]:
        if isinstance(doc, list):
            entries = doc
        elif isinstance(doc, dict):
            if doc.get("resourceType") in ("Bundle",):
                entries = doc.get("entry", [])
            elif "resourceType" in doc:
                return [doc]
            else:
                entries = []
        else:
            entries = []
        result: list[dict[str, Any]] = []
        if not isinstance(entries, list):
            return result
        for entry in entries:
            if isinstance(entry, dict):
                res = entry.get("resource") if "resource" in entry else entry
                if isinstance(res, dict):
                    result.append(res)
        return result

    @staticmethod
    def _observation_ids(
        resources: list[dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        # Prefer DiagnosticReport.result references; fall back to all Observations.
        report_ids: list[str] = []
        for res in resources:
            if res.get("resourceType") != "DiagnosticReport":
                continue
            for ref in res.get("result", []) or []:
                rid = _ref_id(ref)
                if rid and rid in by_id:
                    report_ids.append(rid)
        if report_ids:
            return report_ids
        return [
            r["id"]
            for r in resources
            if r.get("resourceType") == "Observation" and isinstance(r.get("id"), str)
        ]

    def _observation_to_finding(
        self,
        obs: dict[str, Any],
        by_id: dict[str, dict[str, Any]],
        selections: dict[str, dict[str, Any]],
    ) -> AdapterFinding:
        study_uid = ""
        series_uid: str | None = None
        sop_uids: list[str] = []
        geometry: AdapterGeometry | None = None
        for ref in obs.get("derivedFrom", []) or []:
            sid = _ref_id(ref)
            if sid and sid in selections:
                fields = _imaging_selection_fields(selections[sid])
                study_uid, series_uid, sop_uids, geometry = fields
                if study_uid:
                    break
        notes = obs.get("note")
        free_text: str | None = None
        if isinstance(notes, list):
            parts = [n.get("text") for n in notes if isinstance(n, dict)]
            joined = " ".join(p for p in parts if isinstance(p, str) and p)
            free_text = joined or None
        return AdapterFinding(
            study_instance_uid=study_uid,
            series_instance_uid=series_uid,
            sop_instance_uids=sop_uids,
            label=_codeable_display(obs.get("code")) or "",
            body_site=_codeable_display(obs.get("bodySite")),
            measurements=_measurements_from_obs(obs),
            geometry=geometry,
            free_text=free_text,
        )


__all__ = ["FhirR4Adapter"]
