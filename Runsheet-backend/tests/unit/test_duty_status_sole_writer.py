"""
``DutyStatusService`` is the only writer of ``drivers_current.status``.

Two things are asserted here, and they are the two ways the projection could
have drifted from the ``duty_status_events`` log:

1. **No second write path exists.** ``DriverRepository.update`` refuses the
   duty-status projection fields outright, and the one method that writes them —
   ``project_duty_status`` — is named by nothing but the service. The old
   ``PATCH /api/ops/drivers/{driver_id}`` body handed ``status`` straight to
   ``update``, so an administrator moved the projection without appending an
   event; that path now goes through the service and appends one.
2. **Presence is a different axis.** A WebSocket connect or disconnect writes
   the ``driver_presence`` index and nothing else, so a disconnected driver keeps
   the duty status last set (R13.9).

Validates: Requirements 13.9, 13.16, 13.19
- 13.9: presence connection state is independent of duty status — connect and
  disconnect leave ``drivers_current.status`` at its last value
- 13.16: the service is the only writer of ``drivers_current.status``; every
  other write path is refused
- 13.19: an administrator-set transition is recorded as an event carrying the
  administrator in ``actor_id`` and ``source: "admin"``
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from tests.support.auth_seam import auth_headers, install_test_auth

# The fuel driver router transitively imports the ES singleton; stub it exactly
# as the sibling endpoint suites do so the import does not reach a live cluster.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from driver.services.driver_es_mappings import (  # noqa: E402
    DRIVER_PRESENCE_INDEX,
    DUTY_STATUS_EVENTS_INDEX,
)
from driver.services.duty_status_service import DutyStatusService  # noqa: E402
from driver.ws.driver_ws_manager import (  # noqa: E402
    DriverWSManager,
    presence_doc_id,
)
from errors.codes import ErrorCode  # noqa: E402
from fuel.api.driver_endpoints import (  # noqa: E402
    configure_driver_endpoints,
    router as driver_router,
    set_duty_status_service,
)
from fuel.driver_repository import (  # noqa: E402
    DUTY_STATUS_PROJECTION_FIELDS,
    DriverRepository,
    DutyStatusWriteNotPermittedError,
)
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX  # noqa: E402

TENANT = "tenant-alpha"
DRIVER = "drv_001"
ADMIN_USER = "user-admin-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeES:
    """One in-memory store per index, recording every write.

    Exposes only the async surface — ``index_document`` / ``update_document`` /
    ``search_documents`` — which is what the repository, the service, and the
    ``DriverWSManager`` presence writes all use (R10.14).  There is no
    synchronous ``client``, so a blocking presence write would fail here rather
    than pass unnoticed.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.writes: List[tuple[str, str]] = []

    # -- async surface ------------------------------------------------
    async def index_document(self, index, doc_id, document):
        self._put(index, doc_id, dict(document))
        return {"result": "created"}

    async def update_document(self, index, doc_id, partial_doc):
        existing = self.docs.get(index, {}).get(doc_id, {})
        merged = {**existing, **dict(partial_doc)}
        self._put(index, doc_id, merged)
        return {"result": "updated"}

    async def search_documents(self, index, query, size=100):
        hits = [
            {"_source": doc}
            for doc in self.docs.get(index, {}).values()
            if _matches(doc, query)
        ]
        return {"hits": {"hits": hits[:size], "total": {"value": len(hits)}}}

    # -- helpers -----------------------------------------------------
    def _put(self, index, doc_id, document):
        self.docs.setdefault(index, {})[doc_id] = document
        self.writes.append((index, doc_id))

    def driver_doc(self, doc_id: str = DRIVER) -> Dict[str, Any]:
        return self.docs.get(DRIVERS_CURRENT_INDEX, {}).get(doc_id, {})

    def events(self) -> List[Dict[str, Any]]:
        return list(self.docs.get(DUTY_STATUS_EVENTS_INDEX, {}).values())

    def written_indices(self) -> set[str]:
        return {index for index, _ in self.writes}


class _FakeWebSocket:
    """The minimum surface ``BaseWSManager`` needs of a connection."""

    def __init__(self) -> None:
        self.sent: List[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def _matches(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """Evaluate the ``term`` clauses the code under test builds."""
    for clause in _clauses(query.get("query", {})):
        if "term" in clause:
            field, value = next(iter(clause["term"].items()))
            if doc.get(field) != value:
                return False
        elif "terms" in clause:
            field, values = next(iter(clause["terms"].items()))
            if doc.get(field) not in values:
                return False
    return True


def _clauses(node: Any) -> List[Dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    if "term" in node or "terms" in node:
        return [node]
    bool_node = node.get("bool", {})
    out: List[Dict[str, Any]] = []
    for key in ("filter", "must"):
        for clause in bool_node.get(key, []) or []:
            out.extend(_clauses(clause))
    return out


def _driver_doc(status: str = "active", **overrides: Any) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "driver_id": DRIVER,
        "tenant_id": TENANT,
        "driver_name": "Test Driver",
        "phone": "+15551234567",
        "status": status,
        "availability": "available",
        "assigned_truck_id": "truck_001",
        "active_order_count": 0,
        "completed_today": 0,
        "last_event_timestamp": now,
        "source_schema_version": "1.0",
        "trace_id": f"drv_{DRIVER}",
        "created_at": now,
        "updated_at": now,
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def es() -> _FakeES:
    store = _FakeES()
    store.docs[DRIVERS_CURRENT_INDEX] = {DRIVER: _driver_doc()}
    return store


@pytest.fixture
def repo(es: _FakeES) -> DriverRepository:
    return DriverRepository(es)


@pytest.fixture
def service(es: _FakeES, repo: DriverRepository) -> DutyStatusService:
    return DutyStatusService(es_service=es, driver_repository=repo)


# ---------------------------------------------------------------------------
# The repository refuses the projection fields (R13.16)
# ---------------------------------------------------------------------------


class TestRepositoryRefusesTheProjectionFields:
    """``DriverRepository.update`` cannot be used to move ``status``."""

    @pytest.mark.parametrize("field", DUTY_STATUS_PROJECTION_FIELDS)
    @pytest.mark.asyncio
    async def test_update_refuses_each_projection_field(self, repo, field):
        """R13.16: naming ``status`` or its bookkeeping in an update is refused."""
        with pytest.raises(DutyStatusWriteNotPermittedError) as excinfo:
            await repo.update(TENANT, DRIVER, {field: "off_duty"})

        assert excinfo.value.fields == [field]
        # The rejection names the rule and the way in, not just "no".
        assert "DutyStatusService" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_refused_update_writes_nothing(self, repo, es):
        """A refusal must not be a partial write: nothing reaches the index."""
        with pytest.raises(DutyStatusWriteNotPermittedError):
            await repo.update(
                TENANT, DRIVER, {"driver_name": "Renamed", "status": "off_duty"}
            )

        assert es.writes == []
        assert es.driver_doc()["status"] == "active"
        assert es.driver_doc()["driver_name"] == "Test Driver"

    @pytest.mark.asyncio
    async def test_update_still_applies_non_projection_fields(self, repo, es):
        """The guard is scoped: every other field updates as before."""
        updated = await repo.update(
            TENANT, DRIVER, {"driver_name": "Renamed", "availability": "busy"}
        )

        assert updated is not None
        assert updated.driver_name == "Renamed"
        assert updated.availability == "busy"
        assert es.driver_doc()["status"] == "active"


# ---------------------------------------------------------------------------
# ``project_duty_status`` is the one write path (R13.16)
# ---------------------------------------------------------------------------


class TestProjectDutyStatusIsTheOnlyWritePath:
    """The sanctioned writer, and the only module that names it."""

    @pytest.mark.asyncio
    async def test_project_duty_status_writes_status_and_its_bookkeeping(
        self, repo, es
    ):
        """R13.15: the projection records which event produced it."""
        updated = await repo.project_duty_status(
            TENANT,
            DRIVER,
            status="on_break",
            event_id="evt-1",
            updated_at="2026-02-03T08:00:00+00:00",
        )

        assert updated is not None and updated.status == "on_break"
        doc = es.driver_doc()
        assert doc["status"] == "on_break"
        assert doc["duty_status_event_id"] == "evt-1"
        assert doc["duty_status_updated_at"].startswith("2026-02-03T08:00:00")

    @pytest.mark.asyncio
    async def test_project_duty_status_returns_none_for_an_unknown_driver(
        self, repo, es
    ):
        """No record to project onto is a ``None``, not an invented document."""
        result = await repo.project_duty_status(
            TENANT, "drv_missing", status="active", event_id="evt-1"
        )

        assert result is None
        assert es.writes == []

    @pytest.mark.asyncio
    async def test_service_transition_moves_the_projection_through_it(
        self, service, es
    ):
        """R13.3: the service appends the event, then projects it."""
        result = await service.transition(
            TENANT,
            DRIVER,
            "off_duty",
            actor_id=ADMIN_USER,
            source="admin",
            event_timestamp="2026-02-03T08:00:00+00:00",
        )

        events = es.events()
        assert len(events) == 1
        assert events[0]["new_status"] == "off_duty"
        assert events[0]["previous_status"] == "active"
        assert result["projection_applied"] is True

        doc = es.driver_doc()
        assert doc["status"] == "off_duty"
        assert doc["duty_status_event_id"] == events[0]["event_id"]

    def test_only_the_repository_and_the_service_name_the_projection_writer(self):
        """R13.16: no third module reaches for ``project_duty_status``.

        A source scan rather than a behavioural assertion, because the property
        being protected is "no other module names this field in a write" — which
        is a statement about the codebase, not about one call.
        """
        backend_root = Path(__file__).resolve().parents[2]
        skip_parts = {"venv", "tests", ".hypothesis", "__pycache__", "scripts"}

        naming: set[str] = set()
        for path in backend_root.rglob("*.py"):
            if skip_parts.intersection(path.parts):
                continue
            if "project_duty_status" in path.read_text(encoding="utf-8"):
                naming.add(str(path.relative_to(backend_root)))

        assert naming == {
            "fuel/driver_repository.py",
            "driver/services/duty_status_service.py",
        }


# ---------------------------------------------------------------------------
# The administrator PATCH is routed through the service (R13.16, R13.19)
# ---------------------------------------------------------------------------


def _make_app(repo: DriverRepository, duty_status_service: Any) -> FastAPI:
    """Wire ``/api/ops/drivers`` with an explicit duty-status service."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(driver_router)
    configure_driver_endpoints(driver_repository=repo)
    # Assigned explicitly (and unconditionally) so one test cannot leak a
    # service into the next through the module global.
    set_duty_status_service(duty_status_service)
    install_test_auth(app)
    return app


def _admin_headers() -> dict:
    return auth_headers(TENANT, sub=ADMIN_USER, roles=["admin"])


class TestAdminPatchGoesThroughTheService:
    """``PATCH /api/ops/drivers/{id}`` no longer writes ``status`` itself."""

    def test_status_change_appends_an_event_and_moves_the_projection(
        self, repo, service, es
    ):
        """R13.19: the administrator lands in ``actor_id`` with ``source: admin``."""
        client = TestClient(_make_app(repo, service))

        resp = client.patch(
            f"/api/ops/drivers/{DRIVER}",
            json={"status": "inactive"},
            headers=_admin_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "inactive"

        events = es.events()
        assert len(events) == 1
        assert events[0]["new_status"] == "inactive"
        assert events[0]["previous_status"] == "active"
        assert events[0]["actor_id"] == ADMIN_USER
        assert events[0]["source"] == "admin"
        assert es.driver_doc()["status"] == "inactive"

    def test_status_and_other_fields_in_one_body_both_land(self, repo, service, es):
        """The split is invisible to the caller: one request, both writes."""
        client = TestClient(_make_app(repo, service))

        resp = client.patch(
            f"/api/ops/drivers/{DRIVER}",
            json={"status": "on_break", "driver_name": "Renamed"},
            headers=_admin_headers(),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "on_break"
        assert body["driver_name"] == "Renamed"
        assert len(es.events()) == 1

    def test_status_change_without_a_wired_service_fails_closed(self, repo, es):
        """R13.16: refuse the change rather than write behind the event log."""
        client = TestClient(_make_app(repo, None))

        resp = client.patch(
            f"/api/ops/drivers/{DRIVER}",
            json={"status": "off_duty"},
            headers=_admin_headers(),
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == ErrorCode.INTERNAL_ERROR.value
        assert es.driver_doc()["status"] == "active"
        assert es.events() == []

    def test_status_change_for_an_unknown_driver_appends_no_event(
        self, repo, service, es
    ):
        """A 404 must not leave an event behind for a record that isn't there."""
        client = TestClient(_make_app(repo, service))

        resp = client.patch(
            "/api/ops/drivers/drv_missing",
            json={"status": "off_duty"},
            headers=_admin_headers(),
        )

        assert resp.status_code == 404
        assert es.events() == []

    def test_non_status_patch_is_unchanged(self, repo, service, es):
        """The existing contract for every other field still holds."""
        client = TestClient(_make_app(repo, service))

        resp = client.patch(
            f"/api/ops/drivers/{DRIVER}",
            json={"driver_name": "Renamed"},
            headers=_admin_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["driver_name"] == "Renamed"
        assert es.events() == []
        assert es.driver_doc()["status"] == "active"


# ---------------------------------------------------------------------------
# Presence is independent of duty status (R13.9)
# ---------------------------------------------------------------------------


class TestPresenceDoesNotTouchDutyStatus:
    """A connect or a disconnect writes presence, and only presence."""

    @pytest.mark.asyncio
    async def test_connect_then_disconnect_leaves_duty_status_at_its_last_value(
        self, es, service
    ):
        """R13.9: a disconnected driver retains the duty status last set."""
        await service.transition(
            TENANT,
            DRIVER,
            "on_break",
            actor_id=DRIVER,
            source="driver",
            event_timestamp="2026-02-03T08:00:00+00:00",
        )
        es.writes.clear()

        manager = DriverWSManager(es_service=es)
        websocket = _FakeWebSocket()
        await manager.connect_driver(websocket, DRIVER, TENANT)
        await manager.disconnect(websocket)

        # Every write the presence lifecycle made went to one index, on the
        # composite ``{tenant_id}:{driver_id}`` document id (R10.19).
        assert es.written_indices() == {DRIVER_PRESENCE_INDEX}
        presence = es.docs[DRIVER_PRESENCE_INDEX]
        assert set(presence) == {presence_doc_id(TENANT, DRIVER)}
        assert presence[presence_doc_id(TENANT, DRIVER)]["status"] == "offline"

        # The duty status is untouched — both in the projection and on the read.
        assert es.driver_doc()["status"] == "on_break"
        assert await service.current(TENANT, DRIVER) == "on_break"

    @pytest.mark.asyncio
    async def test_presence_offline_does_not_append_a_duty_status_event(
        self, es, service
    ):
        """Presence is not a transition: no event is appended for it."""
        manager = DriverWSManager(es_service=es)

        await manager.update_presence(DRIVER, "offline", tenant_id=TENANT)

        assert es.events() == []
        assert es.driver_doc()["status"] == "active"
