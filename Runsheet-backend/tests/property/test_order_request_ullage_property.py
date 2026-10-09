"""Order requests: orders on one customer tank never get more than its ullage.

Staging finding F10 capped each order at its tank's ullage, but review R3
found the cap was computed per order: two loadable orders on the same tank (a
duplicate import, a re-order before delivery) each got the full ullage as
``hard_cap_liters`` and could overfill the tank together. The solver-level
properties cannot see this because they receive caps, not tanks, so these
properties run over ``CompartmentLoadingAgent._build_delivery_requests_from_orders``.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)
from fuel.customer_tank_models import CustomerTank
from services.unit_conversion import GAL_TO_L

TENANT = "tenant-1"
EPS = 1e-6
TANK_IDS = ["tank-1", "tank-2", "tank-3"]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def tanks(draw) -> Dict[str, Optional[Dict[str, float]]]:
    """Each tank id is known (capacity/level in gallons) or unknown (``None``)."""
    out: Dict[str, Optional[Dict[str, float]]] = {}
    for tank_id in TANK_IDS:
        if draw(st.booleans()) or tank_id == "tank-1":
            capacity = draw(st.floats(min_value=50.0, max_value=3000.0))
            fraction = draw(st.floats(min_value=0.0, max_value=1.0))
            out[tank_id] = {"capacity": capacity, "level": capacity * fraction}
        else:
            out[tank_id] = None
    return out


@st.composite
def orders(draw) -> List[Dict[str, Any]]:
    count = draw(st.integers(min_value=1, max_value=8))
    out = []
    for i in range(count):
        out.append({
            "order_id": f"o{i}",
            "customer_id": f"cust-{i}",
            "customer_tank_id": draw(st.sampled_from(TANK_IDS + [None])),
            "product_code": draw(st.sampled_from(["HEATING_OIL", "DIESEL_2"])),
            "gallons_requested": draw(
                st.one_of(st.none(), st.floats(min_value=1.0, max_value=2000.0))
            ),
            "fill_to_full": draw(st.booleans()),
            "status": "placed",
            "tenant_id": TENANT,
            "_score": draw(st.floats(min_value=0.0, max_value=1.0)),
        })
    return out


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _tank(spec: Dict[str, float]) -> MagicMock:
    tank = MagicMock(spec=CustomerTank)
    tank.capacity_gallons = spec["capacity"]
    tank.current_level_gallons = spec["level"]
    return tank


def _ullage_liters(spec: Dict[str, float]) -> float:
    return max(0.0, spec["capacity"] - spec["level"]) * GAL_TO_L


async def _build(order_docs, tank_specs):
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)
    es = MagicMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    activity_log = MagicMock()
    activity_log.log = AsyncMock()
    agent = CompartmentLoadingAgent(
        signal_bus=signal_bus,
        es_service=es,
        activity_log_service=activity_log,
        ws_manager=MagicMock(),
        confirmation_protocol=MagicMock(),
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )
    agent._query_fuel_orders = AsyncMock(
        return_value=[{k: v for k, v in o.items() if k != "_score"} for o in order_docs]
    )
    agent._fail_order_loading = AsyncMock()
    repo = MagicMock()

    async def _get(*, tenant_id, customer_tank_id):
        spec = tank_specs.get(customer_tank_id)
        return _tank(spec) if spec is not None else None

    repo.get = AsyncMock(side_effect=_get)
    agent._customer_tank_repo = repo
    priorities = DeliveryPriorityList(
        priorities=[
            DeliveryPriority(
                station_id=o["customer_tank_id"] or o["order_id"],
                order_id=o["order_id"],
                fuel_grade=FuelGrade.AGO,
                priority_score=o["_score"],
                priority_bucket=PriorityBucket.CRITICAL,
            )
            for o in order_docs
        ],
        tenant_id=TENANT,
        run_id="run-1",
    )
    return await agent._build_delivery_requests_from_orders(TENANT, priorities)


def _requested_liters(order: Dict[str, Any], spec: Optional[Dict[str, float]]) -> Optional[float]:
    """What the order asks for before any tank budget (the builder's rule)."""
    if order["fill_to_full"] and spec is not None:
        return _ullage_liters(spec)
    gallons = order["gallons_requested"]
    if gallons is not None and gallons > 0:
        return gallons * GAL_TO_L
    return None


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestPerTankUllageBudget:
    @settings(max_examples=200, deadline=None)
    @given(order_docs=orders(), tank_specs=tanks())
    def test_caps_on_one_tank_sum_to_at_most_its_ullage(self, order_docs, tank_specs):
        requests = asyncio.run(_build(order_docs, tank_specs))
        by_id = {o["order_id"]: o for o in order_docs}

        per_tank: Dict[str, float] = defaultdict(float)
        for r in requests:
            assert r.hard_cap_liters > 0
            assert r.quantity_liters == r.hard_cap_liters
            tank_id = by_id[r.order_id]["customer_tank_id"]
            if tank_id is not None and tank_specs.get(tank_id) is not None:
                per_tank[tank_id] += r.hard_cap_liters
        for tank_id, total in per_tank.items():
            assert total <= round(_ullage_liters(tank_specs[tank_id]), 2) + EPS, tank_id

    @settings(max_examples=200, deadline=None)
    @given(order_docs=orders(), tank_specs=tanks())
    def test_tank_room_goes_to_orders_in_priority_order(self, order_docs, tank_specs):
        """Greedy by priority: once an order on a tank is trimmed, no
        lower-priority order on that tank gets anything; uncapped (no known
        tank) orders keep their full request."""
        requests = asyncio.run(_build(order_docs, tank_specs))
        by_id = {o["order_id"]: o for o in order_docs}

        scores = [by_id[r.order_id]["_score"] for r in requests]
        assert scores == sorted(scores, reverse=True)
        assert len({r.order_id for r in requests}) == len(requests)

        trimmed_tanks = set()
        for r in requests:
            order = by_id[r.order_id]
            tank_id = order["customer_tank_id"]
            spec = tank_specs.get(tank_id) if tank_id else None
            requested = _requested_liters(order, spec)
            assert requested is not None
            if spec is None:
                assert r.hard_cap_liters == round(requested, 2)
                continue
            assert tank_id not in trimmed_tanks, "a lower-priority order drew on a used-up tank"
            if r.hard_cap_liters < round(requested, 2) - 0.01:
                trimmed_tanks.add(tank_id)

    @settings(max_examples=200, deadline=None)
    @given(order_docs=orders(), tank_specs=tanks())
    def test_a_single_order_per_tank_still_gets_min_of_request_and_ullage(
        self, order_docs, tank_specs
    ):
        """The budget only bites when a tank is shared."""
        seen = set()
        unique = []
        for o in order_docs:
            key = o["customer_tank_id"] or o["order_id"]
            if key not in seen:
                seen.add(key)
                unique.append(o)
        requests = asyncio.run(_build(unique, tank_specs))

        for r in requests:
            order = next(o for o in unique if o["order_id"] == r.order_id)
            spec = tank_specs.get(order["customer_tank_id"]) if order["customer_tank_id"] else None
            requested = _requested_liters(order, spec)
            expected = requested if spec is None else min(requested, _ullage_liters(spec))
            assert abs(r.hard_cap_liters - round(expected, 2)) <= 0.01
