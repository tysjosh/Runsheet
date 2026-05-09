"""
Unit tests for fuel.services.driver_counter_service.DriverCounterService.

Validates: Requirements 3.2.1, 3.2.2.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from fuel.services.driver_counter_service import (
    DRIVER_COUNTER_SCRIPT,
    DriverCounterService,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def _build_service(*, increment_result: bool = True) -> tuple:
    """Build a DriverCounterService with a mocked driver_repo."""
    driver_repo = AsyncMock()
    driver_repo.increment_counters = AsyncMock(return_value=increment_result)

    service = DriverCounterService(driver_repo=driver_repo)
    return service, driver_repo


# ---------------------------------------------------------------------------
# Tests: Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    """Tests for DriverCounterService construction."""

    def test_raises_on_none_driver_repo(self):
        """Constructor raises ValueError when driver_repo is None."""
        with pytest.raises(ValueError, match="driver_repo must not be None"):
            DriverCounterService(driver_repo=None)

    def test_accepts_valid_driver_repo(self):
        """Constructor accepts a valid driver_repo."""
        repo = AsyncMock()
        service = DriverCounterService(driver_repo=repo)
        assert service._driver_repo is repo


# ---------------------------------------------------------------------------
# Tests: increment_counters
# ---------------------------------------------------------------------------


class TestIncrementCounters:
    """Tests for DriverCounterService.increment_counters."""

    @pytest.mark.asyncio
    async def test_delegates_to_driver_repo(self):
        """increment_counters delegates to DriverRepository.increment_counters."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=1,
        )

        assert result is True
        repo.increment_counters.assert_called_once_with(
            tenant_id="tenant_1",
            driver_id="drv_1",
            delta_active=-1,
            delta_completed=1,
        )

    @pytest.mark.asyncio
    async def test_returns_false_on_empty_driver_id(self):
        """Returns False when driver_id is empty."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=0,
        )

        assert result is False
        repo.increment_counters.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_on_whitespace_driver_id(self):
        """Returns False when driver_id is whitespace only."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="   ",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=0,
        )

        assert result is False
        repo.increment_counters.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_on_empty_tenant_id(self):
        """Returns False when tenant_id is empty."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="",
            delta_active=-1,
            delta_completed=0,
        )

        assert result is False
        repo.increment_counters.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_on_zero_deltas(self):
        """Returns False when both deltas are zero (no-op)."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=0,
            delta_completed=0,
        )

        assert result is False
        repo.increment_counters.assert_not_called()

    @pytest.mark.asyncio
    async def test_positive_delta_active_only(self):
        """Handles positive delta_active with zero delta_completed."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=1,
            delta_completed=0,
        )

        assert result is True
        repo.increment_counters.assert_called_once_with(
            tenant_id="tenant_1",
            driver_id="drv_1",
            delta_active=1,
            delta_completed=0,
        )

    @pytest.mark.asyncio
    async def test_negative_delta_active_only(self):
        """Handles negative delta_active (decrement)."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=0,
        )

        assert result is True
        repo.increment_counters.assert_called_once_with(
            tenant_id="tenant_1",
            driver_id="drv_1",
            delta_active=-1,
            delta_completed=0,
        )

    @pytest.mark.asyncio
    async def test_delta_completed_only(self):
        """Handles positive delta_completed with zero delta_active."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=0,
            delta_completed=1,
        )

        assert result is True
        repo.increment_counters.assert_called_once_with(
            tenant_id="tenant_1",
            driver_id="drv_1",
            delta_active=0,
            delta_completed=1,
        )

    @pytest.mark.asyncio
    async def test_both_deltas_nonzero(self):
        """Handles both deltas being nonzero (delivered transition)."""
        service, repo = _build_service()

        result = await service.increment_counters(
            driver_id="drv_1",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=1,
        )

        assert result is True
        repo.increment_counters.assert_called_once_with(
            tenant_id="tenant_1",
            driver_id="drv_1",
            delta_active=-1,
            delta_completed=1,
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_repo_returns_false(self):
        """Returns False when the repo indicates noop (driver not found)."""
        service, repo = _build_service(increment_result=False)

        result = await service.increment_counters(
            driver_id="drv_nonexistent",
            tenant_id="tenant_1",
            delta_active=-1,
            delta_completed=0,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_propagates_repo_exception(self):
        """Propagates exceptions from the driver repository."""
        service, repo = _build_service()
        repo.increment_counters = AsyncMock(
            side_effect=RuntimeError("ES connection failed")
        )

        with pytest.raises(RuntimeError, match="ES connection failed"):
            await service.increment_counters(
                driver_id="drv_1",
                tenant_id="tenant_1",
                delta_active=-1,
                delta_completed=0,
            )


# ---------------------------------------------------------------------------
# Tests: Painless script constant
# ---------------------------------------------------------------------------


class TestPainlessScript:
    """Tests for the DRIVER_COUNTER_SCRIPT constant."""

    def test_script_contains_delta_active_logic(self):
        """Script handles delta_active increment with floor at 0."""
        assert "params.delta_active" in DRIVER_COUNTER_SCRIPT
        assert "active_order_count" in DRIVER_COUNTER_SCRIPT
        assert "< 0" in DRIVER_COUNTER_SCRIPT

    def test_script_contains_delta_completed_logic(self):
        """Script handles delta_completed increment."""
        assert "params.delta_completed" in DRIVER_COUNTER_SCRIPT
        assert "completed_today" in DRIVER_COUNTER_SCRIPT

    def test_script_stamps_timestamps(self):
        """Script updates last_event_timestamp and updated_at."""
        assert "last_event_timestamp" in DRIVER_COUNTER_SCRIPT
        assert "updated_at" in DRIVER_COUNTER_SCRIPT
        assert "params.now" in DRIVER_COUNTER_SCRIPT
