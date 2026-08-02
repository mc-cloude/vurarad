import logging
import json
from typing import Dict, Any, List

logger = logging.getLogger("vura-logic.ambient-copilot")

# Anatomical Checklist Order (Standard Radiology Workflow)
ORGAN_FLOW = [
    "Lungs", "Heart", "Mediastinum", "Liver", "Spleen", 
    "Kidneys", "Adrenals", "Bowel", "Bone", "Soft Tissue"
]

class AmbientCommand(json.JSONEncoder):
    def default(self, obj):
        return super().encode(obj)

async def process_ambient_audio(transcript: str, current_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyzes 'Roadside' voice transcripts to drive the Viewer and Report state.
    
    Example input: "Co-pilot, focus on the liver and note a 2cm hypodensity."
    """
    logger.info(f"Ambient Co-Pilot Processing: {transcript}")
    
    # 1. Action Extraction (Heuristic/LLM logic)
    # In production, this would use a Nemotron-formatted prompt
    action = "NAVIGATE"
    target_organ = None
    finding = None
    
    t_lower = transcript.lower()
    
    # Simple semantic router
    for organ in ORGAN_FLOW:
        if organ.lower() in t_lower:
            target_organ = organ
            break
            
    if "note" in t_lower or "findings" in t_lower:
        action = "DESCRIBE"
        finding = transcript.split("note" if "note" in t_lower else "findings")[-1].strip()
        
    if "approve" in t_lower or "next phase" in t_lower:
        action = "ADVANCE_WORKFLOW"

    # 2. State Resolution
    response = {
        "action": action,
        "target": target_organ,
        "finding_capture": finding,
        "feedback": f"Acknowledged: {action} {target_organ if target_organ else ''}"
    }
    
    # 3. Riva Lexicon Boosting (Signal to Riva agent - Simulated)
    # We boost medical terms mentioned in the current organ context
    if target_organ:
        response["lexicon_boost"] = [f"{target_organ} lesion", f"{target_organ} margin"]
        
    return response

async def generate_phase_narrative(phase: int, findings: List[Dict]) -> str:
    """
    Generates the structured text for one of the 4 phases.
    """
    phase_titles = {
        1: "NORMAL_ANATOMY_VERIFICATION",
        2: "RADIOMIC_QUANTIFICATION",
        3: "RADIOGENOMIC_PREDICTION",
        4: "PROTEOMIC_STRATEGY"
    }
    
    # Mock LLM generation logic
    narrative = f"[{phase_titles.get(phase, 'REPORT')}]\n"
    for f in findings:
        narrative += f"- {f.get('organ')}: {f.get('finding')}\n"
        
    return narrative
