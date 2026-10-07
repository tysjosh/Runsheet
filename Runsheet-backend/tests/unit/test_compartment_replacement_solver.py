"""``replace_stripped``: re-place cross-contamination strips in the same run (OI-39).

The pass is pure and runs once over the fleet's residual capacity: kept
assignments are seeded as used capacity and weight, and the caller's
predicate excludes compartments the compatibility matrix blocks.
"""
from __future__ import annotations

from typing import List, Optional

from Agents.support.compartment_models import (
    Compartment,
    CompartmentAssignment,
    DeliveryRequest,
    TruckSpec,
)
from Agents.support.compartment_solver import (
    fuel_density_kg_per_liter,
    replace_stripped,
)
from Agents.support.fuel_distribution_models import FuelGrade

TENANT = "tenant-1"


def _comp(truck_id: str, cid: str, capacity: float, position: int = 0) -> Compartment:
    return Compartment(
        compartment_id=cid,
        truck_id=truck_id,
        capacity_liters=capacity,
        allowed_grades=[FuelGrade.AGO, FuelGrade.PMS],
        position_index=position,
        tenant_id=TENANT,
    )


def _truck(truck_id: str, comps: List[Compartment], max_weight_kg: Optional[float] = None) -> TruckSpec:
    return TruckSpec(truck_id=truck_id, compartments=comps, max_weight_kg=max_weight_kg)


def _req(order_id: str, liters: float, product: str = "GASOLINE_REG") -> DeliveryRequest:
    return DeliveryRequest(
        station_id="st-1",
        order_id=order_id,
        fuel_grade=FuelGrade.PMS if product.startswith("GASOLINE") else FuelGrade.AGO,
        product_code=product,
        quantity_liters=liters,
    )


def _kept(cid: str, liters: float, capacity: float, product: str = "DIESEL_2") -> CompartmentAssignment:
    return CompartmentAssignment(
        compartment_id=cid,
        station_id="st-0",
        order_id="ORD-KEPT",
        fuel_grade=product,
        product_code=product,
        quantity_liters=liters,
        compartment_capacity_liters=capacity,
    )


def _block(blocked):
    """Predicate that rejects the given (truck_id, compartment_id) pairs."""
    return lambda truck_id, comp, key: (truck_id, comp.compartment_id) not in blocked


def test_replaced_on_second_truck_when_source_compartment_disallowed():
    trucks = [
        _truck("truck-1", [_comp("truck-1", "c1", 10000)]),
        _truck("truck-2", [_comp("truck-2", "c1", 8000)]),
    ]
    result = replace_stripped(
        trucks, {}, [(_req("ORD-1", 5000), 5000.0)], _block({("truck-1", "c1")})
    )
    assert list(result.placed) == ["truck-2"]
    [a] = result.placed["truck-2"]
    assert a.order_id == "ORD-1"
    assert a.quantity_liters == 5000.0
    assert result.unplaced == []
    assert result.partial == {}


def test_stays_on_same_truck_in_another_compatible_compartment():
    trucks = [
        _truck("truck-1", [_comp("truck-1", "c1", 6000, 0), _comp("truck-1", "c2", 6000, 1)]),
    ]
    result = replace_stripped(
        trucks, {}, [(_req("ORD-1", 4000), 4000.0)], _block({("truck-1", "c1")})
    )
    [a] = result.placed["truck-1"]
    assert a.compartment_id == "c2"
    assert result.unplaced == []


def test_nothing_fits_reports_unplaced_with_reason():
    trucks = [_truck("truck-1", [_comp("truck-1", "c1", 6000)])]
    result = replace_stripped(
        trucks, {}, [(_req("ORD-1", 4000), 4000.0)], _block({("truck-1", "c1")})
    )
    assert result.placed == {}
    [u] = result.unplaced
    assert u.order_id == "ORD-1"
    assert u.order_key == "ORD-1"
    assert u.reason == "no_compatible_compartment"
    assert u.planned_liters == 4000.0


def test_partial_fit_records_shortfall():
    trucks = [_truck("truck-2", [_comp("truck-2", "c1", 3000)])]
    result = replace_stripped(trucks, {}, [(_req("ORD-1", 5000), 5000.0)], _block(set()))
    assert sum(a.quantity_liters for a in result.placed["truck-2"]) == 3000.0
    assert result.partial == {"ORD-1": 2000.0}
    assert result.unplaced == []


def test_seeded_capacity_and_weight_are_respected():
    # truck-2/c1 already holds 6000 L of diesel (5100 kg); 2000 L capacity
    # left in c1 is closed to gasoline (different product), c2 has 4000 L
    # but the weight limit leaves room for only ~1351 L of gasoline.
    c1 = _comp("truck-2", "c1", 8000, 0)
    c2 = _comp("truck-2", "c2", 4000, 1)
    trucks = [_truck("truck-2", [c1, c2], max_weight_kg=6100.0)]
    kept = {"truck-2": [_kept("c1", 6000, 8000)]}
    result = replace_stripped(trucks, kept, [(_req("ORD-1", 4000), 4000.0)], _block(set()))
    placed = result.placed["truck-2"]
    assert all(a.compartment_id == "c2" for a in placed)
    total = sum(a.quantity_liters for a in placed)
    assert total <= c2.capacity_liters
    weight = 6000 * 0.85 + total * fuel_density_kg_per_liter(product_code="GASOLINE_REG")
    assert weight <= 6100.0 + 1e-6
    assert result.partial["ORD-1"] > 0


def test_seeded_same_product_compartment_takes_remaining_capacity():
    c1 = _comp("truck-1", "c1", 8000)
    trucks = [_truck("truck-1", [c1])]
    kept = {"truck-1": [_kept("c1", 6000, 8000, product="GASOLINE_REG")]}
    result = replace_stripped(trucks, kept, [(_req("ORD-1", 5000), 5000.0)], _block(set()))
    assert sum(a.quantity_liters for a in result.placed["truck-1"]) == 2000.0
    assert result.partial == {"ORD-1": 3000.0}


def test_empty_stripped_returns_empty_result():
    trucks = [_truck("truck-1", [_comp("truck-1", "c1", 6000)])]
    result = replace_stripped(trucks, {}, [], _block(set()))
    assert result.placed == {}
    assert result.unplaced == []
    assert result.partial == {}


def test_deterministic():
    trucks = [
        _truck("truck-a", [_comp("truck-a", "c1", 5000)]),
        _truck("truck-b", [_comp("truck-b", "c1", 5000)]),
    ]
    stripped = [(_req("ORD-1", 3000), 3000.0), (_req("ORD-2", 4000), 4000.0)]

    def strip_ids(r):
        return {
            t: [(a.compartment_id, a.order_id, a.quantity_liters) for a in v]
            for t, v in r.placed.items()
        }

    first = replace_stripped(trucks, {}, stripped, _block(set()))
    second = replace_stripped(trucks, {}, stripped, _block(set()))
    assert strip_ids(first) == strip_ids(second)
    assert first.unplaced == second.unplaced
    assert first.partial == second.partial
    # Equal-capacity trucks are tried in truck_id order.
    assert first.placed["truck-a"][0].order_id == "ORD-1"
