# ruff: noqa: B008
"""Report-template routes — static, versioned, PHI-free catalogue (WP9).

- ``GET /report-templates`` — list the catalogue (optional modality/bodyPart
  filters).
- ``GET /report-templates/{templateId}`` — retrieve a single template.

Templates are non-PHI static content, gated by the non-PHI ``TEMPLATE_READ``
capability (admin gets ``PERMISSION_DENIED``, not ``PHI_ACCESS_FORBIDDEN``).
Every permitted role receives a byte-identical response — there is no
per-role data variation (criterion 3).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import get_current_user, require_capability, require_mfa
from app.core.capabilities import Capability
from app.services.template_service import (
    ReportTemplate,
    TemplateCatalogue,
    TemplateService,
)

router = APIRouter(
    prefix="/report-templates",
    tags=["report-templates"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


# ---------------------------------------------------------------------------
# Dependency — overridable via app.state, defaults to the static catalogue
# ---------------------------------------------------------------------------
async def get_template_service(request: Request) -> TemplateService:
    svc = getattr(request.app.state, "template_service", None)
    if svc is None:
        svc = TemplateService()
        request.app.state.template_service = svc
    return svc


TemplateServiceDep = Annotated[TemplateService, Depends(get_template_service)]


# ---------------------------------------------------------------------------
# GET /report-templates — catalogue (optional modality/bodyPart filters)
# ---------------------------------------------------------------------------
@router.get(
    "",
    dependencies=[Depends(require_capability(Capability.TEMPLATE_READ))],
    response_model=TemplateCatalogue,
)
async def list_templates(
    template_service: TemplateServiceDep,
    modality: str | None = Query(default=None),
    body_part: str | None = Query(default=None, alias="bodyPart"),
) -> TemplateCatalogue:
    """List report templates, optionally filtered by modality and body part."""
    return await template_service.list_templates(modality=modality, body_part=body_part)


# ---------------------------------------------------------------------------
# GET /report-templates/{templateId} — single template
# ---------------------------------------------------------------------------
@router.get(
    "/{template_id}",
    dependencies=[Depends(require_capability(Capability.TEMPLATE_READ))],
    response_model=ReportTemplate,
)
async def get_template(
    template_id: str,
    template_service: TemplateServiceDep,
) -> ReportTemplate:
    """Retrieve a single report template by id (404 TEMPLATE_NOT_FOUND)."""
    return await template_service.get_template(template_id)
