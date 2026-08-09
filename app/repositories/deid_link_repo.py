"""``deid_links`` repository — the write-restricted pseudonym->patientKey map.

The ``deid_links`` collection is the single place that maps a cohort pseudonym
(``cs_…``) back to a clinical ``patientKey``.  It is **write-restricted**: only
the de-identification pipeline (``DeidPipeline``) may write a link, via
:meth:`DeidLinkRepository.write_link`.  Every read — ``get_link``,
``query_by_patient_key``, ``delete_link`` — requires the ``patient:erase``
capability, which ``cohort:*`` deliberately does not grant (acceptance criterion
4).  This is what makes the research/clinical barrier re-identification-safe:
a researcher holding every ``cohort:*`` capability still cannot resolve a
pseudonym to a patient.
"""

from __future__ import annotations

from typing import Any

from app.core.capabilities import Capability
from app.core.errors import PermissionDeniedError
from app.repositories.base import DocumentStore

DEID_LINKS_COLLECTION = "deid_links"


def _assert_patient_erase(capabilities: frozenset[Capability]) -> None:
    """Raise ``PermissionDeniedError`` unless ``patient:erase`` is present.

    The check is synchronous and runs *before* any store access, so a caller
    lacking ``patient:erase`` is rejected without touching the collection.
    """
    if Capability.PATIENT_ERASE not in capabilities:
        raise PermissionDeniedError("Reading deid_links requires the patient:erase capability")


class DeidLinkRepository:
    """Read/write the ``deid_links`` collection with read-side capability gating."""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    # -- write (pipeline only — no capability gate) -------------------------
    async def write_link(self, pseudonym: str, patient_key: str) -> None:
        """Record the pseudonym->patientKey mapping.

        Called only by :class:`DeidPipeline`.  The link must exist before the
        cohort subject is created, so the pipeline awaits this write.
        """
        await self._store.set(
            DEID_LINKS_COLLECTION,
            pseudonym,
            {"pseudonym": pseudonym, "patientKey": patient_key},
        )

    # -- read (requires patient:erase) --------------------------------------
    async def get_link(
        self,
        pseudonym: str,
        *,
        capabilities: frozenset[Capability],
    ) -> dict[str, Any] | None:
        """Return the link document, or ``None``.  Requires ``patient:erase``."""
        _assert_patient_erase(capabilities)
        return await self._store.get(DEID_LINKS_COLLECTION, pseudonym)

    async def query_by_patient_key(
        self,
        patient_key: str,
        *,
        capabilities: frozenset[Capability],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return ``(pseudonym, doc)`` pairs for a patient.  Requires patient:erase."""
        _assert_patient_erase(capabilities)
        return await self._store.query(
            DEID_LINKS_COLLECTION,
            where=[("patientKey", "==", patient_key)],
        )

    # -- delete (requires patient:erase) ------------------------------------
    async def delete_link(
        self,
        pseudonym: str,
        *,
        capabilities: frozenset[Capability],
    ) -> None:
        """Delete a link.  Requires ``patient:erase`` (used by ErasureService)."""
        _assert_patient_erase(capabilities)
        await self._store.delete(DEID_LINKS_COLLECTION, pseudonym)


__all__ = ["DEID_LINKS_COLLECTION", "DeidLinkRepository"]
