"""Version repository — append-only report version history (WP5).

Versions are immutable snapshots stored at the logical path
``reports/{reportId}/versions/{version}``.  The flat :class:`DocumentStore`
abstraction models this as the ``report_versions`` collection with a doc id of
``{reportId}__v{version}`` — the same encoding the series repository uses for
multi-part series.  This keeps doc ids slash-free (Firestore document ids cannot
contain ``/``) while preserving the logical hierarchy.
"""

from __future__ import annotations

from app.models.report import ReportVersion
from app.repositories.base import DocumentStore

REPORT_VERSIONS_COLLECTION = "report_versions"


def version_doc_id(report_id: str, version: int) -> str:
    """Stable, slash-free doc id for a version snapshot."""
    return f"{report_id}__v{version}"


class VersionRepo:
    """Append-only writer/reader for report version snapshots."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    async def create_version(self, version: ReportVersion) -> None:
        """Write an immutable version snapshot.

        Uses ``set`` (overwrite) so a re-applied transaction idempotently
        produces the same snapshot; the version number is assigned by the caller
        so collisions only occur on a genuine replay of the same logical write.
        """
        await self._store.set(
            REPORT_VERSIONS_COLLECTION,
            version_doc_id(version.report_id, version.version),
            version.model_dump(by_alias=True),
        )

    async def list_versions(self, report_id: str) -> list[ReportVersion]:
        """Return all versions for a report, ordered ascending by version."""
        rows = await self._store.query(
            REPORT_VERSIONS_COLLECTION,
            where=[("reportId", "==", report_id)],
        )
        versions: list[ReportVersion] = []
        for _doc_id, doc in rows:
            versions.append(ReportVersion.model_validate(doc))
        versions.sort(key=lambda v: v.version)
        return versions

    async def get_version(self, report_id: str, version: int) -> ReportVersion | None:
        """Read a single version snapshot, or ``None`` if it does not exist."""
        doc = await self._store.get(
            REPORT_VERSIONS_COLLECTION,
            version_doc_id(report_id, version),
        )
        if doc is None:
            return None
        return ReportVersion.model_validate(doc)


__all__ = ["REPORT_VERSIONS_COLLECTION", "VersionRepo", "version_doc_id"]
