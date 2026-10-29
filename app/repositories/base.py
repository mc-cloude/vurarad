"""Metadata-store abstraction for the repository layer.

``MetadataStore`` is the repository-layer equivalent of ``ObjectStore``: a
single ``Protocol`` every backend must satisfy so the rest of the application
depends on the seam, never on a backend SDK.  Backed by Firestore in the cloud
tier and PostgreSQL on-prem; an in-memory store is used in unit tests.
``create`` is atomic — it succeeds only when the document is absent — which is
what makes the ingest lease race-free.

``DocumentStore`` is retained as a backward-compatible alias: existing
repositories import it, and it is the very same protocol object.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MetadataStore(Protocol):
    """Backend-agnostic metadata store.  All methods are ``async``.

    The contract every backend (Firestore, PostgreSQL, in-memory) must satisfy.
    A single parametrised contract suite asserts the two production backends
    behave identically (``tests/integration/test_metadata_store_contract.py``).
    """

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """Return the document, or ``None`` if it does not exist."""
        ...

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        """Create or overwrite a document."""
        ...

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        """Atomically create a document.  Return ``False`` if it already exists."""
        ...

    async def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        """Merge ``data`` into an existing document (creates if absent)."""
        ...

    async def delete(self, collection: str, doc_id: str) -> None:
        """Delete a document.  A missing document is not an error."""
        ...

    async def query(
        self,
        collection: str,
        *,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return ``(doc_id, doc)`` pairs matching the equality filters."""
        ...


# Backward-compatible alias — existing repositories import ``DocumentStore``.
# It resolves to the same protocol, so annotations stay identical.  (Used only
# as a type annotation; runtime ``isinstance`` checks use ``MetadataStore``.)
type DocumentStore = MetadataStore


def _match(doc: dict[str, Any], field: str, op: str, value: Any) -> bool:
    if op == "==":
        return bool(doc.get(field) == value)
    if op == "in":
        return bool(doc.get(field) in value)
    return False


class InMemoryDocumentStore:
    """Deterministic, single-process document store used in tests.

    Serialised by an :class:`asyncio.Lock` so ``create`` is atomic even under
    concurrent coroutines.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        async with self._lock:
            doc = self._data.get(collection, {}).get(doc_id)
            return dict(doc) if doc is not None else None

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._data.setdefault(collection, {})[doc_id] = dict(data)

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        async with self._lock:
            col = self._data.setdefault(collection, {})
            if doc_id in col:
                return False
            col[doc_id] = dict(data)
            return True

    async def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        async with self._lock:
            col = self._data.setdefault(collection, {})
            existing = dict(col.get(doc_id, {}))
            existing.update(data)
            col[doc_id] = existing

    async def delete(self, collection: str, doc_id: str) -> None:
        async with self._lock:
            self._data.get(collection, {}).pop(doc_id, None)

    async def query(
        self,
        collection: str,
        *,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        async with self._lock:
            col = self._data.get(collection, {})
            results: list[tuple[str, dict[str, Any]]] = []
            for doc_id, doc in col.items():
                if where and not all(_match(doc, f, op, v) for f, op, v in where):
                    continue
                results.append((doc_id, dict(doc)))
                if len(results) >= limit:
                    break
            return results


class FirestoreDocumentStore:
    """Production document store backed by Firestore (async client).

    Built lazily from settings so a unit-test process that never hits an ingest
    route does not need credentials or an emulator.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, settings: Any) -> FirestoreDocumentStore:
        from google.cloud.firestore import AsyncClient

        client = AsyncClient(
            project=settings.gcp_project_id,
            database=settings.firestore_database,
        )
        return cls(client)

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = await self._client.collection(collection).document(doc_id).get()
        if not snap.exists:
            return None
        return dict(snap.to_dict() or {})

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._client.collection(collection).document(doc_id).set(data)

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        import google.api_core.exceptions as gexc

        try:
            await self._client.collection(collection).document(doc_id).create(data)
            return True
        except gexc.AlreadyExists:
            return False

    async def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._client.collection(collection).document(doc_id).set(data, merge=True)

    async def delete(self, collection: str, doc_id: str) -> None:
        await self._client.collection(collection).document(doc_id).delete()

    async def query(
        self,
        collection: str,
        *,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        q = self._client.collection(collection)
        if where:
            for field, op, value in where:
                q = q.where(field, op, value)
        q = q.limit(limit)
        snaps = await q.get()
        return [(snap.id, dict(snap.to_dict() or {})) for snap in snaps]


# ---------------------------------------------------------------------------
# Factory — ``build_metadata_store(settings)`` mirrors ``build_object_store``.
#
# Backend imports are lazy so a Firestore-only cloud deployment never imports
# the PostgreSQL driver (and an on-prem deployment never imports the Firestore
# SDK).  Selection keys off ``settings.hosting``: ``"onprem"`` → PostgreSQL,
# anything else → Firestore.
# ---------------------------------------------------------------------------
def build_metadata_store(settings: Any) -> MetadataStore:
    """Return the configured ``MetadataStore`` for the current settings.

    On-prem (``hosting == "onprem"``) returns a PostgreSQL-backed store; the
    cloud tier returns a Firestore-backed store.  Imports are deferred so each
    deployment only needs the driver for its own backend.
    """
    if getattr(settings, "hosting", "cloud") == "onprem":
        from app.repositories.postgres_impl.store import PostgresMetadataStore

        return PostgresMetadataStore.from_settings(settings)

    return FirestoreDocumentStore.from_settings(settings)
