# ruff: noqa: B008
"""Findings ingest router — ``POST /studies/{studyId}/findings/ingest`` (§3.15.4).

Accepts a cleared-AI vendor payload, resolves the version-pinned adapter from
the registry, parses it (headers only — DICOM SR uses ``stop_before_pixels``),
normalizes the adapter findings into stored :class:`Finding` records, and
records a ``FINDINGS_INGESTED`` audit event.

Clearance guarantees enforced here and in the normalizer:

- **Cross-tenant → 404, never 403.**  A study that exists in another tenant is
  reported as ``NOT_FOUND`` so existence is not leaked (criterion 6).
- **Idempotent** on ``(tenantId, studyInstanceUid, adapterName, payloadSha256)``.
  The deterministic ingest id is the SHA-256 of that tuple; a replay returns the
  original response with ``idempotent == True`` and writes nothing (criterion 5).
- **Proof-based clearance.**  The vendor identity (FDA K-number / CE mark) comes
  from the registry, never the payload (criterion 2).
- **CADt strip + PHI redaction** happen in the normalizer; the dropped-CADt and
  no-clearance counts are returned and pushed to telemetry (criteria 3, 4).
- **Source retention.**  The raw payload (DICOM SR object) is retained in the
  object store and referenced by each finding's ``provenance.ingestRef``
  (criterion 7).
- ``findings:ingest`` is a PHI capability — admin gets
  ``PHI_ACCESS_FORBIDDEN`` (criterion 8).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.routers.studies_deps import (
    AuditServiceDep,
    DocumentStoreDep,
    require_phi_capability,
)
from app.api.v1.routers.wp12_deps import FindingServiceDep, StudyRecordDep
from app.core.auth import AuthenticatedUser, get_current_user, require_mfa
from app.core.capabilities import Capability
from app.core.errors import NotFoundError
from app.models.common import CamelModel
from app.models.finding import DispositionState, RegulatoryClass
from app.services.analytics_service import AnalyticsCounterStore
from app.services.findings_ingest.normalizer import FindingNormalizer, PhiRedactionFilter
from app.services.findings_ingest.registry import AdapterRegistry
from app.storage.base import ObjectStore

logger = logging.getLogger("vurarad.findings_ingest")

FINDINGS_INGEST_COLLECTION = "findings_ingest"

router = APIRouter(
    prefix="/studies",
    tags=["findings"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class IngestedFindingSummary(CamelModel):
    """Per-finding outcome of an ingest — the clearance-tract view."""

    finding_id: str
    regulatory_class: RegulatoryClass
    clinical_use_allowed: bool
    disposition_state: DispositionState
    no_clearance_reference: bool = False


class FindingsIngestResponse(CamelModel):
    """Response for ``POST /studies/{studyId}/findings/ingest``."""

    ingest_id: str
    study_id: str
    study_instance_uid: str
    adapter_name: str
    adapter_version: str
    vendor_name: str
    payload_sha256: str
    idempotent: bool
    findings: list[IngestedFindingSummary] = []
    cadt_fields_dropped: int = 0
    no_clearance_reference_count: int = 0
    source_object_key: str | None = None
    produced_at: datetime


# ---------------------------------------------------------------------------
# Composition-root dependencies (tests override via app.state)
# ---------------------------------------------------------------------------
async def get_adapter_registry(request: Request) -> AdapterRegistry:
    reg = getattr(request.app.state, "adapter_registry", None)
    if reg is None:
        reg = AdapterRegistry()
        request.app.state.adapter_registry = reg
    return reg


AdapterRegistryDep = Annotated[AdapterRegistry, Depends(get_adapter_registry)]


async def get_source_store(request: Request) -> ObjectStore | None:
    """Optional object store for retaining the raw source payload."""
    return getattr(request.app.state, "source_object_store", None)


SourceStoreDep = Annotated[ObjectStore | None, Depends(get_source_store)]


async def get_counter_store(request: Request) -> AnalyticsCounterStore | None:
    """Optional counter store for ingest telemetry."""
    return getattr(request.app.state, "counter_store", None)


CounterStoreDep = Annotated[AnalyticsCounterStore | None, Depends(get_counter_store)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ingest_id(
    tenant_id: str,
    study_instance_uid: str,
    adapter_name: str,
    adapter_version: str,
    payload_sha256: str,
) -> str:
    """Deterministic ingest id = SHA-256 of the idempotency tuple."""
    raw = f"{tenant_id}:{study_instance_uid}:{adapter_name}:{adapter_version}:{payload_sha256}"
    return "fi_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _source_key(ingest_id: str, content_type: str) -> str:
    ext = "dcm" if content_type == "application/dicom" else "json"
    return f"findings_ingest/{ingest_id}/source.{ext}"


def _empty_response(
    study_id: str,
    adapter_name: str,
    adapter_version: str,
    vendor_name: str,
    payload_sha256: str,
) -> FindingsIngestResponse:
    return FindingsIngestResponse(
        ingest_id="",
        study_id=study_id,
        study_instance_uid="",
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        vendor_name=vendor_name,
        payload_sha256=payload_sha256,
        idempotent=False,
        findings=[],
        cadt_fields_dropped=0,
        no_clearance_reference_count=0,
        source_object_key=None,
        produced_at=datetime.now(UTC),
    )


async def _increment_counters(
    store: AnalyticsCounterStore,
    cadt_dropped: int,
    no_clearance: int,
) -> None:
    if cadt_dropped:
        await store.increment("findings_ingest_cadt_fields_dropped", cadt_dropped)
    if no_clearance:
        await store.increment("findings_ingest_no_clearance_reference", no_clearance)
    await store.increment("findings_ingest_total")


# ---------------------------------------------------------------------------
# POST /studies/{studyId}/findings/ingest
# ---------------------------------------------------------------------------
@router.post(
    "/{study_id}/findings/ingest",
    dependencies=[Depends(require_phi_capability(Capability.FINDINGS_INGEST))],
    response_model=FindingsIngestResponse,
)
async def ingest_findings(
    study_id: str,
    request: Request,
    study: StudyRecordDep,
    doc_store: DocumentStoreDep,
    finding_service: FindingServiceDep,
    audit_service: AuditServiceDep,
    registry: AdapterRegistryDep,
    source_store: SourceStoreDep,
    counter_store: CounterStoreDep,
    user: AuthenticatedUser = Depends(get_current_user),
    adapter_name: str = Query(..., alias="adapterName"),
    adapter_version: str = Query(..., alias="adapterVersion"),
) -> FindingsIngestResponse:
    """Ingest cleared-AI findings for a study from a version-pinned adapter."""
    # Cross-tenant: 404, never 403 — existence must not leak (criterion 6).
    if study.tenant_id != user.tenant_id:
        raise NotFoundError(f"Study {study_id} not found")

    body = await request.body()
    payload_sha256 = hashlib.sha256(body).hexdigest()

    adapter, clearance = registry.resolve(adapter_name, adapter_version)
    adapter_findings = adapter.parse(body)

    if not adapter_findings:
        return _empty_response(
            study_id, adapter_name, adapter_version, clearance.vendor_name, payload_sha256
        )

    study_instance_uid = adapter_findings[0].study_instance_uid
    tenant_id = user.tenant_id
    ingest_id = _ingest_id(
        tenant_id, study_instance_uid, adapter_name, adapter_version, payload_sha256
    )

    # Idempotency: a matching tuple returns the original response (criterion 5).
    existing = await doc_store.get(FINDINGS_INGEST_COLLECTION, ingest_id)
    if existing is not None:
        replay = FindingsIngestResponse.model_validate(existing["response"])
        return replay.model_copy(update={"idempotent": True})

    # Retain the raw source payload (the SR object) — referenced by provenance.
    source_key: str | None = None
    if source_store is not None:
        key = _source_key(ingest_id, adapter.content_type)
        ref = await source_store.put(key, body, adapter.content_type)
        source_key = ref.key

    produced_at = datetime.now(UTC)
    vendor = clearance.to_identity(
        adapter_name, adapter_version, produced_at, payload_ref=source_key
    )
    ingest_ref = f"findings_ingest/{ingest_id}"

    # PHI redaction seeded with the study's own identifiers.
    phi_filter = PhiRedactionFilter(
        known_phi={
            "patient_name": study.patient_name,
            "mrn": study.mrn,
            "patient_birth_date": study.patient_birth_date,
            "accession": study.accession,
        }
    )
    normalizer = FindingNormalizer(phi_filter)
    normalized, stats = normalizer.normalize_many(
        adapter_findings, vendor, study_id, ingest_ref
    )

    for nf in normalized:
        await finding_service.create_finding(nf.finding)

    response = FindingsIngestResponse(
        ingest_id=ingest_id,
        study_id=study_id,
        study_instance_uid=study_instance_uid,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        vendor_name=clearance.vendor_name,
        payload_sha256=payload_sha256,
        idempotent=False,
        findings=[
            IngestedFindingSummary(
                finding_id=nf.finding.finding_id,
                regulatory_class=nf.finding.regulatory_class,
                clinical_use_allowed=nf.finding.clinical_use_allowed,
                disposition_state=nf.finding.disposition.state,
                no_clearance_reference=(nf.finding.regulatory_class == "RUO"),
            )
            for nf in normalized
        ],
        cadt_fields_dropped=stats.cadt_fields_dropped,
        no_clearance_reference_count=stats.no_clearance_reference_count,
        source_object_key=source_key,
        produced_at=produced_at,
    )

    await doc_store.set(
        FINDINGS_INGEST_COLLECTION,
        ingest_id,
        {
            "ingestId": ingest_id,
            "tenantId": tenant_id,
            "studyId": study_id,
            "studyInstanceUid": study_instance_uid,
            "adapterName": adapter_name,
            "adapterVersion": adapter_version,
            "payloadSha256": payload_sha256,
            "findings": [
                {
                    "findingId": nf.finding.finding_id,
                    "redactedFreeText": nf.redacted_free_text,
                }
                for nf in normalized
            ],
            "sourceObjectKey": source_key,
            "response": response.model_dump(by_alias=True),
            "createdAt": produced_at.isoformat(),
        },
    )

    if counter_store is not None:
        await _increment_counters(
            counter_store,
            stats.cadt_fields_dropped,
            stats.no_clearance_reference_count,
        )

    await audit_service.record(
        "FINDINGS_INGESTED",
        actor=user.uid,
        second_factor=user.is_mfa_verified,
        detail={
            "ingestId": ingest_id,
            "studyId": study_id,
            "studyInstanceUid": study_instance_uid,
            "adapterName": adapter_name,
            "adapterVersion": adapter_version,
            "vendorName": clearance.vendor_name,
            "findingCount": len(normalized),
            "cadtFieldsDropped": stats.cadt_fields_dropped,
            "noClearanceReferenceCount": stats.no_clearance_reference_count,
            "operatorId": user.operator_id,
        },
        patient_key=study.patient_key,
    )
    logger.info(
        "FINDINGS_INGESTED",
        extra={
            "ingest_id": ingest_id,
            "adapter": adapter_name,
            "findings": len(normalized),
            "cadt_dropped": stats.cadt_fields_dropped,
            "no_clearance": stats.no_clearance_reference_count,
        },
    )
    return response


__all__ = ["FindingsIngestResponse", "IngestedFindingSummary", "router"]
