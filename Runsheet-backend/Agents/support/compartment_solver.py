"""
Compartment solver — feasibility and optimization.

Pure functions. No side effects.

Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""
from typing import Dict, List, Optional, Tuple

from Agents.support.compartment_models import (
    Compartment, CompartmentAssignment, ConstraintViolation,
    DeliveryRequest, FeasibilityResult, FuelGrade, LoadingPlan,
)

#: Fuel density in kg/litre, keyed on canonical US product code, with the
#: legacy Nigerian grade codes retained as aliases.
#:
#: This is the ONE density table used for axle weight. It previously did not
#: exist: weight was computed from a four-entry table keyed on ``FuelGrade``
#: (AGO/PMS/ATK/LPG), and ``fuel_product_mapping`` collapses nine catalog
#: products onto those four. For diesel, gasoline, kerosene and propane the
#: collapse is harmless — the members of each family are within ~2% of each
#: other. For DEF it is not: DEF is urea in de-ionised water at 1.09 kg/L, it
#: maps to AGO, and AGO is 0.85, so a DEF load was weighed 22% light. The
#: weight check compares directly against ``max_weight_kg``, so the
#: feasibility gate could pass a truck that is actually overweight.
#:
#: The gasoline/diesel/kerosene/propane values deliberately match the old
#: table so plans keep reporting the same ``total_weight_kg`` for the same
#: assignments; only products the old table could not express are new.
#: ``test_fuel_density_matches_product_catalog`` pins every value against
#: ``FUEL_PRODUCT_CATALOG.density_lbs_per_gallon`` so a future divergence of
#: DEF's magnitude fails rather than ships.
FUEL_DENSITY_KG_PER_LITER: Dict[str, float] = {
    # Canonical US product codes (fuel_product_catalog)
    "DIESEL_2": 0.85,
    "OFF_ROAD_DIESEL": 0.85,
    "HEATING_OIL": 0.85,
    "GASOLINE_REG": 0.74,
    "GASOLINE_PREM": 0.74,
    "ETHANOL_E85": 0.78,
    "KEROSENE": 0.80,
    "PROPANE": 0.51,
    "DEF": 1.09,
    # Legacy Nigerian grade codes. Retained so plans persisted before
    # canonicalization, and requests that carry no product_code, still weigh
    # something sensible.
    "AGO": 0.85,
    "PMS": 0.74,
    "ATK": 0.80,
    "LPG": 0.51,
}

#: Density applied when neither the product code nor the grade is recognised.
#: Diesel, the most common product — matching the previous behaviour.
DEFAULT_FUEL_DENSITY_KG_PER_LITER = 0.85

#: Deprecated alias. Kept because ``Agents.support.__init__`` re-exports it.
#: Use :func:`fuel_density_kg_per_liter`, which understands product codes.
FUEL_DENSITY: Dict[str, float] = {
    "AGO": FUEL_DENSITY_KG_PER_LITER["AGO"],
    "PMS": FUEL_DENSITY_KG_PER_LITER["PMS"],
    "ATK": FUEL_DENSITY_KG_PER_LITER["ATK"],
    "LPG": FUEL_DENSITY_KG_PER_LITER["LPG"],
}

DEFAULT_UNCERTAINTY_BUFFER_PCT = 10.0


def fuel_density_kg_per_liter(
    product_code: Optional[str] = None,
    fuel_grade: Optional[str] = None,
) -> float:
    """Density for a delivery, preferring the canonical product code.

    ``product_code`` is checked first because it is the only identifier that
    distinguishes DEF from diesel — by the time a delivery reaches the solver
    as a :class:`FuelGrade`, ``us_to_fuel_grade`` has already mapped both to
    ``AGO``. ``fuel_grade`` is the fallback for legacy requests and for plans
    persisted before ``product_code`` was carried through.

    Unknown codes fall back to diesel rather than raising: an unrecognised
    product must not stop a plan from being weighed at all, and a wrong-by-2%
    diesel assumption is safer than no weight check.
    """
    for candidate in (product_code, fuel_grade):
        if not isinstance(candidate, str):
            continue
        key = candidate.strip().upper()
        if key in FUEL_DENSITY_KG_PER_LITER:
            return FUEL_DENSITY_KG_PER_LITER[key]
    return DEFAULT_FUEL_DENSITY_KG_PER_LITER


def check_feasibility(
    compartments: List[Compartment],
    requests: List[DeliveryRequest],
    max_weight_kg: Optional[float] = None,
    tare_weight_kg: float = 0.0,
    uncertainty_buffer_pct: float = DEFAULT_UNCERTAINTY_BUFFER_PCT,
) -> FeasibilityResult:
    """Check feasibility with grade, capacity, min-drop, and weight constraints."""
    violations = []
    buffer_mult = 1.0 + (uncertainty_buffer_pct / 100.0)

    # Apply uncertainty buffer to demands
    buffered_requests = []
    for req in requests:
        buffered_qty = req.quantity_liters * buffer_mult
        buffered_requests.append(req.model_copy(
            update={"quantity_liters": buffered_qty}
        ))

    total_capacity = sum(c.capacity_liters for c in compartments)
    total_requested = sum(r.quantity_liters for r in buffered_requests)

    # Total capacity check
    if total_requested > total_capacity:
        violations.append(ConstraintViolation(
            violation_type="total_overage",
            shortfall_liters=total_requested - total_capacity,
            message=f"Total requested {total_requested:.0f}L exceeds capacity {total_capacity:.0f}L",
        ))

    # Per-grade capacity check
    grade_demands: Dict[FuelGrade, float] = {}
    for req in buffered_requests:
        grade_demands[req.fuel_grade] = grade_demands.get(req.fuel_grade, 0) + req.quantity_liters

    for grade, demand in grade_demands.items():
        compatible = [c for c in compartments if grade in c.allowed_grades]
        if not compatible:
            violations.append(ConstraintViolation(
                violation_type="no_compatible_compartments",
                fuel_grade=grade.value,
                message=f"No compartments support grade {grade.value}",
            ))
            continue
        cap = sum(c.capacity_liters for c in compatible)
        if demand > cap:
            violations.append(ConstraintViolation(
                violation_type="capacity_shortfall",
                fuel_grade=grade.value,
                shortfall_liters=demand - cap,
                message=f"Grade {grade.value} needs {demand:.0f}L, only {cap:.0f}L available",
            ))

    # Weight check
    if max_weight_kg is not None and not violations:
        total_weight = tare_weight_kg
        for req in buffered_requests:
            density = fuel_density_kg_per_liter(
                product_code=req.product_code,
                fuel_grade=req.fuel_grade.value,
            )
            total_weight += req.quantity_liters * density
        if total_weight > max_weight_kg:
            violations.append(ConstraintViolation(
                violation_type="weight_exceeded",
                shortfall_liters=0,
                message=f"Total weight {total_weight:.0f}kg exceeds limit {max_weight_kg:.0f}kg",
            ))

    # Min drop check
    for req in requests:
        if req.quantity_liters < req.min_drop_liters:
            violations.append(ConstraintViolation(
                violation_type="below_min_drop",
                fuel_grade=req.fuel_grade.value,
                shortfall_liters=req.min_drop_liters - req.quantity_liters,
                message=f"Station {req.station_id} requests {req.quantity_liters:.0f}L, below min {req.min_drop_liters:.0f}L",
            ))

    if violations:
        return FeasibilityResult(feasible=False, violations=violations)

    utilization = round((total_requested / total_capacity) * 100, 2) if total_capacity > 0 else 0.0
    return FeasibilityResult(feasible=True, max_utilization_pct=utilization)


def optimize_loading_plan(
    compartments: List[Compartment],
    requests: List[DeliveryRequest],
    truck_id: str,
    tenant_id: str,
    uncertainty_buffer_pct: float = DEFAULT_UNCERTAINTY_BUFFER_PCT,
) -> Optional[LoadingPlan]:
    """Greedy largest-first loading plan with uncertainty buffer."""
    buffer_mult = 1.0 + (uncertainty_buffer_pct / 100.0)

    grade_demands: Dict[FuelGrade, List[DeliveryRequest]] = {}
    for req in requests:
        grade_demands.setdefault(req.fuel_grade, []).append(req)

    compartment_state = {c.compartment_id: (None, c.capacity_liters) for c in compartments}
    assignments = []

    for grade, reqs in grade_demands.items():
        compatible = sorted(
            [c for c in compartments if grade in c.allowed_grades],
            key=lambda c: c.capacity_liters, reverse=True,
        )
        for req in reqs:
            remaining = req.quantity_liters * buffer_mult
            for comp in compatible:
                cid = comp.compartment_id
                assigned_grade, cap_remaining = compartment_state[cid]
                if assigned_grade is not None and assigned_grade != grade:
                    continue
                if cap_remaining <= 0:
                    continue
                assign_qty = min(remaining, cap_remaining)
                if assign_qty <= 0:
                    continue
                assignments.append(CompartmentAssignment(
                    compartment_id=cid,
                    station_id=req.station_id,
                    order_id=req.order_id,
                    fuel_grade=grade.value,
                    product_code=req.product_code,
                    quantity_liters=round(assign_qty, 2),
                    compartment_capacity_liters=comp.capacity_liters,
                ))
                compartment_state[cid] = (grade, cap_remaining - assign_qty)
                remaining -= assign_qty
                if remaining <= 0:
                    break
            # remaining > 0 means partial fulfillment — tracked as unserved

    total_loaded = sum(a.quantity_liters for a in assignments)
    total_capacity = sum(c.capacity_liters for c in compartments)
    total_requested = sum(r.quantity_liters * buffer_mult for r in requests)
    unserved = max(0, total_requested - total_loaded)
    utilization = round((total_loaded / total_capacity) * 100, 2) if total_capacity > 0 else 0.0

    # Compute weight
    total_weight = 0.0
    for a in assignments:
        density = fuel_density_kg_per_liter(
            product_code=a.product_code,
            fuel_grade=a.fuel_grade,
        )
        total_weight += a.quantity_liters * density

    return LoadingPlan(
        truck_id=truck_id,
        assignments=assignments,
        total_utilization_pct=utilization,
        unserved_demand_liters=round(unserved, 2),
        total_weight_kg=round(total_weight, 2),
        tenant_id=tenant_id,
    )
