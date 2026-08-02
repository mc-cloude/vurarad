"""
dicom_harvester.py — Vision 2026 Mass Metadata Extraction
Transforms raw GCS DICOMs into a Canonical Data-Oriented Index in BigQuery.
"""

import os
import json
import logging
import pydicom
from datetime import datetime, timezone
from google.cloud import bigquery, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s - HARVESTER - %(levelname)s - %(message)s")
logger = logging.getLogger("vura-harvester")

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "vurarad")
BUCKET_NAME = os.environ.get("RADIOMICS_BUCKET", "vura-radiomics")
DATASET_ID = "radiogenomics"
TABLE_ID = "dicom_canonical_index"

client_bq = bigquery.Client(project=PROJECT_ID)
client_gcs = storage.Client(project=PROJECT_ID)

def harvest_metadata(blob_name: str):
    """Downloads a DICOM blob, extracts Part 3 metadata, and maps to BQ."""
    local_tmp = "/tmp/harvest.dcm"
    bucket = client_gcs.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_name)
    
    try:
        blob.download_to_filename(local_tmp)
        ds = pydicom.dcmread(local_tmp, stop_before_pixels=True)
        
        # 1. High-fidelity extraction
        metadata = {}
        for element in ds:
            if element.tag.group < 0x7fe0: # Skip pixel data
                try:
                    metadata[str(element.tag)] = {
                        "name": element.name,
                        "value": str(element.value)
                    }
                except:
                    pass

        # 2. Map to Canonical Schema
        row = {
            "sop_instance_uid":   str(ds.get("SOPInstanceUID", "UKN")),
            "series_instance_uid": str(ds.get("SeriesInstanceUID")),
            "study_instance_uid":  str(ds.get("StudyInstanceUID")),
            "patient_id":         str(ds.get("PatientID")),
            "modality":           str(ds.get("Modality")),
            "study_date":         datetime.strptime(ds.StudyDate, '%Y%m%d').date().isoformat() if hasattr(ds, 'StudyDate') and ds.StudyDate else None,
            "body_part_examined": str(ds.get("BodyPartExamined")),
            # Part 3 Tag Mirroring
            "full_metadata":      json.dumps(metadata),
            "pixel_spacing":      str(ds.get("PixelSpacing", [])),
            "slice_thickness":    float(ds.get("SliceThickness", 0.0)) if ds.get("SliceThickness") else None,
            "kvp":                float(ds.get("KVP", 0.0)) if ds.get("KVP") else None,
            "exposure_time":      int(ds.get("ExposureTime", 0)) if ds.get("ExposureTime") else None,
            "gcs_path":           f"gs://{BUCKET_NAME}/{blob_name}",
            "ingested_at":        datetime.now(timezone.utc).isoformat()
        }
        
        return row
    except Exception as e:
        logger.error(f"Failed to harvest {blob_name}: {e}")
        return None
    finally:
        if os.path.exists(local_tmp):
            os.remove(local_tmp)

def run_harvest(prefix: str, limit: int = 100):
    """Main loop for harvesting a bucket prefix."""
    logger.info(f"🚀 Starting Mass Metadata Harvest: gs://{BUCKET_NAME}/{prefix} (limit={limit})")
    
    blobs = list(client_gcs.list_blobs(BUCKET_NAME, prefix=prefix, max_results=limit))
    rows = []
    
    for blob in blobs:
        if blob.name.endswith(".dcm"):
            row = harvest_metadata(blob.name)
            if row:
                rows.append(row)
    
    if rows:
        logger.info(f"🚚 Streaming {len(rows)} records to BigQuery...")
        table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
        errors = client_bq.insert_rows_json(table_ref, rows)
        if errors:
            logger.error(f"BQ Insert Errors: {errors[:2]}")
        else:
            logger.info("✅ Harvest Batch Successful.")
    
if __name__ == "__main__":
    # Example: Harvest the LCTSC collection pilot
    run_harvest("nsclc-radiogenomics/", limit=10)
