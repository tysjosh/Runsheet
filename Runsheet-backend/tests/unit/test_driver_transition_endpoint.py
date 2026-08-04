"""
Unit tests for ``POST /api/driver/orders/{order_id}/status``.

The handler is wired to a **real** :class:`OrderService` over a fake repository
so the assertions cover the whole path the requirements name — the state-machine
guard, the delivery-window guard, the appended order event, and the three R4.8
stamps — rather than a mock's recorded call.

The ordering assertions are the sharp ones. The equal-target-status
short-circuit has to sit before ``apply_status_transition`` (which rejects
``X → X``) and before the gate stack (a no-op changes nothing), and it must
append no event.

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.8, 4.9, 4.10, 4.11
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any driver imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import driver.middleware.idempotency as idempotency_module
from driver.api.transition_endpoints import router as transition_router
from driver.middleware.idempotency import configure_idempotency_middleware
from driver.services.driver_es_mappings import IDEMPOTENCY_KEYS_INDEX
from driver.services.order_transition_service import (
    configure_transition_endpoints,
)
from errors.handlers import register_exception_handlers
from fuel.services.order_service import OrderService
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT_ID = "t1"
DRIVER_ID = "drv_1"
OTHER_DRIVER_ID = "drv_2"
ORDER_ID = "ord_00000000000000000000000000000001"

_FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
_CLIENT_STAMP = "2026-05-10T11:47:03+00:00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _headers(**extra) -> dict:
    headers = auth_headers(
        TENANT_ID, sub=DRIVER_ID, roles=["driver"], driver_id=DRIVER_ID
    )
    headers.update(extra)
    return headers


def _order_doc(
    *,
    status: str = "dispatched",
    assigned_driver_id: Optional[str] = DRIVER_ID,
    tenant_id: str = TENANT_ID,
    with_window: bool = True,
) -> Dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "tenant_id": tenant_id,
        "status": status,
        "assigned_driver_id": assigned_driver_id,
        "assigned_asset_id": "ast_1",
        "delivery_window_start": "2026-05-11T08:00:00+00:00" if with_window else None,
        "delivery_window_end": "2026-05-11T12:00:00+00:00" if with_window else None,
        "source_schema_version": "1.0",
        "trace_id": "trace_001",
    }


class FakeOrderRepository:
    """Minimal ``FuelOrderRepository`` surface: read, append event, upsert."""

    def __init__(self, order: Optional[dict]) -> None:
        self._order = order
        self.events: List[dict] = []
        self.upserts: List[dict] = []

    async def get(self, tenant_id: str, order_id: str):
        if self._order is None:
            return None
        if self._order.get("order_id") != order_id:
            return None
        return dict(self._order)

    async def append_event(self, tenant_id: str, event: dict) -> None:
        self.events.append(event)

    async def upsert_with_last_event_timestamp(
        self, tenant_id: str, order: dict
    ) -> bool:
        self.upserts.append(dict(order))
        return True


class FakeQualificationService:
    """``Dispatch_Eligibility`` verdict source for the third gate."""

    def __init__(self, eligible: bool = True) -> None:
        self._eligible = eligible
        self.calls = 0

    async def is_dispatch_eligible(self, tenant_id: str, driver_id: str):
        self.calls += 1
        return {
            "eligible": self._eligible,
            "reasons": [] if self._eligible else ["license_expired"],
        }


class FakeIdempotencyES:
    """In-memory stand-in for the ``idempotency_keys`` index."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def index_document(self, index: str, doc_id: str, document: dict) -> None:
        assert index == IDEMPOTENCY_KEYS_INDEX
        self.docs[doc_id] = document

    async def get_document(self, index: str, doc_id: str):
        assert index == IDEMPOTENCY_KEYS_INDEX
        return self.docs.get(doc_id)


@pytest.fixture
def idempotency_store():
    previous = idempotency_module.get_idempotency_middleware()
    store = FakeIdempotencyES()
    configure_idempotency_middleware(es_service=store)
    try:
        yield store
    finally:
        idempotency_module._idempotency_middleware = previous


def _build_app(
    *,
    order: Optional[dict],
    qualification_service: Optional[Any] = None,
) -> tuple[FastAPI, FakeOrderRepository]:
    repo = FakeOrderRepository(order)
    ws_manager = AsyncMock()
    ws_manager.broadcast = AsyncMock(return_value=1)
    order_service = OrderService(
        order_repo=repo,
        ws_manager=ws_manager,
        feature_flag_service=None,
        clock=lambda: _FIXED_NOW,
    )

    configure_transition_endpoints(
        order_repository=repo,
        order_service=order_service,
        driver_qualification_service=qualification_service
        or FakeQualificationService(),
        inspection_service=None,
        feature_flag_service=None,
        hos_advisory_service=None,
    )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(transition_router)
    install_test_auth(app)
    return app, repo


def _post(app: FastAPI, body: dict, **header_extra):
    return TestClient(app).post(
        f"/api/driver/orders/{ORDER_ID}/status",
        json=body,
        headers=_headers(**header_extra),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransitionSucceeds:
    """The happy path — one transition through ``apply_status_transition``."""

    def test_dispatched_to_in_transit_applies_and_appends_one_event(self):
        """The state machine, the window guard, and the event append all run.

        Validates: Requirements 4.1, 4.9
        """
        app, repo = _build_app(order=_order_doc(status="dispatched"))

        resp = _post(app, {"status": "in_transit", "event_timestamp": _CLIENT_STAMP})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status_changed"] is True
        assert body["data"]["status"] == "in_transit"
        assert len(repo.events) == 1
        assert repo.upserts and repo.upserts[-1]["status"] == "in_transit"

        event = repo.events[0]
        assert event["event_type"] == "order_in_transit"
        assert event["event_payload"]["old_status"] == "dispatched"
        assert event["event_payload"]["new_status"] == "in_transit"

    def test_event_carries_all_three_r4_8_stamps(self):
        """Acting driver, client stamp, and server receipt stamp.

        Validates: Requirement 4.8
        """
        app, repo = _build_app(order=_order_doc(status="dispatched"))

        resp = _post(app, {"status": "in_transit", "event_timestamp": _CLIENT_STAMP})

        assert resp.status_code == 200, resp.text
        event = repo.events[0]
        assert event["event_payload"]["actor_user_id"] == DRIVER_ID
        assert event["event_payload"]["client_event_timestamp"] == _CLIENT_STAMP
        assert event["event_timestamp"] == _FIXED_NOW
        assert event["ingested_at"] == _FIXED_NOW

    def test_gate_stack_runs_before_the_transition(self):
        """A transition to ``in_transit`` consults ``Dispatch_Eligibility``."""
        qualification = FakeQualificationService(eligible=True)
        app, _ = _build_app(
            order=_order_doc(status="dispatched"),
            qualification_service=qualification,
        )

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 200, resp.text
        assert qualification.calls == 1


class TestAuthorization:
    """Order resolution — R4.2 and its 404 sibling."""

    def test_another_drivers_order_is_403_forbidden(self):
        """Validates: Requirement 4.2"""
        app, repo = _build_app(
            order=_order_doc(assigned_driver_id=OTHER_DRIVER_ID)
        )

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 403, resp.text
        assert resp.json()["error_code"] == "FORBIDDEN"
        assert repo.events == []

    def test_absent_order_is_404(self):
        app, _ = _build_app(order=None)

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 404, resp.text


class TestGuards:
    """The two state-machine guards, reached unchanged through this handler."""

    def test_illegal_transition_is_409_with_both_statuses(self):
        """Validates: Requirement 4.3"""
        app, repo = _build_app(order=_order_doc(status="placed"))

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 409, resp.text
        error = resp.json()
        assert error["error_code"] == "INVALID_STATUS_TRANSITION"
        assert error["details"]["old_status"] == "placed"
        assert error["details"]["new_status"] == "in_transit"
        assert repo.events == []

    def test_missing_delivery_window_is_409(self):
        """Validates: Requirement 4.4"""
        app, repo = _build_app(
            order=_order_doc(status="dispatched", with_window=False)
        )

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "MISSING_DELIVERY_WINDOW"
        assert repo.events == []

    def test_ineligible_driver_is_409_from_the_gate_stack(self):
        app, repo = _build_app(
            order=_order_doc(status="dispatched"),
            qualification_service=FakeQualificationService(eligible=False),
        )

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 409, resp.text
        assert resp.json()["error_code"] == "DRIVER_NOT_DISPATCH_ELIGIBLE"
        assert repo.events == []

    def test_unknown_status_is_rejected_as_a_malformed_request(self):
        app, repo = _build_app(order=_order_doc(status="dispatched"))

        resp = _post(app, {"status": "teleported"})

        assert resp.status_code == 400, resp.text
        assert repo.events == []


class TestEqualTargetStatusShortCircuit:
    """R4.10 — the no-op, decided before both the gates and the transition."""

    def test_equal_status_returns_200_with_the_unchanged_order(self):
        """Validates: Requirement 4.10"""
        app, repo = _build_app(order=_order_doc(status="in_transit"))

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status_changed"] is False
        assert body["data"]["status"] == "in_transit"
        assert repo.events == []
        assert repo.upserts == []

    def test_equal_status_bypasses_the_gate_stack(self):
        """A no-op changes nothing, so no gate has anything to protect.

        An ineligible driver still gets 200 here, which is only possible if the
        short-circuit precedes the gate stack.

        Validates: Requirement 4.10
        """
        qualification = FakeQualificationService(eligible=False)
        app, repo = _build_app(
            order=_order_doc(status="in_transit"),
            qualification_service=qualification,
        )

        resp = _post(app, {"status": "in_transit"})

        assert resp.status_code == 200, resp.text
        assert qualification.calls == 0
        assert repo.events == []

    def test_equal_terminal_status_is_a_no_op_not_a_409(self):
        """``delivered → delivered`` has no entry in the transition table."""
        app, repo = _build_app(order=_order_doc(status="delivered"))

        resp = _post(app, {"status": "delivered"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["status_changed"] is False
        assert repo.events == []


class TestIdempotency:
    """R4.11 — ``X-Idempotency-Key`` on every driver-initiated transition."""

    def test_repeated_key_replays_the_stored_response(self, idempotency_store):
        """Validates: Requirement 4.11"""
        app, repo = _build_app(order=_order_doc(status="dispatched"))
        headers = {"X-Idempotency-Key": "idem-transition-1"}

        first = _post(app, {"status": "in_transit"}, **headers)
        assert first.status_code == 200, first.text
        assert idempotency_store.docs

        second = _post(app, {"status": "in_transit"}, **headers)

        assert second.status_code == 200, second.text
        assert second.headers.get("X-Idempotent-Replayed") == "true"
        assert second.json() == first.json()
        # The replay never reaches the writer, so only one event exists.
        assert len(repo.events) == 1

    def test_no_op_response_is_also_stored_for_replay(self, idempotency_store):
        """Validates: Requirements 4.10, 4.11"""
        app, _ = _build_app(order=_order_doc(status="in_transit"))
        headers = {"X-Idempotency-Key": "idem-transition-noop"}

        first = _post(app, {"status": "in_transit"}, **headers)
        second = _post(app, {"status": "in_transit"}, **headers)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.headers.get("X-Idempotent-Replayed") == "true"
        assert second.json()["status_changed"] is False
