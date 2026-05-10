"""Sales Pricing Engine — sell-price resolver.

Originally introduced by Task 4.8 of the Fuel Compliance Backbone spec
as a narrow skeleton that wired
:class:`commerce.services.price_protection_service.PriceProtectionService`
as the *first-priority* resolver so active sell-side contracts always
won over the strategy-based rules. Task 5.2 extended the skeleton with
the strategy-dispatch entry point. Task 5.3 implements:

* **Priority resolution** in :meth:`SalesPricingEngine.resolve_rule` —
  candidates are sorted into four tiers:
    1. Customer-specific + account-specific (highest priority)
    2. Customer-specific, no account scope
    3. Account-tier (no customer scope)
    4. Product-default (customer_id=None, account_id=None)
  Within each tier, rules are sorted by ``priority`` ascending (lower
  number = higher priority). The first rule after sorting wins.

* **``posted_price`` strategy** in :meth:`SalesPricingEngine.resolve_price`
  — returns the fixed ``posted_price_cents`` from the matched rule.

Tasks 5.4–5.6 implement the remaining strategies:

* **``rack_plus_margin``** (Task 5.4 / Req 11.3) — fetches the rack
  price via ``market_price_cents`` (seam for callers who already know
  the rack price) and adds ``rule.margin_cents``.
* **``tiered_volume``** (Task 5.5 / Req 11.4) — evaluates the
  ``gallons`` parameter against ``rule.tier_thresholds`` to find the
  matching tier and returns its ``unit_price_cents``.
* **``cost_plus``** (Task 5.6 / Req 11.5) — computes
  ``rack_price + (freight_rate_cents_per_mile × route_miles) + margin_cents``.

Task 5.8 replaces the ``NotImplementedError`` for "no rule matched"
with :class:`PricingNoRuleMatchedError` (error code
``pricing.no_rule_matched``) per Req 11.7.

Task 5.9 adds resolution logging to the ``pricing_resolution_log``
ES index per Req 11.8.

Validates: Requirements 3.8, 11.2, 11.3, 11.4, 11.5, 11.7, 11.8
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Final, List, Optional

from commerce.models.pricing_rule import PricingRule
from commerce.services.price_protection_service import (
    PriceProtectionService,
    PriceResolution,
)
from compliance.services.compliance_es_mappings import PRICING_RULES_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Error code raised when no pricing rule matches the customer/product
#: combination (Req 11.7). Follows the same dotted-namespace pattern as
#: ``tax.jurisdiction_not_found`` in the TaxEngine.
ERROR_CODE_PRICING_NO_RULE_MATCHED: str = "pricing.no_rule_matched"

#: Elasticsearch index for pricing resolution audit logs (Req 11.8).
#: Fire-and-forget writes — failures are logged but never block the
#: resolution path.
PRICING_RESOLUTION_LOG_INDEX: str = "pricing_resolution_log"


# ---------------------------------------------------------------------------
# Custom exception (Task 5.8 / Req 11.7)
# ---------------------------------------------------------------------------


class PricingNoRuleMatchedError(ValueError):
    """Raised when no pricing rule matches the customer/product combination.

    Subclasses :class:`ValueError` so existing call sites that catch
    ``ValueError`` for input-validation style failures continue to work
    without special-casing. The ``error_code`` attribute exposes the
    stable :data:`ERROR_CODE_PRICING_NO_RULE_MATCHED` identifier so
    callers can route on the code rather than parsing the message.

    Follows the same pattern as
    :class:`compliance.services.tax_engine.TaxJurisdictionNotFoundError`.

    Attributes:
        error_code: Stable error-code string
            (``"pricing.no_rule_matched"``).
        tenant_id: Tenant scope of the failed resolution.
        customer_id: Customer the resolution was attempted for.
        product_code: Product code the resolution was attempted for.
        effective_date: Date the resolution was attempted for.

    Validates: Requirement 11.7
    """

    error_code: str = ERROR_CODE_PRICING_NO_RULE_MATCHED

    def __init__(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        product_code: str,
        effective_date: date,
    ) -> None:
        self.tenant_id = tenant_id
        self.customer_id = customer_id
        self.product_code = product_code
        self.effective_date = effective_date
        super().__init__(
            f"No pricing rule matched for "
            f"tenant={tenant_id!r}, customer={customer_id!r}, "
            f"product={product_code!r}, "
            f"effective_date={effective_date.isoformat()}. "
            f"Error code: {self.error_code}"
        )

#: Maximum number of pricing-rule rows fetched for any single
#: ``customer_id`` + ``product_code`` lookup. Priority resolution
#: (Task 5.3) is going to narrow this down client-side across the
#: customer-specific / account-tier / product-default tiers, so the
#: ceiling has to be generous enough to cover the full candidate set
#: for a single product without paging. A handful of overlapping
#: rules per product is normal; 100 is a generous cap that prevents
#: silent truncation while keeping the ES fetch bounded.
_MAX_RULE_ROWS_PER_LOOKUP: Final[int] = 100


class SalesPricingEngine:
    """Per-tenant sell-price resolver.

    Responsibilities implemented today:

    * First-priority contract dispatch (Task 4.8 / Req 3.8) — when a
      :class:`PriceProtectionService` is injected, every call to
      :meth:`resolve_price` consults the contract index first and
      honors any active match.
    * Pricing-rule lookup + strategy-dispatch skeleton (Task 5.2 /
      Req 11.2) — the fall-through path from the contract dispatch
      queries the ``pricing_rules`` index via :meth:`resolve_rule`
      and branches on ``rule.strategy``. Each strategy branch is a
      :class:`NotImplementedError` stub pointing at the Task (5.3–5.6)
      that will fill it in.

    Still pending in Phase 5:

    * Priority resolution across customer-specific / account-tier /
      product-default rules (Task 5.3).
    * Strategy implementations for ``posted_price`` (Task 5.3),
      ``rack_plus_margin`` (Task 5.4), ``tiered_volume`` (Task 5.5),
      and ``cost_plus`` (Task 5.6).
    * Daily OPIS rack refresh (Task 5.7).
    * ``pricing.no_rule_matched`` error wiring (Task 5.8).
    * Per-resolution log write (Task 5.9).
    * ``InvoiceService`` integration (Task 5.11).

    Args:
        es_service: Elasticsearch handle used to query the
            ``pricing_rules`` index (and, in Task 5.4, the OPIS rack
            index). Typed as :class:`typing.Any` to match the
            convention used by :class:`PriceProtectionService` and
            :class:`TaxEngine` — keeps test doubles trivial.
        tenant_id: Tenant scope for every resolve call. Bound at
            construction time so ``inject_tenant_filter`` is applied
            consistently on every query.
        price_protection_service: Optional first-priority resolver.
            When provided, :meth:`resolve_price` asks this service to
            attempt a contract match before any strategy evaluation.
            When absent, :meth:`resolve_price` skips straight to the
            strategy dispatch.

    Validates: Requirements 3.8, 11.2
    """

    def __init__(
        self,
        es_service: Any,
        tenant_id: str,
        price_protection_service: Optional[PriceProtectionService] = None,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        self._es = es_service
        self._tenant_id = tenant_id.strip()
        self._price_protection_service = price_protection_service

    # ------------------------------------------------------------------
    # Pricing-rule lookup (Task 5.2)
    # ------------------------------------------------------------------

    async def resolve_rule(
        self,
        customer_id: str,
        account_id: Optional[str],
        product_code: str,
        effective_date: date,
    ) -> Optional[PricingRule]:
        """Return an active :class:`PricingRule` matching the inputs.

        Queries the ``pricing_rules`` Elasticsearch index for rows
        where:

        * ``product_code`` matches the requested product exactly
          (resolution does not canonicalize — rules are written
          against the canonical code the analyst provisioned),
        * ``status == 'active'``,
        * ``effective_date <= effective_date`` (the rule's window
          opened on or before the requested date),
        * ``expiry_date >= effective_date`` or ``expiry_date`` is
          ``None`` (the rule's window has not closed — checked
          client-side after the fetch so the ES query stays simple
          for Task 5.2; see Task 5.3 for the priority-ordered query).

        The tenant filter is applied via
        :func:`ops.middleware.tenant_guard.inject_tenant_filter`
        (Constraint C3) so cross-tenant rules are never visible.

        Today the method returns the first matching rule in the order
        Elasticsearch hands them back. Task 5.3 replaces this trivial
        selection with the full priority ordering — customer-specific
        → account-tier → product-default, then sorted by the
        ``priority`` field ascending. The ``customer_id`` / ``account_id``
        parameters are already on the signature so the Task 5.3
        implementation can land without a breaking change for callers.

        Args:
            customer_id: Customer being invoiced. Accepted today but
                not used to scope the ES query — Task 5.3 will add
                the customer / account / product-default tier
                resolution.
            account_id: Optional billing account identifier. Same
                story as ``customer_id``: accepted today so callers
                do not need to refactor when Task 5.3 lands.
            product_code: Canonical fuel product code being
                delivered.
            effective_date: Invoice / delivery date used to filter
                rules by their ``[effective_date, expiry_date]``
                window.

        Returns:
            The first :class:`PricingRule` that applies, or ``None``
            when no active rule matches.

        Raises:
            ValueError: When ``customer_id`` or ``product_code`` is
                empty / whitespace-only, or when ``effective_date``
                is not a :class:`datetime.date`.

        Validates: Requirement 11.2
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
        if account_id is not None and not isinstance(account_id, str):
            raise ValueError(
                "account_id must be a string or None, got "
                f"{type(account_id).__name__}"
            )

        iso_date = effective_date.isoformat()

        # Build the ES query. Note: ``expiry_date`` is optional on
        # :class:`PricingRule`, so we cannot express the full window
        # filter as a single ``range`` clause without losing the
        # "no expiry" rules. Task 5.2 resolves this by filtering the
        # lower bound in ES and the upper bound client-side after
        # :class:`PricingRule.model_validate` — cheaper than a
        # should/must_not_exists hybrid and preserves the priority
        # ordering that Task 5.3 will layer on top.
        base_query: dict = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"product_code": product_code.strip()}},
                        {"term": {"status": "active"}},
                        {"range": {"effective_date": {"lte": iso_date}}},
                    ]
                }
            },
            "size": _MAX_RULE_ROWS_PER_LOOKUP,
        }

        query = inject_tenant_filter(base_query, self._tenant_id)

        response = await self._es.search_documents(
            PRICING_RULES_INDEX,
            query,
            size=_MAX_RULE_ROWS_PER_LOOKUP,
        )

        hits = ((response or {}).get("hits") or {}).get("hits") or []
        candidates: List[PricingRule] = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                rule = PricingRule.model_validate(source)
            except Exception as exc:
                logger.warning(
                    "SalesPricingEngine: skipping malformed "
                    "pricing_rules row for tenant=%s product=%s: %s",
                    self._tenant_id,
                    product_code,
                    exc,
                )
                continue

            # Client-side defense in depth: re-check the invariants
            # the ES query is supposed to enforce, plus the optional
            # expiry upper bound the ES filter had to skip.
            if rule.status != "active":
                continue
            if rule.effective_date > effective_date:
                continue
            if (
                rule.expiry_date is not None
                and rule.expiry_date < effective_date
            ):
                continue
            candidates.append(rule)

        if not candidates:
            return None

        # Priority resolution (Task 5.3 / Req 11.2):
        # Sort candidates into tiers based on customer_id / account_id
        # specificity, then by the ``priority`` field ascending within
        # each tier. The first candidate after sorting wins.
        #
        # Tier ordering (highest to lowest priority):
        #   Tier 0: customer_id matches AND account_id matches
        #   Tier 1: customer_id matches, account_id is None on the rule
        #   Tier 2: customer_id is None, account_id matches
        #   Tier 3: customer_id is None AND account_id is None (product-default)
        #
        # Rules that specify a customer_id or account_id that does NOT
        # match the request are excluded from the candidate set.

        customer_id_stripped = customer_id.strip()
        account_id_stripped = account_id.strip() if account_id else None

        def _tier_key(rule: PricingRule) -> tuple:
            """Return (tier_rank, priority) for sorting.

            Lower tier_rank = higher specificity = wins first.
            Within the same tier, lower ``priority`` value wins.
            """
            rule_has_customer = rule.customer_id is not None
            rule_has_account = rule.account_id is not None

            # Determine if the rule's customer/account scope matches
            customer_matches = (
                rule_has_customer
                and rule.customer_id == customer_id_stripped
            )
            account_matches = (
                rule_has_account
                and account_id_stripped is not None
                and rule.account_id == account_id_stripped
            )

            if customer_matches and account_matches:
                tier = 0  # Most specific: customer + account
            elif customer_matches and not rule_has_account:
                tier = 1  # Customer-specific, no account scope
            elif not rule_has_customer and account_matches:
                tier = 2  # Account-tier (no customer scope)
            elif not rule_has_customer and not rule_has_account:
                tier = 3  # Product-default
            else:
                # Rule specifies a customer/account that doesn't match
                # the request — push to the bottom so it never wins.
                tier = 99

            return (tier, rule.priority)

        candidates.sort(key=_tier_key)

        # Exclude rules that don't match the request's customer/account
        # (tier 99 rules are non-matching scoped rules).
        top = candidates[0]
        if _tier_key(top)[0] >= 99:
            return None

        return top

    # ------------------------------------------------------------------
    # Price resolution
    # ------------------------------------------------------------------

    async def resolve_price(
        self,
        customer_id: str,
        product_code: str,
        gallons: float,
        terminal_id: str,
        route_miles: float,
        effective_date: date,
        market_price_cents: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> PriceResolution:
        """Resolve the sell price for a delivery.

        The resolution order (Req 11.2) is:

            Price_Protection_Service → customer-specific rule
              → account-tier rule → product-default rule

        Today the engine implements the first-priority contract
        dispatch in full (Task 4.8 / Req 3.8) and the strategy-dispatch
        skeleton (Task 5.2). The dispatch branches for each of the
        four strategies (``posted_price``, ``rack_plus_margin``,
        ``tiered_volume``, ``cost_plus``) raise
        :class:`NotImplementedError` pointing at their follow-up
        tasks (5.3–5.6); when no pricing rule matches, the engine
        also raises :class:`NotImplementedError` — Task 5.8 replaces
        that raise with the ``pricing.no_rule_matched`` error code
        per Req 11.7.

        ``market_price_cents`` is a seam for Tasks 5.4 / 5.6 which
        will source the rack / market price from the OPIS index
        before calling the price-protection resolver. For Task 4.8
        we accepted it as an explicit keyword so the caller
        (typically a unit test or :class:`InvoiceService`) can opt
        into the price-protection dispatch without waiting for the
        rack lookup to land. When the argument is omitted *and* a
        :class:`PriceProtectionService` is wired, the call raises
        :class:`NotImplementedError` — we cannot invoke the contract
        resolver without a market price, and silently substituting
        zero would produce wrong split-line totals. Callers without
        an injected resolver never touch this path because the
        resolver check comes first.

        Args:
            customer_id: Customer being invoiced.
            product_code: Canonical fuel product code being
                delivered.
            gallons: Delivered volume in net gallons.
            terminal_id: Terminal the delivery loaded from. Unused
                today; Task 5.4 consumes it for the OPIS rack
                lookup.
            route_miles: Distance (miles) from the terminal to the
                customer. Unused today; Task 5.6 consumes it for
                the ``cost_plus`` freight calculation.
            effective_date: Invoice / delivery date used to select
                the active contract (Req 3.2 — contracts apply
                within ``[start_date, end_date]``) and the active
                pricing rule (Req 11.2).
            market_price_cents: Current market / rack price in
                integer cents per gallon. Forwarded to
                :meth:`PriceProtectionService.resolve_price` so the
                resolver can dispatch on ``contract_type`` and build
                split-line outputs when the delivery exceeds
                ``remaining_gallons``. Required whenever a
                :class:`PriceProtectionService` is injected; Tasks
                5.2–5.9 supply it via the rack-price lookup.
            account_id: Optional billing account identifier. Passed
                through to :meth:`resolve_rule` so Task 5.3's
                account-tier resolution lands without a breaking
                signature change.

        Returns:
            A :class:`PriceResolution` from the first-priority
            resolver when a contract matches.

        Raises:
            NotImplementedError: When the engine must fall through
                to the strategy dispatch (Tasks 5.3–5.6), when no
                pricing rule matches (Task 5.8 will wire
                ``pricing.no_rule_matched``), or when a
                :class:`PriceProtectionService` is wired but the
                caller did not supply ``market_price_cents``.

        Validates: Requirements 3.8, 11.2
        """
        # First-priority dispatch: consult the Price_Protection_Service.
        # A non-None contract_id on the result means an active contract
        # matched; we return it verbatim so InvoiceService sees the
        # contract price, contract id, contract type, and any split-line
        # fields exactly as the resolver computed them.
        if self._price_protection_service is not None:
            if market_price_cents is None:
                raise NotImplementedError(
                    "SalesPricingEngine.resolve_price requires "
                    "market_price_cents when a PriceProtectionService "
                    "is wired. The OPIS rack-price lookup that "
                    "supplies this value is tracked by Task 5.4 "
                    "(rack_plus_margin strategy). Pass "
                    "market_price_cents explicitly for now."
                )
            resolution = await self._price_protection_service.resolve_price(
                customer_id=customer_id,
                product_code=product_code,
                market_price_cents=market_price_cents,
                gallons=gallons,
                effective_date=effective_date,
            )
            if resolution.contract_id is not None:
                logger.debug(
                    "SalesPricingEngine: price-protection contract "
                    "matched tenant=%s customer=%s product=%s "
                    "contract=%s",
                    self._tenant_id,
                    customer_id,
                    product_code,
                    resolution.contract_id,
                )
                return resolution

        # Fall-through: consult the pricing_rules index and dispatch
        # on the rule's ``strategy`` (Req 11.2). Task 5.2 establishes
        # the dispatch structure — every strategy branch raises
        # :class:`NotImplementedError` pointing at the follow-up
        # task (5.3–5.6) that will implement it.
        rule = await self.resolve_rule(
            customer_id=customer_id,
            account_id=account_id,
            product_code=product_code,
            effective_date=effective_date,
        )

        if rule is None:
            raise PricingNoRuleMatchedError(
                tenant_id=self._tenant_id,
                customer_id=customer_id,
                product_code=product_code,
                effective_date=effective_date,
            )

        strategy = rule.strategy
        if strategy == "posted_price":
            # posted_price strategy (Task 5.3 / Req 11.1): return the
            # fixed price stored on the rule. No market price lookup
            # needed — the price is predetermined by the analyst.
            resolution = PriceResolution(
                effective_price_cents=rule.posted_price_cents,
                contract_id=None,
                contract_type=None,
                market_price_cents=market_price_cents or 0,
            )
        elif strategy == "rack_plus_margin":
            # rack_plus_margin strategy (Task 5.4 / Req 11.3):
            # effective_price = rack_price + rule.margin_cents
            rack_price = self.get_rack_price(
                product_code=product_code,
                terminal_id=rule.terminal_id or terminal_id,
                market_price_cents=market_price_cents,
            )
            effective_price = rack_price + rule.margin_cents
            resolution = PriceResolution(
                effective_price_cents=effective_price,
                contract_id=None,
                contract_type=None,
                market_price_cents=rack_price,
            )
        elif strategy == "tiered_volume":
            # tiered_volume strategy (Task 5.5 / Req 11.4):
            # Evaluate gallons against tier_thresholds to find the
            # matching tier. Uses the `gallons` parameter directly as
            # the volume to evaluate against tiers.
            # NOTE: A future enhancement will query cumulative
            # billing-period gallons instead of using the delivery
            # gallons directly.
            tier_price = self._resolve_tiered_volume(
                gallons=gallons,
                tier_thresholds=rule.tier_thresholds,
                rule_id=rule.rule_id,
            )
            resolution = PriceResolution(
                effective_price_cents=tier_price,
                contract_id=None,
                contract_type=None,
                market_price_cents=market_price_cents or 0,
            )
        elif strategy == "cost_plus":
            # cost_plus strategy (Task 5.6 / Req 11.5):
            # effective_price = rack_price + (freight_rate × miles) + margin
            rack_price = self.get_rack_price(
                product_code=product_code,
                terminal_id=rule.terminal_id or terminal_id,
                market_price_cents=market_price_cents,
            )
            freight_total = rule.freight_rate_cents_per_mile * route_miles
            effective_price = round(
                rack_price + freight_total + rule.margin_cents
            )
            resolution = PriceResolution(
                effective_price_cents=effective_price,
                contract_id=None,
                contract_type=None,
                market_price_cents=rack_price,
            )
        else:  # pragma: no cover — PricingRule validator restricts to
               # the 4 strategies above; this branch exists only for
               # defense-in-depth against a stored row that somehow
               # slipped past the validator (e.g. a manual ES write).
            raise NotImplementedError(
                "SalesPricingEngine: unknown pricing strategy "
                f"{strategy!r} on rule_id={rule.rule_id!r}"
            )

        # Log the resolution (Task 5.9 / Req 11.8)
        await self._log_resolution(
            customer_id=customer_id,
            product_code=product_code,
            gallons=gallons,
            terminal_id=terminal_id,
            route_miles=route_miles,
            rule_id=rule.rule_id,
            strategy=strategy,
            rack_price_cents=resolution.market_price_cents,
            resolved_price_cents=resolution.effective_price_cents,
        )

        return resolution

    # ------------------------------------------------------------------
    # Rack price helper (Task 5.4 / 5.6)
    # ------------------------------------------------------------------

    def get_rack_price(
        self,
        product_code: str,
        terminal_id: str,
        market_price_cents: Optional[int] = None,
    ) -> int:
        """Return the current rack price for a product/terminal.

        This method provides the seam for callers who already know the
        rack price (via ``market_price_cents``). When the caller passes
        ``market_price_cents``, that value is used directly as the rack
        price — this is the primary path for Tasks 5.4 and 5.6 until
        the daily OPIS refresh (Task 5.7) is implemented.

        When ``market_price_cents`` is not provided, the method raises
        :class:`NotImplementedError` pointing at Task 5.7 (daily OPIS
        refresh) which will implement the automatic rack-price lookup
        from the OPIS index.

        Args:
            product_code: Canonical fuel product code.
            terminal_id: Terminal identifier for the OPIS lookup.
            market_price_cents: If provided, used directly as the rack
                price (seam for callers who already know the rack price).

        Returns:
            The rack price in integer cents per gallon.

        Raises:
            NotImplementedError: When ``market_price_cents`` is not
                provided and the OPIS rack-price lookup is not yet
                implemented (Task 5.7).

        Validates: Requirements 11.3, 11.5
        """
        if market_price_cents is not None:
            return market_price_cents
        raise NotImplementedError(
            "SalesPricingEngine.get_rack_price: automatic OPIS rack-price "
            "lookup is not yet implemented. Pass market_price_cents "
            "explicitly, or wait for Task 5.7 (daily OPIS refresh) to "
            f"land. product_code={product_code!r}, "
            f"terminal_id={terminal_id!r}."
        )

    # ------------------------------------------------------------------
    # Resolution logging (Task 5.9 / Req 11.8)
    # ------------------------------------------------------------------

    async def _log_resolution(
        self,
        *,
        customer_id: str,
        product_code: str,
        gallons: float,
        terminal_id: str,
        route_miles: float,
        rule_id: str,
        strategy: str,
        rack_price_cents: int,
        resolved_price_cents: int,
    ) -> None:
        """Log a price resolution to the pricing_resolution_log ES index.

        Fire-and-forget: exceptions are caught and logged as warnings
        so a logging failure never blocks the resolution path. The
        index is not part of the compliance mappings — it uses a
        simple string constant and calls ``es_service.index_document``.

        Validates: Requirement 11.8
        """
        from uuid import uuid4
        from services.time_utils import utcnow

        doc = {
            "resolution_id": f"prl_{uuid4()}",
            "tenant_id": self._tenant_id,
            "customer_id": customer_id,
            "product_code": product_code,
            "gallons": gallons,
            "terminal_id": terminal_id,
            "route_miles": route_miles,
            "rule_id": rule_id,
            "strategy": strategy,
            "rack_price_cents": rack_price_cents,
            "resolved_price_cents": resolved_price_cents,
            "created_at": utcnow().isoformat(),
        }

        try:
            await self._es.index_document(
                PRICING_RESOLUTION_LOG_INDEX,
                doc["resolution_id"],
                doc,
            )
            logger.info(
                "Pricing resolution logged: tenant=%s customer=%s "
                "product=%s rule=%s strategy=%s resolved=%d¢",
                self._tenant_id,
                customer_id,
                product_code,
                rule_id,
                strategy,
                resolved_price_cents,
            )
        except Exception as exc:
            logger.warning(
                "Failed to log pricing resolution (fire-and-forget): "
                "tenant=%s customer=%s product=%s error=%s",
                self._tenant_id,
                customer_id,
                product_code,
                exc,
            )

    # ------------------------------------------------------------------
    # Tiered volume helper (Task 5.5)
    # ------------------------------------------------------------------

    def _resolve_tiered_volume(
        self,
        gallons: float,
        tier_thresholds: List,
        rule_id: str,
    ) -> int:
        """Find the matching tier for the given gallons and return its price.

        Iterates ``tier_thresholds`` to find the tier where
        ``min_gallons <= gallons < max_gallons`` (or the unbounded top
        tier where ``max_gallons is None``).

        NOTE: The ``gallons`` parameter is used directly as the volume
        to evaluate against tiers. A future enhancement will query
        cumulative billing-period gallons from the invoices index.

        Args:
            gallons: Volume (in gallons) to evaluate against tiers.
            tier_thresholds: Ordered list of :class:`TierBreak` objects.
            rule_id: Rule identifier for error messages.

        Returns:
            The ``unit_price_cents`` of the matching tier.

        Raises:
            ValueError: When no tier matches the given gallons (should
                not happen with well-formed tier_thresholds that include
                an unbounded top tier, but defended against for safety).

        Validates: Requirement 11.4
        """
        for tier in tier_thresholds:
            if tier.max_gallons is None:
                # Unbounded top tier — matches everything >= min_gallons
                if gallons >= tier.min_gallons:
                    return tier.unit_price_cents
            else:
                # Bounded tier: min_gallons <= gallons < max_gallons
                if tier.min_gallons <= gallons < tier.max_gallons:
                    return tier.unit_price_cents

        # Fallback: if gallons is below the first tier's min_gallons,
        # use the first tier's price (most common case: gallons=0 with
        # first tier starting at 0).
        if tier_thresholds:
            return tier_thresholds[0].unit_price_cents

        raise ValueError(
            f"SalesPricingEngine: no tier matched gallons={gallons} "
            f"in rule_id={rule_id!r}. Ensure tier_thresholds covers "
            "the full gallon range."
        )
