# Cleared-AI findings ingest — `generic_v1` contract

vuraRAD ingests findings from external cleared-AI devices through version-pinned
adapters. This document publishes the **`generic_v1`** contract — the minimal
JSON shape any vendor can use to deliver findings without a bespoke adapter.

Dedicated adapters exist for Aidoc (`aidoc_v1`), Qure.ai (`qure_v1`), DICOM
Structured Reports / TID 1500 (`dicom_sr`), and FHIR R4
(`fhir_r4`). If your vendor is not one of those, use `generic_v1`.

## Endpoint

```
POST /api/v1/studies/{studyId}/findings/ingest?adapterName=generic_v1&adapterVersion=1
Authorization: Bearer <token>      (a role holding the findings:ingest capability, MFA-verified)
Content-Type: application/json

<raw generic_v1 JSON body>
```

The request body is the raw `generic_v1` JSON. The `payloadSha256` used for
idempotency is the SHA-256 of these raw bytes, so send a canonical (stable)
serialisation if you want replays to deduplicate.

## `generic_v1` JSON shape

```json
{
  "studyInstanceUid": "1.2.840.113619.2.55.3.604688119.971",
  "seriesInstanceUid": "1.2.840.113619.2.55.3.604688119.972",
  "sopInstanceUids": ["1.2.840.113619.2.55.3.604688119.973"],
  "findings": [
    {
      "label": "Pulmonary nodule",
      "bodySite": "LUNG",
      "measurements": [
        { "name": "long-axis diameter", "value": 8.0, "unit": "mm", "method": "" }
      ],
      "geometry": { "bbox": [10.0, 20.0, 110.0, 120.0], "point": null, "maskRef": null },
      "freeText": "Incidental pulmonary nodule",
      "suspicion": 0.8,
      "urgency": "high",
      "triage": "positive",
      "priority": "urgent"
    }
  ]
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `studyInstanceUid` | string | yes (top-level or per-finding) | DICOM Study Instance UID the findings belong to. |
| `seriesInstanceUid` | string | no | DICOM Series Instance UID. |
| `sopInstanceUids` | string[] | no | SOP Instance UIDs the findings reference. |
| `findings[]` | array | yes | One entry per finding. |
| `findings[].label` | string | no | Finding label (e.g. "Pulmonary nodule"). |
| `findings[].bodySite` | string | no | Coded or free-text body site (e.g. "LUNG"). |
| `findings[].measurements[]` | array | no | `{ name, value, unit, method }`. `value` is numeric; `unit` is required. |
| `findings[].geometry` | object | no | `bbox` `[x0, y0, x1, y1]`, `point` `[x, y, z]`, and/or `maskRef` (object key for a stored mask). |
| `findings[].freeText` | string | no | Free-text description. **Passes a PHI redaction filter before storage.** |
| `findings[].suspicion` | number | no | **CADt field — ignored and dropped.** |
| `findings[].urgency` | string | no | **CADt field — ignored and dropped.** |
| `findings[].triage` | string | no | **CADt field — ignored and dropped.** |
| `findings[].priority` | string | no | **CADt field — ignored and dropped.** |

## Clearance rules (read carefully)

1. **`regulatoryClass == "CLEARED_DEVICE"` requires a registered clearance.**
   The FDA K-number **or** CE mark reference must be registered for the adapter
   in vuraRAD's adapter registry. vuraRAD **never** reads a clearance claim from
   your payload and **never** upgrades a finding to `CLEARED_DEVICE` on a
   vendor's behalf (proof-based clearance).

2. **No registered clearance → `RUO`.** Findings are stored with
   `regulatoryClass == "RUO"`, `clinicalUseAllowed == false`, category
   `RESEARCH_ONLY`, disposition `REJECTED`, and a `NO_CLEARANCE_REFERENCE`
   marker. They never enter clinical use.

3. **CADt fields are stripped.** `suspicion`, `urgency`, `triage`, and
   `priority` are dropped at the normalizer and counted in telemetry. Storing
   them would make vuraRAD the CADt device; they are accepted in the payload
   only so they can be counted and discarded.

4. **Free text is PHI-redacted.** Anything in `freeText` is scrubbed (known
   study identifiers, emails, dates, phone numbers) before it is stored on a
   finding.

## Idempotency

Ingest is idempotent on the tuple
`(tenantId, studyInstanceUid, adapterName, adapterVersion, payloadSha256)`.
Replaying an identical request returns the original response with
`idempotent == true` and writes no new findings.

## Response

```json
{
  "ingestId": "fi_...",
  "studyId": "st_...",
  "studyInstanceUid": "1.2.840....",
  "adapterName": "generic_v1",
  "adapterVersion": "1",
  "vendorName": "generic",
  "payloadSha256": "<hex>",
  "idempotent": false,
  "findings": [
    {
      "findingId": "fd_...",
      "regulatoryClass": "RUO",
      "clinicalUseAllowed": false,
      "dispositionState": "REJECTED",
      "noClearanceReference": true
    }
  ],
  "cadtFieldsDropped": 4,
  "noClearanceReferenceCount": 1,
  "sourceObjectKey": null,
  "producedAt": "2026-08-09T12:00:00Z"
}
```

A `generic_v1` ingest with no registered clearance produces `RUO` / `REJECTED`
findings (as above). Register a clearance for your vendor with vuraRAD to
receive `CLEARED_DEVICE` findings with `clinicalUseAllowed == true`.
