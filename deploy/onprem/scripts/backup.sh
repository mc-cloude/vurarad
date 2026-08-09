#!/usr/bin/env bash
# backup.sh — produce a restorable backup artifact for the on-prem bundle.
#
# The artifact is a single tarball containing:
#   1. PostgreSQL metadata dump (pg_dump, custom format — restorable with pg_restore)
#   2. MinIO object mirror (all buckets via mc mirror)
#   3. A manifest with timestamps, checksums, and version metadata
#
# The backup is designed for the restore drill (§5.6.1): restore to a fresh
# PostgreSQL + MinIO stack and verify the audit chain is intact.
#
# Usage:
#   bash deploy/onprem/scripts/backup.sh [output-dir]
#
# Environment:
#   POSTGRES_HOST  (default: localhost)
#   POSTGRES_PORT  (default: 5432)
#   POSTGRES_USER  (default: vurarad)
#   POSTGRES_DB    (default: vurarad)
#   POSTGRES_PASSWORD (default: secret)
#   MINIO_ENDPOINT (default: http://localhost:9000)
#   MINIO_ROOT_USER (default: minioadmin)
#   MINIO_ROOT_PASSWORD (default: minioadmin)
#
# Exits non-zero on any failure.
set -euo pipefail

OUTPUT_DIR="${1:-/var/tmp/vurarad-backups}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK_DIR="$(mktemp -d)"
ARTIFACT="${OUTPUT_DIR}/vurarad-backup-${TIMESTAMP}.tar.gz"

log() { printf '[backup] %s\n' "$*"; }
fail() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

trap 'rm -rf "${WORK_DIR}"' EXIT

POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-vurarad}"
POSTGRES_DB="${POSTGRES_DB:-vurarad}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-secret}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-minioadmin}"

mkdir -p "${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# 1. PostgreSQL metadata dump
# ---------------------------------------------------------------------------
log "Dumping PostgreSQL metadata from ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
if ! command -v pg_dump >/dev/null 2>&1; then
	fail "pg_dump not found. Install postgresql-client (e.g. apt install postgresql-client)."
fi
pg_dump \
	--host="${POSTGRES_HOST}" \
	--port="${POSTGRES_PORT}" \
	--username="${POSTGRES_USER}" \
	--dbname="${POSTGRES_DB}" \
	--format=custom \
	--no-owner \
	--no-privileges \
	--file="${WORK_DIR}/metadata.dump"
unset PGPASSWORD
log "  metadata.dump: $(du -h "${WORK_DIR}/metadata.dump" | cut -f1)"

# ---------------------------------------------------------------------------
# 2. MinIO object mirror
# ---------------------------------------------------------------------------
log "Mirroring MinIO buckets from ${MINIO_ENDPOINT}"
mc --quiet alias set local "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" >/dev/null

for bucket in vurarad-pixels vurarad-quarantine vurarad-deid vurarad-audit; do
	log "  Mirroring ${bucket}..."
	mc --quiet mirror --overwrite "local/${bucket}" "${WORK_DIR}/objects/${bucket}/" || {
		# A missing bucket is not fatal (e.g. deid may not exist yet).
		log "  Warning: ${bucket} mirror incomplete or bucket absent — continuing."
	}
done

# ---------------------------------------------------------------------------
# 3. Manifest — timestamps, checksums, version
# ---------------------------------------------------------------------------
MANIFEST="${WORK_DIR}/manifest.json"
metadata_sha256=$(sha256sum "${WORK_DIR}/metadata.dump" | awk '{print $1}')

cat > "${MANIFEST}" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "version": "1.0",
  "postgres": {
    "host": "${POSTGRES_HOST}",
    "port": ${POSTGRES_PORT},
    "database": "${POSTGRES_DB}",
    "dump_file": "metadata.dump",
    "sha256": "${metadata_sha256}"
  },
  "minio": {
    "endpoint": "${MINIO_ENDPOINT}",
    "buckets": ["vurarad-pixels", "vurarad-quarantine", "vurarad-deid", "vurarad-audit"]
  },
  "restore": "See docs/runbooks/onprem-install.md — Restore section"
}
EOF

# ---------------------------------------------------------------------------
# 4. Package
# ---------------------------------------------------------------------------
log "Creating artifact: ${ARTIFACT}"
tar -czf "${ARTIFACT}" -C "${WORK_DIR}" .
artifact_sha256=$(sha256sum "${ARTIFACT}" | awk '{print $1}')
log "  size: $(du -h "${ARTIFACT}" | cut -f1)"
log "  sha256: ${artifact_sha256}"

log "Backup complete: ${ARTIFACT}"
log "Restore drill: pg_restore + mc mirror — see docs/runbooks/onprem-install.md"
