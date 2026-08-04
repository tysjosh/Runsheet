"""
Unit tests for the Duty_Status router (``driver/api/duty_status_endpoints.py``).

The router is wired to a **real** :class:`DutyStatusService` over fakes, so the
assertions cover the whole path rather than a mock's recorded call: the append to
``duty_status_events``, the projection onto the ``drivers_current`` record, and
the range query the history read issues.

The sharp assertions here are about *scope*. The write has no ``driver_id``
anywhere on its surface — the subject and the actor are both the verified
session's driver (R13.1) — and the read is role-scoped: a ``dispatcher`` or
``admin`` may name any driver (R13.20), while a ``driver``-role caller naming a
different one is 403 ``FORBIDDEN`` rather than 404 (R13.21).

Validates: Requirements 13.1, 13.20, 13.21
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.duty_status_endpoints import (
    configure_duty_status_endpoints,
    router as duty_status_router,
)
from driver.services.driver_es_mappings import DUTY_STATUS_EVENTS_INDEX
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"
DRIVER = "drv_1"
OTHER_DRIVER = "drv_2"

RANGE_START = "2026-05-01T00:00:00+00:00"
RANGE_END = "2026-05-02T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _event(
    *,
    event_id: str,
    new_status: str,
    event_timestamp: str,
    driver_id: str = DRIVER,
    tenant_id: str = TENANT,
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "driver_id": driver_id,
        "previous_status": None,
        "new_status": new_status,
        "event_timestamp": event_timestamp,
        "server_received_at": event_timestamp,
        "actor_id": driver_id,
        "source": "driver",
        "reason": None,
    }


class FakeES:
    """Records appends and answers the ``duty_status_events`` searches."""

    def __init__(self, *, events: Optional[List[dict]] = None) -> None:
        self._events = list(events or [])
        self.indexed: List[tuple] = []
        self.searches: List[tuple] = []

    async def index_document(self, index, doc_id, document):
        self.indexed.append((index, doc_id, dict(document)))
        return {"result": "created"}

    async def search_documents(self, index, query, size=100):
        self.searches.append((index, query, size))
        if index != DUTY_STATUS_EVENTS_INDEX:
            return {"hits": {"hits": []}}
        return {"hits": {"hits": [{"_source": e} for e in self._events]}}

    async def update_document(self, index, doc_id, updates):  # pragma: no cover
        return {"result": "updated"}


class FakeDriverRepository:
    """``drivers_current`` read + projection write."""

    def __init__(self, *, record: Optional[dict] = None) -> None:
        self.record = record
        self.updates: List[dict] = []

    async def get(self, tenant_id, driver_id):
        if self.record is None:
            return None
        if self.record.get("driver_id") != driver_id:
            return None
        return dict(self.record)

    async def update(self, tenant_id, driver_id, updates):
        self.updates.append(dict(updates))
        if self.record is None:
            return None
        self.record.update(updates)
        return dict(self.record)


class FakeOrderRepository:
    """Answers the R13.6 "any assigned order in ``in_transit``?" read."""

    def __init__(self, *, orders: Optional[List[dict]] = None) -> None:
        self._orders = list(orders or [])

    async def search_for_driver(
        self, tenant_id, driver_id, *, statuses=(), page=1, size=1
    ):
        return {"orders": list(self._orders), "total": len(self._orders)}


def _driver_record(status: str = "off_duty") -> dict:
    return {
        "driver_id": DRIVER,
        "tenant_id": TENANT,
        "driver_name": "Ada Driver",
        "status": status,
    }


def _make_app(
    *,
    es_service: Any = None,
    driver_repository: Any = None,
    order_repository: Any = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(duty_status_router)
    configure_duty_status_endpoints(
        es_service=es_service,
        driver_repository=driver_repository,
        order_repository=order_repository,
    )
    install_test_auth(app)
    return app


def _driver_headers(driver_id: str = DRIVER, **kwargs) -> dict:
    kwargs.setdefault("roles", ["driver"])
    return auth_headers(TENANT, sub="user-1", driver_id=driver_id, **kwargs)


# ---------------------------------------------------------------------------
# POST /api/driver/duty-status
# ---------------------------------------------------------------------------


class TestSetDutyStatus:
    """The driver's own duty-status transition."""

    def test_accepts_active_and_appends_one_event(self):
        """Validates: Requirements 13.1"""
        es = FakeES()
        repo = FakeDriverRepository(record=_driver_record())
        client = TestClient(
            _make_app(es_service=es, driver_repository=repo)
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "active", "event_timestamp": RANGE_START},
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["new_status"] == "active"
        assert data["previous_status"] == "off_duty"
        assert data["source"] == "driver"
        assert data["actor_id"] == DRIVER
        assert data["driver_id"] == DRIVER
        assert data["projection_applied"] is True
        assert "request_id" in resp.json()

        # Exactly one append, on the event log, under the composite id.
        assert len(es.indexed) == 1
        index, doc_id, doc = es.indexed[0]
        assert index == DUTY_STATUS_EVENTS_INDEX
        assert doc_id.startswith(f"{TENANT}:{DRIVER}:")
        assert doc["event_timestamp"] == RANGE_START
        # The projection followed the append.
        assert repo.updates[0]["status"] == "active"

    def test_on_break_is_accepted(self):
        """Validates: Requirements 13.1"""
        es = FakeES()
        client = TestClient(
            _make_app(
                es_service=es,
                driver_repository=FakeDriverRepository(record=_driver_record()),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "on_break"},
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["new_status"] == "on_break"

    def test_off_duty_is_accepted_without_a_delivery_in_progress(self):
        """Validates: Requirements 13.1"""
        es = FakeES()
        client = TestClient(
            _make_app(
                es_service=es,
                driver_repository=FakeDriverRepository(
                    record=_driver_record("active")
                ),
                order_repository=FakeOrderRepository(orders=[]),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "off_duty"},
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["new_status"] == "off_duty"

    def test_driver_submitted_inactive_is_forbidden(self):
        """``inactive`` stays an administrator-set value.

        Validates: Requirements 13.1
        """
        client = TestClient(
            _make_app(
                es_service=FakeES(),
                driver_repository=FakeDriverRepository(record=_driver_record()),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "inactive"},
            headers=_driver_headers(),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"

    def test_unknown_status_is_rejected(self):
        """Validates: Requirements 13.1"""
        client = TestClient(
            _make_app(
                es_service=FakeES(),
                driver_repository=FakeDriverRepository(record=_driver_record()),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "on_duty"},
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"

    def test_body_driver_id_is_rejected_outright(self):
        """The write surface carries no ``driver_id`` at all.

        Validates: Requirements 13.1
        """
        es = FakeES()
        client = TestClient(
            _make_app(
                es_service=es,
                driver_repository=FakeDriverRepository(record=_driver_record()),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "active", "driver_id": OTHER_DRIVER},
            headers=_driver_headers(),
        )

        assert resp.status_code == 422
        assert es.indexed == []

    def test_non_driver_role_cannot_write(self):
        """Validates: Requirements 13.1"""
        client = TestClient(
            _make_app(
                es_service=FakeES(),
                driver_repository=FakeDriverRepository(record=_driver_record()),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "active"},
            headers=auth_headers(TENANT, roles=["dispatcher"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_missing_driver_identity_is_rejected(self):
        """Validates: Requirements 13.1"""
        client = TestClient(
            _make_app(
                es_service=FakeES(),
                driver_repository=FakeDriverRepository(record=_driver_record()),
            )
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "active"},
            headers=auth_headers(TENANT, roles=["driver"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"


# ---------------------------------------------------------------------------
# GET /api/driver/duty-status/history
# ---------------------------------------------------------------------------


def _history_url(*, driver_id: Optional[str] = None) -> str:
    url = (
        "/api/driver/duty-status/history"
        f"?range_start={RANGE_START.replace('+', '%2B')}"
        f"&range_end={RANGE_END.replace('+', '%2B')}"
    )
    if driver_id is not None:
        url += f"&driver_id={driver_id}"
    return url


_EVENTS = [
    _event(
        event_id="e1",
        new_status="active",
        event_timestamp="2026-05-01T06:00:00+00:00",
    ),
    _event(
        event_id="e2",
        new_status="on_break",
        event_timestamp="2026-05-01T10:00:00+00:00",
    ),
]


class TestDutyStatusHistory:
    """The role-scoped history read."""

    def test_dispatcher_reads_a_named_driver_sorted_ascending(self):
        """Validates: Requirements 13.20"""
        es = FakeES(events=_EVENTS)
        client = TestClient(_make_app(es_service=es))

        resp = client.get(
            _history_url(driver_id=DRIVER),
            headers=auth_headers(TENANT, roles=["dispatcher"]),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["driver_id"] == DRIVER
        assert body["count"] == 2
        assert [e["event_id"] for e in body["data"]] == ["e1", "e2"]

        index, query, _size = es.searches[0]
        assert index == DUTY_STATUS_EVENTS_INDEX
        assert query["sort"] == [{"event_timestamp": {"order": "asc"}}]
        # ``inject_tenant_filter`` nests the caller's query under ``must`` and
        # adds the tenant term alongside it.
        assert {"term": {"tenant_id": TENANT}} in query["query"]["bool"]["filter"]
        inner = query["query"]["bool"]["must"][0]["bool"]["filter"]
        assert {"term": {"driver_id": DRIVER}} in inner
        assert any("range" in clause for clause in inner)

    def test_admin_may_read_another_driver(self):
        """Validates: Requirements 13.20"""
        client = TestClient(_make_app(es_service=FakeES(events=_EVENTS)))

        resp = client.get(
            _history_url(driver_id=DRIVER),
            headers=auth_headers(TENANT, roles=["admin"]),
        )

        assert resp.status_code == 200
        assert resp.json()["driver_id"] == DRIVER

    def test_driver_reads_its_own_history_without_naming_itself(self):
        """Validates: Requirements 13.21"""
        client = TestClient(_make_app(es_service=FakeES(events=_EVENTS)))

        resp = client.get(_history_url(), headers=_driver_headers())

        assert resp.status_code == 200
        assert resp.json()["driver_id"] == DRIVER

    def test_driver_may_name_itself(self):
        """Validates: Requirements 13.21"""
        client = TestClient(_make_app(es_service=FakeES(events=_EVENTS)))

        resp = client.get(
            _history_url(driver_id=DRIVER), headers=_driver_headers()
        )

        assert resp.status_code == 200
        assert resp.json()["driver_id"] == DRIVER

    def test_driver_naming_another_driver_is_forbidden(self):
        """Never a 404 — that would confirm the other driver exists.

        Validates: Requirements 13.21
        """
        es = FakeES(events=_EVENTS)
        client = TestClient(_make_app(es_service=es))

        resp = client.get(
            _history_url(driver_id=OTHER_DRIVER), headers=_driver_headers()
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"
        # No read was issued for the other driver.
        assert es.searches == []

    def test_caller_without_any_of_the_three_roles_is_rejected(self):
        """Validates: Requirements 13.20, 13.21"""
        client = TestClient(_make_app(es_service=FakeES(events=_EVENTS)))

        resp = client.get(
            _history_url(driver_id=DRIVER),
            headers=auth_headers(TENANT, roles=["viewer"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_dispatcher_must_name_a_driver(self):
        """A dispatcher has no ``driver_id`` claim of its own to fall back to.

        Validates: Requirements 13.20
        """
        client = TestClient(_make_app(es_service=FakeES(events=_EVENTS)))

        resp = client.get(
            _history_url(), headers=auth_headers(TENANT, roles=["dispatcher"])
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"

    def test_inverted_range_is_rejected(self):
        """Validates: Requirements 13.20"""
        client = TestClient(_make_app(es_service=FakeES(events=_EVENTS)))

        resp = client.get(
            "/api/driver/duty-status/history"
            f"?range_start={RANGE_END.replace('+', '%2B')}"
            f"&range_end={RANGE_START.replace('+', '%2B')}",
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"

    def test_another_tenants_event_is_dropped(self):
        """Validates: Requirements 13.20"""
        leaked = _event(
            event_id="e9",
            new_status="active",
            event_timestamp="2026-05-01T07:00:00+00:00",
            tenant_id="t2",
        )
        client = TestClient(
            _make_app(es_service=FakeES(events=[*_EVENTS, leaked]))
        )

        resp = client.get(_history_url(), headers=_driver_headers())

        assert resp.status_code == 200
        assert [e["event_id"] for e in resp.json()["data"]] == ["e1", "e2"]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    """``configure_duty_status_endpoints`` fails closed when unwired."""

    def test_unconfigured_router_returns_a_structured_error(self):
        client = TestClient(
            _make_app(es_service=None), raise_server_exceptions=False
        )

        resp = client.post(
            "/api/driver/duty-status",
            json={"status": "active"},
            headers=_driver_headers(),
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"

    def test_write_path_carries_no_driver_id_parameter(self):
        """R13.1 as a surface property: the write cannot name another driver."""
        for route in duty_status_router.routes:
            if "POST" not in getattr(route, "methods", set()):
                continue
            assert "driver_id" not in route.path
            params = {p.name for p in getattr(route, "dependant").query_params}
            assert "driver_id" not in params
