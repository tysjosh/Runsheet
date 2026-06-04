"""Price Protection Service — sell-side contract resolution.

Tasks 4.2, 4.3, and 4.4 of the Fuel Compliance Backbone spec. Implements
the public entry points of the :class:`PriceProtectionService`:

* :meth:`PriceProtectionService.find_active_contract` — look up a single
  active contract matching ``customer_id`` + ``product_code`` with
  ``start_date <= effective_date <= end_date`` and ``status == 'active'``
  via the ``price_protection_contracts`` Elasticsearch index (tenant
  filter injected via :func:`ops.middleware.tenant_guard.inject_tenant_filter`).
* :meth:`PriceProtectionService.resolve_price` — dispatch on
  ``contract_type`` and return a :class:`PriceResolution` carrying the
  effective-price-in-cents, the contract that matched (if any), and the
  echoed market price. When no active contract is found the caller
  receives ``effective_price_cents == market_price_cents`` with
  ``contract_id == None`` so it can fall through to the
  :class:`SalesPricingEngine` (Req 3.8). When the delivery exceeds the
  contract's ``remaining_gallons``, the method populates the
  ``split_gallons_at_contract_price`` and ``split_gallons_at_market_price``
  fields on :class:`PriceResolution` so the caller can emit a split
  invoice line (contracted portion at the contract price, excess at the
  market price) per Task 4.4 / Req 3.5.
* :meth:`PriceProtectionService.decrement_gallons` — atomically decrement
  ``remaining_gallons`` on a contract using a version-based
  compare-and-swap retry loop so that concurrent invoices against the
  same contract never double-spend the remaining allotment (Task 4.3,
  Req 3.4).

Task 4.5 adds the lifecycle sweep:

* :meth:`PriceProtectionService.check_and_transition_contract` —
  inspects a single contract and transitions ``active`` to
  ``exhausted`` (when ``remaining_gallons == 0``) or ``expired``
  (when ``end_date < today``). Intended for use after
  ``decrement_gallons`` so the caller can observe the new terminal
  state on the same request. Returns the new status, or ``None`` when
  no transition occurred.
* :meth:`PriceProtectionService.check_expiry` — scans every active
  contract in the service's tenant and applies the same transition
  logic. Returns the list of ``contract_id``s that were transitioned.
  Designed to be called daily by the cron registered in
  ``bootstrap/compliance.py``; the cron iterates over distinct
  ``tenant_id`` values in ``price_protection_contracts`` and calls
  ``check_expiry`` for each tenant via
  ``price_protection_expiry_job.run_price_protection_expiry_cycle``.

Task 4.6 adds settlement-variance reporting:

* :meth:`PriceProtectionService.compute_settlement_variance` — pure
  static computation returning the integer-cents variance
  ``(market_price_cents - effective_price_cents) * gallons`` rounded
  to the nearest cent. Positive variance means the customer saved
  money under the contract (market > contract); negative variance
  means the contract cost the customer more than the market would
  have (a fixed_price contract during a market dip). Expressed as a
  gain/loss from the customer's perspective, which is the convention
  used by the portfolio reports that aggregate the per-delivery
  variances.
* :meth:`PriceProtectionService.compute_portfolio_variance` — async
  aggregator that sums the per-delivery variances for a contract's
  delivery history and returns a breakdown keyed on ``delivery_id``.
  Callers supply the delivery list explicitly so this method stays
  free of invoice-index assumptions.
* :meth:`PriceProtectionService.iter_contract_invoice_events` — batch
  convenience that scans the ``invoices`` index for events tagged
  with ``contract_id`` and yields minimal ``{delivery_id,
  market_price_cents, effective_price_cents, gallons}`` dicts that
  ``compute_portfolio_variance`` can consume directly. Kept minimal
  for now (Task 4.6) — the Task 4.7 endpoints will plug this into
  the CRUD surface.

Integration point: wired into the ``SalesPricingEngine`` as the
first-priority resolver in Task 4.8, which in turn is called by
``InvoiceService.generate_from_order()`` before tax computation. The
``SalesPricingEngine`` inspects the ``split_gallons_*`` fields to emit
a single blended invoice line or a pair of split lines as appropriate.

Validates: Requirements 3.3, 3.4, 3.5, 3.6, 3.7
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import date
from typing import Any, AsyncIterator, Dict, Final, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from commerce.models.price_protection_contract import PriceProtectionContract
from compliance.services.compliance_es_mappings import (
    PRICE_PROTECTION_CONTRACTS_INDEX,
)
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum number of contract rows expected for any single
#: ``customer_id`` + ``product_code`` lookup. A handful of overlapping
#: active contracts for the same combo is unusual; 100 is a generous
#: ceiling that prevents silent truncation while keeping the ES fetch
#: bounded.
_MAX_CONTRACT_ROWS_PER_LOOKUP: Final[int] = 100

#: Page size for the tenant-wide active-contract scan performed by
#: :meth:`PriceProtectionService.check_expiry`. Set large enough that
#: realistic tenant portfolios (typically <<1k active contracts) fit in
#: a single ES page, but capped so a runaway scan cannot overwhelm the
#: cluster. The daily cron calls ``check_expiry`` exactly once per
#: tenant, so paging would only matter for very large portfolios — we
#: log a warning and move on rather than silently truncating the
#: transition list.
_MAX_EXPIRY_SCAN_ROWS: Final[int] = 10_000

#: Maximum retry attempts for the version-based CAS loop in
#: :meth:`PriceProtectionService.decrement_gallons`. A fresh fetch +
#: write is attempted up to this many times before the caller is asked
#: to retry. Three is plenty for realistic concurrent-invoice scenarios
#: against a single contract; going higher would mask deeper systemic
#: contention that should surface to the operator.
_MAX_DECREMENT_RETRIES: Final[int] = 3

#: Base delay (seconds) for the jittered exponential backoff between
#: decrement retries. Kept small because contract decrement is the hot
#: path during invoice finalization.
_DECREMENT_BACKOFF_BASE_SECONDS: Final[float] = 0.01

#: Tolerance (gallons) for the post-write "did my decrement land"
#: verification. The ``remaining_gallons`` field is a float and ES
#: round-trips via JSON so an exact equality check is brittle; a tenth
#: of a millionth of a gallon is well below the 0.1-gallon rounding
#: precision used by VCF / POD workflows.
_REMAINING_GALLONS_EPSILON: Final[float] = 1e-7


# ---------------------------------------------------------------------------
# PriceResolution — per-delivery resolver output
# ---------------------------------------------------------------------------


class PriceResolution(BaseModel):
    """Resolver output returned by :meth:`PriceProtectionService.resolve_price`.

    Carries the effective-price-in-integer-cents per gallon that the
    caller should bill at, plus optional contract-provenance fields so
    downstream consumers (``InvoiceService``, settlement-variance
    reports in Task 4.6) can correlate the line item back to the
    contract that drove the price.

    The two ``split_gallons_*`` fields carry split-line semantics for
    Task 4.4 / Req 3.5: when a contract's ``remaining_gallons`` is less
    than the delivered gallons, ``resolve_price`` populates
    ``split_gallons_at_contract_price`` with the contracted portion and
    ``split_gallons_at_market_price`` with the excess. The
    ``effective_price_cents`` field still reports the contract price
    (which applies to the contracted portion) and the caller is
    responsible for emitting a pair of invoice lines — one at
    ``effective_price_cents`` for the contracted gallons and one at
    ``market_price_cents`` for the excess. When the delivery fits
    entirely within the remaining gallons (including the exact-match
    edge case where ``gallons == remaining_gallons``) both split fields
    remain ``None`` and the caller bills the entire delivery at
    ``effective_price_cents``.

    All money values are integer cents (Commerce Backbone Constraint C1).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    effective_price_cents: int = Field(
        ...,
        ge=0,
        description=(
            "Effective sell price in integer cents per gallon for the "
            "delivery. Produced by dispatching on ``contract_type`` "
            "(fixed_price / cap_price / collar) or echoing "
            "``market_price_cents`` when no active contract matched. "
            "Under split-line semantics (Task 4.4) this is the price "
            "applied to the contracted portion; the excess portion "
            "bills at ``market_price_cents``."
        ),
    )
    contract_id: Optional[str] = Field(
        default=None,
        description=(
            "Identifier of the contract that drove the resolution. "
            "``None`` when no active contract matched — the caller "
            "falls through to the Sales_Pricing_Engine (Req 3.8)."
        ),
    )
    contract_type: Optional[str] = Field(
        default=None,
        description=(
            "Contract type that drove the resolution: 'fixed_price', "
            "'cap_price', or 'collar'. ``None`` when no active "
            "contract matched."
        ),
    )
    split_gallons_at_contract_price: Optional[float] = Field(
        default=None,
        description=(
            "Gallons billed at the contract price under split-line "
            "semantics (Task 4.4 / Req 3.5). Populated only when the "
            "delivery exceeds ``remaining_gallons`` — equals the "
            "contract's ``remaining_gallons`` at resolution time. "
            "``None`` on the single-price path (delivery fits entirely "
            "within remaining gallons, or no contract matched)."
        ),
    )
    split_gallons_at_market_price: Optional[float] = Field(
        default=None,
        description=(
            "Gallons billed at the market price under split-line "
            "semantics (Task 4.4 / Req 3.5). Populated only when the "
            "delivery exceeds ``remaining_gallons`` — equals "
            "``gallons - remaining_gallons``. ``None`` on the "
            "single-price path (delivery fits entirely within "
            "remaining gallons, or no contract matched)."
        ),
    )
    market_price_cents: int = Field(
        ...,
        ge=0,
        description=(
            "Market price in integer cents per gallon that was passed "
            "into ``resolve_price``. Echoed back on every resolution "
            "so settlement-variance reporting (Task 4.6) has the "
            "inputs it needs without a separate lookup."
        ),
    )

    @field_validator("contract_type")
    @classmethod
    def _contract_type_matches_vocabulary(
        cls, v: Optional[str]
    ) -> Optional[str]:
        """Restrict ``contract_type`` to the three supported values."""
        if v is None:
            return None
        allowed = {"fixed_price", "cap_price", "collar"}
        if v not in allowed:
            raise ValueError(
                "contract_type must be one of "
                f"{sorted(allowed)} when provided, got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PriceProtectionService:
    """Per-tenant sell-side price-protection contract resolver.

    The Price_Protection_Service resolves an effective per-gallon sell
    price by matching deliveries against active contracts in the
    ``price_protection_contracts`` Elasticsearch index. It is
    instantiated once per tenant by ``bootstrap/compliance.py`` and
    injected into the :class:`SalesPricingEngine` as the first-priority
    resolver (Req 3.8, wired in Task 4.8).

    Tasks 4.2–4.4 together implement the resolution surface:

    * :meth:`find_active_contract` — single ES query returning the
      active contract (if any) for a customer / product / date combo.
      Uses :func:`ops.middleware.tenant_guard.inject_tenant_filter` so
      cross-tenant contracts are never visible (Constraint C3).
    * :meth:`resolve_price` — dispatches on ``contract_type`` and
      returns a :class:`PriceResolution`. When the requested gallons
      exceed the contract's ``remaining_gallons``, populates the
      ``split_gallons_*`` fields so the caller can emit a pair of
      invoice lines: contracted gallons at the contract price, excess
      gallons at the market price (Task 4.4 / Req 3.5).
    * :meth:`decrement_gallons` — optimistic-concurrency-guarded
      decrement of ``remaining_gallons`` keyed on ``version`` (Task 4.3
      / Req 3.4).

    Task 4.5 adds the daily lifecycle transitions, and Task 4.6 adds
    settlement-variance reporting:

    * :meth:`compute_settlement_variance` — static per-delivery
      variance in integer cents (Req 3.7).
    * :meth:`compute_portfolio_variance` — aggregates per-delivery
      variances for a contract into a total + per-delivery breakdown.
    * :meth:`iter_contract_invoice_events` — batch-friendly scan of
      the invoice index for rows tagged with ``contract_id`` so
      callers can feed ``compute_portfolio_variance`` without
      assembling the delivery list by hand.

    Args:
        es_service: Elasticsearch handle used to query the
            ``price_protection_contracts`` index. Typed as
            :class:`typing.Any` so a fake service can satisfy the
            interface in tests (mirrors :class:`TaxEngine`).
        tenant_id: Tenant scope for every query. The service instance
            is bound to a single tenant so ``inject_tenant_filter`` is
            applied consistently.

    Validates: Requirements 3.3, 3.4, 3.5, 3.6, 3.7
    """

    def __init__(self, es_service: Any, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        self._es = es_service
        self._tenant_id = tenant_id.strip()

    # ------------------------------------------------------------------
    # Contract lookup
    # ------------------------------------------------------------------

    async def find_active_contract(
        self,
        customer_id: str,
        product_code: str,
        effective_date: date,
    ) -> Optional[PriceProtectionContract]:
        """Return the single active contract matching the inputs.

        Queries the ``price_protection_contracts`` index for rows where:

        * ``customer_id`` matches the requested customer,
        * ``product_code`` matches the requested product (exact match —
          resolution does not canonicalize because contracts are
          written against the canonical code the operator provisioned),
        * ``status == 'active'``,
        * ``start_date <= effective_date <= end_date``.

        The tenant filter is applied via
        :func:`ops.middleware.tenant_guard.inject_tenant_filter`
        (Constraint C3) so cross-tenant contracts are never visible.

        When more than one contract matches (unusual but possible if an
        operator creates overlapping contracts), the one with the
        latest ``start_date`` wins — the most-recently-negotiated
        contract takes priority over older paperwork that still happens
        to be in its window. Ties on ``start_date`` break on the
        latest ``end_date`` so the longer-running contract wins.

        Args:
            customer_id: Customer being invoiced.
            product_code: Canonical fuel product code being delivered.
            effective_date: Invoice / delivery date used to filter
                contracts by their ``[start_date, end_date]`` window.

        Returns:
            The :class:`PriceProtectionContract` that applies, or
            ``None`` when no active contract matches.

        Raises:
            ValueError: When ``customer_id`` or ``product_code`` is
                empty, or when ``effective_date`` is not a
                :class:`datetime.date`.

        Validates: Requirement 3.3
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

        iso_date = effective_date.isoformat()

        # Source candidate rows from Postgres when the commerce read-cutover
        # is active, else from the ``price_protection_contracts`` ES index.
        # We fetch every active contract for the customer/product and apply the
        # ``[start_date, end_date]`` window filter client-side (the same
        # defense-in-depth re-check that already runs below), so both back-ends
        # yield an identical candidate set without needing two range clauses.
        sources = await self._fetch_active_contract_sources(
            customer_id=customer_id.strip(),
            product_code=product_code.strip(),
            iso_date=iso_date,
        )

        candidates: List[PriceProtectionContract] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            try:
                contract = PriceProtectionContract.model_validate(source)
            except Exception as exc:
                logger.warning(
                    "PriceProtectionService: skipping malformed "
                    "price_protection_contracts row for tenant=%s "
                    "customer=%s product=%s: %s",
                    self._tenant_id,
                    customer_id,
                    product_code,
                    exc,
                )
                continue

            # Client-side defense in depth: re-check the window and
            # status invariants in case the ES filter is side-stepped
            # by an unusually stale row.
            if contract.status != "active":
                continue
            if contract.start_date > effective_date:
                continue
            if contract.end_date < effective_date:
                continue
            candidates.append(contract)

        if not candidates:
            return None

        # Sort by (-start_date_ordinal, -end_date_ordinal) so the
        # most-recently-started contract wins; longer-running window
        # breaks ties.
        candidates.sort(
            key=lambda c: (
                -c.start_date.toordinal(),
                -c.end_date.toordinal(),
            )
        )
        return candidates[0]

    async def _fetch_active_contract_sources(
        self,
        customer_id: str,
        product_code: str,
        iso_date: str,
    ) -> List[dict]:
        """Return raw active ``price_protection_contracts`` source docs.

        Serves from Postgres (aggregate ``price_protection_contract``) when the
        commerce read-cutover is active, else from the
        ``price_protection_contracts`` ES index. Both filter on
        ``customer_id`` + ``product_code`` + ``status == 'active'``; the
        ``[start_date, end_date]`` window is re-checked client-side in
        :meth:`find_active_contract`, so the candidate sets are identical.
        """
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )

        pg = await read_hybrid_search(
            "price_protection_contract",
            self._tenant_id,
            term_filters={
                "customer_id": customer_id,
                "product_code": product_code,
                "status": "active",
            },
            page=1,
            size=_MAX_CONTRACT_ROWS_PER_LOOKUP,
        )
        if pg is not _NOT_CUT_OVER:
            return list(pg.get("items", []))

        base_query: dict = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"customer_id": customer_id}},
                        {"term": {"product_code": product_code}},
                        {"term": {"status": "active"}},
                        {"range": {"start_date": {"lte": iso_date}}},
                        {"range": {"end_date": {"gte": iso_date}}},
                    ]
                }
            },
            "size": _MAX_CONTRACT_ROWS_PER_LOOKUP,
        }

        query = inject_tenant_filter(base_query, self._tenant_id)

        response = await self._es.search_documents(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            query,
            size=_MAX_CONTRACT_ROWS_PER_LOOKUP,
        )

        hits = ((response or {}).get("hits") or {}).get("hits") or []
        sources: List[dict] = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if isinstance(source, dict):
                sources.append(source)
        return sources

    # ------------------------------------------------------------------
    # Price resolution
    # ------------------------------------------------------------------

    async def resolve_price(
        self,
        customer_id: str,
        product_code: str,
        market_price_cents: int,
        gallons: float,
        effective_date: date,
    ) -> PriceResolution:
        """Resolve the effective sell price for a delivery.

        Dispatches on ``contract_type`` (Req 3.3):

        * ``fixed_price`` — returns ``fixed_price_cents`` regardless of
          the market price.
        * ``cap_price`` — returns ``min(market_price_cents,
          price_cap_cents)``; customers enjoy the market downside when
          it falls below the cap.
        * ``collar`` — returns ``clamp(market_price_cents,
          price_floor_cents, price_cap_cents)`` — the price is bounded
          between the floor (minimum the customer pays) and the cap
          (maximum the customer pays).
        * No contract found — returns ``market_price_cents`` with
          ``contract_id == None`` and ``contract_type == None`` so the
          caller falls through to the :class:`SalesPricingEngine`
          (Req 3.8).

        Split-line semantics (Task 4.4 / Req 3.5): when an active
        contract matches but ``gallons`` exceeds its
        ``remaining_gallons``, the resolution splits the delivery so
        the contracted portion bills at the resolved contract price and
        the excess bills at the market price. The result carries:

        * ``effective_price_cents`` — the contract price (applies to
          the contracted portion),
        * ``split_gallons_at_contract_price`` — the contract's
          ``remaining_gallons`` at resolution time,
        * ``split_gallons_at_market_price`` — ``gallons -
          remaining_gallons``,
        * ``market_price_cents`` — echoed so the caller can price the
          excess portion without a second lookup.

        The exact-match edge case (``gallons == remaining_gallons``) is
        treated as a non-split single-price resolution — the whole
        delivery fits within the contract, so no excess gallons need to
        be billed at market. Both ``split_gallons_*`` fields stay
        ``None``. The caller then decrements the contract to zero and
        the daily lifecycle cron (Task 4.5) transitions its status to
        ``exhausted``.

        The contract's ``remaining_gallons`` is read at resolution time
        but not decremented here — the caller (``SalesPricingEngine``
        in Task 4.8) is responsible for calling
        :meth:`decrement_gallons` after the invoice is finalized. This
        mirrors the read-then-write split used by the tax engine and
        keeps :meth:`resolve_price` free of write-path side effects.

        Args:
            customer_id: Customer being invoiced.
            product_code: Canonical fuel product code being delivered.
            market_price_cents: Current market / rack price in integer
                cents per gallon. Must be non-negative.
            gallons: Delivered volume in net gallons. Must be
                non-negative. Drives the split-line comparison against
                the contract's ``remaining_gallons``.
            effective_date: Invoice / delivery date used to select the
                active contract (Req 3.2 — contracts apply within
                ``[start_date, end_date]``).

        Returns:
            A :class:`PriceResolution` carrying the effective price and
            the contract provenance. When no active contract matches,
            ``effective_price_cents == market_price_cents`` and both
            ``contract_id`` / ``contract_type`` are ``None``. When the
            delivery exceeds ``remaining_gallons``, the
            ``split_gallons_*`` fields are populated.

        Raises:
            ValueError: When ``market_price_cents`` is negative, when
                ``gallons`` is negative, or when the underlying input
                validation in :meth:`find_active_contract` rejects the
                ``customer_id`` / ``product_code`` / ``effective_date``
                arguments.

        Validates: Requirements 3.3, 3.5
        """
        if not isinstance(market_price_cents, int) or isinstance(
            market_price_cents, bool
        ):
            raise ValueError(
                "market_price_cents must be an int, got "
                f"{type(market_price_cents).__name__}"
            )
        if market_price_cents < 0:
            raise ValueError(
                f"market_price_cents must be >= 0, got {market_price_cents}"
            )
        if gallons < 0:
            raise ValueError(f"gallons must be >= 0, got {gallons}")

        contract = await self.find_active_contract(
            customer_id, product_code, effective_date
        )

        if contract is None:
            # Req 3.8: caller falls through to the Sales_Pricing_Engine.
            return PriceResolution(
                effective_price_cents=market_price_cents,
                contract_id=None,
                contract_type=None,
                market_price_cents=market_price_cents,
            )

        effective_price_cents = self._dispatch_contract_price(
            contract, market_price_cents
        )

        # Split-line check (Task 4.4 / Req 3.5). The model defaults
        # ``remaining_gallons`` to ``contracted_gallons`` during
        # construction, so the ``or 0.0`` guards only against a
        # hand-crafted payload that slipped past the validator.
        remaining = float(contract.remaining_gallons or 0.0)
        requested = float(gallons)
        split_contract_gallons: Optional[float] = None
        split_market_gallons: Optional[float] = None

        # Use a tolerance identical to decrement_gallons so the
        # resolver and the writer agree on "fits exactly" vs "exceeds
        # by a sliver of float noise" and never split a delivery over
        # rounding error. Deliveries up to and including
        # ``remaining_gallons`` (inside the epsilon band) take the
        # single-price path; anything strictly beyond triggers the
        # split.
        if requested > remaining + _REMAINING_GALLONS_EPSILON:
            split_contract_gallons = remaining
            split_market_gallons = requested - remaining
            # Clamp tiny negative-zero artefacts from the subtraction.
            if split_market_gallons < 0:
                split_market_gallons = 0.0

        return PriceResolution(
            effective_price_cents=effective_price_cents,
            contract_id=contract.contract_id,
            contract_type=contract.contract_type,
            market_price_cents=market_price_cents,
            split_gallons_at_contract_price=split_contract_gallons,
            split_gallons_at_market_price=split_market_gallons,
        )

    # ------------------------------------------------------------------
    # Atomic decrement — compare-and-swap on ``version``
    # ------------------------------------------------------------------

    async def decrement_gallons(
        self,
        contract_id: str,
        gallons: float,
    ) -> PriceProtectionContract:
        """Atomically decrement a contract's ``remaining_gallons`` (Req 3.4).

        Implements optimistic concurrency control using the
        ``version`` field on :class:`PriceProtectionContract` as the
        compare-and-swap token so two concurrent invoices against the
        same contract cannot both decrement from the same pre-write
        ``remaining_gallons`` and double-spend the allotment.

        Control flow:

        1. Fetch the current contract by ``contract_id`` through
           :meth:`_fetch_contract`, which scopes the query to the
           service's tenant via
           :func:`ops.middleware.tenant_guard.inject_tenant_filter`.
           A missing contract raises ``ValueError("contract_not_found")``.
        2. Validate that ``gallons`` is strictly positive and does not
           exceed ``remaining_gallons``. Exceeding the remainder raises
           ``ValueError("insufficient_remaining_gallons: ...")`` —
           Task 4.4 will introduce the split-line variant that peels
           off the contracted portion here instead of rejecting.
        3. Compute ``new_remaining = remaining_gallons - gallons`` and
           ``new_version = version + 1``.
        4. Issue an ES partial update that writes
           ``remaining_gallons`` / ``version`` / ``updated_at``. After
           the write, re-fetch the contract and verify ``version ==
           new_version`` and ``|remaining - new_remaining| <
           _REMAINING_GALLONS_EPSILON``. A mismatch means another
           writer snuck in between our read and our write — we retry
           up to :data:`_MAX_DECREMENT_RETRIES` times with jittered
           exponential backoff. After the budget is exhausted, a
           ``ValueError("decrement_gallons: optimistic concurrency
           retry exhausted")`` surfaces to the caller so they can
           retry the invoice finalization at a higher level.

        Note on CAS primitive: the ``ElasticsearchService`` facade used
        by the commerce domain exposes only a partial-update API, not
        the raw ``if_seq_no`` / ``if_primary_term`` OCC headers. This
        implementation therefore simulates CAS with a read-modify-write
        cycle and a post-write verification re-read. Because every
        successful write bumps ``version``, a concurrent writer that
        commits between our read and write will either (a) change the
        ``version`` we observe on re-read, or (b) leave the
        ``remaining_gallons`` at a value that no longer matches the
        local expectation — both mismatches trigger a retry with a
        fresh fetch, restoring the safety property provided by native
        ``if_seq_no`` / ``if_primary_term`` OCC.

        Args:
            contract_id: Identifier of the contract to decrement.
            gallons: Gallons to subtract from ``remaining_gallons``.
                Must be strictly positive and less than or equal to the
                current ``remaining_gallons``.

        Returns:
            The refreshed :class:`PriceProtectionContract` with the
            decremented ``remaining_gallons`` and incremented
            ``version``.

        Raises:
            ValueError: ``"contract_not_found"`` when no contract with
                the given id exists for the service's tenant;
                ``"insufficient_remaining_gallons: ..."`` when
                ``gallons`` exceeds ``remaining_gallons``;
                ``"decrement_gallons: optimistic concurrency retry
                exhausted"`` when every retry attempt observed a
                concurrent modification; or the usual input-validation
                messages when ``contract_id`` is empty or ``gallons``
                is non-positive / non-finite.

        Validates: Requirement 3.4
        """
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        if not isinstance(gallons, (int, float)) or isinstance(gallons, bool):
            raise ValueError(
                f"gallons must be a number, got {type(gallons).__name__}"
            )
        gallons = float(gallons)
        if not math.isfinite(gallons):
            raise ValueError(f"gallons must be finite, got {gallons}")
        if gallons <= 0:
            raise ValueError(f"gallons must be > 0, got {gallons}")

        contract_id = contract_id.strip()

        last_observed_version: Optional[int] = None
        for attempt in range(_MAX_DECREMENT_RETRIES):
            contract = await self._fetch_contract(contract_id)
            # Model guarantees remaining_gallons is non-None after
            # construction (defaults to contracted_gallons).
            remaining = float(contract.remaining_gallons or 0.0)

            if gallons > remaining + _REMAINING_GALLONS_EPSILON:
                raise ValueError(
                    "insufficient_remaining_gallons: contract "
                    f"{contract_id} has {remaining} gallons remaining, "
                    f"needed {gallons}"
                )

            new_remaining = remaining - gallons
            # Guard against tiny negative-zero floats slipping through
            # the arithmetic; clamp to zero rather than rejecting
            # because the gap is inside the epsilon band validated
            # above.
            if new_remaining < 0:
                new_remaining = 0.0

            new_version = contract.version + 1
            patch = {
                "remaining_gallons": new_remaining,
                "version": new_version,
                "updated_at": utcnow().isoformat(),
            }

            await self._es.update_document(
                PRICE_PROTECTION_CONTRACTS_INDEX,
                contract_id,
                patch,
            )

            # Verify the write landed against our pre-write snapshot.
            # A concurrent writer would bump ``version`` to a value we
            # did not write, or leave ``remaining_gallons`` at a value
            # inconsistent with our local delta.
            refreshed = await self._fetch_contract(contract_id)
            last_observed_version = refreshed.version
            refreshed_remaining = float(refreshed.remaining_gallons or 0.0)
            if (
                refreshed.version == new_version
                and abs(refreshed_remaining - new_remaining)
                < _REMAINING_GALLONS_EPSILON
            ):
                # Mirror the post-decrement contract state to Postgres.
                from commerce.services.commerce_persistence_bridge import (
                    mirror_compliance_config_upsert,
                )
                await mirror_compliance_config_upsert(
                    "price_protection_contract", refreshed.model_dump(mode="json")
                )
                return refreshed

            logger.info(
                "PriceProtectionService: OCC conflict on decrement for "
                "contract=%s tenant=%s attempt=%d/%d "
                "(expected version=%d, observed version=%d)",
                contract_id,
                self._tenant_id,
                attempt + 1,
                _MAX_DECREMENT_RETRIES,
                new_version,
                refreshed.version,
            )

            if attempt + 1 < _MAX_DECREMENT_RETRIES:
                await asyncio.sleep(self._decrement_backoff_seconds(attempt))

        raise ValueError(
            "decrement_gallons: optimistic concurrency retry exhausted "
            f"for contract {contract_id} "
            f"(last_observed_version={last_observed_version})"
        )

    # ------------------------------------------------------------------
    # Lifecycle transitions — active → exhausted / expired (Req 3.6)
    # ------------------------------------------------------------------

    async def check_and_transition_contract(
        self,
        contract_id: str,
        today: Optional[date] = None,
    ) -> Optional[str]:
        """Inspect a single contract and transition it to a terminal state.

        Implements the per-contract half of Req 3.6 so callers can run
        the lifecycle check inline after :meth:`decrement_gallons`
        (observe the exhausted state on the same request) without
        waiting for the daily cron.

        Transition rules:

        * ``remaining_gallons == 0`` → ``status = "exhausted"``
        * ``end_date < today``         → ``status = "expired"``

        When both conditions hold simultaneously (zero gallons AND a
        past ``end_date``) the contract transitions to ``exhausted``.
        Rationale: exhaustion reflects what actually consumed the
        coverage — deliveries under the contract drew it to zero —
        and the daily cron later reports this terminal state to the
        settlement-variance reports in Task 4.6 as a fully-consumed
        contract rather than a stranded-gallons one. Either terminal
        state is acceptable per the spec; we prefer ``exhausted``
        because it carries more business information.

        Contracts that are already in a terminal state (``exhausted``,
        ``expired``, or ``cancelled``) are left untouched — the method
        is safe to call repeatedly.

        Args:
            contract_id: Identifier of the contract to inspect.
            today: Optional reference date. Defaults to
                :func:`services.time_utils.utcnow`'s date component so
                callers can inject a deterministic clock in tests.
                Transition logic uses UTC so a contract's ``end_date``
                of 2026-06-01 expires at 00:00 UTC on 2026-06-02 —
                fine for our use case where ``end_date`` is a calendar
                date, not a point-in-time.

        Returns:
            The new status (``"exhausted"`` or ``"expired"``) when a
            transition occurred, or ``None`` when the contract is
            still in its active window with gallons remaining, or
            when it was already in a terminal state.

        Raises:
            ValueError: ``"contract_not_found"`` when no contract with
                the given id exists for the service's tenant, or the
                usual input-validation messages when ``contract_id``
                is empty.

        Validates: Requirement 3.6
        """
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        contract_id = contract_id.strip()

        if today is None:
            today = utcnow().date()
        elif not isinstance(today, date):
            raise ValueError(
                "today must be a datetime.date when provided, got "
                f"{type(today).__name__}"
            )

        contract = await self._fetch_contract(contract_id)

        # Only active contracts are eligible for transition.
        # Already-terminal contracts (exhausted / expired / cancelled)
        # are no-ops so the cron can scan them idempotently.
        if contract.status != "active":
            return None

        new_status = self._compute_terminal_status(contract, today)
        if new_status is None:
            return None

        await self._write_status_transition(contract, new_status)
        return new_status

    async def check_expiry(
        self,
        today: Optional[date] = None,
    ) -> List[str]:
        """Scan every active contract in the tenant and transition terminals.

        Implements the tenant-wide half of Req 3.6. Queries the
        ``price_protection_contracts`` index for rows with
        ``status == "active"`` under the service's tenant, then
        evaluates each row with the same transition rules as
        :meth:`check_and_transition_contract`:

        * ``remaining_gallons == 0`` → ``status = "exhausted"``
        * ``end_date < today``         → ``status = "expired"``
        * Both together                → ``exhausted`` (preferred)

        Intended entry point for the daily cron registered in
        ``bootstrap/compliance.py``. The cron iterates over distinct
        tenants (via the :mod:`commerce.services.price_protection_expiry_job`
        helper) and calls this method once per tenant. Each transition
        is written with a bumped ``version`` so concurrent decrement
        writes do not silently clobber the new terminal status — if
        the post-write re-read observes a concurrent modification the
        contract is simply skipped and retried on the next cron pass
        (the contract either became exhausted through a regular
        decrement, or a human operator is re-activating it).

        Args:
            today: Optional reference date. Defaults to
                :func:`services.time_utils.utcnow`'s date component.

        Returns:
            The list of ``contract_id`` values that were transitioned
            during this sweep. Contracts whose transition writes
            failed (OCC conflict, ES error) are logged and excluded
            from the returned list so the cron's per-cycle summary
            reflects actual transitions.

        Validates: Requirement 3.6
        """
        if today is None:
            today = utcnow().date()
        elif not isinstance(today, date):
            raise ValueError(
                "today must be a datetime.date when provided, got "
                f"{type(today).__name__}"
            )

        # Scan every active contract under the tenant. The service is
        # per-tenant, so ``inject_tenant_filter`` scopes the query
        # (Constraint C3). ``status == "active"`` is the only other
        # filter — we evaluate both transition triggers (zero gallons,
        # past end_date) in Python so we only traverse the index once.
        # Reads from Postgres when the commerce read-cutover is active.
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )

        pg = await read_hybrid_search(
            "price_protection_contract",
            self._tenant_id,
            term_filters={"status": "active"},
            page=1,
            size=_MAX_EXPIRY_SCAN_ROWS,
        )
        if pg is not _NOT_CUT_OVER:
            hits = [{"_source": doc} for doc in pg.get("items", [])]
        else:
            base_query: dict = {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"status": "active"}},
                        ]
                    }
                },
                "size": _MAX_EXPIRY_SCAN_ROWS,
            }
            query = inject_tenant_filter(base_query, self._tenant_id)

            try:
                response = await self._es.search_documents(
                    PRICE_PROTECTION_CONTRACTS_INDEX,
                    query,
                    size=_MAX_EXPIRY_SCAN_ROWS,
                )
            except Exception as exc:
                logger.error(
                    "PriceProtectionService.check_expiry: scan failed for "
                    "tenant=%s: %s",
                    self._tenant_id,
                    exc,
                )
                return []

            hits = ((response or {}).get("hits") or {}).get("hits") or []
        if len(hits) >= _MAX_EXPIRY_SCAN_ROWS:
            # Extremely unusual — a single tenant with more active
            # contracts than the scan window. Log so operators notice
            # and raise the cap if their business genuinely needs it.
            logger.warning(
                "PriceProtectionService.check_expiry: tenant=%s hit the "
                "scan ceiling (%d rows). Some active contracts may be "
                "deferred to the next cron pass.",
                self._tenant_id,
                _MAX_EXPIRY_SCAN_ROWS,
            )

        transitioned: List[str] = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                contract = PriceProtectionContract.model_validate(source)
            except Exception as exc:
                logger.warning(
                    "PriceProtectionService.check_expiry: skipping "
                    "malformed price_protection_contracts row for "
                    "tenant=%s: %s",
                    self._tenant_id,
                    exc,
                )
                continue

            if contract.status != "active":
                # Defense in depth — ES filter should have excluded
                # this, but a stale row could slip through.
                continue

            new_status = self._compute_terminal_status(contract, today)
            if new_status is None:
                continue

            try:
                await self._write_status_transition(contract, new_status)
            except Exception as exc:
                logger.error(
                    "PriceProtectionService.check_expiry: failed to "
                    "transition contract=%s tenant=%s to %s: %s",
                    contract.contract_id,
                    self._tenant_id,
                    new_status,
                    exc,
                )
                continue

            transitioned.append(contract.contract_id)
            logger.info(
                "PriceProtectionService.check_expiry: transitioned "
                "contract=%s tenant=%s to status=%s",
                contract.contract_id,
                self._tenant_id,
                new_status,
            )

        return transitioned

    # ------------------------------------------------------------------
    # Settlement-variance reporting — Task 4.6 / Req 3.7
    # ------------------------------------------------------------------

    @staticmethod
    def compute_settlement_variance(
        market_price_cents: int,
        effective_price_cents: int,
        gallons: float,
    ) -> int:
        """Compute per-delivery settlement variance in integer cents (Req 3.7).

        Returns ``(market_price_cents - effective_price_cents) *
        gallons`` rounded to the nearest integer cent. The sign
        expresses the gain/loss from the customer's perspective on
        that delivery:

        * **Positive** — the customer saved money under the contract
          (market price exceeded the contract price). Typical for a
          ``fixed_price`` or ``cap_price`` contract during a rising
          market.
        * **Zero** — the contract price equalled the market price (or
          zero gallons were delivered). No settlement delta.
        * **Negative** — the contract cost the customer more than the
          market would have. Common for a ``fixed_price`` contract
          during a market dip, or a ``collar`` contract when the
          market slips below the floor.

        The computation is a pure function so it is implemented as a
        ``@staticmethod``: no tenant scope, no ES lookup, no side
        effects. Callers supply all three inputs from the resolution
        captured at invoice time (the :class:`PriceResolution` carries
        both ``market_price_cents`` and ``effective_price_cents``) so
        the variance reflects the exact inputs that drove the bill.

        Rounding: the intermediate product is rounded via
        :func:`round` (banker's rounding) before coercion to ``int``.
        Integer cents are the canonical money unit (Constraint C1), so
        fractional-cent deliveries — e.g. 1337.5 gallons at a 70¢
        spread — collapse to the nearest whole cent rather than
        silently dropping the half-cent.

        Args:
            market_price_cents: Market price in integer cents per
                gallon at the time of the delivery. Must be
                non-negative.
            effective_price_cents: Contract-resolved effective price
                in integer cents per gallon. Must be non-negative.
                May exceed ``market_price_cents`` (negative variance
                case) or equal it (zero-variance edge case).
            gallons: Delivered volume in net gallons. Must be
                non-negative and finite. Zero gallons collapse the
                variance to zero regardless of the price delta.

        Returns:
            The integer-cents variance, signed per the customer's
            perspective (positive = customer saved).

        Raises:
            ValueError: When any input is of the wrong type
                (``bool`` is explicitly rejected for the int fields so
                it does not masquerade as 0/1 cents), when either
                price is negative, when ``gallons`` is negative or
                non-finite.

        Validates: Requirement 3.7
        """
        if isinstance(market_price_cents, bool) or not isinstance(
            market_price_cents, int
        ):
            raise ValueError(
                "market_price_cents must be an int, got "
                f"{type(market_price_cents).__name__}"
            )
        if isinstance(effective_price_cents, bool) or not isinstance(
            effective_price_cents, int
        ):
            raise ValueError(
                "effective_price_cents must be an int, got "
                f"{type(effective_price_cents).__name__}"
            )
        if market_price_cents < 0:
            raise ValueError(
                "market_price_cents must be >= 0, got "
                f"{market_price_cents}"
            )
        if effective_price_cents < 0:
            raise ValueError(
                "effective_price_cents must be >= 0, got "
                f"{effective_price_cents}"
            )
        if isinstance(gallons, bool) or not isinstance(gallons, (int, float)):
            raise ValueError(
                f"gallons must be a number, got {type(gallons).__name__}"
            )
        gallons_f = float(gallons)
        if not math.isfinite(gallons_f):
            raise ValueError(f"gallons must be finite, got {gallons}")
        if gallons_f < 0:
            raise ValueError(f"gallons must be >= 0, got {gallons}")

        spread = market_price_cents - effective_price_cents
        variance = spread * gallons_f
        # ``round`` returns an int for float arguments; coerce
        # defensively so callers can rely on the return type.
        return int(round(variance))

    async def compute_portfolio_variance(
        self,
        contract_id: str,
        deliveries: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Aggregate per-delivery variances into a contract-level report.

        Sums :meth:`compute_settlement_variance` across every entry in
        ``deliveries`` and returns the totals plus a per-delivery
        breakdown so callers can render a settlement report without a
        second pass. The method is an instance method so future
        revisions can enrich the output with fields from the contract
        itself (e.g. ``contracted_gallons``, ``status``) without
        breaking the signature, but the Task 4.6 implementation keeps
        the payload minimal.

        Each entry in ``deliveries`` must carry at minimum:

        * ``delivery_id``: identifier used as the breakdown key.
        * ``market_price_cents``: market price at the time of that
          delivery (integer cents).
        * ``effective_price_cents``: contract-resolved effective price
          that was billed (integer cents).
        * ``gallons``: delivered volume.

        Additional keys are accepted and ignored, so callers can
        stream :meth:`iter_contract_invoice_events` output directly.
        Deliveries that fail input validation are skipped and logged
        — the aggregate is a best-effort snapshot, not a hard
        transaction boundary — so a single malformed row does not
        drop the whole report.

        The ``contract_id`` argument is required for provenance: the
        returned payload echoes it back so portfolio dashboards can
        key rows by contract without a separate lookup. No ES query
        is performed against the contract itself in this iteration —
        Task 4.7's endpoint wiring can enrich the response with the
        contract's current status when the REST surface lands.

        Args:
            contract_id: The contract whose deliveries are being
                aggregated. Non-empty string.
            deliveries: Sequence of delivery dicts. May be empty, in
                which case the returned totals are zero.

        Returns:
            A dict with shape::

                {
                    "contract_id": "<contract_id>",
                    "total_variance_cents": <int>,
                    "total_gallons": <float>,
                    "delivery_count": <int>,
                    "breakdown": [
                        {
                            "delivery_id": "...",
                            "market_price_cents": <int>,
                            "effective_price_cents": <int>,
                            "gallons": <float>,
                            "variance_cents": <int>,
                        },
                        ...
                    ],
                }

            ``total_variance_cents`` is signed per the customer
            perspective (positive = portfolio saved the customer
            money; negative = the contract cost more than market).

        Raises:
            ValueError: When ``contract_id`` is empty, or when
                ``deliveries`` is not a list/iterable of dicts.

        Validates: Requirement 3.7
        """
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        if deliveries is None:
            raise ValueError("deliveries must be a list of dicts, got None")

        contract_id = contract_id.strip()
        breakdown: List[Dict[str, Any]] = []
        total_variance_cents = 0
        total_gallons = 0.0

        for index, entry in enumerate(deliveries):
            if not isinstance(entry, dict):
                logger.warning(
                    "PriceProtectionService.compute_portfolio_variance: "
                    "skipping non-dict delivery at index=%d for "
                    "contract=%s tenant=%s (got %s)",
                    index,
                    contract_id,
                    self._tenant_id,
                    type(entry).__name__,
                )
                continue

            delivery_id = entry.get("delivery_id")
            market_price_cents = entry.get("market_price_cents")
            effective_price_cents = entry.get("effective_price_cents")
            gallons = entry.get("gallons")

            try:
                variance_cents = self.compute_settlement_variance(
                    market_price_cents=market_price_cents,  # type: ignore[arg-type]
                    effective_price_cents=effective_price_cents,  # type: ignore[arg-type]
                    gallons=gallons,  # type: ignore[arg-type]
                )
            except ValueError as exc:
                logger.warning(
                    "PriceProtectionService.compute_portfolio_variance: "
                    "skipping malformed delivery id=%s index=%d for "
                    "contract=%s tenant=%s: %s",
                    delivery_id,
                    index,
                    contract_id,
                    self._tenant_id,
                    exc,
                )
                continue

            gallons_f = float(gallons)  # type: ignore[arg-type]
            total_variance_cents += variance_cents
            total_gallons += gallons_f
            breakdown.append(
                {
                    "delivery_id": delivery_id,
                    "market_price_cents": market_price_cents,
                    "effective_price_cents": effective_price_cents,
                    "gallons": gallons_f,
                    "variance_cents": variance_cents,
                }
            )

        return {
            "contract_id": contract_id,
            "total_variance_cents": total_variance_cents,
            "total_gallons": total_gallons,
            "delivery_count": len(breakdown),
            "breakdown": breakdown,
        }

    async def iter_contract_invoice_events(
        self,
        contract_id: str,
        *,
        batch_size: int = 500,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield minimal delivery dicts for every invoice tagged with a contract.

        Batch-friendly convenience (Task 4.6) so callers can stream a
        contract's delivery history into :meth:`compute_portfolio_variance`
        without assembling the list by hand. Implementation is
        deliberately minimal for now — Task 4.7 will wire the
        downstream endpoints and can replace this with a dedicated
        aggregation query if the scan becomes a bottleneck.

        The method searches the ``invoice_events`` index for rows
        that carry ``contract_id`` in their event payload (written by
        the :class:`SalesPricingEngine` in Task 4.8 when a contract
        drives the price resolution). For each matching event it
        yields a dict with the four fields
        :meth:`compute_portfolio_variance` expects:
        ``delivery_id``, ``market_price_cents``, ``effective_price_cents``,
        and ``gallons``. Events without those fields are skipped so
        partial rollouts (where only a subset of invoice events carry
        the contract tag) do not break the report.

        The scan is tenant-scoped via
        :func:`ops.middleware.tenant_guard.inject_tenant_filter`
        (Constraint C3). Under the hood it uses a single ES
        ``search_documents`` call bounded by ``batch_size``; for very
        large portfolios this would need a scroll / search_after
        loop, but the Task 4.6 scope is explicitly "minimal for now".

        Args:
            contract_id: Contract whose invoice history to iterate.
            batch_size: Maximum events to fetch in one ES round-trip.
                Defaults to 500, matching the order of magnitude used
                by other commerce scans. Must be strictly positive.

        Yields:
            Dicts shaped ``{"delivery_id", "market_price_cents",
            "effective_price_cents", "gallons"}``. Designed to be
            consumed directly by :meth:`compute_portfolio_variance`.

        Raises:
            ValueError: When ``contract_id`` is empty or
                ``batch_size`` is non-positive.

        Validates: Requirement 3.7
        """
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise ValueError(
                f"batch_size must be an int, got {type(batch_size).__name__}"
            )
        if batch_size <= 0:
            raise ValueError(f"batch_size must be > 0, got {batch_size}")

        # Lazy import to avoid a top-level cycle: the commerce mapping
        # module is already imported by this file for the
        # ``price_protection_contracts`` constant, but the invoice-
        # events constant lives in ``commerce_es_mappings`` which may
        # pull in additional commerce services during bootstrap.
        from commerce.services.commerce_es_mappings import (
            INVOICE_EVENTS_INDEX,
        )

        contract_id = contract_id.strip()

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"payload.contract_id": contract_id}},
                    ]
                }
            },
            "size": batch_size,
        }
        query = inject_tenant_filter(base_query, self._tenant_id)

        try:
            response = await self._es.search_documents(
                INVOICE_EVENTS_INDEX, query, size=batch_size
            )
        except Exception as exc:
            logger.error(
                "PriceProtectionService.iter_contract_invoice_events: "
                "scan failed for contract=%s tenant=%s: %s",
                contract_id,
                self._tenant_id,
                exc,
            )
            return

        hits = ((response or {}).get("hits") or {}).get("hits") or []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            payload = source.get("payload")
            if not isinstance(payload, dict):
                continue
            # Confirm the contract tag in-payload so we never leak a
            # row that matched on some incidental ``contract_id``
            # field elsewhere in the document.
            if payload.get("contract_id") != contract_id:
                continue

            delivery_id = payload.get("delivery_id")
            market_price_cents = payload.get("market_price_cents")
            effective_price_cents = payload.get("effective_price_cents")
            gallons = payload.get("gallons")

            if (
                delivery_id is None
                or market_price_cents is None
                or effective_price_cents is None
                or gallons is None
            ):
                continue

            yield {
                "delivery_id": delivery_id,
                "market_price_cents": market_price_cents,
                "effective_price_cents": effective_price_cents,
                "gallons": gallons,
            }

    @staticmethod
    def _compute_terminal_status(
        contract: PriceProtectionContract,
        today: date,
    ) -> Optional[str]:
        """Return the new terminal status for an active contract, or ``None``.

        Applies the Req 3.6 transition rules in order of business
        preference:

        1. If ``remaining_gallons`` is at or below the epsilon band
           (a float-noise guard identical to the one used in
           :meth:`decrement_gallons`) the contract has been fully
           consumed. We transition to ``exhausted`` even when
           ``end_date`` is also in the past — exhaustion carries more
           business information (the allotment was actually used) and
           settlement reporting keys off the terminal status to
           classify stranded vs fully-consumed contracts.
        2. Otherwise, if ``end_date < today`` the coverage window has
           closed with gallons still on the table. Transition to
           ``expired`` so downstream settlement-variance reports
           (Task 4.6) can flag stranded gallons.
        3. Otherwise the contract is still active — no transition.

        Args:
            contract: An active contract whose lifecycle we are
                evaluating. Callers must ensure
                ``contract.status == "active"`` before calling this.
            today: Reference date used for the ``end_date`` comparison.

        Returns:
            ``"exhausted"``, ``"expired"``, or ``None`` when no
            transition is warranted.
        """
        remaining = float(contract.remaining_gallons or 0.0)
        if remaining <= _REMAINING_GALLONS_EPSILON:
            return "exhausted"
        if contract.end_date < today:
            return "expired"
        return None

    async def _write_status_transition(
        self,
        contract: PriceProtectionContract,
        new_status: str,
    ) -> None:
        """Persist a status transition with a bumped ``version``.

        Uses the same partial-update surface as
        :meth:`decrement_gallons` so the OCC counter advances
        monotonically. A concurrent decrement that lands between our
        read and write will either (a) observe our bumped version on
        its own post-write re-read and retry, or (b) race us; either
        way the ``version`` counter preserves the happens-before
        ordering required by Req 3.4 / Req 3.6.

        The service is tenant-scoped, so the write itself does not
        need a tenant filter — the contract was looked up via
        :meth:`_fetch_contract` or a tenant-scoped scan, so the id we
        hold already belongs to this tenant. Constraint C3 is
        preserved transitively.

        Args:
            contract: The contract being transitioned. Used for
                provenance logging and for reading the pre-write
                ``version`` so the partial update can bump it.
            new_status: ``"exhausted"`` or ``"expired"`` — the target
                terminal state.

        Raises:
            Exception: Whatever the underlying ES ``update_document``
                call raises. Callers (``check_expiry``,
                ``check_and_transition_contract``) handle/propagate as
                appropriate for their context.
        """
        patch = {
            "status": new_status,
            "version": contract.version + 1,
            "updated_at": utcnow().isoformat(),
        }
        await self._es.update_document(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            contract.contract_id,
            patch,
        )

        # Mirror the terminal status to Postgres so the source-of-truth (and
        # the read-cutover scan in ``check_expiry``) reflects the transition;
        # otherwise a PG-served scan would re-transition the same contract on
        # every cron pass. Best-effort, same as the decrement path.
        mirrored = contract.model_dump(mode="json")
        mirrored.update(patch)
        from commerce.services.commerce_persistence_bridge import (
            mirror_compliance_config_upsert,
        )

        await mirror_compliance_config_upsert(
            "price_protection_contract", mirrored
        )

    async def _fetch_contract(
        self, contract_id: str
    ) -> PriceProtectionContract:
        """Fetch a contract by ``contract_id`` scoped to the tenant.

        The service is tenant-scoped, so the query is always wrapped
        with :func:`ops.middleware.tenant_guard.inject_tenant_filter`
        (Constraint C3). A document with the matching ``contract_id``
        that belongs to a different tenant will be filtered out by the
        ES layer, so this method raises the same
        ``ValueError("contract_not_found")`` as the genuinely-missing
        case — attackers cannot probe existence of cross-tenant
        contracts via error differentiation.

        Args:
            contract_id: Identifier of the contract to fetch.

        Returns:
            The :class:`PriceProtectionContract` for the given id.

        Raises:
            ValueError: ``"contract_not_found"`` when the contract does
                not exist under the service's tenant.
        """
        base_query: dict = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"contract_id": contract_id}},
                    ]
                }
            },
            "size": 1,
        }
        query = inject_tenant_filter(base_query, self._tenant_id)

        response = await self._es.search_documents(
            PRICE_PROTECTION_CONTRACTS_INDEX,
            query,
            size=1,
        )

        hits = ((response or {}).get("hits") or {}).get("hits") or []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                return PriceProtectionContract.model_validate(source)
            except Exception as exc:
                logger.warning(
                    "PriceProtectionService: malformed contract row "
                    "for tenant=%s contract=%s: %s",
                    self._tenant_id,
                    contract_id,
                    exc,
                )
                raise ValueError("contract_not_found") from exc

        raise ValueError("contract_not_found")

    @staticmethod
    def _decrement_backoff_seconds(attempt: int) -> float:
        """Jittered exponential backoff between decrement retries."""
        base = _DECREMENT_BACKOFF_BASE_SECONDS * (2 ** attempt)
        return base * random.uniform(0.5, 1.5)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dispatch_contract_price(
        contract: PriceProtectionContract,
        market_price_cents: int,
    ) -> int:
        """Compute the effective price for a resolved contract (Req 3.3).

        Pure function over ``contract`` + ``market_price_cents`` so the
        dispatch logic is trivially testable without an ES round-trip.
        The :class:`PriceProtectionContract` validators have already
        enforced that the pricing parameters required by each
        ``contract_type`` are present (fixed_price_cents for
        ``fixed_price``; price_cap_cents for ``cap_price``; both cap
        and floor for ``collar``), so this helper can safely dereference
        them without re-checking.

        Args:
            contract: The active contract selected by
                :meth:`find_active_contract`.
            market_price_cents: Current market price in integer cents
                per gallon.

        Returns:
            The effective price in integer cents per gallon.

        Raises:
            ValueError: When ``contract.contract_type`` is unrecognized
                (belt-and-braces — the model validator already
                restricts it to the three supported values).
        """
        if contract.contract_type == "fixed_price":
            # Model validator guarantees fixed_price_cents is not None.
            return contract.fixed_price_cents  # type: ignore[return-value]

        if contract.contract_type == "cap_price":
            # Model validator guarantees price_cap_cents is not None.
            return min(
                market_price_cents, contract.price_cap_cents  # type: ignore[arg-type]
            )

        if contract.contract_type == "collar":
            # Model validator guarantees both price_cap_cents and
            # price_floor_cents are set and floor <= cap.
            cap = contract.price_cap_cents  # type: ignore[assignment]
            floor = contract.price_floor_cents  # type: ignore[assignment]
            if market_price_cents < floor:
                return floor
            if market_price_cents > cap:
                return cap
            return market_price_cents

        raise ValueError(
            f"Unrecognized contract_type {contract.contract_type!r} "
            "on contract "
            f"{contract.contract_id!r}"
        )
