import os
import sys
import logging

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - VURA_FORGE - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    job_type = os.environ.get("JOB_TYPE", "SYNTHETIC")
    logger.info(f"🚀 Starting Vura Forge Job. Mode: {job_type}")

    if job_type == "INGEST_PUBLIC":
        try:
            from jobs.ingest_public_data import ingest_data
            ingest_data()
        except ImportError:
            logger.error("❌ Could not import ingest_public_data module.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"❌ Ingestion Failed: {e}")
            sys.exit(1)

    elif job_type == "ETL_SYNC":
        try:
            from jobs.etl_sync import run_etl_pipeline
            run_etl_pipeline()
        except ImportError as e:
            logger.error(f"❌ Could not import etl_sync module: {e}")
            sys.exit(1)
        except Exception as e:
            logger.error(f"❌ ETL Sync Failed: {e}")
            sys.exit(1)

    elif job_type == "SYNTHETIC":
        logger.info("🎨 Starting Synthetic Data Generation Sequence...")
        # Placeholder for synthetic logic
        # from jobs.generate_synthetic import run_genesis
        # run_genesis()
        logger.info("✅ Synthetic Generation Complete (Mocked).")
    
    else:
        logger.error(f"❌ Unknown JOB_TYPE: {job_type}")
        sys.exit(1)

if __name__ == "__main__":
    main()
