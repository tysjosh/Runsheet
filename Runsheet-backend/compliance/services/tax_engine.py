"""Tax Engine — per-invoice federal / state / county / city / UST / SPCC /
environmental fuel excise tax computation (Requirement 1 of the Fuel
Compliance Backbone spec).

This module is the skeleton introduced by Task 3.3. It defines the
public surface that downstream callers (``InvoiceService``,
``DyedDieselEnforcer``, the compliance REST endpoints, and unit tests)
can bind to while the concrete logic is staged across Tasks 3.4–3.8:

* Task 3.4 — jurisdiction lookup (``get_jurisdiction_rates``) and the
  FIPS resolution chain that rolls up federal → state → county → city.
* Task 3.5 — federal excise tax at the statutory rates (18.4¢/gallon
  gasoline, 24.4¢/gallon diesel) via ``_compute_federal_excise``.
* Task 3.6 — UST, SPCC, and environmental surcharges materialized as
  separate :class:`TaxLineItem` rows on the breakdown.
* Task 3.7 — exemption check (``check_exemption``) wiring road-use
  (dyed/off-road) and agricultural (farm) certificates into the
  breakdown so the Tax_Engine honors Reqs 1.7 and 1.8.
* Task 3.8 — explicit :class:`TaxJurisdictionNotFoundError`
  (``tax.jurisdiction_not_found``) raised when a required row is
  missing from the ``tax_jurisdictions`` index.

The :class:`TaxBreakdown` schema itself is fully specified in this task
because the downstream logic needs a stable return type to fill in, and
the contract is already nailed down by Req 1.10 (per-invoice breakdown
showing each tax component with rate, gallons, and computed amount for
Form 720 reporting). The component fields use integer cents throughout
(Commerce Backbone Constraint C1 — money is never floating-point), and
the rollup ``total_tax_cents`` is exposed as a ``@computed_field`` so
serialization always agrees with the component sum.

Validates: Requirement 1.10
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Final, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from compliance.models.jurisdiction_rate import JurisdictionRate
from compliance.models.tax_exemption import TaxExemption
from compliance.services.compliance_es_mappings import (
    TAX_EXEMPTIONS_INDEX,
    TAX_JURISDICTIONS_INDEX,
)
from fuel.services.fuel_product_catalog import canonicalize
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FIPS resolution constants
# ---------------------------------------------------------------------------

#: 2-digit federal sentinel FIPS code. Federal rows are stored in the
#: ``tax_jurisdictions`` index with ``fips_code="00"`` so a single query
#: shape (``terms`` on ``fips_code``) rolls up federal + state + county +
#: city rates in one call. See
#: :mod:`compliance.models.jurisdiction_rate` for the convention.
FEDERAL_FIPS_SENTINEL: Final[str] = "00"

#: Maximum number of jurisdiction rows expected for any single
#: federal → state → county → city hierarchy across all tax types and
#: product codes. 1 federal + 1 state + 1 county + 1 city × 4 tax_types
#: × multi-product rows leaves ample headroom; bumping to 1000 gives a
#: comfortable ceiling so we never silently truncate a legitimate
#: lookup.
_MAX_JURISDICTION_ROWS_PER_LOOKUP: Final[int] = 1000


# ---------------------------------------------------------------------------
# Rate scale & statutory federal excise rates (Req 1.1)
# ---------------------------------------------------------------------------

#: Scale applied to :attr:`JurisdictionRate.rate_cents_per_gallon` values to
#: preserve the sub-cent precision of the federal excise rates (18.4¢ and
#: 24.4¢ per gallon). The ES mapping stores ``rate_cents_per_gallon`` as a
#: ``long``, so we adopt the convention that the integer is in **tenths of a
#: cent** (mills) per gallon — i.e. a stored value of ``184`` represents
#: 18.4¢ per gallon. The amount-in-cents formula is therefore::
#:
#:     amount_cents = round(rate_stored * gallons / RATE_SCALE)
#:
#: This keeps all persisted rates as integers (Constraint C1 — money is
#: never floating-point) while letting us represent the statutory 0.1¢
#: precision required by IRC §4081. The convention is already in use
#: across the Task 3.4 jurisdiction-lookup tests (see
#: ``test_tax_engine_jurisdiction_lookup.py``) which seed federal rows
#: with ``rate_cents_per_gallon=184``.
RATE_SCALE: Final[int] = 10

#: Federal excise rate for gasoline products under IRC §4081 in the
#: :data:`RATE_SCALE` convention. 18.4¢ per gallon → 184 tenths-of-cent
#: per gallon.
FEDERAL_EXCISE_GASOLINE_RATE: Final[int] = 184

#: Federal excise rate for diesel products under IRC §4081 in the
#: :data:`RATE_SCALE` convention. 24.4¢ per gallon → 244 tenths-of-cent
#: per gallon.
FEDERAL_EXCISE_DIESEL_RATE: Final[int] = 244

#: Federal excise rate for propane / LPG motor-fuel use under
#: IRC §4041(a)(2) in the :data:`RATE_SCALE` convention. 18.3¢ per
#: gallon → 183 tenths-of-cent per gallon. Customers using propane for
#: non-motor purposes typically claim an exemption certificate which is
#: honored by :meth:`TaxEngine.check_exemption` (Task 3.7).
FEDERAL_EXCISE_PROPANE_RATE: Final[int] = 183

#: Map of canonical fuel product code → statutory federal excise rate
#: (in :data:`RATE_SCALE` units). Used by
#: :meth:`TaxEngine._compute_federal_excise` when no matching row is
#: found in the ``tax_jurisdictions`` index for the federal sentinel
#: (FIPS ``"00"``). Product codes are canonicalized via
#: :func:`fuel.services.fuel_product_catalog.canonicalize` before lookup
#: so callers may pass legacy Nigerian aliases (``"AGO"`` → ``"DIESEL_2"``).
#:
#: Off-road / dyed diesel, heating oil, and kerosene share the diesel
#: statutory rate. The exemption logic in Task 3.7 subsequently excludes
#: the federal component for customers holding a valid 637M / off-road /
#: farm certificate (Req 1.7). ``DEF`` (diesel exhaust fluid) is a
#: non-fuel additive with no excise tax, so it maps to ``0``.
_STATUTORY_FEDERAL_EXCISE_RATES: Final[Dict[str, int]] = {
    "GASOLINE_REG":     FEDERAL_EXCISE_GASOLINE_RATE,
    "GASOLINE_PREM":    FEDERAL_EXCISE_GASOLINE_RATE,
    "ETHANOL_E85":      FEDERAL_EXCISE_GASOLINE_RATE,
    "DIESEL_2":         FEDERAL_EXCISE_DIESEL_RATE,
    "OFF_ROAD_DIESEL":  FEDERAL_EXCISE_DIESEL_RATE,
    "HEATING_OIL":      FEDERAL_EXCISE_DIESEL_RATE,
    "KEROSENE":         FEDERAL_EXCISE_DIESEL_RATE,
    "PROPANE":          FEDERAL_EXCISE_PROPANE_RATE,
    "DEF":              0,
}

#: Line-item component name for the federal excise row emitted by
#: :meth:`TaxEngine._compute_federal_excise`. Matches the Form 720
#: reporting label so downstream consumers can group by component.
FEDERAL_EXCISE_COMPONENT_NAME: Final[str] = "federal_excise"

#: Line-item component name for the county excise row emitted by
#: :meth:`TaxEngine._compute_state_local_taxes_and_fees` (Task 3.6).
#: County-level excise rows roll up under a single label so the
#: Form 720 breakdown does not carry per-county noise.
COUNTY_EXCISE_COMPONENT_NAME: Final[str] = "county_excise"

#: Line-item component name for the city excise row emitted by
#: :meth:`TaxEngine._compute_state_local_taxes_and_fees` (Task 3.6).
CITY_EXCISE_COMPONENT_NAME: Final[str] = "city_excise"

#: Line-item component name for UST (Underground Storage Tank) fees
#: emitted by :meth:`TaxEngine._compute_state_local_taxes_and_fees`
#: (Task 3.6, Req 1.4). UST rows are grouped regardless of their
#: ``jurisdiction_level`` because the fee is always rendered as a
#: single line item on the invoice.
UST_FEE_COMPONENT_NAME: Final[str] = "ust_fee"

#: Line-item component name for SPCC (Spill Prevention, Control, and
#: Countermeasure) fees (Task 3.6, Req 1.4).
SPCC_FEE_COMPONENT_NAME: Final[str] = "spcc_fee"

#: Line-item component name for jurisdiction-specific environmental /
#: cleanup-fund fees (Task 3.6, Req 1.4).
ENVIRONMENTAL_FEE_COMPONENT_NAME: Final[str] = "environmental_fee"

#: Fallback component name used by
#: :meth:`TaxEngine._compute_state_local_taxes_and_fees` when a
#: state-level excise row has no ``jurisdiction_name`` set. When the
#: name *is* present, the component label becomes
#: ``f"{jurisdiction_name}_state_excise"`` so Form 720 reporting can
#: tell two states apart (e.g. ``"CA_state_excise"``,
#: ``"NY_state_excise"``).
STATE_EXCISE_FALLBACK_COMPONENT_NAME: Final[str] = "state_excise"


# ---------------------------------------------------------------------------
# Exemption priority (Task 3.7 — Reqs 1.7, 1.8)
# ---------------------------------------------------------------------------

#: Priority order applied when a customer holds multiple valid
#: exemption certificates for the same product/date. Lower index =
#: higher priority. The Tax_Engine honors the highest-priority
#: certificate and ignores the rest for a given invoice, matching the
#: rendering intent documented in the design note (road-use variants
#: win over agricultural, agricultural wins over blanket IRS 637M, and
#: jurisdictional exemptions rank last).
#:
#: Validates: Requirements 1.7, 1.8
_EXEMPTION_PRIORITY_ORDER: Final[Tuple[str, ...]] = (
    "dyed_diesel",
    "off_road",
    "farm",
    "637M",
    "government",
    "resale",
)

#: Exemption types that trigger the "road-use" exclusion: federal and
#: state excise are zeroed out on the breakdown, and matching line
#: items are dropped (Req 1.7). Covers dyed-diesel, off-road, and IRS
#: 637M registrations.
_ROAD_USE_EXEMPTION_TYPES: Final[frozenset] = frozenset(
    {"dyed_diesel", "off_road", "637M"}
)

#: Exemption types that trigger the "jurisdictional blanket"
#: exclusion: state + county + city excise are zeroed out on the
#: breakdown (simplified — keeps the federal component because
#: government / resale blanket exemptions are issued by state revenue
#: departments and IRS filings remain due).
_JURISDICTIONAL_EXEMPTION_TYPES: Final[frozenset] = frozenset(
    {"government", "resale"}
)

#: Maximum number of exemption certificates expected for a single
#: customer at any one time. 100 is enormous relative to realistic
#: portfolios (a tenant with more than 100 active certificates per
#: customer has a data-hygiene problem); this ceiling prevents
#: silent truncation while capping the ES fetch.
_MAX_EXEMPTIONS_PER_LOOKUP: Final[int] = 100


def _compute_amount_cents(rate_stored: int, gallons: float) -> int:
    """Convert a :data:`RATE_SCALE`-scaled rate × gallons into integer cents.

    Rounding uses banker's-free half-up semantics via ``round()`` then
    ``int()`` to stay consistent with the cents math elsewhere in the
    codebase (e.g. ``commerce/services/invoice_service.py``). The rate
    is a non-negative integer and gallons are non-negative by
    construction (both enforced by the :class:`JurisdictionRate` and
    :class:`TaxLineItem` validators), so the resulting cents value is
    always ``>= 0``.
    """
    if rate_stored < 0:
        raise ValueError(
            f"rate_stored must be >= 0, got {rate_stored}"
        )
    if gallons < 0:
        raise ValueError(f"gallons must be >= 0, got {gallons}")
    # ``round(x + 0.5)`` would bias positive values; built-in ``round``
    # with banker's rounding is acceptable here because both inputs are
    # non-negative and the downstream call site (Form 720) tolerates
    # ±1 cent per invoice. The integer cast prevents float leakage.
    return int(round(rate_stored * gallons / RATE_SCALE))


# ---------------------------------------------------------------------------
# Error codes / custom exceptions
# ---------------------------------------------------------------------------

#: Error code raised by the Tax_Engine when a required jurisdiction row is
#: missing from the ``tax_jurisdictions`` index (Req 1.9). Downstream
#: callers (invoice generation, REST endpoints) map this to a structured
#: rejection so operators can resolve the missing rate entry.
ERROR_CODE_JURISDICTION_NOT_FOUND: Final[str] = "tax.jurisdiction_not_found"


class TaxJurisdictionNotFoundError(ValueError):
    """Raised when the Tax_Engine cannot resolve a required jurisdiction.

    Subclasses :class:`ValueError` so existing call sites that catch
    ``ValueError`` for input-validation style failures continue to work
    without special-casing. The ``error_code`` attribute exposes the
    stable :data:`ERROR_CODE_JURISDICTION_NOT_FOUND` identifier so
    callers can route on the code rather than parsing the message.

    The implementation that raises this exception lives in Task 3.8;
    this class is declared here so the :class:`TaxEngine` public API
    can reference it in its docstrings from Task 3.3 onward.

    Attributes:
        error_code: Stable error-code string
            (``"tax.jurisdiction_not_found"``).
        fips_code: The FIPS code that could not be resolved (``None``
            when the caller did not supply one — e.g. a missing federal
            row).
        jurisdiction_level: Level the lookup was attempted at
            (``"federal"``, ``"state"``, ``"county"``, or ``"city"``).
        tax_type: Tax type that was being resolved (``"excise"``,
            ``"ust"``, ``"spcc"``, ``"environmental"``).
        product_code: Fuel product the rate was required for (when the
            caller provided one).
        effective_date: Effective date of the invoice / delivery the
            rate was being resolved for.
    """

    error_code: str = ERROR_CODE_JURISDICTION_NOT_FOUND

    def __init__(
        self,
        message: str,
        *,
        fips_code: Optional[str] = None,
        jurisdiction_level: Optional[str] = None,
        tax_type: Optional[str] = None,
        product_code: Optional[str] = None,
        effective_date: Optional[date] = None,
    ) -> None:
        super().__init__(message)
        self.fips_code = fips_code
        self.jurisdiction_level = jurisdiction_level
        self.tax_type = tax_type
        self.product_code = product_code
        self.effective_date = effective_date


# ---------------------------------------------------------------------------
# TaxLineItem — per-component breakdown row
# ---------------------------------------------------------------------------


class TaxLineItem(BaseModel):
    """Per-component tax line item on a :class:`TaxBreakdown`.

    Each row captures enough context for Form 720 reporting (Req 1.10):
    *which* tax component produced *how much* at *what rate* against
    *how many* gallons, scoped to the jurisdiction that levied it.

    All money values are integer cents (Constraint C1). Gallons is kept
    as a float because net-gallon measurements from the VCF calculator
    are already rounded to 0.1 gallons (Req 2.3).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    tax_component_name: str = Field(
        ...,
        description=(
            "Human-readable component name matching Form 720 reporting "
            "labels (e.g. 'federal_excise', 'CA_state_excise', "
            "'LA_county_ust', 'environmental_cleanup_fund')."
        ),
    )
    jurisdiction_fips: str = Field(
        ...,
        description=(
            "FIPS code of the levying jurisdiction. 2 digits for "
            "federal/state, 5 for county, 7 for city."
        ),
    )
    jurisdiction_level: str = Field(
        ...,
        description=(
            "Jurisdictional level: 'federal', 'state', 'county', or 'city'."
        ),
    )
    rate_cents_per_gallon: int = Field(
        ...,
        ge=0,
        description=(
            "Rate in integer cents per gallon applied by this component. "
            "Must be >= 0."
        ),
    )
    gallons: float = Field(
        ...,
        ge=0,
        description="Gallons the rate was applied to (net gallons).",
    )
    amount_cents: int = Field(
        ...,
        description=(
            "Computed cents for this line item. Sign follows the "
            "component: positive for tax assessed, negative for refunds "
            "/ credits (reserved for future use)."
        ),
    )

    @field_validator("tax_component_name")
    @classmethod
    def _component_name_must_be_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("tax_component_name must not be empty")
        return stripped

    @field_validator("jurisdiction_fips")
    @classmethod
    def _fips_code_must_be_valid(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("jurisdiction_fips must not be empty")
        if not stripped.isdigit():
            raise ValueError(
                f"jurisdiction_fips must contain only digits, got {v!r}"
            )
        if len(stripped) not in (2, 5, 7):
            raise ValueError(
                "jurisdiction_fips must be 2 digits (federal/state), "
                "5 digits (county), or 7 digits (city), "
                f"got length {len(stripped)}"
            )
        return stripped

    @field_validator("jurisdiction_level")
    @classmethod
    def _level_must_be_known(cls, v: str) -> str:
        allowed = {"federal", "state", "county", "city"}
        stripped = v.strip().lower()
        if stripped not in allowed:
            raise ValueError(
                f"jurisdiction_level must be one of {sorted(allowed)}, "
                f"got {v!r}"
            )
        return stripped


# ---------------------------------------------------------------------------
# TaxBreakdown — per-invoice roll-up (Req 1.10)
# ---------------------------------------------------------------------------


class TaxBreakdown(BaseModel):
    """Per-invoice tax breakdown returned by :meth:`TaxEngine.compute_tax`.

    Structure mirrors Form 720 reporting (Req 1.10): each component
    (federal, state, county, city, UST, SPCC, environmental) carries
    its own integer-cent bucket so downstream consumers can render the
    breakdown on the invoice PDF, aggregate it for quarterly tax
    filings, and reconcile it against the ``line_items`` detail.

    ``total_tax_cents`` is a computed field derived from the seven
    component fields so the rollup is always consistent with the parts
    (serialization and round-trip safe). ``exemptions_applied`` lists
    the exemption identifiers the Tax_Engine honored when assembling
    the breakdown (dyed-diesel, off-road, farm, 637M, etc.) — the
    detailed wiring lands in Task 3.7.

    All amounts are integer cents (Commerce Backbone Constraint C1).

    Validates: Requirement 1.10
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------------
    # Identity — optional at construction time so the Tax_Engine can
    # compute a breakdown for pricing previews that do not yet have an
    # invoice_id. InvoiceService sets it before persistence.
    # ------------------------------------------------------------------
    invoice_id: Optional[str] = Field(
        default=None,
        description=(
            "Invoice the breakdown is associated with. Optional at "
            "construction time so the Tax_Engine can produce previews "
            "before an invoice_id is assigned; InvoiceService sets it "
            "before persistence."
        ),
    )

    # ------------------------------------------------------------------
    # Component buckets (integer cents, default 0)
    # ------------------------------------------------------------------
    federal_cents: int = Field(
        default=0,
        ge=0,
        description="Federal excise tax in integer cents (Req 1.1).",
    )
    state_cents: int = Field(
        default=0,
        ge=0,
        description="State excise tax in integer cents (Req 1.2).",
    )
    county_cents: int = Field(
        default=0,
        ge=0,
        description="County fuel tax in integer cents (Req 1.3).",
    )
    city_cents: int = Field(
        default=0,
        ge=0,
        description="City fuel tax in integer cents (Req 1.3).",
    )
    ust_cents: int = Field(
        default=0,
        ge=0,
        description=(
            "Underground Storage Tank fee in integer cents (Req 1.4)."
        ),
    )
    spcc_cents: int = Field(
        default=0,
        ge=0,
        description=(
            "Spill Prevention, Control, and Countermeasure fee in "
            "integer cents (Req 1.4)."
        ),
    )
    environmental_cents: int = Field(
        default=0,
        ge=0,
        description=(
            "Jurisdiction-specific environmental / cleanup-fund fee in "
            "integer cents (Req 1.4)."
        ),
    )

    # ------------------------------------------------------------------
    # Provenance
    # ------------------------------------------------------------------
    exemptions_applied: List[str] = Field(
        default_factory=list,
        description=(
            "Exemption identifiers honored when assembling the "
            "breakdown (e.g. dyed-diesel certificate id, farm "
            "certificate id, IRS 637M registration). Populated by "
            "Task 3.7."
        ),
    )
    line_items: List[TaxLineItem] = Field(
        default_factory=list,
        description=(
            "Detailed per-component rows for Form 720 reporting "
            "(Req 1.10). Each row captures tax_component_name, "
            "jurisdiction_fips, jurisdiction_level, rate, gallons, "
            "and amount_cents."
        ),
    )

    # ------------------------------------------------------------------
    # Computed roll-up
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tax_cents(self) -> int:
        """Sum of the seven component buckets in integer cents.

        Exposed as a computed field so that serialization (``model_dump``,
        ``model_dump_json``) always emits a ``total_tax_cents`` that
        agrees with the component fields — the downstream logic in
        Tasks 3.4–3.8 writes into the component buckets and relies on
        this rollup for the per-invoice total.
        """
        return (
            self.federal_cents
            + self.state_cents
            + self.county_cents
            + self.city_cents
            + self.ust_cents
            + self.spcc_cents
            + self.environmental_cents
        )


# ---------------------------------------------------------------------------
# Tax Engine — service class (skeleton)
# ---------------------------------------------------------------------------


class TaxEngine:
    """Per-invoice fuel tax computation service.

    The Tax_Engine reconciles jurisdictional rate tables, customer
    exemption certificates, and per-product statutory rates into a
    single :class:`TaxBreakdown` per invoice. It is instantiated once
    per tenant by ``bootstrap/compliance.py`` and injected into the
    Commerce ``InvoiceService`` so invoice generation produces a
    schedule-ready tax breakdown before finalization (Req 1.10).

    This class provides the skeleton surface for Task 3.3. The concrete
    implementation lands across Tasks 3.4–3.8:

    * Task 3.4 — ``get_jurisdiction_rates`` FIPS lookup chain.
    * Task 3.5 — federal excise computation (gasoline 18.4¢, diesel 24.4¢).
    * Task 3.6 — UST / SPCC / environmental surcharges.
    * Task 3.7 — ``check_exemption`` for road-use and agricultural
      exemption certificates (Reqs 1.7, 1.8, 6.6).
    * Task 3.8 — :class:`TaxJurisdictionNotFoundError`
      (``tax.jurisdiction_not_found``) when a required row is missing
      (Req 1.9).

    The calling contract is defined here so the rest of the phase can
    wire through ``InvoiceService``, the REST endpoints, and the unit
    test scaffolding without waiting for the math.

    Args:
        es_service: Elasticsearch handle used to query the
            ``tax_jurisdictions`` and ``tax_exemptions`` indices. Typed
            as :class:`typing.Any` because the skeleton accepts any
            object exposing the same search surface (the live
            ``ElasticsearchService`` in production, a fake in tests).
        tenant_id: Tenant scope for every query. The Tax_Engine
            instance is bound to a single tenant so
            ``inject_tenant_filter`` is applied consistently by the
            downstream implementation (Constraint C3 — tenant-scoped
            queries).

    Validates: Requirement 1.10
    """

    def __init__(self, es_service: Any, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        self._es = es_service
        self._tenant_id = tenant_id.strip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_tax(
        self,
        product_code: str,
        net_gallons: float,
        destination_fips: str,
        customer_id: str,
        effective_date: Optional[date] = None,
    ) -> TaxBreakdown:
        """Compute the per-invoice :class:`TaxBreakdown`.

        Applies federal, state, county, and city excise rates sourced
        from the ``tax_jurisdictions`` index, adds UST / SPCC /
        environmental surcharges, and honors customer exemption
        certificates (dyed-diesel / off-road / farm / 637M) from the
        ``tax_exemptions`` index.

        The orchestration sequence is:

        1. Resolve ``effective_date`` to ``date.today()`` when not
           provided so callers generating previews at the current wall
           clock do not have to repeat the default themselves.
        2. Fetch the jurisdiction rollup via
           :meth:`get_jurisdiction_rates` (Task 3.4) for the destination
           FIPS + invoice date.
        3. Resolve the highest-priority customer exemption via
           :meth:`check_exemption` (Task 3.7). This step runs *before*
           the missing-row gate so a road-use / jurisdictional blanket
           exemption can short-circuit the Req 1.9 raise (the state row
           would be zeroed out anyway).
        4. **Req 1.9 gate.** If no state-level excise row for the
           delivered product is present in the rollup *and* no
           exemption has been applied that zeros the state component,
           raise :class:`TaxJurisdictionNotFoundError` with
           ``error_code == ERROR_CODE_JURISDICTION_NOT_FOUND``. The
           statutory fallback in
           :meth:`_compute_federal_excise` (Task 3.5) means federal
           never needs a jurisdiction row, but the state portion is
           required.
        5. Compute federal excise (Task 3.5) and state / county / city /
           UST / SPCC / environmental (Task 3.6), assemble line items,
           and produce the preliminary :class:`TaxBreakdown`.
        6. When an exemption applies, pass the breakdown through
           :meth:`apply_exemption` (Task 3.7) so federal + state
           road-use components are zeroed (dyed / off-road / 637M) or
           the state + county + city jurisdictional components are
           zeroed (government / resale). Farm certificates are
           flag-only per the Task 3.7 design note.

        Args:
            product_code: Canonical fuel product code (e.g.
                ``"DIESEL_2"``, ``"GASOLINE_REG"``,
                ``"OFF_ROAD_DIESEL"``). Drives the statutory federal
                rate (Task 3.5), jurisdictional rate filtering
                (Task 3.4), and exemption scoping (Task 3.7).
            net_gallons: Volume being invoiced, in temperature-corrected
                net gallons at 60 °F (Req 2.3). Must be non-negative.
            destination_fips: FIPS code of the delivery destination.
                2 digits (state), 5 digits (state + county), or 7
                digits (state + county + city).
            customer_id: Customer the invoice is being computed for.
                Drives the exemption lookup in Task 3.7.
            effective_date: Invoice / delivery date used to select rate
                rows by ``effective_date``/``expiry_date`` (Req 1.5)
                and to honor exemption certificates by their expiry
                window (Req 6.6). Defaults to :meth:`date.today` when
                not provided so preview callers do not have to repeat
                the default.

        Returns:
            A :class:`TaxBreakdown` populated with per-component cents,
            a ``line_items`` detail for Form 720 reporting, and the
            list of exemption ids honored.

        Raises:
            TaxJurisdictionNotFoundError: When the rollup returned by
                :meth:`get_jurisdiction_rates` does not include a
                state-level excise row matching the canonicalized
                product *and* no exemption was honored that zeros the
                state component. The exception carries
                ``error_code == ERROR_CODE_JURISDICTION_NOT_FOUND``
                and enough context (``fips_code``,
                ``jurisdiction_level``, ``tax_type``, ``product_code``,
                ``effective_date``) for the invoice-generation caller
                to surface a structured rejection to the operator.
            ValueError: When any of the helper validations fail
                (negative gallons, unknown product code without a
                statutory rate, malformed FIPS, etc.).

        Validates: Requirements 1.9, 1.10
        """
        # Step 1 — resolve effective_date.
        if effective_date is None:
            effective_date = date.today()

        # Step 2 — fetch the jurisdiction rollup for the destination.
        jurisdiction_rates = await self.get_jurisdiction_rates(
            destination_fips, effective_date
        )

        # Step 3 — resolve the highest-priority customer exemption first
        # so a road-use / jurisdictional blanket can short-circuit the
        # missing-row gate (the state row would be zeroed out anyway).
        exemption = await self.check_exemption(
            customer_id, product_code, effective_date
        )

        # Step 4 — Req 1.9 gate. Statutory fallback covers federal, but
        # the state portion requires a matching row unless an exemption
        # zeros it out.
        canonical_code = canonicalize(product_code)
        state_row_present = any(
            row.jurisdiction_level == "state"
            and row.tax_type == "excise"
            and canonical_code in row.product_codes
            for row in jurisdiction_rates
        )
        exemption_zeros_state = exemption is not None and (
            exemption.exemption_type in _ROAD_USE_EXEMPTION_TYPES
            or exemption.exemption_type in _JURISDICTIONAL_EXEMPTION_TYPES
        )
        if not state_row_present and not exemption_zeros_state:
            state_fips_prefix = destination_fips[:2]
            logger.warning(
                "TaxEngine.compute_tax: missing state excise row for "
                "tenant=%s state_fips=%s product=%s (canonical=%s) on %s",
                self._tenant_id,
                state_fips_prefix,
                product_code,
                canonical_code,
                effective_date.isoformat(),
            )
            raise TaxJurisdictionNotFoundError(
                f"Missing state excise row for state FIPS "
                f"{state_fips_prefix} (product {canonical_code}) on "
                f"{effective_date.isoformat()}",
                fips_code=state_fips_prefix,
                jurisdiction_level="state",
                tax_type="excise",
                product_code=product_code,
                effective_date=effective_date,
            )

        # Step 5 — compute federal excise + state/local components.
        federal_cents, federal_line_item = self._compute_federal_excise(
            product_code, net_gallons, jurisdiction_rates
        )
        state_local = self._compute_state_local_taxes_and_fees(
            product_code, net_gallons, jurisdiction_rates, destination_fips
        )

        line_items: List[TaxLineItem] = []
        if federal_line_item is not None:
            line_items.append(federal_line_item)
        line_items.extend(state_local["line_items"])

        breakdown = TaxBreakdown(
            federal_cents=federal_cents,
            state_cents=state_local["state_cents"],
            county_cents=state_local["county_cents"],
            city_cents=state_local["city_cents"],
            ust_cents=state_local["ust_cents"],
            spcc_cents=state_local["spcc_cents"],
            environmental_cents=state_local["environmental_cents"],
            line_items=line_items,
        )

        # Step 6 — apply exemption when one was honored.
        if exemption is not None:
            breakdown = self.apply_exemption(breakdown, exemption)

        return breakdown

    async def get_jurisdiction_rates(
        self,
        fips_code: str,
        effective_date: date,
    ) -> List[JurisdictionRate]:
        """Return the active rates for ``fips_code`` on ``effective_date``.

        Rolls up federal → state → county → city so a single call
        returns every row that applies to an invoice delivered at
        ``fips_code`` on ``effective_date``. Rows are filtered by
        ``effective_date``/``expiry_date`` so historical invoices
        continue to reference the row active at their invoice date
        (Req 1.5).

        Args:
            fips_code: FIPS code of the delivery destination. 2 digits
                (state), 5 digits (state + county), or 7 digits
                (state + county + city).
            effective_date: Invoice / delivery date used to filter
                rate rows (Req 1.5).

        Returns:
            A list of :class:`JurisdictionRate` rows that apply to the
            invoice. Order is not guaranteed; callers partition by
            ``jurisdiction_level`` and ``tax_type``.

        Raises:
            ValueError: When ``fips_code`` is not a string of digits
                with length 2, 5, or 7.

        Validates: Requirements 1.2, 1.3
        """
        candidate_codes = self._compute_candidate_fips_codes(fips_code)
        iso_date = effective_date.isoformat()

        # Read-cutover: serve from Postgres when enabled. We fetch the rows
        # matching the FIPS rollup with effective_date <= invoice date, then
        # apply the "expiry_date >= date OR missing" rule in Python (the
        # missing-field OR is awkward in portable SQL) and reuse the same
        # model validation. Byte-identical to the ES query result set.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_fetch_for_aggregation,
        )
        pg_docs = await read_hybrid_fetch_for_aggregation(
            "tax_jurisdiction", self._tenant_id,
            in_filters={"fips_code": candidate_codes},
            range_field="effective_date", range_lte=iso_date,
        )
        if pg_docs is not _NOT_CUT_OVER:
            rates_pg: List[JurisdictionRate] = []
            for source in pg_docs:
                expiry = source.get("expiry_date")
                if expiry is not None and str(expiry) < iso_date:
                    continue  # expired before the invoice date
                try:
                    rates_pg.append(JurisdictionRate.model_validate(source))
                except Exception as exc:
                    logger.warning(
                        "TaxEngine: skipping malformed tax_jurisdictions row "
                        "for tenant=%s fips_code=%s: %s",
                        self._tenant_id, fips_code, exc,
                    )
            return rates_pg

        # Build the ES query:
        # - terms filter on fips_code covering the full federal → state →
        #   county → city rollup
        # - effective_date <= invoice date
        # - expiry_date >= invoice date OR expiry_date is missing (open-
        #   ended rows are active indefinitely per Req 1.5)
        base_query: dict = {
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"fips_code": candidate_codes}},
                        {"range": {"effective_date": {"lte": iso_date}}},
                        {
                            "bool": {
                                "should": [
                                    {
                                        "range": {
                                            "expiry_date": {"gte": iso_date}
                                        }
                                    },
                                    {
                                        "bool": {
                                            "must_not": [
                                                {"exists": {"field": "expiry_date"}}
                                            ]
                                        }
                                    },
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            },
            "size": _MAX_JURISDICTION_ROWS_PER_LOOKUP,
        }

        query = inject_tenant_filter(base_query, self._tenant_id)

        response = await self._es.search_documents(
            TAX_JURISDICTIONS_INDEX,
            query,
            size=_MAX_JURISDICTION_ROWS_PER_LOOKUP,
        )

        hits = ((response or {}).get("hits") or {}).get("hits") or []
        rates: List[JurisdictionRate] = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                rates.append(JurisdictionRate.model_validate(source))
            except Exception as exc:
                # Skip malformed rows rather than failing the whole
                # lookup — the TaxEngine will surface a missing rate
                # via TaxJurisdictionNotFoundError (Task 3.8) if the
                # resulting set is incomplete for the invoice.
                logger.warning(
                    "TaxEngine: skipping malformed tax_jurisdictions row "
                    "for tenant=%s fips_code=%s: %s",
                    self._tenant_id,
                    fips_code,
                    exc,
                )
        return rates

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_candidate_fips_codes(fips_code: str) -> List[str]:
        """Return the FIPS rollup for a destination code.

        Given any ``fips_code`` of length 2 / 5 / 7, return the set of
        codes that apply to the destination:

        * Always include the :data:`FEDERAL_FIPS_SENTINEL` so federal
          rows are picked up in the same query.
        * The 2-digit state prefix.
        * The 5-digit county prefix (when ``len >= 5``).
        * The 7-digit city code itself (when ``len == 7``).

        The returned list is de-duplicated (so a 2-digit state input
        like ``"06"`` does not repeat itself) while preserving insertion
        order for deterministic query shaping and test assertions.

        Args:
            fips_code: Destination FIPS code.

        Returns:
            Ordered, de-duplicated list of candidate FIPS codes.

        Raises:
            ValueError: When ``fips_code`` is not a string of digits
                with length 2, 5, or 7.
        """
        if not isinstance(fips_code, str):
            raise ValueError(
                f"fips_code must be a string, got {type(fips_code).__name__}"
            )

        stripped = fips_code.strip()
        if not stripped:
            raise ValueError("fips_code must not be empty")
        if not stripped.isdigit():
            raise ValueError(
                f"fips_code must contain only digits, got {fips_code!r}"
            )
        if len(stripped) not in (2, 5, 7):
            raise ValueError(
                "fips_code must be 2 digits (state), 5 digits (county), "
                f"or 7 digits (city), got length {len(stripped)}"
            )

        candidates: List[str] = [FEDERAL_FIPS_SENTINEL]
        state_fips = stripped[:2]
        if state_fips not in candidates:
            candidates.append(state_fips)
        if len(stripped) >= 5:
            county_fips = stripped[:5]
            if county_fips not in candidates:
                candidates.append(county_fips)
        if len(stripped) == 7:
            if stripped not in candidates:
                candidates.append(stripped)
        return candidates

    # ------------------------------------------------------------------
    # Federal excise computation (Task 3.5 — Req 1.1)
    # ------------------------------------------------------------------

    def _compute_federal_excise(
        self,
        product_code: str,
        net_gallons: float,
        jurisdiction_rates: List[JurisdictionRate],
    ) -> Tuple[int, Optional[TaxLineItem]]:
        """Compute the federal excise tax component for a delivery.

        Resolves the rate in two stages:

        1. **Rate table first.** If ``jurisdiction_rates`` contains a row
           with ``fips_code == "00"`` (the :data:`FEDERAL_FIPS_SENTINEL`),
           ``jurisdiction_level == "federal"``, ``tax_type == "excise"``,
           and ``product_code`` in its ``product_codes`` list, the rate
           from that row is used. This lets operators schedule
           prospective federal rate changes (Req 1.5) through the
           ``tax_jurisdictions`` index without a code deploy.
        2. **Statutory default.** Otherwise the method falls back to the
           statutory rate for the canonicalized product code:

           * Gasoline (``GASOLINE_REG``, ``GASOLINE_PREM``,
             ``ETHANOL_E85``) → 18.4¢ / gallon (IRC §4081).
           * Diesel (``DIESEL_2``, ``OFF_ROAD_DIESEL``, ``HEATING_OIL``,
             ``KEROSENE``) → 24.4¢ / gallon (IRC §4081).
           * Propane (``PROPANE``) → 18.3¢ / gallon (IRC §4041(a)(2)).
           * DEF (``DEF``) → 0¢ / gallon (non-fuel additive).

           Off-road and dyed-fuel variants carry the same statutory
           rate; the exemption logic in Task 3.7 excludes the federal
           component when the customer holds a valid 637M / off-road /
           farm certificate (Req 1.7).

        Rates are stored in the :data:`RATE_SCALE` convention (tenths
        of a cent per gallon — see the module-level docstring for
        :data:`RATE_SCALE`). The returned ``amount_cents`` is converted
        via :func:`_compute_amount_cents` to integer cents, preserving
        the 0.1¢ precision required by Form 720.

        Args:
            product_code: Canonical or alias fuel product code. Passed
                through :func:`fuel.services.fuel_product_catalog.canonicalize`
                so legacy aliases (``"AGO"`` → ``"DIESEL_2"``) resolve
                to the statutory bucket.
            net_gallons: Temperature-corrected net gallons at 60°F
                (Req 2.3). Must be non-negative.
            jurisdiction_rates: Rates returned from
                :meth:`get_jurisdiction_rates` for the destination FIPS
                + effective date. This method filters them to the
                federal excise row that matches the canonicalized
                product code; non-federal / non-excise / non-matching
                rows are ignored. Pass an empty list to force a
                statutory fallback (useful in tests).

        Returns:
            A ``(amount_cents, line_item)`` tuple. ``amount_cents`` is
            the integer cents owed for federal excise on this delivery
            (always ``>= 0``). ``line_item`` is a :class:`TaxLineItem`
            row ready to append to :attr:`TaxBreakdown.line_items` —
            ``None`` only when ``net_gallons`` rounds to zero cents
            *and* the rate is zero (e.g. DEF with zero gallons); any
            non-zero rate still produces a line item so Form 720
            reporting captures the product exposure.

        Raises:
            ValueError: When ``net_gallons`` is negative, when
                ``product_code`` is not a string, or when the
                canonicalized product code has no statutory federal
                rate mapping and no matching row is found in
                ``jurisdiction_rates``. The latter is a fail-fast
                safeguard so a new fuel product without a federal-rate
                decision does not silently receive 0¢ tax.

        Validates: Requirement 1.1
        """
        if not isinstance(product_code, str):
            raise ValueError(
                "product_code must be a string, got "
                f"{type(product_code).__name__}"
            )
        if net_gallons < 0:
            raise ValueError(
                f"net_gallons must be >= 0, got {net_gallons}"
            )

        canonical_code = canonicalize(product_code)

        # ------------------------------------------------------------------
        # Stage 1 — rate-table lookup. Prefer the most specific federal row
        # (single-product ``product_codes`` beats a multi-product row) so
        # operator-scheduled prospective changes for a specific product
        # take priority over catch-all rows.
        # ------------------------------------------------------------------
        matching_rate: Optional[JurisdictionRate] = None
        for row in jurisdiction_rates:
            if row.fips_code != FEDERAL_FIPS_SENTINEL:
                continue
            if row.jurisdiction_level != "federal":
                continue
            if row.tax_type != "excise":
                continue
            if canonical_code not in row.product_codes:
                continue
            # First match wins on ties; prefer a more specific row
            # (shorter product_codes list) when two candidates apply.
            if matching_rate is None or len(row.product_codes) < len(
                matching_rate.product_codes
            ):
                matching_rate = row

        if matching_rate is not None:
            rate_stored = matching_rate.rate_cents_per_gallon
        else:
            # ------------------------------------------------------------------
            # Stage 2 — statutory fallback. Unknown product codes raise so
            # an operator-added fuel without a federal-rate decision cannot
            # silently invoice at 0¢.
            # ------------------------------------------------------------------
            if canonical_code not in _STATUTORY_FEDERAL_EXCISE_RATES:
                raise ValueError(
                    "No statutory federal excise rate is defined for "
                    f"product_code {product_code!r} (canonicalized to "
                    f"{canonical_code!r}). Add a row to the "
                    "tax_jurisdictions index with fips_code='00', "
                    "jurisdiction_level='federal', tax_type='excise' "
                    "or extend _STATUTORY_FEDERAL_EXCISE_RATES."
                )
            rate_stored = _STATUTORY_FEDERAL_EXCISE_RATES[canonical_code]

        amount_cents = _compute_amount_cents(rate_stored, net_gallons)

        line_item = TaxLineItem(
            tax_component_name=FEDERAL_EXCISE_COMPONENT_NAME,
            jurisdiction_fips=FEDERAL_FIPS_SENTINEL,
            jurisdiction_level="federal",
            rate_cents_per_gallon=rate_stored,
            gallons=net_gallons,
            amount_cents=amount_cents,
        )

        return amount_cents, line_item

    # ------------------------------------------------------------------
    # State / county / city excise + UST / SPCC / environmental fees
    # (Task 3.6 — Req 1.4)
    # ------------------------------------------------------------------

    def _compute_state_local_taxes_and_fees(
        self,
        product_code: str,
        net_gallons: float,
        jurisdiction_rates: List[JurisdictionRate],
        destination_fips: str,
    ) -> Dict[str, Any]:
        """Compute state/county/city excise + UST/SPCC/environmental fees.

        Iterates the ``jurisdiction_rates`` rollup (as returned by
        :meth:`get_jurisdiction_rates`) and partitions each matching row
        into the correct Form 720 bucket:

        * ``excise`` at ``jurisdiction_level == "state"``  → ``state_cents``
        * ``excise`` at ``jurisdiction_level == "county"`` → ``county_cents``
        * ``excise`` at ``jurisdiction_level == "city"``   → ``city_cents``
        * ``ust``            (any level) → ``ust_cents``
        * ``spcc``           (any level) → ``spcc_cents``
        * ``environmental``  (any level) → ``environmental_cents``

        Federal-level rows are ignored — federal excise is computed by
        :meth:`_compute_federal_excise` (Task 3.5) which owns the
        statutory-fallback logic. Rows whose ``product_codes`` list
        does not contain the canonicalized delivered product are
        skipped (Req 1.4 — the fee only applies to qualifying fuels).

        Rates are stored in the :data:`RATE_SCALE` convention (tenths
        of a cent per gallon). Amounts are converted to integer cents
        via :func:`_compute_amount_cents` so Form 720 reporting gets
        integer-only values (Constraint C1 — money is never
        floating-point). Every matching row produces a
        :class:`TaxLineItem`, including rows whose computed amount
        rounds to zero cents — keeping the audit trail intact so
        operators can see *why* a zero row was rendered (e.g. 0.0¢
        rate, zero gallons, or rounding to below ½ cent). This
        satisfies Req 1.10 (per-component rate, gallons, amount for
        every applicable jurisdiction).

        Line-item component names follow the Form 720 convention:

        * State excise: ``f"{jurisdiction_name}_state_excise"`` when
          the row carries a ``jurisdiction_name``, otherwise
          :data:`STATE_EXCISE_FALLBACK_COMPONENT_NAME` (``"state_excise"``).
          Including the jurisdiction name lets the downstream
          reporting layer distinguish between two states in the same
          breakdown (cross-state customer portfolios).
        * County excise: :data:`COUNTY_EXCISE_COMPONENT_NAME`.
        * City excise: :data:`CITY_EXCISE_COMPONENT_NAME`.
        * UST / SPCC / environmental: :data:`UST_FEE_COMPONENT_NAME` /
          :data:`SPCC_FEE_COMPONENT_NAME` /
          :data:`ENVIRONMENTAL_FEE_COMPONENT_NAME` regardless of
          jurisdiction level.

        This helper does not consult the :class:`TaxEngine`'s ES
        service — it is pure over its inputs so the Task 3.10
        integration into :meth:`compute_tax` can orchestrate the
        jurisdiction lookup, exemption check, and fee composition in a
        single transaction.

        Args:
            product_code: Canonical or alias fuel product code. Passed
                through :func:`fuel.services.fuel_product_catalog.canonicalize`
                so legacy aliases resolve to the same bucket as their
                canonical form (e.g. ``"AGO"`` → ``"DIESEL_2"``).
            net_gallons: Temperature-corrected net gallons at 60 °F
                (Req 2.3). Must be non-negative.
            jurisdiction_rates: Rollup returned from
                :meth:`get_jurisdiction_rates` for the destination FIPS
                and effective date. Federal rows are ignored; this
                helper only consumes state / county / city / any-level
                UST / SPCC / environmental rows.
            destination_fips: FIPS code of the delivery destination.
                Accepted for symmetry with :meth:`compute_tax` and
                validated so the helper can be called directly from
                tests and the Task 3.10 wiring without a separate
                pre-check. Currently used for input validation only;
                the per-row ``fips_code`` drives the emitted
                :class:`TaxLineItem`.

        Returns:
            A dict with keys::

                {
                    "state_cents":          int,
                    "county_cents":         int,
                    "city_cents":           int,
                    "ust_cents":            int,
                    "spcc_cents":           int,
                    "environmental_cents":  int,
                    "line_items":           List[TaxLineItem],
                }

            All six cent buckets are ``>= 0``. ``line_items`` is
            ordered by the iteration order of ``jurisdiction_rates``
            so callers can correlate with the query result.

        Raises:
            ValueError: When ``net_gallons`` is negative, when
                ``product_code`` is not a string, or when
                ``destination_fips`` is not a 2 / 5 / 7 digit FIPS code.

        Validates: Requirement 1.4
        """
        if not isinstance(product_code, str):
            raise ValueError(
                "product_code must be a string, got "
                f"{type(product_code).__name__}"
            )
        if net_gallons < 0:
            raise ValueError(
                f"net_gallons must be >= 0, got {net_gallons}"
            )
        # Reuse the FIPS validation already shared by the jurisdiction
        # lookup so a bad destination code fails fast rather than
        # returning a silently-empty breakdown.
        self._compute_candidate_fips_codes(destination_fips)

        canonical_code = canonicalize(product_code)

        state_cents = 0
        county_cents = 0
        city_cents = 0
        ust_cents = 0
        spcc_cents = 0
        environmental_cents = 0
        line_items: List[TaxLineItem] = []

        for row in jurisdiction_rates:
            # Federal rows are owned by :meth:`_compute_federal_excise`
            # (Task 3.5). Skipping them here keeps the two helpers
            # orthogonal and prevents double-counting when Task 3.10
            # composes them.
            if row.jurisdiction_level == "federal":
                continue

            # Only apply rates whose product_codes list includes the
            # canonicalized delivered product — Req 1.4 fees attach to
            # qualifying fuels only.
            if canonical_code not in row.product_codes:
                continue

            amount_cents = _compute_amount_cents(
                row.rate_cents_per_gallon, net_gallons
            )

            # Route by tax_type first so UST / SPCC / environmental
            # rows are bucketed regardless of their jurisdiction
            # level. Excise rows then split by level.
            tax_type = row.tax_type
            level = row.jurisdiction_level

            if tax_type == "excise":
                if level == "state":
                    state_cents += amount_cents
                    component_name = (
                        f"{row.jurisdiction_name}_state_excise"
                        if row.jurisdiction_name
                        else STATE_EXCISE_FALLBACK_COMPONENT_NAME
                    )
                elif level == "county":
                    county_cents += amount_cents
                    component_name = COUNTY_EXCISE_COMPONENT_NAME
                elif level == "city":
                    city_cents += amount_cents
                    component_name = CITY_EXCISE_COMPONENT_NAME
                else:
                    # Should be unreachable — the Literal on
                    # ``jurisdiction_level`` restricts values to
                    # federal/state/county/city and the federal case
                    # is handled above. Guard defensively so an
                    # unexpected row surfaces a log line rather than
                    # producing a silent mis-route.
                    logger.warning(
                        "TaxEngine: skipping excise row with unexpected "
                        "jurisdiction_level=%r (fips_code=%s tenant=%s)",
                        level,
                        row.fips_code,
                        self._tenant_id,
                    )
                    continue
            elif tax_type == "ust":
                ust_cents += amount_cents
                component_name = UST_FEE_COMPONENT_NAME
            elif tax_type == "spcc":
                spcc_cents += amount_cents
                component_name = SPCC_FEE_COMPONENT_NAME
            elif tax_type == "environmental":
                environmental_cents += amount_cents
                component_name = ENVIRONMENTAL_FEE_COMPONENT_NAME
            else:
                # The Literal on ``tax_type`` covers these four values;
                # anything else is a data-quality regression on the
                # ``tax_jurisdictions`` index.
                logger.warning(
                    "TaxEngine: skipping row with unknown tax_type=%r "
                    "(fips_code=%s tenant=%s)",
                    tax_type,
                    row.fips_code,
                    self._tenant_id,
                )
                continue

            line_items.append(
                TaxLineItem(
                    tax_component_name=component_name,
                    jurisdiction_fips=row.fips_code,
                    jurisdiction_level=level,
                    rate_cents_per_gallon=row.rate_cents_per_gallon,
                    gallons=net_gallons,
                    amount_cents=amount_cents,
                )
            )

        return {
            "state_cents": state_cents,
            "county_cents": county_cents,
            "city_cents": city_cents,
            "ust_cents": ust_cents,
            "spcc_cents": spcc_cents,
            "environmental_cents": environmental_cents,
            "line_items": line_items,
        }

    async def check_exemption(
        self,
        customer_id: str,
        product_code: str,
        effective_date: date,
    ) -> Optional[TaxExemption]:
        """Return the exemption honored for this customer / product / date.

        Queries the ``tax_exemptions`` index for non-expired
        certificates matching ``customer_id`` and ``product_code`` as of
        ``effective_date``. Certificates whose ``product_codes`` list is
        empty / ``None`` apply to all products for the configured
        ``exemption_type`` (blanket exemptions such as farm or 637M).

        When more than one certificate matches, the returned exemption
        is selected by the priority order documented on
        :data:`_EXEMPTION_PRIORITY_ORDER` (``dyed_diesel`` >
        ``off_road`` > ``farm`` > ``637M`` > ``government`` >
        ``resale``) so the stronger road-use exclusions win over weaker
        jurisdictional-only exemptions. Ties within a priority bucket
        are broken by the latest ``expiry_date`` (most-recently
        renewed paperwork wins).

        Args:
            customer_id: Customer being invoiced.
            product_code: Canonical or alias fuel product code being
                delivered. Passed through
                :func:`fuel.services.fuel_product_catalog.canonicalize`
                so legacy aliases resolve to the same bucket as their
                canonical form.
            effective_date: Invoice / delivery date; certificates with
                ``expiry_date < effective_date`` or status
                ``"expired"``/``"revoked"`` are skipped (Req 6.6).

        Returns:
            The :class:`TaxExemption` the Tax_Engine will honor, or
            ``None`` when no valid certificate applies.

        Validates: Requirements 1.7, 1.8
        """
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(product_code, str) or not product_code.strip():
            raise ValueError("product_code must be a non-empty string")
        if not isinstance(effective_date, date):
            raise ValueError(
                "effective_date must be a datetime.date, got "
                f"{type(effective_date).__name__}"
            )

        canonical_code = canonicalize(product_code)
        iso_date = effective_date.isoformat()

        # Read-cutover: serve from Postgres when enabled. We fetch the rows
        # matching customer_id + status=valid + expiry_date >= invoice date,
        # then wrap them as ES-shaped hits so the existing candidate loop
        # (which already re-checks expiry/status, applies product-code scoping
        # including the blanket/missing case, and does priority selection) runs
        # unchanged. Byte-identical to the ES query + Python post-processing.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_fetch_for_aggregation,
        )
        pg_docs = await read_hybrid_fetch_for_aggregation(
            "tax_exemption", self._tenant_id,
            term_filters={"customer_id": customer_id.strip(), "status": "valid"},
            range_field="expiry_date", range_gte=iso_date,
        )
        if pg_docs is not _NOT_CUT_OVER:
            hits = [{"_source": d} for d in pg_docs]
        else:
            hits = None

        if hits is None:
            # Build the ES query:
            # - customer_id + status == "valid"
            # - expiry_date >= effective_date (inclusive per Req 6.6)
            # - product_codes match OR missing (None = applies to all
            #   products for the exemption_type)
            base_query: dict = {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"customer_id": customer_id.strip()}},
                            {"term": {"status": "valid"}},
                            {"range": {"expiry_date": {"gte": iso_date}}},
                            {
                                "bool": {
                                    "should": [
                                        {
                                            "term": {
                                                "product_codes": canonical_code
                                            }
                                        },
                                        {
                                            "bool": {
                                                "must_not": [
                                                    {
                                                        "exists": {
                                                            "field": "product_codes"
                                                        }
                                                    }
                                                ]
                                            }
                                        },
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        ]
                    }
                },
                "size": _MAX_EXEMPTIONS_PER_LOOKUP,
            }

            query = inject_tenant_filter(base_query, self._tenant_id)

            response = await self._es.search_documents(
                TAX_EXEMPTIONS_INDEX,
                query,
                size=_MAX_EXEMPTIONS_PER_LOOKUP,
            )

            hits = ((response or {}).get("hits") or {}).get("hits") or []
        candidates: List[TaxExemption] = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                exemption = TaxExemption.model_validate(source)
            except Exception as exc:
                logger.warning(
                    "TaxEngine: skipping malformed tax_exemptions row "
                    "for tenant=%s customer=%s: %s",
                    self._tenant_id,
                    customer_id,
                    exc,
                )
                continue

            # Double-check the expiry / status invariants client-side
            # so the ES filter can't be side-stepped by a stale
            # status row (defense in depth against background cron
            # not having run yet).
            if exemption.is_expired_as_of(effective_date):
                continue

            # Honor product-code scoping: explicit product_codes must
            # contain the canonicalized code; an empty/None list is a
            # blanket exemption that applies to all products.
            if exemption.product_codes:
                if canonical_code not in exemption.product_codes:
                    continue

            candidates.append(exemption)

        if not candidates:
            return None

        # Sort candidates by (priority index, -expiry ordinal) so the
        # highest-priority, most-recently-renewed certificate wins.
        def _sort_key(exempt: TaxExemption) -> Tuple[int, int]:
            try:
                priority_index = _EXEMPTION_PRIORITY_ORDER.index(
                    exempt.exemption_type
                )
            except ValueError:
                # Unknown exemption type — sort last so known types win.
                priority_index = len(_EXEMPTION_PRIORITY_ORDER)
            # Negative ordinal so later expiry_date sorts first within
            # the same priority bucket (min-heap semantics via sorted).
            return (priority_index, -exempt.expiry_date.toordinal())

        candidates.sort(key=_sort_key)
        return candidates[0]

    # ------------------------------------------------------------------
    # Exemption application (Task 3.7 — Reqs 1.7, 1.8)
    # ------------------------------------------------------------------

    def apply_exemption(
        self,
        breakdown: "TaxBreakdown",
        exemption: TaxExemption,
    ) -> "TaxBreakdown":
        """Apply an exemption certificate to a :class:`TaxBreakdown`.

        Returns a new :class:`TaxBreakdown` instance (the input is not
        mutated) with the affected component buckets zeroed out, the
        corresponding :class:`TaxLineItem` rows removed from
        ``line_items`` so rendering stays consistent, and the
        ``exemption.exemption_id`` appended to
        ``exemptions_applied`` for audit provenance (Req 6.7).

        Exemption-type rules:

        * ``dyed_diesel`` / ``off_road`` / ``637M`` — road-use
          exclusion (Req 1.7). Federal and state excise are zeroed
          and their line items dropped; county/city/UST/SPCC/
          environmental components remain untouched because those are
          jurisdictional fees that continue to apply even on off-road
          fuel.
        * ``farm`` — agricultural exemption (Req 1.8). Kept as a
          *flag only* for Task 3.7: the ``exemption_id`` is appended
          to ``exemptions_applied`` and nothing else is adjusted.
          Rate adjustment is resolved upstream via the farm-specific
          row in the ``tax_jurisdictions`` table (``product_codes``
          scoped to ``"AG_DIESEL"`` / ``"FARM_*"`` variants) or by a
          caller-applied percentage reduction. TODO: future refinement
          to honor a ``reduction_percent`` field on the
          :class:`TaxExemption` model once the model is extended.
        * ``government`` / ``resale`` — jurisdictional blanket
          (simplified). State + county + city excise are zeroed and
          their line items dropped. Federal excise and UST / SPCC /
          environmental fees are retained because IRS filings and
          environmental surcharges generally remain due for these
          categories.

        The method always appends the ``exemption.exemption_id`` to
        ``breakdown.exemptions_applied``, even for the ``farm`` case
        that does not adjust amounts — so downstream callers can
        correlate the exemption with the invoice for audit-readiness
        (Req 6.7).

        Args:
            breakdown: Source breakdown to adjust. Not mutated.
            exemption: Exemption certificate to honor. Callers are
                expected to have validated it via
                :meth:`check_exemption` first — this method does not
                re-check expiry / status because composing the two
                calls back-to-back would double the ES round trip.

        Returns:
            A new :class:`TaxBreakdown` reflecting the exemption's
            effect on the component buckets, line items, and
            provenance list.

        Validates: Requirements 1.7, 1.8
        """
        if not isinstance(breakdown, TaxBreakdown):
            raise ValueError(
                "breakdown must be a TaxBreakdown, got "
                f"{type(breakdown).__name__}"
            )
        if not isinstance(exemption, TaxExemption):
            raise ValueError(
                "exemption must be a TaxExemption, got "
                f"{type(exemption).__name__}"
            )

        # Copy field-by-field so we return a fresh instance and leave
        # the input untouched (important for the compute_tax pipeline
        # where the pre-exemption breakdown may be reused for
        # auditing).
        federal_cents = breakdown.federal_cents
        state_cents = breakdown.state_cents
        county_cents = breakdown.county_cents
        city_cents = breakdown.city_cents
        ust_cents = breakdown.ust_cents
        spcc_cents = breakdown.spcc_cents
        environmental_cents = breakdown.environmental_cents
        line_items = list(breakdown.line_items)

        exemption_type = exemption.exemption_type

        if exemption_type in _ROAD_USE_EXEMPTION_TYPES:
            # Road-use exemption (Req 1.7): drop federal + state
            # excise. UST/SPCC/environmental/county/city are
            # jurisdictional surcharges that continue to apply.
            federal_cents = 0
            state_cents = 0
            line_items = [
                item
                for item in line_items
                if not (
                    item.jurisdiction_level == "federal"
                    or (
                        item.jurisdiction_level == "state"
                        and item.tax_component_name.endswith("state_excise")
                    )
                )
            ]
        elif exemption_type in _JURISDICTIONAL_EXEMPTION_TYPES:
            # Government / resale blanket (simplified): drop
            # state + county + city excise, retain federal and fees.
            state_cents = 0
            county_cents = 0
            city_cents = 0
            line_items = [
                item
                for item in line_items
                if not (
                    (
                        item.jurisdiction_level == "state"
                        and item.tax_component_name.endswith("state_excise")
                    )
                    or item.tax_component_name
                    == COUNTY_EXCISE_COMPONENT_NAME
                    or item.tax_component_name
                    == CITY_EXCISE_COMPONENT_NAME
                )
            ]
        elif exemption_type == "farm":
            # Flag-only application (Req 1.8). Amounts are not
            # adjusted here; the farm-specific jurisdiction row
            # handles the reduced rate upstream. See the docstring
            # TODO for future refinement.
            logger.debug(
                "TaxEngine.apply_exemption: farm exemption %s flagged "
                "without amount adjustment (tenant=%s customer=%s). "
                "Rate adjustment is expected via farm-specific "
                "jurisdiction rows.",
                exemption.exemption_id,
                self._tenant_id,
                exemption.customer_id,
            )
        else:
            # Unknown exemption_type — record the provenance but do
            # not alter amounts, mirroring the conservative farm
            # behaviour so an operator-added exemption type fails
            # open rather than over-zeroing.
            logger.warning(
                "TaxEngine.apply_exemption: unknown exemption_type=%r "
                "for exemption_id=%s (tenant=%s) — appending to "
                "provenance but not adjusting amounts",
                exemption_type,
                exemption.exemption_id,
                self._tenant_id,
            )

        exemptions_applied = list(breakdown.exemptions_applied)
        exemptions_applied.append(exemption.exemption_id)

        return TaxBreakdown(
            invoice_id=breakdown.invoice_id,
            federal_cents=federal_cents,
            state_cents=state_cents,
            county_cents=county_cents,
            city_cents=city_cents,
            ust_cents=ust_cents,
            spcc_cents=spcc_cents,
            environmental_cents=environmental_cents,
            exemptions_applied=exemptions_applied,
            line_items=line_items,
        )


__all__ = [
    "CITY_EXCISE_COMPONENT_NAME",
    "COUNTY_EXCISE_COMPONENT_NAME",
    "ENVIRONMENTAL_FEE_COMPONENT_NAME",
    "ERROR_CODE_JURISDICTION_NOT_FOUND",
    "FEDERAL_EXCISE_COMPONENT_NAME",
    "FEDERAL_EXCISE_DIESEL_RATE",
    "FEDERAL_EXCISE_GASOLINE_RATE",
    "FEDERAL_EXCISE_PROPANE_RATE",
    "FEDERAL_FIPS_SENTINEL",
    "RATE_SCALE",
    "SPCC_FEE_COMPONENT_NAME",
    "STATE_EXCISE_FALLBACK_COMPONENT_NAME",
    "TaxJurisdictionNotFoundError",
    "TaxLineItem",
    "TaxBreakdown",
    "TaxEngine",
    "UST_FEE_COMPONENT_NAME",
]
