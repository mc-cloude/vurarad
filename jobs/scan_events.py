"""
scan_events.py — VuraRAD Scan Events Writer
============================================
Shared utility used by vura-logic, vura-health-bridge, and other services
to write structured events to the DataMind Swarm's vura_core.scan_events table.

Usage in vura-logic:
    from jobs.scan_events import write_scan_event, ScanEventType
    await write_scan_event(
        event_type=ScanEventType.AI_ANALYZED,
        case_uid=req.case_uid,
        ai_confidence=result["confidence"],
        ai_model_used="egfr_mlp_v1",
    )
"""

import os
import uuid
import json
import asyncio
import httpx
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional

logger = logging.getLogger(__name__)

DATAMIND_URL    = os.environ.get("DATAMIND_SERVICE_URL", "")
INTERNAL_SECRET = os.environ.get("VURA_INTERNAL_CALL_SECRET", "")
PROJECT_ID      = os.environ.get("GCP_PROJECT_ID", "vurarad")

# Fallback: write directly to BQ if DataMind URL not set
_bq_client = None


class ScanEventType:
    SCAN_RECEIVED          = "SCAN_RECEIVED"
    AI_ANALYZED            = "AI_ANALYZED"
    REPORT_DRAFTED         = "REPORT_DRAFTED"
    REPORT_SIGNED          = "REPORT_SIGNED"
    RADIOLOGIST_OVERRIDDEN = "RADIOLOGIST_OVERRIDDEN"
    PATHOLOGY_CONFIRMED    = "PATHOLOGY_CONFIRMED"


@dataclass
class ScanEvent:
    event_type:              str
    actor_type:              str = "AI_AGENT"
    vura_patient_id:         Optional[str] = None
    study_uid:               Optional[str] = None
    case_uid:                Optional[str] = None
    actor_id:                Optional[str] = None
    modality:                Optional[str] = None
    body_part:               Optional[str] = None
    institution_id:          Optional[str] = None
    cohort_region:           Optional[str] = None
    ai_model_used:           Optional[str] = None
    ai_confidence:           Optional[float] = None
    ai_finding:              Optional[str] = None
    radiologist_agreement:   Optional[bool] = None
    radiologist_override_reason: Optional[str] = None
    turnaround_seconds:      Optional[int] = None
    rvu_value:               Optional[float] = None
    source_agent:            str = "vura-logic"
    metadata:                Optional[dict] = None


async def write_scan_event(
    event_type: str,
    case_uid: Optional[str] = None,
    vura_patient_id: Optional[str] = None,
    study_uid: Optional[str] = None,
    ai_confidence: Optional[float] = None,
    ai_model_used: Optional[str] = None,
    turnaround_seconds: Optional[int] = None,
    modality: Optional[str] = None,
    body_part: Optional[str] = None,
    cohort_region: Optional[str] = None,
    institution_id: Optional[str] = None,
    radiologist_agreement: Optional[bool] = None,
    actor_type: str = "AI_AGENT",
    actor_id: Optional[str] = None,
    ai_finding: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Optional[str]:
    """
    Async: Write a scan event to DataMind Swarm (via REST or direct BQ fallback).
    Returns event_id on success, None on failure.
    Fire-and-forget safe — exceptions are caught and logged.
    """
    event_id = str(uuid.uuid4())

    try:
        payload = {
            "event_id":           event_id,
            "event_type":         event_type,
            "actor_type":         actor_type,
            "actor_id":           actor_id,
            "source_agent":       "vura-logic",
            "event_ts":           datetime.now(timezone.utc).isoformat(),
            "case_uid":           case_uid,
            "vura_patient_id":    vura_patient_id,
            "study_uid":          study_uid,
            "ai_confidence":      ai_confidence,
            "ai_model_used":      ai_model_used,
            "ai_finding":         ai_finding,
            "turnaround_seconds": turnaround_seconds,
            "modality":           modality,
            "body_part":          body_part,
            "cohort_region":      cohort_region,
            "institution_id":     institution_id,
            "radiologist_agreement": radiologist_agreement,
            "metadata":           json.dumps(metadata) if metadata else None,
        }
        # Remove None values
        payload = {k: v for k, v in payload.items() if v is not None}

        if DATAMIND_URL:
            # Route through DataMind orchestrator
            await _post_to_datamind(payload)
        else:
            # Fallback: write directly to BigQuery
            await asyncio.get_event_loop().run_in_executor(
                None, _write_bq_direct, payload
            )
        return event_id

    except Exception as e:
        logger.error(f"[ScanEvents] Failed to write {event_type} event: {e}")
        return None


async def _post_to_datamind(payload: dict) -> None:
    """POST scan event to DataMind /scan_events endpoint."""
    url = f"{DATAMIND_URL}/scan_events"
    headers = {
        "Authorization": f"Bearer {INTERNAL_SECRET}",
        "Content-Type":  "application/json",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.warning(f"[ScanEvents] DataMind returned {resp.status_code}: {resp.text[:200]}")


def _write_bq_direct(payload: dict) -> None:
    """Fallback: write directly to BQ when DataMind service URL is not set."""
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery
        _bq_client = bigquery.Client(project=PROJECT_ID)
    errors = _bq_client.insert_rows_json(
        f"{PROJECT_ID}.vura_core.scan_events", [payload]
    )
    if errors:
        logger.error(f"[ScanEvents] BQ direct insert errors: {errors[:2]}")
