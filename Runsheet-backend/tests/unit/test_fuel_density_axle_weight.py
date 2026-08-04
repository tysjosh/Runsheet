"""
Axle weight must be computed from the exact product, not the coarse grade.

``fuel_product_mapping`` collapses the nine catalog products onto four legacy
Nigerian grades (AGO/PMS/ATK/LPG). For diesel, gasoline, kerosene and propane
that is harmless — family members sit within ~2% of each other. For DEF it is
not: DEF is urea in de-ionised water at 1.09 kg/L, it maps to ``AGO``, and
``AGO`` was 0.85. The weight check compares directly against ``max_weight_kg``
with no safety margin of its own, so the feasibility gate would pass a truck
that is genuinely overweight — a DOT violation the planner reported as legal.

Two density tables also existed. Only the one in ``compartment_loading_agent``
had DEF right, and it was consulted only when recomputing a plan's weight
*after* assignments had been stripped by contamination or dyed-diesel
enforcement. The gate used the other one. The perverse result: the more
heavily filtered a plan was, the more accurate its weight became.

Validates: Requirements 3.3, 3.7
"""

from typing import List, Optional

import pytest

from Agents.support.compartment_models import (
    Compartment,
    DeliveryRequest,
    FuelGrade,
)
from Agents.support.compartment_solver import (
    DEFAULT_FUEL_DENSITY_KG_PER_LITER,
    FUEL_DENSITY,
    FUEL_DENSITY_KG_PER_LITER,
    check_feasibility,
    fuel_density_kg_per_liter,
    optimize_loading_plan,
)
from fuel.services.fuel_product_catalog import FUEL_PRODUCT_CATALOG
from fuel.services.fuel_product_mapping import fuel_product_mapper

#: Exact conversions, so the expected values are derived rather than restated.
KG_PER_LB = 0.45359237
LITERS_PER_US_GALLON = 3.785411784


def _catalog_density_kg_per_liter(lbs_per_gallon: float) -> float:
    return lbs_per_gallon * KG_PER_LB / LITERS_PER_US_GALLON


def _compartments(
    count: int = 2,
    capacity: float = 5000.0,
    allowed_product_codes: Optional[List[str]] = None,
) -> List[Compartment]:
    """Compartments for the weight tests.

    ``allowed_product_codes`` is needed for the DEF cases. A legacy
    ``allowed_grades`` list cannot express DEF eligibility: DEF is its own
    catalog category (``tax_class=non_fuel``) and belongs to no legacy grade
    family, so ``compartment_accepts`` refuses it from a diesel compartment —
    which is what ``compatibility_matrix`` Req 7.2.1 already required ("DEF
    with any non-DEF blocked"). Declaring DEF explicitly is how a tenant says
    this compartment really does carry it.
    """
    return [
        Compartment(
            compartment_id=f"c{i}",
            truck_id="t1",
            capacity_liters=capacity,
            allowed_grades=list(FuelGrade),
            allowed_product_codes=allowed_product_codes,
            position_index=i,
            tenant_id="tenant-1",
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# The guard that would have caught this
# ---------------------------------------------------------------------------


class TestDensityAgreesWithCatalog:
    """The solver's table is pinned to the catalog's own published densities.

    The catalog is the authority: it carries ``density_lbs_per_gallon`` per
    product. The solver keeps rounded kg/L values so existing plans report
    unchanged ``total_weight_kg``, so this asserts agreement within a
    tolerance rather than equality — wide enough for the deliberate rounding,
    far narrower than DEF's 22% error.
    """

    TOLERANCE_PCT = 3.0

    @pytest.mark.parametrize(
        "product",
        FUEL_PRODUCT_CATALOG,
        ids=[p.product_code for p in FUEL_PRODUCT_CATALOG],
    )
    def test_every_catalog_product_has_a_density_within_tolerance(self, product):
        expected = _catalog_density_kg_per_liter(product.density_lbs_per_gallon)
        actual = fuel_density_kg_per_liter(product_code=product.product_code)

        error_pct = abs(actual - expected) / expected * 100.0
        assert error_pct <= self.TOLERANCE_PCT, (
            f"{product.product_code}: solver uses {actual} kg/L, catalog "
            f"implies {expected:.4f} kg/L ({error_pct:.1f}% off). Axle weight "
            "is computed from the solver value."
        )

    def test_every_catalog_product_is_covered(self):
        """A new catalog product must not silently inherit the diesel default."""
        missing = [
            p.product_code
            for p in FUEL_PRODUCT_CATALOG
            if p.product_code.upper() not in FUEL_DENSITY_KG_PER_LITER
        ]
        assert missing == [], (
            f"catalog products with no density entry: {missing}. They would "
            f"be weighed as diesel ({DEFAULT_FUEL_DENSITY_KG_PER_LITER} kg/L)."
        )


# ---------------------------------------------------------------------------
# The specific defect
# ---------------------------------------------------------------------------


class TestDefIsNotWeighedAsDiesel:
    def test_def_maps_to_ago_so_the_grade_cannot_distinguish_it(self):
        """Pins the root cause: by solver time DEF and diesel are both AGO."""
        assert fuel_product_mapper.us_to_fuel_grade("DEF") is FuelGrade.AGO
        assert fuel_product_mapper.us_to_fuel_grade("DIESEL_2") is FuelGrade.AGO
        # ...which is exactly why the product code has to travel separately.
        assert fuel_density_kg_per_liter(product_code="DEF") == pytest.approx(1.09)
        assert fuel_density_kg_per_liter(
            product_code="DIESEL_2"
        ) == pytest.approx(0.85)

    def test_product_code_wins_over_the_collapsed_grade(self):
        """A DEF request carrying grade AGO must still weigh as DEF."""
        assert fuel_density_kg_per_liter(
            product_code="DEF", fuel_grade="AGO"
        ) == pytest.approx(1.09)

    def test_an_overweight_def_load_is_rejected(self):
        """The gate must fail a DEF load that diesel density would have passed.

        3000 L of DEF is 3271 kg. With the 10% volume buffer the solver sees
        3300 L. At DEF's density that is 3597 kg; at diesel's it would be
        2805 kg. A 3000 kg limit must therefore reject it — before the fix it
        was accepted.

        Validates: Requirement 3.7
        """
        request = DeliveryRequest(
            station_id="s1",
            fuel_grade=FuelGrade.AGO,  # what us_to_fuel_grade produces for DEF
            product_code="DEF",
            quantity_liters=3000.0,
        )

        result = check_feasibility(
            # A compartment that genuinely carries DEF, so this test exercises
            # the weight gate rather than stopping at eligibility.
            compartments=_compartments(allowed_product_codes=["DEF"]),
            requests=[request],
            max_weight_kg=3000.0,
        )

        assert result.feasible is False
        assert [v.violation_type for v in result.violations] == ["weight_exceeded"]

    def test_the_same_volume_of_diesel_still_passes(self):
        """The fix must not make diesel loads spuriously infeasible."""
        request = DeliveryRequest(
            station_id="s1",
            fuel_grade=FuelGrade.AGO,
            product_code="DIESEL_2",
            quantity_liters=3000.0,
        )

        result = check_feasibility(
            compartments=_compartments(),
            requests=[request],
            max_weight_kg=3000.0,
        )

        assert result.feasible is True, [v.message for v in result.violations]


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


class TestLegacyBehaviourUnchanged:
    @pytest.mark.parametrize(
        "grade,expected",
        [("AGO", 0.85), ("PMS", 0.74), ("ATK", 0.80), ("LPG", 0.51)],
    )
    def test_legacy_grade_codes_keep_their_old_densities(self, grade, expected):
        """Existing plans must report the same total_weight_kg as before."""
        assert fuel_density_kg_per_liter(fuel_grade=grade) == pytest.approx(
            expected
        )
        assert FUEL_DENSITY[grade] == pytest.approx(expected)

    def test_a_request_without_a_product_code_falls_back_to_the_grade(self):
        """Legacy station-priority requests carry no product code."""
        request = DeliveryRequest(
            station_id="s1",
            fuel_grade=FuelGrade.PMS,
            quantity_liters=1000.0,
        )
        assert request.product_code is None

        result = check_feasibility(
            compartments=_compartments(),
            requests=[request],
            # 1000 L * 1.10 buffer * 0.74 = 814 kg
            max_weight_kg=900.0,
        )
        assert result.feasible is True

    def test_unknown_codes_fall_back_to_diesel_rather_than_raising(self):
        """An unrecognised product must not stop the load being weighed."""
        assert fuel_density_kg_per_liter(
            product_code="NOT_A_PRODUCT"
        ) == pytest.approx(DEFAULT_FUEL_DENSITY_KG_PER_LITER)
        assert fuel_density_kg_per_liter() == pytest.approx(
            DEFAULT_FUEL_DENSITY_KG_PER_LITER
        )
        assert fuel_density_kg_per_liter(product_code=None) == pytest.approx(
            DEFAULT_FUEL_DENSITY_KG_PER_LITER
        )

    def test_codes_are_matched_case_and_whitespace_insensitively(self):
        assert fuel_density_kg_per_liter(
            product_code="  def  "
        ) == pytest.approx(1.09)


# ---------------------------------------------------------------------------
# The product code has to survive the solver
# ---------------------------------------------------------------------------


class TestProductCodeReachesTheAssignment:
    def test_optimize_carries_the_product_code_onto_the_assignment(self):
        """Otherwise a persisted plan cannot be re-weighed correctly.

        Validates: Requirement 3.4
        """
        plan = optimize_loading_plan(
            compartments=_compartments(allowed_product_codes=["DEF"]),
            requests=[
                DeliveryRequest(
                    station_id="s1",
                    order_id="o1",
                    fuel_grade=FuelGrade.AGO,
                    product_code="DEF",
                    quantity_liters=2000.0,
                )
            ],
            truck_id="t1",
            tenant_id="tenant-1",
        )

        assert plan is not None
        assert plan.assignments, "no assignment produced"
        assert {a.product_code for a in plan.assignments} == {"DEF"}

    def test_plan_weight_uses_the_assignment_product_code(self):
        """The reported total_weight_kg must match the gate's arithmetic."""
        plan = optimize_loading_plan(
            compartments=_compartments(count=1, allowed_product_codes=["DEF"]),
            requests=[
                DeliveryRequest(
                    station_id="s1",
                    fuel_grade=FuelGrade.AGO,
                    product_code="DEF",
                    quantity_liters=1000.0,
                )
            ],
            truck_id="t1",
            tenant_id="tenant-1",
        )

        assert plan is not None
        loaded = sum(a.quantity_liters for a in plan.assignments)
        assert plan.total_weight_kg == pytest.approx(loaded * 1.09, rel=1e-3)
