"""Meter catalogue and units — the billable dimensions of vuraRAD (§3.19.1).

Every billable unit is recorded at the point it is incurred, by the service
that incurs it, through ``MeteringService.record``.  The catalogue is the
single source of truth for what is metered; per-meter unit costs are data
files (``app/billing/rates/{region}.yaml``), never constants in code.
"""

from __future__ import annotations

from enum import StrEnum


class Meter(StrEnum):
    """What is metered. Values are the Firestore / rate-card keys."""

    IMAGES_INGESTED = "images_ingested"
    STUDIES_INGESTED = "studies_ingested"
    AI_DRAFT_REQUESTS = "ai_draft_requests"
    AI_DRAFT_TOKENS = "ai_draft_tokens"
    AI_QA_REQUESTS = "ai_qa_requests"
    AI_QA_TOKENS = "ai_qa_tokens"
    SEGMENTATION_INFERENCES = "segmentation_inferences"
    DICOMWEB_QIDO = "dicomweb_qido"
    DICOMWEB_WADO = "dicomweb_wado"
    DICOMWEB_STOW = "dicomweb_stow"
    STORAGE_BYTES_MONTH = "storage_bytes_month"
    EGRESS_BYTES = "egress_bytes"
    REPORTS_SIGNED = "reports_signed"
    REPORTS_ADDENDA = "reports_addenda"
    DEID_STUDIES = "deid_studies"
    DEID_BURNED_IN = "deid_burned_in"
    RESEARCH_EXTRACTIONS = "research_extractions"
    # Derived: counted from images_ingested once the ceiling is reached (§3.19.3
    # OVERAGE).  Priced by config (overage_per_100_images), not a base meter.
    OVERAGE_IMAGES = "overage_images"


class MeterUnit(StrEnum):
    """The unit a meter quantity is expressed in."""

    COUNT = "count"
    GB_MONTH = "GB-month"
    GB = "GB"
    TOKENS = "tokens"
    SECONDS = "seconds"


# The unit of every meter — used by the metering service when flushing so the
# persisted doc carries the unit alongside the quantity.
METER_UNITS: dict[Meter, MeterUnit] = {
    Meter.IMAGES_INGESTED: MeterUnit.COUNT,
    Meter.STUDIES_INGESTED: MeterUnit.COUNT,
    Meter.AI_DRAFT_REQUESTS: MeterUnit.COUNT,
    Meter.AI_DRAFT_TOKENS: MeterUnit.TOKENS,
    Meter.AI_QA_REQUESTS: MeterUnit.COUNT,
    Meter.AI_QA_TOKENS: MeterUnit.TOKENS,
    Meter.SEGMENTATION_INFERENCES: MeterUnit.COUNT,
    Meter.DICOMWEB_QIDO: MeterUnit.COUNT,
    Meter.DICOMWEB_WADO: MeterUnit.COUNT,
    Meter.DICOMWEB_STOW: MeterUnit.COUNT,
    Meter.STORAGE_BYTES_MONTH: MeterUnit.GB_MONTH,
    Meter.EGRESS_BYTES: MeterUnit.GB,
    Meter.REPORTS_SIGNED: MeterUnit.COUNT,
    Meter.REPORTS_ADDENDA: MeterUnit.COUNT,
    Meter.DEID_STUDIES: MeterUnit.COUNT,
    Meter.DEID_BURNED_IN: MeterUnit.COUNT,
    Meter.RESEARCH_EXTRACTIONS: MeterUnit.COUNT,
    Meter.OVERAGE_IMAGES: MeterUnit.COUNT,
}


def unit_for(meter: Meter) -> MeterUnit:
    """Return the unit a meter is counted in."""
    return METER_UNITS[meter]
