"""
Unit tests for TenantInventoryConfigService.

Tests cover:
- Default settings returned when no Redis client
- Default settings returned when key not set
- Correct deserialization of stored settings
- Graceful handling of invalid JSON
- Graceful handling of Redis errors
- Setting persistence
- Convenience methods

Requirements: 1.4, 5.1
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from inventory.tenant_config import (
    TenantInventoryConfigService,
    InventorySettings,
    DEFAULT_INVENTORY_SETTINGS,
)


@pytest.fixture
def redis_client():
    """Mock async Redis client."""
    client = AsyncMock()
    return client


@pytest.fixture
def service(redis_client):
    """Service with mocked Redis."""
    return TenantInventoryConfigService(redis_client=redis_client)


@pytest.fixture
def service_no_redis():
    """Service without Redis client."""
    return TenantInventoryConfigService(redis_client=None)


class TestInventorySettingsDefaults:
    """Test default values for InventorySettings."""

    def test_default_block_on_critical_shortage_is_false(self):
        settings = InventorySettings()
        assert settings.block_on_critical_shortage is False

    def test_default_readiness_check_enabled_is_true(self):
        settings = InventorySettings()
        assert settings.readiness_check_enabled is True

    def test_default_auto_consume_on_completion_is_true(self):
        settings = InventorySettings()
        assert settings.auto_consume_on_completion is True


class TestGetSettings:
    """Test get_settings method."""

    @pytest.mark.asyncio
    async def test_returns_defaults_when_no_redis(self, service_no_redis):
        result = await service_no_redis.get_settings("tenant-1")
        assert result == DEFAULT_INVENTORY_SETTINGS

    @pytest.mark.asyncio
    async def test_returns_defaults_when_key_not_set(self, service, redis_client):
        redis_client.get = AsyncMock(return_value=None)
        result = await service.get_settings("tenant-1")
        assert result == InventorySettings()

    @pytest.mark.asyncio
    async def test_deserializes_stored_settings(self, service, redis_client):
        stored = json.dumps({
            "block_on_critical_shortage": True,
            "readiness_check_enabled": False,
            "auto_consume_on_completion": False,
        })
        redis_client.get = AsyncMock(return_value=stored)

        result = await service.get_settings("tenant-1")

        assert result.block_on_critical_shortage is True
        assert result.readiness_check_enabled is False
        assert result.auto_consume_on_completion is False

    @pytest.mark.asyncio
    async def test_handles_partial_settings(self, service, redis_client):
        """Missing keys in stored JSON use defaults."""
        stored = json.dumps({"block_on_critical_shortage": True})
        redis_client.get = AsyncMock(return_value=stored)

        result = await service.get_settings("tenant-1")

        assert result.block_on_critical_shortage is True
        assert result.readiness_check_enabled is True  # default
        assert result.auto_consume_on_completion is True  # default

    @pytest.mark.asyncio
    async def test_handles_bytes_response(self, service, redis_client):
        """Redis may return bytes instead of str."""
        stored = json.dumps({
            "block_on_critical_shortage": True,
            "readiness_check_enabled": True,
            "auto_consume_on_completion": False,
        }).encode("utf-8")
        redis_client.get = AsyncMock(return_value=stored)

        result = await service.get_settings("tenant-1")

        assert result.block_on_critical_shortage is True
        assert result.auto_consume_on_completion is False

    @pytest.mark.asyncio
    async def test_returns_defaults_on_invalid_json(self, service, redis_client):
        redis_client.get = AsyncMock(return_value="not-valid-json{{{")

        result = await service.get_settings("tenant-1")

        assert result == InventorySettings()

    @pytest.mark.asyncio
    async def test_returns_defaults_on_redis_error(self, service, redis_client):
        redis_client.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        result = await service.get_settings("tenant-1")

        assert result == InventorySettings()

    @pytest.mark.asyncio
    async def test_uses_correct_key_pattern(self, service, redis_client):
        redis_client.get = AsyncMock(return_value=None)

        await service.get_settings("my-tenant-123")

        redis_client.get.assert_awaited_once_with(
            "tenant:my-tenant-123:inventory_settings"
        )


class TestSetSettings:
    """Test set_settings method."""

    @pytest.mark.asyncio
    async def test_raises_when_no_redis(self, service_no_redis):
        with pytest.raises(RuntimeError, match="no Redis client configured"):
            await service_no_redis.set_settings(
                "tenant-1", InventorySettings()
            )

    @pytest.mark.asyncio
    async def test_persists_settings_as_json(self, service, redis_client):
        settings = InventorySettings(
            block_on_critical_shortage=True,
            readiness_check_enabled=False,
            auto_consume_on_completion=True,
        )

        await service.set_settings("tenant-1", settings)

        redis_client.set.assert_awaited_once()
        call_args = redis_client.set.call_args
        key = call_args[0][0]
        value = json.loads(call_args[0][1])

        assert key == "tenant:tenant-1:inventory_settings"
        assert value["block_on_critical_shortage"] is True
        assert value["readiness_check_enabled"] is False
        assert value["auto_consume_on_completion"] is True


class TestConvenienceMethods:
    """Test individual setting getter methods."""

    @pytest.mark.asyncio
    async def test_get_block_on_critical_shortage(self, service, redis_client):
        stored = json.dumps({"block_on_critical_shortage": True})
        redis_client.get = AsyncMock(return_value=stored)

        result = await service.get_block_on_critical_shortage("tenant-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_readiness_check_enabled(self, service, redis_client):
        stored = json.dumps({"readiness_check_enabled": False})
        redis_client.get = AsyncMock(return_value=stored)

        result = await service.get_readiness_check_enabled("tenant-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_auto_consume_on_completion(self, service, redis_client):
        stored = json.dumps({"auto_consume_on_completion": False})
        redis_client.get = AsyncMock(return_value=stored)

        result = await service.get_auto_consume_on_completion("tenant-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_convenience_methods_return_defaults_on_error(
        self, service, redis_client
    ):
        redis_client.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        assert await service.get_block_on_critical_shortage("t1") is False
        assert await service.get_readiness_check_enabled("t1") is True
        assert await service.get_auto_consume_on_completion("t1") is True
