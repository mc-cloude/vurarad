"""Study service — worklist assembly, search, detail, series, access URLs.

Single-read worklist assembly from ``worklist_index/current``; search execution
against composite indexes; detail composition from the study document; series
listing via direct document gets (1 read per series, not 412); access-URL
issuance via :class:`SignedUrlService`.

Every method that resolves a study runs :class:`StudyAccessPolicy` before any
data is returned or any URL is minted.  Audit events are written on
patient-identity view, study view, and image access.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from app.core.auth import AuthenticatedUser
from app.core.errors import (
    InstanceChunkTooLargeError,
    NotFoundError,
    SearchFilterRequiredError,
)
from app.models.series import Series
from app.models.study import (
    AccessUrlChunk,
    AssignedTo,
    InstanceGeometry,
    PatientIdentity,
    PriorStudyRef,
    SearchResult,
    SeriesListResponse,
    SeriesSummary,
    StudyDetail,
    StudyRecord,
    ViewerScope,
    WorklistEnvelope,
    WorklistRow,
)
from app.repositories.patient_repo import PatientRepository
from app.repositories.study_repo import StudyRepository
from app.services.access_policy import StudyAccessPolicy
from app.services.audit_service import AuditService
from app.services.signed_url_service import SignedUrlService


class StudyService:
    """Orchestrate worklist, search, detail, series, and access-URL reads."""

    def __init__(
        self,
        study_repo: StudyRepository,
        patient_repo: PatientRepository,
        signed_url_service: SignedUrlService,
        audit_service: AuditService,
        *,
        worklist_cap: int = 200,
        chunk_max: int = 250,
    ) -> None:
        self._study_repo = study_repo
        self._patient_repo = patient_repo
        self._signed_urls = signed_url_service
        self._audit = audit_service
        self._policy = StudyAccessPolicy()
        self._worklist_cap = worklist_cap
        self._chunk_max = chunk_max

    # -- worklist (§3.3) — 1 read --------------------------------------------
    async def get_worklist(self) -> WorklistEnvelope:
        """Assemble the worklist from ``worklist_index/current`` — 1 Firestore read."""
        doc = await self._study_repo.get_worklist_index()
        if doc is None:
            return WorklistEnvelope(
                studies=[],
                truncated=False,
                cap=self._worklist_cap,
                total_known=0,
                generated_at=datetime.now(UTC).isoformat(),
                oldest_study_date="",
            )
        items_raw = doc.get("items", [])
        studies: list[WorklistRow] = []
        for item in items_raw:
            studies.append(_worklist_row_from_dict(item))
        cap = doc.get("cap", self._worklist_cap)
        total_known = doc.get("totalKnown", doc.get("total_known", len(studies)))
        return WorklistEnvelope(
            studies=studies,
            truncated=total_known > cap,
            cap=cap,
            total_known=total_known,
            generated_at=doc.get("generatedAt", doc.get("generated_at", "")),
            oldest_study_date=doc.get("oldestStudyDate", doc.get("oldest_study_date", "")),
        )

    # -- search (§3.4) -------------------------------------------------------
    async def search(
        self,
        user: AuthenticatedUser,
        *,
        patient_ref: str | None = None,
        accession: str | None = None,
        modality: str | None = None,
        status: str | None = None,
        study_date_from: str | None = None,
        study_date_to: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResult:
        """Run a search query; at least one filter is required."""
        if not any([patient_ref, accession, study_date_from, study_date_to]):
            raise SearchFilterRequiredError(
                "At least one of patientRef, accession, or from/to is required"
            )
        docs = await self._study_repo.search(
            patient_ref=patient_ref,
            accession=accession,
            modality=modality,
            status=status,
            study_date_from=study_date_from,
            study_date_to=study_date_to,
            limit=limit,
        )
        # Sort by study_date descending (composite index order).
        docs.sort(key=lambda d: d.get("study_date", d.get("studyDate", "")), reverse=True)
        # Cursor pagination.
        offset = _decode_cursor(cursor)
        page = docs[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = _encode_cursor(next_offset) if next_offset < len(docs) else None
        items = [_worklist_row_from_dict(d) for d in page]
        return SearchResult(
            items=items,
            next_cursor=next_cursor,
            query_cost_reads=len(docs),
        )

    # -- study detail (§3.5) -------------------------------------------------
    async def get_study_detail(
        self,
        user: AuthenticatedUser,
        study_id: str,
        viewer_scope: ViewerScope | None = None,
    ) -> StudyDetail:
        """Compose study detail from the study document — 1 read + access policy."""
        study = await self._resolve_and_authorise(user, study_id, viewer_scope)
        await self._audit.record(
            "STUDY_VIEWED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"studyId": study_id},
        )
        return _study_detail_from_record(study)

    # -- patient identity (§3.5) — the ONLY name-releasing route -------------
    async def get_patient_identity(
        self,
        user: AuthenticatedUser,
        study_id: str,
    ) -> PatientIdentity:
        """Return patient identity for a study — radiologist-only, audited."""
        study = await self._resolve_and_authorise(user, study_id)
        identity = await self._patient_repo.get_identity(study.patient_key)
        if identity is None:
            raise NotFoundError(f"Patient record for study {study_id} not found")
        await self._audit.record(
            "PATIENT_IDENTITY_VIEWED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={"studyId": study_id},
            patient_key=study.patient_key,
        )
        return identity

    # -- series listing (§3.5) — 1 + N reads --------------------------------
    async def get_series(
        self,
        user: AuthenticatedUser,
        study_id: str,
        viewer_scope: ViewerScope | None = None,
    ) -> SeriesListResponse:
        """List series with geometry — 1 study read + 1 read per series."""
        study = await self._resolve_and_authorise(user, study_id, viewer_scope)
        series_list = await self._study_repo.get_series_for_study(study)
        summaries = [_series_summary_from_model(s) for s in series_list]
        return SeriesListResponse(study_id=study_id, series=summaries)

    # -- access URLs (§3.6) — chunked signed URLs ---------------------------
    async def get_access_urls(
        self,
        user: AuthenticatedUser,
        study_id: str,
        series_uid: str,
        *,
        from_stack_index: int = 0,
        count: int = 250,
        viewer_scope: ViewerScope | None = None,
    ) -> AccessUrlChunk:
        """Issue a chunk of signed URLs — access policy first, then sign."""
        if count > self._chunk_max:
            raise InstanceChunkTooLargeError(
                f"count {count} exceeds the maximum of {self._chunk_max}"
            )
        study = await self._resolve_and_authorise(user, study_id, viewer_scope)
        series_list = await self._study_repo.get_series_for_study(study)
        series = _find_series(series_list, series_uid)
        if series is None:
            raise NotFoundError(f"Series {series_uid} not found in study {study_id}")
        instances = sorted(series.instances, key=lambda i: i.stack_index)
        chunk = await self._signed_urls.issue_chunk(
            study_id=study_id,
            series_uid=series_uid,
            instances=instances,
            from_stack_index=from_stack_index,
            count=count,
            series_instance_count=len(instances),
        )
        await self._audit.record(
            "STUDY_IMAGES_ACCESSED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "studyId": study_id,
                "seriesUid": series_uid,
                "fromStackIndex": from_stack_index,
                "count": chunk.count,
            },
            patient_key=study.patient_key,
        )
        return chunk

    # -- helpers -------------------------------------------------------------
    async def _resolve_and_authorise(
        self,
        user: AuthenticatedUser,
        study_id: str,
        viewer_scope: ViewerScope | None = None,
    ) -> StudyRecord:
        """Read the study document and run the access policy."""
        study = await self._study_repo.get_study(study_id)
        if study is None:
            raise NotFoundError(f"Study {study_id} not found")
        self._policy.assert_can_read(user, study, viewer_scope)
        return study


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------
def _worklist_row_from_dict(d: dict[str, Any]) -> WorklistRow:
    """Build a :class:`WorklistRow` from a Firestore/study document dict."""

    def _g(snake: str, camel: str) -> Any:
        return d.get(camel, d.get(snake))

    assigned_raw = _g("assigned_to", "assignedTo")
    assigned: AssignedTo | None = None
    if isinstance(assigned_raw, dict):
        assigned = AssignedTo(
            uid=str(assigned_raw.get("uid", "")),
            operator_id=str(assigned_raw.get("operatorId", assigned_raw.get("operator_id", ""))),
            display_name=str(assigned_raw.get("displayName", assigned_raw.get("display_name", ""))),
        )

    from app.models.study import StudyPriority, StudyStatus

    status_raw = _g("status", "status")
    try:
        status = StudyStatus(status_raw) if status_raw else StudyStatus.UNREAD
    except ValueError:
        status = StudyStatus.UNREAD

    priority_raw = _g("priority", "priority")
    try:
        priority = StudyPriority(priority_raw) if priority_raw else StudyPriority.ROUTINE
    except ValueError:
        priority = StudyPriority.ROUTINE

    return WorklistRow(
        study_id=_g("study_id", "studyId") or "",
        patient_key=_g("patient_key", "patientKey") or "",
        patient_ref=_g("patient_ref", "patientRef") or "",
        patient_age_sex=_g("patient_age_sex", "patientAgeSex") or "",
        accession=_g("accession", "accession") or "",
        modality=_g("modality", "modality") or "",
        body_part=_g("body_part", "bodyPart") or "",
        description=_g("description", "description") or "",
        study_date=_g("study_date", "studyDate") or "",
        priority=priority,
        status=status,
        assigned_to=assigned,
        series_count=_g("series_count", "seriesCount") or 0,
        instance_count=_g("instance_count", "instanceCount") or 0,
        study_bytes=_g("study_bytes", "studyBytes") or 0,
        has_report=_g("has_report", "hasReport") or False,
        report_id=_g("report_id", "reportId"),
        signed_at=_g("signed_at", "signedAt"),
        updated_at=_g("updated_at", "updatedAt") or "",
    )


def _study_detail_from_record(study: StudyRecord) -> StudyDetail:
    """Build a :class:`StudyDetail` from a :class:`StudyRecord` (no PHI names)."""
    prior_refs = [
        PriorStudyRef(
            study_id=p.study_id,
            study_date=p.study_date,
            modality=p.modality,
            body_part=p.body_part,
            description=p.description,
        )
        for p in study.prior_studies
    ]
    return StudyDetail(
        study_id=study.study_id,
        patient_key=study.patient_key,
        patient_ref=study.patient_ref,
        patient_age_sex=study.patient_age_sex,
        patient_sex=study.patient_sex,
        accession=study.accession,
        modality=study.modality,
        body_part=study.body_part,
        description=study.description,
        study_date=study.study_date,
        referring_physician=study.referring_physician,
        clinical_history=study.clinical_history,
        status=study.status,
        priority=study.priority,
        assigned_to=study.assigned_to,
        series_count=study.series_count,
        instance_count=study.instance_count,
        study_bytes=study.study_bytes,
        report_id=study.report_id,
        prior_studies=prior_refs,
        created_at=study.created_at,
        updated_at=study.updated_at,
        version=study.version,
    )


def _series_summary_from_model(series: Series) -> SeriesSummary:
    """Build a :class:`SeriesSummary` from a :class:`Series` model."""
    instances: list[InstanceGeometry] = []
    total_bytes = 0
    for inst in sorted(series.instances, key=lambda i: i.stack_index):
        total_bytes += inst.size_bytes
        instances.append(
            InstanceGeometry(
                instance_uid=inst.sop_instance_uid,
                sop_instance_uid=inst.sop_instance_uid,
                stack_index=inst.stack_index,
                instance_number=inst.instance_number,
                object_path=inst.object_path,
                size_bytes=inst.size_bytes,
                image_position_patient=inst.image_position_patient,
                image_orientation_patient=inst.image_orientation_patient,
                slice_location=inst.slice_location,
                number_of_frames=inst.number_of_frames,
                window_center=inst.window_center,
                window_width=inst.window_width,
                rescale_slope=inst.rescale_slope,
                rescale_intercept=inst.rescale_intercept,
            )
        )
    # Derive series-level geometry from the first instance when available.
    first = series.instances[0] if series.instances else None
    pixel_spacing = first.pixel_spacing if first else None
    spacing = first.spacing_between_slices_mm if first else None
    return SeriesSummary(
        series_uid=series.series_id,
        modality=series.modality,
        instance_count=series.instance_count,
        frame_count=series.frame_count,
        is_multi_frame=series.is_multi_frame,
        pixel_spacing=pixel_spacing,
        spacing_between_slices_mm=spacing,
        series_bytes=total_bytes,
        stack_order_basis=series.stack_order_basis,
        stack_order_confidence=series.stack_order_confidence,
        instances=instances,
    )


def _find_series(series_list: list[Series], series_uid: str) -> Series | None:
    """Find a series by its internal id or series-instance UID."""
    for s in series_list:
        if s.series_id == series_uid or s.series_instance_uid == series_uid:
            return s
    return None


# ---------------------------------------------------------------------------
# Cursor encoding (opaque, base64 JSON)
# ---------------------------------------------------------------------------
def _encode_cursor(offset: int) -> str:
    payload = json.dumps({"o": offset})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return int(payload.get("o", 0))
    except (ValueError, KeyError, TypeError):
        return 0
