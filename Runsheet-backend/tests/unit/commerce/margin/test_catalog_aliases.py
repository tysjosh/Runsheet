"""``fuel_product_catalog.aliases_for`` (margin-feed rack reader alias terms).

The catalog has no ``ULSD`` code (tasks.md item 1 used it as an example), so
the alias round trip is pinned on the real aliased products instead.
"""

from __future__ import annotations

import pytest

from fuel.services.fuel_product_catalog import (
    FUEL_PRODUCT_CATALOG,
    UnknownFuelProductError,
    aliases_for,
    canonicalize,
)


def test_diesel_aliases_include_catalog_and_lower_case_forms():
    assert aliases_for("DIESEL_2") == frozenset({"DIESEL_2", "diesel_2", "AGO", "ago"})


def test_product_without_aliases_has_only_its_code():
    assert aliases_for("HEATING_OIL") == frozenset({"HEATING_OIL", "heating_oil"})


def test_alias_input_resolves_to_the_canonical_set():
    assert aliases_for("ago") == aliases_for("DIESEL_2")


@pytest.mark.parametrize("product", FUEL_PRODUCT_CATALOG, ids=lambda p: p.product_code)
def test_every_alias_maps_back_through_canonicalize(product):
    spellings = aliases_for(product.product_code)
    assert product.product_code in spellings
    for alias in product.aliases:
        assert alias in spellings and alias.lower() in spellings
    assert {canonicalize(s) for s in spellings} == {product.product_code}


def test_alias_sets_do_not_overlap():
    seen: set[str] = set()
    for product in FUEL_PRODUCT_CATALOG:
        spellings = aliases_for(product.product_code)
        assert not (seen & spellings)
        seen |= spellings


def test_built_once_and_immutable():
    assert aliases_for("PROPANE") is aliases_for("PROPANE")
    assert isinstance(aliases_for("PROPANE"), frozenset)


def test_unknown_code_raises():
    with pytest.raises(UnknownFuelProductError):
        aliases_for("ULSD")
