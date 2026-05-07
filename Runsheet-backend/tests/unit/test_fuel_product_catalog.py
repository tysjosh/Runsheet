"""
Unit tests for ``services.fuel_product_catalog``.

Covers the default catalog composition, ``canonicalize`` normalization and
idempotence, legacy Nigerian alias resolution, and ``get_products_for_region``
filtering semantics.

Validates: Requirements 6.1.1, 6.1.2, 6.1.3, 6.1.5.
"""
from __future__ import annotations

import pytest

from fuel.services.fuel_product_catalog import (
    FUEL_PRODUCT_CATALOG,
    FuelProduct,
    UnknownFuelProductError,
    canonicalize,
    get_product,
    get_products_for_region,
    is_known_product,
)


EXPECTED_PRODUCT_CODES = {
    "DIESEL_2",
    "HEATING_OIL",
    "GASOLINE_REG",
    "GASOLINE_PREM",
    "PROPANE",
    "KEROSENE",
    "OFF_ROAD_DIESEL",
    "DEF",
    "ETHANOL_E85",
}

EXPECTED_ALIAS_MAP = {
    "AGO": "DIESEL_2",
    "PMS": "GASOLINE_REG",
    "ATK": "KEROSENE",
    "LPG": "PROPANE",
}


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


class TestCatalogShape:
    def test_catalog_contains_all_required_us_products(self) -> None:
        codes = {p.product_code for p in FUEL_PRODUCT_CATALOG}
        assert codes == EXPECTED_PRODUCT_CODES

    def test_every_entry_has_positive_density(self) -> None:
        for product in FUEL_PRODUCT_CATALOG:
            assert product.density_lbs_per_gallon > 0, product.product_code

    def test_every_entry_lists_at_least_one_region(self) -> None:
        for product in FUEL_PRODUCT_CATALOG:
            assert len(product.region_availability) >= 1, product.product_code

    def test_entry_is_immutable_frozen_model(self) -> None:
        product = FUEL_PRODUCT_CATALOG[0]
        with pytest.raises(Exception):
            # Pydantic v2 frozen models raise on attribute mutation.
            product.display_name = "tampered"  # type: ignore[misc]

    def test_entry_round_trips_through_json(self) -> None:
        # Req 6.1.6 (round-trip property check at the unit-test level).
        for product in FUEL_PRODUCT_CATALOG:
            restored = FuelProduct.model_validate_json(product.model_dump_json())
            assert restored == product


# ---------------------------------------------------------------------------
# canonicalize
# ---------------------------------------------------------------------------


class TestCanonicalize:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("AGO", "DIESEL_2"),
            ("PMS", "GASOLINE_REG"),
            ("ATK", "KEROSENE"),
            ("LPG", "PROPANE"),
        ],
    )
    def test_legacy_nigerian_aliases_resolve_to_us_codes(
        self, alias: str, expected: str
    ) -> None:
        assert canonicalize(alias) == expected

    @pytest.mark.parametrize("code", sorted(EXPECTED_PRODUCT_CODES))
    def test_canonical_codes_return_themselves(self, code: str) -> None:
        assert canonicalize(code) == code

    def test_is_case_insensitive(self) -> None:
        assert canonicalize("ago") == "DIESEL_2"
        assert canonicalize("Diesel_2") == "DIESEL_2"

    def test_trims_whitespace(self) -> None:
        assert canonicalize("  PMS  ") == "GASOLINE_REG"

    def test_is_idempotent(self) -> None:
        # Req 6.1.6: canonicalize(canonicalize(x)) == canonicalize(x).
        for code in ["AGO", "pms", "DIESEL_2", "  LPG  ", "ATK"]:
            once = canonicalize(code)
            twice = canonicalize(once)
            assert once == twice

    def test_unknown_code_raises(self) -> None:
        with pytest.raises(UnknownFuelProductError):
            canonicalize("BIODIESEL_B99")

    def test_unknown_code_raises_is_value_error(self) -> None:
        # Subclass of ValueError so callers that only know stdlib catch it.
        with pytest.raises(ValueError):
            canonicalize("not_a_product")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(UnknownFuelProductError):
            canonicalize("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(UnknownFuelProductError):
            canonicalize("   ")

    def test_non_string_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            canonicalize(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_products_for_region
# ---------------------------------------------------------------------------


class TestGetProductsForRegion:
    def test_us_returns_full_catalog(self) -> None:
        codes = {p.product_code for p in get_products_for_region("US")}
        assert codes == EXPECTED_PRODUCT_CODES

    def test_ng_returns_only_legacy_compatible_products(self) -> None:
        products = get_products_for_region("NG")
        codes = {p.product_code for p in products}
        # The four US products that have legacy NG aliases.
        assert codes == {"DIESEL_2", "GASOLINE_REG", "PROPANE", "KEROSENE"}

    def test_ng_entries_retain_legacy_aliases(self) -> None:
        # Req 6.1.5: NG should surface legacy equivalents as display aliases.
        products = {p.product_code: p for p in get_products_for_region("NG")}
        assert "AGO" in products["DIESEL_2"].aliases
        assert "PMS" in products["GASOLINE_REG"].aliases
        assert "LPG" in products["PROPANE"].aliases
        assert "ATK" in products["KEROSENE"].aliases

    def test_is_case_insensitive(self) -> None:
        assert {p.product_code for p in get_products_for_region("us")} == (
            EXPECTED_PRODUCT_CODES
        )

    def test_unknown_region_returns_empty_list(self) -> None:
        assert get_products_for_region("ZZ") == []

    def test_empty_region_returns_empty_list(self) -> None:
        assert get_products_for_region("") == []

    def test_non_string_region_raises(self) -> None:
        with pytest.raises(TypeError):
            get_products_for_region(None)  # type: ignore[arg-type]

    def test_result_is_a_list_not_a_tuple(self) -> None:
        # Tests defend against accidental mutation of the module-level tuple.
        result = get_products_for_region("US")
        assert isinstance(result, list)
        result.append(result[0])  # must not affect subsequent calls
        again = get_products_for_region("US")
        assert len(again) == len(EXPECTED_PRODUCT_CODES)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_get_product_returns_catalog_entry(self) -> None:
        product = get_product("AGO")
        assert product.product_code == "DIESEL_2"
        assert product.display_name == "Diesel #2"

    def test_get_product_raises_for_unknown_code(self) -> None:
        with pytest.raises(UnknownFuelProductError):
            get_product("BIODIESEL_B99")

    def test_is_known_product_true_for_alias_and_canonical(self) -> None:
        assert is_known_product("AGO") is True
        assert is_known_product("PROPANE") is True

    def test_is_known_product_false_for_unknown(self) -> None:
        assert is_known_product("UNKNOWN") is False
        assert is_known_product("") is False
        assert is_known_product(None) is False  # type: ignore[arg-type]
