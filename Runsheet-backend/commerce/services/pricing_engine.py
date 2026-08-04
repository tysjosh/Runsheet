"""PricingEngine.resolve() — deterministic pricing rule resolution.

Implements the resolution algorithm from design §4.1:
1. Canonicalize product_code via canonicalize_fn.
2. Read cached rule set from Redis (TTL 300s), fallback to ES query.
3. Filter rules by effective window at moment.
4. Filter rules by min_quantity_gallons <= quantity_gallons.
5. Precedence lookup: account scope → tier scope → default.
6. Tiebreak: higher min_quantity_gallons first, newer effective_from second,
   lexicographically smaller rule_id third.
7. Return the winning rule; emit metric.

Validates: Requirements 3.2, 3.3, 3.5, 3.6, 9.1
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from commerce.models.account import Account
from commerce.models.price_book import PricingResult, PricingScopeType
from commerce.services.commerce_es_mappings import PRICING_RULES_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.error_codes import CommerceErrorCode
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 300  # 5 minutes (Req 3.6)

# Scope precedence order (lower index = higher priority)
_SCOPE_PRECEDENCE = {
    PricingScopeType.ACCOUNT: 0,
    PricingScopeType.TIER: 1,
    PricingScopeType.DEFAULT: 2,
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PricingError(Exception):
    """Base exception for pricing resolution failures."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @classmethod
    def unknown_product(cls, product_code: str) -> "PricingError":
        """Product code failed canonicalization."""
        return cls(
            code=CommerceErrorCode.PRICING_UNKNOWN_PRODUCT.value,
            message=f"Unknown product code: {product_code!r}",
            details={"product_code": product_code},
        )

    @classmethod
    def no_rule_matched(
        cls, tenant_id: str, product_code: str, account_id: str
    ) -> "PricingError":
        """No pricing rule matched the given parameters."""
        return cls(
            code=CommerceErrorCode.PRICING_NO_RULE.value,
            message=(
                f"No pricing rule matched for tenant={tenant_id}, "
                f"product={product_code}, account={account_id}"
            ),
            details={
                "tenant_id": tenant_id,
                "product_code": product_code,
                "account_id": account_id,
            },
        )


# ---------------------------------------------------------------------------
# PricingEngine
# ---------------------------------------------------------------------------


class PricingEngine:
    """Deterministic pricing rule resolver.

    Resolves the single applicable PricingRule for a given
    (tenant, account, product, moment, quantity) tuple using the
    algorithm defined in design §4.1.

    Args:
        es_service: ElasticsearchService-compatible instance for querying
            pricing_rules_current.
        redis_client: Async Redis client for per-tenant rule caching.
            When None, caching is bypassed (every call hits ES).
        canonicalize_fn: Callable that canonicalizes a product code string.
            Expected to raise on unknown products.
    """

    def __init__(
        self,
        es_service: Any,
        redis_client: Any = None,
        canonicalize_fn: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._es = es_service
        self._redis = redis_client
        self._canonicalize_fn = canonicalize_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve(
        self,
        *,
        tenant_id: str,
        account: Account,
        product_code: str,
        moment: Optional[datetime] = None,
        quantity_gallons: float,
    ) -> PricingResult:
        """Resolve the single applicable PricingRule.

        Returns PricingResult(unit_price_cents, rule_id, scope_type,
        matched_from_cache). Raises PricingError.no_rule_matched when
        no rule applies.

        Args:
            tenant_id: Tenant identifier for data isolation.
            account: The Account being priced against.
            product_code: Raw product code (will be canonicalized).
            moment: Point in time for effective-window filtering.
                Defaults to utcnow() if not provided.
            quantity_gallons: Quantity in gallons for quantity-break filtering.

        Returns:
            PricingResult with the resolved pricing information.

        Raises:
            PricingError: On unknown product or no matching rule.
        """
        if moment is None:
            moment = utcnow()

        # Step 1: Canonicalize product_code
        canonical_product = self._canonicalize_product(product_code)

        # Step 2: Read rule set (cache or ES)
        rules, matched_from_cache = await self._get_rules(tenant_id, canonical_product)

        # Step 3: Filter by effective window at moment
        rules = self._filter_by_effective_window(rules, moment)

        # Step 4: Filter by min_quantity_gallons <= quantity_gallons
        rules = self._filter_by_quantity(rules, quantity_gallons)

        # Step 5-6: Precedence lookup with tiebreak
        winning_rule = self._select_winning_rule(
            rules, account, tenant_id, canonical_product
        )

        if winning_rule is None:
            self._emit_metric(tenant_id, "no_rule")
            raise PricingError.no_rule_matched(
                tenant_id, canonical_product, account.account_id
            )

        # Step 7: Return the winning rule
        scope_type = PricingScopeType(winning_rule["scope_type"])
        result = PricingResult(
            unit_price_cents=int(winning_rule["unit_price_cents"]),
            unit_price_micros=(
                int(winning_rule["unit_price_micros"])
                if winning_rule.get("unit_price_micros") is not None
                else None
            ),
            rule_id=winning_rule["rule_id"],
            scope_type=scope_type,
            matched_from_cache=matched_from_cache,
        )

        outcome = "cache_hit" if matched_from_cache else "cache_miss"
        self._emit_metric(tenant_id, "matched")
        self._emit_metric(tenant_id, outcome)

        return result

    # ------------------------------------------------------------------
    # Step 1: Canonicalization
    # ------------------------------------------------------------------

    def _canonicalize_product(self, product_code: str) -> str:
        """Canonicalize product_code via the injected canonicalize_fn.

        Raises PricingError.unknown_product on failure.
        """
        if self._canonicalize_fn is None:
            # No canonicalize function provided — pass through
            return product_code

        try:
            return self._canonicalize_fn(product_code)
        except (ValueError, TypeError):
            raise PricingError.unknown_product(product_code)

    # ------------------------------------------------------------------
    # Step 2: Cache + ES query
    # ------------------------------------------------------------------

    def _cache_key(self, tenant_id: str, product_code: str) -> str:
        """Build the Redis cache key per design §4.1 / §12."""
        return f"commerce:pricing:{tenant_id}:{product_code}"

    async def _get_rules(
        self, tenant_id: str, product_code: str
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Retrieve the rule set from cache or ES.

        Returns (rules, matched_from_cache).
        """
        cache_key = self._cache_key(tenant_id, product_code)

        # Try Redis cache first
        if self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached is not None:
                    rules = json.loads(cached)
                    return rules, True
            except Exception:
                # Redis failure is non-fatal — fall through to ES
                logger.warning(
                    "Redis cache read failed for key %s, falling back to ES",
                    cache_key,
                    exc_info=True,
                )

        # Cache miss — query ES
        rules = await self._query_es_rules(tenant_id, product_code)

        # Write to cache
        if self._redis is not None:
            try:
                await self._redis.set(
                    cache_key,
                    json.dumps(rules),
                    ex=_CACHE_TTL_SECONDS,
                )
            except Exception:
                logger.warning(
                    "Redis cache write failed for key %s",
                    cache_key,
                    exc_info=True,
                )

        return rules, False

    async def _query_es_rules(
        self, tenant_id: str, product_code: str
    ) -> List[Dict[str, Any]]:
        """Query all rules matching tenant + product.

        Reads from Postgres when the commerce read-cutover is active (the
        engine then applies effective-window / quantity / precedence filtering
        in Python, identical to the ES path); otherwise falls back to the
        ``pricing_rules_current`` ES index.
        """
        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_pricing_rules_by_product,
        )

        pg = await read_pricing_rules_by_product(tenant_id, product_code)
        if pg is not _NOT_CUT_OVER:
            return pg

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"product_code": product_code}},
                    ]
                }
            },
            "size": 1000,  # Generous upper bound for rules per product
        }
        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            PRICING_RULES_CURRENT_INDEX, query, size=1000
        )

        hits = response.get("hits", {}).get("hits", [])
        return [hit["_source"] for hit in hits]

    # ------------------------------------------------------------------
    # Step 3: Filter by effective window
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_by_effective_window(
        rules: List[Dict[str, Any]], moment: datetime
    ) -> List[Dict[str, Any]]:
        """Filter rules by effective window at the given moment.

        A rule is effective if:
        - effective_from <= moment
        - effective_to is None (active indefinitely, Req 3.3) OR effective_to > moment
        """
        result = []
        for rule in rules:
            effective_from = _parse_datetime(rule.get("effective_from"))
            effective_to = rule.get("effective_to")

            # effective_from must be <= moment
            if effective_from is None or effective_from > moment:
                continue

            # effective_to in the past → skip (Req 3.3)
            if effective_to is not None:
                parsed_to = _parse_datetime(effective_to)
                if parsed_to is not None and parsed_to <= moment:
                    continue

            result.append(rule)
        return result

    # ------------------------------------------------------------------
    # Step 4: Filter by quantity
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_by_quantity(
        rules: List[Dict[str, Any]], quantity_gallons: float
    ) -> List[Dict[str, Any]]:
        """Filter rules by min_quantity_gallons <= quantity_gallons.

        Rules with min_quantity_gallons = None or 0 always pass.
        """
        result = []
        for rule in rules:
            min_qty = rule.get("min_quantity_gallons")
            if min_qty is None or min_qty <= 0:
                # No quantity break — always applies
                result.append(rule)
            elif min_qty <= quantity_gallons:
                result.append(rule)
        return result

    # ------------------------------------------------------------------
    # Step 5-6: Precedence + tiebreak
    # ------------------------------------------------------------------

    def _select_winning_rule(
        self,
        rules: List[Dict[str, Any]],
        account: Account,
        tenant_id: str,
        product_code: str,
    ) -> Optional[Dict[str, Any]]:
        """Select the winning rule using precedence and tiebreak logic.

        Precedence (Req 3.2):
        1. account scope (scope_value == account.account_id)
        2. tier scope (scope_value == account.tier)
        3. default scope

        Tiebreak within each tier (Req 3.2, 3.5):
        - Higher min_quantity_gallons first
        - Newer effective_from second
        - Lexicographically smaller rule_id third (determinism)
        """
        # Bucket rules by scope precedence
        account_rules: List[Dict[str, Any]] = []
        tier_rules: List[Dict[str, Any]] = []
        default_rules: List[Dict[str, Any]] = []

        for rule in rules:
            scope_type = rule.get("scope_type", "")
            scope_value = rule.get("scope_value", "")

            if scope_type == PricingScopeType.ACCOUNT.value:
                # Only include if scope_value matches this account
                if scope_value == account.account_id:
                    account_rules.append(rule)
            elif scope_type == PricingScopeType.TIER.value:
                # Only include if scope_value matches this account's tier
                if scope_value == account.tier.value:
                    tier_rules.append(rule)
            elif scope_type == PricingScopeType.DEFAULT.value:
                default_rules.append(rule)

        # Try each precedence tier in order
        for tier_name, tier_bucket in [
            ("account", account_rules),
            ("tier", tier_rules),
            ("default", default_rules),
        ]:
            if not tier_bucket:
                continue

            winner = self._tiebreak(tier_bucket, tenant_id, product_code)
            if winner is not None:
                return winner

        return None

    def _tiebreak(
        self,
        rules: List[Dict[str, Any]],
        tenant_id: str,
        product_code: str,
    ) -> Optional[Dict[str, Any]]:
        """Apply tiebreak logic within a single precedence tier.

        Sort order:
        1. Higher min_quantity_gallons first (descending)
        2. Newer effective_from second (descending)
        3. Lexicographically smaller rule_id third (ascending, for determinism)

        If after sorting the top two rules have identical tiebreak values,
        log an ambiguous resolution warning and emit metric (Req 3.5).
        """
        if not rules:
            return None

        if len(rules) == 1:
            return rules[0]

        # Sort with the tiebreak criteria
        sorted_rules = sorted(
            rules,
            key=lambda r: (
                -(r.get("min_quantity_gallons") or 0),  # Higher first (negate)
                _sort_datetime_desc(r.get("effective_from")),  # Newer first (negate)
                r.get("rule_id", ""),  # Smaller rule_id first (ascending)
            ),
        )

        # Check for ambiguity: top two rules have identical tiebreak values
        if len(sorted_rules) >= 2:
            first = sorted_rules[0]
            second = sorted_rules[1]
            if (
                (first.get("min_quantity_gallons") or 0)
                == (second.get("min_quantity_gallons") or 0)
                and first.get("effective_from") == second.get("effective_from")
            ):
                # Ambiguous — deterministic pick is lexicographically smaller rule_id
                # which is already handled by the sort, but we log + emit metric (Req 3.5)
                logger.warning(
                    "Ambiguous pricing rule resolution for tenant=%s product=%s: "
                    "rules %s and %s tied at same precedence. "
                    "Picking rule_id=%s (lexicographically smaller).",
                    tenant_id,
                    product_code,
                    first.get("rule_id"),
                    second.get("rule_id"),
                    sorted_rules[0].get("rule_id"),
                )
                self._emit_metric(tenant_id, "ambiguous_resolved")

        return sorted_rules[0]

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_metric(tenant_id: str, outcome: str) -> None:
        """Emit commerce.pricing.resolve_total metric.

        In Phase 14 this will be wired to a real metrics backend.
        For now, structured logging serves as the metric emission (Req 9.1).
        """
        logger.info(
            "commerce.pricing.resolve_total",
            extra={
                "metric": "commerce.pricing.resolve_total",
                "tenant_id": tenant_id,
                "outcome": outcome,
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse a datetime value from various formats.

    Handles:
    - datetime objects (returned as-is)
    - ISO-8601 strings (with or without timezone)
    - None (returns None)
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Try parsing ISO format
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
    return None


def _sort_datetime_desc(value: Any) -> str:
    """Return a string suitable for descending datetime sort.

    Newer dates should sort first, so we negate by returning the
    ISO string (which sorts lexicographically for ISO dates) and
    the caller uses it in a tuple where we want descending order.

    We invert by prepending a character that makes newer dates sort
    earlier. Since ISO strings sort ascending naturally, we use a
    complement approach: return the negative timestamp as a string.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        # Unknown dates sort last
        return ""
    # Return negative timestamp for descending sort
    # (larger timestamps = newer = should come first)
    return str(-parsed.timestamp())
