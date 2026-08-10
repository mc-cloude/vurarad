"""WP8 post-migration verification — the cutover gate (§5.6.2 step 6).

Run **after** :mod:`app.tools.migrate_v1` has completed and written its manifest.
It re-derives the migrated counts from the real Firestore + object store and
compares them against the manifest's legacy counts.  Any mismatch — a missing
study, a series whose ``stackOrderConfidence`` is ``UNVERIFIED`` without cause,
a fabricated field name that survived, a report-status drift — exits
**non-zero**, and the cutover runbook forbids proceeding until it is green.

It deliberately re-reads the stores rather than trusting the migration's own
self-check: the cutover gate must be an independent confirmation that what the
operator *thinks* was migrated is actually present and clean.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.series import StackOrderConfidence
from app.repositories.base import DocumentStore, FirestoreDocumentStore
from app.repositories.series_repo import SERIES_COLLECTION, SeriesRepository
from app.storage.base import ObjectStore
from app.tools.migrate_v1 import (
    DICOM_PREFIX,
    FABRICATED_FIELD_NAMES,
    MIGRATABLE,
    MIGRATION_MAP_COLLECTION,
    NEEDS_REINGEST,
    PATIENTS_COLLECTION,
    REPORT_VERSIONS_COLLECTION,
    REPORTS_COLLECTION,
    VerifyCheck,
)

logger = logging.getLogger("vurarad.migration.verify")


@dataclass
class VerificationResult:
    """Outcome of the post-migration verification gate."""

    passed: bool
    checks: list[VerifyCheck] = field(default_factory=list)

    def exit_code(self) -> int:
        return 0 if self.passed else 1


class MigrationVerifier:
    """Independent re-derivation of migrated state vs the migration manifest."""

    def __init__(
        self,
        manifest_path: Path,
        doc_store: DocumentStore,
        object_store: ObjectStore,
    ) -> None:
        self._manifest_path = manifest_path
        self._docs = doc_store
        self._objects = object_store

    async def verify(self) -> VerificationResult:
        manifest = self._load_manifest()
        checks = await self._collect_checks(manifest)
        passed = all(c.passed for c in checks)
        result = VerificationResult(passed=passed, checks=checks)
        for check in checks:
            level = logger.info if check.passed else logger.warning
            level(
                "verify %s: %s (%s)",
                "PASS" if check.passed else "FAIL",
                check.name,
                check.detail,
            )
        return result

    # -- manifest ------------------------------------------------------------
    def _load_manifest(self) -> dict[str, Any]:
        if not self._manifest_path.exists():
            raise SystemExit(f"manifest not found: {self._manifest_path}")
        data: dict[str, Any] = json.loads(self._manifest_path.read_text())
        return data

    # -- checks --------------------------------------------------------------
    async def _collect_checks(self, manifest: dict[str, Any]) -> list[VerifyCheck]:
        legacy = manifest.get("legacy", {})
        studies_meta = manifest.get("studies", [])
        migratable = [
            s for s in studies_meta if s.get("classification") in (MIGRATABLE, NEEDS_REINGEST)
        ]

        series_repo = SeriesRepository(self._docs)
        migrated_studies = 0
        migrated_series = 0
        migrated_instances = 0
        unverified_without_cause: list[str] = []
        for study in migratable:
            new_study_id = study.get("newStudyId")
            if not new_study_id:
                continue
            series_list = await series_repo.get_series_for_study(new_study_id)
            if series_list:
                migrated_studies += 1
            for series in series_list:
                migrated_series += 1
                migrated_instances += series.instance_count
                if series.stack_order_confidence == StackOrderConfidence.UNVERIFIED and (
                    not study.get("sourceLacksPosition")
                ):
                    unverified_without_cause.append(series.series_id)

        migrated_reports = len(await self._docs.query(REPORTS_COLLECTION, limit=10_000))
        migrated_objects = len(await self._objects.list_prefix(DICOM_PREFIX, limit=200_000))

        checks: list[VerifyCheck] = [
            self._count_check("study_count", legacy.get("migratable", 0), migrated_studies),
            self._count_check("series_count", legacy.get("series", 0), migrated_series),
            self._count_check("instance_count", legacy.get("instances", 0), migrated_instances),
            self._count_check("report_count", legacy.get("reports", 0), migrated_reports),
            self._count_check("dicom_object_count", legacy.get("objects", 0), migrated_objects),
        ]

        legacy_dist = dict(legacy.get("reportStatusDistribution", {}))
        migrated_dist = await self._migrated_report_status_distribution()
        checks.append(
            VerifyCheck(
                name="report_status_distribution",
                passed=legacy_dist == migrated_dist,
                detail=f"legacy={legacy_dist} migrated={migrated_dist}",
            )
        )

        checks.append(
            VerifyCheck(
                name="no_unverified_without_cause",
                passed=not unverified_without_cause,
                detail=(
                    "ok"
                    if not unverified_without_cause
                    else f"unverified series without positional cause: {unverified_without_cause}"
                ),
            )
        )

        leaked = await self._scan_fabricated_fields()
        checks.append(
            VerifyCheck(
                name="no_fabricated_fields",
                passed=not leaked,
                detail="ok" if not leaked else f"leaked fields: {sorted(leaked)}",
            )
        )
        return checks

    async def _migrated_report_status_distribution(self) -> dict[str, int]:
        rows = await self._docs.query(REPORTS_COLLECTION, limit=10_000)
        dist: dict[str, int] = {}
        for _doc_id, doc in rows:
            status = str(doc.get("status", ""))
            dist[status] = dist.get(status, 0) + 1
        return dist

    async def _scan_fabricated_fields(self) -> set[str]:
        leaked: set[str] = set()
        for collection in (
            PATIENTS_COLLECTION,
            REPORTS_COLLECTION,
            REPORT_VERSIONS_COLLECTION,
            MIGRATION_MAP_COLLECTION,
            SERIES_COLLECTION,
        ):
            rows = await self._docs.query(collection, limit=50_000)
            for _doc_id, doc in rows:
                leaked.update(_find_fabricated_keys(doc))
        return leaked

    @staticmethod
    def _count_check(name: str, expected: Any, actual: int) -> VerifyCheck:
        expected_int = int(expected) if not isinstance(expected, bool) else 0
        return VerifyCheck(
            name=name,
            passed=expected_int == actual,
            detail=f"expected={expected_int} actual={actual}",
        )


def _find_fabricated_keys(value: object) -> set[str]:
    """Recursively collect any fabricated field names present in a doc tree."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str) and key in FABRICATED_FIELD_NAMES:
                found.add(key)
            found.update(_find_fabricated_keys(sub))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_fabricated_keys(item))
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_migration",
        description="WP8 post-migration verification gate — non-zero exit blocks cutover.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("migration-manifest.json"),
        help="path to the migration manifest written by migrate_v1",
    )
    return parser


async def _verify(args: argparse.Namespace) -> VerificationResult:
    from app.core.config import settings
    from app.storage.factory import build_object_store

    doc_store = FirestoreDocumentStore.from_settings(settings)
    object_store = build_object_store(settings)
    return await MigrationVerifier(args.manifest, doc_store, object_store).verify()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _build_arg_parser().parse_args(argv)
    result = asyncio.run(_verify(args))
    return result.exit_code()


if __name__ == "__main__":
    sys.exit(main())
