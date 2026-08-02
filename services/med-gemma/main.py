from fastapi import FastAPI, Body, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Union
import time
import logging
import uuid # Added for ID generation

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("med-gemma")

app = FastAPI(title="Med-Gemma 2 Service (OpenAI Compatible Stub)")

# --- OpenAI Compatibility Schemas ---
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "gemma-2-med"
    messages: List[ChatMessage]
    max_tokens: int = 512
    temperature: float = 0.7
    stream: bool = False

class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

# --- Endpoints ---

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    Simulates OpenAI /v1/chat/completions endpoint.
    Compatible with standard AI frontends.
    """
    logger.info(f"Received chat request. Model: {request.model}")
    
    # Extract prompt from last message
    last_message = request.messages[-1].content if request.messages else ""
    logger.info(f"Last User Message: {last_message[:50]}...")

    # Simulate inference time
    time.sleep(1.0)

    # Mock Response Logic (Medical Flavor)
    response_text = "Analysis (Med-Gemma 2):\n\n"
    if "fracture" in last_message.lower():
        response_text += "The clinical presentation suggests a potential fracture. Recommend orthogonal views to confirm displacement."
    elif "pneumonia" in last_message.lower():
        response_text += "Opacities indicated. Compatible with pneumonia. Antibiotic course recommended."
    else:
        response_text += "Please provide specific clinical findings or DICOM context for a detailed analysis."

    # Construct Standard Response
    response = ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4()}",
        created=int(time.time()),
        model=request.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=response_text)
            )
        ]
    )
    
    return response

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "gemma-2-med", "object": "model", "owned_by": "google"},
            {"id": "gemma-2-9b", "object": "model", "owned_by": "google"}
        ]
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "med-gemma-openai-stub"}
