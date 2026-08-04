"""
`tax_class` describes tax treatment, and so does the dyed-diesel enforcer.

`FuelProduct.tax_class` (Requirement 6.1.1) is currently read by nothing except
`GET /api/fuel/products`. No business logic consults it: the tax engine keys on
`product_code` against each rate row's `product_codes` list, and the dyed-diesel
enforcer keys on its own `DYED_DIESEL_PRODUCT_CODES` frozenset.

That leaves two independent, unvalidated descriptions of the same fact. This
module makes the field load-bearing for drift detection rather than deleting it
(the requirement mandates it) by pinning:

* `tax_class` values to a closed vocabulary, so a typo like ``"off-road"``
  cannot slip in unnoticed, and
* the relationship between the catalog's ``off_road`` products and the dyed
  set, so changing either list forces a deliberate decision about the other.

Validates: Requirement 6.1.1
"""

import pytest

from compliance.services.dyed_diesel_enforcer import DYED_DIESEL_PRODUCT_CODES
from fuel.services.fuel_product_catalog import FUEL_PRODUCT_CATALOG

#: The closed vocabulary the shipped catalog uses. Adding a value here should be
#: a deliberate act, because tax treatment is the thing being named.
KNOWN_TAX_CLASSES = frozenset(
    {
        "road_diesel",
        "off_road",
        "gasoline",
        "propane",
        "kerosene",
        "non_fuel",
    }
)


class TestTaxClassVocabulary:
    @pytest.mark.parametrize(
        "product",
        FUEL_PRODUCT_CATALOG,
        ids=[p.product_code for p in FUEL_PRODUCT_CATALOG],
    )
    def test_tax_class_is_from_the_known_vocabulary(self, product):
        assert product.tax_class in KNOWN_TAX_CLASSES, (
            f"{product.product_code} has tax_class={product.tax_class!r}, "
            "which is not a recognised value. `tax_class` is a plain str on the "
            "model, so a typo is not caught at construction."
        )

    def test_every_product_declares_a_tax_class(self):
        missing = [
            p.product_code for p in FUEL_PRODUCT_CATALOG if not p.tax_class
        ]
        assert missing == []


class TestDyedSetAgreesWithTheCatalog:
    """The dyed set and the catalog's `off_road` class must not drift silently.

    Both describe "this product is not subject to road-use tax", from two
    different modules, with no link between them.
    """

    def test_every_dyed_catalog_product_is_classed_off_road(self):
        """A product the enforcer treats as dyed must not be road-taxed.

        This is the direction that carries money: if the enforcer excludes road
        excise for a product the catalog calls ``road_diesel``, the two are
        contradicting each other about a taxable sale.
        """
        contradictions = [
            p.product_code
            for p in FUEL_PRODUCT_CATALOG
            if p.product_code.upper() in DYED_DIESEL_PRODUCT_CODES
            and p.tax_class != "off_road"
        ]
        assert contradictions == [], (
            f"{contradictions} are treated as dyed by DyedDieselEnforcer but "
            "are not classed off_road in the catalog."
        )

    def test_the_heating_oil_divergence_is_deliberate(self):
        """HEATING_OIL is `off_road` in the catalog but not in the dyed set.

        Pinned rather than resolved, because it is a tax question rather than a
        code question and it has legal consequence:

        * US No. 2 heating oil is dyed red, the same marker as off-road diesel,
          and is not subject to federal road-use tax — which is what the
          catalog's ``off_road`` class asserts.
        * But `DyedDieselEnforcer` drives IRS 637M exemption checks and the
          dyed-sale audit record. Whether heating-oil sales belong in that audit
          trail, or in a separate heating-oil regime, is a domain decision.

        So `is_dyed_diesel("HEATING_OIL")` is False today, meaning a heating-oil
        invoice gets neither the road-excise exclusion check nor a dyed-sale
        audit record. If that is wrong, add HEATING_OIL to
        DYED_DIESEL_PRODUCT_CODES and this test will tell you to update it.
        """
        heating_oil = next(
            p for p in FUEL_PRODUCT_CATALOG if p.product_code == "HEATING_OIL"
        )

        assert heating_oil.tax_class == "off_road"
        assert "HEATING_OIL" not in DYED_DIESEL_PRODUCT_CODES, (
            "HEATING_OIL was added to the dyed set. That is a defensible "
            "change, but it also means heating-oil sales now generate IRS "
            "dyed-sale audit records — confirm that is intended, then update "
            "this test."
        )

    def test_dyed_codes_outside_the_catalog_are_aliases_not_products(self):
        """The dyed set names codes the catalog does not ship.

        DYED_DIESEL / DYED_ULSD / OFF_ROAD_ULSD are accepted as inbound aliases
        on customer or ERP data; only OFF_ROAD_DIESEL is a shipped catalog
        product. Pinned so a future catalog addition does not accidentally
        collide with an alias.
        """
        catalog_codes = {p.product_code.upper() for p in FUEL_PRODUCT_CATALOG}
        aliases = DYED_DIESEL_PRODUCT_CODES - catalog_codes

        assert aliases == {"DYED_DIESEL", "DYED_ULSD", "OFF_ROAD_ULSD"}
        assert DYED_DIESEL_PRODUCT_CODES & catalog_codes == {"OFF_ROAD_DIESEL"}
