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

# Fuel density in kg/liter (approximate)
FUEL_DENSITY: Dict[str, float] = {
    "AGO": 0.85,
    "PMS": 0.74,
    "ATK": 0.80,
    "LPG": 0.51,
}

DEFAULT_UNCERTAINTY_BUFFER_PCT = 10.0


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
            density = FUEL_DENSITY.get(req.fuel_grade.value, 0.85)
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
                    fuel_grade=grade.value,
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
        density = FUEL_DENSITY.get(a.fuel_grade, 0.85)
        total_weight += a.quantity_liters * density

    return LoadingPlan(
        truck_id=truck_id,
        assignments=assignments,
        total_utilization_pct=utilization,
        unserved_demand_liters=round(unserved, 2),
        total_weight_kg=round(total_weight, 2),
        tenant_id=tenant_id,
    )
