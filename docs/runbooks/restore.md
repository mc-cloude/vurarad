# Runbook — Firestore restore and disaster recovery

The tested recovery path for vuraRAD. Point-in-time recovery is **not** a
backup and is not treated as one: PITR keeps 7 days of Firestore history
in-place, so it protects against an operator bad-write but not against a
deleted database, a compromised project, or a ransomware-style mass delete.
This runbook is the real path.

**Objectives (commitments, not aspirations):**

| Objective | Value | Basis |
|---|---|---|
| RPO — Firestore + DICOM | 24 h | nightly export / Storage Transfer |
| RPO — audit trail | ~0 | streamed to the bucket-locked, immutable `vurarad-audit` (§5.1) |
| RPO — Firestore operator error | 1 h | PITR (7-day in-place history) |
| RTO — full restore | 4 h | see breakdown below |

## What is backed up, and where

| Asset | Mechanism | Frequency | Retention | Destination |
|---|---|---|---|---|
| Firestore (all collections) | `gcloud firestore export` (Cloud Scheduler → Cloud Run job) | nightly 03:00 UTC | 35 days | `gs://vurarad-backup/firestore-exports/<YYYYMMDD-HHMMSS>/` (versioning on) |
| Firestore | PITR | continuous | 7 days | in-place — operator error only |
| DICOM objects | Storage Transfer Service, incremental | nightly | 35 days, Coldline | `gs://vurarad-backup-dicom` |
| Audit system of record | none needed | — | 6 years (locked) | already bucket-locked and immutable (`gs://vurarad-audit`, §5.1) |
| Reports | covered by the Firestore export | nightly | 35 days | — |
| Terraform state | GCS bucket versioning | per apply | 90 versions | `gs://vurarad-tfstate` |
| Secrets | Secret Manager versions | per rotation | 6 versions | in-place |

Backup buckets live in **`us-east1`**, deliberately separate from the primary
`us-central1`, so a single-region outage or an accidental region-wide lifecycle
rule cannot take both the live data and the backup.

## RTO breakdown (target 4 h)

| Step | Wall-clock | Mechanism |
|---|---|---|
| Import the Firestore export into a new database | ~30–60 min | `gcloud firestore import` (data-volume dependent) |
| Redeploy the Cloud Run revision from the pinned image digest | ~5 min | `gcloud run deploy --image <digest> --no-traffic` |
| Re-point the CloudFront origin at the new Cloud Run service | ~15 min | CloudFront origin change + propagation |
| Transfer DICOM objects back from the backup bucket | ~2 h | `gsutil -m cp -r` / Storage Transfer for 70 GB |

## Restore procedure

> The **quarterly restore drill** (`.github/workflows/restore-drill.yml`)
> automates steps 1–4 against a scratch database and verifies the result with
> `tests/integration/test_restore_smoke.py`. Run the drill to prove this path;
> the steps below are the production cutover, performed in a maintenance window.

1. **Identify the latest good export.**
   ```bash
   gcloud storage ls -d 'gs://vurarad-backup/firestore-exports/' | sort | tail -1
   ```
   Verify the chosen folder contains an `ALL_NAMESPACES_KIND`/`OVERALL_EXPORT_METADATA`
   structure. If the latest export is suspect, walk back by date.

2. **Create the recovery database.**
   ```bash
   gcloud firestore databases create \
     --database=vurarad-restored --location=us-central1 --project=$PROJECT
   ```

3. **Import the export.**
   ```bash
   gcloud firestore import gs://vurarad-backup/firestore-exports/<LATEST>/ \
     --database=vurarad-restored --project=$PROJECT
   ```
   This is the long step (see RTO table). It blocks until complete.

4. **Deploy the pinned image against the restored database** (`--no-traffic`).
   ```bash
   PROD_IMAGE=$(gcloud run services describe vurarad-api --region=us-central1 \
     --project=$PROJECT --format='value(template.containers[0].image)')
   gcloud run deploy vurarad-api-restored \
     --image="$PROD_IMAGE" --region=us-central1 --project=$PROJECT \
     --service-account=vurarad-run@$PROJECT.iam.gserviceaccount.com \
     --no-traffic --no-allow-unauthenticated \
     --set-env-vars="...,FIRESTORE_DATABASE=vurarad-restored"
   ```
   `gcloud run deploy` waits for the `/readyz` startup probe, so a green deploy
   means the image boots against the restored data.

5. **Verify the restored data** before shifting traffic.
   ```bash
   RESTORE_SMOKE_FIRESTORE_DB=vurarad-restored \
   RESTORE_SMOKE_CREDS_PATH=<creds.json> \
   GCP_PROJECT_ID=$PROJECT \
   pytest tests/integration/test_restore_smoke.py -q
   ```
   The smoke test asserts: study count > 0, report count ≤ study count, worklist
   index consistency (status index == full scan), and audit-chain continuity.

6. **Re-point CloudFront** at the new Cloud Run service and shift Cloud Run
   traffic to the restored revision. This is a DNS/origin switch, not a data
   event — the old and new databases share no data.

7. **Transfer DICOM objects back** from `gs://vurarad-backup-dicom` to
   `gs://vurarad-dicom` (or to the recovery region's DICOM bucket) via Storage
   Transfer Service or `gsutil -m cp -r`.

8. **Watch for 24 hours.** Rollback is repointing the origin back at the
   previous service — no data is destroyed during cutover.

## Rollback

Repoint the CloudFront origin and shift Cloud Run traffic back to the previous
revision. The restored database can be retained for forensics and deleted once
the cutover is confirmed stable (it is a scratch resource, not the system of
record).

## What this runbook does not cover

- **Audit restore.** The audit system of record (`gs://vurarad-audit`) is
  bucket-locked and immutable; it is never restored from a backup because it is
  never deleted. Restoring a locked bucket's contents is impossible to undo —
  deliberate, that is the point.
- **Cross-border / residency-aware restore.** For `africa` / `onprem` tenants
  the recovery database and DICOM transfer must stay inside the tenant's
  residency zone (§5.11). The same steps apply with region selection constrained
  by `ResidencyPolicy`.
