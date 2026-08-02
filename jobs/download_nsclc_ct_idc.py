"""
download_nsclc_ct_idc.py — NSCLC-Radiogenomics CT Download Pipeline
====================================================================
Downloads the TCIA NSCLC-Radiogenomics CT series from NCI Imaging Data 
Commons (IDC) public GCS bucket to the vurarad GCS bucket, then updates
BigQuery with GCS paths for real feature extraction.

Strategy:
  1. Query IDC BigQuery (bigquery-public-data.idc_current.dicom_all)
     to get all series GCS URLs for the NSCLC-Radiogenomics collection
  2. Filter to CT-only series (Modality = CT)
  3. Use GCS object copy (gsutil -m cp) to transfer each series from
     the IDC public bucket to gs://vura-radiomics/nsclc/
  4. Update clinical_cohorts.imaging_gcs_prefix in BigQuery
  5. Trigger feature extraction for downloaded cases

Cost estimate: NSCLC-Radiogenomics ~85 GB total.
- IDC→GCS (same Google network): FREE (no egress within GCP)
- GCS storage: ~$1.80/month at Standard tier for 85 GB

Usage:
    python download_nsclc_ct_idc.py [--dry-run] [--patient R01-001] [--limit 10]
    
    --dry-run   Print what would be downloaded without copying
    --patient   Download only a specific patient ID (e.g. R01-001)
    --limit N   Download only N patients (for testing)
    --all       Download all 211 patients (default behaviour)
"""

import os, sys, json, subprocess, hashlib, argparse, uuid, logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google.cloud import bigquery, storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - NSCLC_DL - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

PROJECT_ID          = os.environ.get("GCP_PROJECT_ID", "vurarad")
DEST_BUCKET         = os.environ.get("RADIOMICS_BUCKET", "vura-radiomics")
DEST_PREFIX         = "nsclc-radiogenomics"          # gs://vura-radiomics/nsclc-radiogenomics/
IDC_COLLECTION_ID   = "nsclc_radiogenomics"           # Confirmed IDC collection_id
NOW                 = datetime.now(timezone.utc).isoformat()

# Parallel download workers — IDC recommends ≤16 parallel streams
MAX_WORKERS = 8


# ── Step 1: Query IDC for NSCLC-Radiogenomics series ──────────────────────────

def get_idc_series(client: bigquery.Client, patient_filter=None, limit=None) -> list[dict]:
    """
    Returns list of {PatientID, SeriesInstanceUID, Modality, 
                     series_gcs_url, gcs_bucket, instances} dicts
    for the NSCLC-Radiogenomics CT collection.
    """
    logger.info(f"Querying IDC for NSCLC-Radiogenomics CT series...")
    
    query_params = [
        bigquery.ScalarQueryParameter("collection_id", "STRING", IDC_COLLECTION_ID)
    ]
    where_clause = "WHERE collection_id = @collection_id AND Modality = 'CT'"
    
    if patient_filter:
        where_clause += " AND PatientID = @patient_id"
        query_params.append(bigquery.ScalarQueryParameter("patient_id", "STRING", patient_filter))

    sql = f"""
    SELECT
        PatientID,
        StudyInstanceUID,
        SeriesInstanceUID,
        Modality,
        SeriesDescription,
        series_gcs_url,
        gcs_bucket,
        COUNT(*) AS instances
    FROM `bigquery-public-data.idc_current.dicom_all`
    {where_clause}
    GROUP BY 1,2,3,4,5,6,7
    ORDER BY PatientID, SeriesInstanceUID
    """
    
    if limit:
        sql += f" LIMIT {int(limit)}" # Safe cast to int

    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    rows = list(client.query(sql, job_config=job_config).result())
    logger.info(f"Found {len(rows)} CT series for {len(set(r.PatientID for r in rows))} patients")
    
    # Also try without CT filter if no results — some may be dual PET/CT
    if not rows:
        logger.warning("No CT series found — checking all modalities...")
        sql_all = sql.replace("AND Modality = 'CT'", "")
        rows = list(client.query(sql_all).result())
        logger.info(f"All modalities: {len(rows)} series")
        # Filter to CT + PT only
        rows = [r for r in rows if r.Modality in ('CT', 'PT')]
        
    return [dict(r) for r in rows]


# ── Step 2: Ensure destination GCS bucket exists ───────────────────────────────

def ensure_bucket(storage_client: storage.Client) -> storage.Bucket:
    try:
        bucket = storage_client.get_bucket(DEST_BUCKET)
        logger.info(f"[OK] GCS bucket gs://{DEST_BUCKET} exists.")
    except Exception:
        logger.info(f"[CREATE] Creating GCS bucket gs://{DEST_BUCKET} in regional location...")
        bucket = storage_client.bucket(DEST_BUCKET)
        bucket.storage_class = "STANDARD"
        bucket = storage_client.create_bucket(bucket, location=os.environ.get("GCP_REGION", "me-central1"))
        logger.info(f"[OK] Bucket gs://{DEST_BUCKET} created.")
    return bucket


# ── Step 3: Copy series from IDC public GCS to vura-radiomics ─────────────────

def copy_series_to_dest(series: dict, dry_run: bool) -> tuple[str, str, bool]:
    """
    Uses gsutil -m cp -r to copy a series from IDC public bucket to dest.
    Returns (patient_id, series_uid, success).
    
    IDC series_gcs_url format: gs://idc-open-data-cr/<crdc_series_uuid>/
    """
    src  = series["series_gcs_url"].rstrip("/") + "/*"
    pid  = series["PatientID"]
    sid  = series["SeriesInstanceUID"]
    mod  = series["Modality"]
    dest = f"gs://{DEST_BUCKET}/{DEST_PREFIX}/{pid}/{mod}/{sid}/"

    if dry_run:
        logger.info(f"[DRY] {pid} | {mod} | {series['instances']} files | {src} -> {dest}")
        return pid, sid, True

    cmd = [
        "gsutil", "-m", "-q",
        "cp", "-r", "-n",     # -n = no-clobber (idempotent)
        src, dest
    ]
    logger.info(f"[COPY] {pid}/{mod}/{sid[:12]}... -> {dest}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    
    if result.returncode != 0:
        logger.error(f"[FAIL] {pid}: {result.stderr[:200]}")
        return pid, sid, False
    return pid, sid, True


# ── Step 4: Update BigQuery with GCS paths ────────────────────────────────────

def update_bq_gcs_paths(client: bigquery.Client, patient_series: list[dict]):
    """
    Updates clinical_cohorts.imaging_gcs_prefix (stored as ground_truth_genotype JSON)
    with the GCS path now that images have been downloaded.
    Also updates external_genomic_labels.segmentation_mask_gcs for future use.
    """
    logger.info(f"Updating BigQuery with {len(patient_series)} GCS paths...")

    rows_to_insert = []
    for s in patient_series:
        pid       = s["PatientID"]
        case_uid  = "CASE-" + hashlib.sha256(f"TCIA_NSCLC-{pid}".encode()).hexdigest()[:16].upper()
        gcs_path  = f"gs://{DEST_BUCKET}/{DEST_PREFIX}/{pid}/CT/{s['SeriesInstanceUID']}/"
        
        rows_to_insert.append({
            "case_uid":            case_uid,
            "gcs_path":            gcs_path,
            "series_uid":          s["SeriesInstanceUID"],
            "patient_id":          pid,
            "modality":            s["Modality"],
            "instances":           s["instances"],
            "updated_at":          NOW,
        })

    # Log as a structured manifest (we don't have an imaging_registry table yet)
    manifest_path = Path("data/nsclc_gcs_manifest.jsonl")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "a") as f:
        for row in rows_to_insert:
            f.write(json.dumps(row) + "\n")
    logger.info(f"[OK] GCS manifest written to {manifest_path}")

    # Update the clinical_cohorts ground_truth_genotype field with GCS path
    # (temporary — Phase C1 will add imaging_gcs_prefix as a proper column)
    if rows_to_insert:
        update_sql = f"""
        UPDATE `{PROJECT_ID}.radiogenomics.clinical_cohorts`
        SET ground_truth_genotype = JSON_SET(
            COALESCE(ground_truth_genotype, JSON '{{}}'),
            '$.imaging_gcs_prefix', @gcs_path,
            '$.series_uid', @series_uid,
            '$.imaging_ready', true
        )
        WHERE case_uid = @case_uid
        """
        for row in rows_to_insert:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("case_uid",   "STRING", row["case_uid"]),
                    bigquery.ScalarQueryParameter("gcs_path",   "STRING", row["gcs_path"]),
                    bigquery.ScalarQueryParameter("series_uid", "STRING", row["series_uid"]),
                ]
            )
            try:
                client.query(update_sql, job_config=job_config).result()
            except Exception as e:
                logger.warning(f"  BQ update error for {row['case_uid']}: {e}")
    logger.info("[OK] BigQuery GCS paths updated.")


# ── Step 5: Trigger feature extraction for downloaded cases ───────────────────

def trigger_feature_extraction(bq_client: bigquery.Client, downloaded_uids: list[str]):
    """
    Deletes the stub feature rows for downloaded patients so extract_features.py
    will pick them up and run real PyRadiomics extraction on next run.
    """
    if not downloaded_uids:
        return
    uid_list = ", ".join(f"'{u}'" for u in downloaded_uids)
    count_before = list(bq_client.query(f"""
        SELECT COUNT(*) as cnt FROM `{PROJECT_ID}.radiogenomics.radiomics_features`
        WHERE case_uid IN ({uid_list})
          AND extraction_model_version = 'STUB-v1.0'
    """).result())[0].cnt

    if count_before > 0:
        bq_client.query(f"""
            DELETE FROM `{PROJECT_ID}.radiogenomics.radiomics_features`
            WHERE case_uid IN ({uid_list})
              AND extraction_model_version = 'STUB-v1.0'
        """).result()
        logger.info(f"[OK] Removed {count_before} stub feature rows — ready for real extraction.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NSCLC-Radiogenomics CT download from IDC")
    parser.add_argument("--dry-run",   action="store_true", help="Preview without copying")
    parser.add_argument("--patient",   type=str,  help="Single patient ID (e.g. R01-001)")
    parser.add_argument("--limit",     type=int,  help="Limit N patients for testing")
    parser.add_argument("--workers",   type=int,  default=MAX_WORKERS)
    parser.add_argument("--skip-bq",   action="store_true", help="Skip BigQuery updates")
    args = parser.parse_args()

    bq_client  = bigquery.Client(project=PROJECT_ID)
    gcs_client = storage.Client(project=PROJECT_ID)

    # 1. Ensure destination bucket exists
    if not args.dry_run:
        ensure_bucket(gcs_client)

    # 2. Get series list from IDC
    series_list = get_idc_series(
        bq_client,
        patient_filter = args.patient,
        limit          = args.limit * 20 if args.limit else None,   # ~20 series per patient avg
    )

    if not series_list:
        logger.error(f"""
[ERROR] No series found in IDC for collection '{IDC_COLLECTION_ID}'.
The NSCLC-Radiogenomics collection may not yet be mirrored in your IDC version.

Manual steps:
  1. Go to: https://imaging.datacommons.cancer.gov/explore/filters/?collection_id=nsclc_radiogenomics
  2. Click 'Download via manifest'
  3. Use: gsutil -m cp -r gs://idc-open-data/<uuid>/ gs://{DEST_BUCKET}/{DEST_PREFIX}/
        """)
        sys.exit(1)

    # Limit to N unique patients
    if args.limit:
        seen_patients = set()
        filtered = []
        for s in series_list:
            if s["PatientID"] not in seen_patients:
                if len(seen_patients) >= args.limit:
                    break
                seen_patients.add(s["PatientID"])
            filtered.append(s)
        series_list = filtered

    unique_patients = len(set(s["PatientID"] for s in series_list))
    total_instances = sum(s["instances"] for s in series_list)
    logger.info(f"\n{'='*60}")
    logger.info(f"NSCLC-Radiogenomics CT Download Plan")
    logger.info(f"  Patients : {unique_patients}")
    logger.info(f"  Series   : {len(series_list)}")
    logger.info(f"  Instances: {total_instances} DICOM files")
    logger.info(f"  Dest     : gs://{DEST_BUCKET}/{DEST_PREFIX}/")
    logger.info(f"  Mode     : {'DRY RUN' if args.dry_run else 'LIVE'}")
    logger.info(f"  Workers  : {args.workers}")
    logger.info(f"{'='*60}\n")

    # 3. Copy series in parallel
    succeeded = []
    failed    = []
    
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(copy_series_to_dest, s, args.dry_run): s
            for s in series_list
        }
        for future in as_completed(futures):
            pid, sid, ok = future.result()
            if ok:
                succeeded.append(futures[future])
            else:
                failed.append(futures[future])
            done = len(succeeded) + len(failed)
            if done % 10 == 0:
                logger.info(f"  Progress: {done}/{len(series_list)} | OK={len(succeeded)} FAIL={len(failed)}")

    # 4. Update BigQuery with GCS paths
    if not args.skip_bq and succeeded:
        update_bq_gcs_paths(bq_client, succeeded)

        # 5. Trigger real feature extraction by removing stubs
        downloaded_case_uids = list(set(
            "CASE-" + hashlib.sha256(f"TCIA_NSCLC-{s['PatientID']}".encode()).hexdigest()[:16].upper()
            for s in succeeded
        ))
        if not args.dry_run:
            trigger_feature_extraction(bq_client, downloaded_case_uids)
            logger.info(f"\nRun next: python vura-radiomics-job/extract_features.py --cohort TCIA_NSCLC")

    # Final report
    logger.info(f"\n{'='*60}")
    logger.info(f"DOWNLOAD COMPLETE")
    logger.info(f"  Succeeded : {len(succeeded)} series")
    logger.info(f"  Failed    : {len(failed)} series")
    if failed:
        for s in failed:
            logger.warning(f"  FAILED: {s['PatientID']} / {s['SeriesInstanceUID'][:16]}...")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
