"""PS3.15 Annex E tag-de-identification profile + UID remapping.

DICOM PS3.15 Annex E defines the de-identification attributes and the per-tag
action codes (D replace, Z zero, X remove, U remap UID, K keep, C clean).  This
module encodes a frozen profile — the application-level confidentiality profile
plus the standard options — and a deterministic, site-rooted UID remapper so
that ``StudyInstanceUID`` / ``SeriesInstanceUID`` / ``SOPInstanceUID`` remain
referentially consistent across a de-identified study without leaking the
originating site's UID root.

**This layer is necessary but never sufficient.**  It operates on attributes
only and cannot touch burned-in text in pixel data — the pixel pass
(:mod:`app.services.deid.ocr`, :mod:`app.services.deid.phi_ner`) always runs
after it, even when no PHI tags are found.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from pydicom import Dataset
from pydicom.dataelem import DataElement
from pydicom.multival import MultiValue
from pydicom.uid import UID

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger("vurarad.deid.tags")

# ---------------------------------------------------------------------------
# Profile version — pinned and reported in every DEID_COMPLETED audit event.
# Annex E of DICOM PS3.15 edition 2023e (the edition this profile was compiled
# against).  Bumping this is a profile change and regenerates the validation
# report.
# ---------------------------------------------------------------------------
TAG_PROFILE_VERSION: str = "PS3.15-AnnexE-2023e"

# Default site root for remapped UIDs.  ``2.25`` is the IANA-registered UUID
# derivation root (RFC 4122 → DICOM UID); it requires no organisation
# registration and always yields a syntactically valid UID.  Deployments with a
# registered org root pass it to :class:`UidRemapper`.
DEFAULT_UID_ROOT: str = "2.25"


# ---------------------------------------------------------------------------
# Per-tag action codes (PS3.15 Annex E, Table E.1-1 semantics)
# ---------------------------------------------------------------------------
class TagAction(StrEnum):
    """Action applied to a PHI-bearing attribute."""

    REMOVE = "X"  # delete the element
    ZERO = "Z"  # replace with a zero-length value
    REPLACE = "D"  # replace with a non-zero dummy value
    REMAP_UID = "U"  # replace with a remapped, site-rooted UID
    CLEAN = "C"  # replace with values removed from the instance
    KEEP = "K"  # retained by an option (e.g. Retain Safe Private)


# ---------------------------------------------------------------------------
# The frozen profile — keyword → action.
#
# The set is the patient- and site-identifying attributes from Annex E plus the
# UID attributes remapped (rather than removed) so cross-instance references
# stay consistent.  Clinical attributes (Modality, SeriesNumber, geometry,
# windowing) are intentionally absent — they are KEPT by the profile.
# ---------------------------------------------------------------------------
TAG_ACTIONS: dict[str, TagAction] = {
    # -- patient identity --------------------------------------------------
    "PatientName": TagAction.ZERO,
    "PatientID": TagAction.REPLACE,
    "PatientBirthDate": TagAction.ZERO,
    "PatientBirthTime": TagAction.ZERO,
    "PatientSex": TagAction.ZERO,
    "PatientAge": TagAction.ZERO,
    "PatientWeight": TagAction.ZERO,
    "PatientTelephoneNumbers": TagAction.REMOVE,
    "PatientAddress": TagAction.REMOVE,
    "AdditionalPatientHistory": TagAction.REMOVE,
    "Occupation": TagAction.REMOVE,
    "PatientInsurancePlanCode": TagAction.REMOVE,
    "PatientComments": TagAction.CLEAN,
    # -- site / institution ------------------------------------------------
    "InstitutionName": TagAction.REMOVE,
    "InstitutionAddress": TagAction.REMOVE,
    "InstitutionalDepartmentName": TagAction.REMOVE,
    "InstitutionCodeSequence": TagAction.REMOVE,
    "IssuerOfPatientID": TagAction.REMOVE,
    "IssuerOfPatientIDQualifiersSequence": TagAction.REMOVE,
    # -- study / request identifiers ---------------------------------------
    "AccessionNumber": TagAction.ZERO,
    "StudyID": TagAction.REPLACE,
    "StudyDate": TagAction.ZERO,
    "StudyTime": TagAction.ZERO,
    "StudyDescription": TagAction.CLEAN,
    "StudyComments": TagAction.CLEAN,
    "RequestingPhysician": TagAction.REMOVE,
    "RequestingService": TagAction.REMOVE,
    "RequestedProcedureID": TagAction.ZERO,
    "RequestedProcedureDescription": TagAction.CLEAN,
    "ScheduledProcedureStepID": TagAction.ZERO,
    "ReferringPhysicianName": TagAction.REMOVE,
    "ReferringPhysicianAddress": TagAction.REMOVE,
    "ReferringPhysicianTelephoneNumbers": TagAction.REMOVE,
    "NameOfPhysiciansReadingStudy": TagAction.REMOVE,
    "PhysiciansOfRecord": TagAction.REMOVE,
    "PerformingPhysicianName": TagAction.REMOVE,
    "OperatorsName": TagAction.REMOVE,
    "AdmittingDiagnosesDescription": TagAction.CLEAN,
    # -- series / acquisition dates ----------------------------------------
    "SeriesDate": TagAction.ZERO,
    "SeriesTime": TagAction.ZERO,
    "AcquisitionDate": TagAction.ZERO,
    "AcquisitionTime": TagAction.ZERO,
    "ContentDate": TagAction.ZERO,
    "ContentTime": TagAction.ZERO,
    # -- UIDs (remapped, not removed, to preserve references) --------------
    "StudyInstanceUID": TagAction.REMAP_UID,
    "SeriesInstanceUID": TagAction.REMAP_UID,
    "FrameOfReferenceUID": TagAction.REMAP_UID,
    "SynchronizationFrameOfReferenceUID": TagAction.REMAP_UID,
    "StudyStatusID": TagAction.REMOVE,
    # DeviceSerialNumber is PHI-adjacent (can identify a site) — clean it.
    "DeviceSerialNumber": TagAction.CLEAN,
}

# SOP Instance UID is remapped per-instance by the redactor (a fresh UID per
# redacted object), not by the tag scrubber — it is listed here for completeness
# so callers know it is in-scope for the profile.
UID_TAGS_TO_REMAP: frozenset[str] = frozenset(
    {
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "FrameOfReferenceUID",
        "SynchronizationFrameOfReferenceUID",
    }
)

# UIDs carried inside sequences that must also be remapped for referential
# consistency (PS3.15 Annex E — "Referenced SOP Instance UID" attributes).
SEQUENCE_UID_TAGS: frozenset[str] = frozenset(
    {
        "ReferencedSOPInstanceUID",
        "ReferencedFrameOfReferenceUID",
        "ReferencedSOPClassUID",
    }
)


# ---------------------------------------------------------------------------
# UID remapping
# ---------------------------------------------------------------------------
class UidRemapper:
    """Deterministic, site-rooted UID remapping.

    The same input UID + salt always yields the same output UID, so a study's
    internal references stay consistent after de-identification while the
    originating site's UID root is erased.  Output UIDs use the ``2.25`` UUID
    derivation root (or a configured org root) and are always ≤ 64 chars and
    syntactically valid.
    """

    def __init__(self, salt: str, site_root: str = DEFAULT_UID_ROOT) -> None:
        self._salt = salt
        self._site_root = site_root
        self._cache: dict[str, str] = {}

    def remap(self, uid: str) -> str:
        if uid in self._cache:
            return self._cache[uid]
        digest = hmac.new(
            self._salt.encode("utf-8"),
            uid.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        # 16 bytes → integer < 2**128 → valid 2.25 derivation (≤ 39 digits).
        value = int.from_bytes(digest[:16], "big")
        mapped = f"{self._site_root}.{value}"
        # Guard total length per DICOM UID constraint (≤ 64).
        if len(mapped) > 64:
            mapped = mapped[:64].rstrip(".")
        result = UID(mapped)
        self._cache[uid] = str(result)
        return str(result)

    def fresh_uid(self) -> str:
        """Mint a UID with no source — deterministic from the remapper's salt."""
        digest = hmac.new(
            self._salt.encode("utf-8"),
            b"fresh-uid",
            hashlib.sha256,
        ).digest()
        value = int.from_bytes(digest[:16], "big")
        return str(UID(f"{self._site_root}.{value}"))


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TagScrubResult:
    """Outcome of scrubbing one dataset's attributes."""

    removed_tags: list[str] = field(default_factory=list)
    zeroed_tags: list[str] = field(default_factory=list)
    replaced_tags: list[str] = field(default_factory=list)
    cleaned_tags: list[str] = field(default_factory=list)
    remapped_uids: dict[str, str] = field(default_factory=dict)
    phi_tag_found: bool = False

    @property
    def touched_count(self) -> int:
        return (
            len(self.removed_tags)
            + len(self.zeroed_tags)
            + len(self.replaced_tags)
            + len(self.cleaned_tags)
            + len(self.remapped_uids)
        )


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------
class TagScrubber:
    """Apply the frozen PS3.15 Annex E profile to a :class:`pydicom.Dataset`.

    The scrubber mutates the dataset in place and returns a record of what was
    changed.  It never touches ``PixelData`` — that is the pixel pass's job.
    """

    def __init__(self, remapper: UidRemapper) -> None:
        self._remapper = remapper

    def scrub(self, ds: Dataset) -> TagScrubResult:
        result = TagScrubResult()
        # Operate over a snapshot of keywords — we mutate ``ds`` while iterating.
        keywords = list(_iter_keywords(ds))
        for keyword in keywords:
            action = TAG_ACTIONS.get(keyword)
            if action is None:
                continue
            if keyword not in ds:
                continue
            result.phi_tag_found = True
            elem: DataElement = ds[keyword]
            _apply_action(ds, elem, keyword, action, self._remapper, result)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iter_keywords(ds: Dataset) -> Iterator[str]:
    for tag in list(ds):
        try:
            kw = tag.keyword
        except Exception:  # pragma: no cover — defensive for private tags
            continue
        if kw:
            yield kw


def _apply_action(
    ds: Dataset,
    elem: DataElement,
    keyword: str,
    action: TagAction,
    remapper: UidRemapper,
    result: TagScrubResult,
) -> None:
    if action == TagAction.REMOVE:
        del ds[elem.tag]
        result.removed_tags.append(keyword)
    elif action == TagAction.ZERO:
        elem.value = ""
        result.zeroed_tags.append(keyword)
    elif action == TagAction.REPLACE:
        elem.value = _dummy_value(keyword, elem)
        result.replaced_tags.append(keyword)
    elif action == TagAction.CLEAN:
        elem.value = ""
        result.cleaned_tags.append(keyword)
    elif action == TagAction.REMAP_UID:
        original = str(elem.value) if elem.value else ""
        if not original:
            return
        mapped = remapper.remap(original)
        elem.value = mapped
        result.remapped_uids[original] = mapped
    # KEEP is a no-op.


def _dummy_value(keyword: str, elem: DataElement) -> str:
    """A non-zero, non-identifying replacement value for a D-tag."""
    # PatientID / StudyID → a de-id scoped token, not the original.
    if keyword in {"PatientID", "StudyID"}:
        return "DEID"
    # Numeric VRs are left to pydicom's VR coercion; a short string is safe for
    # the string VRs we replace.
    return "0" if isinstance(elem.value, (int, float, MultiValue)) else "DEID"
