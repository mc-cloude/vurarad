# Cutover runbook — vuraRAD WP8 migration

The ordered procedure for cutting traffic from the legacy vuraRAD deployment to
the new system after the WP8 migration (`app/tools/migrate_v1.py`) has run.

The migration is **additive**: legacy `studies/{StudyInstanceUID}` documents and
legacy objects are left in place under their original keys — nothing is deleted
during cutover. The old and new systems share no database, so cutover is a
DNS/origin switch, not a data event.

## Prerequisites

- The new Cloud Run revision is deployed with `--no-traffic`.
- The migration has been run against production Firestore in a maintenance
  window (see `app/tools/migrate_v1.py`). Legacy documents are untouched.
- `python -m app.tools.verify_migration` is available and pointed at the
  manifest written by the migration (`migration-manifest.json` by default).

## 1. Verification gate

Run the independent verification gate. **It must exit 0 before any traffic is
shifted.** A non-zero exit blocks cutover — do not proceed, do not override.

```bash
python -m app.tools.verify_migration --manifest migration-manifest.json
```

The gate re-derives migrated counts from the real Firestore + object store and
compares them against the manifest:

- study count, series count, instance count, report count, and object count in
  `vurarad-dicom` against the legacy source;
- report count and **status distribution** (DRAFT / REPORTED / SIGNED) match;
- no migrated series is `stackOrderConfidence: UNVERIFIED` unless the source
  genuinely lacks position data;
- no fabricated legacy field name (`ai_triage`, `ai_confidence`,
  `accuracy_tier`, radiogenomics / BigQuery artefacts) survives anywhere in the
  migrated Firestore data.

If any check fails, investigate and re-run the migration (it is resumable — it
continues from its checkpoint rather than duplicating), then re-run the gate.
Do not shift traffic until the gate is green.

## 2. Post-deploy smoke check

Run the production smoke check (§10.5) against the new revision directly (its
private URL), before shifting any public traffic: auth, a study search, a
series read, and an audit read. Any failure stops the cutover.

## 3. Traffic shift

1. Point the **CloudFront origin** at the new Cloud Run service.
2. Shift **Cloud Run traffic** to the new revision
   (`gcloud run services update-traffic vurarad-api --region us-central1
   --to-latest`).
3. Repoint the **DNS / API base URL** the SPA uses at the new service.

CloudFront cache invalidation for the SPA shell:

```bash
aws cloudfront create-invalidation \
  --distribution-id $CLOUDFRONT_DIST_ID --paths "/index.html" "/"
```

## 4. Seven-day observation window

Watch for 7 days. Monitor:

- error rate and latency on the new Cloud Run revision;
- ingest job success rate (no FAILED jobs that were SUCCEEDED on legacy);
- report sign events and audit-chain continuity;
- any client reports of stacks rendering out of order (a `stackIndex` defect
  would surface here).

A "clean day" is a day with no migration-attributable incidents.

## 5. Rollback

**Rollback is repointing the CloudFront origin and the DNS/API base URL back to
the legacy service — no data rollback is required**, because the migration is
additive and the legacy store is not mutated.

To roll back:

1. Repoint the CloudFront origin back to the legacy service.
2. Revert Cloud Run traffic to the previous (legacy-facing) revision, or shift
   the DNS/API base URL back to the legacy service.
3. Invalidate the CloudFront cache for `/index.html` and `/`.

Because no legacy document or object was deleted or rewritten, the legacy
service resumes serving from its unchanged store immediately. Any studies
ingested into the new system *during* the observation window remain in the new
store; they are not lost and do not block rollback.

## 6. Legacy deletion (WP21) — not before 7 clean days

**Legacy deletion (WP21) does not begin until 7 clean days have passed.** Only
after seven consecutive clean observation days, in a separate, audited step
with its own `PATIENT_ERASED`-style tombstone, delete the legacy
`studies/*` documents, the `triage_history` subcollection, and the legacy
objects. Deletion is its own work package (WP21) and is never part of cutover.

## Quick reference

| Step | Command / action | Gate |
|---|---|---|
| Verify | `python -m app.tools.verify_migration` | must exit 0 |
| Smoke | run §10.5 smoke check on the new revision | must pass |
| Shift | CloudFront origin → new service; Cloud Run `--to-latest`; DNS/API repoint | — |
| Observe | 7 days of monitoring | 7 clean days required |
| Rollback | repoint CloudFront origin + DNS/API back to legacy | no data rollback |
| Delete (WP21) | delete legacy `studies/*` + `triage_history` + legacy objects | only after 7 clean days |
