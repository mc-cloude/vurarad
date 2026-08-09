# ruff: noqa: E402
"""Prompt redaction — zero PHI in the bytes sent to the model (criterion 13).

Builds a :class:`PromptInput` from a study record that carries PHI (patient
name, MRN, DOB, accession, ``patientRef``, ``patientKey``, a raw UID), renders
the prompt, runs it through :class:`GeminiService` with a **recording fake
client**, and greps the actual bytes sent upstream for every PHI pattern.

The allow-list (:func:`build_prompt_input`) copies only ``description``,
``modality``, ``bodyPart``, ``patientSex``, ``priority`` and the caller-supplied
confirmed findings / dictation — so the study's PHI can never reach the model.
This test verifies that end-to-end against the real rendered bytes.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from google.genai import types

from app.models.study import StudyPriority, StudyRecord, StudyStatus
from app.services.gemini_service import GeminiService
from app.services.prompt_builder import (
    age_days_from_age_sex,
    build_prompt_input,
    render_prompt_contents,
)
from tests.conftest import REPO_ROOT


# ---------------------------------------------------------------------------
# A recording fake client — captures the exact bytes sent upstream
# ---------------------------------------------------------------------------
class _RecordingModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_content_stream(
        self, *, model: str, contents: str, config: Any = None
    ) -> AsyncIterator[Any]:
        # Mirror the real SDK: an `async def` that RETURNS an async iterator.
        self.calls.append({"model": model, "contents": contents, "config": config})

        async def _iterator() -> AsyncIterator[Any]:
            # Yield a single terminating chunk so the stream completes.
            yield types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            parts=[types.Part(text="<<SECTION:Findings>>ok")]
                        ),
                        finish_reason=types.FinishReason.STOP,
                    )
                ],
            )

        return _iterator()


class _RecordingAio:
    def __init__(self) -> None:
        self.models = _RecordingModels()


class _RecordingClient:
    def __init__(self) -> None:
        self.aio = _RecordingAio()


class _AllowingBudget:
    async def check_budget(self, tenant_id: str) -> bool:
        return True

    async def record_usage(self, tenant_id: str, usage: dict[str, int]) -> None:
        return None


# ---------------------------------------------------------------------------
# A study record carrying every PHI field the allow-list must NOT copy
# ---------------------------------------------------------------------------
def _phi_study() -> StudyRecord:
    return StudyRecord(
        study_id="st_test",
        patient_key="pk-secret-key-999",
        patient_ref="PT-SECRET-REF",
        patient_age_sex="41 F",
        patient_sex="F",
        patient_name="Doe, John Q.",
        patient_birth_date="1985-03-02",
        mrn="MRN-4471",
        accession="ACC-9001-SECRET",
        modality="CT",
        body_part="CHEST",
        description="CT Chest with contrast",
        study_date="2026-08-01",
        referring_physician="1.2.840.10008.5.1.4.1.1.2",
        status=StudyStatus.UNREAD,
        priority=StudyPriority.ROUTINE,
        tenant_id="default",
    )


_PHI_PATTERNS = [
    "Doe",
    "John",
    "MRN-4471",
    "1985-03-02",
    "ACC-9001",
    "PT-SECRET-REF",
    "pk-secret-key-999",
    "1.2.840.10008.5.1.4.1.1.2",
]

# A DICOM-style UID pattern: dotted numeric components.  None should appear.
_UID_RE = re.compile(r"\b\d{1,3}\.\d+\.\d+\.\d+(?:\.\d+)*\b")


async def _consume(stream: Any) -> None:
    async for _frame in stream:
        pass


async def test_no_phi_in_bytes_sent_to_client() -> None:
    """The rendered prompt + system instruction sent upstream contain zero PHI."""
    study = _phi_study()
    prompt_input = build_prompt_input(
        study,
        confirmed_findings=["Liver volume 1450 mL, within normal limits."],
        patient_age_days=age_days_from_age_sex(study.patient_age_sex),
        template_id=None,
        priors_summary=None,
    )
    dictation_text = "No acute findings. The patient is neurologically intact."
    contents = render_prompt_contents(prompt_input, dictation_text)

    system_instruction = (REPO_ROOT / "app" / "prompts" / "report_draft.md").read_text()

    recording = _RecordingClient()
    service = GeminiService(
        project="vurarad-test",
        location="us-central1",
        client=recording,
        budget_repo=_AllowingBudget(),
    )

    await _consume(
        service.generate_stream(
            request_id="r",
            contents=contents,
            system_instruction=system_instruction,
            tenant_id="default",
        )
    )

    assert len(recording.aio.models.calls) == 1
    call = recording.aio.models.calls[0]
    sent_contents: str = call["contents"]
    sent_system: str = call["config"].system_instruction

    # The combined bytes the model actually receives.
    sent_bytes = sent_contents + "\n" + sent_system

    for pattern in _PHI_PATTERNS:
        assert pattern not in sent_bytes, f"PHI pattern {pattern!r} leaked into prompt bytes"

    # No DICOM-style UID pattern anywhere in the sent bytes.
    assert not _UID_RE.search(sent_bytes), "A raw UID pattern leaked into prompt bytes"

    # No date-of-birth-shaped string (YYYY-MM-DD) in the sent bytes.
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b", sent_bytes), "A DOB-shaped date leaked"


def test_prompt_input_does_not_carry_phi_fields() -> None:
    """The PromptInput model itself has no PHI field names."""
    phi_field_names = {
        "patient_name",
        "mrn",
        "patient_birth_date",
        "accession",
        "patient_ref",
        "patient_key",
    }
    assert phi_field_names.isdisjoint(set(prompt_input_fields()))


def prompt_input_fields() -> set[str]:
    from app.models.ai import PromptInput

    return set(PromptInput.model_fields.keys())


def test_allow_listed_fields_are_present() -> None:
    """Redaction is selective — the allowed clinical fields DO reach the prompt."""
    study = _phi_study()
    prompt_input = build_prompt_input(
        study,
        confirmed_findings=["Liver volume 1450 mL"],
        patient_age_days=age_days_from_age_sex(study.patient_age_sex),
    )
    contents = render_prompt_contents(prompt_input, "Dictated narrative here.")

    assert "CT" in contents  # modality
    assert "CHEST" in contents  # body part
    assert "CT Chest with contrast" in contents  # study description (allowed)
    assert "Liver volume 1450 mL" in contents  # confirmed finding (allowed)
    assert "Dictated narrative here." in contents  # dictation (allowed)
    # Bucketed age — "adult" for a 41-year-old, never the DOB.
    assert "adult" in contents
    assert "1985" not in contents


def test_age_bucketing_not_phi() -> None:
    """Bucketed ages are descriptors, not dates of birth."""
    from app.services.prompt_builder import bucket_age

    assert bucket_age(30) == {"ageInDays": 30}
    assert bucket_age(365 * 5) == {"ageInYears": 5}
    assert bucket_age(365 * 41) == {"age": "adult"}
