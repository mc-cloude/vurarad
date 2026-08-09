# Desktop add-on — MONAI Label authorization proxy

This document describes the vuraRAD desktop add-on backend: how the add-on
connects for imaging data, the MONAI Label authorization proxy, the licence
feature flag, and the **trademark and licensing constraints** that govern this
work.

> Scope note: vuraRAD contains **no** MONAI Label, VTK, or 3D Slicer source
> code. The proxy in this repository is a thin authorization boundary in front
> of an *external*, separately deployed MONAI Label server. See
> [Trademark & licensing constraints](#trademark--licensing-constraints).

---

## 1. DICOMweb connection (no bespoke protocol)

The desktop add-on retrieves pixels and metadata over **standard DICOMweb**
(QIDO-RS, WADO-RS, STOW-RS) as implemented in WP10. There is no bespoke binary
protocol and no vendor-specific imaging transport.

| Concern | Mechanism |
|---|---|
| Study/series/instance search | `GET /dicomweb/studies`, `…/series`, `…/instances` (QIDO-RS) |
| Pixel retrieval | `GET /dicomweb/studies/{uid}` and sub-paths (WADO-RS, multipart) |
| Upload / ingest | `POST /dicomweb/studies` (STOW-RS) |
| Auth | Bearer token + MFA on every `/dicomweb/*` route (DICOMweb is not a side door) |

The add-on authenticates with a vuraRAD bearer token (Identity Platform, MFA
enforced) and uses the same DICOMweb endpoints as any other vuraRAD client.
Tenant scoping is enforced server-side: a cross-tenant study UID resolves to
`404`, never `403`, so no study existence leaks.

---

## 2. MONAI Label authorization proxy

The MONAI Label *inference/training* REST API is fronted by an authorization
proxy so that every request is authorized before a single byte is forwarded to
the external MONAI Label server.

```
POST /api/v1/monailabel/{path:path}
```

`{path}` (a path converter) captures the remainder of the URL, including
slashes, so MONAI Label endpoints such as `infer/{model}` or `train/{model}`
map directly. Query parameters are forwarded verbatim.

### Referenced study

The add-on declares the study it is operating on with the **`X-Study-Id`**
header (the internal `studyId` obtained from the worklist / study-detail API).
The header is required; omitting it returns `400 VALIDATION_ERROR`.

### Authorization order (all BEFORE the proxy hop)

1. **Authentication** — `get_current_user` (401 `MISSING_TOKEN` without a bearer
   token).
2. **MFA** — `require_mfa` (403 `MFA_REQUIRED` / `MFA_ENROLMENT_REQUIRED`).
3. **Capability** — `monailabel:use` (a PHI capability). A viewer without the
   capability gets `403 PERMISSION_DENIED`; an admin gets
   `403 PHI_ACCESS_FORBIDDEN` (separation of duties — admin holds zero PHI
   capabilities).
4. **Licence gate** — `features.slicer_addon` must be enabled, else
   `403 FEATURE_NOT_LICENSED`.
5. **Tenant + study authorization** — the referenced study is resolved and
   tenant-scoped (cross-tenant → `404 NOT_FOUND`, never `403`), then
   `StudyAccessPolicy` runs. A study the caller may not read yields `403`
   (`NOT_ASSIGNED` for a study assigned to another reader).

Only after every gate passes is the request forwarded. On any denial the
upstream is **never** contacted.

### Credential hygiene

The caller's vuraRAD bearer token (`Authorization`) and the `X-Study-Id` header
are stripped before forwarding, along with all hop-by-hop headers. The
upstream MONAI Label server never receives vuraRAD credentials.

### Size + timeout bounds

The proxy never streams unbounded bytes:

- **Request body** — read with a hard byte cap (`monailabel_max_request_bytes`,
  default 16 MiB). Exceeding it returns `413 PAYLOAD_TOO_LARGE`.
- **Response body** — read with a hard byte cap
  (`monailabel_max_response_bytes`, default 64 MiB). Exceeding it returns
  `413 PAYLOAD_TOO_LARGE`.
- **Timeout** — `monailabel_timeout_seconds` (default 30 s). A timeout returns
  `504 UPSTREAM_TIMEOUT`; any other upstream connection failure returns
  `502 UPSTREAM_UNAVAILABLE`.

Bounds and timeout are overridable via `app.state` for deployment tuning.

### Backend wiring

The external MONAI Label server base URL is configured on `app.state.monailabel_backend_url`
(a default `httpx`-backed client is built from it), or a custom
`MonaiLabelBackend` is injected on `app.state.monailabel_backend` (used by
tests). If no backend is configured, the proxy returns `502 UPSTREAM_UNAVAILABLE`.

---

## 3. Licence feature flag

```
GET /api/v1/licence
```

Returns the tenant's licence feature flags:

```json
{ "features": { "slicer_addon": true } }
```

`slicer_addon` is always present (default `false`). The MONAI Label proxy
checks this flag on every request via `LicenceService.has_feature("slicer_addon")`
and returns `403 FEATURE_NOT_LICENSED` when it is closed.

Production wires the real tenant licence document on `app.state.licence_service`.
The default is **fail-closed**: with no licence service configured, no feature
is licensed and the proxy refuses all requests.

---

## 4. Capability

`monailabel:use` is a PHI capability granted to the `radiologist` role. It
authorizes use of the proxy; study-level access is still enforced independently
by `StudyAccessPolicy` on every request. It is **not** granted to `viewer` or
`admin` (admin is denied at the PHI-capability gate).

---

## 5. Trademark & licensing constraints

These constraints are **binding** on this work and on any downstream packaging
or marketing of the desktop add-on.

### "3D Slicer" is a trademark of Brigham and Women's Hospital (BWH)

- **Do not** use the name **"3D Slicer"** or the 3D Slicer logo in the vuraRAD
  product, UI, marketing, documentation, or package metadata to identify or
  endorse vuraRAD. The name and logo are trademarks of Brigham and Women's
  Hospital (BWH) / Isomics and may not be used without permission.
- The internal licence feature key is `slicer_addon` (a lowercase identifier,
  not a displayed product name) and must not be surfaced to end users as a
  brand name.
- When referring to the upstream project in user-facing text, describe it
  generically (e.g., "an open-source medical imaging platform extension") and
  attribute the trademark to its owner where attribution is required.

### Preserve upstream licence notices

- vuraRAD ships **no** MONAI Label, VTK, or 3D Slicer source code. The external
  MONAI Label server is deployed and licensed separately by the operator.
- If any upstream artefact (notice file, licence text, attribution) is ever
  distributed alongside the add-on, the original upstream licence notices
  (MONAI Label — Apache 2.0; 3D Slicer components; VTK — BSD-3-Clause as
  applicable) must be preserved verbatim and not removed or altered.
- Do not represent vuraRAD as being affiliated with, endorsed by, or part of
  the MONAI Label or 3D Slicer projects.

### No upstream code in this repository

This repository intentionally contains **no** Slicer, VTK, or MONAI Label code.
The proxy (`app/services/monailabel_proxy.py`,
`app/api/v1/routers/monailabel.py`) is a pure authorization + forwarding
boundary. Adding upstream libraries here would conflate licences and is
prohibited by this work's scope.
