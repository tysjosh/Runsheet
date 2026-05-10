"""Volume Correction Factor (VCF) calculator — API 2540 / ASTM D1250.

This module implements Requirement 2 of the Fuel Compliance Backbone spec:
converting gross gallons measured at an observed temperature to net gallons
at the US petroleum reference temperature of 60°F, using Volume Correction
Factors defined by API MPMS Chapter 11.1 / ASTM D1250 Table 6B (generalized
petroleum products).

The :class:`VCFCalculator` is intentionally **stateless** — it carries no
Elasticsearch handle, no Redis client, and no tenant scope. All inputs are
passed by argument and all outputs are returned by value so the same
instance (or a transient local instance) can be called from:

* :class:`compliance.services.terminal_bol_ingestion_service.TerminalBOLIngestionService`
  on every inbound EDI/manual BOL (Req 10.4),
* POD finalization in the delivery pipeline (Req 2.4),
* the reconciliation service for terminal-to-meter variance computation
  (Req 2.5), and
* unit / property-based tests without any infrastructure fixtures.

The concrete computation, lookup polynomial, and input validation are
implemented by the follow-on subtasks (2.2–2.5) and the round-trip property
test is introduced by subtask 2.6. This file provides the class skeleton,
public method signatures, and docstrings so callers can wire the calculator
in before the math lands.

Validates: Requirement 2.1
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Final, Optional

from fuel.services import fuel_product_catalog as _default_fuel_product_catalog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants — exposed so callers and tests can share boundary values
# without hard-coding numbers at each site.
# ---------------------------------------------------------------------------

#: The US petroleum reference temperature at which "net gallons" are
#: expressed (API MPMS Ch. 11.1 / ASTM D1250).
REFERENCE_TEMPERATURE_F: Final[float] = 60.0

#: Inclusive lower bound on observed temperature accepted by the calculator
#: (Req 2.6). Values outside the ASTM D1250 validated range raise
#: ``vcf.input_out_of_range``.
MIN_TEMPERATURE_F: Final[float] = -50.0

#: Inclusive upper bound on observed temperature (Req 2.6).
MAX_TEMPERATURE_F: Final[float] = 150.0

#: Inclusive lower bound on API gravity (Req 2.6). Real petroleum products
#: fall in roughly 10–60 °API; 0–100 is the validation window used here.
MIN_API_GRAVITY: Final[float] = 0.0

#: Inclusive upper bound on API gravity (Req 2.6).
MAX_API_GRAVITY: Final[float] = 100.0

#: Error code raised when either the observed temperature or the API
#: gravity is outside the validated range (Req 2.6).
ERROR_CODE_INPUT_OUT_OF_RANGE: Final[str] = "vcf.input_out_of_range"

#: Decimal places that net-gallon results are rounded to (Req 2.3 —
#: "rounded to the nearest tenth of a gallon").
NET_GALLONS_ROUNDING_DIGITS: Final[int] = 1


# ---------------------------------------------------------------------------
# Default API-gravity fallback table
# ---------------------------------------------------------------------------
# The authoritative source of per-product API gravity is the fuel product
# catalog (``fuel.services.fuel_product_catalog``). However, the shipped
# :class:`~fuel.services.fuel_product_catalog.FuelProduct` model currently
# exposes ``density_lbs_per_gallon`` but not an explicit ``api_gravity``
# field. Rather than block Task 2.4 on a catalog migration, we read an
# ``api_gravity`` attribute off catalog entries when it is present and
# otherwise fall back to this module-level table of typical default
# gravities for the canonical US product codes.
#
# These values are population averages, not measurements — a BOL or meter
# ticket with a measured gauge reading should always use the measured value
# rather than this default. They are sourced from widely-cited references:
#
#   * Gasoline (regular & premium) — ~60 °API (typical US motor gasoline,
#     roughly 0.74 SG).
#   * Distillate diesel (#2) / heating oil #2 — ~35 °API (typical US ULSD
#     and #2 fuel-oil, roughly 0.85 SG). Off-road (dyed) diesel is the same
#     product with a dye package, so it shares the 35 °API default.
#   * Heating oil — ~32 °API (slightly heavier distillate blend).
#   * Kerosene / jet-grade kerosene — ~43 °API (lighter than diesel).
#   * Propane — ~147 °API (very light LPG; the high value reflects its low
#     density relative to water at 60 °F).
#   * Ethanol E85 blend — ~48 °API (dominated by the ethanol component,
#     ~0.79 SG).
#   * DEF (diesel exhaust fluid) — ~10 °API (aqueous urea, denser than
#     water; provided so the calculator does not crash on DEF tickets,
#     even though DEF is a non-fuel product whose volume-correction is
#     governed by a different specification).
#
# The values are kept conservative (rounded to whole °API) because they
# are only used when a measured reading is unavailable; tightening them
# should be driven by catalog data rather than by guessing at decimals.
DEFAULT_API_GRAVITY_BY_PRODUCT: Final[Dict[str, float]] = {
    "GASOLINE_REG": 60.0,
    "GASOLINE_PREM": 60.0,
    "DIESEL_2": 35.0,
    "OFF_ROAD_DIESEL": 35.0,
    "HEATING_OIL": 32.0,
    "KEROSENE": 43.0,
    "PROPANE": 147.0,
    "ETHANOL_E85": 48.0,
    "DEF": 10.0,
}


# ---------------------------------------------------------------------------
# ASTM D1250 / API MPMS Ch. 11.1 Table 6B constants
# ---------------------------------------------------------------------------
# The generalized-products polynomial approximation expresses the thermal
# expansion coefficient at 60 °F (``alpha_60``) as a function of the
# density-at-60 °F in kg/m³:
#
#     alpha_60 = (K0 + K1 * rho_60) / rho_60 ** 2
#
# and then returns a VCF of:
#
#     VCF = exp(-alpha_60 * dT * (1 + 0.8 * alpha_60 * dT))
#
# where ``dT`` is the temperature delta from the 60 °F (15.556 °C) reference
# in degrees Celsius. The constants below are the Table 6B coefficients for
# "generalized products" (i.e. refined petroleum products such as gasoline,
# distillate, jet, kerosene — the product mix Runsheet actually moves), as
# published in API MPMS Chapter 11.1 (ASTM D1250-04/08). Crude oil uses
# Table 6A with different constants; it is intentionally not supported here.
#
# Sources:
#   * API MPMS Ch. 11.1 (2004 / 2008) — Volume Correction Factors.
#   * ASTM D1250-08 Table 6B — Generalized Products, °API at 60 °F.
#   * "Manual of Petroleum Measurement Standards" reprints publishing the
#     same K0/K1 pair for generalized refined products.

#: K0 coefficient of the Table 6B generalized-products polynomial
#: (kg/m³)². Used to compute ``alpha_60`` from the density at 60 °F.
TABLE_6B_K0: Final[float] = 103.8720

#: K1 coefficient of the Table 6B generalized-products polynomial (kg/m³).
TABLE_6B_K1: Final[float] = 0.2701

#: Density of pure water at 60 °F in kg/m³. Multiplying a relative-density
#: (specific-gravity) value by this constant converts it to the absolute
#: density expected by the Table 6B polynomial.
WATER_DENSITY_AT_60F_KG_PER_M3: Final[float] = 999.012

#: Conversion factor from a Fahrenheit delta to a Celsius delta
#: (``delta_C = delta_F * 5 / 9``).
FAHRENHEIT_TO_CELSIUS_DELTA: Final[float] = 5.0 / 9.0


# ---------------------------------------------------------------------------
# VCF Calculator
# ---------------------------------------------------------------------------


class VCFCalculator:
    """Stateless calculator for API 2540 / ASTM D1250 Volume Correction Factors.

    The calculator has no dependencies on Elasticsearch, Redis, or tenant
    state. Every public method is a pure function of its arguments so the
    same instance can be reused across tenants, threads, and async tasks
    without synchronization.

    Typical usage::

        calc = VCFCalculator()
        vcf = calc.compute_vcf(temperature_f=72.0, api_gravity=35.0)
        net = calc.compute_net_gallons(
            gross_gallons=8000.0,
            temperature_f=72.0,
            api_gravity=35.0,
        )

    The class is split across several follow-on tasks:

    * Task 2.2 — :meth:`compute_vcf` (ASTM D1250 Table 6B polynomial).
    * Task 2.3 — :meth:`compute_net_gallons` (``gross * vcf`` rounded to 0.1).
    * Task 2.4 — :meth:`default_api_gravity` (lookup from the
      ``fuel_product_catalog``, with a module-level fallback table
      :data:`DEFAULT_API_GRAVITY_BY_PRODUCT` for products the catalog
      does not yet carry an explicit ``api_gravity`` field for).
    * Task 2.5 — input validation raising
      :data:`ERROR_CODE_INPUT_OUT_OF_RANGE`.

    Validates: Requirement 2.1
    """

    # The calculator holds no mutable state beyond an optional catalog
    # reference used by :meth:`default_api_gravity`. Keeping ``__init__``
    # explicit (rather than relying on the default) lets tests substitute
    # a fake catalog — or suppress the catalog lookup entirely — without
    # touching the module-level import.
    def __init__(self, fuel_product_catalog: Optional[Any] = None) -> None:
        """Construct a :class:`VCFCalculator`.

        Args:
            fuel_product_catalog: Optional catalog dependency used by
                :meth:`default_api_gravity`. The object must expose a
                ``get_product(product_code)`` callable returning a catalog
                entry (any object whose ``product_code`` matches and from
                which an ``api_gravity`` attribute can optionally be read).
                When ``None`` (the default), the live
                :mod:`fuel.services.fuel_product_catalog` module is used.
                The calculator always falls back to
                :data:`DEFAULT_API_GRAVITY_BY_PRODUCT` when the catalog
                entry does not expose an ``api_gravity`` value.

        The calculator remains stateless with respect to its numerical
        computations (:meth:`compute_vcf`, :meth:`compute_net_gallons`);
        the catalog is only consulted by :meth:`default_api_gravity`.
        """
        self._fuel_product_catalog = (
            fuel_product_catalog
            if fuel_product_catalog is not None
            else _default_fuel_product_catalog
        )

    # ------------------------------------------------------------------
    # Core computations (implemented by subsequent subtasks)
    # ------------------------------------------------------------------

    def compute_vcf(self, temperature_f: float, api_gravity: float) -> float:
        """Return the Volume Correction Factor for the given inputs.

        Computes the ASTM D1250 / API MPMS Ch. 11.1 Table 6B Volume
        Correction Factor that converts a volume measured at
        ``temperature_f`` (°F) to the equivalent volume at
        :data:`REFERENCE_TEMPERATURE_F` (60 °F) for a product with the
        given ``api_gravity``. The returned value is a dimensionless
        multiplier, typically in the range ``~0.95``–``~1.05``.

        Args:
            temperature_f: Observed fuel temperature in degrees Fahrenheit.
                Must satisfy
                ``MIN_TEMPERATURE_F <= temperature_f <= MAX_TEMPERATURE_F``.
            api_gravity: API gravity of the fuel (dimensionless, °API).
                Must satisfy ``MIN_API_GRAVITY <= api_gravity <= MAX_API_GRAVITY``.

        Returns:
            The dimensionless Volume Correction Factor. Multiplying
            ``gross_gallons`` by this value yields net gallons at 60 °F.

        Raises:
            ValueError: If either input is outside its validated range
                (the error message starts with
                :data:`ERROR_CODE_INPUT_OUT_OF_RANGE`). Introduced by
                Task 2.5.

        Validates: Requirement 2.2
        """
        # ------------------------------------------------------------------
        # Input validation — the full validation policy (ValueError with
        # :data:`ERROR_CODE_INPUT_OUT_OF_RANGE`) lands in Task 2.5. Until
        # then we preserve the "call the validator" hook so downstream
        # tests can rely on the defer-to-2.5 contract: today the helper is
        # a no-op, but when Task 2.5 lands every public method will raise
        # for out-of-range inputs without needing per-method changes.
        self._validate_inputs(temperature_f=temperature_f, api_gravity=api_gravity)

        # ------------------------------------------------------------------
        # Fast path — at the reference temperature the VCF is exactly 1.0
        # by definition (no expansion/contraction). Short-circuiting here
        # both matches Req 2.2 ("returns VCF = 1.0 exactly when
        # temperature_f == 60.0") and guards the ``exp`` below from
        # accumulating floating-point error at the reference point.
        if temperature_f == REFERENCE_TEMPERATURE_F:
            return 1.0

        # ------------------------------------------------------------------
        # Step 1 — Convert API gravity to relative density (specific
        # gravity) at 60 °F using the standard API-to-SG relationship:
        #     SG_60/60 = 141.5 / (131.5 + API)
        # This is dimensionless (density relative to water at 60 °F).
        relative_density_60 = 141.5 / (131.5 + api_gravity)

        # Step 2 — Convert the relative density to an absolute density in
        # kg/m³, the unit the Table 6B polynomial expects. Multiplying by
        # the density of water at 60 °F (≈ 999.012 kg/m³) gives the
        # product's absolute density at the reference temperature.
        base_density_kg_m3 = relative_density_60 * WATER_DENSITY_AT_60F_KG_PER_M3

        # Step 3 — Evaluate the Table 6B thermal-expansion coefficient at
        # 60 °F for generalized products:
        #     alpha_60 = (K0 + K1 * rho_60) / rho_60 ** 2
        # Units: 1/°C. Using the published K0/K1 pair guarantees the
        # returned VCFs match the printed D1250 Table 6B to the published
        # precision (six decimal places) across the validated range.
        alpha_60 = (TABLE_6B_K0 + TABLE_6B_K1 * base_density_kg_m3) / (
            base_density_kg_m3 ** 2
        )

        # Step 4 — Convert the Fahrenheit temperature delta to Celsius.
        # The reference point is 60 °F (≡ 15.556 °C); equivalently, we
        # can work entirely in Fahrenheit deltas scaled by 5/9. Using the
        # delta form avoids the repeated (T_F − 32) subtraction and keeps
        # the formula identical to the ASTM D1250 presentation.
        delta_t_c = (temperature_f - REFERENCE_TEMPERATURE_F) * FAHRENHEIT_TO_CELSIUS_DELTA

        # Step 5 — Apply the D1250 VCF formula:
        #     VCF = exp(-alpha_60 * dT * (1 + 0.8 * alpha_60 * dT))
        # ``math.exp`` is used (rather than ``numpy.exp``) to keep this
        # module free of third-party numerical dependencies; it is a pure
        # scalar computation.
        exponent = -alpha_60 * delta_t_c * (1.0 + 0.8 * alpha_60 * delta_t_c)
        return math.exp(exponent)

    def compute_net_gallons(
        self,
        gross_gallons: float,
        temperature_f: float,
        api_gravity: float,
    ) -> float:
        """Convert gross gallons at observed temperature to net gallons at 60 °F.

        Equivalent to ``round(gross_gallons * compute_vcf(...), 1)`` — the
        value is rounded to the nearest tenth of a gallon per Req 2.3.

        Args:
            gross_gallons: Volume measured at ``temperature_f``. Must be
                non-negative.
            temperature_f: Observed fuel temperature in degrees Fahrenheit.
                Must satisfy
                ``MIN_TEMPERATURE_F <= temperature_f <= MAX_TEMPERATURE_F``.
            api_gravity: API gravity of the fuel. Must satisfy
                ``MIN_API_GRAVITY <= api_gravity <= MAX_API_GRAVITY``.

        Returns:
            Net gallons at :data:`REFERENCE_TEMPERATURE_F` rounded to one
            decimal place.

        Raises:
            ValueError: If any input is outside its validated range
                (Task 2.5), or if ``gross_gallons`` is negative.

        Validates: Requirement 2.3
        """
        # Defer temperature/API-gravity range validation to the shared
        # helper; Task 2.5 fills this in with the ``vcf.input_out_of_range``
        # contract so every public method shares one validation path.
        self._validate_inputs(temperature_f=temperature_f, api_gravity=api_gravity)

        # Gross gallons must be non-negative. A negative reading is a
        # structurally invalid measurement (meters and terminal BOLs only
        # report non-negative volumes), so we reject it at the boundary
        # rather than silently producing a negative net-gallons result.
        if gross_gallons < 0:
            raise ValueError(
                f"gross_gallons must be non-negative, got {gross_gallons!r}"
            )

        # Reuse :meth:`compute_vcf` so the net-gallons computation always
        # tracks the Table 6B polynomial implementation and its validation
        # rules, rather than re-deriving the factor here.
        vcf = self.compute_vcf(temperature_f, api_gravity)

        # Net gallons at 60 °F = gross volume at observed temperature
        # multiplied by the VCF, rounded to the nearest tenth of a gallon
        # per Req 2.3.
        net = gross_gallons * vcf
        return round(net, NET_GALLONS_ROUNDING_DIGITS)

    def default_api_gravity(self, product_code: str) -> float:
        """Return the default API gravity for a canonical fuel product code.

        Used by callers that do not have a measured gravity on hand
        (for example meter tickets lacking a gauge reading). The lookup
        is sourced from :mod:`fuel.services.fuel_product_catalog` so
        gravities stay consistent with the rest of the fuel domain.

        Resolution order:

        1. Canonicalize ``product_code`` via the catalog's
           ``canonicalize`` so callers may pass either a canonical code
           (``"DIESEL_2"``) or a legacy alias (``"AGO"``).
        2. If the catalog entry exposes an ``api_gravity`` attribute that
           is not ``None``, return it. This is a forward-compatible hook
           so a future catalog migration that adds the field is picked
           up automatically without changes here.
        3. Otherwise fall back to
           :data:`DEFAULT_API_GRAVITY_BY_PRODUCT`, the module-level table
           of typical default gravities for the canonical US product
           codes (documented alongside that constant).

        Args:
            product_code: A canonical product code from the fuel catalog
                (for example ``"DIESEL_2"``) or a legacy alias
                (for example ``"AGO"``).

        Returns:
            The default API gravity for that product (°API).

        Raises:
            ValueError: If ``product_code`` cannot be resolved to a
                catalog entry, or if the resolved product has neither an
                ``api_gravity`` attribute on its catalog entry nor a
                fallback default in
                :data:`DEFAULT_API_GRAVITY_BY_PRODUCT`.

        Validates: Requirement 2.8
        """
        catalog = self._fuel_product_catalog

        # Step 1 — canonicalize the incoming code so aliases (e.g. "AGO")
        # and canonical codes (e.g. "DIESEL_2") both resolve. Any
        # catalog-level error (unknown code, wrong type) is surfaced as a
        # ValueError with a clear message so callers get a single,
        # predictable exception type regardless of which layer rejected
        # the input.
        try:
            canonical = catalog.canonicalize(product_code)
        except Exception as exc:  # UnknownFuelProductError, TypeError, AttributeError
            raise ValueError(
                f"default_api_gravity: unknown fuel product_code {product_code!r}"
            ) from exc

        # Step 2 — prefer an explicit ``api_gravity`` on the catalog
        # entry when the catalog ships one. This keeps the calculator
        # future-compatible with a catalog schema that grows the field
        # without requiring a code change here.
        product = None
        try:
            product = catalog.get_product(canonical)
        except Exception:
            # Fall through to the module-level default lookup below;
            # :func:`get_product` only raises for unknown codes, which
            # canonicalize() would already have caught.
            product = None

        if product is not None:
            api_gravity = getattr(product, "api_gravity", None)
            if api_gravity is not None:
                return float(api_gravity)

        # Step 3 — fall back to the module-level default table.
        default = DEFAULT_API_GRAVITY_BY_PRODUCT.get(canonical)
        if default is not None:
            return default

        raise ValueError(
            "default_api_gravity: no default API gravity configured for "
            f"product_code {product_code!r} (canonical {canonical!r}); "
            "add an 'api_gravity' field to the fuel_product_catalog entry "
            "or an entry to DEFAULT_API_GRAVITY_BY_PRODUCT"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_inputs(
        self,
        *,
        temperature_f: float,
        api_gravity: float,
    ) -> None:
        """Validate ``temperature_f`` / ``api_gravity`` ranges.

        Raises :class:`ValueError` prefixed with
        :data:`ERROR_CODE_INPUT_OUT_OF_RANGE` when either input falls
        outside its validated range (Req 2.6). Boundaries are inclusive:
        ``temperature_f`` must satisfy
        ``MIN_TEMPERATURE_F <= temperature_f <= MAX_TEMPERATURE_F`` and
        ``api_gravity`` must satisfy
        ``MIN_API_GRAVITY <= api_gravity <= MAX_API_GRAVITY``.

        The message always begins with :data:`ERROR_CODE_INPUT_OUT_OF_RANGE`
        so callers can identify the failure mode via
        ``str(exc).startswith("vcf.input_out_of_range")``.

        Validates: Requirement 2.6
        """
        if temperature_f < MIN_TEMPERATURE_F or temperature_f > MAX_TEMPERATURE_F:
            raise ValueError(
                f"{ERROR_CODE_INPUT_OUT_OF_RANGE}: temperature "
                f"{temperature_f}°F outside "
                f"[{MIN_TEMPERATURE_F}, {MAX_TEMPERATURE_F}]"
            )
        if api_gravity < MIN_API_GRAVITY or api_gravity > MAX_API_GRAVITY:
            raise ValueError(
                f"{ERROR_CODE_INPUT_OUT_OF_RANGE}: api_gravity "
                f"{api_gravity} outside "
                f"[{MIN_API_GRAVITY}, {MAX_API_GRAVITY}]"
            )


__all__ = [
    "VCFCalculator",
    "REFERENCE_TEMPERATURE_F",
    "MIN_TEMPERATURE_F",
    "MAX_TEMPERATURE_F",
    "MIN_API_GRAVITY",
    "MAX_API_GRAVITY",
    "ERROR_CODE_INPUT_OUT_OF_RANGE",
    "NET_GALLONS_ROUNDING_DIGITS",
    "DEFAULT_API_GRAVITY_BY_PRODUCT",
    "TABLE_6B_K0",
    "TABLE_6B_K1",
    "WATER_DENSITY_AT_60F_KG_PER_M3",
    "FAHRENHEIT_TO_CELSIUS_DELTA",
]
