"""Unit tests for PricingEngine cache-hit / cache-miss metric emission.

Validates: Requirements 9.1

Asserts that the metric `commerce.pricing.resolve_total{outcome=cache_hit|cache_miss}`
increments correctly depending on whether the pricing rules were served from
Redis cache (cache_hit) or fetched from Elasticsearch (cache_miss).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from commerce.models.account import Account, AccountTier
from commerce.services.pricing_engine import PricingEngine


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "tenant_cache_test"
_PRODUCT_CODE = "ULSD"
_FIXED_MOMENT = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_account() -> Account:
    """Create a valid Account for testing."""
    return Account(
        account_id="acct_cache_test_001",
        tenant_id=_TENANT_ID,
        customer_id="cust_cache_test",
        display_name="Cache Test Account",
        tier=AccountTier.DEFAULT,
        credit_limit_cents=1_000_000,
        net_terms_days=30,
    )


def _make_rule(rule_id: str = "rule_cache_test_001") -> Dict[str, Any]:
    """Create a valid pricing rule dict that will match the test account."""
    return {
        "rule_id": rule_id,
        "price_book_id": "pb_cache_test",
        "tenant_id": _TENANT_ID,
        "product_code": _PRODUCT_CODE,
        "scope_type": "default",
        "scope_value": "default",
        "effective_from": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
        "effective_to": None,
        "min_quantity_gallons": 0,
        "unit_price_cents": 35000,
        "created_at": (_FIXED_MOMENT - timedelta(days=30)).isoformat(),
    }


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


def _make_redis_cache_hit(rules: List[Dict[str, Any]]) -> AsyncMock:
    """Create a mocked Redis client that returns cached rules (cache hit)."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps(rules))
    redis.set = AsyncMock(return_value=True)
    return redis


def _make_redis_cache_miss() -> AsyncMock:
    """Create a mocked Redis client that returns None (cache miss)."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    return redis


def _identity_canonicalize(product_code: str) -> str:
    """Identity canonicalization — passes through unchanged."""
    return product_code


# ---------------------------------------------------------------------------
# Tests: Cache HIT scenario
# ---------------------------------------------------------------------------


class TestCacheHitMetric:
    """When Redis returns cached rules, the 'cache_hit' metric is emitted."""

    @pytest.mark.asyncio
    async def test_cache_hit_emits_metric(self):
        """Redis returns cached rules → verify 'cache_hit' metric is emitted.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        # Redis returns cached rules (cache hit)
        redis = _make_redis_cache_hit([rule])
        es = _make_es_service([rule])  # ES should NOT be called
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            result = await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Verify cache_hit metric was emitted with correct tenant_id
            mock_emit.assert_any_call(_TENANT_ID, "cache_hit")

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_emit_cache_miss(self):
        """Redis returns cached rules → 'cache_miss' metric is NOT emitted.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        redis = _make_redis_cache_hit([rule])
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # cache_miss should NOT have been called
            calls = [call.args for call in mock_emit.call_args_list]
            assert (_TENANT_ID, "cache_miss") not in calls

    @pytest.mark.asyncio
    async def test_cache_hit_also_emits_matched(self):
        """On successful resolution from cache, both 'matched' and 'cache_hit' are emitted.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        redis = _make_redis_cache_hit([rule])
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Both 'matched' and 'cache_hit' should be emitted
            mock_emit.assert_any_call(_TENANT_ID, "matched")
            mock_emit.assert_any_call(_TENANT_ID, "cache_hit")

    @pytest.mark.asyncio
    async def test_cache_hit_metric_includes_correct_tenant_id(self):
        """The cache_hit metric includes the correct tenant_id dimension.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()
        specific_tenant = "tenant_specific_123"

        # Update rule and account to use specific tenant
        rule["tenant_id"] = specific_tenant
        account_data = account.model_dump()
        account_data["tenant_id"] = specific_tenant
        specific_account = Account(**account_data)

        redis = _make_redis_cache_hit([rule])
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=specific_tenant,
                account=specific_account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Verify the metric was emitted with the specific tenant_id
            mock_emit.assert_any_call(specific_tenant, "cache_hit")


# ---------------------------------------------------------------------------
# Tests: Cache MISS scenario
# ---------------------------------------------------------------------------


class TestCacheMissMetric:
    """When Redis returns None and rules are fetched from ES, 'cache_miss' is emitted."""

    @pytest.mark.asyncio
    async def test_cache_miss_emits_metric(self):
        """Redis returns None, ES returns rules → verify 'cache_miss' metric is emitted.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        # Redis returns None (cache miss), ES returns rules
        redis = _make_redis_cache_miss()
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            result = await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Verify cache_miss metric was emitted with correct tenant_id
            mock_emit.assert_any_call(_TENANT_ID, "cache_miss")

    @pytest.mark.asyncio
    async def test_cache_miss_does_not_emit_cache_hit(self):
        """Redis returns None → 'cache_hit' metric is NOT emitted.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        redis = _make_redis_cache_miss()
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # cache_hit should NOT have been called
            calls = [call.args for call in mock_emit.call_args_list]
            assert (_TENANT_ID, "cache_hit") not in calls

    @pytest.mark.asyncio
    async def test_cache_miss_also_emits_matched(self):
        """On successful resolution from ES, both 'matched' and 'cache_miss' are emitted.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        redis = _make_redis_cache_miss()
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Both 'matched' and 'cache_miss' should be emitted
            mock_emit.assert_any_call(_TENANT_ID, "matched")
            mock_emit.assert_any_call(_TENANT_ID, "cache_miss")

    @pytest.mark.asyncio
    async def test_cache_miss_metric_includes_correct_tenant_id(self):
        """The cache_miss metric includes the correct tenant_id dimension.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()
        specific_tenant = "tenant_specific_456"

        rule["tenant_id"] = specific_tenant
        account_data = account.model_dump()
        account_data["tenant_id"] = specific_tenant
        specific_account = Account(**account_data)

        redis = _make_redis_cache_miss()
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=specific_tenant,
                account=specific_account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Verify the metric was emitted with the specific tenant_id
            mock_emit.assert_any_call(specific_tenant, "cache_miss")

    @pytest.mark.asyncio
    async def test_cache_miss_writes_to_redis_after_es_fetch(self):
        """On cache miss, rules fetched from ES are written back to Redis cache.

        **Validates: Requirements 3.6**
        """
        rule = _make_rule()
        account = _make_account()

        redis = _make_redis_cache_miss()
        es = _make_es_service([rule])
        engine = PricingEngine(es, redis, _identity_canonicalize)

        await engine.resolve(
            tenant_id=_TENANT_ID,
            account=account,
            product_code=_PRODUCT_CODE,
            moment=_FIXED_MOMENT,
            quantity_gallons=100.0,
        )

        # Verify Redis set was called to cache the rules
        redis.set.assert_called_once()
        call_args = redis.set.call_args
        # The cache key should include tenant_id and product_code
        assert _TENANT_ID in call_args[0][0]
        assert _PRODUCT_CODE in call_args[0][0]


# ---------------------------------------------------------------------------
# Tests: No Redis client (caching bypassed)
# ---------------------------------------------------------------------------


class TestNoCacheMetric:
    """When Redis client is None, caching is bypassed and 'cache_miss' is emitted."""

    @pytest.mark.asyncio
    async def test_no_redis_emits_cache_miss(self):
        """When redis_client is None, rules come from ES → 'cache_miss' metric.

        **Validates: Requirements 9.1**
        """
        rule = _make_rule()
        account = _make_account()

        # No Redis client — caching bypassed entirely
        es = _make_es_service([rule])
        engine = PricingEngine(es, None, _identity_canonicalize)

        with patch.object(PricingEngine, "_emit_metric") as mock_emit:
            await engine.resolve(
                tenant_id=_TENANT_ID,
                account=account,
                product_code=_PRODUCT_CODE,
                moment=_FIXED_MOMENT,
                quantity_gallons=100.0,
            )

            # Without Redis, it's always a cache miss
            mock_emit.assert_any_call(_TENANT_ID, "cache_miss")
            calls = [call.args for call in mock_emit.call_args_list]
            assert (_TENANT_ID, "cache_hit") not in calls
