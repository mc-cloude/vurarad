"""Prompt construction — PHI allow-list, age bucketing, prompt rendering.

:func:`build_prompt_input` is the single funnel through which study context
reaches the model.  It copies **only** the allow-listed fields onto
:class:`PromptInput` — by construction it never reads ``patient_name``,
``mrn``, ``patient_birth_date``, ``accession``, ``patient_ref``,
``patient_key``, or any DICOM UID from the study record.  The redaction is
verified end-to-end by ``test_prompt_redaction.py``, which scans the actual
bytes sent to a (fake) client for every PHI pattern.

Age is bucketed before rendering (§PHI): ``< 1 year`` → ``ageInDays``,
``1–18 years`` → ``ageInYears``, ``> 18 years`` → ``"adult"``.  A bucketed age
is not a date of birth and is the only age information the model ever sees.
"""

from __future__ import annotations

from app.models.ai import PromptInput
from app.models.study import StudyRecord

_DAYS_PER_YEAR = 365
_ADULT_AGE_DAYS = _DAYS_PER_YEAR * 18
# Default age used when the formatted ``patientAgeSex`` string cannot be parsed
# — a large adult value so the bucket is ``"adult"`` (no age granularity leaked).
_UNPARSEABLE_AGE_DAYS = _DAYS_PER_YEAR * 19

# PHI fields that must NEVER be copied onto a PromptInput.  This set is the
# negative of the allow-list; it is asserted by ``test_prompt_redaction.py``.
_STUDY_PHI_FIELDS: frozenset[str] = frozenset(
    {
        "patient_name",
        "mrn",
        "patient_birth_date",
        "accession",
        "patient_ref",
        "patient_key",
        "patient_age_sex",  # formatted string may leak age+sex granularity
    }
)


def bucket_age(age_days: int) -> dict[str, int | str]:
    """Bucket a raw age-in-days into a non-PHI age descriptor.

    - ``< 1 year``  → ``{"ageInDays": n}``
    - ``1–18 years`` → ``{"ageInYears": n}``
    - ``> 18 years`` → ``{"age": "adult"}``

    A bucketed age carries no date of birth and is the only age signal the
    model receives.
    """
    if age_days < _DAYS_PER_YEAR:
        return {"ageInDays": age_days}
    if age_days < _ADULT_AGE_DAYS:
        return {"ageInYears": age_days // _DAYS_PER_YEAR}
    return {"age": "adult"}


def _age_descriptor(age_days: int) -> str:
    bucket = bucket_age(age_days)
    key, value = next(iter(bucket.items()))
    return f"{key}: {value}"


def age_days_from_age_sex(age_sex: str) -> int:
    """Best-effort parse of a formatted ``patientAgeSex`` string into age in days.

    The study record carries only the formatted ``patientAgeSex`` string (e.g.
    ``"41 F"``, ``"041Y"``, ``"6M"``); the prompt needs a raw age-in-days so it
    can bucket it.  DICOM age units (``Y``/``M``/``W``/``D``) and a bare integer
    (interpreted as years) are handled.  An unparseable string yields an adult
    value so no age granularity is leaked.
    """
    token = (age_sex or "").strip().split()[0] if age_sex else ""
    if not token:
        return _UNPARSEABLE_AGE_DAYS
    suffix = token[-1].upper()
    digits = token[:-1] if not token.isdigit() else token
    if not digits.isdigit():
        return _UNPARSEABLE_AGE_DAYS
    n = int(digits)
    if suffix == "Y" or token.isdigit():
        return n * _DAYS_PER_YEAR
    if suffix == "M":
        return n * 30
    if suffix == "W":
        return n * 7
    if suffix == "D":
        return n
    return _UNPARSEABLE_AGE_DAYS


def build_prompt_input(
    study: StudyRecord,
    *,
    confirmed_findings: list[str],
    patient_age_days: int,
    template_id: str | None = None,
    priors_summary: str | None = None,
) -> PromptInput:
    """Build a :class:`PromptInput` from study context — allow-listed fields only.

    Only ``description``, ``modality``, ``bodyPart``, ``patientSex``, and
    ``priority`` are read from ``study``.  Every PHI field
    (``patient_name``, ``mrn``, ``patient_birth_date``, ``accession``,
    ``patient_ref``, ``patient_key``) is deliberately ignored — it never
    reaches the returned model and therefore never reaches the LLM.
    """
    return PromptInput(
        study_description=study.description,
        modality=study.modality,
        body_part=study.body_part,
        patient_age_days=patient_age_days,
        patient_sex=study.patient_sex,
        priority=study.priority.value,
        confirmed_findings=list(confirmed_findings),
        template_id=template_id,
        priors_summary=priors_summary,
    )


def render_prompt_contents(
    prompt_input: PromptInput,
    dictation_text: str,
) -> str:
    """Render the user-facing prompt contents from a :class:`PromptInput`.

    The output contains **only** allow-listed fields plus the radiologist's
    dictation (clinical narrative, not identifiers).  No patient name, MRN,
    DOB, accession, UIDs, ``patientRef``, or ``patientKey`` can appear here —
    they are absent from :class:`PromptInput` by construction.
    """
    findings_block = (
        "\n".join(f"- {text}" for text in prompt_input.confirmed_findings)
        if prompt_input.confirmed_findings
        else "- (none)"
    )
    template_block = prompt_input.template_id or "none"
    priors_block = prompt_input.priors_summary or "none"
    dictation_block = dictation_text.strip() if dictation_text.strip() else "(none)"

    return (
        "[STUDY]\n"
        f"Description: {prompt_input.study_description}\n"
        f"Modality: {prompt_input.modality}\n"
        f"Body part: {prompt_input.body_part}\n"
        f"Age: {_age_descriptor(prompt_input.patient_age_days)}\n"
        f"Sex: {prompt_input.patient_sex}\n"
        f"Priority: {prompt_input.priority}\n"
        "\n[CONFIRMED FINDINGS]\n"
        f"{findings_block}\n"
        "\n[DICTATION]\n"
        f"{dictation_block}\n"
        "\n[REPORT TEMPLATE]\n"
        f"{template_block}\n"
        "\n[PRIOR STUDIES SUMMARY]\n"
        f"{priors_block}\n"
    )


__all__ = [
    "age_days_from_age_sex",
    "build_prompt_input",
    "bucket_age",
    "render_prompt_contents",
]
