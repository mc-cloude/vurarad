"""Shared fixtures for the WP11 de-identification unit tests.

A minimal DICOM dataset factory plus lightweight test doubles (a canned OCR
engine and an in-memory object store) so the pipeline / redactor / repo tests
can run without paddleocr, transformers, GCS, or Firestore.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
from pydicom import Dataset
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

from app.services.deid.ocr import BBox, OcrRegion


# ---------------------------------------------------------------------------
# DICOM dataset factory
# ---------------------------------------------------------------------------
def make_dataset(
    *,
    rows: int = 16,
    cols: int = 32,
    patient_name: str = "DOE^JOHN^M",
    patient_id: str = "MRN-12345",
    study_uid: str = "1.2.3.4.5",
    series_uid: str = "1.2.3.4.5.1",
    sop_uid: str = "1.2.3.4.5.1.1",
    modality: str = "SC",
    manufacturer: str = "TestImaging",
    ink: tuple[int, int, int, int] | None = (0, 0, 10, 4),
) -> Dataset:
    """Build a minimal, pixel-decodable DICOM dataset with PHI attributes.

    ``ink`` draws a dark-on-light rectangle of ink pixels so the threshold OCR
    engine has something to localise; ``None`` yields a blank image.
    """
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.AccessionNumber = "ACC123"
    ds.InstitutionName = "General Hospital"
    ds.Modality = modality
    ds.Manufacturer = manufacturer
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    arr = np.full((rows, cols), 220, dtype=np.uint8)
    if ink is not None:
        x0, y0, x1, y1 = ink
        arr[y0:y1, x0:x1] = 30  # dark ink on light gray
    ds.PixelData = arr.tobytes()
    return ds


# ---------------------------------------------------------------------------
# Canned OCR engine — returns fixed regions, no real recognition
# ---------------------------------------------------------------------------
@dataclass
class FakeOcrEngine:
    """An :class:`OcrEngine` that returns caller-configured regions per frame.

    ``regions`` maps ``frame_index -> list[(text, bbox)]``.  Frames with no
    entry produce no regions.  The confidence is fixed at 0.95 so the
    classifier — not the OCR confidence — drives the decision.  A monotonic
    call counter keys the frame index because the pipeline calls ``detect``
    once per frame in order.
    """

    regions: dict[int, list[tuple[str, BBox]]] = field(default_factory=dict)
    _version: str = "fake-ocr-v1"
    _call_count: int = field(default=0, repr=False)

    @property
    def version(self) -> str:
        return self._version

    def detect(self, image: np.ndarray) -> list[OcrRegion]:  # noqa: ARG002
        idx = self._call_count
        self._call_count += 1
        out: list[OcrRegion] = []
        for text, bbox in self.regions.get(idx, []):
            out.append(OcrRegion(bbox, text, 0.95, frame_index=idx))
        return out


# ---------------------------------------------------------------------------
# In-memory object store — for the pipeline persistence path
# ---------------------------------------------------------------------------
@dataclass
class InMemoryObjectStore:
    """A minimal :class:`ObjectStore` double recording writes."""

    blobs: dict[str, bytes] = field(default_factory=dict)

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> object:
        self.blobs[key] = data
        return key

    async def get_blob(self, key: str) -> bytes:
        return self.blobs[key]

    async def get_range(self, key: str, start: int, end: int) -> bytes:  # noqa: ARG002
        return self.blobs.get(key, b"")[start:end]

    async def delete(self, key: str) -> None:
        self.blobs.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self.blobs


def utcnow() -> datetime:
    return datetime.now(tz=UTC)
