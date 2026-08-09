"""Synthetic de-identification overlay corpus + manifest of known PHI locations.

A deterministic, PIL-rendered corpus of synthetic radiological overlays used by
the measured-recall validation gate (``tests/validation/test_deid_recall.py``)
and the validation report (``app/tools/deid_validation_report.py``).

Each synthetic image burns in a mix of **PHI** regions (patient names, MRNs,
dates) and **clinical annotations** (laterality, measurements, scale bars) as
dark text on a light-gray background — the overlay shape that is worst in the
primary market (ultrasound / secondary capture).  The manifest records the
ground-truth bounding box and label of every region so recall can be measured
against the per-modality floors without a real OCR transcriber: the
:class:`~app.services.deid.ocr.ThresholdOcrEngine` *localises* regions (genuine
connected-component detection); the manifest supplies the labels.

The manifest is checked in as ``manifest.json`` (the declarative corpus
definition); this module renders the images from it deterministically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageDraw, ImageFont

from app.services.deid.ocr import BBox

DATA_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = DATA_DIR / "manifest.json"

# A bold DejaVu face shipped on the build image — deterministic rendering.
_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_SIZE = 14
_BACKGROUND = 220  # light gray
_INK = 30  # dark text


# ---------------------------------------------------------------------------
# Ground-truth value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GroundTruthRegion:
    """One burned-in region with its ground-truth box, text, and label."""

    bbox: BBox
    text: str
    is_phi: bool
    kind: str  # PATIENT, ID, DATE, LATERALITY, MEASUREMENT, SCALE_BAR


@dataclass(frozen=True, slots=True)
class CorpusImage:
    """One rendered synthetic image + its ground-truth regions."""

    image_id: str
    modality: str
    manufacturer: str
    array: npt.NDArray[np.uint8]  # uint8 grayscale
    regions: list[GroundTruthRegion]


# ---------------------------------------------------------------------------
# Manifest builder — deterministic corpus definition
# ---------------------------------------------------------------------------
# Nine modalities grouped by recall floor.  Two manufacturers each so the
# coverage section of the validation report names real (modality, manufacturer)
# pairs.  Three images per (modality, manufacturer) with three PHI + two
# clinical regions each.
_MODALITIES: list[tuple[str, list[str]]] = [
    ("US", ["AcmeUltrasound", "SonixPro"]),
    ("SC", ["CaptureCo", "GeneralImaging"]),
    ("OT", ["OtherMod", "MiscCorp"]),
    ("XC", ["XrayCapture", "FilmScan"]),
    ("CR", ["CRVendor", "XrayDigital"]),
    ("DX", ["DXSystems", "FlatPanel"]),
    ("MG", ["MamoTech", "BreastScan"]),
    ("CT", ["CTScanCo", "TomoVendor"]),
    ("MR", ["MRIWorks", "MagnetCo"]),
]

_PHI_NAMES = ["John Doe", "Jane Smith", "Robert Brown"]
_PHI_MRNS = ["MRN:12345", "MRN:67890", "MRN:54321"]
_PHI_DATES = ["1980-01-15", "1975-03-22", "1990-11-08"]
_CLINICAL = [
    ("LEFT", "LATERALITY"),
    ("15.2 cm", "MEASUREMENT"),
]


def build_manifest() -> dict[str, Any]:
    """Build the deterministic corpus definition as a serialisable dict."""
    images: list[dict[str, Any]] = []
    for modality, manufacturers in _MODALITIES:
        for manufacturer in manufacturers:
            for img_idx in range(3):
                regions: list[dict[str, Any]] = []
                # Three PHI regions down the left column.
                phi_texts = [
                    (_PHI_NAMES[img_idx], "PATIENT"),
                    (_PHI_MRNS[img_idx], "ID"),
                    (_PHI_DATES[img_idx], "DATE"),
                ]
                for row, (text, kind) in enumerate(phi_texts):
                    regions.append(
                        {
                            "text": text,
                            "x": 8,
                            "y": 8 + row * 20,
                            "is_phi": True,
                            "kind": kind,
                        }
                    )
                # Two clinical annotations in the right column.
                for row, (text, kind) in enumerate(_CLINICAL):
                    regions.append(
                        {
                            "text": text,
                            "x": 180,
                            "y": 8 + row * 20,
                            "is_phi": False,
                            "kind": kind,
                        }
                    )
                images.append(
                    {
                        "id": f"{modality.lower()}-{manufacturer.lower()}-{img_idx + 1:02d}",
                        "modality": modality,
                        "manufacturer": manufacturer,
                        "width": 256,
                        "height": 96,
                        "regions": regions,
                    }
                )
    return {"version": 1, "font": "DejaVuSans-Bold", "images": images}


def load_manifest() -> dict[str, Any]:
    """Load the checked-in manifest.json."""
    with MANIFEST_PATH.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Rendering — manifest -> CorpusImage (with ground-truth bboxes)
# ---------------------------------------------------------------------------
def _font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    except OSError:  # pragma: no cover — fallback for headless CI without DejaVu
        return ImageFont.load_default()


def render_image(entry: dict[str, Any]) -> CorpusImage:
    """Render one manifest entry into a :class:`CorpusImage` with ground truth."""
    width = int(entry["width"])
    height = int(entry["height"])
    img = Image.new("L", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(img)
    font = _font()
    regions: list[GroundTruthRegion] = []
    for r in entry["regions"]:
        x, y = int(r["x"]), int(r["y"])
        text = str(r["text"])
        draw.text((x, y), text, fill=_INK, font=font)
        bbox = draw.textbbox((x, y), text, font=font)
        regions.append(
            GroundTruthRegion(
                bbox=BBox(bbox[0], bbox[1], bbox[2], bbox[3]),
                text=text,
                is_phi=bool(r["is_phi"]),
                kind=str(r["kind"]),
            )
        )
    return CorpusImage(
        image_id=str(entry["id"]),
        modality=str(entry["modality"]),
        manufacturer=str(entry["manufacturer"]),
        array=np.array(img, dtype=np.uint8),
        regions=regions,
    )


def render_corpus() -> list[CorpusImage]:
    """Render every image in the checked-in manifest."""
    manifest = load_manifest()
    return [render_image(entry) for entry in manifest["images"]]


# ---------------------------------------------------------------------------
# Coverage helpers — for the validation report
# ---------------------------------------------------------------------------
def covered_pairs(manifest: dict[str, Any] | None = None) -> set[tuple[str, str]]:
    """Return the set of ``(modality, manufacturer)`` pairs the corpus covers."""
    if manifest is None:
        manifest = load_manifest()
    return {(str(img["modality"]), str(img["manufacturer"])) for img in manifest["images"]}


def covered_modalities(manifest: dict[str, Any] | None = None) -> set[str]:
    """Return the set of modalities the corpus covers."""
    if manifest is None:
        manifest = load_manifest()
    return {str(img["modality"]) for img in manifest["images"]}
