"""PostgreSQL-backed ``MetadataStore`` — matches the Firestore contract.

Schema: a single ``metadata_documents`` table keyed by ``(collection, doc_id)``
with a ``JSONB`` ``data`` column.  The Firestore document model maps directly:
``collection`` ↔ Firestore collection, ``doc_id`` ↔ document id, ``data`` ↔ the
document fields.  Equality queries map to JSONB containment (``data @> $j``);
the ``in`` operator is applied in Python (it is rare and the contract suite
exercises it on small sets).

Atomicity:
- ``create`` uses ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` — the row is
  inserted only when ``(collection, doc_id)`` is absent, so concurrent
  ``create`` calls are race-free, exactly like Firestore ``create``.
- ``update`` uses a server-side ``jsonb_deep_merge`` function so nested maps
  are merged recursively — matching Firestore ``set(merge=True)`` (which deep-
  merges maps and replaces scalars/arrays), not the shallow JSONB ``||``.

The asyncpg driver is imported lazily inside :meth:`_ensure_connected` so that
importing this module (and the package ``__init__``) never requires the driver
to be installed — a cloud-tier process that only uses Firestore pays nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.repositories.base import _match

# SQL is stored as module constants so the contract is easy to audit and the
# parametrised test can assert on shape without parsing strings.
_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS metadata_documents (
    collection  TEXT      NOT NULL,
    doc_id      TEXT      NOT NULL,
    data        JSONB     NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, doc_id)
)"""

_CREATE_INDEX_SQL_COLLECTION = (
    "CREATE INDEX IF NOT EXISTS idx_metadata_documents_collection "
    "ON metadata_documents (collection)"
)
_CREATE_INDEX_SQL_DATA_GIN = (
    "CREATE INDEX IF NOT EXISTS idx_metadata_documents_data_gin "
    "ON metadata_documents USING GIN (data)"
)

# Recursive deep-merge: for two JSON objects, merge key-by-key, recursing into
# nested objects; scalars and arrays in ``b`` win (matching Firestore
# ``set(merge=True)``).  Non-object operands return ``b``.
_CREATE_DEEP_MERGE_SQL = """\
CREATE OR REPLACE FUNCTION jsonb_deep_merge(a jsonb, b jsonb) RETURNS jsonb AS $$
DECLARE
    result jsonb;
    k text;
    av jsonb;
    bv jsonb;
BEGIN
    IF jsonb_typeof(a) <> 'object' OR jsonb_typeof(b) <> 'object' THEN
        RETURN b;
    END IF;
    result := a;
    FOR k IN SELECT jsonb_object_keys(b) LOOP
        av := a -> k;
        bv := b -> k;
        IF a ? k AND jsonb_typeof(av) = 'object' AND jsonb_typeof(bv) = 'object' THEN
            result := jsonb_set(result, ARRAY[k], jsonb_deep_merge(av, bv));
        ELSE
            result := jsonb_set(result, ARRAY[k], bv);
        END IF;
    END LOOP;
    RETURN result;
END;
$$ LANGUAGE plpgsql IMMUTABLE"""


class PostgresMetadataStore:
    """``MetadataStore`` backed by PostgreSQL via the asyncpg driver.

    Construction captures the DSN; the connection is opened lazily on first use
    (and guarded by a lock so concurrent first-calls connect once).  This keeps
    :meth:`from_settings` synchronous, matching
    :meth:`FirestoreDocumentStore.from_settings`.
    """

    def __init__(
        self,
        *,
        dsn: str,
        init_schema: bool = True,
        ssl: Any = None,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._init_schema = init_schema
        # ``ssl`` is forwarded to ``asyncpg.create_pool`` (``None`` = driver
        # default, ``False`` / ``"disable"`` for a plaintext local connection).
        # Kept as ``Any`` because asyncpg accepts several ssl option types.
        self._ssl = ssl
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()

    # -- construction --------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Any) -> PostgresMetadataStore:
        """Build a store from application settings (``settings.postgres_dsn``)."""
        dsn = getattr(settings, "postgres_dsn", None)
        if not dsn:
            raise ValueError("postgres_dsn is required for the PostgreSQL metadata backend")
        return cls(dsn=dsn)

    @classmethod
    async def connect(
        cls,
        *,
        dsn: str,
        init_schema: bool = True,
        ssl: Any = None,
    ) -> PostgresMetadataStore:
        """Create a store and open the connection pool immediately."""
        store = cls(dsn=dsn, init_schema=init_schema, ssl=ssl)
        await store._ensure_pool()
        return store

    async def close(self) -> None:
        """Close the underlying connection pool.  Idempotent."""
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    # -- connection management ----------------------------------------------
    async def _init_connection(self, conn: Any) -> None:
        """Per-connection init: register the JSONB codec.

        ``asyncpg.create_pool`` runs this for every new connection so that
        every connection acquired from the pool decodes JSONB to dicts and
        accepts dicts as jsonb params.
        """
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def _ensure_pool(self) -> None:
        if self._pool is not None:
            return
        async with self._pool_lock:
            if self._pool is not None:
                return
            import asyncpg  # lazy: cloud-tier processes never import the driver

            # Retry pool creation: a freshly-started PostgreSQL accepts the TCP
            # connection before it is ready to negotiate, which surfaces as an
            # OSError/ConnectionReset.  A few short retries absorb that race.
            pool: Any = None
            last_exc: Exception | None = None
            for attempt in range(6):
                try:
                    pool = await asyncpg.create_pool(
                        dsn=self._dsn,
                        ssl=self._ssl,
                        min_size=self._min_size,
                        max_size=self._max_size,
                        init=self._init_connection,
                    )
                    break
                except (OSError, asyncpg.PostgresError) as exc:
                    last_exc = exc
                    pool = None
                    if attempt < 5:
                        await asyncio.sleep(1.0)
            if pool is None:
                raise last_exc or RuntimeError("failed to create PostgreSQL pool")

            if self._init_schema:
                async with pool.acquire() as conn:
                    await conn.execute(_CREATE_TABLE_SQL)
                    await conn.execute(_CREATE_DEEP_MERGE_SQL)
                    await conn.execute(_CREATE_INDEX_SQL_COLLECTION)
                    await conn.execute(_CREATE_INDEX_SQL_DATA_GIN)
            self._pool = pool

    # -- MetadataStore contract ---------------------------------------------
    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM metadata_documents WHERE collection = $1 AND doc_id = $2",
                collection,
                doc_id,
            )
        if row is None:
            return None
        return dict(row["data"])

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO metadata_documents (collection, doc_id, data) "
                "VALUES ($1, $2, $3::jsonb) "
                "ON CONFLICT (collection, doc_id) DO UPDATE "
                "SET data = EXCLUDED.data, updated_at = now()",
                collection,
                doc_id,
                data,
            )

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO metadata_documents (collection, doc_id, data) "
                "VALUES ($1, $2, $3::jsonb) "
                "ON CONFLICT (collection, doc_id) DO NOTHING "
                "RETURNING doc_id",
                collection,
                doc_id,
                data,
            )
        return row is not None

    async def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        """Deep-merge ``data`` into the document via ``jsonb_deep_merge``.

        Nested maps are merged recursively and scalars/arrays in ``data``
        overwrite — identical to Firestore ``set(merge=True)``.  Creates the
        document if absent (the ``INSERT`` arm), matching the protocol's
        "creates if absent" semantics.
        """
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO metadata_documents (collection, doc_id, data) "
                "VALUES ($1, $2, $3::jsonb) "
                "ON CONFLICT (collection, doc_id) DO UPDATE "
                "SET data = jsonb_deep_merge(metadata_documents.data, EXCLUDED.data), "
                "updated_at = now()",
                collection,
                doc_id,
                data,
            )

    async def delete(self, collection: str, doc_id: str) -> None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM metadata_documents WHERE collection = $1 AND doc_id = $2",
                collection,
                doc_id,
            )

    async def query(
        self,
        collection: str,
        *,
        where: list[tuple[str, str, Any]] | None = None,
        limit: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        await self._ensure_pool()
        filters = where or []
        eq_filters = [(f, v) for f, op, v in filters if op == "=="]
        # Non-`==` operators (`in`, anything else) are applied in Python after
        # the equality-filtered fetch — they are rare and operate on small sets.
        py_filters = [(f, op, v) for f, op, v in filters if op != "=="]

        params: list[Any] = [collection]
        clauses: list[str] = []
        for idx, (field, value) in enumerate(eq_filters, start=2):
            clauses.append(f"data @> ${idx}::jsonb")
            params.append({field: value})

        sql = "SELECT doc_id, data FROM metadata_documents WHERE collection = $1"
        if clauses:
            sql += " AND " + " AND ".join(clauses)

        async with self._pool.acquire() as conn:
            if not py_filters:
                # Push the limit down to SQL.
                sql += f" LIMIT ${len(params) + 1}"
                params.append(limit)
                rows = await conn.fetch(sql, *params)
                return [(str(r["doc_id"]), dict(r["data"])) for r in rows]

            # Fetch all equality matches, apply the remaining filters in Python,
            # then enforce the limit.
            rows = await conn.fetch(sql, *params)
        out: list[tuple[str, dict[str, Any]]] = []
        for r in rows:
            doc = dict(r["data"])
            if all(_match(doc, f, op, v) for f, op, v in py_filters):
                out.append((str(r["doc_id"]), doc))
                if len(out) >= limit:
                    break
        return out
