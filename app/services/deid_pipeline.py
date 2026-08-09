"""De-identification pipeline — the ONLY path that mints a cohort pseudonym.

A cohort subject is added **only** through :class:`DeidPipeline`.  The pipeline
runs the pixel pass + metadata de-identification, mints an opaque pseudonym
(``cs_…``), writes the de-identified pixels to the de-ID bucket, and records the
write-restricted ``deid_links`` mapping (pseudonym → ``patientKey``).  It returns
a :class:`DeidResult`; the cohort subject service builds the
:class:`CohortSubject` from that result — there is no code path that copies
pixels or mints a pseudonym without one (acceptance criterion 2).

The ``DeidResult`` carries any open :class:`DeidReviewItem`\\ s; a subject with
open items cannot become ``ACTIVE`` and feature extraction against it returns
``409 DEID_REVIEW_PENDING`` (criterion 3).

This module is the dependency seam for WP11's real de-identification engine; the
:class:`StubDeidPipeline` is the deterministic dev/CI implementation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from ulid import ULID

from app.models.cohort import DeidReviewItem
from app.models.common import CamelModel
from app.repositories.deid_link_repo import DeidLinkRepository


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class DeidSourceKind(StrEnum):
    """Where the pixels being de-identified came from."""

    WORKLIST = "WORKLIST"
    UPLOAD = "UPLOAD"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class DeidSource(CamelModel):
    """The input to de-identification — a clinical study or a research upload.

    Carries ``studyId`` only because it is the *input* to de-identification; no
    cohort response model ever holds it.
    """

    kind: DeidSourceKind
    study_id: str = ""
    upload_ref: str = ""
    modality: str = ""
    body_part: str = ""


class DeidResult(CamelModel):
    """The output of de-identification — the cohort subject's provenance.

    Holds the minted pseudonym, the de-identified object path, the pixel-pass
    verdict, and any open review items.  Carries **no** ``studyId`` or
    ``patientKey`` — the pseudonym→patient mapping lives only in the
    write-restricted ``deid_links`` collection.
    """

    pseudonym: str
    deid_object_path: str
    pixel_pass_passed: bool
    confidence: float
    review_items: list[DeidReviewItem] = []
    source_kind: DeidSourceKind = DeidSourceKind.WORKLIST
    deid_metadata: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class DeidPipeline(Protocol):
    """The de-identification interface — produce a :class:`DeidResult`."""

    async def deidentify(
        self,
        source: DeidSource,
        *,
        patient_key: str,
    ) -> DeidResult:
        """De-identify ``source``, mint a pseudonym, write the deid_link.

        Returns a :class:`DeidResult` describing the de-identified subject.
        """
        ...


# ---------------------------------------------------------------------------
# Stub implementation — dev / CI
# ---------------------------------------------------------------------------
class StubDeidPipeline:
    """Deterministic, dependency-free de-ID pipeline for dev/CI.

    Mints ``cs_<ULID>``, writes the ``deid_links`` mapping, and returns a
    :class:`DeidResult`.  ``review_items`` and ``pixel_pass_passed`` are
    constructor-configurable so tests can exercise the review-pending path
    (criterion 3) without a real pixel pass.
    """

    def __init__(
        self,
        deid_link_repo: DeidLinkRepository,
        *,
        pixel_pass_passed: bool = True,
        confidence: float = 1.0,
        review_items: list[DeidReviewItem] | None = None,
        deid_bucket: str = "deid",
    ) -> None:
        self._repo = deid_link_repo
        self._pixel_pass_passed = pixel_pass_passed
        self._confidence = confidence
        self._review_items = list(review_items) if review_items else []
        self._deid_bucket = deid_bucket
        self.calls: list[tuple[DeidSource, str]] = []

    async def deidentify(
        self,
        source: DeidSource,
        *,
        patient_key: str,
    ) -> DeidResult:
        self.calls.append((source, patient_key))
        pseudonym = f"cs_{ULID()}"
        deid_object_path = f"{self._deid_bucket}/{pseudonym}/volume.nii"
        # The pipeline is the ONLY writer of the write-restricted deid_links
        # collection — the pseudonym->patientKey mapping lives here, never on
        # the cohort subject.
        await self._repo.write_link(pseudonym, patient_key)
        return DeidResult(
            pseudonym=pseudonym,
            deid_object_path=deid_object_path,
            pixel_pass_passed=self._pixel_pass_passed,
            confidence=self._confidence,
            review_items=[item.model_copy() for item in self._review_items],
            source_kind=source.kind,
            deid_metadata={
                "modality": source.modality,
                "bodyPart": source.body_part,
            },
        )


__all__ = [
    "DeidPipeline",
    "DeidResult",
    "DeidSource",
    "DeidSourceKind",
    "StubDeidPipeline",
]
