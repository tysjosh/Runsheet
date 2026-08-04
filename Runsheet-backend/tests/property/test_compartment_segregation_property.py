"""Grade segregation is absolute in US product terms, not Nigerian grade terms.

MVP Requirement 3.2 calls segregation absolute: "no compartment may carry more
than one fuel grade simultaneously". That held when "grade" meant one of four
Nigerian codes. Under the US catalog it did not, because the solver keyed
segregation on ``FuelGrade`` while nine catalog products collapse onto four
grades:

    DIESEL_2, OFF_ROAD_DIESEL, HEATING_OIL, DEF   -> AGO
    GASOLINE_REG, GASOLINE_PREM, ETHANOL_E85      -> PMS
    KEROSENE                                      -> ATK
    PROPANE                                       -> LPG

So a plan could co-load ``DIESEL_2`` (tax_class ``road_diesel``) with
``HEATING_OIL`` (tax_class ``off_road``) in one compartment and report itself
feasible. Those are different products with different tax treatment — dyed
untaxed fuel and taxed road fuel — and the platform's own
``compatibility_matrix`` (Requirement 7.2.1) already blocks the pairing.

The properties below are stated over the whole catalog rather than over a
hand-picked pair, because the defect was a *class* of pairing, not one instance.
"""

from __future__ import annotations

from typing import List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from Agents.support.compartment_models import Compartment, DeliveryRequest
from Agents.support.compartment_solver import (
    LEGACY_GRADE_CATEGORIES,
    compartment_accepts,
    optimize_loading_plan,
    segregation_key,
)
from Agents.support.fuel_distribution_models import FuelGrade
from fuel.services.fuel_product_catalog import (
    FUEL_PRODUCT_CATALOG,
    canonicalize,
    get_product,
)

ALL_PRODUCTS = [p.product_code for p in FUEL_PRODUCT_CATALOG]

#: Every product the legacy grade families can express. DEF is excluded by
#: design — it belongs to no family and must be named explicitly.
FAMILY_PRODUCTS = [
    p.product_code
    for p in FUEL_PRODUCT_CATALOG
    if any(p.category in cats for cats in LEGACY_GRADE_CATEGORIES.values())
]

products = st.sampled_from(ALL_PRODUCTS)
quantities = st.floats(min_value=600.0, max_value=4000.0, allow_nan=False)


def _universal_compartments(count: int = 4, capacity: float = 6000.0):
    """Compartments eligible for every catalog product.

    Declared via ``allowed_product_codes`` so eligibility is never the reason a
    property fails — these tests are about segregation, not eligibility.
    """
    return [
        Compartment(
            compartment_id=f"c{i}",
            truck_id="t1",
            capacity_liters=capacity,
            allowed_grades=list(FuelGrade),
            allowed_product_codes=ALL_PRODUCTS,
            position_index=i,
            tenant_id="tenant-1",
        )
        for i in range(count)
    ]


def _request(product_code: str, qty: float, station: str) -> DeliveryRequest:
    """A request whose grade is the collapsed value the old code keyed on."""
    canonical = canonicalize(product_code)
    category = get_product(canonical).category
    grade = next(
        (
            FuelGrade(g)
            for g, cats in LEGACY_GRADE_CATEGORIES.items()
            if category in cats
        ),
        FuelGrade.AGO,  # DEF and anything else family-less
    )
    return DeliveryRequest(
        station_id=station,
        fuel_grade=grade,
        product_code=canonical,
        quantity_liters=qty,
        min_drop_liters=500.0,
    )


# ---------------------------------------------------------------------------
# The core invariant
# ---------------------------------------------------------------------------


class TestSegregationIsProductExact:
    @settings(max_examples=200, deadline=None)
    @given(
        picks=st.lists(products, min_size=2, max_size=5, unique=True),
        qty=quantities,
    )
    def test_no_compartment_ever_holds_two_products(
        self, picks: List[str], qty: float
    ) -> None:
        """The property the defect violated.

        Any set of distinct catalog products, planned onto compartments that
        accept all of them, must never share a compartment — regardless of
        whether they collapse onto the same legacy grade.
        """
        requests = [
            _request(code, qty, f"s{i}") for i, code in enumerate(picks)
        ]
        plan = optimize_loading_plan(
            compartments=_universal_compartments(count=len(picks) + 2),
            requests=requests,
            truck_id="t1",
            tenant_id="tenant-1",
        )
        assert plan is not None

        per_compartment: dict[str, set[str]] = {}
        for a in plan.assignments:
            key = segregation_key(
                product_code=a.product_code, fuel_grade=a.fuel_grade
            )
            per_compartment.setdefault(a.compartment_id, set()).add(key)

        offenders = {
            cid: sorted(keys)
            for cid, keys in per_compartment.items()
            if len(keys) > 1
        }
        assert offenders == {}, f"co-mingled compartments: {offenders}"

    @settings(max_examples=100, deadline=None)
    @given(qty=quantities)
    def test_road_diesel_never_shares_with_off_road(self, qty: float) -> None:
        """The tax-class case, stated directly.

        DIESEL_2 is ``road_diesel``; HEATING_OIL and OFF_ROAD_DIESEL are
        ``off_road`` (dyed, untaxed for highway use). Co-loading them is an
        excise problem, not just a quality one.
        """
        requests = [
            _request("DIESEL_2", qty, "s1"),
            _request("HEATING_OIL", qty, "s2"),
            _request("OFF_ROAD_DIESEL", qty, "s3"),
        ]
        plan = optimize_loading_plan(
            compartments=_universal_compartments(count=5),
            requests=requests,
            truck_id="t1",
            tenant_id="tenant-1",
        )
        assert plan is not None

        by_compartment: dict[str, set[str]] = {}
        for a in plan.assignments:
            by_compartment.setdefault(a.compartment_id, set()).add(
                get_product(a.product_code or a.fuel_grade).tax_class
            )
        mixed = {c: t for c, t in by_compartment.items() if len(t) > 1}
        assert mixed == {}, f"tax classes co-loaded: {mixed}"


# ---------------------------------------------------------------------------
# Eligibility, which is a separate question from segregation
# ---------------------------------------------------------------------------


class TestLegacyEligibilityIsPreserved:
    @pytest.mark.parametrize("product_code", FAMILY_PRODUCTS)
    def test_family_products_still_fit_their_legacy_compartment(
        self, product_code: str
    ) -> None:
        """Narrowing eligibility would make existing plans infeasible.

        A compartment recorded as AGO-eligible predates the catalog, so it keeps
        family eligibility. Only *segregation* became stricter.
        """
        category = get_product(product_code).category
        grade = next(
            g for g, cats in LEGACY_GRADE_CATEGORIES.items() if category in cats
        )
        compartment = Compartment(
            compartment_id="c1",
            truck_id="t1",
            capacity_liters=5000.0,
            allowed_grades=[FuelGrade(grade)],
            position_index=0,
            tenant_id="tenant-1",
        )
        assert compartment_accepts(compartment, product_code) is True

    @pytest.mark.parametrize("grade", sorted(LEGACY_GRADE_CATEGORIES))
    def test_def_is_refused_by_every_legacy_family(self, grade: str) -> None:
        """DEF is urea in de-ionised water, not a fuel grade.

        ``fuel_product_mapping`` mapped DEF onto AGO, which made it loadable
        into a diesel compartment. ``compatibility_matrix`` Req 7.2.1 blocks DEF
        against every non-DEF product; this pins the loading solver to the same
        rule.
        """
        compartment = Compartment(
            compartment_id="c1",
            truck_id="t1",
            capacity_liters=5000.0,
            allowed_grades=[FuelGrade(grade)],
            position_index=0,
            tenant_id="tenant-1",
        )
        assert compartment_accepts(compartment, "DEF") is False

    def test_def_is_accepted_when_named_explicitly(self) -> None:
        compartment = Compartment(
            compartment_id="c1",
            truck_id="t1",
            capacity_liters=5000.0,
            allowed_grades=[FuelGrade.AGO],
            allowed_product_codes=["DEF"],
            position_index=0,
            tenant_id="tenant-1",
        )
        assert compartment_accepts(compartment, "DEF") is True
        # ...and declaring DEF closes the compartment to diesel.
        assert compartment_accepts(compartment, "DIESEL_2") is False


# ---------------------------------------------------------------------------
# Guards on the derivation itself
# ---------------------------------------------------------------------------


class TestFamilyDerivation:
    def test_legacy_families_cover_the_catalog_except_def(self) -> None:
        """A tenth product must land in a family or be a deliberate isolate."""
        covered = set(FAMILY_PRODUCTS)
        uncovered = {p.product_code for p in FUEL_PRODUCT_CATALOG} - covered
        assert uncovered == {"DEF"}, (
            "catalog products belong to no legacy grade family: "
            f"{sorted(uncovered)}. Either add the category to "
            "LEGACY_GRADE_CATEGORIES or confirm it is an isolate like DEF."
        )

    def test_no_product_belongs_to_two_families(self) -> None:
        for product in FUEL_PRODUCT_CATALOG:
            families = [
                g
                for g, cats in LEGACY_GRADE_CATEGORIES.items()
                if product.category in cats
            ]
            assert len(families) <= 1, (
                f"{product.product_code} maps to several legacy grades "
                f"({families}); eligibility would be ambiguous"
            )

    def test_segregation_key_is_idempotent(self) -> None:
        for code in ALL_PRODUCTS:
            once = segregation_key(product_code=code)
            assert segregation_key(product_code=once) == once

    def test_legacy_alias_and_canonical_code_share_a_key(self) -> None:
        """Otherwise the same physical product would be split across compartments."""
        assert segregation_key(fuel_grade="AGO") == segregation_key(
            product_code="DIESEL_2"
        )
        assert segregation_key(fuel_grade="LPG") == segregation_key(
            product_code="PROPANE"
        )
