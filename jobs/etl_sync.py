import os
import logging
from google.cloud import bigquery
from datetime import datetime, timezone  # [FIX H-14] timezone added for timezone-aware datetimes

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - VURA_ETL - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
PROJECT_ID  = os.environ.get("GCP_PROJECT_ID", "vurarad")
# [FIX M-19/C-10] Align DATASET_ID with actual schema datasets
# The correct dataset for TCIA DICOM catalog data is 'radiogenomics',
# not 'vura_analytics' (which doesn't exist)
DATASET_ID  = os.environ.get("BIGQUERY_DATASET", "radiogenomics")
# [FIX H-15] DICOM_ROOT is now env-configurable, not hardcoded
DICOM_ROOT  = os.environ.get("DICOM_ROOT", "/mnt/dicom/public")

def get_bq_client():
    return bigquery.Client(project=PROJECT_ID)

def ensure_schema(client):
    """
    Ensures BigQuery tables exist with the correct schema.
    """
    dataset_ref = client.dataset(DATASET_ID)
    
    # 1. Dimension: Patients
    table_patients_ref = dataset_ref.table("dim_patients")
    schema_patients = [
        bigquery.SchemaField("patient_uid", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("collection", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="NULLABLE"),
    ]
    try:
        client.get_table(table_patients_ref)
        logger.info("✅ Table 'dim_patients' exists.")
    except Exception:
        logger.info("⚠️ Table 'dim_patients' not found. Creating...")
        table = bigquery.Table(table_patients_ref, schema=schema_patients)
        client.create_table(table)
        logger.info("✅ Table 'dim_patients' created.")

    # 2. Fact: Studies
    table_studies_ref = dataset_ref.table("fact_studies")
    schema_studies = [
        bigquery.SchemaField("study_uid", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("patient_uid", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("modality", "STRING", mode="NULLABLE"),   # e.g., CT
        bigquery.SchemaField("body_part", "STRING", mode="NULLABLE"),   # e.g., Thorax
        bigquery.SchemaField("file_count", "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("data_source", "STRING", mode="NULLABLE"), # e.g., TCIA
        bigquery.SchemaField("gcs_path", "STRING", mode="NULLABLE"),    # GCS mount path for Forge seeding
    ]
    try:
        client.get_table(table_studies_ref)
        logger.info("✅ Table 'fact_studies' exists.")
    except Exception:
        logger.info("⚠️ Table 'fact_studies' not found. Creating...")
        table = bigquery.Table(table_studies_ref, schema=schema_studies)
        client.create_table(table)
        logger.info("✅ Table 'fact_studies' created.")

def run_etl_pipeline():
    """
    Main Entry Point: Scans GCS and Syncs to BigQuery.
    """
    logger.info(f"🚀 Starting ETL Sync for Project: {PROJECT_ID}")
    
    # [FIX H-15] Non-configurable hardcoded path silently returned. Now raises for visibility.
    if not os.path.exists(DICOM_ROOT):
        msg = (
            f"DICOM Root path {DICOM_ROOT} does not exist. "
            "Set DICOM_ROOT env var to the GCS FUSE mount point or local path."
        )
        logger.error(f"\u274c {msg}")
        raise RuntimeError(msg)  # surface in Cloud Run error logs + alerting

    client = get_bq_client()
    ensure_schema(client)
    
    # 1. EXTRACT (Scan GCS)
    logger.info(f"📂 Scanning Data Lake at {DICOM_ROOT}...")
    
    patients_buffer = []
    studies_buffer = []
    
    try:
        # Structure: /mnt/dicom/public/{Collection}/{PatientID}/{SeriesUID} or similar
        # For LCTSC from TCIA it is often: Collection -> Patient -> Study -> Series
        # We did a simple depth scan in the API, let's replicate/deepen it here.
        
        for collection in os.listdir(DICOM_ROOT):
            coll_path = os.path.join(DICOM_ROOT, collection)
            if not os.path.isdir(coll_path): continue
            
            # Assuming next level is patients
            for patient_id in os.listdir(coll_path):
                pat_path = os.path.join(coll_path, patient_id)
                if not os.path.isdir(pat_path): continue
                
                # Add to Patient Dim
                patients_buffer.append({
                    "patient_uid": patient_id,
                    "collection": collection,
                    # [FIX H-14] Use timezone-aware UTC timestamp; datetime.utcnow() is deprecated Python 3.12+
                    "ingested_at": datetime.now(timezone.utc).isoformat()
                })
                
                # Scan Studies/Series (Simplified)
                # We'll treat the immediate children as "Studies/Series" for the sake of this ETL
                # In reality TCIA struct can be deep.
                # Let's count files recursively for the "Fact" table.
                
                file_count = 0
                for root, dirs, files in os.walk(pat_path):
                    file_count += len(files)
                
                studies_buffer.append({
                    "study_uid": f"STY-{patient_id}",
                    "patient_uid": patient_id,
                    "modality": "CT",    # Assumption for LCTSC collection
                    "body_part": "Thorax",  # Assumption for LCTSC collection
                    "file_count": file_count,
                    "data_source": "TCIA",
                    "gcs_path": pat_path  # Full path for Forge reference seeding
                })
                
    except Exception as e:
        logger.error(f"❌ Extraction Error: {e}")
        return

    # 2. LOAD (Insert to BigQuery)
    if patients_buffer:
        logger.info(f"\U0001f69a Loading {len(patients_buffer)} patients into BigQuery...")
        # [FIX C-10] Fully-qualified table reference required (project.dataset.table)
        errors = client.insert_rows_json(f"{PROJECT_ID}.{DATASET_ID}.dim_patients", patients_buffer)
        if errors:
            logger.error(f"\u274c Patient Insert Errors: {errors}")
        else:
            logger.info("\u2705 Patients Data Loaded Successfully.")
            
    if studies_buffer:
        logger.info(f"\U0001f69a Loading {len(studies_buffer)} studies into BigQuery...")
        # [FIX C-10] Fully-qualified table reference required (project.dataset.table)
        errors = client.insert_rows_json(f"{PROJECT_ID}.{DATASET_ID}.fact_studies", studies_buffer)
        if errors:
            logger.error(f"\u274c Study Insert Errors: {errors}")
        else:
            logger.info("\u2705 Study Data Loaded Successfully.")

    logger.info("🎉 ETL Pipeline Completed Successfully.")
