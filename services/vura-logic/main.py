from fastapi import FastAPI, HTTPException, Body, BackgroundTasks, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any
import logging
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
import io
import time
import asyncio
import os
import httpx
import json

# GCP Imports
from google.cloud import pubsub_v1
from google.cloud import storage

# Configure Logging
import logging
try:
    import google.cloud.logging
    from google.cloud.logging.handlers import CloudLoggingHandler
    
    # Connect to GCP Logging if Configured
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("K_SERVICE"):
        client = google.cloud.logging.Client()
        handler = CloudLoggingHandler(client)
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
    else:
        logging.basicConfig(level=logging.INFO)

except ImportError:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("vura-logic")

app = FastAPI(title="VuraLogic Microservice (Cloud Run)")

# --- Configuration ---
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
PUBSUB_TOPIC_NAME = os.getenv("PUBSUB_TOPIC_INFERENCE", "inference-jobs")
BUCKET_NAME = os.getenv("GCS_BUCKET_REPORTS", "vura-reports")

# Initialize Clients
publisher = pubsub_v1.PublisherClient()
storage_client = storage.Client()

try:
    topic_path = publisher.topic_path(PROJECT_ID, PUBSUB_TOPIC_NAME) if PROJECT_ID else None
except Exception as e:
    logger.warning(f"Could not configure Pub/Sub path: {e}")
    topic_path = None

class ReportRequest(BaseModel):
    patient_id: str
    study_instance_uid: str
    report_content: str
    referring_physician_phone: str

class LLMQuery(BaseModel):
    query: str
    context: Optional[str] = ""

@app.get("/")
def health_check():
    return {"status": "ok", "service": "vura-logic", "cloud_run": True}

@app.post("/webhook/dicom-received")
async def on_dicom_received(payload: Dict[str, Any] = Body(...)):
    """
    Triggered by Orthanc when a new DICOM instance/study is received.
    Publishes a message to Pub/Sub for async processing (Auto-Triage).
    """
    logger.info(f"DICOM Received Webhook Triggered: {payload}")
    
    study_id = payload.get("ID", "unknown_study")
    
    if not topic_path:
        logger.error("Pub/Sub topic not configured. Cannot queue job.")
        return {"status": "error", "message": "Async processing unavailable"}

    # Publish to Pub/Sub
    try:
        data_str = json.dumps({"study_id": study_id, "payload": payload})
        data = data_str.encode("utf-8")
        future = publisher.publish(topic_path, data)
        message_id = future.result()
        logger.info(f"Published study {study_id} to {topic_path}. Message ID: {message_id}")
        return {"status": "queued", "message_id": message_id}
    except Exception as e:
        logger.error(f"Failed to publish to Pub/Sub: {e}")
        raise HTTPException(status_code=500, detail="Failed to queue study")

@app.post("/ask-llm")
async def ask_med_gemma(query: LLMQuery):
    """
    Proxy endpoint for VolView Insight to ask Med-Gemma questions.
    Uses internal service URL (assuming Cloud Run Service Directory or internal DNS).
    """
    logger.info(f"Asking Med-Gemma: {query.query}")
    med_gemma_url = os.getenv("MED_GEMMA_URL", "http://med-gemma:8000") # Use Env Var for Cloud Run URL

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            openai_payload = {
                "model": "gemma-2-med",
                "messages": [
                    {"role": "system", "content": f"Context: {query.context}"},
                    {"role": "user", "content": query.query}
                ]
            }
            
            # Authenticated call if needed (Cloud Run to Cloud Run often requires ID token)
            # For now assuming public or internal unauthenticated for simplicity, 
            # or sidecar handling auth.
            response = await client.post(f"{med_gemma_url}/v1/chat/completions", json=openai_payload)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Med-Gemma Error: {response.text}")
                raise HTTPException(status_code=500, detail="Med-Gemma unavailable")
    except Exception as e:
        logger.error(f"Error calling Med-Gemma: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/report/finalize")
async def finalize_report(request: ReportRequest):
    """
    1. Generate PDF
    2. Upload to GCS
    3. Initiate M-Pesa payment
    4. Send WhatsApp notification
    """
    logger.info(f"Finalizing report for patient {request.patient_id}")

    # 1. Generate PDF
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.drawString(100, 750, f"Radiology Report - {request.patient_id}")
    p.drawString(100, 730, f"Study UID: {request.study_instance_uid}")
    p.drawString(100, 700, "Findings:")
    text = p.beginText(100, 680)
    for line in request.report_content.split('\n'):
        text.textLine(line)
    p.drawText(text)
    p.save()
    pdf_bytes = buffer.getvalue()
    
    # 2. Upload to GCS
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob_name = f"reports/{request.study_instance_uid}.pdf"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(pdf_bytes, content_type="application/pdf")
        logger.info(f"PDF uploaded to gs://{BUCKET_NAME}/{blob_name}")
        public_url = blob.public_url # Or signed URL
    except Exception as e:
        logger.error(f"GCS Upload failed: {e}")
        # Continue? or Fail? Let's fail for now.
        raise HTTPException(status_code=500, detail=f"Storage failure: {e}")

    # 3. Mock M-Pesa
    # (Leaving mock for now, but in real life we'd call the API)
    # payment_success = await mock_mpesa_stk_push(...)
    
    return {"status": "completed", "report_url": f"gs://{BUCKET_NAME}/{blob_name}"}

# --- Legacy / Stubs ---
# Keeping endpoints for compatibility but they should be refactored too.

