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

from datetime import timedelta, timezone

from fuel.services.driver_daily_reset import (
    DEFAULT_TIMEZONE,
    LEDGER_KEY_PREFIX,
    METRIC_RESET_ERRORS,
    RESET_CHECK_INTERVAL_SECONDS,
    DriverDailyResetJob,
    _get_tenant_timezone,
    _needs_reset,
    run_daily_reset_cycle,
)
from persistence.periodic_runs import InMemoryRunLedger, set_run_ledger


@pytest.fixture(autouse=True)
def ledger():
    """A shared ledger standing in for ``periodic_job_runs``."""
    led = InMemoryRunLedger()
    set_run_ledger(led)
    yield led
    set_run_ledger(None)


#: 2026-10-08 12:00 America/Chicago (CDT, UTC-5).
NOON_CHICAGO = datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)
YESTERDAY_CHICAGO = NOON_CHICAGO - timedelta(days=1)


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


class TestNeedsReset:
    def test_new_local_day_triggers_reset(self):
        assert _needs_reset("America/Chicago", YESTERDAY_CHICAGO, NOON_CHICAGO) is True

    def test_same_local_day_does_not_trigger(self):
        earlier = NOON_CHICAGO - timedelta(hours=6)
        assert _needs_reset("America/Chicago", earlier, NOON_CHICAGO) is False

    def test_utc_midnight_is_not_chicago_midnight(self):
        """At 03:00Z the UTC date has changed but the Chicago date has not."""
        last = datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc)  # 18:00 CDT
        now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)  # 22:00 CDT
        assert _needs_reset("America/Chicago", last, now) is False
        assert _needs_reset("UTC", last, now) is True

    def test_invalid_timezone_falls_back_to_default(self):
        assert _needs_reset("Invalid/Timezone", YESTERDAY_CHICAGO, NOON_CHICAGO) is True


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
    async def test_first_ever_cycle_seeds_and_does_not_reset(
        self, es_service, ledger
    ):
        """A boot is not midnight (F6): staging wiped counters on every deploy."""
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )
        await job.run_cycle(now=NOON_CHICAGO)
        assert driver_repo.reset_calls == []
        assert ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] == NOON_CHICAGO
        assert ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-b"] == NOON_CHICAGO

    @pytest.mark.asyncio
    async def test_record_from_yesterday_resets_once(self, es_service, ledger):
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] = YESTERDAY_CHICAGO
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-b"] = YESTERDAY_CHICAGO
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )
        await job.run_cycle(now=NOON_CHICAGO)
        assert sorted(driver_repo.reset_calls) == ["tenant-a", "tenant-b"]
        assert ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] == NOON_CHICAGO

        await job.run_cycle(now=NOON_CHICAGO + timedelta(minutes=1))
        assert sorted(driver_repo.reset_calls) == ["tenant-a", "tenant-b"]

    @pytest.mark.asyncio
    async def test_restart_with_todays_record_does_not_reset(
        self, es_service, ledger
    ):
        """A new job instance (deploy) sharing the ledger skips today's reset."""
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] = YESTERDAY_CHICAGO
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-b"] = YESTERDAY_CHICAGO
        first = FakeDriverRepository()
        await DriverDailyResetJob(
            es_service=es_service, driver_repository=first
        ).run_cycle(now=NOON_CHICAGO)
        assert len(first.reset_calls) == 2

        after_restart = FakeDriverRepository()
        await DriverDailyResetJob(
            es_service=es_service, driver_repository=after_restart
        ).run_cycle(now=NOON_CHICAGO + timedelta(hours=2))
        assert after_restart.reset_calls == []

    @pytest.mark.asyncio
    async def test_reset_waits_for_tenant_local_midnight(self, es_service, ledger):
        """03:00Z on 10-09 is still 10-08 in Chicago: no reset until 05:00Z."""
        seeded = datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc)
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] = seeded
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-b"] = seeded
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(es_service=es_service, driver_repository=driver_repo)

        await job.run_cycle(now=datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))
        assert driver_repo.reset_calls == []
        await job.run_cycle(now=datetime(2026, 10, 9, 5, 1, tzinfo=timezone.utc))
        assert sorted(driver_repo.reset_calls) == ["tenant-a", "tenant-b"]

    @pytest.mark.asyncio
    async def test_no_ledger_falls_back_to_memory_with_seed_rule(self, es_service):
        set_run_ledger(None)
        with patch("persistence.database.is_persistence_enabled", return_value=False):
            driver_repo = FakeDriverRepository()
            job = DriverDailyResetJob(
                es_service=es_service, driver_repository=driver_repo
            )
            await job.run_cycle(now=NOON_CHICAGO)
            assert driver_repo.reset_calls == []
            await job.run_cycle(now=NOON_CHICAGO + timedelta(days=1))
            assert sorted(driver_repo.reset_calls) == ["tenant-a", "tenant-b"]
        set_run_ledger(None)

    @pytest.mark.asyncio
    async def test_ledger_read_failure_skips_instead_of_seeding(self, es_service):
        class BrokenLedger(InMemoryRunLedger):
            async def last_run(self, job):
                raise RuntimeError("db down")

        broken = BrokenLedger()
        set_run_ledger(broken)
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(es_service=es_service, driver_repository=driver_repo)
        await job.run_cycle(now=NOON_CHICAGO)
        assert driver_repo.reset_calls == []
        assert broken.runs == {}, "seeded over a record it could not read"

    @pytest.mark.asyncio
    async def test_run_cycle_continues_on_single_tenant_failure(
        self, es_service, ledger
    ):
        """If one tenant fails, the other still gets reset; the failed one
        is retried on the next cycle."""
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] = YESTERDAY_CHICAGO
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-b"] = YESTERDAY_CHICAGO
        driver_repo = FakeDriverRepository(fail_for={"tenant-a"})
        job = DriverDailyResetJob(
            es_service=es_service,
            driver_repository=driver_repo,
            tenant_settings_service=None,
        )

        await job.run_cycle(now=NOON_CHICAGO)

        assert "tenant-a" in driver_repo.reset_calls
        assert "tenant-b" in driver_repo.reset_calls
        assert ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-a"] == YESTERDAY_CHICAGO

    @pytest.mark.asyncio
    async def test_tenant_timezone_used_for_midnight_check(self, ledger):
        """Each tenant's configured timezone is used for the midnight check.

        At 05:30Z on 10-09 it is 01:30 in New York (new day) but 22:30 on
        10-08 in Los Angeles (same day as the 10-08 18:00Z record)."""

        class FakeTenantSettingsService:
            async def get(self, tenant_id):
                if tenant_id == "tenant-east":
                    return FakeTenantSettings(timezone="US/Eastern")
                return FakeTenantSettings(timezone="US/Pacific")

        last = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-east"] = last
        ledger.runs[f"{LEDGER_KEY_PREFIX}tenant-pacific"] = last
        es = FakeESService(tenant_ids=["tenant-east", "tenant-pacific"])
        driver_repo = FakeDriverRepository()
        job = DriverDailyResetJob(
            es_service=es,
            driver_repository=driver_repo,
            tenant_settings_service=FakeTenantSettingsService(),
        )

        await job.run_cycle(now=datetime(2026, 10, 9, 5, 30, tzinfo=timezone.utc))
        assert driver_repo.reset_calls == ["tenant-east"]


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
