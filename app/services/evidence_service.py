"""Evidence service — confirmation gate, lookup, and accept path (WP13 §3.16).

The evidence service is the single place evidence is matched against a finding
and the single place an accepted evidence reference is persisted.  It enforces:

- **Confirmation gate** (criterion 1): evidence may only be looked up for a
  finding whose disposition is ``CONFIRMED`` or ``EDITED``.  A ``PENDING`` or
  ``REJECTED`` finding yields ``409 FINDING_NOT_CONFIRMED`` — evidence is an aid
  to a confirmed finding, never a substitute for the radiologist's disposition.
- **Audit on every operation** (criterion 8): every lookup writes
  ``EVIDENCE_LOOKUP`` (carrying the rule-set version) and every accept writes
  ``EVIDENCE_ACCEPTED``.
- **Nothing accepted without a real match** (criterion 9): ``accept`` re-runs
  the pure engine against the supplied attributes and refuses a ``ruleId`` that
  is not among the current matches — so a stale or fabricated reference can
  never be pinned to a finding.  Only accepted references land in
  ``finding.evidence``, which is the sole source the report body may draw from.

The rule engine itself (:mod:`app.evidence.engine`) is pure; this service owns
all I/O (finding/study reads, audit writes, evidence-ref persistence).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from app.core.auth import AuthenticatedUser
from app.core.errors import ConflictError, FindingNotConfirmedError, NotFoundError
from app.evidence.attributes import validate_attributes
from app.evidence.engine import RuleEngine, RuleMatch
from app.evidence.loader import RuleSetRegistry
from app.models.common import CamelModel
from app.models.finding import EvidenceRef, Finding
from app.models.study import StudyRecord
from app.repositories.base import DocumentStore
from app.repositories.study_repo import StudyRepository
from app.services.access_policy import StudyAccessPolicy
from app.services.audit_service import AuditService
from app.services.finding_service import FINDINGS_COLLECTION

logger = logging.getLogger("vurarad.evidence")

# Disposition states that satisfy the confirmation gate.
_CONFIRMED_STATES = frozenset({"CONFIRMED", "EDITED"})


class EvidenceLookupResult(CamelModel):
    """Response for ``POST /evidence/lookup``."""

    finding_id: str
    rule_set_id: str
    rule_set_version: str
    finding_type: str
    matches: list[RuleMatch] = []
    looked_up_at: datetime


class EvidenceService:
    """Confirmation-gated evidence lookup and accept path."""

    def __init__(
        self,
        store: DocumentStore,
        study_repo: StudyRepository,
        audit: AuditService,
        registry: RuleSetRegistry,
        *,
        access_policy: StudyAccessPolicy | None = None,
    ) -> None:
        self._store = store
        self._study_repo = study_repo
        self._audit = audit
        self._registry = registry
        self._policy = access_policy or StudyAccessPolicy()

    # -- lookup (route) ------------------------------------------------------
    async def lookup(
        self,
        user: AuthenticatedUser,
        finding_id: str,
        rule_set_id: str,
        attributes: dict[str, Any],
    ) -> EvidenceLookupResult:
        """Return the rules in ``rule_set_id`` matching ``attributes``.

        Raises ``409 FINDING_NOT_CONFIRMED`` if the finding is not confirmed, and
        ``404`` if the finding or rule set is absent.  Writes an
        ``EVIDENCE_LOOKUP`` audit event carrying the rule-set version.
        """
        finding = await self._load_finding(finding_id)
        study = await self._load_study(finding.study_id)
        self._policy.assert_can_read(user, study)
        self._assert_confirmed(finding)

        rule_set = self._registry.get(rule_set_id)
        validated = validate_attributes(rule_set.finding_type, attributes)
        matches = RuleEngine.evaluate(rule_set, validated)

        await self._audit.record(
            "EVIDENCE_LOOKUP",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "findingId": finding_id,
                "studyId": study.study_id,
                "ruleSetId": rule_set.id,
                "ruleSetVersion": rule_set.version,
                "findingType": rule_set.finding_type,
                "matchCount": len(matches),
                "operatorId": user.operator_id,
            },
            patient_key=study.patient_key,
        )
        logger.info(
            "EVIDENCE_LOOKUP",
            extra={
                "finding_id": finding_id,
                "rule_set_id": rule_set.id,
                "rule_set_version": rule_set.version,
                "match_count": len(matches),
            },
        )
        return EvidenceLookupResult(
            finding_id=finding_id,
            rule_set_id=rule_set.id,
            rule_set_version=rule_set.version,
            finding_type=rule_set.finding_type,
            matches=matches,
            looked_up_at=datetime.now(UTC),
        )

    # -- accept (route) ------------------------------------------------------
    async def accept(
        self,
        user: AuthenticatedUser,
        finding_id: str,
        rule_set_id: str,
        rule_id: str,
        attributes: dict[str, Any],
    ) -> Finding:
        """Pin an accepted evidence reference to a finding.

        Re-evaluates the rule set against ``attributes`` and refuses a
        ``ruleId`` that is not among the current matches — a reference can only
        be accepted for evidence that genuinely applies right now.  Persists the
        :class:`EvidenceRef` on ``finding.evidence`` and writes an
        ``EVIDENCE_ACCEPTED`` audit event.
        """
        finding = await self._load_finding(finding_id)
        study = await self._load_study(finding.study_id)
        self._policy.assert_can_write(user, study)
        self._assert_confirmed(finding)

        rule_set = self._registry.get(rule_set_id)
        # The rule must exist in the set (404) and match the attributes (409).
        if not any(rule.id == rule_id for rule in rule_set.rules):
            raise NotFoundError(f"Rule {rule_id!r} not found in rule set {rule_set.id!r}")
        validated = validate_attributes(rule_set.finding_type, attributes)
        matches = RuleEngine.evaluate(rule_set, validated)
        match = next((m for m in matches if m.rule_id == rule_id), None)
        if match is None:
            raise ConflictError(
                f"Evidence rule {rule_id!r} does not match the supplied attributes"
            )

        ref = EvidenceRef(
            evidence_id=f"ev_{ULID()}",
            rule_id=match.rule_id,
            citation_id=match.citation_id,
        )
        finding.evidence.append(ref)
        await self._store.set(FINDINGS_COLLECTION, finding_id, finding.model_dump())

        await self._audit.record(
            "EVIDENCE_ACCEPTED",
            actor=user.uid,
            second_factor=user.is_mfa_verified,
            detail={
                "findingId": finding_id,
                "studyId": study.study_id,
                "ruleSetId": rule_set.id,
                "ruleSetVersion": rule_set.version,
                "ruleId": rule_id,
                "citationId": match.citation_id,
                "evidenceId": ref.evidence_id,
                "operatorId": user.operator_id,
            },
            patient_key=study.patient_key,
        )
        logger.info(
            "EVIDENCE_ACCEPTED",
            extra={
                "finding_id": finding_id,
                "rule_set_id": rule_set.id,
                "rule_set_version": rule_set.version,
                "rule_id": rule_id,
                "evidence_id": ref.evidence_id,
            },
        )
        return finding

    # -- helpers -------------------------------------------------------------
    async def _load_finding(self, finding_id: str) -> Finding:
        doc = await self._store.get(FINDINGS_COLLECTION, finding_id)
        if doc is None:
            raise NotFoundError(f"Finding {finding_id} not found")
        return Finding.model_validate(doc)

    async def _load_study(self, study_id: str) -> StudyRecord:
        study = await self._study_repo.get_study(study_id)
        if study is None:
            raise NotFoundError(f"Study {study_id} not found")
        return study

    @staticmethod
    def _assert_confirmed(finding: Finding) -> None:
        if finding.disposition.state not in _CONFIRMED_STATES:
            raise FindingNotConfirmedError(finding.disposition.state)


__all__ = ["EvidenceLookupResult", "EvidenceService"]
