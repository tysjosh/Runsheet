"""Unit tests for HOSChecker — model creation, caching, and refresh logic.

Tests cover:
- HOSStatus model creation with valid data
- HOSStatus model validation (rejects extra fields, requires all fields)
- HOSEligibility model creation with eligible=True and eligible=False
- HOSEligibility model with earliest_eligible_time
- HOSChecker class instantiation with dependencies
- Constants are correctly defined per FMCSA regulations
- Cache hit returns cached data without calling Geotab
- Cache miss calls Geotab and stores in Redis
- Redis key format is correct (hos:{tenant_id}:{driver_id})
- TTL is set to 900 seconds
- Geotab connector failure is handled gracefully

Validates: Requirements 4.1, 4.6
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from compliance.services.hos_checker import (
    BUFFER_HOURS,
    CACHE_TTL_SECONDS,
    CYCLE_7_DAY_LIMIT,
    CYCLE_8_DAY_LIMIT,
    DRIVE_LIMIT_HOURS,
    HOSChecker,
    HOSEligibility,
    HOSStatus,
    WINDOW_LIMIT_HOURS,
    _build_cache_key,
    _parse_geotab_hos_response,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_hos_status(
    *,
    driver_id: str = "driver_001",
    available_drive_hours: float = 8.5,
    available_window_hours: float = 10.0,
    cumulative_cycle_hours: float = 45.0,
    cycle_type: str = "7_day",
    last_updated: datetime = _FIXED_NOW,
    source: str = "geotab",
) -> HOSStatus:
    """Build a valid HOSStatus instance."""
    return HOSStatus(
        driver_id=driver_id,
        available_drive_hours=available_drive_hours,
        available_window_hours=available_window_hours,
        cumulative_cycle_hours=cumulative_cycle_hours,
        cycle_type=cycle_type,
        last_updated=last_updated,
        source=source,
    )


def _make_dependencies():
    """Create mocked dependencies for HOSChecker."""
    es_service = AsyncMock()
    redis_client = MagicMock()
    redis_client.get = AsyncMock(return_value=None)
    redis_client.setex = AsyncMock()
    geotab_connector = AsyncMock()
    return es_service, redis_client, geotab_connector


# ---------------------------------------------------------------------------
# HOSStatus Model Tests
# ---------------------------------------------------------------------------


class TestHOSStatus:
    """Tests for the HOSStatus Pydantic model."""

    def test_create_valid_hos_status(self):
        """HOSStatus can be created with all required fields."""
        status = _make_hos_status()

        assert status.driver_id == "driver_001"
        assert status.available_drive_hours == 8.5
        assert status.available_window_hours == 10.0
        assert status.cumulative_cycle_hours == 45.0
        assert status.cycle_type == "7_day"
        assert status.last_updated == _FIXED_NOW
        assert status.source == "geotab"

    def test_create_with_8_day_cycle(self):
        """HOSStatus supports 8-day cycle type."""
        status = _make_hos_status(cycle_type="8_day", cumulative_cycle_hours=55.0)

        assert status.cycle_type == "8_day"
        assert status.cumulative_cycle_hours == 55.0

    def test_default_source_is_geotab(self):
        """HOSStatus defaults source to 'geotab'."""
        status = HOSStatus(
            driver_id="driver_002",
            available_drive_hours=11.0,
            available_window_hours=14.0,
            cumulative_cycle_hours=0.0,
            cycle_type="7_day",
            last_updated=_FIXED_NOW,
        )
        assert status.source == "geotab"

    def test_rejects_extra_fields(self):
        """HOSStatus rejects extra fields (extra='forbid')."""
        with pytest.raises(Exception):
            HOSStatus(
                driver_id="driver_001",
                available_drive_hours=8.5,
                available_window_hours=10.0,
                cumulative_cycle_hours=45.0,
                cycle_type="7_day",
                last_updated=_FIXED_NOW,
                source="geotab",
                unexpected_field="should_fail",
            )

    def test_requires_driver_id(self):
        """HOSStatus requires driver_id field."""
        with pytest.raises(Exception):
            HOSStatus(
                available_drive_hours=8.5,
                available_window_hours=10.0,
                cumulative_cycle_hours=45.0,
                cycle_type="7_day",
                last_updated=_FIXED_NOW,
            )

    def test_zero_hours_valid(self):
        """HOSStatus accepts zero hours (driver at limit)."""
        status = _make_hos_status(
            available_drive_hours=0.0,
            available_window_hours=0.0,
            cumulative_cycle_hours=60.0,
        )
        assert status.available_drive_hours == 0.0
        assert status.available_window_hours == 0.0
        assert status.cumulative_cycle_hours == 60.0


# ---------------------------------------------------------------------------
# HOSEligibility Model Tests
# ---------------------------------------------------------------------------


class TestHOSEligibility:
    """Tests for the HOSEligibility Pydantic model."""

    def test_create_eligible(self):
        """HOSEligibility can represent an eligible driver."""
        eligibility = HOSEligibility(
            driver_id="driver_001",
            eligible=True,
            reasons=[],
        )

        assert eligibility.driver_id == "driver_001"
        assert eligibility.eligible is True
        assert eligibility.reasons == []
        assert eligibility.earliest_eligible_time is None

    def test_create_ineligible_with_reasons(self):
        """HOSEligibility can represent an ineligible driver with reasons."""
        eligibility = HOSEligibility(
            driver_id="driver_002",
            eligible=False,
            reasons=[
                "Insufficient drive hours: 2.0h available, need 3.5h (including 0.5h buffer)",
                "14-hour window exceeded",
            ],
        )

        assert eligibility.eligible is False
        assert len(eligibility.reasons) == 2
        assert "Insufficient drive hours" in eligibility.reasons[0]

    def test_create_with_earliest_eligible_time(self):
        """HOSEligibility can include earliest_eligible_time for blocked routes."""
        earliest = datetime(2026, 6, 2, 6, 0, 0, tzinfo=timezone.utc)
        eligibility = HOSEligibility(
            driver_id="driver_003",
            eligible=False,
            reasons=["Cycle limit exceeded"],
            earliest_eligible_time=earliest,
        )

        assert eligibility.earliest_eligible_time == earliest

    def test_reasons_default_to_empty_list(self):
        """HOSEligibility defaults reasons to empty list."""
        eligibility = HOSEligibility(
            driver_id="driver_001",
            eligible=True,
        )
        assert eligibility.reasons == []

    def test_rejects_extra_fields(self):
        """HOSEligibility rejects extra fields (extra='forbid')."""
        with pytest.raises(Exception):
            HOSEligibility(
                driver_id="driver_001",
                eligible=True,
                extra_field="nope",
            )


# ---------------------------------------------------------------------------
# HOSChecker Class Tests
# ---------------------------------------------------------------------------


class TestHOSChecker:
    """Tests for HOSChecker class instantiation and structure."""

    def test_instantiation(self):
        """HOSChecker can be instantiated with required dependencies."""
        es_service, redis_client, geotab_connector = _make_dependencies()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="tenant_001")

        assert checker._es is es_service
        assert checker._redis is redis_client
        assert checker._geotab is geotab_connector
        assert checker._tenant_id == "tenant_001"

    def test_instantiation_default_tenant_id(self):
        """HOSChecker defaults tenant_id to empty string."""
        es_service, redis_client, geotab_connector = _make_dependencies()

        checker = HOSChecker(es_service, redis_client, geotab_connector)

        assert checker._tenant_id == ""

    def test_has_is_eligible_method(self):
        """HOSChecker has an is_eligible async method."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        checker = HOSChecker(es_service, redis_client, geotab_connector)

        assert hasattr(checker, "is_eligible")
        assert callable(checker.is_eligible)

    def test_has_refresh_hos_data_method(self):
        """HOSChecker has a refresh_hos_data async method."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        checker = HOSChecker(es_service, redis_client, geotab_connector)

        assert hasattr(checker, "refresh_hos_data")
        assert callable(checker.refresh_hos_data)

    def test_has_get_hos_status_method(self):
        """HOSChecker has a get_hos_status async method."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        checker = HOSChecker(es_service, redis_client, geotab_connector)

        assert hasattr(checker, "get_hos_status")
        assert callable(checker.get_hos_status)

    @pytest.mark.asyncio
    async def test_is_eligible_method_exists(self):
        """is_eligible is an async method that can be called."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        checker = HOSChecker(es_service, redis_client, geotab_connector)

        assert hasattr(checker, "is_eligible")
        assert callable(checker.is_eligible)


# ---------------------------------------------------------------------------
# Constants Tests
# ---------------------------------------------------------------------------


class TestHOSConstants:
    """Tests verifying HOS constants match FMCSA regulations."""

    def test_drive_limit_hours(self):
        """Drive limit is 11 hours per FMCSA regulations."""
        assert DRIVE_LIMIT_HOURS == 11.0

    def test_buffer_hours(self):
        """Buffer is 30 minutes (0.5 hours) per design spec."""
        assert BUFFER_HOURS == 0.5

    def test_window_limit_hours(self):
        """On-duty window limit is 14 hours per FMCSA regulations."""
        assert WINDOW_LIMIT_HOURS == 14.0

    def test_cycle_7_day_limit(self):
        """7-day cycle limit is 60 hours per FMCSA regulations."""
        assert CYCLE_7_DAY_LIMIT == 60.0

    def test_cycle_8_day_limit(self):
        """8-day cycle limit is 70 hours per FMCSA regulations."""
        assert CYCLE_8_DAY_LIMIT == 70.0

    def test_cache_ttl_seconds(self):
        """Cache TTL is 900 seconds (15 minutes) per design spec."""
        assert CACHE_TTL_SECONDS == 900


# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------


class TestBuildCacheKey:
    """Tests for the _build_cache_key helper."""

    def test_key_format(self):
        """Cache key follows hos:{tenant_id}:{driver_id} pattern."""
        key = _build_cache_key("tenant_abc", "driver_123")
        assert key == "hos:tenant_abc:driver_123"

    def test_key_with_different_ids(self):
        """Cache key correctly interpolates different tenant/driver IDs."""
        key = _build_cache_key("acme_fuel", "drv_456")
        assert key == "hos:acme_fuel:drv_456"


class TestParseGeotabHosResponse:
    """Tests for the _parse_geotab_hos_response helper."""

    def test_parses_camel_case_fields(self):
        """Parses Geotab camelCase response fields."""
        raw = {
            "availableDriveHours": 8.5,
            "availableWindowHours": 10.0,
            "cumulativeCycleHours": 45.0,
            "cycleType": "7_day",
        }
        status = _parse_geotab_hos_response("driver_001", raw)

        assert status.driver_id == "driver_001"
        assert status.available_drive_hours == 8.5
        assert status.available_window_hours == 10.0
        assert status.cumulative_cycle_hours == 45.0
        assert status.cycle_type == "7_day"
        assert status.source == "geotab"

    def test_parses_snake_case_fields(self):
        """Parses snake_case response fields."""
        raw = {
            "available_drive_hours": 6.0,
            "available_window_hours": 9.0,
            "cumulative_cycle_hours": 50.0,
            "cycle_type": "8_day",
        }
        status = _parse_geotab_hos_response("driver_002", raw)

        assert status.available_drive_hours == 6.0
        assert status.cycle_type == "8_day"

    def test_defaults_missing_fields_to_zero(self):
        """Missing numeric fields default to 0.0."""
        raw = {}
        status = _parse_geotab_hos_response("driver_003", raw)

        assert status.available_drive_hours == 0.0
        assert status.available_window_hours == 0.0
        assert status.cumulative_cycle_hours == 0.0
        assert status.cycle_type == "7_day"

    def test_normalizes_8day_cycle_type(self):
        """Normalizes various 8-day cycle type strings."""
        for variant in ("8_day", "8day", "8-day"):
            raw = {"cycleType": variant}
            status = _parse_geotab_hos_response("driver_004", raw)
            assert status.cycle_type == "8_day"


# ---------------------------------------------------------------------------
# Refresh HOS Data Tests (Task 7.2)
# ---------------------------------------------------------------------------


class TestRefreshHosData:
    """Tests for HOSChecker.refresh_hos_data — Geotab fetch + Redis cache.

    Validates: Requirement 4.6
    """

    @pytest.mark.asyncio
    async def test_calls_geotab_and_caches_in_redis(self):
        """refresh_hos_data calls Geotab and stores result in Redis with TTL."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(return_value={
            "availableDriveHours": 8.5,
            "availableWindowHours": 10.0,
            "cumulativeCycleHours": 45.0,
            "cycleType": "7_day",
        })

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="tenant_001")
        result = await checker.refresh_hos_data("driver_001")

        # Verify Geotab was called
        geotab_connector.get_hos_status.assert_awaited_once_with("driver_001")

        # Verify result
        assert result.driver_id == "driver_001"
        assert result.available_drive_hours == 8.5
        assert result.available_window_hours == 10.0
        assert result.cumulative_cycle_hours == 45.0
        assert result.cycle_type == "7_day"
        assert result.source == "geotab"

        # Verify Redis setex was called with correct key and TTL
        redis_client.setex.assert_awaited_once()
        call_args = redis_client.setex.call_args
        assert call_args[0][0] == "hos:tenant_001:driver_001"
        assert call_args[0][1] == 900  # CACHE_TTL_SECONDS
        # Verify the payload is valid JSON containing the status data
        payload = json.loads(call_args[0][2])
        assert payload["driver_id"] == "driver_001"
        assert payload["available_drive_hours"] == 8.5

    @pytest.mark.asyncio
    async def test_redis_key_format(self):
        """Redis key follows hos:{tenant_id}:{driver_id} pattern."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(return_value={
            "availableDriveHours": 5.0,
            "availableWindowHours": 7.0,
            "cumulativeCycleHours": 30.0,
            "cycleType": "7_day",
        })

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="acme_fuel")
        await checker.refresh_hos_data("drv_xyz")

        call_args = redis_client.setex.call_args
        assert call_args[0][0] == "hos:acme_fuel:drv_xyz"

    @pytest.mark.asyncio
    async def test_ttl_is_900_seconds(self):
        """TTL is set to exactly 900 seconds (15 minutes)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(return_value={
            "availableDriveHours": 11.0,
            "availableWindowHours": 14.0,
            "cumulativeCycleHours": 0.0,
            "cycleType": "7_day",
        })

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        await checker.refresh_hos_data("d1")

        call_args = redis_client.setex.call_args
        assert call_args[0][1] == 900

    @pytest.mark.asyncio
    async def test_geotab_failure_raises_runtime_error(self):
        """Geotab connector failure raises RuntimeError."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(
            side_effect=ConnectionError("Geotab API unreachable")
        )

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")

        with pytest.raises(RuntimeError, match="Failed to retrieve HOS data"):
            await checker.refresh_hos_data("driver_001")

        # Redis should NOT have been called
        redis_client.setex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_geotab_returns_non_mapping_raises_runtime_error(self):
        """Geotab returning non-mapping data raises RuntimeError."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(return_value="invalid")

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")

        with pytest.raises(RuntimeError, match="invalid response"):
            await checker.refresh_hos_data("driver_001")

    @pytest.mark.asyncio
    async def test_redis_failure_does_not_block_response(self):
        """Redis cache write failure is logged but does not raise."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(return_value={
            "availableDriveHours": 8.0,
            "availableWindowHours": 10.0,
            "cumulativeCycleHours": 40.0,
            "cycleType": "7_day",
        })
        redis_client.setex = AsyncMock(side_effect=ConnectionError("Redis down"))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.refresh_hos_data("driver_001")

        # Should still return the HOS status despite Redis failure
        assert result.driver_id == "driver_001"
        assert result.available_drive_hours == 8.0

    @pytest.mark.asyncio
    async def test_tenant_id_override(self):
        """tenant_id parameter overrides instance default."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        geotab_connector.get_hos_status = AsyncMock(return_value={
            "availableDriveHours": 5.0,
            "availableWindowHours": 7.0,
            "cumulativeCycleHours": 30.0,
            "cycleType": "7_day",
        })

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="default_tenant")
        await checker.refresh_hos_data("driver_001", tenant_id="override_tenant")

        call_args = redis_client.setex.call_args
        assert call_args[0][0] == "hos:override_tenant:driver_001"


# ---------------------------------------------------------------------------
# Get Cached HOS Data Tests
# ---------------------------------------------------------------------------


class TestGetCachedHosData:
    """Tests for HOSChecker._get_cached_hos_data — Redis cache reads."""

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self):
        """Returns None when Redis has no cached data."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        redis_client.get = AsyncMock(return_value=None)

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker._get_cached_hos_data("t1", "driver_001")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_hos_status_on_cache_hit(self):
        """Returns HOSStatus when valid cached data exists."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker._get_cached_hos_data("t1", "driver_001")

        assert result is not None
        assert result.driver_id == "driver_001"
        assert result.available_drive_hours == 8.5

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json(self):
        """Returns None when cached data is invalid JSON."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        redis_client.get = AsyncMock(return_value="not-valid-json{{{")

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker._get_cached_hos_data("t1", "driver_001")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_redis_error(self):
        """Returns None when Redis raises an exception."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        redis_client.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker._get_cached_hos_data("t1", "driver_001")

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_correct_cache_key(self):
        """Reads from the correct Redis key."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        redis_client.get = AsyncMock(return_value=None)

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        await checker._get_cached_hos_data("my_tenant", "my_driver")

        redis_client.get.assert_awaited_once_with("hos:my_tenant:my_driver")


# ---------------------------------------------------------------------------
# Get HOS Status Tests (cache-first with fallback)
# ---------------------------------------------------------------------------


class TestGetHosStatus:
    """Tests for HOSChecker.get_hos_status — cache-first with refresh fallback.

    Validates: Requirements 4.1, 4.6
    """

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_without_calling_geotab(self):
        """Cache hit returns cached data without calling Geotab."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.get_hos_status("driver_001")

        assert result.driver_id == "driver_001"
        assert result.available_drive_hours == 8.5
        # Geotab should NOT have been called
        geotab_connector.get_hos_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_geotab_and_caches(self):
        """Cache miss triggers Geotab fetch and stores result in Redis."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        redis_client.get = AsyncMock(return_value=None)
        geotab_connector.get_hos_status = AsyncMock(return_value={
            "availableDriveHours": 6.0,
            "availableWindowHours": 9.0,
            "cumulativeCycleHours": 50.0,
            "cycleType": "8_day",
        })

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.get_hos_status("driver_001")

        assert result.driver_id == "driver_001"
        assert result.available_drive_hours == 6.0
        assert result.cycle_type == "8_day"
        # Geotab should have been called
        geotab_connector.get_hos_status.assert_awaited_once_with("driver_001")
        # Redis should have been written
        redis_client.setex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_tenant_id_override(self):
        """tenant_id parameter overrides instance default for cache lookup."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 7.0,
            "available_window_hours": 11.0,
            "cumulative_cycle_hours": 35.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="default")
        await checker.get_hos_status("driver_001", tenant_id="custom_tenant")

        redis_client.get.assert_awaited_once_with("hos:custom_tenant:driver_001")


# ---------------------------------------------------------------------------
# is_eligible Tests (Task 7.3 — Drive Hours Check)
# ---------------------------------------------------------------------------


class TestIsEligibleDriveHours:
    """Tests for HOSChecker.is_eligible — 11-hour drive + 30-min buffer check.

    Validates: Requirement 4.2
    """

    @pytest.mark.asyncio
    async def test_eligible_when_sufficient_drive_hours(self):
        """Driver with 8.5h available, route needs 3h drive → eligible (8.5 > 3.0 + 0.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.driver_id == "driver_001"
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_ineligible_when_drive_hours_insufficient(self):
        """Driver with 3.0h available, route needs 3h drive → ineligible (3.0 < 3.0 + 0.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 3.0,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 1
        assert "Insufficient drive hours" in result.reasons[0]
        assert "3.0h available" in result.reasons[0]
        assert "3.5h" in result.reasons[0]  # 3.0 + 0.5 buffer

    @pytest.mark.asyncio
    async def test_ineligible_when_zero_drive_hours(self):
        """Driver with 0h available → ineligible."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 0.0,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "Insufficient drive hours" in result.reasons[0]
        assert "0.0h available" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_eligible_at_exact_boundary(self):
        """Boundary: available = estimated + buffer → eligible (not strictly less)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        # available_drive_hours = 3.5, estimated = 3.0, buffer = 0.5
        # 3.5 is NOT less than 3.5, so driver is eligible
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 3.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_hos_retrieval_failure(self):
        """HOS data retrieval failure → eligible (graceful degradation)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        # Both cache and Geotab fail
        redis_client.get = AsyncMock(return_value=None)
        geotab_connector.get_hos_status = AsyncMock(
            side_effect=ConnectionError("Geotab API unreachable")
        )

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 1
        assert "HOS data unavailable" in result.reasons[0]


# ---------------------------------------------------------------------------
# is_eligible Tests (Task 7.4 — 14-Hour Window Check)
# ---------------------------------------------------------------------------


class TestIsEligibleWindowHours:
    """Tests for HOSChecker.is_eligible — 14-hour on-duty window check.

    Validates: Requirement 4.3
    """

    @pytest.mark.asyncio
    async def test_eligible_when_sufficient_window_hours(self):
        """Driver with 10h window available, route needs 5h total → eligible."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.driver_id == "driver_001"
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_ineligible_when_window_hours_insufficient(self):
        """Driver with 4h window available, route needs 5h total → ineligible."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 4.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 1
        assert "Insufficient on-duty window" in result.reasons[0]
        assert "4.0h available" in result.reasons[0]
        assert "5.0h total route duration" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_ineligible_when_zero_window_hours(self):
        """Driver with 0h window available → ineligible."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 0.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert len(result.reasons) == 1
        assert "Insufficient on-duty window" in result.reasons[0]
        assert "0.0h available" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_eligible_at_exact_window_boundary(self):
        """Boundary: available_window = estimated_total → eligible (not strictly less)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        # available_window_hours = 5.0, estimated_total_hours = 5.0
        # 5.0 is NOT less than 5.0, so driver is eligible
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 5.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_both_drive_and_window_checks_fail(self):
        """Both drive hours and window hours insufficient → both reasons reported."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 2.0,
            "available_window_hours": 3.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 2
        assert "Insufficient drive hours" in result.reasons[0]
        assert "Insufficient on-duty window" in result.reasons[1]


# ---------------------------------------------------------------------------
# is_eligible Tests (Task 7.5 — 60/70-Hour Cycle Limit Check)
# ---------------------------------------------------------------------------


class TestIsEligibleCycleLimit:
    """Tests for HOSChecker.is_eligible — 60/70-hour cycle limit check.

    Validates: Requirement 4.4
    """

    @pytest.mark.asyncio
    async def test_7day_cycle_eligible_under_limit(self):
        """7-day cycle: 45h cumulative + 5h route = 50h → eligible (under 60h)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.driver_id == "driver_001"
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_7day_cycle_ineligible_over_limit(self):
        """7-day cycle: 57h cumulative + 5h route = 62h → ineligible (over 60h)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 57.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 1
        assert "Cycle limit exceeded" in result.reasons[0]
        assert "57.0h cumulative" in result.reasons[0]
        assert "5.0h route" in result.reasons[0]
        assert "62.0h" in result.reasons[0]
        assert "60h" in result.reasons[0]
        assert "7_day" in result.reasons[0]
        assert "2.0h" in result.reasons[0]  # excess

    @pytest.mark.asyncio
    async def test_8day_cycle_eligible_under_limit(self):
        """8-day cycle: 65h cumulative + 4h route = 69h → eligible (under 70h)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 65.0,
            "cycle_type": "8_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 4.0)

        assert result.eligible is True
        assert result.driver_id == "driver_001"
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_8day_cycle_ineligible_over_limit(self):
        """8-day cycle: 68h cumulative + 4h route = 72h → ineligible (over 70h)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 68.0,
            "cycle_type": "8_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 4.0)

        assert result.eligible is False
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 1
        assert "Cycle limit exceeded" in result.reasons[0]
        assert "68.0h cumulative" in result.reasons[0]
        assert "4.0h route" in result.reasons[0]
        assert "72.0h" in result.reasons[0]
        assert "70h" in result.reasons[0]
        assert "8_day" in result.reasons[0]
        assert "2.0h" in result.reasons[0]  # excess

    @pytest.mark.asyncio
    async def test_cycle_exactly_at_limit_is_eligible(self):
        """Boundary: cumulative + route = limit exactly → eligible (not exceeded)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        # 7-day: 55h + 5h = 60h exactly → eligible (not > 60)
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 55.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_all_three_checks_fail_simultaneously(self):
        """All three checks fail → three reasons reported."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        # Drive: 2.0h available, need 3.0 + 0.5 = 3.5h → fail
        # Window: 3.0h available, need 5.0h → fail
        # Cycle: 57.0h + 5.0h = 62.0h > 60h → fail
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 2.0,
            "available_window_hours": 3.0,
            "cumulative_cycle_hours": 57.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.driver_id == "driver_001"
        assert len(result.reasons) == 3
        assert "Insufficient drive hours" in result.reasons[0]
        assert "Insufficient on-duty window" in result.reasons[1]
        assert "Cycle limit exceeded" in result.reasons[2]


# ---------------------------------------------------------------------------
# is_eligible Tests (Task 7.6 — Earliest Eligible Time & hos_blocked)
# ---------------------------------------------------------------------------


class TestIsEligibleEarliestTime:
    """Tests for HOSChecker.is_eligible — earliest_eligible_time computation.

    Validates: Requirement 4.5
    """

    @pytest.mark.asyncio
    async def test_eligible_driver_has_no_earliest_eligible_time(self):
        """Eligible driver has earliest_eligible_time = None."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.earliest_eligible_time is None

    @pytest.mark.asyncio
    async def test_ineligible_drive_hours_sets_earliest_eligible_time(self):
        """Drive-hours failure sets earliest_eligible_time to now + 10 hours (rest reset)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 2.0,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.earliest_eligible_time is not None
        # Drive-hours failure → 10-hour rest reset
        # Verify it's approximately 10 hours from now (within a few seconds tolerance)
        from datetime import timezone, timedelta
        from services.time_utils import utcnow

        now = utcnow()
        expected_earliest = now + timedelta(hours=10)
        # Allow 5 seconds tolerance for test execution time
        assert abs((result.earliest_eligible_time - expected_earliest).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_ineligible_window_sets_earliest_eligible_time(self):
        """Window failure sets earliest_eligible_time to now + 10 hours (off-duty reset)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 3.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.earliest_eligible_time is not None
        from datetime import timezone, timedelta
        from services.time_utils import utcnow

        now = utcnow()
        expected_earliest = now + timedelta(hours=10)
        assert abs((result.earliest_eligible_time - expected_earliest).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_ineligible_cycle_sets_earliest_eligible_time_34h(self):
        """Cycle failure sets earliest_eligible_time to now + 34 hours (34-hour restart)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 57.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert result.earliest_eligible_time is not None
        from datetime import timezone, timedelta
        from services.time_utils import utcnow

        now = utcnow()
        expected_earliest = now + timedelta(hours=34)
        assert abs((result.earliest_eligible_time - expected_earliest).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_multiple_failures_uses_latest_earliest_time(self):
        """Multiple failures use the LATEST (most restrictive) earliest time.

        Drive failure → +10h, Window failure → +10h, Cycle failure → +34h
        Most restrictive = +34h (cycle restart).
        """
        es_service, redis_client, geotab_connector = _make_dependencies()
        # All three checks fail
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 2.0,
            "available_window_hours": 3.0,
            "cumulative_cycle_hours": 57.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert len(result.reasons) == 3
        assert result.earliest_eligible_time is not None
        # Most restrictive is cycle failure at +34h
        from datetime import timezone, timedelta
        from services.time_utils import utcnow

        now = utcnow()
        expected_earliest = now + timedelta(hours=34)
        assert abs((result.earliest_eligible_time - expected_earliest).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_drive_and_window_failures_uses_10h(self):
        """Drive + window failures both compute +10h → earliest is +10h."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 2.0,
            "available_window_hours": 3.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        assert len(result.reasons) == 2
        assert result.earliest_eligible_time is not None
        from datetime import timezone, timedelta
        from services.time_utils import utcnow

        now = utcnow()
        expected_earliest = now + timedelta(hours=10)
        assert abs((result.earliest_eligible_time - expected_earliest).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_graceful_degradation_no_earliest_time(self):
        """Graceful degradation (HOS data unavailable) → eligible, no earliest time."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        redis_client.get = AsyncMock(return_value=None)
        geotab_connector.get_hos_status = AsyncMock(
            side_effect=ConnectionError("Geotab API unreachable")
        )

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        assert result.earliest_eligible_time is None


# ---------------------------------------------------------------------------
# log_route_assignment Tests (Task 7.7 — Audit Trail Logging)
# ---------------------------------------------------------------------------


class TestLogRouteAssignment:
    """Tests for HOSChecker.log_route_assignment — projected post-route duty hours.

    Validates: Requirement 4.7
    """

    @pytest.mark.asyncio
    async def test_eligible_driver_triggers_log(self):
        """Eligible driver triggers log_route_assignment via is_eligible."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is True
        # ES index should have been called for audit logging
        es_service.index.assert_awaited_once()
        call_kwargs = es_service.index.call_args[1]
        assert call_kwargs["index"] == "hos_assignment_log"
        body = call_kwargs["body"]
        assert body["driver_id"] == "driver_001"
        assert body["estimated_drive_hours"] == 3.0
        assert body["estimated_total_hours"] == 5.0

    @pytest.mark.asyncio
    async def test_ineligible_driver_does_not_trigger_log(self):
        """Ineligible driver does NOT trigger log_route_assignment."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 2.0,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        assert result.eligible is False
        # ES index should NOT have been called
        es_service.index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_projected_values_computed_correctly(self):
        """Projected post-route values are computed correctly."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        await checker.is_eligible("driver_001", 3.0, 5.0)

        call_kwargs = es_service.index.call_args[1]
        body = call_kwargs["body"]

        # projected_drive_hours_remaining = 8.5 - 3.0 = 5.5
        assert body["projected_drive_hours_remaining"] == 5.5
        # projected_window_hours_remaining = 10.0 - 5.0 = 5.0
        assert body["projected_window_hours_remaining"] == 5.0
        # projected_cumulative_cycle_hours = 45.0 + 5.0 = 50.0
        assert body["projected_cumulative_cycle_hours"] == 50.0

    @pytest.mark.asyncio
    async def test_es_persistence_includes_all_fields(self):
        """ES document includes all required audit fields."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="tenant_abc")
        await checker.is_eligible("driver_001", 4.0, 6.0)

        call_kwargs = es_service.index.call_args[1]
        body = call_kwargs["body"]

        # Verify all expected fields are present
        assert body["driver_id"] == "driver_001"
        assert body["tenant_id"] == "tenant_abc"
        assert body["estimated_drive_hours"] == 4.0
        assert body["estimated_total_hours"] == 6.0
        assert body["pre_assignment_drive_hours"] == 8.5
        assert body["pre_assignment_window_hours"] == 10.0
        assert body["pre_assignment_cumulative_cycle_hours"] == 45.0
        assert body["projected_drive_hours_remaining"] == 4.5  # 8.5 - 4.0
        assert body["projected_window_hours_remaining"] == 4.0  # 10.0 - 6.0
        assert body["projected_cumulative_cycle_hours"] == 51.0  # 45.0 + 6.0
        assert body["cycle_type"] == "7_day"
        assert "timestamp" in body

    @pytest.mark.asyncio
    async def test_es_failure_does_not_block_eligibility(self):
        """ES persistence failure is logged but does not block the eligibility result."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock(side_effect=ConnectionError("ES down"))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 5.0)

        # Should still return eligible despite ES failure
        assert result.eligible is True
        assert result.driver_id == "driver_001"

    @pytest.mark.asyncio
    async def test_logger_info_called_on_assignment(self, caplog):
        """logger.info is called with projected values on successful assignment."""
        import logging

        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 8.5,
            "available_window_hours": 10.0,
            "cumulative_cycle_hours": 45.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")

        with caplog.at_level(logging.INFO, logger="compliance.services.hos_checker"):
            await checker.is_eligible("driver_001", 3.0, 5.0)

        # Verify the log message contains projected values
        log_messages = [r.message for r in caplog.records if "log_route_assignment" in r.message]
        assert len(log_messages) >= 1
        log_msg = log_messages[0]
        assert "driver_001" in log_msg
        assert "projected_drive_hours_remaining=5.5h" in log_msg
        assert "projected_window_hours_remaining=5.0h" in log_msg
        assert "projected_cumulative_cycle_hours=50.0h" in log_msg


# ---------------------------------------------------------------------------
# HOS Rule Boundary Tests (Task 7.9)
# ---------------------------------------------------------------------------


class TestHOSBoundaryTests:
    """Comprehensive boundary tests for each HOS rule at exact thresholds.

    Tests the precise boundary behavior of:
    - Drive-hours check (with 0.5h buffer): available < estimated + 0.5
    - Window-hours check: available < estimated_total
    - Cycle-hours check (7-day): cumulative + estimated_total > 60

    Validates: Requirements 4.2, 4.3, 4.4
    """

    # -------------------------------------------------------------------
    # Drive-hours boundary tests (with 0.5h buffer)
    # Rule: ineligible when available_drive_hours < estimated_drive + 0.5
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_drive_boundary_exactly_at_threshold_eligible(self):
        """Drive: route=3h, available=3.5h → eligible (3.5 >= 3.0 + 0.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 3.5,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 3.0)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_drive_boundary_just_below_threshold_ineligible(self):
        """Drive: route=3h, available=3.4h → ineligible (3.4 < 3.0 + 0.5 = 3.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 3.4,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 3.0, 3.0)

        assert result.eligible is False
        assert len(result.reasons) >= 1
        assert "Insufficient drive hours" in result.reasons[0]

    @pytest.mark.asyncio
    async def test_drive_boundary_10_5h_route_with_max_available(self):
        """Drive: route=10.5h, available=11.0h → eligible (11.0 >= 10.5 + 0.5 = 11.0)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 10.5, 10.5)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_drive_boundary_11h_route_with_max_available_ineligible(self):
        """Drive: route=11h, available=11.0h → ineligible (11.0 < 11.0 + 0.5 = 11.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 11.0, 11.0)

        assert result.eligible is False
        assert any("Insufficient drive hours" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_drive_boundary_11_5h_route_impossible(self):
        """Drive: route=11.5h, available=11.0h → ineligible (11.0 < 11.5 + 0.5 = 12.0, exceeds max)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        result = await checker.is_eligible("driver_001", 11.5, 11.5)

        assert result.eligible is False
        assert any("Insufficient drive hours" in r for r in result.reasons)

    # -------------------------------------------------------------------
    # Window-hours boundary tests
    # Rule: ineligible when available_window_hours < estimated_total_hours
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_window_boundary_13_5h_route_available_13_5h_eligible(self):
        """Window: route=13.5h total, available=13.5h → eligible (13.5 >= 13.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 13.5,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive hours: need 10.0 + 0.5 = 10.5, have 11.0 → OK
        result = await checker.is_eligible("driver_001", 10.0, 13.5)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_window_boundary_14h_route_available_14h_eligible(self):
        """Window: route=14h total, available=14.0h → eligible (14.0 >= 14.0)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive hours: need 10.0 + 0.5 = 10.5, have 11.0 → OK
        result = await checker.is_eligible("driver_001", 10.0, 14.0)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_window_boundary_14_5h_route_available_14h_ineligible(self):
        """Window: route=14.5h total, available=14.0h → ineligible (14.0 < 14.5)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive hours: need 10.0 + 0.5 = 10.5, have 11.0 → OK (only window fails)
        result = await checker.is_eligible("driver_001", 10.0, 14.5)

        assert result.eligible is False
        assert any("Insufficient on-duty window" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_window_boundary_14h_route_available_13_9h_ineligible(self):
        """Window: route=14h total, available=13.9h → ineligible (13.9 < 14.0)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 13.9,
            "cumulative_cycle_hours": 0.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive hours: need 10.0 + 0.5 = 10.5, have 11.0 → OK (only window fails)
        result = await checker.is_eligible("driver_001", 10.0, 14.0)

        assert result.eligible is False
        assert any("Insufficient on-duty window" in r for r in result.reasons)

    # -------------------------------------------------------------------
    # Cycle-hours boundary tests (7-day, limit=60h)
    # Rule: ineligible when cumulative + estimated_total > cycle_limit
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cycle_boundary_59h_plus_1h_equals_60h_eligible(self):
        """Cycle: cumulative=59h + route=1h = 60h → eligible (60 <= 60, not >)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 59.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive: need 0.5 + 0.5 = 1.0, have 11.0 → OK
        # Window: need 1.0, have 14.0 → OK
        result = await checker.is_eligible("driver_001", 0.5, 1.0)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_cycle_boundary_60h_plus_1h_equals_61h_ineligible(self):
        """Cycle: cumulative=60h + route=1h = 61h → ineligible (61 > 60)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 60.0,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive: need 0.5 + 0.5 = 1.0, have 11.0 → OK
        # Window: need 1.0, have 14.0 → OK
        result = await checker.is_eligible("driver_001", 0.5, 1.0)

        assert result.eligible is False
        assert any("Cycle limit exceeded" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_cycle_boundary_59_5h_plus_0_5h_equals_60h_eligible(self):
        """Cycle: cumulative=59.5h + route=0.5h = 60h → eligible (60 <= 60)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 59.5,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))
        es_service.index = AsyncMock()

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive: need 0.25 + 0.5 = 0.75, have 11.0 → OK
        # Window: need 0.5, have 14.0 → OK
        result = await checker.is_eligible("driver_001", 0.25, 0.5)

        assert result.eligible is True
        assert result.reasons == []

    @pytest.mark.asyncio
    async def test_cycle_boundary_59_5h_plus_0_6h_equals_60_1h_ineligible(self):
        """Cycle: cumulative=59.5h + route=0.6h = 60.1h → ineligible (60.1 > 60)."""
        es_service, redis_client, geotab_connector = _make_dependencies()
        cached_data = {
            "driver_id": "driver_001",
            "available_drive_hours": 11.0,
            "available_window_hours": 14.0,
            "cumulative_cycle_hours": 59.5,
            "cycle_type": "7_day",
            "last_updated": "2026-06-01T12:00:00+00:00",
            "source": "geotab",
        }
        redis_client.get = AsyncMock(return_value=json.dumps(cached_data))

        checker = HOSChecker(es_service, redis_client, geotab_connector, tenant_id="t1")
        # Drive: need 0.3 + 0.5 = 0.8, have 11.0 → OK
        # Window: need 0.6, have 14.0 → OK
        result = await checker.is_eligible("driver_001", 0.3, 0.6)

        assert result.eligible is False
        assert any("Cycle limit exceeded" in r for r in result.reasons)
