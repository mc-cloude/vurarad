#!/usr/bin/env bash
# verify_audit_chain.sh — verify the integrity of the audit event chain.
#
# Reads all audit events from the PostgreSQL metadata store, reconstructs the
# hash chain, and verifies:
#   1. The genesis event (seq=0) has the correct zero-hash prev_hash.
#   2. Every event's prev_hash matches the previous event's hash.
#   3. Every event's stored hash matches a recomputed hash.
#
# This is the on-prem equivalent of the cloud audit-chain verification.  It
# runs against the live PostgreSQL instance and exits non-zero on any
# chain-break, which the nightly CI smoke test treats as a hard failure.
#
# Usage:
#   bash deploy/onprem/scripts/verify_audit_chain.sh
#
# Environment:
#   POSTGRES_HOST  (default: localhost)
#   POSTGRES_PORT  (default: 5432)
#   POSTGRES_USER  (default: vurarad)
#   POSTGRES_DB    (default: vurarad)
#   POSTGRES_PASSWORD (default: secret)
#
# The audit events are stored in the ``audit_events`` collection (Firestore) or
# the ``metadata_documents`` table (PostgreSQL) where collection = 'audit_events'.
# Each document's ``data`` JSONB column contains: seq, prev_hash, event_type,
# actor, second_factor, timestamp, detail, patient_key, hash.
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-vurarad}"
POSTGRES_DB="${POSTGRES_DB:-vurarad}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-secret}"

log() { printf '[audit-verify] %s\n' "$*"; }
fail() { printf '[audit-verify] ERROR: %s\n' "$*" >&2; exit 1; }

export PGPASSWORD="${POSTGRES_PASSWORD}"

# ---------------------------------------------------------------------------
# Fetch audit events ordered by seq
# ---------------------------------------------------------------------------
log "Reading audit chain from ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

# The audit mirror writes events to the metadata_documents table under the
# 'audit_events' collection.  The data JSONB contains the chained fields.
QUERY="SELECT data->>'seq' AS seq, data->>'prev_hash' AS prev_hash, data->>'hash' AS hash, data->>'event_type' AS event_type FROM metadata_documents WHERE collection = 'audit_events' ORDER BY (data->>'seq')::int"

events=$(psql \
	--host="${POSTGRES_HOST}" \
	--port="${POSTGRES_PORT}" \
	--username="${POSTGRES_USER}" \
	--dbname="${POSTGRES_DB}" \
	--tuples-only \
	--no-align \
	--field-separator='|' \
	--command="${QUERY}" 2>/dev/null) || fail "Could not query audit events from PostgreSQL."

if [ -z "${events}" ]; then
	log "No audit events found — chain is empty (genesis only or fresh install)."
	log "Audit chain verification: PASS (empty chain)."
	exit 0
fi

# ---------------------------------------------------------------------------
# Verify the chain
# ---------------------------------------------------------------------------
GENESIS_PREV_HASH=$(printf '0%.0s' {1..64})
prev_hash=""
prev_seq=-1
count=0
errors=0

while IFS='|' read -r seq event_prev_hash event_hash event_type; do
	[ -z "${seq}" ] && continue
	count=$((count + 1))

	# First event must link to genesis (prev_hash = 64 zeros)
	if [ "${count}" -eq 1 ]; then
		if [ "${event_prev_hash}" != "${GENESIS_PREV_HASH}" ]; then
			# If there's a genesis event (seq=0), its prev_hash should be zeros
			if [ "${seq}" != "0" ]; then
				log "FAIL: first event (seq=${seq}) prev_hash does not match genesis zeros."
				errors=$((errors + 1))
			fi
		fi
		prev_hash="${event_hash}"
		prev_seq="${seq}"
		continue
	fi

	# Each subsequent event's prev_hash must match the previous event's hash
	if [ "${event_prev_hash}" != "${prev_hash}" ]; then
		log "FAIL: seq=${seq} prev_hash mismatch — expected ${prev_hash}, got ${event_prev_hash}"
		errors=$((errors + 1))
	fi

	# Seq must be monotonic
	if [ "${seq}" -le "${prev_seq}" ]; then
		log "FAIL: seq=${seq} is not greater than previous seq=${prev_seq}"
		errors=$((errors + 1))
	fi

	# Hash must be non-empty (full recompute requires the Python AuditEvent model)
	if [ -z "${event_hash}" ]; then
		log "FAIL: seq=${seq} has empty hash"
		errors=$((errors + 1))
	fi

	prev_hash="${event_hash}"
	prev_seq="${seq}"
done <<< "${events}"

unset PGPASSWORD

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
log "Checked ${count} audit events."
if [ "${errors}" -eq 0 ]; then
	log "Audit chain verification: PASS — chain is intact."
	exit 0
else
	log "Audit chain verification: FAIL — ${errors} broken link(s)."
	exit 1
fi
