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
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    get_product,
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


#: Which catalog categories each legacy Nigerian grade was used to mean.
#:
#: A compartment recorded as ``AGO``-eligible predates the US catalog, so we
#: cannot know whether the operator meant road diesel, off-road diesel or
#: heating oil. Narrowing it to one product would make existing plans
#: infeasible; widening it to "anything AGO maps to" is what
#: ``fuel_product_mapping`` did. So a legacy grade keeps *family* eligibility
#: while segregation (below) is product-exact.
#:
#: ``DEF`` is deliberately absent from every family. ``fuel_product_mapping``
#: maps DEF onto AGO, which made DEF loadable into a diesel compartment. DEF is
#: urea in de-ionised water — ``tax_class=non_fuel``, ``category=def`` — and
#: mixing it with diesel is a contamination event, not a grade substitution.
#: A compartment must name DEF explicitly via ``allowed_product_codes``.
#:
#: Derived against ``FUEL_PRODUCT_CATALOG.category`` rather than listing
#: product codes, so a tenth product joins the right family automatically.
#: ``test_legacy_families_cover_the_catalog`` pins that every catalog product
#: except DEF belongs to exactly one family.
LEGACY_GRADE_CATEGORIES: Dict[str, frozenset] = {
    "AGO": frozenset({"diesel", "off_road", "heating_oil"}),
    "PMS": frozenset({"gasoline", "ethanol"}),
    "ATK": frozenset({"kerosene"}),
    "LPG": frozenset({"propane"}),
}


def legacy_grade_for_product(product_code: str) -> Optional[str]:
    """The legacy NG grade whose family contains ``product_code``, if any.

    The inverse of :data:`LEGACY_GRADE_CATEGORIES`. Used when hydrating a
    compartment whose stored ``allowed_grades`` already holds US product codes:
    the model still requires at least one legacy grade, so we derive the family
    rather than dropping the compartment. Returns ``None`` for isolates like
    DEF that belong to no family.
    """
    try:
        category = get_product(product_code).category
    except (UnknownFuelProductError, TypeError):
        return None
    for grade, categories in LEGACY_GRADE_CATEGORIES.items():
        if category in categories:
            return grade
    return None


def segregation_key(
    product_code: Optional[str] = None,
    fuel_grade: Optional[str] = None,
) -> str:
    """The identity two deliveries must share to occupy one compartment.

    Prefers the canonical product code, mirroring
    :func:`fuel_density_kg_per_liter`. This is what makes grade segregation
    absolute in US terms: ``DIESEL_2`` and ``HEATING_OIL`` both collapse to the
    ``AGO`` grade, so keying on the grade let taxed road diesel and untaxed
    dyed heating oil share a compartment. They are different products with
    different tax classes and must not be co-loaded.

    Falls back to the upper-cased grade for legacy requests that carry no
    product code, which preserves their existing behaviour exactly.
    """
    if isinstance(product_code, str) and product_code.strip():
        try:
            return canonicalize(product_code)
        except (UnknownFuelProductError, TypeError):
            # An unrecognised code still segregates from everything else by its
            # own normalised name — safer than silently merging it into a grade.
            return product_code.strip().upper()
    if isinstance(fuel_grade, str) and fuel_grade.strip():
        # Canonicalize the grade too, so a legacy request carrying only
        # ``AGO`` shares a compartment with a ``DIESEL_2`` request rather than
        # being segregated from the same physical product on a naming
        # difference. ``HEATING_OIL`` still segregates from both.
        try:
            return canonicalize(fuel_grade)
        except (UnknownFuelProductError, TypeError):
            return fuel_grade.strip().upper()
    return ""


def compartment_accepts(
    compartment: Compartment,
    product_code: Optional[str] = None,
    fuel_grade: Optional[str] = None,
) -> bool:
    """Whether ``compartment`` is eligible to carry this product at all.

    Eligibility and segregation are separate questions. This answers the first:
    *could* this compartment ever hold this product. Segregation — whether two
    products may share it simultaneously — is answered by
    :func:`segregation_key`, and is always product-exact.

    * ``allowed_product_codes`` set: exact canonical match. The tenant has told
      us precisely what this compartment takes.
    * otherwise: family match through :data:`LEGACY_GRADE_CATEGORIES`, because
      a legacy ``allowed_grades`` list records a family and nothing finer.
    """
    key = segregation_key(product_code=product_code, fuel_grade=fuel_grade)
    if not key:
        return False

    if compartment.allowed_product_codes:
        try:
            return canonicalize(key) in compartment.allowed_product_codes
        except (UnknownFuelProductError, TypeError):
            return False

    # Legacy family eligibility.
    try:
        category = get_product(key).category
    except (UnknownFuelProductError, TypeError):
        # Not a catalog product (e.g. a bare legacy grade like "AGO"): fall back
        # to the original exact-grade comparison so behaviour is unchanged.
        return any(g.value == key for g in compartment.allowed_grades)

    for grade in compartment.allowed_grades:
        if category in LEGACY_GRADE_CATEGORIES.get(grade.value, frozenset()):
            return True
    return False


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

    # Per-product capacity check. Keyed on the segregation key, not the grade:
    # DIESEL_2 and HEATING_OIL both collapse to AGO, so a grade-keyed check
    # pooled their capacity and could report a feasible plan that cannot
    # actually be loaded without co-mingling two tax classes.
    product_demands: Dict[str, float] = {}
    for req in buffered_requests:
        key = segregation_key(
            product_code=req.product_code, fuel_grade=req.fuel_grade.value
        )
        product_demands[key] = product_demands.get(key, 0) + req.quantity_liters

    for key, demand in product_demands.items():
        compatible = [c for c in compartments if compartment_accepts(c, key)]
        if not compatible:
            violations.append(ConstraintViolation(
                violation_type="no_compatible_compartments",
                fuel_grade=key,
                message=f"No compartments support product {key}",
            ))
            continue
        cap = sum(c.capacity_liters for c in compatible)
        if demand > cap:
            violations.append(ConstraintViolation(
                violation_type="capacity_shortfall",
                fuel_grade=key,
                shortfall_liters=demand - cap,
                message=f"Product {key} needs {demand:.0f}L, only {cap:.0f}L available",
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

    # Group by segregation key so two products in the same legacy family are
    # planned separately. Grouping by grade let DIESEL_2 and HEATING_OIL share
    # a compartment, which Req 3.2 ("no compartment may carry more than one
    # fuel grade simultaneously") forbids once "grade" means a US product.
    product_demands: Dict[str, List[DeliveryRequest]] = {}
    for req in requests:
        key = segregation_key(
            product_code=req.product_code, fuel_grade=req.fuel_grade.value
        )
        product_demands.setdefault(key, []).append(req)

    compartment_state = {c.compartment_id: (None, c.capacity_liters) for c in compartments}
    assignments = []

    for key, reqs in product_demands.items():
        compatible = sorted(
            [c for c in compartments if compartment_accepts(c, key)],
            key=lambda c: c.capacity_liters, reverse=True,
        )
        for req in reqs:
            remaining = req.quantity_liters * buffer_mult
            for comp in compatible:
                cid = comp.compartment_id
                assigned_key, cap_remaining = compartment_state[cid]
                # Product-exact: a compartment already holding DIESEL_2 is
                # closed to HEATING_OIL even though both are AGO-eligible.
                if assigned_key is not None and assigned_key != key:
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
                    fuel_grade=req.fuel_grade.value,
                    product_code=req.product_code,
                    quantity_liters=round(assign_qty, 2),
                    compartment_capacity_liters=comp.capacity_liters,
                ))
                compartment_state[cid] = (key, cap_remaining - assign_qty)
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
