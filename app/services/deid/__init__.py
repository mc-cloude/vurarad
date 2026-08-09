"""De-identification pipeline — tags, OCR, OpenMed PHI NER, review, validation.

Two layers, in this exact order:

1. **Tag scrubbing** (``tags.py``) — remove/hash PHI-bearing DICOM attributes per
   PS3.15 Annex E.  Necessary but *never* sufficient: it cannot touch pixel data.
2. **Burned-in-text pass** — OCR (``ocr.py``) finds text regions in the pixel
   data; the OpenMed PHI NER classifier (``phi_ner.py``) labels each region PHI
   vs. clinical; ``decision.py`` resolves keep / redact / review; ``redact.py``
   box-fills PHI regions and mints a fresh SOP Instance UID; low-confidence and
   forced-modality regions route to ``review_queue.py``.

``pipeline.py`` orchestrates the layers and is **fail-closed**: a classifier
error or an OCR-text-with-no-classification region routes to *review*, never to
*keep*.  Recall on PHI — not accuracy — is the reported metric, enforced per
modality by build-failing floors (``tests/validation/test_deid_recall.py``).
"""

from app.services.deid.pipeline import DeidPipeline, DeidResult, DeidRunConfig

__all__ = ["DeidPipeline", "DeidResult", "DeidRunConfig"]
