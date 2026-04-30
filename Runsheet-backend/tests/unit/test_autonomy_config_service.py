"""
Unit tests for the Autonomy Config Service module.

Tests the AutonomyConfigService class including Redis-backed get/set,
default fallback behavior, validation, and error handling.

Requirements: 10.1, 10.2, 10.6
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.autonomy_config_service import (
    AutonomyConfigService,
    DEFAULT_AUTONOMY_LEVEL,
    VALID_AUTONOMY_LEVELS,
)


class TestConstants:
    """Tests for module-level constants."""

    def test_default_autonomy_level_is_suggest_only(self):
        assert DEFAULT_AUTONOMY_LEVEL == "suggest-only"

    def test_valid_levels_contains_all_four(self):
        expected = {"suggest-only", "auto-low", "auto-medium", "full-auto"}
        assert VALID_AUTONOMY_LEVELS == expected

    def test_valid_levels_is_frozenset(self):
        assert isinstance(VALID_AUTONOMY_LEVELS, frozenset)


class TestGetLevelWithoutRedis:
    """Tests for get_level when no Redis client is configured."""

    @pytest.fixture
    def service(self):
        return AutonomyConfigService(redis_client=None)

    async def test_returns_default_for_any_tenant(self, service):
        result = await service.get_level("tenant-123")
        assert result == "suggest-only"

    async def test_returns_default_for_empty_tenant_id(self, service):
        result = await service.get_level("")
        assert result == "suggest-only"


class TestGetLevelWithRedis:
    """Tests for get_level with Redis client."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        return redis

    @pytest.fixture
    def service(self, mock_redis):
        return AutonomyConfigService(redis_client=mock_redis)

    async def test_returns_stored_level_bytes(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=b"auto-low")
        result = await service.get_level("tenant-123")
        assert result == "auto-low"
        mock_redis.get.assert_called_once_with("tenant:tenant-123:autonomy_level")

    async def test_returns_stored_level_string(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value="auto-medium")
        result = await service.get_level("tenant-123")
        assert result == "auto-medium"

    async def test_returns_full_auto(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=b"full-auto")
        result = await service.get_level("tenant-123")
        assert result == "full-auto"

    async def test_returns_default_when_not_set(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        result = await service.get_level("tenant-123")
        assert result == "suggest-only"

    async def test_returns_default_on_redis_error(self, service, mock_redis):
        mock_redis.get = AsyncMock(side_effect=Exception("Redis connection error"))
        result = await service.get_level("tenant-123")
        assert result == "suggest-only"

    async def test_returns_default_for_invalid_stored_value(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=b"invalid-level")
        result = await service.get_level("tenant-123")
        assert result == "suggest-only"

    async def test_uses_correct_key_pattern(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        await service.get_level("my-tenant")
        mock_redis.get.assert_called_once_with("tenant:my-tenant:autonomy_level")


class TestSetLevel:
    """Tests for set_level."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        return redis

    @pytest.fixture
    def service(self, mock_redis):
        return AutonomyConfigService(redis_client=mock_redis)

    async def test_set_valid_level_writes_to_redis(self, service, mock_redis):
        await service.set_level("tenant-123", "auto-low")
        mock_redis.set.assert_called_once_with(
            "tenant:tenant-123:autonomy_level", "auto-low"
        )

    async def test_set_returns_previous_level(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=b"auto-low")
        previous = await service.set_level("tenant-123", "auto-medium")
        assert previous == "auto-low"

    async def test_set_returns_default_when_no_previous(self, service, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        previous = await service.set_level("tenant-123", "auto-low")
        assert previous == "suggest-only"

    async def test_set_all_valid_levels(self, service, mock_redis):
        for level in VALID_AUTONOMY_LEVELS:
            mock_redis.set.reset_mock()
            await service.set_level("tenant-123", level)
            mock_redis.set.assert_called_once_with(
                "tenant:tenant-123:autonomy_level", level
            )

    async def test_set_invalid_level_raises_value_error(self, service):
        with pytest.raises(ValueError, match="Invalid autonomy level"):
            await service.set_level("tenant-123", "invalid-level")

    async def test_set_empty_level_raises_value_error(self, service):
        with pytest.raises(ValueError, match="Invalid autonomy level"):
            await service.set_level("tenant-123", "")

    async def test_set_without_redis_does_not_raise(self):
        service = AutonomyConfigService(redis_client=None)
        # Should not raise, just log a warning; returns default as previous
        previous = await service.set_level("tenant-123", "auto-low")
        assert previous == "suggest-only"

    async def test_set_propagates_redis_write_error(self, service, mock_redis):
        mock_redis.set = AsyncMock(side_effect=Exception("Redis write error"))
        with pytest.raises(Exception, match="Redis write error"):
            await service.set_level("tenant-123", "auto-low")

    async def test_set_uses_correct_key_pattern(self, service, mock_redis):
        await service.set_level("my-tenant", "full-auto")
        mock_redis.set.assert_called_once_with(
            "tenant:my-tenant:autonomy_level", "full-auto"
        )


class TestSetThenGet:
    """Integration-style tests verifying set followed by get."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis that simulates actual storage."""
        store = {}
        redis = MagicMock()

        async def mock_get(key):
            return store.get(key)

        async def mock_set(key, value):
            store[key] = value
            return True

        redis.get = AsyncMock(side_effect=mock_get)
        redis.set = AsyncMock(side_effect=mock_set)
        return redis

    @pytest.fixture
    def service(self, mock_redis):
        return AutonomyConfigService(redis_client=mock_redis)

    async def test_get_after_set_returns_new_level(self, service):
        await service.set_level("tenant-123", "auto-medium")
        result = await service.get_level("tenant-123")
        assert result == "auto-medium"

    async def test_different_tenants_have_independent_levels(self, service):
        await service.set_level("tenant-a", "auto-low")
        await service.set_level("tenant-b", "full-auto")

        assert await service.get_level("tenant-a") == "auto-low"
        assert await service.get_level("tenant-b") == "full-auto"

    async def test_overwrite_level(self, service):
        await service.set_level("tenant-123", "auto-low")
        previous = await service.set_level("tenant-123", "full-auto")
        assert previous == "auto-low"
        assert await service.get_level("tenant-123") == "full-auto"
