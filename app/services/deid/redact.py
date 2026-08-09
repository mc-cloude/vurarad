"""Pixel redaction — per-frame box fill, re-encode, fresh SOP Instance UID.

The redactor operates on decoded pixel data: for every PHI region resolved by
the decision layer it fills the bounding box with black (zero) across *all*
frames, re-encodes the dataset to Explicit VR Little Endian (uncompressed, the
universally readable form), and mints a fresh ``SOPInstanceUID`` via the UID
remapper so the redacted object is a distinct, reproducible SOP instance.

Multi-frame and RGB data are handled by normalising the decoded array to an
explicit frame axis before filling.  This module has no ``try/except`` — a
redaction failure propagates to the pipeline, which routes the object to review
(fail-closed: an un-redactable PHI-bearing object is never released).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from pydicom import Dataset
from pydicom.uid import UID, ExplicitVRLittleEndian

from app.services.deid.ocr import BBox
from app.services.deid.tags import UidRemapper

logger = logging.getLogger("vurarad.deid.redact")

# Decoded pixel data may be uint8 or uint16 (mono) or RGB — redaction sets
# regions to zero, which is valid for any integer dtype.
PixelArray = npt.NDArray[Any]


@dataclass(slots=True)
class RedactResult:
    """Outcome of redacting one dataset's pixel regions."""

    new_sop_instance_uid: str
    regions_redacted: int
    frames_redacted: int


class Redactor:
    """Box-fill PHI regions in pixel data and mint a fresh SOP Instance UID."""

    def __init__(self, remapper: UidRemapper) -> None:
        self._remapper = remapper

    def redact(self, ds: Dataset, regions: Sequence[BBox]) -> RedactResult:
        """Fill ``regions`` with black across all frames and re-encode ``ds``.

        Mutates ``ds`` in place: pixel data, transfer syntax, and SOP Instance
        UID.  Returns the count of regions and frames touched plus the new UID.
        """
        if not regions:
            # No PHI regions — still mint a fresh SOP Instance UID for the
            # de-identified object so it is distinct from the source.
            return self._mint_uid_only(ds)

        arr = _decoded_array(ds)
        frames = _normalize_frames(arr, ds)
        redacted = _fill_regions(frames, regions, ds)
        _encode_uncompressed(ds, redacted)
        new_uid = self._mint_sop_uid(ds)
        return RedactResult(
            new_sop_instance_uid=new_uid,
            regions_redacted=len(regions),
            frames_redacted=int(redacted.shape[0]),
        )

    def _mint_uid_only(self, ds: Dataset) -> RedactResult:
        return RedactResult(
            new_sop_instance_uid=self._mint_sop_uid(ds),
            regions_redacted=0,
            frames_redacted=0,
        )

    def _mint_sop_uid(self, ds: Dataset) -> str:
        original = str(ds.SOPInstanceUID) if "SOPInstanceUID" in ds else ""
        new_uid = self._remapper.remap(original) if original else self._remapper.fresh_uid()
        ds.SOPInstanceUID = UID(new_uid)
        return new_uid


# ---------------------------------------------------------------------------
# Pixel-array helpers
# ---------------------------------------------------------------------------
def _decoded_array(ds: Dataset) -> PixelArray:
    # ``pixel_array`` decodes compressed data via the configured backend.
    return np.asarray(ds.pixel_array)


def _normalize_frames(arr: PixelArray, ds: Dataset) -> PixelArray:
    """Return the array with an explicit leading frame axis."""
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if arr.ndim == 2:
        return arr[np.newaxis, :, :]
    if arr.ndim == 3 and samples == 1:
        # (frames, rows, cols)
        return arr
    if arr.ndim == 3 and samples > 1:
        # single-frame RGB: (rows, cols, samples)
        return arr[np.newaxis, :, :, :]
    return arr  # already (frames, rows, cols, samples)


def _fill_regions(
    frames: PixelArray,
    regions: Sequence[BBox],
    ds: Dataset,
) -> PixelArray:
    rows = int(ds.Rows)
    cols = int(ds.Columns)
    out = frames.copy()
    for frame in out:
        for box in regions:
            x0, y0 = max(0, box.x0), max(0, box.y0)
            x1, y1 = min(cols, box.x1), min(rows, box.y1)
            if x1 <= x0 or y1 <= y0:
                continue
            if frame.ndim == 2:
                frame[y0:y1, x0:x1] = 0
            else:
                frame[y0:y1, x0:x1, :] = 0
    return out


def _encode_uncompressed(ds: Dataset, frames: PixelArray) -> None:
    """Write ``frames`` back as Explicit VR Little Endian uncompressed data."""
    contiguous = np.ascontiguousarray(frames)
    ds.PixelData = contiguous.tobytes()
    # Transfer Syntax UID is a group-(0002) file-meta element.  Setting it on
    # the main dataset would place a group-0002 element outside ``file_meta``,
    # which ``dcmwrite`` rejects — so it must go on ``file_meta``.
    _file_meta(ds).TransferSyntaxUID = ExplicitVRLittleEndian
    # Interleaved-by-pixel layout matches the C-contiguous frame array.
    if int(getattr(ds, "SamplesPerPixel", 1) or 1) > 1:
        ds.PlanarConfiguration = 0


def _file_meta(ds: Dataset) -> Any:
    """Return the dataset's file-meta, creating an empty one if absent."""
    meta = getattr(ds, "file_meta", None)
    if meta is None:
        from pydicom.dataset import FileMetaDataset

        meta = FileMetaDataset()
        ds.file_meta = meta
    return meta
