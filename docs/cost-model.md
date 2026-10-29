# Cost model

Reference for vuraRAD infrastructure cost. Full arithmetic is in the plan
(§9.1–§9.7); this document states the governing model, the budget alerts the
Terraform implements, and the line-item total at stated traffic so a regression
in any line is visible without re-deriving the rest.

## Governing model (D15 / D18 / §9.7) — cost-per-image, not a flat cap

The flat **$20/mo global cap is retired** (D15). Cost-per-image governs, and the
control is a **per-seat $10/mo infrastructure ceiling** that includes all
metered infrastructure **including viewer bandwidth** (D18), enforced in code by
the metering service with staged degradation (AI first, ingest last). The Cloud
Billing budgets below **alert**; they do **not** cap — the app-level ceiling is
the hard stop.

- Lowest cost per image at **$0.0766/1,000 images** on Google Cloud (D1).
- Africa marginal cost: storage $0.0229 + CPU segmentation $0.0348 + PyRadiomics
  $0.00042 + OpenMed $0.0004 + LLM drafting $0.0097 = **~$0.068/1,000 images**
  against **$2.50/1,000** retail — a 36× markup (§9.7.1).
- Per-seat all-in at the D18 ceiling volume (~300 studies/mo, low-bandwidth
  default): **~$13.67/seat** → effective **$0.0334/study**, **~79% margin** on a
  $49 seat (§9.7.2). At ~300 studies/mo the ceiling spend is ~$10.02, consistent
  with D18's advertised "~300 studies / ~120,000 images."
- Overage: **$0.25 per 100 images** above the included volume — ~95% margin,
  metered from `images_ingested`, not estimated (§9.7.3).

## Budget alerts — `infra/monitoring.tf`

Cloud Billing budgets alert a human; they are coarse project-level alarms, not
caps, and they sit on top of the per-seat ceiling.

| Budget | Amount | Scope | Thresholds |
|---|---|---|---|
| Project | $20/mo | whole project (credits excluded) | 50% / 90% / 100% email |
| Vertex AI | $15/mo | Vertex AI service only | 90% / 100% email |
| Cloud Storage | $10/mo | Storage service only (egress is the largest variable line) | 90% / 100% email |

The Vertex AI line is additionally hard-capped at $5.00 by the in-app Gemini
circuit breaker (§7.5) regardless of traffic.

## Line items at stated traffic (§9.2 / §9.3)

Stated traffic: 500 studies/mo, 5 users, ~75 GB average billable imaging storage,
~1,500 study opens/mo, 300 Gemini calls/mo, `us-central1` regional. This is the
line-item reference build; §9.7's per-seat model governs pricing and the ceiling.

| Line item | Monthly |
|---|---|
| Cloud Run (incl. JSON egress) | $0.09 |
| Firestore ops | $0.00 |
| Firestore storage | $0.00 |
| Firestore PITR | $0.05 |
| **GCS — DICOM storage** | **$1.40** |
| **GCS — Class A ops** | **$1.98** |
| GCS — Class B ops | $0.14 |
| **GCS — egress to browsers** | **$9.60** |
| GCS — quarantine bucket | $0.47 |
| GCS — audit bucket + exports | $0.01 |
| **GCS — cross-region backup transfer** | **$2.00** |
| GCS — backup storage (Coldline + Nearline) | $0.37 |
| Terraform state bucket | $0.00 |
| Vertex AI Gemini | $1.31 |
| IAM `signBlob` | $0.00 |
| Identity Platform | $0.00 |
| Secret Manager | $0.00 |
| Cloud Logging | $0.00 |
| Artifact Registry | $0.07 |
| Cloud Build / Scheduler / Run jobs / Monitoring | $0.00 |
| AWS S3 + CloudFront (SPA) | $0.00 |
| **Total** | **$17.39** |

Egress is the largest line (55% of spend) and the single variable that governs
the real capacity ceiling: with ~$12 available for egress after everything else,
the free 100 GiB plus paid egress funds **≈ 202 GB/mo, ≈ 1,680 study opens/mo**
at the 120 MB/open budget — ~1.44 cents of egress per study opened (§9.3).

## Cost cliffs, in the order they bite (§9.5)

1. **Egress** — proportional to how much radiologists read; nothing in the
   software reduces it. Mitigated by D17/D18 low-bandwidth progressive loading
   (default), which cuts bytes/open ~58%.
2. **Backup transfer + cross-region storage** — 14% of spend, scales linearly
   with ingest; the price of §5.6.1 being a real backup. A dual-region bucket is
   rejected because an accidental lifecycle rule applies to both halves.
3. **Cloud Run vCPU-seconds** — the first free tier exhausted on request volume
   (worklist polling puts it at 78% of the 180K free vCPU-s). `min-instances=0`
   keeps idle at $0.

## What is deliberately not spent

- **CMEK** — Google-managed AES-256 is BAA-covered and free; CMEK adds a KMS
  key + ops for a rotation control we have no requirement for (added only on a
  customer contract).
- **VPC-SC** — a meaningful perimeter needs a Serverless VPC Access connector
  (~$7–9/mo, always-on), violating scale-to-zero. Deferred (§5.8).
- **AWS PHI path** — DICOM pixels stay on GCS; AWS holds only the SPA (~$0.00,
  three orders of magnitude inside a permanent free tier).
