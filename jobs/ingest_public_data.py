import os
import logging
from tcia_utils import nbia

logger = logging.getLogger(__name__)

# Target GCS Fuse Mount Path
DATA_ROOT = "/mnt/dicom/public"

def ingest_data():
    logger.info("⬇️ Initiating Public Data Ingestion Protocol from TCIA...")
    
    # ensure target directory exists
    if not os.path.exists(DATA_ROOT):
        logger.info(f"creating directory {DATA_ROOT}")
        os.makedirs(DATA_ROOT, exist_ok=True)
    
    # Dataset Selection: LCTSC (Lung CT Segmentation Challenge 2017)
    # Why? It's moderate size (~60 patients), high quality CTs, and relevant for cancer/lung analysis.
    collection = "LCTSC"
    
    logger.info(f"📡 Querying TCIA for collection: {collection}")
    
    try:
        # Download Series
        # NBIA downloader will handle recursive downloading of series
        logger.info(f"💾 Downloading {collection} to {DATA_ROOT}...")
        
        # Helper to download specific number of patients (limit to 5 for initial test to save time/bandwidth)
        patients = nbia.getPatient(collection=collection)
        if not patients:
            logger.error("No patients found for collection")
            return

        target_patients = patients[0:5] # Limit to top 5 patients for the "Pilot"
        logger.info(f"🎯 Targeted Patients for Ingestion: {len(target_patients)}")
        
        for patient in target_patients:
            patient_id = patient['PatientID']
            logger.info(f"Processing Patient: {patient_id}")
            nbia.downloadSeries(collection=collection, patientId=patient_id, path=DATA_ROOT, input_type="list")
            
        logger.info(f"✅ Ingestion Complete. Data resides at {DATA_ROOT}")
        
    except Exception as e:
        logger.error(f"❌ Error communicating with TCIA: {e}")
        raise e
