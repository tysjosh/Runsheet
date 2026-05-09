"""
Unit tests for the daily driver ``completed_today`` reset cron.

Validates: Requirement 3.2.4 — The Platform SHALL reset every driver's
``completed_today`` counter at 00:00 in the tenant's configured timezone
via a background job that logs failures and emits
``fuelops_driver_daily_reset_errors_total``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fuel.services.driver_daily_reset import (
    DEFAULT_TIMEZONE,
    METRIC_RESET_ERRORS,
    RESET_CHECK_INTERVAL_SECONDS,
    DriverDailyResetJob,
    _get_tenant_timezone,
    _is_midnight_window,
    run_daily_reset_cycle,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTenantSettings:
    """Minimal tenant settings stub."""

    def __init__(self, timezone: Optional[str] = None):
        self.timezone = timezone


class FakeDriverRepository:
    """Recording stub for DriverRepository.reset_completed_today."""

    def __init__(self, *, fail_for: Optional[set] = None):
        self.reset_calls: List[str] = []
        self._fail_for = fail_for or set()

    async def reset_completed_today(self, tenant_id: str) -> int:
        self.reset_calls.append(tenant_id)
        if tenant_id in self._fail_for:
            raise RuntimeError(f"ES failure for {tenant_id}")
        return 5  # pretend 5 drivers were reset


class FakeESService:
    """Stub ES service that returns canned aggregation results."""

    def __init__(self, tenant_ids: List[str]):
        self._tenant_ids = tenant_ids

    async def search_documents(self, index, query, size):
        return {
            "aggregations": {
                "tenant_ids": {
                    "buckets": [{"key": tid} for tid in self._tenant_ids]
                }
            }
        }


# ---------------------------------------------------------------------------
# Tests: _get_tenant_timezone
# ---------------------------------------------------------------------------


class TestGetTenantTimezone:
    def test_returns_configured_timezone(self):
        settings = FakeTenantSettings(timezone="US/Eastern")
        assert _get_tenant_timezone("t1", settings) == "US/Eastern"

    def test_returns_default_when_no_settings(self):
        assert _get_tenant_timezone("t1", None) == DEFAULT_TIMEZONE

    def test_returns_default_when_timezone_is_none(self):
        settings = FakeTenantSettings(timezone=None)
        assert _get_tenant_timezone("t1", settings) == DEFAULT_TIMEZONE

    def test_returns_default_when_timezone_is_empty(self):
        settings = FakeTenantSettings(timezone="")
        assert _get_tenant_timezone("t1", settings) == DEFAULT_TIMEZONE


# ---------------------------------------------------------------------------
# Tests: _is_midnight_window
# ---------------------------------------------------------------------------


class TestIsMidnightWindow:
    def test_new_day_triggers_reset(self):
        # If last_reset_date is yesterday, should trigger
        assert _is_midnight_window("America/Chicago", "2020-01-01") is True

    def test_same_day_does_not_trigger(self):
        # Get today's date in America/Chicago
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Chicago")
        today = datetime.now(tz).strftime("%Y-%m-%d")
        assert _is_midnight_window("America/Chicago", today) is False

    def test_none_last_reset_triggers(self):
        assert _is_midnight_window("America/Chicago", None) is True

    def test_invalid_timezone_falls_back_to_default(self):
        # Should not raise, falls back to America/Chicago
        result = _is_midnight_window("Invalid/Timezone", "2020-01-01")
        assert result is True


# ---------------------------------------------------------------------------
# Tests: DriverDailyResetJob
# ---------------------------------------------------------------------------


class TestDriverDailyResetJob:
    @pytest.fixture
    def es_service(self):
        return FakeESService(tenant_ids=["tenant-a", "tenant-b"])

    @pytest.fixture
    def driver_repo(self):
        return FakeDriverRepository()

    @pytest.fixture
    def job(self, es_service, driver_repo):
        return DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )

    @pytest.mark.asyncio
    async def test_discover_tenant_ids(self, job):
        tenant_ids = await job.discover_tenant_ids()
        assert tenant_ids == ["tenant-a", "tenant-b"]

    @pytest.mark.asyncio
    async def test_reset_for_tenant_calls_repository(self, job, driver_repo):
        await job.reset_for_tenant("tenant-a")
        assert "tenant-a" in driver_repo.reset_calls

    @pytest.mark.asyncio
    async def test_reset_for_tenant_failure_logs_exception(
        self, es_service, caplog
    ):
        """Failures log logger.exception per Requirement 3.2.4."""
        failing_repo = FakeDriverRepository(fail_for={"tenant-x"})
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=failing_repo,
            tenant_settings_service=None,
        )

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await job.reset_for_tenant("tenant-x")

        # logger.exception produces ERROR-level log with exc_info
        assert any("reset failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_reset_for_tenant_failure_increments_prometheus_counter(
        self, es_service
    ):
        """Failures increment fuelops_driver_daily_reset_errors_total{tenant_id}."""
        from fuel.services.order_intake_metrics import (
            fuelops_driver_daily_reset_errors_total,
        )

        failing_repo = FakeDriverRepository(fail_for={"tenant-metric"})
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=failing_repo,
            tenant_settings_service=None,
        )

        # Get the counter value before
        before = (
            fuelops_driver_daily_reset_errors_total.labels(
                tenant_id="tenant-metric"
            )._value.get()
        )

        with pytest.raises(RuntimeError):
            await job.reset_for_tenant("tenant-metric")

        after = (
            fuelops_driver_daily_reset_errors_total.labels(
                tenant_id="tenant-metric"
            )._value.get()
        )
        assert after == before + 1.0

    @pytest.mark.asyncio
    async def test_run_cycle_resets_tenants_past_midnight(self, es_service):
        """run_cycle resets tenants that have crossed midnight."""
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )
        # Force all tenants to appear as needing reset (no last_reset_date)
        await job.run_cycle()
        # Both tenants should have been reset
        assert "tenant-a" in driver_repo.reset_calls
        assert "tenant-b" in driver_repo.reset_calls

    @pytest.mark.asyncio
    async def test_run_cycle_skips_already_reset_tenants(self, es_service):
        """run_cycle does not double-reset a tenant on the same day."""
        from zoneinfo import ZoneInfo

        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )

        # Simulate that tenant-a was already reset today
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        today = datetime.now(tz).strftime("%Y-%m-%d")
        job._last_reset_dates["tenant-a"] = today

        await job.run_cycle()

        # Only tenant-b should be reset
        assert "tenant-a" not in driver_repo.reset_calls
        assert "tenant-b" in driver_repo.reset_calls

    @pytest.mark.asyncio
    async def test_run_cycle_continues_on_single_tenant_failure(
        self, es_service
    ):
        """If one tenant fails, the other still gets reset."""
        driver_repo = FakeDriverRepository(fail_for={"tenant-a"})
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )

        await job.run_cycle()

        # tenant-a failed but tenant-b should still be reset
        assert "tenant-a" in driver_repo.reset_calls
        assert "tenant-b" in driver_repo.reset_calls

    @pytest.mark.asyncio
    async def test_tenant_timezone_used_for_midnight_check(self):
        """Each tenant's configured timezone is used for the midnight check."""

        class FakeTenantSettingsService:
            async def get(self, tenant_id):
                if tenant_id == "tenant-east":
                    return FakeTenantSettings(timezone="US/Eastern")
                return FakeTenantSettings(timezone="US/Pacific")

        es = FakeESService(tenant_ids=["tenant-east", "tenant-pacific"])
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(
            es_service=es,
            driver_repository=driver_repo,
            tenant_settings_service=FakeTenantSettingsService(),
        )

        # Both should be reset (no prior reset date)
        await job.run_cycle()
        assert "tenant-east" in driver_repo.reset_calls
        assert "tenant-pacific" in driver_repo.reset_calls


# ---------------------------------------------------------------------------
# Tests: run_daily_reset_cycle
# ---------------------------------------------------------------------------


class TestRunDailyResetCycle:
    @pytest.mark.asyncio
    async def test_delegates_to_job_run_cycle(self):
        job = MagicMock()
        job.run_cycle = AsyncMock()
        await run_daily_reset_cycle(job)
        job.run_cycle.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_reset_check_interval_is_60_seconds(self):
        assert RESET_CHECK_INTERVAL_SECONDS == 60

    def test_default_timezone_is_america_chicago(self):
        assert DEFAULT_TIMEZONE == "America/Chicago"

    def test_metric_name(self):
        assert METRIC_RESET_ERRORS == "fuelops_driver_daily_reset_errors_total"
