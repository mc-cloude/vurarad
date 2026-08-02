"""
ingest_radiogenomics.py
=======================
VuraRAD Radiogenomics Module — Phase A0/A1/A2
Multi-source ingestion of open-access datasets into BigQuery radiogenomics tables.

Datasets ingested:
  1. TCIA NSCLC-Radiogenomics     — 211 CT patients, RNA-Seq + mutation labels   [Open]
  2. BCBM-RadioGenomics           — 268 MRI + molecular + pre-segmented masks    [Open]
  3. TCGA Clinical Open Tier      — Clinical/pathology data for 33 cancer types  [Open]

Run AFTER create_radiogenomics_schema.py.

Usage:
    python jobs/ingest_radiogenomics.py [--dataset NSCLC|BCBM|TCGA|ALL]

Environment vars:
    GCP_PROJECT_ID     — defaults to 'vurarad'
    GOOGLE_APPLICATION_CREDENTIALS — service account key (or use ADC)
"""

import os
import sys
import uuid
import logging
import argparse
import hashlib
import json
import requests
import time  # [FIX M-17] Added for rate limiting
from datetime import datetime, timezone
from google.cloud import bigquery
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - RADIOGENOMICS_ETL - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

PROJECT_ID   = os.environ.get("GCP_PROJECT_ID", "vurarad")
DATASET_ID   = "radiogenomics"
NOW          = datetime.now(timezone.utc).isoformat()

# TCIA Public REST API (no auth needed for open-access collections)
TCIA_API_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v2"
TCIA_LEGACY   = "https://services.cancerimagingarchive.net/nbia-api/services/v1"

# TCGA Open-Tier Clinical via GDC Data Portal REST API
GDC_API_BASE  = "https://api.gdc.cancer.gov"


# ── Helpers ───────────────────────────────────────────────────────────────────

def deidentify(raw_id: str) -> str:
    """One-way hash of a patient/case ID for de-identification."""
    return "CASE-" + hashlib.sha256(raw_id.encode()).hexdigest()[:16].upper()


def bq_insert(client: bigquery.Client, table_name: str, rows: list[dict]):
    """Insert rows into a BigQuery table. Skips empty batches."""
    if not rows:
        logger.info(f"[SKIP] No rows to insert into {table_name}.")
        return
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"
    errors = client.insert_rows_json(table_ref, rows)
    if errors:
        logger.error(f"[ERROR] BQ insert errors for {table_name}: {errors}")
    else:
        logger.info(f"[OK] Inserted {len(rows)} rows into {table_name}.")


# [FIX M-17] Persistent session with exponential backoff
_session = requests.Session()
_retries = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"]
)
_session.mount("https://", HTTPAdapter(max_retries=_retries))

def tcia_get(endpoint: str, params: dict = None, use_legacy: bool = False) -> list:
    """TCIA REST GET \u2014 returns list of dicts or empty list on error."""
    base = TCIA_LEGACY if use_legacy else TCIA_API_BASE
    try:
        resp = _session.get(f"{base}/{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"[TCIA] {endpoint} failed: {e}")
        return []

def gdc_get(endpoint: str, params: dict = None) -> dict:
    """GDC REST GET \u2014 returns dict or empty dict on error."""
    try:
        resp = _session.get(f"{GDC_API_BASE}/{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"[GDC] {endpoint} failed: {e}")
        return {}


# ── Dataset 1: TCIA NSCLC-Radiogenomics ──────────────────────────────────────

def ingest_nsclc_radiogenomics(client: bigquery.Client):
    """
    Ingest TCIA NSCLC-Radiogenomics collection.
    Collection name: NSCLC-Radiogenomics
    Patients: 211 | Modality: CT | Labels: RNA-Seq + mutation annotations
    https://wiki.cancerimagingarchive.net/display/Public/NSCLC-Radiogenomics
    """
    logger.info("=" * 60)
    logger.info("[PHASE A0] Ingesting TCIA NSCLC-Radiogenomics (211 cases)...")

    COLLECTION = "NSCLC-Radiogenomics"

    # Step 1: Get all patients in collection
    patients = tcia_get("getPatient", {"Collection": COLLECTION})
    if not patients:
        # Fallback: use legacy TCIA endpoint
        patients = tcia_get("getPatientStudy", {"Collection": COLLECTION}, use_legacy=True)

    logger.info(f"[TCIA] Found {len(patients)} patients in {COLLECTION}")

    cohort_rows = []
    label_rows  = []

    for p in patients:
        raw_id   = p.get("PatientID", p.get("patientId", str(uuid.uuid4())))
        case_uid = deidentify(f"NSCLC-{raw_id}")

        # [FIX M-17] Small sleep between patients to prevent TCIA throttling
        time.sleep(0.2)

        # Get studies for this patient
        studies  = tcia_get("getPatientStudy", {
            "Collection": COLLECTION,
            "PatientID": raw_id
        })
        study    = studies[0] if studies else {}

        cohort_rows.append({
            "case_uid":              case_uid,
            "cohort_region":         "TCIA",
            "cancer_type":           "NSCLC",
            "modality":              study.get("Modality", "CT"),
            "body_part":             study.get("BodyPartExamined", "Thorax"),
            "institution_id":        deidentify(study.get("InstitutionName", "TCIA_NSCLC")),
            "acquisition_date":      None,        # scrubbed for privacy
            "ground_truth_genotype": None,        # added from label table
            "data_source":           "TCIA",
            "data_tier":             "TRAINING",
            "ingested_at":           NOW,
        })

        # Mutation label stubs — real labels come from the NSCLC-Radiogenomics spreadsheet
        # (EGFR, KRAS, ALK, TP53 status published alongside collection)
        # We mark these as requiring annotation lookup; a separate label loader
        # (load_nsclc_labels.py) populates mutation_labels from the CSV.
        label_rows.append({
            "label_id":            str(uuid.uuid4()),
            "case_uid":            case_uid,
            "dataset_source":      "TCIA_NSCLC",
            "rnaseq_signature":    None,   # populated from separately downloaded RNA-Seq CSV
            "mutation_labels":     json.dumps({"pending_label_load": True}),
            "molecular_subtype":   None,
            "ihc_er":              None,
            "ihc_pr":              None,
            "ihc_her2":            None,
            "tumour_purity":       None,
            "segmentation_mask_gcs": None,
            "dataset_access_tier": "OPEN",
            "ingested_at":         NOW,
        })

    bq_insert(client, "clinical_cohorts",       cohort_rows)
    bq_insert(client, "external_genomic_labels", label_rows)
    logger.info(f"[A0] NSCLC-Radiogenomics ingestion complete: {len(cohort_rows)} cases.")


# ── Dataset 2: BCBM-RadioGenomics ────────────────────────────────────────────

def ingest_bcbm(client: bigquery.Client):
    """
    Ingest BCBM-RadioGenomics (Breast Cancer Brain Metastasis).
    268 MRI studies + molecular markers + pre-segmented tumour masks.
    https://wiki.cancerimagingarchive.net/display/Public/BCBM+RadioGenomics
    """
    logger.info("=" * 60)
    logger.info("[PHASE A1] Ingesting BCBM-RadioGenomics (268 MRI cases)...")

    COLLECTION = "BCBM-Radiogenomics"

    patients = tcia_get("getPatient", {"Collection": COLLECTION})
    if not patients:
        patients = tcia_get("getPatientStudy", {"Collection": COLLECTION}, use_legacy=True)

    logger.info(f"[TCIA] Found {len(patients)} patients in {COLLECTION}")

    cohort_rows = []
    label_rows  = []

    for p in patients:
        raw_id   = p.get("PatientID", p.get("patientId", str(uuid.uuid4())))
        case_uid = deidentify(f"BCBM-{raw_id}")

        studies  = tcia_get("getPatientStudy", {
            "Collection": COLLECTION,
            "PatientID": raw_id
        })
        study    = studies[0] if studies else {}

        cohort_rows.append({
            "case_uid":              case_uid,
            "cohort_region":         "BCBM",
            "cancer_type":           "BREAST_BRAIN_METS",
            "modality":              study.get("Modality", "MRI"),
            "body_part":             "Brain",
            "institution_id":        deidentify(study.get("InstitutionName", "TCIA_BCBM")),
            "acquisition_date":      None,
            "ground_truth_genotype": None,
            "data_source":           "BCBM",
            "data_tier":             "TRAINING",
            "ingested_at":           NOW,
        })

        # BCBM includes IHC (ER/PR/HER2) and molecular subtype labels
        # Real values loaded from the BCBM clinical metadata spreadsheet
        label_rows.append({
            "label_id":              str(uuid.uuid4()),
            "case_uid":              case_uid,
            "dataset_source":        "BCBM",
            "rnaseq_signature":      None,
            "mutation_labels":       json.dumps({"pending_label_load": True}),
            "molecular_subtype":     None,   # TNBC / HER2+ / HR+ etc.
            "ihc_er":                None,
            "ihc_pr":                None,
            "ihc_her2":              None,
            "tumour_purity":         None,
            "segmentation_mask_gcs": None,   # Brain tumour NIfTI masks — gcs_path populated post-transfer
            "dataset_access_tier":   "OPEN",
            "ingested_at":           NOW,
        })

    bq_insert(client, "clinical_cohorts",       cohort_rows)
    bq_insert(client, "external_genomic_labels", label_rows)
    logger.info(f"[A1] BCBM-RadioGenomics ingestion complete: {len(cohort_rows)} cases.")


# ── Dataset 3: TCGA Open Tier (via GDC API) ──────────────────────────────────

TCGA_OPEN_PROJECTS = [
    # Cancer type  , GDC project ID         , body_part
    ("NSCLC-LUAD",  "TCGA-LUAD",            "Thorax"),
    ("NSCLC-LUSC",  "TCGA-LUSC",            "Thorax"),
    ("BREAST",      "TCGA-BRCA",            "Breast"),
    ("GBM",         "TCGA-GBM",             "Brain"),
    ("COLORECTAL",  "TCGA-COAD",            "Abdomen"),
    ("THYROID",     "TCGA-THCA",            "Neck"),
    ("HCC",         "TCGA-LIHC",            "Abdomen"),
    ("CERVICAL",    "TCGA-CESC",            "Pelvis"),
]

def ingest_tcga_open(client: bigquery.Client):
    """
    Ingest TCGA open-tier clinical data via the GDC Data Portal REST API.
    This covers case demographics, cancer type, and pathological stage.
    NO controlled genomic data is accessed here — open tier only.
    """
    logger.info("=" * 60)
    logger.info("[PHASE A2] Ingesting TCGA open-tier clinical cohorts via GDC API...")

    cohort_rows = []
    label_rows  = []

    for cancer_type, project_id, body_part in TCGA_OPEN_PROJECTS:
        logger.info(f"  [GDC] Fetching {project_id} ({cancer_type})...")

        # GDC open API: cases endpoint with clinical fields
        params = {
            "filters": json.dumps({
                "op": "in",
                "content": {
                    "field": "project.project_id",
                    "value": [project_id]
                }
            }),
            "fields": "case_id,primary_site,disease_type,demographic.vital_status,diagnoses.primary_diagnosis",
            "format": "JSON",
            "size": "500",    # max per request
        }
        result = gdc_get("cases", params=params)
        cases  = result.get("data", {}).get("hits", [])
        logger.info(f"  [GDC] Retrieved {len(cases)} open-tier cases for {project_id}")

        for c in cases:
            # [FIX M-17] Small sleep between cases for GDC API
            time.sleep(0.1)
            case_uid  = deidentify(f"TCGA-{c.get('case_id', uuid.uuid4())}")
            primary_d = (c.get("diagnoses") or [{}])[0].get("primary_diagnosis", "")

            cohort_rows.append({
                "case_uid":              case_uid,
                "cohort_region":         "TCGA",
                "cancer_type":           cancer_type,
                "modality":              None,       # imaging not in open tier
                "body_part":             body_part,
                "institution_id":        deidentify(project_id),
                "acquisition_date":      None,
                "ground_truth_genotype": None,       # controlled genomic data — pending dbGaP approval
                "data_source":           "TCGA",
                "data_tier":             "TRAINING",
                "ingested_at":           NOW,
            })

            label_rows.append({
                "label_id":              str(uuid.uuid4()),
                "case_uid":              case_uid,
                "dataset_source":        "TCGA",
                "rnaseq_signature":      None,       # controlled — pending dbGaP
                "mutation_labels":       json.dumps({"dbgap_pending": True, "primary_diagnosis": primary_d}),
                "molecular_subtype":     None,
                "ihc_er":                None,
                "ihc_pr":                None,
                "ihc_her2":              None,
                "tumour_purity":         None,
                "segmentation_mask_gcs": None,
                "dataset_access_tier":   "TIERED",
                "ingested_at":           NOW,
            })

    bq_insert(client, "clinical_cohorts",       cohort_rows)
    bq_insert(client, "external_genomic_labels", label_rows)
    logger.info(f"[A2] TCGA open-tier ingestion complete: {len(cohort_rows)} total cases.")


# ── Ingestion summary view ────────────────────────────────────────────────────

TRAINING_MASTER_VIEW_SQL = """
CREATE OR REPLACE VIEW `{project}.radiogenomics.training_master` AS
SELECT
    c.case_uid,
    c.cohort_region,
    c.cancer_type,
    c.modality,
    c.body_part,
    c.data_source,
    c.data_tier,
    l.dataset_source,
    l.molecular_subtype,
    l.dataset_access_tier,
    l.ihc_er,
    l.ihc_pr,
    l.ihc_her2,
    c.ingested_at
FROM `{project}.radiogenomics.clinical_cohorts` c
LEFT JOIN `{project}.radiogenomics.external_genomic_labels` l
    ON c.case_uid = l.case_uid
WHERE c.data_tier IN ('TRAINING', 'VALIDATION')
""".format(project=PROJECT_ID)


def create_training_view(client: bigquery.Client):
    logger.info("[VIEW] Creating training_master view...")
    job = client.query(TRAINING_MASTER_VIEW_SQL)
    job.result()
    logger.info("[OK] training_master view created.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VuraRAD Radiogenomics Ingestion")
    parser.add_argument(
        "--dataset",
        default="ALL",
        choices=["NSCLC", "BCBM", "TCGA", "ALL"],
        help="Which dataset to ingest (default: ALL)"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("VuraRAD Radiogenomics — Open Data Ingestion")
    logger.info(f"  Project : {PROJECT_ID}")
    logger.info(f"  Dataset : radiogenomics")
    logger.info(f"  Target  : {args.dataset}")
    logger.info("=" * 60)

    client = bigquery.Client(project=PROJECT_ID)

    if args.dataset in ("NSCLC", "ALL"):
        ingest_nsclc_radiogenomics(client)

    if args.dataset in ("BCBM", "ALL"):
        ingest_bcbm(client)

    if args.dataset in ("TCGA", "ALL"):
        ingest_tcga_open(client)

    # Create the unified training view
    create_training_view(client)

    logger.info("")
    logger.info("=" * 60)
    logger.info("[COMPLETE] Open-access radiogenomics ingestion finished.")
    logger.info("Run 'python jobs/load_nsclc_labels.py' next to load mutation labels from the NSCLC CSV.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
