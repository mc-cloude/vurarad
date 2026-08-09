"""DICOM JSON model builders and tag constants for QIDO/WADO/STOW.

The DICOMweb JSON model represents each attribute as ``{"vr": "XX", "Value": [...]}``.
These helpers produce correctly VR-tagged attribute objects so QIDO-RS
responses conform to PS3.18 §Q.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# DICOM tag constants (group, element as hex string without parens)
# ---------------------------------------------------------------------------
STUDY_INSTANCE_UID = "0020000D"
SERIES_INSTANCE_UID = "0020000E"
SOP_INSTANCE_UID = "00080018"
SOP_CLASS_UID = "00080016"
PATIENT_ID = "00100020"
PATIENT_NAME = "00100010"
STUDY_DATE = "00080020"
STUDY_TIME = "00080030"
ACCESSION_NUMBER = "00080050"
MODALITIES_IN_STUDY = "00080061"
MODALITY = "00080060"
STUDY_DESCRIPTION = "00081030"
SERIES_DESCRIPTION = "0008103E"
SERIES_NUMBER = "00200011"
INSTANCE_NUMBER = "00200013"
NUM_STUDY_RELATED_SERIES = "00201206"
NUM_STUDY_RELATED_INSTANCES = "00201208"
NUM_SERIES_RELATED_INSTANCES = "00201209"
ROWS = "00280010"
COLUMNS = "00280011"
NUM_FRAMES = "00280008"

# STOW-RS response tags
REFERENCED_SOP_SEQUENCE = "00081199"
REFERENCED_SOP_CLASS_UID = "00081150"
REFERENCED_SOP_INSTANCE_UID = "00081155"
FAILED_SOP_SEQUENCE = "00081198"
FAILURE_REASON = "00081197"

# STOW-RS failure codes
FAILURE_CANNOT_UNDERSTAND = 49152
FAILURE_SOP_CLASS_NOT_RECOGNIZED = 272


# ---------------------------------------------------------------------------
# VR tagging helpers — each returns a single-attribute DICOM JSON object
# ---------------------------------------------------------------------------
def _pn(value: str) -> dict[str, Any]:
    return {"vr": "PN", "Value": [{"Alphabetic": value}]}


def _ui(value: str) -> dict[str, Any]:
    return {"vr": "UI", "Value": [value]}


def _lo(value: str) -> dict[str, Any]:
    return {"vr": "LO", "Value": [value]}


def _sh(value: str) -> dict[str, Any]:
    return {"vr": "SH", "Value": [value]}


def _cs(value: str) -> dict[str, Any]:
    return {"vr": "CS", "Value": [value]}


def _is(value: int) -> dict[str, Any]:
    return {"vr": "IS", "Value": [str(value)]}


def _us(value: int) -> dict[str, Any]:
    return {"vr": "US", "Value": [value]}


def _da(value: str) -> dict[str, Any]:
    return {"vr": "DA", "Value": [value]}


def _tm(value: str) -> dict[str, Any]:
    return {"vr": "TM", "Value": [value]}


# ---------------------------------------------------------------------------
# Metadata records
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class StudyRecord:
    study_uid: str
    patient_id: str
    patient_name: str
    study_date: str
    study_time: str
    accession_number: str
    modalities_in_study: list[str]
    study_description: str
    tenant_id: str
    num_series: int = 0
    num_instances: int = 0


@dataclass(slots=True)
class SeriesRecord:
    study_uid: str
    series_uid: str
    modality: str
    series_number: int
    series_description: str
    tenant_id: str
    num_instances: int = 0


@dataclass(slots=True)
class InstanceRecord:
    study_uid: str
    series_uid: str
    sop_uid: str
    sop_class_uid: str
    instance_number: int
    rows: int
    columns: int
    num_frames: int
    tenant_id: str
    object_ref: str = ""
    pixel_data_offset: int = 0
    frame_offsets: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON model builders
# ---------------------------------------------------------------------------
def study_to_dicom_json(study: StudyRecord) -> dict[str, Any]:
    """Build a QIDO-RS study-level attribute map."""
    result: dict[str, Any] = {
        STUDY_INSTANCE_UID: _ui(study.study_uid),
        PATIENT_ID: _lo(study.patient_id),
        PATIENT_NAME: _pn(study.patient_name),
        STUDY_DATE: _da(study.study_date),
        STUDY_TIME: _tm(study.study_time),
        ACCESSION_NUMBER: _sh(study.accession_number),
        STUDY_DESCRIPTION: _lo(study.study_description),
        NUM_STUDY_RELATED_SERIES: _is(study.num_series),
        NUM_STUDY_RELATED_INSTANCES: _is(study.num_instances),
    }
    if study.modalities_in_study:
        result[MODALITIES_IN_STUDY] = {"vr": "CS", "Value": study.modalities_in_study}
    return result


def series_to_dicom_json(series: SeriesRecord) -> dict[str, Any]:
    """Build a QIDO-RS series-level attribute map."""
    return {
        STUDY_INSTANCE_UID: _ui(series.study_uid),
        SERIES_INSTANCE_UID: _ui(series.series_uid),
        MODALITY: _cs(series.modality),
        SERIES_NUMBER: _is(series.series_number),
        SERIES_DESCRIPTION: _lo(series.series_description),
        NUM_SERIES_RELATED_INSTANCES: _is(series.num_instances),
    }


def instance_to_dicom_json(instance: InstanceRecord) -> dict[str, Any]:
    """Build a QIDO-RS instance-level attribute map."""
    return {
        STUDY_INSTANCE_UID: _ui(instance.study_uid),
        SERIES_INSTANCE_UID: _ui(instance.series_uid),
        SOP_INSTANCE_UID: _ui(instance.sop_uid),
        SOP_CLASS_UID: _ui(instance.sop_class_uid),
        INSTANCE_NUMBER: _is(instance.instance_number),
        ROWS: _us(instance.rows),
        COLUMNS: _us(instance.columns),
        NUM_FRAMES: _us(instance.num_frames),
    }
