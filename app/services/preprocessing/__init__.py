"""Pre-processing service package — orchestrator + stage handlers (§7.10.2).

Pre-processing is the pipeline that runs before the radiologist opens the study:
segmentation → volumetry → priors.  Each stage degrades gracefully — a failure
or unavailable model never blocks reading; it produces an empty findings panel
with a reason (acceptance criterion 6).
"""

from app.services.preprocessing.orchestrator import PreprocessingOrchestrator
from app.services.preprocessing.priors import PriorsHandler, PriorStudyRef
from app.services.preprocessing.segmentation_dispatcher import (
    SegmentationDispatcher,
    SegmentationPlan,
)
from app.services.preprocessing.volumetry import VolumetryHandler

__all__ = [
    "PriorsHandler",
    "PreprocessingOrchestrator",
    "PriorStudyRef",
    "SegmentationDispatcher",
    "SegmentationPlan",
    "VolumetryHandler",
]
