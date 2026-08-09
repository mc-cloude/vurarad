# ruff: noqa: B008
"""Contract suite — one ``MetadataStore`` contract, two production backends.

Parametrised over the Firestore emulator and a PostgreSQL container.  Every
backend must pass every test: ``get``/``set``/``create``/``update``/``delete``/
``query`` semantics, atomic ``create``, and the deep-merge behaviour that makes
``update`` match Firestore ``set(merge=True)``.  A backend that fails any
contract test is not shippable — the on-prem PostgreSQL tier is a compliance
requirement, so it must be observably identical to the cloud Firestore tier.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid
from typing import Any

import pytest

from app.repositories.base import FirestoreDocumentStore, MetadataStore
from app.repositories.postgres_impl.store import PostgresMetadataStore

FIRESTORE_IMAGE = "mtlynch/firestore-emulator:latest"
POSTGRES_IMAGE = "postgres:16-alpine"
FIRESTORE_PROJECT = "vurarad-contract-test"


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------
def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_port(host: str, port: int, timeout: int = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def _start_container(
    image: str,
    name: str,
    ports: dict[str, int],
    env: list[str] | None = None,
    command: list[str] | None = None,
) -> None:
    cmd = ["docker", "run", "-d", "--name", name]
    for container_port, host_port in ports.items():
        cmd.extend(["-p", f"{host_port}:{container_port}"])
    if env:
        for e in env:
            cmd.extend(["-e", e])
    cmd.append(image)
    if command:
        cmd.extend(command)
    subprocess.run(cmd, check=True, capture_output=True)


def _stop_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


# ---------------------------------------------------------------------------
# Backend containers (module-scoped)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def firestore_emulator() -> Any:
    if not _docker_available():
        pytest.skip("Docker not available")
    host_port = _free_port()
    name = f"vurarad-fs-contract-{uuid.uuid4().hex[:8]}"
    _start_container(
        FIRESTORE_IMAGE,
        name,
        {"8080": host_port},
        env=[f"FIRESTORE_PROJECT_ID={FIRESTORE_PROJECT}"],
    )
    if not _wait_for_port("localhost", host_port):
        _stop_container(name)
        pytest.skip("Firestore emulator did not start")
    os.environ["FIRESTORE_EMULATOR_HOST"] = f"localhost:{host_port}"
    yield name
    os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
    _stop_container(name)


@pytest.fixture(scope="module")
def postgres_container() -> Any:
    if not _docker_available():
        pytest.skip("Docker not available")
    host_port = _free_port()
    name = f"vurarad-pg-contract-{uuid.uuid4().hex[:8]}"
    _start_container(
        POSTGRES_IMAGE,
        name,
        {"5432": host_port},
        env=[
            "POSTGRES_USER=test",
            "POSTGRES_PASSWORD=test",
            "POSTGRES_DB=test",
        ],
    )
    if not _wait_for_port("localhost", host_port):
        _stop_container(name)
        pytest.skip("PostgreSQL container did not start")
    yield {
        "name": name,
        "dsn": f"postgres://test:test@localhost:{host_port}/test",
    }
    _stop_container(name)


# ---------------------------------------------------------------------------
# Parametrised store (module-scoped) + per-test collection (function-scoped)
# ---------------------------------------------------------------------------
def _make_postgres_store(dsn: str) -> MetadataStore:
    # ``ssl=False`` keeps the local container handshake plaintext.  Lazy-connect:
    # the first operation opens the connection and initialises the schema
    # (idempotent).  The function-scoped fixture closes it after each test.
    return PostgresMetadataStore(dsn=dsn, ssl=False)


@pytest.fixture(params=["firestore", "postgres"])
async def store(
    request: pytest.FixtureRequest,
    firestore_emulator: Any,
    postgres_container: Any,
) -> Any:
    """Parametrised MetadataStore — every contract test runs against both.

    Function-scoped (not module-scoped) on purpose: ``asyncio_default_fixture
    _loop_scope`` is ``"function"``, and an asyncpg/Firestore connection is
    bound to the loop that created it.  A module-scoped store would hand a
    dead connection to every test after the first.
    """
    if request.param == "firestore":
        from google.cloud.firestore import AsyncClient

        client = AsyncClient(project=FIRESTORE_PROJECT)
        yield FirestoreDocumentStore(client)
        # AsyncClient.close() is a sync method (returns None).
        client.close()
    else:
        s = _make_postgres_store(postgres_container["dsn"])
        yield s
        await s.close()


@pytest.fixture
def collection() -> str:
    """A unique collection name per test so module-scoped stores stay isolated."""
    return f"contract_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Contract tests — every backend must pass all of these
# ---------------------------------------------------------------------------
class TestMetadataStoreContract:
    def test_isinstance_metadata_store(self, store: Any) -> None:
        assert isinstance(store, MetadataStore)

    async def test_get_missing_returns_none(self, store: Any, collection: str) -> None:
        assert await store.get(collection, "nope") is None

    async def test_set_then_get(self, store: Any, collection: str) -> None:
        await store.set(collection, "d1", {"a": 1, "name": "alice"})
        assert await store.get(collection, "d1") == {"a": 1, "name": "alice"}

    async def test_set_overwrites_entire_document(self, store: Any, collection: str) -> None:
        """``set`` is a full overwrite, not a merge."""
        await store.set(collection, "d1", {"a": 1, "b": 2})
        await store.set(collection, "d1", {"c": 3})
        assert await store.get(collection, "d1") == {"c": 3}

    async def test_create_atomic_first_wins(self, store: Any, collection: str) -> None:
        first = await store.create(collection, "d1", {"a": 1})
        second = await store.create(collection, "d1", {"a": 2})
        assert first is True
        assert second is False
        # The winning payload is the first one.
        assert await store.get(collection, "d1") == {"a": 1}

    async def test_concurrent_create_exactly_one_wins(self, store: Any, collection: str) -> None:
        """``create`` must be atomic under concurrency — exactly one True."""
        results = await asyncio.gather(
            store.create(collection, "race", {"a": 1}),
            store.create(collection, "race", {"a": 2}),
            store.create(collection, "race", {"a": 3}),
        )
        assert sum(1 for r in results if r) == 1

    async def test_update_creates_if_absent(self, store: Any, collection: str) -> None:
        await store.update(collection, "d1", {"a": 1})
        assert await store.get(collection, "d1") == {"a": 1}

    async def test_update_merges_top_level_fields(self, store: Any, collection: str) -> None:
        await store.set(collection, "d1", {"a": 1, "b": 2})
        await store.update(collection, "d1", {"c": 3})
        assert await store.get(collection, "d1") == {"a": 1, "b": 2, "c": 3}

    async def test_update_deep_merges_nested_maps(
        self, store: Any, collection: str
    ) -> None:
        """Nested maps merge recursively — matching Firestore ``set(merge=True)``."""
        await store.set(collection, "d1", {"m": {"x": 1, "y": 2}, "s": "old"})
        await store.update(collection, "d1", {"m": {"y": 20, "z": 30}, "t": True})
        assert await store.get(collection, "d1") == {
            "m": {"x": 1, "y": 20, "z": 30},
            "s": "old",
            "t": True,
        }

    async def test_update_replaces_scalars_and_arrays(
        self, store: Any, collection: str
    ) -> None:
        """Scalars and arrays are replaced, not merged (Firestore semantics)."""
        await store.set(collection, "d1", {"n": 1, "tags": ["a", "b"]})
        await store.update(collection, "d1", {"n": 99, "tags": ["c"]})
        assert await store.get(collection, "d1") == {"n": 99, "tags": ["c"]}

    async def test_delete_is_idempotent(self, store: Any, collection: str) -> None:
        await store.set(collection, "d1", {"a": 1})
        await store.delete(collection, "d1")
        await store.delete(collection, "d1")  # must not raise
        assert await store.get(collection, "d1") is None

    async def test_query_equality_string(self, store: Any, collection: str) -> None:
        await store.set(collection, "a", {"status": "UNREAD", "modality": "CT"})
        await store.set(collection, "b", {"status": "READ", "modality": "CT"})
        rows = await store.query(collection, where=[("status", "==", "UNREAD")])
        ids = {doc_id for doc_id, _ in rows}
        assert ids == {"a"}

    async def test_query_equality_number(self, store: Any, collection: str) -> None:
        await store.set(collection, "a", {"part_index": 0})
        await store.set(collection, "b", {"part_index": 1})
        rows = await store.query(collection, where=[("part_index", "==", 0)])
        assert {doc_id for doc_id, _ in rows} == {"a"}

    async def test_query_equality_boolean(self, store: Any, collection: str) -> None:
        await store.set(collection, "a", {"signed": True})
        await store.set(collection, "b", {"signed": False})
        rows = await store.query(collection, where=[("signed", "==", True)])
        assert {doc_id for doc_id, _ in rows} == {"a"}

    async def test_query_multiple_equality_filters(self, store: Any, collection: str) -> None:
        await store.set(collection, "a", {"status": "UNREAD", "modality": "CT"})
        await store.set(collection, "b", {"status": "UNREAD", "modality": "MR"})
        rows = await store.query(
            collection, where=[("status", "==", "UNREAD"), ("modality", "==", "MR")]
        )
        assert {doc_id for doc_id, _ in rows} == {"b"}

    async def test_query_in_operator(self, store: Any, collection: str) -> None:
        await store.set(collection, "a", {"modality": "CT"})
        await store.set(collection, "b", {"modality": "MR"})
        await store.set(collection, "c", {"modality": "US"})
        rows = await store.query(collection, where=[("modality", "in", ["CT", "MR"])])
        assert {doc_id for doc_id, _ in rows} == {"a", "b"}

    async def test_query_limit(self, store: Any, collection: str) -> None:
        for i in range(5):
            await store.set(collection, f"d{i}", {"status": "UNREAD"})
        rows = await store.query(collection, where=[("status", "==", "UNREAD")], limit=2)
        assert len(rows) == 2

    async def test_query_returns_doc_id_and_data(self, store: Any, collection: str) -> None:
        await store.set(collection, "d1", {"a": 1})
        rows = await store.query(collection, where=[("a", "==", 1)])
        assert rows == [("d1", {"a": 1})]

    async def test_query_no_filters_returns_all_up_to_limit(
        self, store: Any, collection: str
    ) -> None:
        for i in range(3):
            await store.set(collection, f"d{i}", {"i": i})
        rows = await store.query(collection, limit=100)
        assert {doc_id for doc_id, _ in rows} == {"d0", "d1", "d2"}
