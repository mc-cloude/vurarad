#!/usr/bin/env bash
# bootstrap.sh — one-time initialisation for the on-prem bundle.
#
# Creates MinIO buckets (pixels, quarantine, deid, audit) and applies the
# object-lock retention policy to the audit bucket in COMPLIANCE mode.
# Also verifies the PostgreSQL metadata backend is reachable and the API
# readyz endpoint reports the audit store as immutable.
#
# Usage:
#   bash deploy/onprem/scripts/bootstrap.sh
#
# Environment:
#   MINIO_ENDPOINT   (default: http://localhost:9000)
#   MINIO_ROOT_USER  (default: minioadmin)
#   MINIO_ROOT_PASSWORD (default: minioadmin)
#   AUDIT_RETENTION_DAYS (default: 2555  ≈ 7 years)
#   API_URL          (default: http://localhost:8080)
#
# Exits non-zero on any failure.  Idempotent — safe to re-run.
set -euo pipefail

MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:-minioadmin}"
AUDIT_RETENTION_DAYS="${AUDIT_RETENTION_DAYS:-2555}"
API_URL="${API_URL:-http://localhost:8080}"

log() { printf '[bootstrap] %s\n' "$*"; }
fail() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# MinIO bucket creation + object-lock configuration
# ---------------------------------------------------------------------------
log "Configuring MinIO at ${MINIO_ENDPOINT}"

MC="mc --quiet"
$MC alias set local "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" >/dev/null

# Standard buckets (no object lock)
for bucket in vurarad-pixels vurarad-quarantine vurarad-deid; do
	log "Creating bucket: ${bucket}"
	$MC mb --ignore-existing "local/${bucket}"
done

# Audit bucket — must already exist with object-lock enabled (created by
# minio-init in docker-compose).  Apply the COMPLIANCE retention policy.
log "Configuring audit bucket (object lock, COMPLIANCE, ${AUDIT_RETENTION_DAYS} days)"
$MC mb --ignore-existing --with-lock "local/vurarad-audit"

# Apply retention policy in COMPLIANCE mode.
# mc retention set requires the bucket to have object lock enabled.
$MC retention set --default compliance "${AUDIT_RETENTION_DAYS}d" "local/vurarad-audit" 2>/dev/null || {
	log "Warning: could not set retention via 'mc retention set'."
	log "Applying via s3 api (put-object-lock-configuration)..."
	# Fallback: use aws-cli-style configuration via mc admin
	$MC support object-lock set --compliance --days "${AUDIT_RETENTION_DAYS}" "local/vurarad-audit" 2>/dev/null || \
		fail "Could not configure object lock on audit bucket. Ensure MinIO supports object lock."
}

# Verify object lock is enabled on the audit bucket
lock_config=$($MC support object-lock info "local/vurarad-audit" 2>/dev/null || echo "")
if echo "${lock_config}" | grep -qi "compliance"; then
	log "Audit bucket object lock: COMPLIANCE mode confirmed."
else
	log "Warning: could not verify object lock mode via mc. Checking via API readyz..."
fi

# ---------------------------------------------------------------------------
# Verify API readiness — readyz must report audit_store_immutable=true
# ---------------------------------------------------------------------------
log "Waiting for API readiness at ${API_URL}/readyz ..."

ready=false
for i in $(seq 1 30); do
	resp=$(curl -sf "${API_URL}/readyz" 2>/dev/null || echo "")
	if echo "${resp}" | grep -q '"audit_store_immutable":true'; then
		ready=true
		break
	fi
	sleep 2
done

if [ "${ready}" = "true" ]; then
	log "API readyz: audit store is immutable (object lock confirmed)."
else
	fail "API readyz did not confirm audit store immutability after 60s."
fi

log "Bootstrap complete."
log "  - Buckets: vurarad-pixels, vurarad-quarantine, vurarad-deid, vurarad-audit"
log "  - Audit object lock: COMPLIANCE, ${AUDIT_RETENTION_DAYS} days"
log "  - API readyz: OK"
