# ruff: noqa: B008
"""Evidence router — routes for evidence lookup and accept (WP13 §3.16).

- ``POST /evidence/lookup`` (evidence:read, MFA) — match a rule set against a
  confirmed finding's attributes and return the matching rules.
- ``POST /evidence/accept`` (evidence:accept, MFA, Idempotency-Key) — pin an
  accepted evidence reference to a finding after re-verifying the match.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.api.v1.routers.studies_deps import (
    AuditServiceDep,
    DocumentStoreDep,
    require_mfa,
    require_phi_capability,
)
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.capabilities import Capability
from app.evidence.loader import RuleSetRegistry, default_registry
from app.models.common import CamelModel
from app.models.finding import Finding
from app.repositories.study_repo import StudyRepository
from app.services.evidence_service import EvidenceLookupResult, EvidenceService

router = APIRouter(
    prefix="/evidence",
    tags=["evidence"],
    dependencies=[Depends(get_current_user), Depends(require_mfa)],
)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------
class EvidenceLookupRequest(CamelModel):
    """Request body for ``POST /evidence/lookup``."""

    finding_id: str
    rule_set_id: str
    attributes: dict[str, Any]


class EvidenceAcceptRequest(CamelModel):
    """Request body for ``POST /evidence/accept``."""

    finding_id: str
    rule_set_id: str
    rule_id: str
    attributes: dict[str, Any]


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------
async def get_evidence_registry(request: Request) -> RuleSetRegistry:
    """Return the rule-set registry from ``app.state`` or the bundled default."""
    registry = getattr(request.app.state, "evidence_registry", None)
    if registry is None:
        registry = default_registry()
        request.app.state.evidence_registry = registry
    return registry


EvidenceRegistryDep = Annotated[RuleSetRegistry, Depends(get_evidence_registry)]


async def get_study_repo_for_evidence(doc_store: DocumentStoreDep) -> StudyRepository:
    return StudyRepository(doc_store)


StudyRepoForEvidenceDep = Annotated[StudyRepository, Depends(get_study_repo_for_evidence)]


async def get_evidence_service(
    doc_store: DocumentStoreDep,
    study_repo: StudyRepoForEvidenceDep,
    audit: AuditServiceDep,
    registry: EvidenceRegistryDep,
) -> EvidenceService:
    return EvidenceService(doc_store, study_repo, audit, registry)


EvidenceServiceDep = Annotated[EvidenceService, Depends(get_evidence_service)]


def _require_idempotency_key(key: str | None) -> str:
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Idempotency-Key header is required",
                }
            },
        )
    return key


# ---------------------------------------------------------------------------
# POST /evidence/lookup
# ---------------------------------------------------------------------------
@router.post(
    "/lookup",
    dependencies=[Depends(require_phi_capability(Capability.EVIDENCE_READ))],
    response_model=EvidenceLookupResult,
)
async def evidence_lookup(
    body: EvidenceLookupRequest,
    evidence_service: EvidenceServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
) -> EvidenceLookupResult:
    """Match a rule set against a confirmed finding's attributes.

    Returns ``409 FINDING_NOT_CONFIRMED`` for a ``PENDING`` / ``REJECTED``
    finding.  Writes an ``EVIDENCE_LOOKUP`` audit event with the rule-set
    version.
    """
    return await evidence_service.lookup(user, body.finding_id, body.rule_set_id, body.attributes)


# ---------------------------------------------------------------------------
# POST /evidence/accept
# ---------------------------------------------------------------------------
@router.post(
    "/accept",
    dependencies=[Depends(require_phi_capability(Capability.EVIDENCE_ACCEPT))],
    response_model=Finding,
)
async def evidence_accept(
    body: EvidenceAcceptRequest,
    evidence_service: EvidenceServiceDep,
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Finding:
    """Pin an accepted evidence reference to a finding.

    Re-evaluates the rule set against the supplied attributes and refuses a
    ``ruleId`` that is not among the current matches.  Persists the reference on
    ``finding.evidence`` and writes an ``EVIDENCE_ACCEPTED`` audit event.
    """
    _require_idempotency_key(idempotency_key)
    return await evidence_service.accept(
        user, body.finding_id, body.rule_set_id, body.rule_id, body.attributes
    )
