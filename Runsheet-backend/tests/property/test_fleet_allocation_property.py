"""Fleet allocation: each order on at most one truck, never above its cap.

Staging finding F10: ``CompartmentLoadingAgent.evaluate`` ran
``optimize_loading_plan`` once per truck with the SAME full request list, so
both trucks were planned to carry both orders and each plan was queued as its
own approval. It also multiplied customer orders by the 10 % uncertainty
buffer and had no ullage cap (200 gal into a tank with 120 gal of room was
planned as 832.79 L), and wrote the legacy grade (``AGO``) on assignments.

``allocate_across_trucks`` replaces the per-truck loop. The properties below
are the contract it has to keep for any fleet and any request list.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from Agents.support.compartment_models import (
    Compartment,
    DeliveryRequest,
    TruckSpec,
)
from Agents.support.compartment_solver import (
    LEGACY_GRADE_CATEGORIES,
    allocate_across_trucks,
    compartment_accepts,
    fuel_density_kg_per_liter,
    legacy_grade_for_product,
    planned_liters,
    segregation_key,
)
from Agents.support.fuel_distribution_models import FuelGrade
from fuel.services.fuel_product_catalog import FUEL_PRODUCT_CATALOG, canonicalize
from services.unit_conversion import GAL_TO_L

TENANT = "tenant-1"
ALL_PRODUCTS = [p.product_code for p in FUEL_PRODUCT_CATALOG]
LEGACY_GRADES = {"AGO", "PMS", "ATK", "LPG"}
EPS = 1e-6


def _family(code: str) -> FuelGrade:
    return FuelGrade(legacy_grade_for_product(code) or "AGO")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def compartments_for(draw, truck_id: str) -> List[Compartment]:
    count = draw(st.integers(min_value=1, max_value=5))
    out = []
    for i in range(count):
        capacity = draw(st.floats(min_value=500.0, max_value=12000.0))
        if draw(st.booleans()):
            codes = draw(
                st.lists(st.sampled_from(ALL_PRODUCTS), min_size=1, max_size=4, unique=True)
            )
            grades = [FuelGrade.AGO]
        else:
            codes = None
            grades = [
                FuelGrade(g)
                for g in draw(
                    st.lists(
                        st.sampled_from(sorted(LEGACY_GRADE_CATEGORIES)),
                        min_size=1, max_size=4, unique=True,
                    )
                )
            ]
        out.append(Compartment(
            compartment_id=f"{truck_id}-c{i}",
            truck_id=truck_id,
            capacity_liters=capacity,
            allowed_grades=grades,
            allowed_product_codes=codes,
            position_index=i,
            tenant_id=TENANT,
        ))
    return out


@st.composite
def fleets(draw) -> List[TruckSpec]:
    n = draw(st.integers(min_value=1, max_value=4))
    trucks = []
    for i in range(n):
        truck_id = f"T{i}"
        max_weight = draw(
            st.one_of(st.none(), st.floats(min_value=2000.0, max_value=40000.0))
        )
        tare = draw(st.floats(min_value=0.0, max_value=1500.0)) if max_weight else 0.0
        trucks.append(TruckSpec(
            truck_id=truck_id,
            compartments=draw(compartments_for(truck_id)),
            max_weight_kg=max_weight,
            tare_weight_kg=tare,
        ))
    return trucks


@st.composite
def request_lists(draw) -> List[DeliveryRequest]:
    n = draw(st.integers(min_value=1, max_value=12))
    out = []
    for i in range(n):
        code = draw(st.sampled_from(ALL_PRODUCTS))
        qty = draw(st.floats(min_value=100.0, max_value=8000.0))
        cap = draw(
            st.one_of(st.none(), st.floats(min_value=1.0, max_value=qty))
        )
        out.append(DeliveryRequest(
            station_id=f"s{i}",
            order_id=f"ord-{i}",
            fuel_grade=_family(code),
            product_code=code,
            quantity_liters=qty,
            hard_cap_liters=cap,
        ))
    return out


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestFleetAllocationProperties:
    @settings(max_examples=200, deadline=None)
    @given(trucks=fleets(), requests=request_lists())
    def test_no_order_appears_in_two_plans(self, trucks, requests) -> None:
        allocation = allocate_across_trucks(trucks, requests)
        trucks_per_order: Dict[str, set] = defaultdict(set)
        for truck_id, plan in allocation.plans.items():
            for a in plan.assignments:
                trucks_per_order[a.order_id].add(truck_id)
        doubled = {o: t for o, t in trucks_per_order.items() if len(t) > 1}
        assert doubled == {}, f"orders on several trucks: {doubled}"

    @settings(max_examples=200, deadline=None)
    @given(trucks=fleets(), requests=request_lists())
    def test_load_never_exceeds_quantity_or_hard_cap(self, trucks, requests) -> None:
        allocation = allocate_across_trucks(trucks, requests)
        loaded: Dict[str, float] = defaultdict(float)
        for plan in allocation.plans.values():
            for a in plan.assignments:
                loaded[a.order_id] += a.quantity_liters
        for req in requests:
            got = loaded.get(req.order_id, 0.0)
            assert got <= req.quantity_liters + EPS, (req.order_id, got)
            if req.hard_cap_liters is not None:
                assert got <= req.hard_cap_liters + EPS, (req.order_id, got)

    @settings(max_examples=200, deadline=None)
    @given(trucks=fleets(), requests=request_lists())
    def test_output_grades_are_canonical(self, trucks, requests) -> None:
        allocation = allocate_across_trucks(trucks, requests)
        for plan in allocation.plans.values():
            for a in plan.assignments:
                assert a.fuel_grade == canonicalize(a.product_code)
                assert a.fuel_grade not in LEGACY_GRADES

    @settings(max_examples=200, deadline=None)
    @given(trucks=fleets(), requests=request_lists())
    def test_one_product_per_compartment_within_capacity(self, trucks, requests) -> None:
        allocation = allocate_across_trucks(trucks, requests)
        by_id = {c.compartment_id: c for t in trucks for c in t.compartments}
        keys: Dict[str, set] = defaultdict(set)
        volume: Dict[str, float] = defaultdict(float)
        for plan in allocation.plans.values():
            for a in plan.assignments:
                keys[a.compartment_id].add(
                    segregation_key(product_code=a.product_code, fuel_grade=a.fuel_grade)
                )
                volume[a.compartment_id] += a.quantity_liters
                assert compartment_accepts(by_id[a.compartment_id], a.product_code)
        for cid, ks in keys.items():
            assert len(ks) == 1, f"{cid} holds {sorted(ks)}"
            assert volume[cid] <= by_id[cid].capacity_liters + EPS

    @settings(max_examples=200, deadline=None)
    @given(trucks=fleets(), requests=request_lists())
    def test_weight_within_limit(self, trucks, requests) -> None:
        allocation = allocate_across_trucks(trucks, requests)
        specs = {t.truck_id: t for t in trucks}
        for truck_id, plan in allocation.plans.items():
            spec = specs[truck_id]
            if spec.max_weight_kg is None:
                continue
            weight = sum(
                a.quantity_liters * fuel_density_kg_per_liter(product_code=a.product_code)
                for a in plan.assignments
            )
            assert weight <= spec.max_weight_kg - spec.tare_weight_kg + EPS

    @settings(max_examples=200, deadline=None)
    @given(trucks=fleets(), requests=request_lists())
    def test_planned_litres_are_conserved(self, trucks, requests) -> None:
        """assigned + partial + unassigned == planned, to floor-rounding residue."""
        allocation = allocate_across_trucks(trucks, requests)
        assigned = sum(
            a.quantity_liters
            for plan in allocation.plans.values()
            for a in plan.assignments
        )
        partial = sum(allocation.partial.values())
        unassigned = sum(u.planned_liters for u in allocation.unassigned)
        total = sum(planned_liters(r, 1.1, buffer_orders=False) for r in requests)
        # Each request may lose < 0.01 L to floor rounding (not reported as
        # partial) or 0.005 L to rounding its unassigned planned_liters.
        assert abs(assigned + partial + unassigned - total) <= 0.01 * len(requests) + EPS


# ---------------------------------------------------------------------------
# Deterministic cases
# ---------------------------------------------------------------------------


def _staging_truck(truck_id: str) -> TruckSpec:
    return TruckSpec(
        truck_id=truck_id,
        compartments=[
            Compartment(
                compartment_id=f"{truck_id}-c{i}",
                truck_id=truck_id,
                capacity_liters=3000.0,
                allowed_grades=[FuelGrade.AGO],
                position_index=i,
                tenant_id=TENANT,
            )
            for i in range(2)
        ],
    )


def _staging_requests() -> List[DeliveryRequest]:
    ullage_cap = round(120 * GAL_TO_L, 2)  # tank 500 gal, at 380 gal
    return [
        DeliveryRequest(
            station_id="tank-A", order_id="ord_A", fuel_grade=FuelGrade.AGO,
            product_code="DIESEL_2", quantity_liters=round(300 * GAL_TO_L, 2),
        ),
        DeliveryRequest(
            station_id="tank-B", order_id="ord_B", fuel_grade=FuelGrade.AGO,
            product_code="HEATING_OIL", quantity_liters=ullage_cap,
            hard_cap_liters=ullage_cap,
        ),
    ]


class TestStagingRepro:
    def test_two_trucks_two_orders_each_order_in_exactly_one_plan(self) -> None:
        allocation = allocate_across_trucks(
            [_staging_truck("QA-TRUCK-01"), _staging_truck("QA-TRUCK-02")],
            _staging_requests(),
        )
        plans_per_order: Dict[str, List[str]] = defaultdict(list)
        for truck_id, plan in allocation.plans.items():
            for order_id in {a.order_id for a in plan.assignments}:
                plans_per_order[order_id].append(truck_id)
        assert {o: len(t) for o, t in plans_per_order.items()} == {
            "ord_A": 1, "ord_B": 1,
        }
        assert allocation.unassigned == []
        assert allocation.partial == {}

        ord_b = [
            a for plan in allocation.plans.values()
            for a in plan.assignments if a.order_id == "ord_B"
        ]
        assert sum(a.quantity_liters for a in ord_b) == 454.25  # was 832.79
        assert {a.fuel_grade for a in ord_b} == {"HEATING_OIL"}  # was AGO

    def test_order_backed_requests_are_not_buffered(self) -> None:
        allocation = allocate_across_trucks(
            [_staging_truck("QA-TRUCK-01")], _staging_requests()[:1]
        )
        (plan,) = allocation.plans.values()
        assert sum(a.quantity_liters for a in plan.assignments) == round(300 * GAL_TO_L, 2)

    def test_legacy_station_demand_keeps_the_buffer(self) -> None:
        req = DeliveryRequest(
            station_id="station-1", fuel_grade=FuelGrade.AGO, quantity_liters=1000.0,
        )
        allocation = allocate_across_trucks([_staging_truck("T1")], [req])
        (plan,) = allocation.plans.values()
        assert sum(a.quantity_liters for a in plan.assignments) == 1100.0
        assert {a.fuel_grade for a in plan.assignments} == {"DIESEL_2"}

    def test_no_room_anywhere_is_unassigned(self) -> None:
        req = DeliveryRequest(
            station_id="s1", order_id="ord-P", fuel_grade=FuelGrade.LPG,
            product_code="PROPANE", quantity_liters=500.0,
        )
        allocation = allocate_across_trucks([_staging_truck("T1")], [req])
        assert allocation.plans == {}
        assert [u.order_id for u in allocation.unassigned] == ["ord-P"]
        assert allocation.unassigned[0].reason == "no_truck_capacity"

    def test_overflow_goes_to_the_truck_with_most_room_and_is_partial(self) -> None:
        req = DeliveryRequest(
            station_id="s1", order_id="ord-big", fuel_grade=FuelGrade.AGO,
            product_code="DIESEL_2", quantity_liters=7000.0,
        )
        allocation = allocate_across_trucks(
            [_staging_truck("T1"), _staging_truck("T2")], [req]
        )
        assert list(allocation.plans) == ["T1"]
        assert allocation.partial == {"ord-big": 1000.0}
        assert allocation.plans["T1"].unserved_demand_liters == 1000.0
