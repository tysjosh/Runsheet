"""MarginAttribution.terminal_for_order (design "Terminal attribution (D11)")."""

from __future__ import annotations

import pytest

from commerce.services.margin_attribution import MarginAttribution

from ._margin_fakes import FakeDocStore, tenant_of_query
from .conftest import TENANT_A, TENANT_B


def plan(plan_id: str, *, tenant: str = TENANT_A, terminal: str | None = None, stations=("TANK-1",), run_id: str | None = None):
    return {
        "plan_id": plan_id,
        "run_id": run_id,
        "tenant_id": tenant,
        "terminal_id": terminal,
        "assignments": [{"station_id": s, "quantity_liters": 1000} for s in stations],
    }


def bol(bol_id: str, plan_id: str, terminal: str | None, *, tenant: str = TENANT_A):
    return {"bol_id": bol_id, "tenant_id": tenant, "load_plan_id": plan_id, "terminal_id": terminal}


ORDER = {"order_id": "ORD-1", "customer_tank_id": "TANK-1", "assigned_run_id": "PLAN-1", "tenant_id": TENANT_A}


@pytest.fixture
def store() -> FakeDocStore:
    return FakeDocStore()


@pytest.fixture
def attribution(store) -> MarginAttribution:
    return MarginAttribution(store)


async def test_no_assigned_run_is_no_plan(attribution, store):
    assert await attribution.terminal_for_order(TENANT_A, {"order_id": "ORD-1"}) == (None, "no_plan")
    assert store.calls == []


async def test_plan_terminal_wins(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1", terminal="TERM-9"))
    store.add("terminal_bols", bol("B1", "PLAN-1", "TERM-1"))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == ("TERM-9", "plan_terminal")
    assert store.calls_to("terminal_bols") == []


async def test_plan_query_shape_matches_pod(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1", terminal="TERM-9"))
    await attribution.terminal_for_order(TENANT_A, ORDER)
    (index, body), = store.calls
    assert index == "mvp_load_plans"
    # Same body as PodService._resolve_loading_plan (driver/services/pod_service.py).
    assert body == {
        "query": {
            "bool": {
                "must": [{"term": {"tenant_id": TENANT_A}}],
                "should": [{"term": {"plan_id": "PLAN-1"}}, {"term": {"run_id": "PLAN-1"}}],
                "minimum_should_match": 1,
            }
        },
        "size": 5,
    }


async def test_run_id_reference_and_order_id_station_match(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-X", run_id="RUN-7", terminal="TERM-3", stations=("ORD-2",)))
    order = {"order_id": "ORD-2", "assigned_run_id": "RUN-7"}
    assert await attribution.terminal_for_order(TENANT_A, order) == ("TERM-3", "plan_terminal")


async def test_first_plan_with_a_matching_assignment_is_used(attribution, store):
    store.add(
        "mvp_load_plans",
        plan("PLAN-1", terminal="TERM-OTHER", stations=("TANK-9",)),
        {**plan("PLAN-1b", terminal="TERM-OK"), "run_id": "PLAN-1"},
    )
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == ("TERM-OK", "plan_terminal")


async def test_no_matching_assignment_is_no_plan(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1", terminal="TERM-9", stations=("TANK-9",)))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == (None, "no_plan")


async def test_bol_fallback_with_exactly_one_distinct_terminal(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1"))
    store.add("terminal_bols", bol("B1", "PLAN-1", "TERM-1"), bol("B2", "PLAN-1", "TERM-1"), bol("B3", "PLAN-1", None))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == ("TERM-1", "bol_terminal")
    bol_query = store.calls_to("terminal_bols")[0]
    assert tenant_of_query(bol_query) == TENANT_A
    assert bol_query["size"] == 20


async def test_two_bol_terminals_is_unattributed(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1"))
    store.add("terminal_bols", bol("B1", "PLAN-1", "TERM-1"), bol("B2", "PLAN-1", "TERM-2"))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == (None, "unattributed")


async def test_no_bols_is_unattributed(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1"))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == (None, "unattributed")


async def test_other_tenants_plans_and_bols_are_never_used(attribution, store):
    store.add("mvp_load_plans", plan("PLAN-1", tenant=TENANT_B, terminal="TERM-B"))
    store.add("terminal_bols", bol("B1", "PLAN-1", "TERM-B", tenant=TENANT_B))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == (None, "no_plan")
    store.add("mvp_load_plans", plan("PLAN-1"))
    assert await attribution.terminal_for_order(TENANT_A, ORDER) == (None, "unattributed")
    assert all(tenant_of_query(body) == TENANT_A for _, body in store.calls)
