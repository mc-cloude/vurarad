# On-Prem Installation Runbook

This runbook covers installing, upgrading, backing up, and licensing the
vuraRAD on-prem bundle — the compliance deployment for strict-residency
markets (e.g. Kenya under a strict ODPC reading).

## Architecture

| Service | Image | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine` | Metadata backend (`MetadataStore` — studies, series, ingest jobs, audit mirror) |
| `minio` | `minio/minio:latest` | S3-compatible object store (pixels, quarantine, deid, audit) |
| `minio-init` | `minio/mc:latest` | One-shot bucket creation with object lock |
| `firestore-emu` | `mtlynch/firestore-emulator` | DICOM metadata store (temporary — replaced by PG in a future release) |
| `api` | Built from `Dockerfile` | vuraRAD FastAPI application |
| `caddy` | `caddy:2-alpine` | Reverse proxy, TLS termination |

No outbound internet is required for steady-state operation. Licence
verification is offline, rule sets are local files, and segmentation models
are baked into the image.

## Prerequisites

- Docker Engine 24+ and Docker Compose v2
- A host with at least 4 GB RAM and 50 GB disk (adjust for study volume)
- The local OIDC provider's shared secret (HS256) or JWKS file (RS256)
- PostgreSQL credentials for the metadata database

## 1. Installation

### 1.1 Configure environment

Copy the example environment file and edit it:

```bash
cp deploy/onprem/.env.example deploy/onprem/.env
```

Set at minimum:

| Variable | Example | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | `<strong-password>` | PostgreSQL metadata DB password |
| `MINIO_ROOT_USER` | `minioadmin` | MinIO root user |
| `MINIO_ROOT_PASSWORD` | `<strong-password>` | MinIO root password |
| `OIDC_HMAC_SECRET` | `<shared-secret>` | HS256 signing secret for local OIDC |
| `HTTP_PORT` | `8080` | Host port for Caddy |

For RS256 (recommended for production), set `OIDC_JWKS_PATH` to a path inside
the API container where the JWKS file is mounted, and leave `OIDC_HMAC_SECRET`
unset.

### 1.2 Bring up the stack

```bash
docker compose -f deploy/onprem/docker-compose.yml up -d
```

### 1.3 Bootstrap buckets and verify readiness

```bash
bash deploy/onprem/scripts/bootstrap.sh
```

This creates MinIO buckets (pixels, quarantine, deid, audit), applies the
object-lock retention policy to the audit bucket in **COMPLIANCE** mode, and
verifies the API `readyz` endpoint reports `audit_store_immutable: true`.

### 1.4 Verify the deployment

```bash
# Liveness
curl -sf http://localhost:8080/healthz

# Readiness (must report audit_store_immutable: true)
curl -sf http://localhost:8080/readyz
```

## 2. Licence

The on-prem licence is verified **offline** — no outbound call to a licence
server.  The licence file is a signed JSON document placed at
`/etc/vurarad/licence.json` inside the API container (mount it as a volume).

The licence contains:

- Hospital name and site ID
- Entitled study volume (per month)
- Expiry date
- Signature (verified with the vendor's public key, baked into the image)

When the licence expires or the volume is exceeded, the API returns `402
LICENCE_EXPIRED` or `429 LICENCE_VOLUME_EXCEEDED` respectively.  Renewal is
offline: the vendor issues a new signed file, the operator replaces it, and
the API picks it up on the next health check.

## 3. Backup

### 3.1 Create a backup

```bash
bash deploy/onprem/scripts/backup.sh /var/tmp/vurarad-backups
```

The script produces a single tarball (`vurarad-backup-<timestamp>.tar.gz`)
containing:

1. **PostgreSQL metadata dump** — `pg_dump --format=custom`, restorable with
   `pg_restore`.
2. **MinIO object mirror** — all four buckets mirrored via `mc mirror`.
3. **Manifest** — timestamps, SHA-256 checksums, and version metadata.

### 3.2 Restore drill (once per release)

The restore drill validates that a backup artifact can be restored to a fresh
stack.  Run it once per release and append the result to
`docs/runbooks/restore-drill-log.md`.

```bash
# 1. Start a fresh stack (different port to avoid collision)
HTTP_PORT=8090 docker compose -f deploy/onprem/docker-compose.yml \
  -p vurarad-restore up -d

# 2. Restore PostgreSQL metadata
pg_restore --host=localhost --port=5433 --username=vurarad \
  --dbname=vurarad --clean --if-exists metadata.dump

# 3. Restore MinIO objects
mc mirror /path/to/objects/ local/

# 4. Verify the audit chain
bash deploy/onprem/scripts/verify_audit_chain.sh

# 5. Tear down the restore stack
docker compose -f deploy/onprem/docker-compose.yml -p vurarad-restore down -v
```

## 4. Audit Chain Verification

```bash
bash deploy/onprem/scripts/verify_audit_chain.sh
```

This reads all audit events from PostgreSQL, reconstructs the hash chain, and
verifies every `prev_hash` link and stored hash.  It exits non-zero on any
chain-break.

## 5. Upgrade

### 5.1 One minor version step (including metadata migration)

```bash
# 1. Back up the current state
bash deploy/onprem/scripts/backup.sh /var/tmp/vurarad-backups

# 2. Pull the new image
docker compose -f deploy/onprem/docker-compose.yml pull

# 3. Run metadata migrations (if any)
#    The API applies schema migrations automatically on startup via the
#    PostgresMetadataStore._ensure_schema() method.  For manual migrations:
docker compose -f deploy/onprem/docker-compose.yml exec api \
  python -c "from app.repositories.postgres_impl.store import PostgresMetadataStore; \
  import asyncio; asyncio.run(PostgresMetadataStore.connect(dsn='$POSTGRES_DSN', init_schema=True))"

# 4. Rolling restart — zero downtime with caddy health checks
docker compose -f deploy/onprem/docker-compose.yml up -d --no-deps api

# 5. Verify readiness
curl -sf http://localhost:8080/readyz
bash deploy/onprem/scripts/verify_audit_chain.sh
```

### 5.2 Rollback

If the upgrade fails:

```bash
# 1. Roll back to the previous image
docker compose -f deploy/onprem/docker-compose.yml up -d --no-deps api  # previous tag

# 2. Restore metadata from the pre-upgrade backup
pg_restore --host=localhost --username=vurarad --dbname=vurarad \
  --clean --if-exists /var/tmp/vurarad-backups/metadata.dump
```

## 6. No-Egress Verification

To verify the system operates with no outbound internet:

```bash
# Block egress on the compose network
docker network create --internal vurarad-internal
docker compose -f deploy/onprem/docker-compose.yml up -d

# Run the smoke test — it exercises login, STOW, ingest, segmentation,
# and audit verification entirely within the internal network.
bash tests/e2e/onprem_smoke.sh
```

The `--internal` flag prevents outbound traffic from the compose network.  If
any service attempts an outbound call, it fails — which is the test.

## 7. Known Limitations

- **DICOM metadata store**: the DICOM-specific metadata store
  (`FirestoreDicomMetadataStore`) currently uses the Firestore emulator.  A
  PostgreSQL backend is planned for a future release.  The general
  `MetadataStore` (studies, series, ingest jobs, audit) already uses PostgreSQL.
- **Segmentation**: CPU-only segmentation is configured.  GPU segmentation
  requires a MONAI MAP container (not included in this bundle).
- **OIDC provider**: the bundle verifies tokens offline but does not include
  an OIDC provider (e.g. Keycloak).  The hospital deploys its own IdP and
  provides the signing key (JWKS) or shared secret (HS256).
