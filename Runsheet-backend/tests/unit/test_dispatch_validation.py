"""DispatchValidationService (plan task 10; design K3.1-K3.4, K8.1; P3, P4).

``build_context`` is exercised through fakes for every source; the check
catalogue (K3.2) is exercised row by row on hand-built contexts, because
``validate_lane`` only reads the context.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from Agents.support.compartment_models import Compartment
from Agents.support.fuel_distribution_models import FuelGrade
from fuel.services import dispatch_board_engine as engine
from fuel.services.dispatch_board_models import (
    BoardDraft,
    CommandBody,
    CompartmentShare,
    DragItem,
    Lane,
    LaneContent,
    LanePublish,
    PublishedPlan,
    Target,
)
from fuel.services.dispatch_validation import (
    BLOCKING_UNAVAILABLE,
    DispatchValidationService,
    TTLCache,
    ValidationContext,
    _unavailable,
    candidate_results,
    lane_checks,
    warning_id_for,
    worst_outcome,
)
from tests.unit._dispatch_board_fakes import (
    COMPARTMENTS,
    DRAFTS,
    EXECUTIONS,
    NOW,
    T,
    TODAY,
    TOMORROW,
    TZ,
    BoardStore,
    FakeCertification,
    FakeDriverRepository,
    FakeDyed,
    FakeHOS,
    FakeOrderRepository,
    FakeQualification,
    compartment,
    driver,
    order,
)
from tests.unit.test_dispatch_board_engine import apply, cmd, empty, make_ctx, versions, with_lanes


def lane_with(ctx, *order_ids, truck="T1", driver_id="d1", target=None):
    d = with_lanes(ctx, truck)
    if order_ids:
        d = apply(d, cmd("assign_orders", versions(d, truck), order_ids=list(order_ids), truck_id=truck, target=target or {"load_id": "new"}), ctx).draft
    if driver_id:
        d.lanes[truck].driver_id = driver_id
    return d, d.lanes[truck]


def codes(checks, outcome=None):
    return [c.reason_code for c in checks if outcome is None or c.outcome == outcome]


def by_check(checks, name):
    return [c for c in checks if c.check == name]


# ---- build_context -------------------------------------------------------


def _service(store, **overrides):
    deps = dict(
        es_service=store,
        order_repository=FakeOrderRepository(store),
        driver_repository=FakeDriverRepository([driver("d1"), driver("dX", tenant_id="tenant-2")]),
        qualification_service=FakeQualification(),
        hos_advisory_service=FakeHOS(),
        asset_certification_service=FakeCertification(),
        dyed_diesel_enforcer=FakeDyed(),
        cache=TTLCache(),
        clock=lambda: NOW,
    )
    deps.update(overrides)
    return DispatchValidationService(**deps), deps


def _seed(store):
    store.seed("fuel_orders_current", "o1", order("o1"))
    store.seed("fuel_orders_current", "oX", order("oX", tenant_id="tenant-2"))
    store.seed(COMPARTMENTS, "T1_C1", compartment("T1", "C1"))
    store.seed(COMPARTMENTS, "TX_C1", compartment("TX", "C1", tenant_id="tenant-2"))


async def test_build_context_reads_every_source_tenant_scoped():
    store = BoardStore()
    _seed(store)
    store.seed(EXECUTIONS, "e1", {"tenant_id": T, "plan_id": "bp-L1-r1", "route_id": "br-L1-r1", "completed_stops": 2})
    store.seed(EXECUTIONS, "e2", {"tenant_id": "tenant-2", "plan_id": "bp-L1-r1", "completed_stops": 9})
    store.seed(DRAFTS, f"{T}:{TOMORROW}", {"tenant_id": T, "service_date": TOMORROW.isoformat(), "order_ids": ["o1"]})
    store.seed("terminals", "TERM1", {"tenant_id": T, "terminal_id": "TERM1", "location_lat": 41.7, "location_lon": -87.6})
    svc, deps = _service(store)
    ctx = await svc.build_context(
        T, TODAY, truck_ids=["T1", "TX"], driver_ids=["d1", "dX"], order_ids=["o1", "oX"],
        plan_ids=["bp-L1-r1"], terminal_ids=[], timezone_name=TZ,
    )
    assert set(ctx.orders) == {"o1"}
    assert set(ctx.drivers) == {"d1"}
    assert set(ctx.compartments) == {"T1"}
    assert ctx.executions == {"bp-L1-r1": 2}
    assert ctx.other_day == {"o1": TOMORROW}
    assert ctx.terminals == {"TERM1"} and ctx.terminal_locations["TERM1"] == {"lat": 41.7, "lon": -87.6}
    assert set(ctx.qualification) == {"d1"} and set(ctx.hos_verdicts) == {"d1"}
    assert ctx.order_locations["o1"] == {"lat": 41.88, "lon": -87.63}
    assert ctx.is_today and not ctx.unavailable
    # Qualification gets the route requirements of the context's orders (K3.2).
    _tenant, _driver, requirements = deps["qualification_service"].calls[0]
    assert requirements == {"requires_hazmat": True, "requires_tanker": True, "min_cdl_class": "A"}


async def test_build_context_runs_sources_in_parallel():
    store = BoardStore()
    _seed(store)

    class Slow(FakeCertification):
        async def is_dispatch_eligible(self, tenant_id, asset_id):
            await asyncio.sleep(0.15)
            return await super().is_dispatch_eligible(tenant_id, asset_id)

    class SlowRepo(FakeOrderRepository):
        async def get_current(self, tenant_id, order_id):
            await asyncio.sleep(0.15)
            return await super().get_current(tenant_id, order_id)

    svc, _ = _service(store, asset_certification_service=Slow(), order_repository=SlowRepo(store))
    started = time.monotonic()
    await svc.build_context(T, TODAY, truck_ids=["T1"], driver_ids=[], order_ids=["o1"], timezone_name=TZ)
    assert time.monotonic() - started < 0.28


async def test_hanging_source_times_out_and_blocks_or_warns(caplog):
    store = BoardStore()
    _seed(store)
    qual = FakeQualification()
    qual.hang = True
    svc, _ = _service(store, qualification_service=qual, source_timeout_s=0.05)
    caplog.set_level(logging.WARNING, logger="fuel.services.dispatch_validation")
    ctx = await svc.build_context(T, TODAY, truck_ids=["T1"], driver_ids=["d1"], order_ids=["o1"], timezone_name=TZ)
    assert ctx.unavailable == {"qualification"}
    warnings = [r.getMessage() for r in caplog.records if "source unavailable" in r.getMessage()]
    assert warnings == ["dispatch_board validation source unavailable: source=qualification error=TimeoutError"]
    lane = Lane(truck_id="T1", version=1, driver_id="d1")
    assert [(c.check, c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane), "driver_qualification")] == [
        ("driver_qualification", "block", "check_unavailable")
    ]


async def test_failing_source_logs_type_only(caplog):
    store = BoardStore()
    _seed(store)
    cert = FakeCertification()
    cert.fail = RuntimeError("connection to db-host-secret refused")
    svc, _ = _service(store, asset_certification_service=cert)
    caplog.set_level(logging.WARNING)
    ctx = await svc.build_context(T, TODAY, truck_ids=["T1"], driver_ids=[], order_ids=[], timezone_name=TZ)
    assert ctx.unavailable == {"certification"}
    assert "db-host-secret" not in caplog.text and "error=RuntimeError" in caplog.text


async def test_ttl_cache_and_fresh_bypass():
    store = BoardStore()
    _seed(store)
    clock = [0.0]
    cache = TTLCache(30.0, clock=lambda: clock[0])
    svc, deps = _service(store, cache=cache)
    qual, cert = deps["qualification_service"], deps["asset_certification_service"]
    kwargs = dict(truck_ids=["T1"], driver_ids=["d1"], order_ids=["o1"], timezone_name=TZ)
    await svc.build_context(T, TODAY, **kwargs)
    await svc.build_context(T, TODAY, **kwargs)
    assert len(qual.calls) == 1 and len(cert.calls) == 1
    await svc.build_context(T, TODAY, fresh=True, **kwargs)
    assert len(qual.calls) == 2 and len(cert.calls) == 2
    clock[0] = 31.0
    await svc.build_context(T, TODAY, **kwargs)
    assert len(qual.calls) == 3
    # The tenant is part of every key.
    await svc.build_context("tenant-2", TODAY, truck_ids=["T1"], driver_ids=[], order_ids=[], timezone_name=TZ)
    assert cert.calls[-1][0] == "tenant-2"


async def test_hos_is_today_only():
    store = BoardStore()
    _seed(store)
    svc, deps = _service(store)
    hos = deps["hos_advisory_service"]
    ctx = await svc.build_context(T, TOMORROW, truck_ids=["T1"], driver_ids=["d1"], order_ids=["o1"], timezone_name=TZ)
    assert hos.calls == [] and ctx.is_future
    lane = Lane(truck_id="T1", version=1, driver_id="d1")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane), "hos")] == [("info", "hos_not_projected")]


async def test_unknown_sources_mark_unavailable_when_not_configured():
    store = BoardStore()
    _seed(store)
    svc, _ = _service(store, order_repository=None, driver_repository=None)
    ctx = await svc.build_context(T, TODAY, truck_ids=[], driver_ids=["d1"], order_ids=["o1"], timezone_name=TZ)
    assert {"orders", "drivers"} <= ctx.unavailable


# ---- catalogue rows (K3.2) -----------------------------------------------


def test_order_state_rows():
    orders = [order(f"o{i}") for i in range(1, 9)]
    ctx = make_ctx(orders)
    d, lane = lane_with(ctx, *[o["order_id"] for o in orders])
    ctx.orders.pop("o1")  # vanished
    ctx.orders["o2"]["status"] = "on_hold"
    ctx.orders["o3"]["status"] = "cancelled"
    ctx.orders["o4"].update(status="scheduled", assigned_run_id="agent-run-9")
    ctx.orders["o5"]["product_code"] = "GASOLINE_REG"
    ctx.orders["o6"]["gallons_requested"] = 750.0
    ctx.other_day = {"o7": TOMORROW}
    rows = {c.scope.order_id: (c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "order_state")}
    assert rows == {
        "o1": ("block", "order_not_found"),
        "o2": ("block", "on_hold"),
        "o3": ("block", "order_terminal"),
        "o4": ("block", "order_committed_elsewhere"),
        "o5": ("block", "order_identity_changed"),
        "o6": ("info", "order_quantity_changed"),
        "o7": ("block", "order_on_other_day"),
    }
    msg = next(c for c in lane_checks(ctx, lane, draft=d) if c.reason_code == "order_on_other_day").message
    assert msg == "Order o7 is planned on Oct 9. Remove it there first."


def test_order_identity_covers_customer_and_tank():
    ctx = make_ctx([order("o1"), order("o2")])
    d, lane = lane_with(ctx, "o1", "o2")
    ctx.orders["o1"]["customer_id"] = "someone-else"
    ctx.orders["o2"]["customer_tank_id"] = "tank-new"
    assert codes(by_check(lane_checks(ctx, lane, draft=d), "order_state")) == ["order_identity_changed", "order_identity_changed"]


def test_order_linked_to_this_lanes_published_plan_is_fine():
    ctx = make_ctx([order("o1")])
    d, lane = lane_with(ctx, "o1")
    load_id = lane.loads[0].load_id
    lane.publish = LanePublish(state="published", plans={load_id: PublishedPlan(plan_id="bp-1", route_id="br-1", run_id="bp-1", revision=1)})
    ctx.orders["o1"].update(status="delivered", assigned_run_id="bp-1")
    assert by_check(lane_checks(ctx, lane, draft=d), "order_state") == []


def test_delivery_window_rows():
    early = order("o1", delivery_window_start="2026-10-08T20:00:00+00:00", delivery_window_end="2026-10-08T23:00:00+00:00")
    late = order("o2", delivery_window_start="2026-10-08T05:00:00+00:00", delivery_window_end="2026-10-08T11:05:00+00:00")
    missing = order("o3", delivery_window_start=None, delivery_window_end=None)
    ctx = make_ctx([early, late, missing])
    d, lane = lane_with(ctx, "o1", "o2", "o3")
    rows = {c.scope.order_id: (c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "delivery_window")}
    assert rows == {"o1": ("info", "early_arrival"), "o2": ("warn", "eta_after_window"), "o3": ("block", "missing_delivery_window")}
    warn = next(c for c in lane_checks(ctx, lane, draft=d) if c.reason_code == "eta_after_window")
    assert warn.warning_id and len(warn.warning_id) == 16


def test_driver_rows():
    ctx = make_ctx([order("o1")])
    d, lane = lane_with(ctx, "o1", driver_id=None)
    assert codes(by_check(lane_checks(ctx, lane, draft=d), "driver_pairing")) == ["no_driver"]
    lane.driver_id = "ghost"
    assert codes(by_check(lane_checks(ctx, lane, draft=d), "driver_pairing"), "block") == ["driver_not_in_tenant"]
    lane.driver_id = "d1"
    ctx.qualification["d1"] = {"eligible": False, "reasons": ["cdl_expired", "missing_hazmat"]}
    assert codes(by_check(lane_checks(ctx, lane, draft=d), "driver_qualification"), "block") == ["cdl_expired", "missing_hazmat"]
    ctx.qualification["d1"] = {"eligible": True, "reasons": []}
    ctx.drivers["d1"]["medical_card_expiry"] = (NOW + timedelta(days=3)).isoformat()
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "driver_qualification")] == [("info", "expires_within_7_days")]


def test_driver_double_booked_rule():
    ctx = make_ctx([order("o1"), order("o2")])
    d = with_lanes(ctx, "T1", "T2")
    d = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["o1"], truck_id="T1"), ctx).draft
    d = apply(d, cmd("assign_orders", versions(d, "T2"), order_ids=["o2"], truck_id="T2"), ctx).draft
    d.lanes["T1"].driver_id = d.lanes["T2"].driver_id = "d1"
    rows = by_check(lane_checks(ctx, d.lanes["T2"], draft=d), "driver_pairing")
    assert [(c.outcome, c.reason_code) for c in rows] == [("block", "driver_double_booked")]
    assert "T1" in rows[0].message


def test_hos_rows_today():
    ctx = make_ctx([order("o1")])
    d, lane = lane_with(ctx, "o1")
    ctx.hos_verdicts["d1"] = {"outcome": "blocked"}
    row = by_check(lane_checks(ctx, lane, draft=d), "hos")
    assert [(c.outcome, c.reason_code, c.fix_link.kind) for c in row] == [("block", "hos_gate_blocked", "hos_override")]
    ctx.hos_verdicts["d1"] = {"outcome": "passed"}
    ctx.hos_advisories["d1"] = {"remaining_drive_time": {"availability": "available", "value": 0.1}}
    assert codes(by_check(lane_checks(ctx, lane, draft=d), "hos")) == ["hos_projected_over"]
    ctx.hos_advisories["d1"] = {"remaining_drive_time": {"availability": "available", "value": 9.5}}
    row = by_check(lane_checks(ctx, lane, draft=d), "hos")
    assert [(c.outcome, c.reason_code, c.message) for c in row] == [("info", "hos_remaining", "9.5 h of driving left today.")]
    ctx.unavailable.add("hos")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "hos")] == [("warn", "check_unavailable")]


def test_hos_future_day_lane_with_blocked_driver_is_info_only():
    ctx = make_ctx([order("o1", day=TOMORROW)])
    ctx.service_date = TOMORROW
    d, lane = lane_with(ctx, "o1")
    ctx.hos_verdicts["d1"] = {"outcome": "blocked"}
    rows = by_check(lane_checks(ctx, lane, draft=d), "hos")
    assert [(c.outcome, c.reason_code, c.message) for c in rows] == [("info", "hos_not_projected", "Hours of service are checked on the day.")]


def test_certification_rows():
    ctx = make_ctx([order("o1")])
    d, lane = lane_with(ctx, "o1")
    ctx.certification["T1"] = {"eligible": False, "reasons": ["dot_cert_expired"]}
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "asset_certification")] == [("block", "dot_cert_expired")]
    ctx.certification.pop("T1")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "asset_certification")] == [("block", "check_unavailable")]


def test_compartment_fit_rows():
    ctx = make_ctx([order("o1", gallons_requested=8000.0)], cap=10000.0)
    d, lane = lane_with(ctx, "o1")
    assert ("block", "total_overage") in [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "compartment_fit")]
    ctx2 = make_ctx([order("o1", gallons_requested=100.0)])
    d2, lane2 = lane_with(ctx2, "o1")
    rows = [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx2, lane2, draft=d2), "compartment_fit")]
    assert ("warn", "below_min_drop") in rows
    ctx3 = make_ctx([order("o1")])
    d3, lane3 = lane_with(ctx3, "o1")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx3, lane3, draft=d3), "compartment_fit")] == [("info", "fill_percent")]
    lane3.loads[0].allocation_overrides = {"o1": [CompartmentShare(compartment_id="C9", liters=10)]}
    assert "override_invalid" in codes(by_check(lane_checks(ctx3, lane3, draft=d3), "compartment_fit"), "block")
    ctx3.compartments["T1"] = []
    assert codes(by_check(lane_checks(ctx3, lane3, draft=d3), "compartment_fit"), "block") == ["no_compatible_compartments"]
    ctx3.unavailable.add("compartments")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx3, lane3, draft=d3), "compartment_fit")] == [("warn", "check_unavailable")]


def test_no_compatible_compartment_for_product():
    ctx = make_ctx([order("o1", product_code="DEF")])
    d, lane = lane_with(ctx, "o1")
    assert "no_compatible_compartments" in codes(by_check(lane_checks(ctx, lane, draft=d), "compartment_fit"), "block")


def test_compatibility_rows():
    ctx = make_ctx([order("o1", product_code="DEF")])
    ctx.compartments["T1"] = [
        Compartment(compartment_id="C1", truck_id="T1", capacity_liters=10000, allowed_grades=[FuelGrade.AGO], allowed_product_codes=["DEF"], position_index=1, tenant_id=T)
    ]
    ctx.compartment_states["T1"] = {"C1": {"state": "dirty", "last_loaded_product": "GASOLINE_REG", "last_loaded_at": "2026-10-07T10:00:00+00:00"}}
    d, lane = lane_with(ctx, "o1")
    rows = by_check(lane_checks(ctx, lane, draft=d), "compartment_compatibility")
    assert [(c.outcome, c.reason_code) for c in rows] == [("block", "compatibility_blocked")]
    ctx2 = make_ctx([order("o1")])
    ctx2.compartment_states["T1"] = {
        "C1": {"last_loaded_product": "GASOLINE_REG", "last_loaded_at": "2026-10-07T10:00:00+00:00"},
        "C2": {"last_loaded_product": "GASOLINE_REG", "last_loaded_at": "2026-10-07T10:00:00+00:00"},
    }
    d2, lane2 = lane_with(ctx2, "o1")
    rows = by_check(lane_checks(ctx2, lane2, draft=d2), "compartment_compatibility")
    assert rows and all((c.outcome, c.reason_code) == ("warn", "requires_cleaning") for c in rows)
    ctx2.unavailable.add("rules")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx2, lane2, draft=d2), "compartment_compatibility")] == [("warn", "check_unavailable")]


def test_dyed_diesel_rows():
    ctx = make_ctx([order("o1", product_code="OFF_ROAD_DIESEL")])
    d, lane = lane_with(ctx, "o1")
    comp = lane.loads[0].allocations[0].compartment_id
    ctx.dyed[("T1", comp, "OFF_ROAD_DIESEL")] = {"valid": False, "error_code": "dyed.compartment_incompatible"}
    rows = by_check(lane_checks(ctx, lane, draft=d), "dyed_diesel")
    assert [(c.outcome, c.reason_code) for c in rows] == [("block", "dyed_compartment_incompatible")]
    ctx.dyed.clear()
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "dyed_diesel")] == [("block", "check_unavailable")]


async def test_dyed_enforcer_called_only_for_dyed_products():
    store = BoardStore()
    _seed(store)
    svc, deps = _service(store)
    await svc.build_context(T, TODAY, truck_ids=["T1"], driver_ids=[], order_ids=["o1"], timezone_name=TZ)
    assert deps["dyed_diesel_enforcer"].calls == []
    store.seed("fuel_orders_current", "o2", order("o2", product_code="OFF_ROAD_DIESEL"))
    ctx = await svc.build_context(T, TODAY, truck_ids=["T1"], driver_ids=[], order_ids=["o2"], timezone_name=TZ)
    assert deps["dyed_diesel_enforcer"].calls == [(T, "C1", "OFF_ROAD_DIESEL")]
    assert ctx.dyed[("T1", "C1", "OFF_ROAD_DIESEL")] == {"valid": True}


def test_schedule_rows():
    ctx = make_ctx([order("o1", ship_to_lat=None, ship_to_lon=None)])
    d, lane = lane_with(ctx, "o1")
    assert codes(by_check(lane_checks(ctx, lane, draft=d), "schedule")) == ["eta_unavailable"]
    far = make_ctx([order(f"o{i}", ship_to_lat=44.0 + i * 0.5, ship_to_lon=-87.6) for i in range(6)])
    d2, lane2 = lane_with(far, *[f"o{i}" for i in range(6)])
    assert "outside_shift" in codes(by_check(lane_checks(far, lane2, draft=d2), "schedule"))


def test_terminal_rows():
    ctx = make_ctx([order("o1")])
    d, lane = lane_with(ctx, "o1")
    assert [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "terminal_supply")] == [("info", "terminal_unset")]
    lane.loads[0].terminal_id = "TERM1"
    ctx.terminal_waits = {"TERM1": 70.0}
    ctx.contract_usage = {"TERM1": 93.0}
    rows = [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "terminal_supply")]
    assert rows == [("warn", "long_terminal_wait"), ("warn", "contract_lift_limit")]
    ctx.unavailable.add("terminal_waits")
    assert ("warn", "check_unavailable") in [(c.outcome, c.reason_code) for c in by_check(lane_checks(ctx, lane, draft=d), "terminal_supply")]


async def test_contract_usage_from_supplier_contracts():
    store = BoardStore()
    _seed(store)
    store.seed("supplier_contracts", "k1", {"tenant_id": T, "contract_id": "k1", "status": "active", "preferred_terminal_ids": ["TERM1"], "minimum_lift_gallons_per_month": 1000})

    class Lift:
        async def get_summary(self, tenant_id, contract_id, minimum):
            return {"percent_of_minimum": 95.0}

    class Waits:
        async def resolve(self, tenant_id, terminal_id):
            return 50.0

    svc, _ = _service(store, contract_lift_service=Lift(), terminal_wait_resolver=Waits())
    ctx = await svc.build_context(T, TODAY, truck_ids=[], driver_ids=[], order_ids=[], terminal_ids=["TERM1"], timezone_name=TZ)
    assert ctx.contract_usage == {"TERM1": 95.0} and ctx.terminal_waits == {"TERM1": 50.0}


def test_unavailable_split_k34():
    from fuel.services.dispatch_board_models import Scope

    scope = Scope(truck_id="T1")
    for check in ("driver_qualification", "asset_certification", "dyed_diesel"):
        assert _unavailable(check, "s", scope).outcome == "block"
    for check in ("order_state", "driver_pairing", "hos", "compartment_fit", "compartment_compatibility", "terminal_supply"):
        assert _unavailable(check, "s", scope).outcome == "warn"
    assert BLOCKING_UNAVAILABLE == {"driver_qualification", "asset_certification", "dyed_diesel"}


def test_warning_ids_are_deterministic_and_change_with_the_condition():
    from fuel.services.dispatch_board_models import Scope

    scope = Scope(truck_id="T1", load_id="L1", order_id="o1")
    assert warning_id_for("delivery_window", "eta_after_window", scope, 15) == warning_id_for("delivery_window", "eta_after_window", scope, 15)
    assert warning_id_for("delivery_window", "eta_after_window", scope, 15) != warning_id_for("delivery_window", "eta_after_window", scope, 30)


def test_messages_carry_no_customer_text():
    ctx = make_ctx([order("o1", customer_name="Jane Doe Farms", ship_to_address="9 Secret Lane")])
    d, lane = lane_with(ctx, "o1")
    ctx.orders["o1"]["status"] = "on_hold"
    for c in lane_checks(ctx, lane, draft=d):
        assert "Jane" not in c.message and "Secret" not in c.message


def test_worst_outcome_tiers():
    ctx = make_ctx([order("o1")])
    d, lane = lane_with(ctx, "o1")
    assert worst_outcome(lane_checks(ctx, lane, draft=d)) == "info"
    lane.driver_id = None
    assert worst_outcome(lane_checks(ctx, lane, draft=d)) == "warn"
    ctx.orders["o1"]["status"] = "on_hold"
    assert worst_outcome(lane_checks(ctx, lane, draft=d)) == "block"


# ---- candidates (K3.1) -----------------------------------------------------


def test_validate_candidates_preview_and_outcomes():
    ctx = make_ctx([order("o1"), order("o2", status="on_hold")])
    d = with_lanes(ctx, "T1", "T2")
    d.lanes["T1"].driver_id = "d1"
    results = candidate_results(ctx, d, DragItem(kind="order", ids=["o1"]), ["T1", "T2", "T9"])
    assert results["T1"].outcome == "info"
    assert results["T1"].preview.insertion_index == 0
    assert set(results["T1"].preview.fill_by_compartment) <= {"C1", "C2"}
    assert results["T2"].outcome == "warn"  # no driver
    assert results["T9"].outcome == "block" and results["T9"].reason == "unknown_truck"
    held = candidate_results(ctx, d, DragItem(kind="order", ids=["o2"]), ["T1"])
    assert held["T1"].outcome == "block" and held["T1"].worst_checks[0].reason_code == "on_hold"
    bad_position = candidate_results(ctx, d, DragItem(kind="order", ids=["o1"]), ["T1"], Target(load_id="nope"))
    assert bad_position["T1"].reason == "unknown_load"
    # The draft itself is never changed.
    assert d.order_index == {}


def test_validate_candidates_for_a_driver():
    ctx = make_ctx([order("o1")])
    ctx.qualification["d2"] = {"eligible": False, "reasons": ["driver_inactive"]}
    d = with_lanes(ctx, "T1")
    results = candidate_results(ctx, d, DragItem(kind="driver", ids=["d2"]), ["T1"])
    assert results["T1"].outcome == "block"


# ---- properties ------------------------------------------------------------


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    gallons=st.lists(st.floats(50, 4000), min_size=1, max_size=5),
    hold=st.booleans(),
    driver_on=st.booleans(),
)
def test_p3_validation_is_deterministic(gallons, hold, driver_on):
    orders = [order(f"o{i}", gallons_requested=g) for i, g in enumerate(gallons)]
    ctx = make_ctx(orders)
    d, lane = lane_with(ctx, *[o["order_id"] for o in orders], driver_id="d1" if driver_on else None)
    if hold:
        ctx.orders["o0"]["status"] = "on_hold"
    first = lane_checks(ctx, lane, draft=d)
    second = lane_checks(ctx, lane.model_copy(deep=True), draft=d.model_copy(deep=True))
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    gallons=st.lists(st.floats(50, 6000), min_size=1, max_size=4),
    extra=st.floats(50, 6000),
    product=st.sampled_from(["DIESEL_2", "DEF", "GASOLINE_REG"]),
)
def test_p4_monotone_compartment_fit(gallons, extra, product):
    """Adding a stop never turns a compartment_fit block into pass."""
    orders = [order(f"o{i}", gallons_requested=g, product_code=product if i == 0 else "DIESEL_2") for i, g in enumerate(gallons)]
    new = order("new", gallons_requested=extra)
    ctx = make_ctx(orders + [new])
    d, lane = lane_with(ctx, *[o["order_id"] for o in orders])
    def fit_blocked(lane_):
        return any(c.outcome == "block" for c in by_check(lane_checks(ctx, lane_, draft=d), "compartment_fit"))
    if not fit_blocked(lane):
        return
    load_id = lane.loads[0].load_id
    grown = apply(d, cmd("assign_orders", versions(d, "T1"), order_ids=["new"], truck_id="T1", target={"load_id": load_id}), ctx).draft
    assert fit_blocked(grown.lanes["T1"])
