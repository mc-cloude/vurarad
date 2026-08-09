"""Series document repository — embedded instances with a 2,000-entry cap.

A series with more than :data:`MAX_INSTANCES_PER_DOC` instances is split into
sibling part documents; ``stackIndex`` is dense and continues across parts, so no
acquisition is rejected for being large.  Part 0 is the primary document
(``series/{seriesId}``); subsequent parts are ``series/{seriesId}__p{N}``.  Every
part carries ``series_id``, ``study_id``, ``study_instance_uid``,
``series_instance_uid``, ``part_index`` and ``part_count`` so the series can be
re-assembled by querying on any of them.
"""

from __future__ import annotations

from typing import Any

from app.models.series import Instance, Series
from app.repositories.base import DocumentStore

MAX_INSTANCES_PER_DOC = 2000
SERIES_COLLECTION = "series"


class SeriesRepository:
    """Persist and re-assemble :class:`Series` documents."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    # -- writes --------------------------------------------------------------
    async def save(self, series: Series) -> list[str]:
        """Persist a series, splitting into part documents as needed.

        Returns the document ids written.  Existing parts beyond the new
        ``part_count`` are removed so a re-save after instance growth does not
        leave stale trailing parts.
        """
        instances = series.instances
        part_count = max(1, (len(instances) + MAX_INSTANCES_PER_DOC - 1) // MAX_INSTANCES_PER_DOC)
        doc_ids: list[str] = []
        for part_index in range(part_count):
            start = part_index * MAX_INSTANCES_PER_DOC
            end = start + MAX_INSTANCES_PER_DOC
            chunk = instances[start:end]
            part = series.model_copy(
                update={
                    "instances": chunk,
                    "part_index": part_index,
                    "part_count": part_count,
                    "instance_count": len(instances),
                }
            )
            doc_id = self._doc_id(series.series_id, part_index)
            await self._store.set(SERIES_COLLECTION, doc_id, part.model_dump())
            doc_ids.append(doc_id)
        # Remove any stale trailing parts from a previous (larger) save.
        for stale in range(part_count, series.part_count):
            await self._store.delete(SERIES_COLLECTION, self._doc_id(series.series_id, stale))
        return doc_ids

    async def delete_series(self, series_id: str) -> None:
        """Delete every part of a series."""
        parts = await self._store.query(SERIES_COLLECTION, where=[("series_id", "==", series_id)])
        for doc_id, _doc in parts:
            await self._store.delete(SERIES_COLLECTION, doc_id)

    # -- reads ---------------------------------------------------------------
    async def get(self, study_id: str, series_instance_uid: str) -> Series | None:
        """Read and merge all parts of one series within a study."""
        parts = await self._store.query(
            SERIES_COLLECTION,
            where=[
                ("study_id", "==", study_id),
                ("series_instance_uid", "==", series_instance_uid),
            ],
        )
        return self._merge(parts)

    async def get_by_id(self, series_id: str) -> Series | None:
        """Read and merge all parts of a series by its internal id."""
        parts = await self._store.query(SERIES_COLLECTION, where=[("series_id", "==", series_id)])
        return self._merge(parts)

    async def find_by_study_instance_uid(self, study_instance_uid: str) -> list[Series]:
        """Return every (merged) series belonging to a study UID.

        Used for duplicate detection: a non-empty result means the study already
        exists and a re-upload must end ``DUPLICATE``.
        """
        roots = await self._store.query(
            SERIES_COLLECTION,
            where=[
                ("study_instance_uid", "==", study_instance_uid),
                ("part_index", "==", 0),
            ],
        )
        merged: list[Series] = []
        for _doc_id, doc in roots:
            series = await self.get_by_id(doc["series_id"])
            if series is not None:
                merged.append(series)
        return merged

    async def get_series_for_study(self, study_id: str) -> list[Series]:
        """Return every (merged) series for a study — one logical series each."""
        roots = await self._store.query(
            SERIES_COLLECTION,
            where=[("study_id", "==", study_id), ("part_index", "==", 0)],
        )
        merged: list[Series] = []
        for _doc_id, doc in roots:
            series = await self.get_by_id(doc["series_id"])
            if series is not None:
                merged.append(series)
        return merged

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _doc_id(series_id: str, part_index: int) -> str:
        return series_id if part_index == 0 else f"{series_id}__p{part_index}"

    @staticmethod
    def _merge(parts: list[tuple[str, dict[str, Any]]]) -> Series | None:
        if not parts:
            return None
        parts.sort(key=lambda p: p[1].get("part_index", 0))
        base = parts[0][1]
        instances: list[Instance] = []
        for _doc_id, doc in parts:
            for inst in doc.get("instances", []):
                instances.append(Instance.model_validate(inst))
        series = Series.model_validate(base)
        series.instances = instances
        series.instance_count = len(instances)
        return series
