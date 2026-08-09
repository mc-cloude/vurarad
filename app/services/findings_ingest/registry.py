"""registry — adapter name → adapter, version-pinned, vendor clearance records.

The registry is the single authority that maps an ``(adapterName, adapterVersion)``
pair to a pinned adapter instance and a :class:`VendorClearance` record carrying
the vendor's FDA K-number / CE mark.  Clearance is sourced from HERE, never from
the vendor payload — so the system never upgrades a finding to
``CLEARED_DEVICE`` on the vendor's behalf (proof-based clearance, §3.15.4).

``resolve()`` raises :class:`UnknownAdapterError` for an unregistered
name/version pair — only pinned adapters may ingest.  The default registry pins
the built-in adapters; cleared vendors (Aidoc, Qure.ai) carry a registered
clearance, while the format adapters (DICOM SR, FHIR R4) and the ``generic_v1``
catch-all default to NO clearance (RUO) unless an operator registers one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.errors import UnknownAdapterError
from app.services.findings_ingest.base import FindingAdapter, VendorIdentity
from app.services.findings_ingest.dicom_sr import DicomSRAdapter
from app.services.findings_ingest.fhir_r4 import FhirR4Adapter
from app.services.findings_ingest.vendor.aidoc_v1 import AidocV1Adapter
from app.services.findings_ingest.vendor.generic_v1 import GenericV1Adapter
from app.services.findings_ingest.vendor.qure_v1 import QureV1Adapter

__all__ = ["AdapterRegistry", "VendorClearance"]


@dataclass(frozen=True, slots=True)
class VendorClearance:
    """A registered vendor's clearance record — the proof behind CLEARED_DEVICE.

    ``fda_k_number`` (FDA 510(k) K-number) and ``ce_mark_ref`` (CE mark
    certificate reference) are the two accepted clearance proofs.  At least one
    must be set for a finding to be ``CLEARED_DEVICE``; otherwise the normalizer
    downgrades to ``RUO`` with ``NO_CLEARANCE_REFERENCE``.
    """

    vendor_name: str
    producer: str
    model_version: str
    fda_k_number: str | None = None
    ce_mark_ref: str | None = None
    runtime: str = "external"

    @property
    def has_clearance(self) -> bool:
        return self.fda_k_number is not None or self.ce_mark_ref is not None

    def to_identity(
        self,
        adapter_name: str,
        adapter_version: str,
        produced_at: datetime,
        payload_ref: str | None = None,
    ) -> VendorIdentity:
        """Build the :class:`VendorIdentity` stamped onto every finding."""
        return VendorIdentity(
            vendor_name=self.vendor_name,
            producer=self.producer,
            model_version=self.model_version,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            fda_k_number=self.fda_k_number,
            ce_mark_ref=self.ce_mark_ref,
            runtime=self.runtime,
            produced_at=produced_at,
            payload_ref=payload_ref,
        )


class AdapterRegistry:
    """Version-pinned adapter registry with vendor clearance records."""

    def __init__(self) -> None:
        self._adapters: dict[str, FindingAdapter] = {}
        self._clearance: dict[str, VendorClearance] = {}
        self._register_defaults()

    @staticmethod
    def _key(name: str, version: str) -> str:
        return f"{name}:{version}"

    def register(self, adapter: FindingAdapter, clearance: VendorClearance) -> None:
        """Pin an adapter version with its vendor clearance record."""
        key = self._key(adapter.name, adapter.version)
        self._adapters[key] = adapter
        self._clearance[key] = clearance

    def resolve(self, name: str, version: str) -> tuple[FindingAdapter, VendorClearance]:
        """Return the pinned adapter + clearance, or raise ``UnknownAdapterError``."""
        key = self._key(name, version)
        if key not in self._adapters:
            raise UnknownAdapterError(f"Adapter '{name}' v{version} is not registered")
        return self._adapters[key], self._clearance[key]

    def names(self) -> list[str]:
        """Return the registered adapter keys (``name:version``)."""
        return list(self._adapters)

    def _register_defaults(self) -> None:
        # Cleared vendors with a registered clearance proof.
        self.register(
            AidocV1Adapter(),
            VendorClearance(
                vendor_name="Aidoc",
                producer="Aidoc",
                model_version="aidoc-x-1.0",
                fda_k_number="K223258",
            ),
        )
        self.register(
            QureV1Adapter(),
            VendorClearance(
                vendor_name="Qure.ai",
                producer="qER",
                model_version="qer-3.0",
                ce_mark_ref="CE-1234-UK",
            ),
        )
        # Format adapters default to NO clearance (RUO) unless an operator
        # registers a clearance for the delivering vendor.
        self.register(
            DicomSRAdapter(),
            VendorClearance(vendor_name="DICOM-SR", producer="external", model_version="tid1500-1"),
        )
        self.register(
            FhirR4Adapter(),
            VendorClearance(vendor_name="FHIR-R4", producer="external", model_version="r4-1"),
        )
        # Generic catch-all — no clearance (RUO).
        self.register(
            GenericV1Adapter(),
            VendorClearance(vendor_name="generic", producer="external", model_version="generic-1"),
        )
