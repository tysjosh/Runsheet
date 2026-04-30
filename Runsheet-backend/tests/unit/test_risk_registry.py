"""
Unit tests for the Risk Registry module.

Tests the RiskLevel enum, DEFAULT_RISK_REGISTRY, and RiskRegistry class
including Redis-backed overrides and fallback behavior.

Requirements: 1.4, 1.5
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.risk_registry import RiskLevel, RiskRegistry, DEFAULT_RISK_REGISTRY


class TestRiskLevel:
    """Tests for the RiskLevel enum."""

    def test_risk_level_values(self):
        assert RiskLevel.LOW == "low"
        assert RiskLevel.MEDIUM == "medium"
        assert RiskLevel.HIGH == "high"

    def test_risk_level_is_string(self):
        assert isinstance(RiskLevel.LOW, str)
        assert isinstance(RiskLevel.MEDIUM, str)
        assert isinstance(RiskLevel.HIGH, str)

    def test_risk_level_from_value(self):
        assert RiskLevel("low") == RiskLevel.LOW
        assert RiskLevel("medium") == RiskLevel.MEDIUM
        assert RiskLevel("high") == RiskLevel.HIGH

    def test_risk_level_invalid_value(self):
        with pytest.raises(ValueError):
            RiskLevel("invalid")


class TestDefaultRiskRegistry:
    """Tests for the DEFAULT_RISK_REGISTRY mapping."""

    def test_low_risk_tools(self):
        assert DEFAULT_RISK_REGISTRY["update_fuel_threshold"] == RiskLevel.LOW

    def test_medium_risk_tools(self):
        medium_tools = [
            "assign_asset_to_job",
            "update_job_status",
            "create_job",
            "escalate_shipment",
            "request_fuel_refill",
        ]
        for tool in medium_tools:
            assert DEFAULT_RISK_REGISTRY[tool] == RiskLevel.MEDIUM, f"{tool} should be MEDIUM"

    def test_high_risk_tools(self):
        high_tools = ["cancel_job", "reassign_rider"]
        for tool in high_tools:
            assert DEFAULT_RISK_REGISTRY[tool] == RiskLevel.HIGH, f"{tool} should be HIGH"

    def test_all_tools_have_valid_risk_levels(self):
        for tool_name, level in DEFAULT_RISK_REGISTRY.items():
            assert isinstance(level, RiskLevel), f"{tool_name} has invalid risk level"

    def test_registry_has_expected_tool_count(self):
        assert len(DEFAULT_RISK_REGISTRY) == 8


class TestRiskRegistryClassifyWithoutRedis:
    """Tests for RiskRegistry.classify without Redis."""

    @pytest.fixture
    def registry(self):
        return RiskRegistry(redis_client=None)

    async def test_classify_known_low_risk(self, registry):
        result = await registry.classify("update_fuel_threshold")
        assert result == RiskLevel.LOW

    async def test_classify_known_medium_risk(self, registry):
        result = await registry.classify("assign_asset_to_job")
        assert result == RiskLevel.MEDIUM

    async def test_classify_known_high_risk(self, registry):
        result = await registry.classify("cancel_job")
        assert result == RiskLevel.HIGH

    async def test_classify_unknown_tool_defaults_to_high(self, registry):
        result = await registry.classify("unknown_tool")
        assert result == RiskLevel.HIGH

    async def test_classify_empty_string_defaults_to_high(self, registry):
        result = await registry.classify("")
        assert result == RiskLevel.HIGH


class TestRiskRegistryClassifyWithRedis:
    """Tests for RiskRegistry.classify with Redis overrides."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        return redis

    @pytest.fixture
    def registry(self, mock_redis):
        return RiskRegistry(redis_client=mock_redis)

    async def test_classify_uses_redis_override(self, registry, mock_redis):
        mock_redis.get = AsyncMock(return_value=b"low")
        result = await registry.classify("cancel_job")
        assert result == RiskLevel.LOW
        mock_redis.get.assert_called_once_with("risk_override:cancel_job")

    async def test_classify_falls_back_to_default_when_no_override(self, registry, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        result = await registry.classify("cancel_job")
        assert result == RiskLevel.HIGH

    async def test_classify_handles_string_redis_value(self, registry, mock_redis):
        mock_redis.get = AsyncMock(return_value="medium")
        result = await registry.classify("cancel_job")
        assert result == RiskLevel.MEDIUM

    async def test_classify_falls_back_on_redis_error(self, registry, mock_redis):
        mock_redis.get = AsyncMock(side_effect=Exception("Redis connection error"))
        result = await registry.classify("cancel_job")
        assert result == RiskLevel.HIGH

    async def test_classify_unknown_tool_with_redis_miss(self, registry, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)
        result = await registry.classify("nonexistent_tool")
        assert result == RiskLevel.HIGH


class TestRiskRegistrySetOverride:
    """Tests for RiskRegistry.set_override."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        return redis

    async def test_set_override_writes_to_redis(self, mock_redis):
        registry = RiskRegistry(redis_client=mock_redis)
        await registry.set_override("cancel_job", RiskLevel.LOW)
        mock_redis.set.assert_called_once_with("risk_override:cancel_job", "low")

    async def test_set_override_without_redis_does_not_raise(self):
        registry = RiskRegistry(redis_client=None)
        # Should not raise, just log a warning
        await registry.set_override("cancel_job", RiskLevel.LOW)

    async def test_set_override_then_classify_uses_override(self, mock_redis):
        registry = RiskRegistry(redis_client=mock_redis)
        # After setting override, Redis returns the new value
        mock_redis.get = AsyncMock(return_value=b"low")
        await registry.set_override("cancel_job", RiskLevel.LOW)
        result = await registry.classify("cancel_job")
        assert result == RiskLevel.LOW
