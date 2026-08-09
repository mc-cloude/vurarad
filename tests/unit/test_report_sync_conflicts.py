"""ReportSyncService — conflict resolution, idempotency, versioning, audit (WP15).

Unit tests for the offline draft sync mutation ledger (criteria 4-9):

- **Cross-user conflict** (criterion 6): a section modified by a different user
  after ``baseVersion`` conflicts — both texts returned, mutation not applied,
  not recorded in the ledger (re-send re-evaluates).
- **Same-user later-at-wins** (criterion 6): the same user's later edit wins.
- **APPEND_DICTATION never conflicts** (criterion 7): ordered by ``at``.
- **Idempotency by mutationId** (criterion 4): re-send is a no-op replay.
- **SIGNED → 409 REPORT_SIGNED** (criterion 5).
- **One version doc per sync** (criterion 8): only when something changed.
- **REPORT_SYNCED audit** (criterion 9): applied + conflicted mutation IDs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.v1.routers.acquisition_deps import InMemoryAuditMirror
from app.core.auth import AuthenticatedUser
from app.core.capabilities import Role
from app.core.errors import ReportSignedError
from app.repositories.base import InMemoryDocumentStore
from app.repositories.study_repo import StudyRepository
from app.repositories.sync_repo import (
    REPORT_SYNC_MUTATIONS_COLLECTION,
    REPORT_VERSIONS_COLLECTION,
    REPORTS_COLLECTION,
    SyncRepository,
)
from app.services.audit_service import AuditService
from app.services.report_sync_service import (
    MutationType,
    ReportStatus,
    ReportSyncService,
    SyncMutation,
    SyncRequest,
)
from tests.conftest import make_user

STUDY_ID = "st_test"
REPORT_ID = "rp_test"


# ---------------------------------------------------------------------------
# Study doc builder — assigned to a radiologist so assert_can_write passes
# ---------------------------------------------------------------------------
def _study_doc(*, assigned_uid: str = "rad-a") -> dict[str, Any]:
    return {
        "studyId": STUDY_ID,
        "patientKey": "pk_test",
        "patientRef": "PT-001",
        "patientAgeSex": "41 F",
        "patientSex": "F",
        "patientName": "Doe, John",
        "patientBirthDate": "1985-03-02",
        "mrn": "MRN-4471",
        "accession": "ACC-001",
        "modality": "CT",
        "bodyPart": "CHEST",
        "description": "CT Chest",
        "studyDate": "2026-08-01T09:14:00Z",
        "status": "UNREAD",
        "priority": "ROUTINE",
        "assignedTo": {
            "uid": assigned_uid,
            "operatorId": "01HZTEST",
            "displayName": "Rad A",
        },
        "seriesCount": 1,
        "instanceCount": 10,
        "studyBytes": 5263360,
        "hasReport": False,
        "reportId": None,
        "signedAt": None,
        "priorStudies": [],
        "seriesIds": ["se_1"],
        "tenantId": "default",
        "createdAt": "2026-08-01T09:20:11Z",
        "updatedAt": "2026-08-01T09:20:11Z",
        "version": 1,
    }


async def _reassign(doc_store: InMemoryDocumentStore, uid: str) -> None:
    """Re-assign the study to a different radiologist."""
    doc = await doc_store.get("studies", STUDY_ID)
    assert doc is not None
    doc["assignedTo"] = {"uid": uid, "operatorId": "01HZTEST", "displayName": uid}
    await doc_store.set("studies", STUDY_ID, doc)


async def _set_report_signed(doc_store: InMemoryDocumentStore) -> None:
    """Flip a report's status to SIGNED (simulates a sign operation).

    The report doc was stored via ``model_dump()`` (snake_case keys), so we
    overwrite the snake_case fields rather than adding camelCase aliases that
    ``extra="forbid"`` would reject.
    """
    doc = await doc_store.get(REPORTS_COLLECTION, REPORT_ID)
    assert doc is not None
    doc["status"] = ReportStatus.SIGNED.value
    doc["signed_at"] = datetime.now(UTC).isoformat()
    doc["signed_by"] = "rad-a"
    await doc_store.set(REPORTS_COLLECTION, REPORT_ID, doc)


# ---------------------------------------------------------------------------
# Service builder
# ---------------------------------------------------------------------------
def _build_service(
    doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
) -> ReportSyncService:
    sync_repo = SyncRepository(doc_store)
    study_repo = StudyRepository(doc_store)
    audit = AuditService(audit_mirror)
    return ReportSyncService(doc_store, sync_repo, study_repo, audit)


def _mut(
    mid: str,
    *,
    type: MutationType,  # noqa: A002
    text: str,
    at: str,
    section_id: str | None = None,
    base_version: int = 0,
) -> SyncMutation:
    return SyncMutation(
        mutation_id=mid,
        type=type,
        section_id=section_id,
        text=text,
        base_version=base_version,
        at=datetime.fromisoformat(at),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def doc_store() -> InMemoryDocumentStore:
    store = InMemoryDocumentStore()
    asyncio.run(store.set("studies", STUDY_ID, _study_doc()))
    return store


@pytest.fixture
def audit_mirror() -> InMemoryAuditMirror:
    return InMemoryAuditMirror()


@pytest.fixture
def service(
    doc_store: InMemoryDocumentStore, audit_mirror: InMemoryAuditMirror
) -> ReportSyncService:
    return _build_service(doc_store, audit_mirror)


@pytest.fixture
def rad_a() -> AuthenticatedUser:
    return make_user(uid="rad-a", role=Role.RADIOLOGIST)


@pytest.fixture
def rad_b() -> AuthenticatedUser:
    return make_user(uid="rad-b", role=Role.RADIOLOGIST)


async def _create(service: ReportSyncService, user: AuthenticatedUser) -> None:
    await service.create_report(user, STUDY_ID, report_id=REPORT_ID)


# ---------------------------------------------------------------------------
# Criterion 6 — cross-user section conflict
# ---------------------------------------------------------------------------
class TestCrossUserConflict:
    async def test_cross_user_conflict_carries_both_texts(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
        rad_b: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        # User A sets the "findings" section (version 0 → 1).
        res_a = await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="A's text",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        assert res_a.applied == ["m1"]
        assert res_a.conflicted == []
        assert res_a.version == 1

        # Reassign to User B — now B can write.
        await _reassign(doc_store, "rad-b")
        # User B tries to set the same section based on baseVersion 0.
        res_b = await service.sync(
            rad_b,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m2",
                        type=MutationType.SET_SECTION,
                        text="B's text",
                        at="2026-08-01T11:00:00Z",
                        section_id="findings",
                        base_version=0,
                    )
                ]
            ),
        )
        # Conflict — not applied, carries both texts.
        assert "m2" not in res_b.applied
        assert len(res_b.conflicted) == 1
        conflict = res_b.conflicted[0]
        assert conflict.mutation_id == "m2"
        assert conflict.section_id == "findings"
        assert conflict.server_text == "A's text"
        assert conflict.client_text == "B's text"
        assert conflict.server_by == "rad-a"
        assert conflict.client_by == "rad-b"
        assert conflict.server_version == 1
        # Version not bumped (nothing applied).
        assert res_b.version == 1

    async def test_conflict_not_recorded_in_ledger_resend_reevaluates(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
        rad_b: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="A",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        await _reassign(doc_store, "rad-b")
        # First send — conflict.
        req = SyncRequest(
            mutations=[
                _mut(
                    "m2",
                    type=MutationType.SET_SECTION,
                    text="B",
                    at="2026-08-01T11:00:00Z",
                    section_id="findings",
                    base_version=0,
                )
            ]
        )
        res1 = await service.sync(rad_b, REPORT_ID, req)
        assert len(res1.conflicted) == 1
        # Re-send the SAME mutationId — it is NOT in the ledger, so it
        # re-evaluates and conflicts again (not a no-op replay).
        res2 = await service.sync(rad_b, REPORT_ID, req)
        assert len(res2.conflicted) == 1
        assert "m2" not in res2.applied


# ---------------------------------------------------------------------------
# Criterion 6 — same-user later-at-wins
# ---------------------------------------------------------------------------
class TestSameUserLaterAtWins:
    async def test_same_user_later_at_wins(
        self,
        service: ReportSyncService,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        # First edit at t1.
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="first",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        # Second edit by same user at t2 > t1, same baseVersion.
        res = await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m2",
                        type=MutationType.SET_SECTION,
                        text="second",
                        at="2026-08-01T11:00:00Z",
                        section_id="findings",
                        base_version=0,
                    )
                ]
            ),
        )
        assert res.applied == ["m2"]
        assert res.conflicted == []
        # The later edit won.
        report = await service.get_report(rad_a, REPORT_ID)
        assert any(s.text == "second" for s in report.sections)

    async def test_same_user_earlier_at_superseded(
        self,
        service: ReportSyncService,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        # Edit at t2 (later).
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="later",
                        at="2026-08-01T11:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        # Edit at t1 < t2 — superseded, no change, but still "applied" (processed).
        res = await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m2",
                        type=MutationType.SET_SECTION,
                        text="earlier",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                        base_version=0,
                    )
                ]
            ),
        )
        assert "m2" in res.applied
        assert res.conflicted == []
        # The later text won.
        report = await service.get_report(rad_a, REPORT_ID)
        assert all(s.text == "later" for s in report.sections)


# ---------------------------------------------------------------------------
# Criterion 7 — APPEND_DICTATION never conflicts + ordered by at
# ---------------------------------------------------------------------------
class TestAppendDictation:
    async def test_append_never_conflicts_ordered_by_at(
        self,
        service: ReportSyncService,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        # Send segments out of order.
        res = await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "d2",
                        type=MutationType.APPEND_DICTATION,
                        text="second",
                        at="2026-08-01T10:02:00Z",
                    ),
                    _mut(
                        "d1",
                        type=MutationType.APPEND_DICTATION,
                        text="first",
                        at="2026-08-01T10:01:00Z",
                    ),
                    _mut(
                        "d3",
                        type=MutationType.APPEND_DICTATION,
                        text="third",
                        at="2026-08-01T10:03:00Z",
                    ),
                ]
            ),
        )
        assert res.applied == ["d2", "d1", "d3"]
        assert res.conflicted == []
        report = await service.get_report(rad_a, REPORT_ID)
        texts = [s.text for s in report.dictation_segments]
        assert texts == ["first", "second", "third"]
        assert report.dictation_text == "first\nsecond\nthird"

    async def test_append_interleaved_users_no_conflict(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
        rad_b: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "d1",
                        type=MutationType.APPEND_DICTATION,
                        text="A says",
                        at="2026-08-01T10:01:00Z",
                    ),
                ]
            ),
        )
        await _reassign(doc_store, "rad-b")
        res = await service.sync(
            rad_b,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "d2",
                        type=MutationType.APPEND_DICTATION,
                        text="B says",
                        at="2026-08-01T10:00:00Z",
                    ),
                ]
            ),
        )
        assert res.applied == ["d2"]
        assert res.conflicted == []
        report = await service.get_report(rad_b, REPORT_ID)
        # Ordered by at — B's earlier segment comes first.
        assert [s.text for s in report.dictation_segments] == ["B says", "A says"]


# ---------------------------------------------------------------------------
# Criterion 4 — idempotency by mutationId
# ---------------------------------------------------------------------------
class TestIdempotency:
    async def test_resend_is_noop_replay(
        self,
        service: ReportSyncService,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        req = SyncRequest(
            mutations=[
                _mut(
                    "m1",
                    type=MutationType.SET_SECTION,
                    text="v1",
                    at="2026-08-01T10:00:00Z",
                    section_id="findings",
                )
            ]
        )
        res1 = await service.sync(rad_a, REPORT_ID, req)
        assert res1.version == 1
        # Re-send the exact same mutation.
        res2 = await service.sync(rad_a, REPORT_ID, req)
        assert "m1" in res2.applied
        assert res2.conflicted == []
        # Version NOT bumped — pure replay.
        assert res2.version == 1

    async def test_duplicate_in_same_request_is_noop(
        self,
        service: ReportSyncService,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        res = await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="v1",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    ),
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="v1",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    ),
                ]
            ),
        )
        # Both copies appear in applied; version bumped once.
        assert res.applied == ["m1", "m1"]
        assert res.version == 1


# ---------------------------------------------------------------------------
# Criterion 5 — SIGNED report returns 409 REPORT_SIGNED
# ---------------------------------------------------------------------------
class TestSignedReport:
    async def test_signed_report_raises_report_signed_error(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        # Flip the report to SIGNED.
        await _set_report_signed(doc_store)
        with pytest.raises(ReportSignedError):
            await service.sync(
                rad_a,
                REPORT_ID,
                SyncRequest(
                    mutations=[
                        _mut(
                            "m1",
                            type=MutationType.SET_SECTION,
                            text="x",
                            at="2026-08-01T10:00:00Z",
                            section_id="findings",
                        )
                    ]
                ),
            )


# ---------------------------------------------------------------------------
# Criterion 8 — one version document per sync (only when changed)
# ---------------------------------------------------------------------------
class TestVersionDocuments:
    async def test_one_version_doc_per_changing_sync(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        sync_repo = SyncRepository(doc_store)
        # First sync — changes the report → one version doc.
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="v1",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        assert await sync_repo.count_versions(REPORT_ID) == 1
        # Replay sync — no change → no new version doc.
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="v1",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        assert await sync_repo.count_versions(REPORT_ID) == 1
        # Second changing sync → second version doc.
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m2",
                        type=MutationType.SET_SECTION,
                        text="v2",
                        at="2026-08-01T11:00:00Z",
                        section_id="impressions",
                    )
                ]
            ),
        )
        assert await sync_repo.count_versions(REPORT_ID) == 2

    async def test_pure_conflict_sync_no_version_doc(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
        rad_b: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        sync_repo = SyncRepository(doc_store)
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="A",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        await _reassign(doc_store, "rad-b")
        # A sync that only conflicts → no version doc.
        await service.sync(
            rad_b,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m2",
                        type=MutationType.SET_SECTION,
                        text="B",
                        at="2026-08-01T11:00:00Z",
                        section_id="findings",
                        base_version=0,
                    )
                ]
            ),
        )
        # Still only the one version doc from rad-a's sync.
        assert await sync_repo.count_versions(REPORT_ID) == 1


# ---------------------------------------------------------------------------
# Criterion 9 — REPORT_SYNCED audit with applied + conflicted IDs
# ---------------------------------------------------------------------------
class TestAuditEvent:
    async def test_every_sync_writes_report_synced(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        audit_mirror: InMemoryAuditMirror,
        rad_a: AuthenticatedUser,
        rad_b: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        # A sync that applies one and conflicts one.
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="A",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        await _reassign(doc_store, "rad-b")
        await service.sync(
            rad_b,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m2",
                        type=MutationType.SET_SECTION,
                        text="B",
                        at="2026-08-01T11:00:00Z",
                        section_id="findings",
                        base_version=0,
                    )
                ]
            ),
        )
        synced_events = [e for e in audit_mirror._events if e.event_type == "REPORT_SYNCED"]  # noqa: SLF001
        assert len(synced_events) == 2
        # Second event: applied empty, conflicted ["m2"].
        detail = synced_events[1].detail
        assert detail["reportId"] == REPORT_ID
        assert detail["conflicted"] == ["m2"]
        assert "applied" in detail

    async def test_replay_sync_still_writes_audit(
        self,
        service: ReportSyncService,
        audit_mirror: InMemoryAuditMirror,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        req = SyncRequest(
            mutations=[
                _mut(
                    "m1",
                    type=MutationType.SET_SECTION,
                    text="v1",
                    at="2026-08-01T10:00:00Z",
                    section_id="findings",
                )
            ]
        )
        await service.sync(rad_a, REPORT_ID, req)
        await service.sync(rad_a, REPORT_ID, req)  # replay
        synced_events = [e for e in audit_mirror._events if e.event_type == "REPORT_SYNCED"]  # noqa: SLF001
        # Both syncs write REPORT_SYNCED — even the replay.
        assert len(synced_events) == 2


# ---------------------------------------------------------------------------
# Erasure (criterion 3) — reports + versions + ledger erased with study
# ---------------------------------------------------------------------------
class TestErasure:
    async def test_erase_for_study_removes_all_report_data(
        self,
        service: ReportSyncService,
        doc_store: InMemoryDocumentStore,
        rad_a: AuthenticatedUser,
    ) -> None:
        await _create(service, rad_a)
        await service.sync(
            rad_a,
            REPORT_ID,
            SyncRequest(
                mutations=[
                    _mut(
                        "m1",
                        type=MutationType.SET_SECTION,
                        text="v1",
                        at="2026-08-01T10:00:00Z",
                        section_id="findings",
                    )
                ]
            ),
        )
        # Verify data exists.
        assert await doc_store.get(REPORTS_COLLECTION, REPORT_ID) is not None
        assert await doc_store.get(REPORT_VERSIONS_COLLECTION, f"{REPORT_ID}__v1") is not None
        ledger_id = SyncRepository._ledger_doc_id(REPORT_ID, "m1")  # noqa: SLF001
        assert await doc_store.get(REPORT_SYNC_MUTATIONS_COLLECTION, ledger_id) is not None

        count = await service.erase_for_study(STUDY_ID)
        assert count >= 3  # report + version + ledger
        assert await doc_store.get(REPORTS_COLLECTION, REPORT_ID) is None
        assert await doc_store.get(REPORT_VERSIONS_COLLECTION, f"{REPORT_ID}__v1") is None
        assert await doc_store.get(REPORT_SYNC_MUTATIONS_COLLECTION, ledger_id) is None
