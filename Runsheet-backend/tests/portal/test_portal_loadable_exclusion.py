"""A portal request is not loadable until a dispatcher confirms it
(design §4.3; ORD-3 / T-ORD-LOADABLE, R4.9).

The candidate queries of the route planner, the delivery prioritizer and the
compartment loader run against the same in-memory store the portal request
was written to (document-store path; ``tests/unit/test_loadable_order_statuses_n2.py``
pins that the Postgres path passes the same status list).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.overlay.delivery_prioritization_agent import DeliveryPrioritizationAgent
from Agents.overlay.route_planning_agent import RoutePlanningAgent
from tests.portal.conftest import CUSTOMER_A, T1, call
from tests.portal.test_order_release_hold_portal import staff_orders  # noqa: F401 — fixture
from tests.portal.test_portal_orders import TANK, order_body
from tests.unit.test_route_planning_agent_order_stops import _make_deps


def _agents(store):
    out = []
    for cls in (RoutePlanningAgent, DeliveryPrioritizationAgent, CompartmentLoadingAgent):
        deps = _make_deps()
        deps["es_service"].search_documents = AsyncMock(side_effect=store.search_documents)
        out.append(cls(**deps))
    return out


async def _candidate_ids(store):
    route, prioritization, loading = _agents(store)
    return {
        "route_planning": {o["order_id"] for o in await route._fetch_routable_orders(T1)},
        "prioritization": {o["order_id"] for o in await prioritization._fetch_pending_orders(T1)},
        "compartment_loading": {o["order_id"] for o in await loading._query_fuel_orders(T1)},
    }


@pytest.fixture
def h(portal_on, staff_orders, cA):  # noqa: F811
    staff_orders.add_tank(T1, CUSTOMER_A, TANK)
    # A loadable control order, so an empty result can't pass by accident.
    staff_orders.add_order(T1, CUSTOMER_A, "QA-ORD-CONTROL", customer_tank_id=TANK, status="placed")
    return staff_orders


def test_portal_order_excluded_until_release_hold(client, sessions, h, cA):
    import asyncio

    resp = call(client, "POST", "/api/portal/orders", cA, json=order_body())
    assert resp.status_code == 201, resp.text
    oid = resp.json()["data"]["order_id"]

    before = asyncio.run(_candidate_ids(h.store))
    for agent, ids in before.items():
        assert "QA-ORD-CONTROL" in ids, agent
        assert oid not in ids, agent

    dispatcher = sessions.staff("dispatcher")
    released = call(client, "POST", f"/api/orders/{oid}/release-hold", dispatcher, json={})
    assert released.status_code == 200, released.text

    after = asyncio.run(_candidate_ids(h.store))
    for agent, ids in after.items():
        assert oid in ids, agent
