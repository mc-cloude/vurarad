"""PostgreSQL backend for the repository-layer :class:`MetadataStore`.

On-prem deployments store all study/series/patient/ingest/audit metadata in
PostgreSQL instead of Firestore.  ``PostgresMetadataStore`` satisfies the exact
same :class:`~app.repositories.base.MetadataStore` contract as
:class:`~app.repositories.base.FirestoreDocumentStore`, asserted by the shared
parametrised contract suite in
``tests/integration/test_metadata_store_contract.py``.
"""

from __future__ import annotations

from app.repositories.postgres_impl.store import PostgresMetadataStore

__all__ = ["PostgresMetadataStore"]
