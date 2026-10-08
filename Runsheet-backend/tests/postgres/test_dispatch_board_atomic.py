"""Dispatch Board commands on a real PostgreSQL (plan task 11; design K4.2, K4.5, K5.1).

* Two concurrent commands on one lane from the same version: one commits, the
  other gets 409 ``BOARD_LANE_CONFLICT`` (I2, P6 on the real row lock).
* Two concurrent requests with one ``client_command_id``: one draft commit and
  exactly one committed log document (I13).
* The board's own queries run on the real translator: the cross-day
  ``terms order_ids`` lookup (K5.1, the same lookup the order listener uses in
  Phase 3), the order-tray query and the history query.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, Dict, Optional

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.dispatch_board_models import CommandBody, HistoryQuery
from fuel.services.dispatch_board_service import DispatchBoardService
from fuel.services.dispatch_board_telemetry import BoardTelemetry
from fuel.services.dispatch_validation import DispatchValidationService, TTLCache, ValidationContext
from tests.postgres.test_loading_plan_executor_postgres import _Namespaced
from tests.unit._dispatch_board_fakes import (
    NOW,
    TODAY,
    TOMORROW,
    TZ,
    FakeCertification,
    FakeDriverRepository,
    FakeDyed,
    FakeHOS,
    FakeQualification,
    SpyTelemetry,
    compartment,
    driver,
    order,
)

pytestmark = pytest.mark.asyncio

DRAFTS = "dispatch_board_drafts"
COMMANDS = "dispatch_board_commands"


class _BoardNs(_Namespaced):
    """``_Namespaced`` plus ``create_document`` (the board's log writes)."""

    def __getattr__(self, name: str) -> Any:
        if name == "create_document":
            inner = getattr(self._inner, name)

            async def call(index: str, doc_id: str, document: Dict[str, Any]) -> bool:
                return await inner(self._prefix + index, doc_id, document)

            return call
        return super().__getattr__(name)


class _Orders:
    def __init__(self, es) -> None:
        self.es = es

    async def get_current(self, tenant_id: str, order_id: str) -> Optional[Dict[str, Any]]:
        doc = await self.es.get_document("fuel_orders_current", order_id)
        return doc if doc and doc.get("tenant_id") == tenant_id else None


@pytest.fixture
def tenant() -> str:
    return f"pytest-board-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def es(store, index_name):
    return _BoardNs(store, f"{index_name}__")


async def _service(es, tenant):
    for truck in ("T1", "T2"):
        await es.index_document("truck_compartments", f"{tenant}-{truck}-C1", compartment(truck, "C1", tenant_id=tenant))
    validation = DispatchValidationService(
        es_service=es,
        order_repository=_Orders(es),
        driver_repository=FakeDriverRepository([driver("d1", tenant_id=tenant), driver("d2", tenant_id=tenant)]),
        qualification_service=FakeQualification(),
        hos_advisory_service=FakeHOS(),
        asset_certification_service=FakeCertification(),
        dyed_diesel_enforcer=FakeDyed(),
        cache=TTLCache(),
        clock=lambda: NOW,
    )
    return DispatchBoardService(
        es_service=es,
        validation=validation,
        telemetry=BoardTelemetry(telemetry=SpyTelemetry()),
        name_lookup=lambda t, u: asyncio.sleep(0, result=None),
        clock=lambda: NOW,
        log_read_delay_s=0.01,
    )


def _cmd(type_: str, versions: Dict[str, int], cid: Optional[str] = None, **fields):
    return CommandBody.model_validate(
        {"type": type_, "client_command_id": cid or str(uuid.uuid4()), "expected_lane_versions": versions, **fields}
    ).root


async def _send(svc, tenant, command, day=TODAY):
    return await svc.handle_command(tenant_id=tenant, user_id="user-1", service_date=day, command=command, tz=TZ)


async def _committed_logs(es, tenant):
    resp = await es.search_documents(
        COMMANDS, {"query": {"bool": {"filter": [{"term": {"tenant_id": tenant}}, {"term": {"result": "committed"}}]}}, "size": 50}, 50
    )
    return [h["_source"] for h in resp["hits"]["hits"]]


async def test_same_lane_race_one_commits(es, tenant):
    svc = await _service(es, tenant)
    await _send(svc, tenant, _cmd("add_lane", {"T1": 0}, truck_id="T1"))
    a = _cmd("pair_driver", {"T1": 1}, truck_id="T1", driver_id="d1")
    b = _cmd("pair_driver", {"T1": 1}, truck_id="T1", driver_id="d2")
    results = await asyncio.gather(_send(svc, tenant, a), _send(svc, tenant, b), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errors) == 1
    assert isinstance(errors[0], AppException) and errors[0].error_code == ErrorCode.BOARD_LANE_CONFLICT
    draft = await es.get_document(DRAFTS, f"{tenant}:{TODAY.isoformat()}")
    assert draft["draft_version"] == 2 and draft["lanes"]["T1"]["version"] == 2
    assert draft["lanes"]["T1"]["driver_id"] == ok[0]["lanes"][0]["driver_id"]


async def test_same_id_race_one_commit_one_log(es, tenant):
    svc = await _service(es, tenant)
    command = _cmd("add_lane", {"T1": 0}, truck_id="T1")
    results = await asyncio.gather(*(_send(svc, tenant, command) for _ in range(3)), return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)], results
    draft = await es.get_document(DRAFTS, f"{tenant}:{TODAY.isoformat()}")
    assert draft["draft_version"] == 1 and list(draft["applied_commands"]) == [command.client_command_id]
    logs = await _committed_logs(es, tenant)
    assert len(logs) == 1
    for response in results:
        if response.get("already_applied"):
            assert [l["version"] for l in response["lanes"]] == [l["version"] for l in logs[0]["response"]["lanes"]]
        else:
            assert response == logs[0]["response"]


async def test_cross_day_terms_lookup_on_the_real_translator(es, tenant):
    svc = await _service(es, tenant)
    await es.index_document("fuel_orders_current", "o1", order("o1", tenant_id=tenant, day=TOMORROW))
    await es.index_document("fuel_orders_current", "o2", order("o2", tenant_id=tenant))
    await _send(svc, tenant, _cmd("add_lane", {"T1": 0}, truck_id="T1"), day=TOMORROW)
    await _send(svc, tenant, _cmd("assign_orders", {"T1": 1}, order_ids=["o1"], truck_id="T1"), day=TOMORROW)
    ctx = ValidationContext(tenant_id=tenant, service_date=TODAY, timezone=TZ, now=NOW, today=TODAY)
    await svc.validation._fetch_other_day(ctx, ["o1", "o2"])
    assert ctx.other_day == {"o1": TOMORROW}
    # The draft for the day itself is never reported.
    ctx2 = ValidationContext(tenant_id=tenant, service_date=TOMORROW, timezone=TZ, now=NOW, today=TODAY)
    await svc.validation._fetch_other_day(ctx2, ["o1"])
    assert ctx2.other_day == {}
    # Another tenant never sees it.
    ctx3 = ValidationContext(tenant_id="pytest-board-other", service_date=TODAY, timezone=TZ, now=NOW, today=TODAY)
    await svc.validation._fetch_other_day(ctx3, ["o1"])
    assert ctx3.other_day == {}


async def test_order_listener_lookup_on_the_real_translator(es, tenant):
    """K10.4: ``term order_ids`` inside the keyword array, ``service_date >= today``,
    ``_source`` filtering; the lane is marked stale with no version bump."""
    from fuel.services.dispatch_board_order_listener import BoardOrderListener

    svc = await _service(es, tenant)
    await es.index_document("fuel_orders_current", "o1", order("o1", tenant_id=tenant))
    await es.index_document("fuel_orders_current", "o2", order("o2", tenant_id=tenant, day=TOMORROW))
    for day, oid in ((TODAY, "o1"), (TOMORROW, "o2")):
        await _send(svc, tenant, _cmd("add_lane", {"T1": 0}, truck_id="T1"), day=day)
        await _send(svc, tenant, _cmd("assign_orders", {"T1": 1}, order_ids=[oid], truck_id="T1"), day=day)
    events = []

    async def broadcast(tenant_id, service_date, event_type, data):
        events.append((service_date, data["truck_ids"]))

    listener = BoardOrderListener(es_service=es, broadcast=broadcast, timezone_for=lambda t: TZ, clock=lambda: NOW)
    assert await listener.on_order_event({"tenant_id": tenant, "order_id": "o2"}, "cancelled") == [f"{TOMORROW.isoformat()}:T1"]
    assert events == [(TOMORROW, ["T1"])]
    tomorrow = await es.get_document(DRAFTS, f"{tenant}:{TOMORROW.isoformat()}")
    today = await es.get_document(DRAFTS, f"{tenant}:{TODAY.isoformat()}")
    assert tomorrow["lanes"]["T1"]["checks_stale"] is True and tomorrow["lanes"]["T1"]["version"] == 2
    assert tomorrow["draft_version"] == 2
    assert today["lanes"]["T1"]["checks_stale"] is False
    # Another tenant's event with the same order id finds nothing.
    assert await listener.on_order_event({"tenant_id": "pytest-board-other", "order_id": "o1"}, "cancelled") == []
    assert await listener.on_order_event({"tenant_id": tenant, "order_id": "o9"}, "cancelled") == []


async def test_tray_and_history_queries_on_the_real_translator(es, tenant):
    svc = await _service(es, tenant)
    await es.index_document("fuel_orders_current", "a", order("a", tenant_id=tenant, delivery_window_start=f"{TODAY}T15:00:00+00:00"))
    await es.index_document("fuel_orders_current", "b", order("b", tenant_id=tenant, delivery_window_start=f"{TODAY}T14:00:00+00:00"))
    await es.index_document("fuel_orders_current", "w", order("w", tenant_id=tenant, delivery_window_start=None, delivery_window_end=None))
    await es.index_document("fuel_orders_current", "x", order("x", tenant_id=tenant, day=TODAY + timedelta(days=4)))
    await es.index_document("fuel_orders_current", "h", order("h", tenant_id=tenant, status="on_hold"))
    snap = await svc.snapshot(tenant, TODAY, mode="active_gated", tz=TZ)
    # Window start ascending, no window last; "h" (on hold, 13:00) stays in the tray.
    assert [o["order_id"] for o in snap["trays"]["orders"]] == ["h", "b", "a", "w"]
    assert next(o for o in snap["trays"]["orders"] if o["order_id"] == "h")["draggable"] is False
    for truck in ("T1", "T2"):
        await _send(svc, tenant, _cmd("add_lane", {truck: 0}, truck_id=truck))
    page = await svc.history(tenant, TODAY, HistoryQuery(size=1), tz=TZ)
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = await svc.history(tenant, TODAY, HistoryQuery(size=5, cursor=page["next_cursor"]), tz=TZ)
    assert len(rest["items"]) == 1 and rest["items"][0]["command_id"] != page["items"][0]["command_id"]
    t2 = await svc.history(tenant, TODAY, HistoryQuery(truck_id="T2"), tz=TZ)
    assert [i["lanes"][0]["truck_id"] for i in t2["items"]] == ["T2"]


async def test_truck_type_and_dq_lookups_on_the_real_translator(es, tenant):
    """Plan task 36b: the ``should`` of two ``terms`` with ``minimum_should_match``
    (R2.8) and the ``_source``-filtered DQ read (R3.4) on Postgres."""
    svc = await _service(es, tenant)
    await es.index_document("trucks", f"{tenant}-a1", {"tenant_id": tenant, "asset_id": "T1", "asset_subtype": "tank_wagon"})
    await es.index_document("trucks", f"{tenant}-a2", {"tenant_id": tenant, "truck_id": "T2", "asset_subtype": "transport"})
    await es.index_document("trucks", f"{tenant}-a3", {"tenant_id": tenant, "asset_id": "T9", "asset_subtype": "bobtail"})
    await es.index_document("trucks", f"{tenant}-x", {"tenant_id": "pytest-board-other", "asset_id": "T1", "asset_subtype": "bobtail"})
    assert await svc._truck_types(tenant, ["T1", "T2", "T3"]) == {"T1": "tank_wagon", "T2": "transport"}
    await es.index_document("drivers", f"{tenant}-dq", {
        "tenant_id": tenant, "driver_id": f"driver_{tenant}", "external_refs": {"ops_driver_id": "d1"},
        "full_name": "PII never read", "cdl_expiry_date": "2027-05-01", "tanker_endorsement_expiry_date": "2027-01-01",
    })
    records = await svc._dq_records(tenant)
    assert set(records) == {f"driver_{tenant}", "d1"}
    assert "full_name" not in records["d1"] and records["d1"]["tanker_endorsement_expiry_date"] == "2027-01-01"
