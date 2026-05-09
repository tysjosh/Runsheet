"""Property-based tests for PricingEngine.resolve().

**Validates: Requirements 3.2**

Properties tested:
1. Determinism: Given the same (tenant_id, account, product_code, moment,
   quantity_gallons) and the same rule set, resolve() always returns the
   same PricingResult.
2. Precedence invariant: If both an account-scope rule and a tier-scope rule
   match, the account-scope rule always wins. If both tier and default match,
   tier always wins.
3. Quantity-break monotonicity: If a rule with min_quantity_gallons=X matches
   at quantity X, it also matches at quantity X+1.
4. No float drift: unit_price_cents in the result is always an integer,
   never a float.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

from commerce.models.account import Account, AccountTier, CreditState
from commerce.models.price_book import PricingResult, PricingScopeType
from commerce.services.pricing_engine import PricingEngine, PricingError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_prop_test"
_PRODUCT_CODE = "ULSD"
_FIXED_MOMENT = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_unit_prices = st.integers(min_value=0, max_value=99_999_999)
_quantities = st.floats(min_value=0.1, max_value=100_000.0, allow_nan=False, allow_infinity=False)
_min_quantities = st.floats(min_value=0.0, max_value=50_000.0, allow_nan=False, allow_infinity=False)

_tiers = st.sampled_from([t.value for t in AccountTier])

_rule_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_",
    min_size=5,
    max_size=20,
).map(lambda s: f"rule_{s}")


@st.composite
def _account_strategy(draw):
    """Generate a valid Account with a random tier."""
    tier = draw(st.sampled_from(list(AccountTier)))
    return Account(
        account_id=f"acct_{draw(st.text(alphabet='abcdef0123456789', min_size=8, max_size=8))}",
        tenant_id=_TENANT_ID,
        customer_id="cust_prop_test",
        display_name="Property Test Account",
        tier=tier,
        credit_limit_cents=1_000_000,
        net_terms_days=30,
    )


@st.composite
def _pricing_rule_dict(draw, scope_type: str, scope_value: str):
    """Generate a pricing rule dict as returned from ES."""
    rule_id = draw(_rule_ids)
    unit_price_cents = draw(_unit_prices)
    min_qty = draw(_min_quantities)
    # effective_from is always before _FIXED_MOMENT
    days_before = draw(st.integers(min_value=1, max_value=365))
    effective_from = _FIXED_MOMENT - timedelta(days=days_before)

    return {
        "rule_id": rule_id,
        "price_book_id": "pb_test",
        "tenant_id": _TENANT_ID,
        "product_code": _PRODUCT_CODE,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "effective_from": effective_from.isoformat(),
        "effective_to": None,
        "min_quantity_gallons": min_qty,
        "unit_price_cents": unit_price_cents,
        "created_at": effective_from.isoformat(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_es_service(rules: List[Dict[str, Any]]) -> AsyncMock:
    """Create a mocked ES service that returns the given rules."""
    es = AsyncMock()
    es.search_documents = AsyncMock(
        return_value={
            "hits": {
                "hits": [{"_source": r} for r in rules],
                "total": {"value": len(rules)},
            }
        }
    )
    return es


def _make_redis(rules: List[Dict[str, Any]] | None = None) -> AsyncMock:
    """Create a mocked Redis client.

    If rules is provided, the cache returns them (cache hit).
    If None, cache returns None (cache miss).
    """
    redis = AsyncMock()
    if rules is not None:
        redis.get = AsyncMock(return_value=json.dumps(rules))
    else:
        redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    return redis


def _identity_canonicalize(product_code: str) -> str:
    """Identity canonicalization — passes through unchanged."""
    return product_code


# ---------------------------------------------------------------------------
# Property 1: Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Given the same inputs and rule set, resolve() always returns the same result."""

    @given(
        account=_account_strategy(),
        quantity=_quantities,
        unit_price=_unit_prices,
        min_qty=_min_quantities,
    )
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_same_inputs_same_output(
        self, account: Account, quantity: float, unit_price: int, min_qty: float
    ):
        """Determinism: same inputs → same output, every time.

        **Validates: Requirements 3.2**
        """
        assume(quantity >= min_qty)

        # Build a default-scope rule that will always match
        rule = {
            "rule_id": "rule_determinism_test",
            "price_book_id": "pb_test",
            "tenant_id": _TENANT_ID,
            "product_code": _PRODUCT_CODE,
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
            "effective_to": None,
            "min_quantity_gallons": min_qty,
            "unit_price_cents": unit_price,
            "created_at": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
        }

        # Run resolve multiple times with the same inputs
        results = []
        for _ in range(3):
            es = _make_es_service([rule])
            redis = _make_redis(None)
            engine = PricingEngine(es, redis, _identity_canonicalize)
            result = await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=quantity,
            )
            results.append(result)

        # All results must be identical
        for r in results[1:]:
            assert r.unit_price_cents == results[0].unit_price_cents
            assert r.rule_id == results[0].rule_id
            assert r.scope_type == results[0].scope_type


# ---------------------------------------------------------------------------
# Property 2: Precedence invariant
# ---------------------------------------------------------------------------


class TestPrecedenceInvariant:
    """Account scope strictly beats tier, tier strictly beats default."""

    @given(
        account=_account_strategy(),
        account_price=_unit_prices,
        tier_price=_unit_prices,
        default_price=_unit_prices,
    )
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_account_beats_tier_beats_default(
        self,
        account: Account,
        account_price: int,
        tier_price: int,
        default_price: int,
    ):
        """Precedence: account scope strictly beats tier, tier strictly beats default.

        **Validates: Requirements 3.2**
        """
        # Ensure prices are distinct so we can verify which rule won
        assume(account_price != tier_price)
        assume(tier_price != default_price)
        assume(account_price != default_price)

        base_effective_from = (_FIXED_MOMENT - timedelta(days=30)).isoformat()

        account_rule = {
            "rule_id": "rule_account_scope",
            "price_book_id": "pb_test",
            "tenant_id": _TENANT_ID,
            "product_code": _PRODUCT_CODE,
            "scope_type": "account",
            "scope_value": account.account_id,
            "effective_from": base_effective_from,
            "effective_to": None,
            "min_quantity_gallons": 0,
            "unit_price_cents": account_price,
            "created_at": base_effective_from,
        }

        tier_rule = {
            "rule_id": "rule_tier_scope",
            "price_book_id": "pb_test",
            "tenant_id": _TENANT_ID,
            "product_code": _PRODUCT_CODE,
            "scope_type": "tier",
            "scope_value": account.tier.value,
            "effective_from": base_effective_from,
            "effective_to": None,
            "min_quantity_gallons": 0,
            "unit_price_cents": tier_price,
            "created_at": base_effective_from,
        }

        default_rule = {
            "rule_id": "rule_default_scope",
            "price_book_id": "pb_test",
            "tenant_id": _TENANT_ID,
            "product_code": _PRODUCT_CODE,
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": base_effective_from,
            "effective_to": None,
            "min_quantity_gallons": 0,
            "unit_price_cents": default_price,
            "created_at": base_effective_from,
        }

        # Test: all three present → account wins
        all_rules = [account_rule, tier_rule, default_rule]
        es = _make_es_service(all_rules)
        redis = _make_redis(None)
        engine = PricingEngine(es, redis, _identity_canonicalize)

        result = await engine.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=100.0,
        )
        assert result.unit_price_cents == account_price
        assert result.scope_type == PricingScopeType.ACCOUNT

        # Test: tier + default present (no account) → tier wins
        tier_and_default = [tier_rule, default_rule]
        es2 = _make_es_service(tier_and_default)
        redis2 = _make_redis(None)
        engine2 = PricingEngine(es2, redis2, _identity_canonicalize)

        result2 = await engine2.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=100.0,
        )
        assert result2.unit_price_cents == tier_price
        assert result2.scope_type == PricingScopeType.TIER

        # Test: only default present → default wins
        es3 = _make_es_service([default_rule])
        redis3 = _make_redis(None)
        engine3 = PricingEngine(es3, redis3, _identity_canonicalize)

        result3 = await engine3.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=100.0,
        )
        assert result3.unit_price_cents == default_price
        assert result3.scope_type == PricingScopeType.DEFAULT


# ---------------------------------------------------------------------------
# Property 3: Quantity-break monotonicity
# ---------------------------------------------------------------------------


class TestQuantityBreakMonotonicity:
    """If a rule matches at quantity X, it also matches at quantity X+delta (delta > 0)."""

    @given(
        account=_account_strategy(),
        min_qty=st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
        delta=st.floats(min_value=0.01, max_value=10_000.0, allow_nan=False, allow_infinity=False),
        unit_price=_unit_prices,
    )
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_rule_matching_at_x_also_matches_at_x_plus_delta(
        self,
        account: Account,
        min_qty: float,
        delta: float,
        unit_price: int,
    ):
        """Quantity-break monotonicity: matching at X implies matching at X+delta.

        **Validates: Requirements 3.2**
        """
        rule = {
            "rule_id": "rule_qty_mono",
            "price_book_id": "pb_test",
            "tenant_id": _TENANT_ID,
            "product_code": _PRODUCT_CODE,
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
            "effective_to": None,
            "min_quantity_gallons": min_qty,
            "unit_price_cents": unit_price,
            "created_at": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
        }

        # Resolve at exactly min_qty — should match
        es1 = _make_es_service([rule])
        redis1 = _make_redis(None)
        engine1 = PricingEngine(es1, redis1, _identity_canonicalize)

        result_at_x = await engine1.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=min_qty,
        )
        assert result_at_x.unit_price_cents == unit_price

        # Resolve at min_qty + delta — must also match
        es2 = _make_es_service([rule])
        redis2 = _make_redis(None)
        engine2 = PricingEngine(es2, redis2, _identity_canonicalize)

        result_at_x_plus = await engine2.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=min_qty + delta,
        )
        assert result_at_x_plus.unit_price_cents == unit_price
        assert result_at_x_plus.rule_id == result_at_x.rule_id


# ---------------------------------------------------------------------------
# Property 4: No float drift
# ---------------------------------------------------------------------------


class TestNoFloatDrift:
    """unit_price_cents in the result is always an integer, never a float."""

    @given(
        account=_account_strategy(),
        unit_price=_unit_prices,
        quantity=_quantities,
    )
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.asyncio
    async def test_unit_price_cents_is_always_int(
        self,
        account: Account,
        unit_price: int,
        quantity: float,
    ):
        """No float drift: unit_price_cents is always an integer.

        **Validates: Requirements 3.2**

        Constraint C1: All money fields are int cents — no float drift.
        """
        rule = {
            "rule_id": "rule_no_float",
            "price_book_id": "pb_test",
            "tenant_id": _TENANT_ID,
            "product_code": _PRODUCT_CODE,
            "scope_type": "default",
            "scope_value": "default",
            "effective_from": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
            "effective_to": None,
            "min_quantity_gallons": 0,
            "unit_price_cents": unit_price,
            "created_at": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
        }

        es = _make_es_service([rule])
        redis = _make_redis(None)
        engine = PricingEngine(es, redis, _identity_canonicalize)

        result = await engine.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=quantity,
        )

        # The result's unit_price_cents MUST be an int, not a float
        assert isinstance(result.unit_price_cents, int), (
            f"unit_price_cents should be int, got {type(result.unit_price_cents).__name__}: "
            f"{result.unit_price_cents}"
        )
        # Verify no floating point representation crept in
        assert result.unit_price_cents == int(result.unit_price_cents)
