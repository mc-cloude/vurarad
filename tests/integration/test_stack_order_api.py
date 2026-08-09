# ruff: noqa: B008
"""Integration tests for the stack-order / geometry contract on the wire (§3.5, B6).

Verifies that series and instance payloads carry ``imagePositionPatient``,
``imageOrientationPatient``, ``sliceLocation``, ``numberOfFrames``,
``stackIndex``, ``stackOrderBasis``, and ``stackOrderConfidence`` — and that
the server presents instances in geometric (``stackIndex``) order, not
``InstanceNumber`` order, even when the two contradict (acceptance criteria 8
and 9).

Three fixtures are exercised:
- **Reversed InstanceNumber** — ``InstanceNumber`` descends while
  ``ImagePositionPatient`` ascends; the response must follow the geometric
  order with basis ``IMAGE_POSITION_PATIENT_PROJECTED``.
- **Irregular spacing** — monotonic but unevenly spaced positions; the
  response must carry confidence ``IRREGULAR_SPACING``.
- **No geometry** — no position data at all; the response must carry basis
  ``INSTANCE_NUMBER`` and confidence ``UNVERIFIED``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import SecondFactorState
from app.core.capabilities import Role
from app.models.series import Instance, Series, StackOrderBasis, StackOrderConfidence
from app.repositories.base import InMemoryDocumentStore
from app.storage.base import ObjectMetadata, ObjectRef
from tests.conftest import VALID_TOKEN, FakeTokenVerifier, StubAuditStore, make_user


# ---------------------------------------------------------------------------
# Minimal pixel store stub (not exercised — only needed for app wiring)
# ---------------------------------------------------------------------------
class _StubPixelStore:
    _bucket = "test-pix"

    async def generate_signed_read_url(
        self,
        key: str,
        ttl_seconds: int,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        return f"https://signed.example/{self._bucket}/{key}?ttl={ttl_seconds}"

    async def put(
        self, key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None
    ) -> ObjectRef:
        return ObjectRef(bucket=self._bucket, key=key)

    async def get_blob(self, key: str) -> bytes:
        return b""

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        return b""

    async def delete(self, key: str) -> None:
        pass

    async def exists(self, key: str) -> bool:
        return False

    async def list_prefix(self, prefix: str, limit: int = 1000) -> list[ObjectRef]:
        return []

    async def copy(self, src_key: str, dst_key: str) -> ObjectRef:
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def rewrite(
        self,
        src_key: str,
        dst_key: str,
        *,
        cache_control: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectRef:
        return ObjectRef(bucket=self._bucket, key=dst_key)

    async def object_metadata(self, key: str) -> ObjectMetadata:
        return ObjectMetadata(
            ref=ObjectRef(bucket=self._bucket, key=key),
            size=0,
            content_type="",
            etag="",
            updated=datetime.now(UTC),
        )

    async def generate_signed_upload_url(
        self, key: str, content_type: str, ttl_seconds: int
    ) -> str:
        return f"https://upload.example/{key}"

    async def create_resumable_upload(
        self, key: str, content_type: str, expected_bytes: int
    ) -> str:
        return f"https://resumable.example/{key}"

    @property
    def supports_bucket_lock(self) -> bool:
        return True

    async def set_retention_policy(self, retention_days: int) -> None:
        pass


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------
AXIAL_IOP = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _instance(
    sop: str,
    idx: int,
    *,
    z: float | None = None,
    instance_number: int | None = None,
    iop: list[float] | None = None,
    size: int = 526336,
) -> Instance:
    return Instance(
        sop_instance_uid=sop,
        stack_index=idx,
        instance_number=instance_number,
        number_of_frames=1,
        image_position_patient=[0.0, 0.0, z] if z is not None else None,
        image_orientation_patient=(
            iop if iop is not None else (AXIAL_IOP if z is not None else None)
        ),
        slice_location=z,
        size_bytes=size,
        object_path=f"studies/st_geom/se_geom/{idx:04d}.dcm",
    )


def _series(
    n: int = 5,
    *,
    spacing: float = 5.0,
    basis: StackOrderBasis = StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED,
    confidence: StackOrderConfidence = StackOrderConfidence.RELIABLE,
    irregular: bool = False,
    no_geometry: bool = False,
    reversed_instance_number: bool = False,
    shuffle: bool = False,
) -> Series:
    """Build a series with the requested geometry configuration.

    ``shuffle`` scrambles the instance list order before returning so the
    server must sort by ``stackIndex`` — proving the order is server-computed,
    not just passed through from storage.
    """
    instances: list[Instance] = []
    for i in range(n):
        if no_geometry:
            inst = _instance(f"1.2.3.{i}", i, instance_number=i + 1)
            inst.image_position_patient = None
            inst.image_orientation_patient = None
            inst.slice_location = None
        elif irregular:
            z = i * spacing if i % 2 == 0 else i * spacing * 1.5
            inst = _instance(f"1.2.3.{i}", i, z=z, instance_number=i + 1)
        elif reversed_instance_number:
            inst = _instance(f"1.2.3.{i}", i, z=i * spacing, instance_number=n - i)
        else:
            inst = _instance(f"1.2.3.{i}", i, z=i * spacing, instance_number=i + 1)
        instances.append(inst)
    if shuffle:
        # Reverse the list so stored order ≠ stackIndex order.
        instances.reverse()
    return Series(
        series_id="se_geom",
        study_id="st_geom",
        study_instance_uid="1.2.840.113619.2.55.3.604688119.971",
        series_instance_uid="1.2.840.113619.2.55.3.se_geom",
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        stack_order_basis=basis,
        stack_order_confidence=confidence,
        instance_count=n,
        frame_count=n,
        is_multi_frame=False,
        instances=instances,
        created_at="2026-08-01T09:20:11Z",
    )


def _study_doc(*, series_ids: list[str]) -> dict[str, Any]:
    return {
        "studyId": "st_geom",
        "patientKey": "pk_geom",
        "patientRef": "PT-GEOM",
        "patientAgeSex": "55 M",
        "patientSex": "M",
        "patientName": "Test, Patient",
        "patientBirthDate": "1970-01-01",
        "mrn": "MRN-GEOM",
        "accession": "ACC-GEOM",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:14:00Z",
        "referringPhysician": "",
        "clinicalHistory": "",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "assignedTo": None,
        "seriesCount": len(series_ids),
        "instanceCount": 5,
        "studyBytes": 2631680,
        "hasReport": False,
        "reportId": None,
        "signedAt": None,
        "priorStudies": [],
        "seriesIds": series_ids,
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


def _seed_series(doc_store: InMemoryDocumentStore, series: Series) -> None:
    asyncio.run(doc_store.set("series", series.series_id, series.model_dump()))
    asyncio.run(doc_store.set("studies", "st_geom", _study_doc(series_ids=[series.series_id])))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def pixel_store() -> _StubPixelStore:
    return _StubPixelStore()


@pytest.fixture
def client(
    doc_store: InMemoryDocumentStore,
    audit_mirror: InMemoryAuditMirror,
    pixel_store: _StubPixelStore,
) -> TestClient:
    from app.main import create_app

    app = create_app()
    app.state.token_verifier = FakeTokenVerifier(
        default_user=make_user(role=Role.RADIOLOGIST, mfa_state=SecondFactorState.VERIFIED)
    )
    app.state.audit_object_store = StubAuditStore(locked=True)
    app.state.object_store = pixel_store
    app.state.document_store = doc_store
    app.state.audit_mirror = audit_mirror
    app.state.viewer_scopes = {}
    return TestClient(app)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _series_payload(client: TestClient) -> dict[str, Any]:
    """GET /studies/st_geom/series and return the first (only) series."""
    r = client.get("/api/v1/studies/st_geom/series", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["studyId"] == "st_geom"
    assert len(body["series"]) == 1
    return cast(dict[str, Any], body["series"][0])


# ---------------------------------------------------------------------------
# Criterion 8 — geometry fields present on the wire
# ---------------------------------------------------------------------------
class TestGeometryFieldsOnWire:
    def test_series_carries_stack_order_basis_and_confidence(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_series(doc_store, _series(5))
        s = _series_payload(client)
        assert s["stackOrderBasis"] == "IMAGE_POSITION_PATIENT_PROJECTED"
        assert s["stackOrderConfidence"] == "RELIABLE"

    def test_instance_carries_all_geometry_fields(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_series(doc_store, _series(5))
        s = _series_payload(client)
        inst = s["instances"][0]
        for field in (
            "imagePositionPatient",
            "imageOrientationPatient",
            "sliceLocation",
            "numberOfFrames",
            "stackIndex",
        ):
            assert field in inst, f"Instance payload missing {field}"
        assert inst["imagePositionPatient"] == [0.0, 0.0, 0.0]
        assert inst["imageOrientationPatient"] == AXIAL_IOP
        assert inst["sliceLocation"] == 0.0
        assert inst["numberOfFrames"] == 1
        assert inst["stackIndex"] == 0

    def test_instances_ordered_by_stack_index(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """Even when stored in reverse, the response is sorted by stackIndex."""
        _seed_series(doc_store, _series(5, shuffle=True))
        s = _series_payload(client)
        stack_indices = [i["stackIndex"] for i in s["instances"]]
        assert stack_indices == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Criterion 8 — InstanceNumber contradicts ImagePositionPatient
# ---------------------------------------------------------------------------
class TestReversedInstanceNumber:
    def test_geometric_order_returned_with_ipp_basis(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """InstanceNumber descends but ImagePositionPatient ascends.

        The response must follow the geometric (IPP) order, not InstanceNumber.
        """
        n = 5
        _seed_series(
            doc_store,
            _series(
                n,
                reversed_instance_number=True,
                basis=StackOrderBasis.IMAGE_POSITION_PATIENT_PROJECTED,
                confidence=StackOrderConfidence.RELIABLE,
            ),
        )
        s = _series_payload(client)

        # Basis confirms the geometric derivation.
        assert s["stackOrderBasis"] == "IMAGE_POSITION_PATIENT_PROJECTED"

        # stackIndex is dense, gapless, zero-based — geometric order.
        stack_indices = [i["stackIndex"] for i in s["instances"]]
        assert stack_indices == [0, 1, 2, 3, 4]

        # instanceNumber descends — proving it contradicts the geometric order.
        instance_numbers = [i["instanceNumber"] for i in s["instances"]]
        assert instance_numbers == [n, n - 1, n - 2, n - 3, n - 4]

        # z-positions (IPP[2]) ascend — the geometric order the server follows.
        z_values = [i["imagePositionPatient"][2] for i in s["instances"]]
        assert z_values == [0.0, 5.0, 10.0, 15.0, 20.0]

    def test_reversed_with_shuffle_still_geometric(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """Stored in reverse AND shuffled — server still sorts by stackIndex."""
        n = 5
        _seed_series(
            doc_store,
            _series(n, reversed_instance_number=True, shuffle=True),
        )
        s = _series_payload(client)
        stack_indices = [i["stackIndex"] for i in s["instances"]]
        assert stack_indices == [0, 1, 2, 3, 4]
        # instanceNumber still descends in stackIndex order.
        instance_numbers = [i["instanceNumber"] for i in s["instances"]]
        assert instance_numbers == [n, n - 1, n - 2, n - 3, n - 4]


# ---------------------------------------------------------------------------
# Criterion 9 — irregular spacing → IRREGULAR_SPACING
# ---------------------------------------------------------------------------
class TestIrregularSpacing:
    def test_irregular_spacing_returns_irregular_confidence(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_series(
            doc_store,
            _series(
                5,
                irregular=True,
                confidence=StackOrderConfidence.IRREGULAR_SPACING,
            ),
        )
        s = _series_payload(client)
        assert s["stackOrderConfidence"] == "IRREGULAR_SPACING"
        # Still geometrically ordered — just unevenly spaced.
        stack_indices = [i["stackIndex"] for i in s["instances"]]
        assert stack_indices == [0, 1, 2, 3, 4]

    def test_irregular_spacing_z_values_not_uniform(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """Irregular spacing means the z-gaps are not uniform."""
        _seed_series(
            doc_store,
            _series(5, irregular=True, confidence=StackOrderConfidence.IRREGULAR_SPACING),
        )
        s = _series_payload(client)
        z_values = [i["imagePositionPatient"][2] for i in s["instances"]]
        gaps = [z_values[i + 1] - z_values[i] for i in range(len(z_values) - 1)]
        # At least one gap differs from the others.
        assert len(set(gaps)) > 1, f"Expected non-uniform gaps, got {gaps}"


# ---------------------------------------------------------------------------
# Criterion 9 — no position data → INSTANCE_NUMBER / UNVERIFIED
# ---------------------------------------------------------------------------
class TestNoGeometry:
    def test_no_position_data_returns_instance_number_basis(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_series(
            doc_store,
            _series(
                5,
                no_geometry=True,
                basis=StackOrderBasis.INSTANCE_NUMBER,
                confidence=StackOrderConfidence.UNVERIFIED,
            ),
        )
        s = _series_payload(client)
        assert s["stackOrderBasis"] == "INSTANCE_NUMBER"
        assert s["stackOrderConfidence"] == "UNVERIFIED"

    def test_no_position_data_fields_are_null(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """The geometry fields are present but null — never silently omitted."""
        _seed_series(
            doc_store,
            _series(
                5,
                no_geometry=True,
                basis=StackOrderBasis.INSTANCE_NUMBER,
                confidence=StackOrderConfidence.UNVERIFIED,
            ),
        )
        s = _series_payload(client)
        inst = s["instances"][0]
        # Fields are present on the wire but null.
        assert "imagePositionPatient" in inst
        assert inst["imagePositionPatient"] is None
        assert "imageOrientationPatient" in inst
        assert inst["imageOrientationPatient"] is None
        assert "sliceLocation" in inst
        assert inst["sliceLocation"] is None
        # stackIndex and numberOfFrames still present.
        assert inst["stackIndex"] == 0
        assert inst["numberOfFrames"] == 1

    def test_unverified_never_presented_as_reliable(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """The API never silently presents an unverified order as reliable."""
        _seed_series(
            doc_store,
            _series(
                5,
                no_geometry=True,
                basis=StackOrderBasis.INSTANCE_NUMBER,
                confidence=StackOrderConfidence.UNVERIFIED,
            ),
        )
        s = _series_payload(client)
        assert s["stackOrderConfidence"] != "RELIABLE"


# ---------------------------------------------------------------------------
# Multi-frame geometry on the wire (§3.5 / WP9)
# ---------------------------------------------------------------------------
class TestMultiFrameGeometryOnWire:
    """Multi-frame series expose numberOfFrames, perFrameFunctionalGroups, and
    a dense, gapless per-frame ``stackIndex`` (criterion 4).

    A multi-frame SOP instance carries ``numberOfFrames > 1`` frames; the
    series listing expands it into one logical-frame entry per frame so the
    viewer receives a flat, dense ``stackIndex`` sequence spanning the whole
    series.
    """

    @staticmethod
    def _multiframe_series() -> Series:
        # Two multi-frame SOP instances: 4 frames + 3 frames = 7 logical frames.
        inst0 = Instance(
            sop_instance_uid="1.2.3.100",
            stack_index=0,
            instance_number=1,
            number_of_frames=4,
            image_position_patient=[0.0, 0.0, 0.0],
            image_orientation_patient=AXIAL_IOP,
            slice_location=0.0,
            size_bytes=2097152,
            object_path="studies/st_geom/se_mf/0000.dcm",
        )
        inst1 = Instance(
            sop_instance_uid="1.2.3.101",
            stack_index=1,
            instance_number=2,
            number_of_frames=3,
            image_position_patient=[0.0, 0.0, 20.0],
            image_orientation_patient=AXIAL_IOP,
            slice_location=20.0,
            size_bytes=1572864,
            object_path="studies/st_geom/se_mf/0001.dcm",
        )
        return Series(
            series_id="se_mf",
            study_id="st_geom",
            study_instance_uid="1.2.840.113619.2.55.3.604688119.971",
            series_instance_uid="1.2.840.113619.2.55.3.se_mf",
            modality="CT",
            sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
            stack_order_basis=StackOrderBasis.FRAME_INDEX,
            stack_order_confidence=StackOrderConfidence.RELIABLE,
            instance_count=2,
            frame_count=7,
            is_multi_frame=True,
            instances=[inst0, inst1],
            created_at="2026-08-01T09:20:11Z",
        )

    def test_series_exposes_multiframe_flags(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_series(doc_store, self._multiframe_series())
        s = _series_payload(client)
        assert s["isMultiFrame"] is True
        assert s["perFrameFunctionalGroups"] is True
        assert s["instanceCount"] == 2
        assert s["frameCount"] == 7

    def test_instances_expanded_to_per_frame_entries(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        _seed_series(doc_store, self._multiframe_series())
        s = _series_payload(client)
        frames = s["instances"]
        # 4 + 3 = 7 logical frames on the wire.
        assert len(frames) == 7
        # Dense, gapless, zero-based stackIndex across the whole series.
        assert [f["stackIndex"] for f in frames] == [0, 1, 2, 3, 4, 5, 6]
        assert len({f["stackIndex"] for f in frames}) == 7
        # frameIndex cycles within each parent SOP instance.
        assert [f["frameIndex"] for f in frames] == [0, 1, 2, 3, 0, 1, 2]
        # numberOfFrames is the parent instance frame count on every frame.
        assert [f["numberOfFrames"] for f in frames] == [4, 4, 4, 4, 3, 3, 3]
        # Each frame carries the SOP instance UID of its parent object.
        assert [f["sopInstanceUid"] for f in frames] == ["1.2.3.100"] * 4 + [
            "1.2.3.101"
        ] * 3

    def test_single_frame_series_has_no_per_frame_functional_groups(
        self, client: TestClient, doc_store: InMemoryDocumentStore
    ) -> None:
        """Single-frame series keep the existing contract: frameIndex null, no
        perFrameFunctionalGroups flag, dense stackIndex per instance."""
        _seed_series(doc_store, _series(5))
        s = _series_payload(client)
        assert s["isMultiFrame"] is False
        assert s["perFrameFunctionalGroups"] is False
        for f in s["instances"]:
            assert f["frameIndex"] is None
            assert f["numberOfFrames"] == 1
        assert [f["stackIndex"] for f in s["instances"]] == [0, 1, 2, 3, 4]
