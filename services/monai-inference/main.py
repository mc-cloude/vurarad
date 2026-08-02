from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Body
import torch
from monai.networks.nets import UNet
import logging
import time
import os
import base64
import json
from pydantic import BaseModel
from typing import Dict, Any

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monai-inference")

app = FastAPI(title="MONAI Inference (Zoo Enabled)")

# In-Memory Model Registry (Simulating Model Zoo Storage)
loaded_models = {}

# Default CPU Device
device = torch.device("cpu")

class PubSubMessage(BaseModel):
    message: Dict[str, Any]
    subscription: str

@app.on_event("startup")
async def startup_event():
    # Load a default model (e.g., Spleen) on startup
    logger.info("Initializing default bundle: spleen_ct_segmentation...")
    model = UNet(
        spatial_dims=2,
        in_channels=1,
        out_channels=2,
        channels=(16, 32, 64),
        strides=(2, 2),
    ).to(device)
    loaded_models["spleen_ct_segmentation"] = model
    logger.info("Default model loaded.")

@app.post("/zoo/download")
async def download_bundle(model_name: str, version: str = "latest", background_tasks: BackgroundTasks = None):
    """
    Simulates downloading a specific Bundle from the MONAI Zoo.
    E.g., 'whole_body_ct_segmentation', 'lung_nodule_ct_detection'
    """
    logger.info(f"Request to download bundle: {model_name} (v{version})")
    
    # In a real scenario, we would use:
    # monai.bundle.download(name=model_name, bundle_dir="./models")
    
    # Simulation: Just accept it and 'load' a dummy model
    logger.info("Simulating download from NGC/GitHub...")
    time.sleep(1) # Fake download time
    
    new_model = UNet(
        spatial_dims=2,
        in_channels=1,
        out_channels=2, # Dummy
        channels=(16, 32),
        strides=(2, 2),
    ).to(device)
    
    loaded_models[model_name] = new_model
    logger.info(f"Bundle {model_name} downloaded and loaded successfully.")
    
    return {"status": "success", "message": f"Bundle {model_name} ready."}

@app.post("/inference")
async def run_inference(file: UploadFile = File(...), model_name: str = "spleen_ct_segmentation"):
    """
    Run inference using a specific MONAI Zoo Bundle.
    """
    logger.info(f"Received file for inference: {file.filename} using model: {model_name}")
    
    if model_name not in loaded_models:
        return {"status": "error", "message": f"Model {model_name} not found. Call /zoo/download first."}
    
    # Simulate processing time
    time.sleep(2) 
    
    # Return dummy result
    return {
        "status": "success",
        "model": model_name,
        "timestamp": time.time(),
        "message": "Inference completed on CPU (Bundle Execution)"
    }

@app.post("/pubsub/handler")
async def pubsub_handler(envelope: PubSubMessage = Body(...)):
    """
    Handle incoming Pub/Sub messages (Push subscription).
    """
    try:
        # Decode the Pub/Sub message
        payload_bytes = base64.b64decode(envelope.message["data"])
        payload = json.loads(payload_bytes)
        
        study_id = payload.get("study_id")
        logger.info(f"Received Async Inference Job for Study: {study_id}")
        
        # In a real app, here we would:
        # 1. Download DICOM from GCS or Orthanc using study_id
        # 2. Run Inference
        # 3. Upload Result to GCS
        # 4. Callback to vura-logic via HTTP or Pub/Sub
        
        # Simulate work
        time.sleep(2)
        
        logger.info(f"Inference completed for Study {study_id}")
        return {"status": "success"}

    except Exception as e:
        logger.error(f"Error processing Pub/Sub message: {e}")
        # Return 200 to acknowledge message (otherwise Pub/Sub retries), 
        # unless we want retry logic.
        return {"status": "error", "detail": str(e)}


@app.get("/health")
def health():
    return {"status": "ok", "service": "monai-inference"}
