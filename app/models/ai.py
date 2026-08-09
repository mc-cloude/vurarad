"""AI streaming models — prompt input (PHI allow-list) and SSE frame shapes.

This module is deliberately **independent of WP5's report models**.  The AI
service emits ``ReportSections`` over an SSE stream; that wire shape is defined
here so WP6 has no import dependency on the (separately developed) report
package.  The two models may converge later, but for now they are separate.

``PromptInput`` is the PHI allow-list: only the fields the model may see.  It
deliberately carries **no** patient name, MRN, date of birth, accession number,
or DICOM UIDs — those are stripped by :func:`build_prompt_input` before this
model is ever constructed (and re-verified by ``test_prompt_redaction.py``,
which scans the actual bytes sent to the model).
"""

from __future__ import annotations

from app.models.common import CamelModel


# ---------------------------------------------------------------------------:
# Prompt input — the PHI allow-list (only what the model may see)
# ---------------------------------------------------------------------------:
class PromptInput(CamelModel):
    """The de-identified, allow-listed input to a drafting or Q&A prompt.

    Allowed fields (and only these): ``studyDescription``, ``modality``,
    ``bodyPart``, ``patientAgeDays`` (raw age in days; rendered as a bucketed
    descriptor by the prompt builder), ``patientSex``, ``priority``,
    ``confirmedFindings`` (the ``confirmedText`` of CONFIRMED findings — the
    only finding text that may enter a report), ``templateId``, and
    ``priorsSummary``.

    NOT present (and never constructable here): patient name, MRN, date of
    birth, accession number, study/series/SOP UIDs, ``patientRef``,
    ``patientKey``.
    """

    study_description: str
    modality: str
    body_part: str
    patient_age_days: int
    patient_sex: str
    priority: str
    confirmed_findings: list[str]
    template_id: str | None = None
    priors_summary: str | None = None


# ---------------------------------------------------------------------------:
# Report sections — the AI output shape (independent of WP5's report model)
# ---------------------------------------------------------------------------:
class ReportSection(CamelModel):
    """One section of a drafted report — a title and its body text."""

    title: str
    body: str


class ReportSections(CamelModel):
    """The assembled report draft — a list of :class:`ReportSection`.

    Defined here, separately from WP5's report model, so WP6 has no dependency
    on the report package.  The SSE stream emits ``delta`` frames that the
    client concatenates per section to reconstruct this shape.
    """

    sections: list[ReportSection]


# ---------------------------------------------------------------------------:
# SSE stream frames — meta → delta(s) → done, or meta → error
# ---------------------------------------------------------------------------:
class AiStreamMeta(CamelModel):
    """First frame of every stream — identifies the request and model.

    ``modelVersion`` is the version string the SDK returns on the first chunk
    (``None`` when no chunk was produced, e.g. a budget refusal before any SDK
    call).
    """

    request_id: str
    model: str
    model_version: str | None = None


class AiStreamDelta(CamelModel):
    """An incremental text fragment for one report section."""

    section: str
    fragment: str


class AiStreamDone(CamelModel):
    """Terminal success frame — finish reason and token accounting.

    ``totalTokens`` is the SDK's ``total_token_count`` (which includes thoughts
    tokens).  ``cachedInputTokens`` is ``cached_content_token_count``.
    """

    finish_reason: str | None
    total_tokens: int
    cached_input_tokens: int


class AiStreamError(CamelModel):
    """Terminal error frame — carries the canonical ``ErrorCode`` string."""

    code: str
    message: str


# ---------------------------------------------------------------------------:
# Request bodies
# ---------------------------------------------------------------------------:
class AiQaRequest(CamelModel):
    """Request body for ``POST /studies/{studyId}/ai/qa`` — a radiologist's question.

    Carries no PHI; the question is the radiologist's clinical query about the
    de-identified study context.  ``templateId`` and ``priorsSummary`` are
    optional overrides; otherwise priors are derived from the study record.
    """

    question: str
    template_id: str | None = None
    priors_summary: str | None = None


__all__ = [
    "AiQaRequest",
    "AiStreamDelta",
    "AiStreamDone",
    "AiStreamError",
    "AiStreamMeta",
    "PromptInput",
    "ReportSection",
    "ReportSections",
]
