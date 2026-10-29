# HIPAA — technical control matrix

The **technical** half of the HIPAA Security Rule, mapped to what the code and
infrastructure actually deliver. The **organizational** half — BAAs, risk
analysis, workforce training, breach notification — is in
`docs/hipaa-organizational.md`, and its first line is load-bearing: **until the
Google Cloud BAA and the AWS BAA are both signed and accepted, none of the
controls below make the system HIPAA-compliant.** They make compliance
*achievable*; the BAAs make the perimeter real.

Every row below names a concrete implementation that exists in the repository
today (WP1 application code or WP2 Terraform) — there are no "planned" or empty
implementation cells, per acceptance criterion 14.

## Technical safeguards — §164.312

| Control | Citation | Implementation | Verification |
|---|---|---|---|
| Access control — unique-user identification & role-based access | §164.312(a)(2)(i) | `app/core/capabilities.py` defines `Capability` + `Role` enums and `has_capability()`; `app/core/auth.py` `require_capability` dependency denies by default (401 missing token, 403 unrecognised role — never falls back to a default role) | `tests/unit/test_capability_matrix.py`, `tests/unit/test_route_security.py` |
| Access control — emergency access (break-glass) | §164.312(a)(2)(ii) | `Capability.BREAK_GLASS` in `capabilities.py`, gated by `require_capability` | `tests/unit/test_capability_matrix.py` |
| Access control — automatic session termination | §164.312(a)(2)(iii) | Short-lived Identity Platform ID tokens; `session_idle_timeout_seconds` and `token_revocation_check_seconds` configured in `app/core/config.py`; token verification in `auth.py` rejects expired/revoked tokens | `app/core/config.py` Settings, `app/core/auth.py` `TokenVerifier` |
| Audit controls — record examination, use, disclosure | §164.312(b) | `app/services/audit_service.py` `AuditService.record` writes every examined/created/changed/signed/exported/erased event; `app/models/audit.py` `AuditEvent` carries `seq`/`prev_hash`/`hash`/`actor`/`second_factor` | `tests/unit/test_verify_audit_chain.py`, `app/models/audit.py` |
| Integrity — tamper-evident audit chain | §164.312(c)(1) | `app/models/audit.py` `compute_hash`/`seal`/`verify_chain`/`genesis` SHA-256 chain; `AuditService.record` reads the chain head, seals, and writes inside the caller's transaction with **no try/except** (a write failure aborts the mutation) | `tests/unit/test_verify_audit_chain.py` |
| Person/entity authentication | §164.312(d) | `app/core/auth.py` `TokenVerifier.verify` validates Identity Platform ID tokens; `SecondFactorVerifier.verify_totp` enforces TOTP; `AuthenticatedUser.is_mfa_verified` | `app/core/auth.py`, `app/models/errors.py` `MFA_REQUIRED`/`MFA_CHALLENGE_FAILED` |
| Transmission security — encryption in transit | §164.312(e)(1) | Cloud Run terminates TLS 1.2+ (HTTPS-only, HTTP redirected by platform); `app/core/security.py` `SecurityHeadersMiddleware` sets `Strict-Transport-Security: max-age=63072000; includeSubDomains`; GCS signed URLs are HTTPS-only | `app/core/security.py`, `infra/run.tf` ingress |

## Immutable audit trail — §164.312(b) and the identity boundary (§5.1)

The audit system of record is **not** in Firestore (Firestore IAM is
database-scoped, not collection-scoped, so the runtime identity that deletes
studies could delete audit records). It is a bucket-locked GCS bucket written
by a Cloud Logging sink under a Google-managed service agent.

| Control | Implementation | Verification |
|---|---|---|
| WORM system of record | `infra/storage.tf` `google_storage_bucket.audit` with `retention_policy { is_locked = true }` 2,190-day retention and `lifecycle { prevent_destroy = true }`; once locked the period cannot be shortened and objects cannot be deleted before expiry — by anyone, including a project owner | `terraform plan` is idempotent; `gcloud storage buckets describe gs://vurarad-audit --format='value(retentionPolicy.isLocked,retentionPolicy.retentionPeriod)'` |
| Runtime identity cannot touch the system of record | `infra/iam.tf`: `vurarad-run@` holds **no** `storage.objects.create/update/delete` binding on `vurarad-audit` — only the Cloud Logging sink's Google-managed writer writes there | `gcloud projects get-iam-policy` + bucket policy show no `objectAdmin`/`objectCreator` for `vurarad-run@` on the audit bucket (criterion 3) |
| Write path is a log sink, not the application | `infra/logging.tf` `google_logging_project_sink.audit` routes `logName="…/logs/vurarad-audit"` to the bucket | `infra/logging.tf` |
| Cross-store chain verification | `app/tools/verify_audit_chain.py` compares the bucket against the Firestore `audit_mirror` weekly; a record in the bucket but missing from the mirror is the tamper case; emits `AUDIT_CHAIN_BROKEN` to stdout (captured as `jsonPayload`) — the verifier holds no Logging write grant, preserving the verifier-is-not-writer boundary | `tests/unit/test_verify_audit_chain.py` (22 tests); `infra/scheduler.tf` weekly job as `vurarad-audit-verifier@` |
| 6-year retention | Bucket retention 2,190 days (`storage.tf`); `audit_mirror` Firestore TTL 2,190 days (`infra/firestore.tf`) | `infra/storage.tf`, `infra/firestore.tf` |
| Read-only verifier identity | `infra/iam.tf` `vurarad-audit-verifier@` holds only `storage.objectViewer` on the audit bucket + `datastore.entities.get/list` — read-only in both stores | `infra/iam.tf`, `infra/scheduler.tf` |

## Administrative & infrastructure controls — §164.308, §164.310(a/d)

| Control | Citation | Implementation | Verification |
|---|---|---|---|
| Audit log monitoring & alerting | §164.308(a)(1)(ii)(D) | `infra/logging.tf`: 11 alert policies incl. `AUDIT_CHAIN_BROKEN` (pages), nightly-backup-failure, `min-instances != 0` drift, sink delivery errors, `MFA_CHALLENGE_FAILED` rate, `AUTH_LOCKOUT` | `infra/logging.tf` metric + alert policies |
| Contingency / data backup | §164.308(a)(7)(i) | `infra/scheduler.tf` nightly `gcloud firestore export` → `gs://vurarad-backup` (35-day retention); incremental DICOM `google_storage_transfer_job` → `gs://vurarad-backup-dicom` | `infra/scheduler.tf`, `infra/storage.tf` |
| Disaster recovery & tested restore | §164.308(a)(7)(ii)(B) | `infra/firestore.tf` PITR (7-day, operator-error only); `.github/workflows/restore-drill.yml` quarterly drill: scratch DB ← latest export, pinned image booted `--no-traffic`, `tests/integration/test_restore_smoke.py` green, RTO recorded in `docs/runbooks/restore-drill-log.md`; RPO 24 h / RTO 4 h stated in `docs/runbooks/restore.md` | `tests/integration/test_restore_smoke.py`, `docs/runbooks/restore.md` |
| Backup region isolation | §164.308(a)(7)(ii)(D) | Backup buckets in `us-east1`, deliberately separate from the primary `us-central1` (criterion 13); `infra/variables.tf` `backup_region` | `infra/storage.tf`, `infra/variables.tf` |
| Encryption at rest | §164.312(a)(2)(iv) | Google-managed AES-256 on Firestore, GCS, and Secret Manager — BAA-covered, no configuration. CMEK is a documented cost trade-off (not a gap): added only when a customer contract demands customer-managed keys | `infra/firestore.tf`, `infra/storage.tf`, `infra/secrets.tf` |
| Least-privilege service accounts | §164.308(a)(4) | `infra/iam.tf`: 4 GCP SAs with per-bucket IAM bindings, no project-level `Editor`; `vurarad-run@` scoped per bucket; `vurarad-deploy@` has no Firestore/Storage/aiplatform | `infra/iam.tf` |
| No long-lived credentials | §164.308(a)(5)(ii)(D) | `infra/iam.tf` org policy `iam.disableServiceAccountKeyCreation` (enforced); CI federates via WIF; signed URLs use `signBlob`; `infra/aws/oidc.tf` GitHub OIDC role — **no AWS access keys** | `gcloud iam service-accounts keys list` returns none; `aws iam list-access-keys` returns none (criterion 5) |
| CI cannot read PHI | §164.308(a)(3)(ii)(B) | `infra/iam.tf` `vurarad-deploy@` has no Firestore/Storage/aiplatform/firebaseauth role; `infra/aws/oidc.tf` CDN role has no `s3:GetObject` | `infra/iam.tf`, `infra/aws/oidc.tf` (criterion 6) |
| Data Access audit-log scoping | §164.312(b) | `infra/logging.tf`: `DATA_WRITE` enabled, `DATA_READ` **not** enabled for firestore + storage (a blanket DATA_READ would emit one entry per instance fetch; the app-level audit log already records reads at the correct granularity) | `infra/logging.tf` `google_project_iam_audit_config` |
| Public access prevention | §164.310(d), §164.312(e) | `infra/storage.tf`: uniform bucket-level access + `public_prevention = "enforced"` on every bucket; `infra/aws/s3.tf` Block Public Access fully on + `infra/aws/cloudfront.tf` OAC as the only read principal for `vurarad-spa` | `gcloud storage buckets describe` public-access-prevention `enforced`; anonymous `curl` returns 403 (criterion 8) |
| Soft-delete cost/retention trap | §164.308(a)(7) | `infra/storage.tf`: `soft_delete_policy { retention_duration_seconds = 0 }` on `vurarad-quarantine` and `vurarad-backup-dicom` (GCS enables 7-day soft delete by default, which would double the quarantine line) | `infra/storage.tf`, drift check in `logging.tf` |
| Spend monitoring | §164.308(a)(8) | `infra/monitoring.tf`: $20 project / $15 Vertex / $10 Storage budgets with email alerts (budgets alert, they do not cap; the app-level metering ceiling is the hard stop) | `infra/monitoring.tf` |
| Availability & latency SLOs | §164.308(a)(7)(ii)(C) | `infra/monitoring.tf`: `/healthz` uptime check (60 s); API availability 99.5%/30 d; request latency p95 < 300 ms/7 d | `infra/monitoring.tf` |
| Scale-to-zero drift detection | §164.308(a)(8) | `infra/run.tf` `min_instance_count = 0` / `max = 10`; `infra/logging.tf` alert on `min-instances != 0` drift; `terraform plan` in CI | `infra/run.tf`, `infra/logging.tf` (criterion 11) |
| Secret management | §164.312(a)(2)(iv) | `infra/secrets.tf`: 2 secrets (Identity Platform config, AWS CDN role ARN) pinned to numeric versions, mounted as Cloud Run env vars so a missing secret fails the revision at startup; **no HMAC signing key** (report integrity is the hash-chained audit trail) | `infra/secrets.tf`, `infra/run.tf` |

## Application-layer controls

| Control | Implementation | Verification |
|---|---|---|
| PHI-safe structured logging | `app/core/logging.py` `JsonFormatter` + `PhiRedactionFilter` drops any record whose message contains a `PHI_FIELD_NAMES` token and emits a `LOGGING_POLICY_VIOLATION` counter (fail-closed); `app/core/redaction.py` `redact`/`is_phi_key`/`hash_identifier` | `app/core/logging.py`, `app/core/redaction.py` |
| Rate limiting on sensitive paths | `app/core/ratelimit.py` `RateLimiter` with `RateLimitBucket` (auth, sign, export, admin, mfa, imaging, import) | `app/core/ratelimit.py` |
| Idempotent mutations | `app/core/idempotency.py` prevents duplicate writes on retried requests | `app/core/idempotency.py` |
| Multi-factor authentication | `app/core/auth.py` `SecondFactorVerifier.verify_totp`; `require_second_factor = True` and `mfa_verification_seconds = 300` in `app/core/config.py` (fresh 2FA window for signing/erasure/export) | `app/core/auth.py`, `app/core/config.py` |
| Deny-by-default authorisation | `app/core/auth.py` `require_capability` returns 401 (no token) / 403 (invalid or unrecognised role) — never a defaulted role | `app/core/auth.py`, `tests/unit/test_route_security.py` |

## Deliberate deferrals — stated, not omitted

| Control | Status & rationale | Compensating controls (in place) |
|---|---|---|
| VPC Service Controls perimeter | **Deliberately deferred** (§5.8): VPC-SC requires an organization node (a standalone project cannot create a perimeter); with one Cloud Run service and no VPC/VM/GKE footprint there are no exfiltration paths to guard; a meaningful perimeter needs a Serverless VPC Access connector (~$7–9/mo, always-on) that violates the scale-to-zero budget. **Trigger to revisit:** an org node exists, OR a second PHI-holding service is added, OR a customer contract requires perimeter controls. | Least-privilege IAM (`iam.tf`), uniform bucket-level access + public-access prevention (`storage.tf`), no service-account keys (`iam.tf` org policy), audited application-level access (`audit_service.py`) |
