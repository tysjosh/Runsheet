"""
Unit tests for overlay feature-flag methods on FeatureFlagService.

Tests cover:
- get_overlay_state: default disabled, reads from Redis, fail-closed on error
- set_overlay_state: validates state, sets Redis key, logs transition, returns previous
- RuntimeError when Redis client not connected

Validates: Requirements 12.1, 12.4, 12.5, 12.7
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ops.services.feature_flags import (
    FeatureFlagService,
    OVERLAY_PREFIX,
    VALID_OVERLAY_STATES,
)


@pytest.fixture()
def service():
    """Create a FeatureFlagService with a mock Redis client."""
    svc = FeatureFlagService(redis_url="redis://localhost:6379")
    svc.client = AsyncMock()
    return svc


@pytest.fixture()
def disconnected_service():
    """Create a FeatureFlagService without a Redis client."""
    svc = FeatureFlagService(redis_url="redis://localhost:6379")
    svc.client = None
    return svc


class TestOverlayConstants:
    def test_overlay_prefix(self):
        assert OVERLAY_PREFIX == "overlay_ff:"

    def test_valid_overlay_states(self):
        assert VALID_OVERLAY_STATES == frozenset(
            {"disabled", "shadow", "active_gated", "active_auto"}
        )

    def test_valid_overlay_states_is_frozenset(self):
        assert isinstance(VALID_OVERLAY_STATES, frozenset)


class TestGetOverlayState:
    @pytest.mark.asyncio
    async def test_returns_disabled_when_key_not_set(self, service):
        service.client.get = AsyncMock(return_value=None)
        result = await service.get_overlay_state("dispatch_optimizer", "tenant-1")
        assert result == "disabled"
        service.client.get.assert_awaited_once_with(
            "overlay_ff:dispatch_optimizer:tenant-1"
        )

    @pytest.mark.asyncio
    async def test_returns_stored_state(self, service):
        service.client.get = AsyncMock(return_value="shadow")
        result = await service.get_overlay_state("dispatch_optimizer", "tenant-1")
        assert result == "shadow"

    @pytest.mark.asyncio
    async def test_returns_active_gated(self, service):
        service.client.get = AsyncMock(return_value="active_gated")
        result = await service.get_overlay_state("exception_commander", "tenant-2")
        assert result == "active_gated"

    @pytest.mark.asyncio
    async def test_returns_active_auto(self, service):
        service.client.get = AsyncMock(return_value="active_auto")
        result = await service.get_overlay_state("revenue_guard", "tenant-3")
        assert result == "active_auto"

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_not_connected(self, disconnected_service):
        with pytest.raises(RuntimeError, match="Redis client not connected"):
            await disconnected_service.get_overlay_state("dispatch_optimizer", "t1")

    @pytest.mark.asyncio
    async def test_returns_disabled_on_redis_error(self, service, caplog):
        service.client.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        with caplog.at_level(logging.WARNING):
            result = await service.get_overlay_state("dispatch_optimizer", "tenant-1")
        assert result == "disabled"
        assert "Failed to read overlay state" in caplog.text

    @pytest.mark.asyncio
    async def test_key_pattern(self, service):
        service.client.get = AsyncMock(return_value=None)
        await service.get_overlay_state("customer_promise", "tenant-xyz")
        service.client.get.assert_awaited_once_with(
            "overlay_ff:customer_promise:tenant-xyz"
        )


class TestSetOverlayState:
    @pytest.mark.asyncio
    async def test_sets_state_and_returns_previous(self, service):
        service.client.get = AsyncMock(return_value="disabled")
        result = await service.set_overlay_state(
            "dispatch_optimizer", "tenant-1", "shadow", "user-admin"
        )
        assert result == "disabled"
        service.client.set.assert_awaited_once_with(
            "overlay_ff:dispatch_optimizer:tenant-1", "shadow"
        )

    @pytest.mark.asyncio
    async def test_returns_previous_state_when_transitioning(self, service):
        service.client.get = AsyncMock(return_value="shadow")
        result = await service.set_overlay_state(
            "dispatch_optimizer", "tenant-1", "active_gated", "user-admin"
        )
        assert result == "shadow"

    @pytest.mark.asyncio
    async def test_raises_value_error_for_invalid_state(self, service):
        with pytest.raises(ValueError, match="Invalid overlay state 'bogus'"):
            await service.set_overlay_state(
                "dispatch_optimizer", "tenant-1", "bogus", "user-admin"
            )

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_not_connected(self, disconnected_service):
        with pytest.raises(RuntimeError, match="Redis client not connected"):
            await disconnected_service.set_overlay_state(
                "dispatch_optimizer", "t1", "shadow", "user-1"
            )

    @pytest.mark.asyncio
    async def test_logs_transition(self, service, caplog):
        service.client.get = AsyncMock(return_value="disabled")
        with caplog.at_level(logging.INFO):
            await service.set_overlay_state(
                "dispatch_optimizer", "tenant-1", "shadow", "user-admin"
            )
        assert "Overlay flag transition" in caplog.text
        assert "dispatch_optimizer" in caplog.text
        assert "tenant-1" in caplog.text
        assert "disabled" in caplog.text
        assert "shadow" in caplog.text
        assert "user-admin" in caplog.text

    @pytest.mark.asyncio
    async def test_all_valid_states_accepted(self, service):
        for state in sorted(VALID_OVERLAY_STATES):
            service.client.get = AsyncMock(return_value="disabled")
            result = await service.set_overlay_state(
                "test_flag", "tenant-1", state, "user-1"
            )
            assert result == "disabled"

    @pytest.mark.asyncio
    async def test_validates_state_before_checking_connection(self):
        """ValueError for invalid state should be raised even without a client."""
        svc = FeatureFlagService(redis_url="redis://localhost:6379")
        svc.client = None
        with pytest.raises(ValueError, match="Invalid overlay state"):
            await svc.set_overlay_state("flag", "t1", "invalid", "u1")
