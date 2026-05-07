"""
Unit tests for :class:`fuel.services.contract_lift_service.ContractLiftService`.

Covers Task 7.6 / Requirement 8.3.4 of the fuel-ops hardening spec:

* :func:`month_bucket` formats ``YYYY-MM`` in UTC for the current time
  and for explicit datetimes (naive and timezone-aware).
* :meth:`ContractLiftService.record_lift` increments the canonical Redis
  counter with the mandated key shape
  ``contract_lift:{tenant_id}:{contract_id}:{YYYY-MM}`` and stamps the
  monthly-bucket TTL.
* ``record_lift`` degrades to a no-op when Redis is unavailable or when
  the client is ``None`` — a transient outage never fails the
  Loading_Plan commit that triggered the counter update.
* :meth:`ContractLiftService.get_monthly_lift` reads the counter with
  bytes/str/float tolerance and falls back to zero when the key is
  missing.
* :meth:`ContractLiftService.get_summary` projects ``below_minimum``
  and ``percent_of_minimum`` correctly given a contract minimum.
* Property-based tests cover:
    - key shape stability for any (tenant, contract, month) triple,
    - counter monotonicity (bumping by non-negative gallons never
      decreases the total).

Validates: Requirements 8.3.4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fuel.services.contract_lift_service import (
    CONTRACT_LIFT_KEY_PATTERN,
    CONTRACT_LIFT_TTL_SECONDS,
    ContractLiftService,
    ContractLiftSummary,
    month_bucket,
)


# ---------------------------------------------------------------------------
# Fake Redis
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal async Redis mock supporting ``get``, ``incrbyfloat``, and ``expire``.

    Tracks TTL calls separately so tests can assert that a freshly-
    written monthly counter receives the expected 62-day TTL.
    """

    def __init__(self) -> None:
        self.store: Dict[str, float] = {}
        self.ttls: Dict[str, int] = {}
        self.incr_calls: List[Tuple[str, float]] = []

    async def incrbyfloat(self, key: str, amount: float) -> float:
        self.incr_calls.append((key, float(amount)))
        current = self.store.get(key, 0.0)
        new_value = current + float(amount)
        self.store[key] = new_value
        return new_value

    async def get(self, key: str) -> Optional[Any]:
        value = self.store.get(key)
        if value is None:
            return None
        # Mirror real Redis returning strings for floats.
        return str(value)

    async def expire(self, key: str, ttl_seconds: int) -> None:
        self.ttls[key] = int(ttl_seconds)


# ---------------------------------------------------------------------------
# month_bucket
# ---------------------------------------------------------------------------


class TestMonthBucket:
    def test_returns_yyyy_mm_for_explicit_datetime(self) -> None:
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert month_bucket(dt) == "2025-01"

    def test_zero_pads_single_digit_month(self) -> None:
        dt = datetime(2025, 3, 1, tzinfo=timezone.utc)
        assert month_bucket(dt) == "2025-03"

    def test_treats_naive_datetime_as_utc(self) -> None:
        dt = datetime(2025, 6, 1, 0, 0, 0)  # naive
        assert month_bucket(dt) == "2025-06"

    def test_converts_offset_aware_to_utc(self) -> None:
        # 2025-01-01 00:30 in UTC+04 is still 2024-12-31 in UTC.
        from datetime import timedelta, timezone as tz

        dt = datetime(2025, 1, 1, 0, 30, tzinfo=tz(timedelta(hours=4)))
        assert month_bucket(dt) == "2024-12"

    def test_defaults_to_current_month_when_moment_omitted(self) -> None:
        # We can't assert an exact month without freezing time; just check
        # the shape.
        bucket = month_bucket()
        assert len(bucket) == 7
        assert bucket[4] == "-"


# ---------------------------------------------------------------------------
# record_lift
# ---------------------------------------------------------------------------


class TestRecordLift:
    @pytest.mark.asyncio
    async def test_increments_counter_and_stamps_ttl(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)

        moment = datetime(2025, 5, 10, 12, 0, tzinfo=timezone.utc)
        result = await service.record_lift(
            tenant_id="tenant-1",
            contract_id="sc-001",
            gallons=1000.0,
            moment=moment,
        )

        key = CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id="tenant-1", contract_id="sc-001", yyyy_mm="2025-05"
        )
        assert redis.store[key] == pytest.approx(1000.0)
        assert redis.ttls[key] == CONTRACT_LIFT_TTL_SECONDS
        assert result == pytest.approx(1000.0)

    @pytest.mark.asyncio
    async def test_cumulative_bumps_accumulate(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        moment = datetime(2025, 5, 10, tzinfo=timezone.utc)

        await service.record_lift("tenant-1", "sc-001", 250.0, moment=moment)
        total = await service.record_lift(
            "tenant-1", "sc-001", 750.0, moment=moment
        )

        assert total == pytest.approx(1000.0)
        key = CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id="tenant-1", contract_id="sc-001", yyyy_mm="2025-05"
        )
        assert redis.store[key] == pytest.approx(1000.0)

    @pytest.mark.asyncio
    async def test_zero_is_noop(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        result = await service.record_lift("t", "c", 0.0)
        assert result == 0.0
        assert not redis.incr_calls

    @pytest.mark.asyncio
    async def test_negative_is_noop(self) -> None:
        """We never silently decrement the counter."""

        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        result = await service.record_lift("t", "c", -100.0)
        assert result == 0.0
        assert not redis.incr_calls

    @pytest.mark.asyncio
    async def test_no_redis_returns_zero_without_raising(self) -> None:
        service = ContractLiftService(redis_client=None)
        result = await service.record_lift("t", "c", 100.0)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_redis_failure_returns_zero_without_raising(self) -> None:
        redis = AsyncMock()
        redis.incrbyfloat = AsyncMock(side_effect=ConnectionError("redis down"))
        redis.expire = AsyncMock()
        service = ContractLiftService(redis_client=redis)
        result = await service.record_lift("t", "c", 100.0)
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_different_months_use_different_keys(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)

        m1 = datetime(2025, 1, 15, tzinfo=timezone.utc)
        m2 = datetime(2025, 2, 15, tzinfo=timezone.utc)
        await service.record_lift("t", "c", 100.0, moment=m1)
        await service.record_lift("t", "c", 250.0, moment=m2)

        k1 = CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id="t", contract_id="c", yyyy_mm="2025-01"
        )
        k2 = CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id="t", contract_id="c", yyyy_mm="2025-02"
        )
        assert redis.store[k1] == pytest.approx(100.0)
        assert redis.store[k2] == pytest.approx(250.0)

    @pytest.mark.asyncio
    async def test_different_tenants_isolated(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        moment = datetime(2025, 5, 10, tzinfo=timezone.utc)
        await service.record_lift("tenant-a", "c-1", 100.0, moment=moment)
        await service.record_lift("tenant-b", "c-1", 999.0, moment=moment)

        total_a = await service.get_monthly_lift("tenant-a", "c-1", moment=moment)
        total_b = await service.get_monthly_lift("tenant-b", "c-1", moment=moment)
        assert total_a == pytest.approx(100.0)
        assert total_b == pytest.approx(999.0)

    @pytest.mark.asyncio
    async def test_rejects_empty_tenant(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        # Empty tenant / contract degrades to no-op (logged, not raised).
        result = await service.record_lift("", "c", 100.0)
        assert result == 0.0
        assert not redis.incr_calls


# ---------------------------------------------------------------------------
# get_monthly_lift + get_summary
# ---------------------------------------------------------------------------


class TestGetMonthlyLift:
    @pytest.mark.asyncio
    async def test_returns_zero_without_redis(self) -> None:
        service = ContractLiftService(redis_client=None)
        value = await service.get_monthly_lift("t", "c")
        assert value == 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_when_key_missing(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        value = await service.get_monthly_lift("t", "c")
        assert value == 0.0

    @pytest.mark.asyncio
    async def test_coerces_bytes_value(self) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=b"42.5")
        service = ContractLiftService(redis_client=redis)
        value = await service.get_monthly_lift("t", "c")
        assert value == pytest.approx(42.5)

    @pytest.mark.asyncio
    async def test_returns_zero_on_redis_error(self) -> None:
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=ConnectionError("down"))
        service = ContractLiftService(redis_client=redis)
        value = await service.get_monthly_lift("t", "c")
        assert value == 0.0


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_below_minimum_when_counter_short(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        moment = datetime(2025, 5, 10, tzinfo=timezone.utc)
        await service.record_lift("t", "c", 500.0, moment=moment)

        summary = await service.get_summary(
            "t", "c", minimum_lift_gallons_per_month=1000.0, moment=moment
        )
        assert isinstance(summary, ContractLiftSummary)
        assert summary.gallons_lifted_this_month == pytest.approx(500.0)
        assert summary.minimum_lift_gallons_per_month == pytest.approx(1000.0)
        assert summary.percent_of_minimum == pytest.approx(50.0)
        assert summary.below_minimum is True
        assert summary.yyyy_mm == "2025-05"

    @pytest.mark.asyncio
    async def test_at_or_above_minimum_clears_flag(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        moment = datetime(2025, 5, 10, tzinfo=timezone.utc)
        await service.record_lift("t", "c", 1500.0, moment=moment)

        summary = await service.get_summary(
            "t", "c", minimum_lift_gallons_per_month=1000.0, moment=moment
        )
        assert summary.below_minimum is False
        assert summary.percent_of_minimum == pytest.approx(150.0)

    @pytest.mark.asyncio
    async def test_no_minimum_returns_none_percent(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        summary = await service.get_summary("t", "c", None)
        assert summary.percent_of_minimum is None
        assert summary.below_minimum is False

    @pytest.mark.asyncio
    async def test_zero_minimum_returns_none_percent(self) -> None:
        """A zero minimum should behave like 'no minimum configured'."""

        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        summary = await service.get_summary("t", "c", 0.0)
        assert summary.percent_of_minimum is None
        assert summary.below_minimum is False

    @pytest.mark.asyncio
    async def test_summary_to_dict_shape(self) -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        moment = datetime(2025, 5, 10, tzinfo=timezone.utc)
        summary = await service.get_summary(
            "t", "c", minimum_lift_gallons_per_month=500.0, moment=moment
        )
        d = summary.to_dict()
        assert set(d.keys()) == {
            "tenant_id",
            "contract_id",
            "yyyy_mm",
            "gallons_lifted_this_month",
            "minimum_lift_gallons_per_month",
            "percent_of_minimum",
            "below_minimum",
        }


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


# Valid path components: no colons or whitespace (those would be illegal in
# a real Redis key). We don't strictly reject them in the service because
# Redis itself handles any bytes, but for the property we want predictable
# behaviour.
_TENANT_STRATEGY = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"
    ),
    min_size=1,
    max_size=20,
)
_CONTRACT_STRATEGY = _TENANT_STRATEGY


@given(
    tenant_id=_TENANT_STRATEGY,
    contract_id=_CONTRACT_STRATEGY,
    year=st.integers(min_value=2020, max_value=2099),
    month=st.integers(min_value=1, max_value=12),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_key_shape_matches_task_spec(
    tenant_id: str, contract_id: str, year: int, month: int
) -> None:
    """Property: the key always matches the Task 7.6 pattern exactly.

    Validates: Requirement 8.3.4 key layout.
    """

    dt = datetime(year, month, 15, tzinfo=timezone.utc)
    yyyy_mm = month_bucket(dt)
    expected = f"contract_lift:{tenant_id}:{contract_id}:{yyyy_mm}"
    assert (
        CONTRACT_LIFT_KEY_PATTERN.format(
            tenant_id=tenant_id, contract_id=contract_id, yyyy_mm=yyyy_mm
        )
        == expected
    )


@given(
    gallons_list=st.lists(
        st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_counter_is_monotonic_nondecreasing(gallons_list: List[float]) -> None:
    """Property: applying a sequence of non-negative bumps never decreases
    the counter between successive reads.

    Validates: Requirement 8.3.4 monotonicity.
    """

    import asyncio

    async def run() -> None:
        redis = _FakeRedis()
        service = ContractLiftService(redis_client=redis)
        moment = datetime(2025, 5, 10, tzinfo=timezone.utc)
        previous = 0.0
        for gallons in gallons_list:
            await service.record_lift("t", "c", gallons, moment=moment)
            current = await service.get_monthly_lift(
                "t", "c", moment=moment
            )
            assert current + 1e-9 >= previous
            previous = current

    asyncio.run(run())
