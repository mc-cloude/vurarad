"""OCR — locate and transcribe burned-in text in DICOM pixel data.

The pixel pass of de-identification.  An :class:`OcrEngine` finds text regions
in a decoded pixel array and returns bounding boxes plus transcribed text; the
OpenMed classifier (:mod:`app.services.deid.phi_ner`) then labels each region.

Three engines share one :class:`OcrEngine` protocol:

* :class:`PaddleOcrEngine` — production default for Latin + CJK scripts.
* :class:`TesseractEngine` — the open-source fallback (Tesseract OCR).
* :class:`ThresholdOcrEngine` — a dependency-free engine that performs *genuine*
  region localisation via luminance thresholding + connected-component
  labelling.  It does not transcribe (``text`` is empty), so downstream
  classification treats its regions as unclassified → review (fail-closed).  It
  is also the detector under the synthetic validation corpus, where the test
  harness supplies transcriptions from the ground-truth manifest.

``paddleocr`` and ``pytesseract`` are optional, lazily imported via
:mod:`importlib` so this module imports cleanly when they are absent.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

logger = logging.getLogger("vurarad.deid.ocr")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BBox:
    """An integer pixel rectangle (half-open in x1/y1 for width/height)."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return max(0, self.x1 - self.x0)

    @property
    def height(self) -> int:
        return max(0, self.y1 - self.y0)

    @property
    def area(self) -> int:
        return self.width * self.height

    def iou(self, other: BBox) -> float:
        ix0, iy0 = max(self.x0, other.x0), max(self.y0, other.y0)
        ix1, iy1 = min(self.x1, other.x1), min(self.y1, other.y1)
        inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0


@dataclass(slots=True)
class OcrRegion:
    """One detected text region — bbox, transcribed text, OCR confidence."""

    bbox: BBox
    text: str
    confidence: float
    frame_index: int = 0


# ---------------------------------------------------------------------------
# Engine protocol
# ---------------------------------------------------------------------------
class OcrEngine(Protocol):
    """Locate and transcribe text regions in a decoded pixel array."""

    @property
    def version(self) -> str:
        """A pinned, reported version string for the audit trail."""
        ...

    def detect(self, image: npt.NDArray[np.uint8]) -> list[OcrRegion]:
        """Return every detected text region in ``image`` (per-frame)."""
        ...


# ---------------------------------------------------------------------------
# PaddleOCR — production default (lazy)
# ---------------------------------------------------------------------------
class PaddleOcrEngine:
    """PaddleOCR-backed engine.  ``paddleocr`` is imported lazily on first use."""

    def __init__(self, *, lang: str = "en", use_gpu: bool = False) -> None:
        self._lang = lang
        self._use_gpu = use_gpu
        self._engine: Any = None
        self._version: str | None = None

    def _ensure(self) -> None:
        if self._engine is not None:
            return
        mod = importlib.import_module("paddleocr")
        self._engine = mod.PaddleOCR(use_angle_cls=True, lang=self._lang, use_gpu=self._use_gpu)
        self._version = f"paddleocr:{getattr(mod, '__version__', 'unknown')}"

    @property
    def version(self) -> str:
        if self._version is None:
            # Report the configured engine without forcing the heavy import.
            return f"paddleocr:lazy(lang={self._lang})"
        return self._version

    def detect(self, image: npt.NDArray[np.uint8]) -> list[OcrRegion]:
        self._ensure()
        assert self._engine is not None  # noqa: S101 — narrow for mypy
        result: Any = self._engine.ocr(image, cls=True)
        regions: list[OcrRegion] = []
        for entry in _flatten_ocr_result(result):
            box_pts, (text, conf) = entry
            x0 = int(min(p[0] for p in box_pts))
            y0 = int(min(p[1] for p in box_pts))
            x1 = int(max(p[0] for p in box_pts))
            y1 = int(max(p[1] for p in box_pts))
            regions.append(OcrRegion(BBox(x0, y0, x1, y1), text.strip(), float(conf)))
        return regions


# ---------------------------------------------------------------------------
# Tesseract — open-source fallback (lazy)
# ---------------------------------------------------------------------------
class TesseractEngine:
    """Tesseract-backed engine.  ``pytesseract`` is imported lazily on first use."""

    def __init__(self, *, lang: str = "eng") -> None:
        self._lang = lang
        self._version: str | None = None

    def _ensure(self) -> Any:
        if self._version is None:
            mod = importlib.import_module("pytesseract")
            self._version = f"tesseract:{mod.get_tesseract_version()}"
            return mod
        return importlib.import_module("pytesseract")

    @property
    def version(self) -> str:
        if self._version is None:
            return f"tesseract:lazy(lang={self._lang})"
        return self._version

    def detect(self, image: npt.NDArray[np.uint8]) -> list[OcrRegion]:
        pytesseract = self._ensure()
        from PIL import Image

        img = Image.fromarray(image)
        data: Any = pytesseract.image_to_data(
            img, lang=self._lang, output_type=pytesseract.Output.DICT
        )
        regions: list[OcrRegion] = []
        lefts = list(data["left"])
        tops = list(data["top"])
        widths = list(data["width"])
        heights = list(data["height"])
        texts = list(data["text"])
        confs = list(data["conf"])
        for left, top, w, h, text, conf in zip(
            lefts, tops, widths, heights, texts, confs, strict=False
        ):
            if not text or not str(text).strip():
                continue
            try:
                confidence = float(conf)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0:
                continue
            bbox = BBox(int(left), int(top), int(left) + int(w), int(top) + int(h))
            regions.append(OcrRegion(bbox, str(text).strip(), confidence))
        return _merge_word_regions(regions)


# ---------------------------------------------------------------------------
# Threshold engine — dependency-free, genuine region localisation
# ---------------------------------------------------------------------------
# Below this luminance (0-255) a pixel is "ink".  Tuned for light-background
# radiological overlays; the validation corpus renders dark text on light gray.
DEFAULT_INK_THRESHOLD: int = 128
# Ignore components smaller than this many ink pixels (sensor noise, speckle).
DEFAULT_MIN_AREA: int = 8


class ThresholdOcrEngine:
    """Dependency-free OCR that localises — but does not transcribe — text.

    Performs genuine luminance thresholding + connected-component labelling to
    find text regions.  Because it has no recogniser, ``text`` is always empty;
    the OpenMed classifier therefore treats every region as unclassified, which
    routes it to review (fail-closed).  The validation harness subclasses or
    wraps this engine to supply manifest transcriptions for genuine detection
    recall measurement.
    """

    def __init__(
        self,
        *,
        ink_threshold: int = DEFAULT_INK_THRESHOLD,
        min_area: int = DEFAULT_MIN_AREA,
        version_tag: str = "threshold-v1",
    ) -> None:
        self._ink_threshold = ink_threshold
        self._min_area = min_area
        self._version_tag = version_tag

    @property
    def version(self) -> str:
        return self._version_tag

    def detect(self, image: npt.NDArray[np.uint8]) -> list[OcrRegion]:
        boxes = detect_text_regions(
            image, ink_threshold=self._ink_threshold, min_area=self._min_area
        )
        return [OcrRegion(b, "", 0.9) for b in boxes]


def detect_text_regions(
    image: npt.NDArray[np.uint8],
    *,
    ink_threshold: int = DEFAULT_INK_THRESHOLD,
    min_area: int = DEFAULT_MIN_AREA,
) -> list[BBox]:
    """Genuine pixel-based text-region localisation.

    Converts to luminance, thresholds to an ink mask, labels connected
    components via flood fill, and returns the bounding box of every component
    whose area meets ``min_area``.  No transcription — this is localisation only.
    """
    gray = _to_gray(image)
    mask = gray < ink_threshold
    boxes = _connected_component_boxes(mask, min_area)
    return _merge_adjacent(boxes)


# ---------------------------------------------------------------------------
# Detection internals — pure numpy + python (no scipy dependency)
# ---------------------------------------------------------------------------
def _to_gray(image: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] >= 3:
        # Rec. 601 luma.
        weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
        flat = image[:, :, :3].astype(np.float32) @ weights
        return flat.astype(np.uint8)
    if image.ndim == 3 and image.shape[2] == 1:
        return image[:, :, 0]
    # Fallback: mean over trailing axis.
    mean: npt.NDArray[np.uint8] = image.mean(axis=-1).astype(np.uint8)
    return mean


def _connected_component_boxes(
    mask: npt.NDArray[np.bool_],
    min_area: int,
) -> list[BBox]:
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=np.bool_)
    boxes: list[BBox] = []
    for y in range(h):
        for x in range(w):
            if mask[y, x] and not visited[y, x]:
                comp = _flood_fill(mask, visited, y, x)
                if comp.area >= min_area:
                    boxes.append(comp)
    return boxes


def _flood_fill(
    mask: npt.NDArray[np.bool_],
    visited: npt.NDArray[np.bool_],
    sy: int,
    sx: int,
) -> BBox:
    h, w = mask.shape
    stack: list[tuple[int, int]] = [(sy, sx)]
    x0 = x1 = sx
    y0 = y1 = sy
    area = 0
    while stack:
        y, x = stack.pop()
        if y < 0 or y >= h or x < 0 or x >= w:
            continue
        if visited[y, x] or not mask[y, x]:
            continue
        visited[y, x] = True
        area += 1
        if x < x0:
            x0 = x
        if x > x1:
            x1 = x
        if y < y0:
            y0 = y
        if y > y1:
            y1 = y
        stack.extend([(y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)])
    return BBox(x0, y0, x1 + 1, y1 + 1)


def _merge_adjacent(boxes: list[BBox], *, gap: int = 4) -> list[BBox]:
    """Merge boxes separated by a small gap on the same text line."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b.y0, b.x0))
    merged: list[BBox] = [boxes[0]]
    for b in boxes[1:]:
        last = merged[-1]
        same_row = abs(b.y0 - last.y0) <= max(b.height, last.height) // 2 + gap
        close_x = b.x0 - last.x1 <= gap
        if same_row and close_x:
            merged[-1] = BBox(
                min(last.x0, b.x0),
                min(last.y0, b.y0),
                max(last.x1, b.x1),
                max(last.y1, b.y1),
            )
        else:
            merged.append(b)
    return merged


def _merge_word_regions(regions: list[OcrRegion], *, gap: int = 6) -> list[OcrRegion]:
    """Merge Tesseract word boxes on the same line into phrase regions."""
    if not regions:
        return []
    regions = sorted(regions, key=lambda r: (r.bbox.y0, r.bbox.x0))
    merged: list[OcrRegion] = [regions[0]]
    for r in regions[1:]:
        last = merged[-1]
        same_row = abs(r.bbox.y0 - last.bbox.y0) <= max(r.bbox.height, last.bbox.height) // 2
        close_x = r.bbox.x0 - last.bbox.x1 <= gap
        if same_row and close_x:
            mbox = BBox(
                min(last.bbox.x0, r.bbox.x0),
                min(last.bbox.y0, r.bbox.y0),
                max(last.bbox.x1, r.bbox.x1),
                max(last.bbox.y1, r.bbox.y1),
            )
            text = f"{last.text} {r.text}".strip()
            conf = (last.confidence + r.confidence) / 2
            merged[-1] = OcrRegion(mbox, text, conf, last.frame_index)
        else:
            merged.append(r)
    return merged


def _flatten_ocr_result(result: Any) -> list[tuple[Any, tuple[str, float]]]:
    """Normalise PaddleOCR's nested result shape into a flat list."""
    flat: list[tuple[Any, tuple[str, float]]] = []
    if result is None:
        return flat
    for page in result:
        if page is None:
            continue
        for line in page:
            if line is None:
                continue
            box_pts, rest = line[0], line[1]
            text, conf = rest[0], float(rest[1])
            flat.append((box_pts, (text, conf)))
    return flat
