"""
Fuel Product Catalog — US fuel-marketer defaults with Nigerian-alias backwards
compatibility.

This module is the authoritative list of fuel products the Runsheet platform
ships with out of the box. It replaces the legacy Nigerian-only AGO/PMS/ATK/LPG
enum with the nine US-marketer products mandated by Capability 6: DIESEL_2,
HEATING_OIL, GASOLINE_REG, GASOLINE_PREM, PROPANE, KEROSENE, OFF_ROAD_DIESEL,
DEF, and ETHANOL_E85.

Key responsibilities:

* Expose a strongly-typed :class:`FuelProduct` Pydantic model that every other
  subsystem can import rather than re-declaring shapes.
* Ship :data:`FUEL_PRODUCT_CATALOG` as the default catalog for a fresh tenant.
* Provide :func:`canonicalize` so callers can accept either a US product_code
  or a legacy Nigerian alias on input and persist a single canonical value
  (Requirement 6.1.4). Canonicalization is case-insensitive and whitespace-
  tolerant and is idempotent: ``canonicalize(canonicalize(x)) == canonicalize(x)``.
* Provide :func:`get_products_for_region` so the `GET /api/fuel/products`
  endpoint can serve a region-filtered view for US and NG tenants, with NG
  tenants seeing the subset that includes legacy-alias equivalents.

Validates: Requirements 6.1.1, 6.1.2, 6.1.3, 6.1.5.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


_module_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: Coarse grouping used by contamination rules and reporting (Requirement 7.2).
FuelCategory = Literal[
    "gasoline",
    "diesel",
    "heating_oil",
    "propane",
    "kerosene",
    "off_road",
    "def",
    "ethanol",
]


#: Regions the platform currently recognizes. US ships the full nine-product
#: catalog; NG ships the subset that maps to the legacy AGO/PMS/ATK/LPG enum.
RegionCode = Literal["US", "NG"]


class FuelProduct(BaseModel):
    """A single entry in the fuel product catalog.

    Fields mirror Requirement 6.1.1 (stable product codes, display names,
    density_lbs_per_gallon, tax_class, region_availability) and add a coarse
    :attr:`category` for downstream contamination-rule lookups.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_code: str = Field(
        ..., description="Stable uppercase identifier, e.g. DIESEL_2 or GASOLINE_REG."
    )
    display_name: str = Field(
        ..., description="Human-readable label surfaced to drivers and customers."
    )
    category: FuelCategory = Field(
        ...,
        description=(
            "Coarse product family used by contamination rules and reporting."
        ),
    )
    density_lbs_per_gallon: float = Field(
        ..., gt=0, description="Typical US density at 60°F, in lbs per gallon."
    )
    tax_class: str = Field(
        ...,
        description=(
            "Tax treatment bucket (road_diesel, off_road, gasoline, propane, "
            "kerosene, non_fuel)."
        ),
    )
    aliases: Tuple[str, ...] = Field(
        default=(),
        description=(
            "Legacy codes (e.g. AGO, PMS, ATK, LPG) that resolve to this product "
            "for backwards compatibility with region=NG tenants."
        ),
    )
    region_availability: Tuple[RegionCode, ...] = Field(
        ...,
        description=(
            "Region codes (US, NG) where this product is offered in the default "
            "catalog."
        ),
    )


# ---------------------------------------------------------------------------
# Catalog data
# ---------------------------------------------------------------------------


#: The shipped default catalog. Entries with a non-empty ``aliases`` tuple are
#: legacy-mapped so NG tenants migrating from the AGO/PMS/ATK/LPG enum continue
#: to work without reconfiguration (Requirement 6.1.2).
FUEL_PRODUCT_CATALOG: Tuple[FuelProduct, ...] = (
    FuelProduct(
        product_code="DIESEL_2",
        display_name="Diesel #2",
        category="diesel",
        density_lbs_per_gallon=7.08,
        tax_class="road_diesel",
        aliases=("AGO",),
        region_availability=("US", "NG"),
    ),
    FuelProduct(
        product_code="HEATING_OIL",
        display_name="Heating Oil #2",
        category="heating_oil",
        density_lbs_per_gallon=7.20,
        tax_class="off_road",
        aliases=(),
        region_availability=("US",),
    ),
    FuelProduct(
        product_code="GASOLINE_REG",
        display_name="Gasoline Regular 87",
        category="gasoline",
        density_lbs_per_gallon=6.17,
        tax_class="gasoline",
        aliases=("PMS",),
        region_availability=("US", "NG"),
    ),
    FuelProduct(
        product_code="GASOLINE_PREM",
        display_name="Gasoline Premium 93",
        category="gasoline",
        density_lbs_per_gallon=6.17,
        tax_class="gasoline",
        aliases=(),
        region_availability=("US",),
    ),
    FuelProduct(
        product_code="PROPANE",
        display_name="Propane",
        category="propane",
        density_lbs_per_gallon=4.24,
        tax_class="propane",
        aliases=("LPG",),
        region_availability=("US", "NG"),
    ),
    FuelProduct(
        product_code="KEROSENE",
        display_name="Kerosene",
        category="kerosene",
        density_lbs_per_gallon=6.82,
        tax_class="kerosene",
        aliases=("ATK",),
        region_availability=("US", "NG"),
    ),
    FuelProduct(
        product_code="OFF_ROAD_DIESEL",
        display_name="Off-Road Diesel (Red)",
        category="off_road",
        density_lbs_per_gallon=7.08,
        tax_class="off_road",
        aliases=(),
        region_availability=("US",),
    ),
    FuelProduct(
        product_code="DEF",
        display_name="Diesel Exhaust Fluid",
        category="def",
        density_lbs_per_gallon=9.10,
        tax_class="non_fuel",
        aliases=(),
        region_availability=("US",),
    ),
    FuelProduct(
        product_code="ETHANOL_E85",
        display_name="E85 Ethanol Blend",
        category="ethanol",
        density_lbs_per_gallon=6.33,
        tax_class="gasoline",
        aliases=(),
        region_availability=("US",),
    ),
)


# ---------------------------------------------------------------------------
# Indexes (computed once at import time)
# ---------------------------------------------------------------------------


_PRODUCT_CODES: frozenset[str] = frozenset(p.product_code for p in FUEL_PRODUCT_CATALOG)


def _build_alias_index() -> Dict[str, str]:
    """Map every alias AND every canonical product_code to its product_code.

    Including canonical codes lets :func:`canonicalize` perform a single
    dictionary lookup regardless of whether the caller passed an alias or an
    already-canonical code.
    """

    index: Dict[str, str] = {}
    for product in FUEL_PRODUCT_CATALOG:
        index[product.product_code] = product.product_code
        for alias in product.aliases:
            # Aliases must not collide with another product's canonical code.
            if alias in _PRODUCT_CODES and alias != product.product_code:
                raise RuntimeError(
                    f"alias {alias!r} on {product.product_code} collides with "
                    "an existing product_code"
                )
            existing = index.get(alias)
            if existing is not None and existing != product.product_code:
                raise RuntimeError(
                    f"alias {alias!r} resolves to both {existing} and "
                    f"{product.product_code}"
                )
            index[alias] = product.product_code
    return index


_ALIAS_INDEX: Dict[str, str] = _build_alias_index()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class UnknownFuelProductError(ValueError):
    """Raised when :func:`canonicalize` receives a code it does not recognize.

    Subclass of :class:`ValueError` so callers that only care about the
    standard-library exception can catch it without importing this module.
    """

    def __init__(self, code_or_alias: str) -> None:
        super().__init__(f"unknown fuel product: {code_or_alias!r}")
        self.code_or_alias = code_or_alias


def canonicalize(code_or_alias: str) -> str:
    """Return the canonical US ``product_code`` for the given code or alias.

    The input is trimmed and upper-cased before lookup, so ``"ago"``, ``" AGO "``
    and ``"AGO"`` all resolve to ``"DIESEL_2"``. If the input is already a
    canonical product code it is returned unchanged (after normalization),
    which makes this function idempotent:

        canonicalize(canonicalize(x)) == canonicalize(x)

    Raises:
        UnknownFuelProductError: if ``code_or_alias`` does not match any
            canonical code or alias in the catalog.
        TypeError: if ``code_or_alias`` is not a string.
    """

    if not isinstance(code_or_alias, str):
        raise TypeError(
            f"canonicalize() expects a string, got {type(code_or_alias).__name__}"
        )
    normalized = code_or_alias.strip().upper()
    if not normalized:
        raise UnknownFuelProductError(code_or_alias)
    try:
        return _ALIAS_INDEX[normalized]
    except KeyError as exc:
        raise UnknownFuelProductError(code_or_alias) from exc


def get_products_for_region(region_code: str) -> List[FuelProduct]:
    """Return the default catalog entries available in ``region_code``.

    Filtering is case-insensitive. Unknown regions return an empty list rather
    than raising so callers iterating over tenants with non-default regions
    degrade gracefully; the REST layer is responsible for surfacing an error
    when a tenant has no products configured.

    Region semantics (Requirement 6.1.5):
        * ``"US"`` returns the full nine-product catalog.
        * ``"NG"`` returns the four products whose ``region_availability``
          includes ``"NG"`` — DIESEL_2, GASOLINE_REG, PROPANE, KEROSENE — and
          each retains its legacy Nigerian alias (AGO/PMS/LPG/ATK) for display.
    """

    if not isinstance(region_code, str):
        raise TypeError(
            f"get_products_for_region() expects a string, got "
            f"{type(region_code).__name__}"
        )
    normalized = region_code.strip().upper()
    if not normalized:
        return []
    return [p for p in FUEL_PRODUCT_CATALOG if normalized in p.region_availability]


def get_product(product_code: str) -> FuelProduct:
    """Return the catalog entry for ``product_code`` after canonicalization.

    Convenience accessor used by endpoints and agents that need the full
    :class:`FuelProduct` record (e.g. to look up ``density_lbs_per_gallon``).

    Raises:
        UnknownFuelProductError: if no entry matches.
    """

    canonical = canonicalize(product_code)
    for product in FUEL_PRODUCT_CATALOG:
        if product.product_code == canonical:
            return product
    # Unreachable because canonicalize() already validated membership.
    raise UnknownFuelProductError(product_code)


def is_known_product(code_or_alias: str) -> bool:
    """Return True if ``code_or_alias`` resolves to a catalog entry."""

    try:
        canonicalize(code_or_alias)
    except (UnknownFuelProductError, TypeError):
        return False
    return True


def canonicalize_or_warn(
    code_or_alias: Any,
    *,
    context: str = "",
    logger_: Optional[Any] = None,
) -> Any:
    """Best-effort canonicalize that preserves unknown inputs and logs a warning.

    Unlike :func:`canonicalize`, this variant is intended for write paths
    where the field may legitimately hold non-fuel values (e.g.
    ``inventory.compatible_assets`` holds asset types that are never fuel
    products). Input that cannot be canonicalized is returned unchanged so
    the caller can persist it as-is without breaking unrelated domains.

    * If ``code_or_alias`` resolves to a catalog entry, the canonical
      ``product_code`` is returned.
    * If it does not resolve (unknown code or non-string input), the
      original value is returned and a warning is logged via ``logger_``
      (or the module logger when ``logger_`` is None).

    Args:
        code_or_alias: The candidate string (or any value) to canonicalize.
        context: Optional free-form label included in the warning log to
            help operators locate the non-canonicalizable field (e.g.,
            ``"inventory.compatible_assets"``).
        logger_: Optional caller-supplied logger so warnings appear under
            the caller's module name rather than this module.

    Returns:
        The canonical product_code when resolution succeeds, otherwise the
        original input.
    """

    try:
        return canonicalize(code_or_alias)  # type: ignore[arg-type]
    except (UnknownFuelProductError, TypeError):
        log = logger_ or _module_logger
        log.warning(
            "canonicalize_or_warn: unable to canonicalize %r%s; "
            "persisting original value",
            code_or_alias,
            f" (context={context})" if context else "",
        )
        return code_or_alias


__all__ = [
    "FuelCategory",
    "RegionCode",
    "FuelProduct",
    "FUEL_PRODUCT_CATALOG",
    "UnknownFuelProductError",
    "canonicalize",
    "canonicalize_or_warn",
    "get_products_for_region",
    "get_product",
    "is_known_product",
]
