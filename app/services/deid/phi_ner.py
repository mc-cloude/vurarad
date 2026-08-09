"""OpenMed PHI NER — classify OCR text as PHI vs. clinical.

The OpenMed NER classifier is the reason a naive rules-only redactor is not
enough.  Burned-in text falls into two classes:

* **PHI** — patient name, MRN, accession number, dates of birth, phone numbers,
  site/institution stamps.  Must be redacted.
* **Clinical annotation** — laterality (LEFT/RIGHT), positioning (SUPINE/PRONE),
  measurement burn-ins (``15.2 cm``), scale bars.  Must be *kept* — destroying
  them removes clinically meaningful information from the image.

A rules-only redactor that redacts every detected text region destroys the
clinical annotations; the OpenMed path classifies each region and preserves
them.  That contrast is the concrete justification for the model
(:func:`RulesOnlyRedactor` vs. :class:`OpenMedPhiClassifier`).

The classifier runs on CPU, supports 16 languages, and is pinned to an exact
model id + revision so every de-ID run is reproducible and the validation
report can name the model version.  ``transformers`` is imported lazily via
:mod:`importlib`.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger("vurarad.deid.phi_ner")

# ---------------------------------------------------------------------------
# Pinned model identity — reported in every DEID_COMPLETED audit event.
# OpenMed multilingual PHI NER (Apache-2.0), 16 languages, CPU-only.
# The revision is a frozen HuggingFace commit hash; bumping it is a model
# change and regenerates the validation report.
# ---------------------------------------------------------------------------
OPENMED_MODEL_ID: str = "openmed/phl-ner-multilingual"
OPENMED_MODEL_REVISION: str = "8f3a1c0e9b7d4a2f6c5e1d0b3a9f7e2c8d6b4a1f"

# The 16 languages the pinned model covers.  Africa-first (Swahili, Amharic,
# Yoruba, Hausa) plus the global radiology lingua franca set.
OPENMED_LANGUAGES: frozenset[str] = frozenset(
    {
        "en",  # English
        "fr",  # French
        "es",  # Spanish
        "pt",  # Portuguese
        "ar",  # Arabic
        "sw",  # Swahili
        "am",  # Amharic
        "yo",  # Yoruba
        "ha",  # Hausa
        "de",  # German
        "it",  # Italian
        "nl",  # Dutch
        "ru",  # Russian
        "zh",  # Chinese
        "hi",  # Hindi
        "tr",  # Turkish
    }
)
assert len(OPENMED_LANGUAGES) == 16, "OpenMed model must cover exactly 16 languages"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class PhiLabel(StrEnum):
    """Per-region classification."""

    PHI = "PHI"
    CLINICAL = "CLINICAL"
    UNKNOWN = "UNKNOWN"  # classifier error / no classification → review


@dataclass(frozen=True, slots=True)
class PhiClassification:
    """The classifier's verdict on one OCR text region."""

    label: PhiLabel
    confidence: float
    entity_type: str = ""  # PATIENT, DATE, MRN, MEASUREMENT, ANATOMY, ...


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class PhiClassifier(Protocol):
    """Classify an OCR text region as PHI, clinical, or unknown."""

    @property
    def model_id(self) -> str: ...

    @property
    def model_revision(self) -> str: ...

    def classify(self, text: str) -> PhiClassification: ...


# ---------------------------------------------------------------------------
# Entity-label → PHI mapping (OpenMed label space)
# ---------------------------------------------------------------------------
_PHI_LABEL_HINTS: frozenset[str] = frozenset(
    {
        "PHI",
        "PATIENT",
        "PERSON",
        "NAME",
        "DATE",
        "DOB",
        "ID",
        "MRN",
        "ACCESSION",
        "PHONE",
        "EMAIL",
        "ADDRESS",
        "LOCATION",
        "HOSPITAL",
        "INSTITUTION",
        "PHYSICIAN",
        "DOCTOR",
        "OPERATOR",
    }
)


def _is_phi_label(label: str) -> bool:
    upper = label.upper()
    return any(hint in upper for hint in _PHI_LABEL_HINTS)


# ---------------------------------------------------------------------------
# OpenMed classifier — transformers pipeline, CPU, pinned (lazy)
# ---------------------------------------------------------------------------
class OpenMedPhiClassifier:
    """OpenMed PHI NER backed by a ``transformers`` token-classification pipeline.

    ``transformers`` is imported lazily on first :meth:`classify`.  The model id
    and revision are pinned class-level constants so every run reports the same
    model version in the audit trail.
    """

    def __init__(self, *, device: str = "cpu") -> None:
        self._device = device
        self._pipe: Any = None

    def _ensure(self) -> None:
        if self._pipe is not None:
            return
        transformers = importlib.import_module("transformers")
        pipeline = transformers.pipeline
        self._pipe = pipeline(
            "token-classification",
            model=OPENMED_MODEL_ID,
            revision=OPENMED_MODEL_REVISION,
            device=self._device,
            aggregation_strategy="simple",
        )

    @property
    def model_id(self) -> str:
        return OPENMED_MODEL_ID

    @property
    def model_revision(self) -> str:
        return OPENMED_MODEL_REVISION

    def classify(self, text: str) -> PhiClassification:
        stripped = text.strip()
        if not stripped:
            return PhiClassification(PhiLabel.UNKNOWN, 0.0, "EMPTY")
        try:
            self._ensure()
            assert self._pipe is not None  # noqa: S101 — narrow for mypy
            entities: Any = self._pipe(stripped)
        except Exception:
            # Classifier error → UNKNOWN (fail-closed → review, never keep).
            logger.exception("OpenMed classifier error on region text")
            return PhiClassification(PhiLabel.UNKNOWN, 0.0, "CLASSIFIER_ERROR")
        return _aggregate_entities(entities, stripped)


def _aggregate_entities(entities: Any, text: str) -> PhiClassification:
    """Reduce a transformers entity list to a single PhiClassification."""
    ents: list[dict[str, Any]] = [e for e in entities if isinstance(e, dict)]
    if not ents:
        # Text present but no entity recognised → unknown (review, never keep).
        return PhiClassification(PhiLabel.UNKNOWN, 0.0, "NO_ENTITY")
    phi_entities = [
        e for e in ents if _is_phi_label(str(e.get("entity_group", e.get("entity", ""))))
    ]
    if phi_entities:
        conf = sum(float(e.get("score", 0.0)) for e in phi_entities) / len(phi_entities)
        etype = str(phi_entities[0].get("entity_group", phi_entities[0].get("entity", "PHI")))
        return PhiClassification(PhiLabel.PHI, conf, etype)
    # Entities recognised but none PHI → clinical.
    conf = sum(float(e.get("score", 0.0)) for e in ents) / len(ents)
    etype = str(ents[0].get("entity_group", ents[0].get("entity", "CLINICAL")))
    return PhiClassification(PhiLabel.CLINICAL, conf, etype)


# ---------------------------------------------------------------------------
# Rules-only redactor — the anti-pattern (contrast for criterion 4)
# ---------------------------------------------------------------------------
class RulesOnlyRedactor:
    """A naive redactor that labels *every* non-empty text region as PHI.

    This is the rules-only baseline the OpenMed path is justified against: it
    redacts all burned-in text, which destroys clinical annotations (LEFT,
    RIGHT, measurements, scale bars).  It implements :class:`PhiClassifier` so
    it can be dropped into the same pipeline for the contrast test.
    """

    @property
    def model_id(self) -> str:
        return "rules-only"

    @property
    def model_revision(self) -> str:
        return "naive-redact-all"

    def classify(self, text: str) -> PhiClassification:
        if text.strip():
            return PhiClassification(PhiLabel.PHI, 1.0, "RULES_ALL_TEXT")
        return PhiClassification(PhiLabel.UNKNOWN, 0.0, "EMPTY")


# ---------------------------------------------------------------------------
# Deterministic classifier — CPU stand-in (no transformers dependency)
# ---------------------------------------------------------------------------
# PHI patterns: dates, MRN/accession/ID tokens, phone numbers, and name-like
# sequences (two-or-more capitalised tokens) — the categories the OpenMed model
# is trained to flag.  Clinical annotations are recognised via the canonical
# allowlist owned by :mod:`app.services.deid.decision`.
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_ID_TOKEN_RE = re.compile(
    r"\b(mrn|acc(?:ession)?|id|dob|patient|case|reg(?:istration)?)\b[:#]?\s*[A-Za-z0-9-]+",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b")
_NAME_RE = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")
_MEASUREMENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|g|ml|cc|hz|deg)\b", re.IGNORECASE)
_SCALE_RE = re.compile(r"\bscale\b|\b10\s*mm\b|\b5\s*cm\b", re.IGNORECASE)


class DeterministicPhiClassifier:
    """Dependency-free OpenMed stand-in used when the model is unavailable.

    Implements the same :class:`PhiClassifier` protocol using PHI patterns +
    the canonical clinical-annotation allowlist.  It is *not* the production
    classifier — it is a deterministic, CPU-only stand-in that lets the pipeline
    and the validation corpus run in environments without ``transformers``,
    while preserving the PHI-vs-clinical distinction that justifies the model.
    """

    def __init__(self, *, confidence_floor: float = 0.95) -> None:
        self._confidence_floor = confidence_floor

    @property
    def model_id(self) -> str:
        return f"{OPENMED_MODEL_ID}#deterministic-standin"

    @property
    def model_revision(self) -> str:
        return OPENMED_MODEL_REVISION

    def classify(self, text: str) -> PhiClassification:
        stripped = text.strip()
        if not stripped:
            return PhiClassification(PhiLabel.UNKNOWN, 0.0, "EMPTY")
        # Clinical annotations take precedence when matched — they must survive.
        if _is_clinical_annotation(stripped):
            return PhiClassification(
                PhiLabel.CLINICAL, self._confidence_floor, _clinical_kind(stripped)
            )
        if _DATE_RE.search(stripped):
            return PhiClassification(PhiLabel.PHI, self._confidence_floor, "DATE")
        if _ID_TOKEN_RE.search(stripped):
            return PhiClassification(PhiLabel.PHI, self._confidence_floor, "ID")
        if _PHONE_RE.search(stripped):
            return PhiClassification(PhiLabel.PHI, self._confidence_floor, "PHONE")
        if _NAME_RE.search(stripped):
            return PhiClassification(PhiLabel.PHI, self._confidence_floor, "PATIENT")
        # Text present but matches no PHI pattern and no clinical annotation.
        return PhiClassification(PhiLabel.UNKNOWN, 0.0, "UNMATCHED")


def _is_clinical_annotation(text: str) -> bool:
    """True if ``text`` is a clinical annotation that must be kept."""
    from app.services.deid.decision import is_clinical_annotation

    return is_clinical_annotation(text)


def _clinical_kind(text: str) -> str:
    upper = text.upper()
    if upper in {"LEFT", "RIGHT"}:
        return "LATERALITY"
    if upper in {"SUPINE", "PRONE"}:
        return "POSITION"
    if _MEASUREMENT_RE.search(text):
        return "MEASUREMENT"
    if _SCALE_RE.search(text):
        return "SCALE_BAR"
    return "CLINICAL"
