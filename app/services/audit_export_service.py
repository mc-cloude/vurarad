"""Audit export service — NDJSON export to a locked bucket with a signed URL.

``export_audit`` materialises a window of audit records as NDJSON, uploads the
file to the ``vurarad-audit-exports`` bucket, produces a 10-minute signed read
URL, and writes an ``AUDIT_EXPORTED`` audit record capturing the actor, window,
record count, SHA-256, reason, and recipient.  Exporting the audit log is
itself audited.
"""

from __future__ import annotations

import hashlib
import json
import time

from ulid import ULID

from app.core.errors import AuditWindowTooWideError
from app.models.admin import AuditExportResponse, AuditFilter
from app.models.audit import AuditEvent
from app.services.audit_query_service import (
    MAX_AUDIT_WINDOW_SECONDS,
    AuditQueryService,
    AuditReadStore,
)
from app.services.audit_service import AuditService
from app.storage.base import ObjectStore

# Signed-URL lifetime for an exported audit file — 10 minutes.
EXPORT_URL_TTL_SECONDS = 600
EXPORT_KEY_PREFIX = "exports/"
EXPORT_CONTENT_TYPE = "application/x-ndjson"

# A generous page size when paging through the window for an export.
_EXPORT_PAGE_SIZE = 5000


class AuditExportService:
    """Produces, uploads, and signs an NDJSON audit export."""

    def __init__(
        self,
        read_store: AuditReadStore,
        export_store: ObjectStore,
        audit_service: AuditService,
    ) -> None:
        self._read_store = read_store
        self._export_store = export_store
        self._audit = audit_service

    async def _collect_events(self, filters: AuditFilter) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        page_token: str | None = None
        query_filters = filters.model_copy(update={"limit": _EXPORT_PAGE_SIZE})
        while True:
            page, page_token = await self._read_store.query_events(query_filters)
            events.extend(page)
            if page_token is None:
                break
            query_filters = query_filters.model_copy(update={"page_token": page_token})
        return events

    @staticmethod
    def _to_ndjson(events: list[AuditEvent]) -> bytes:
        lines: list[str] = []
        for event in events:
            line = json.dumps(
                {
                    "seq": event.seq,
                    "timestamp": event.timestamp,
                    "actor": event.actor,
                    "action": event.event_type,
                    "patientKey": event.patient_key,
                    "studyId": event.detail.get("studyId", ""),
                    "resource": event.detail.get("resource", ""),
                    "details": event.detail,
                    "hash": event.hash,
                    "prevHash": event.prev_hash,
                    "secondFactor": event.second_factor,
                },
                sort_keys=True,
            )
            lines.append(line)
        return ("\n".join(lines) + "\n" if lines else "").encode()

    async def export_audit(
        self,
        filters: AuditFilter,
        *,
        reason: str,
        recipient: str,
        actor: str,
        second_factor: bool,
    ) -> AuditExportResponse:
        if filters.from_ is None or filters.to is None:
            from app.core.errors import SearchFilterRequiredError

            raise SearchFilterRequiredError("Audit export requires from and to bounds")

        window = filters.to - filters.from_
        if window < 0 or window > MAX_AUDIT_WINDOW_SECONDS:
            raise AuditWindowTooWideError("Audit export window must be between 0 and 92 days")

        events = await self._collect_events(filters)
        ndjson = self._to_ndjson(events)
        sha256 = hashlib.sha256(ndjson).hexdigest()
        export_id = f"ax_{ULID()}"
        key = f"{EXPORT_KEY_PREFIX}{export_id}.ndjson"

        await self._export_store.put(key, ndjson, content_type=EXPORT_CONTENT_TYPE)
        signed_url = await self._export_store.generate_signed_read_url(
            key, ttl_seconds=EXPORT_URL_TTL_SECONDS
        )
        expires_at = int(time.time()) + EXPORT_URL_TTL_SECONDS

        await self._audit.record(
            "AUDIT_EXPORTED",
            actor=actor,
            second_factor=second_factor,
            detail={
                "exportId": export_id,
                "from": filters.from_,
                "to": filters.to,
                "recordCount": len(events),
                "sha256": sha256,
                "reason": reason,
                "recipient": recipient,
            },
            patient_key="",
        )

        return AuditExportResponse(
            export_id=export_id,
            signed_url=signed_url,
            expires_at=expires_at,
            record_count=len(events),
            sha256=sha256,
        )


# Re-export so routers can import the query service from one place.
__all__ = ["AuditExportService", "AuditQueryService"]
