"""
Unit tests for the ops AI tools feature guard.

Validates: Requirement 27.3 — AI tools return structured disabled response
for disabled tenants, never raise exceptions, and fail-open on errors.
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mock elasticsearch_service before any transitive import can trigger it.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from Agents.tools.ops_feature_guard import (  # noqa: E402
    DISABLED_RESPONSE,
    check_ops_feature_flag,
    configure_ops_feature_guard,
)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the module-level service reference between tests."""
    import Agents.tools.ops_feature_guard as mod

    original = mod._feature_flag_service
    yield
    mod._feature_flag_service = original


@pytest.fixture()
def mock_ff_service():
    svc = AsyncMock()
    svc.is_enabled = AsyncMock(return_value=True)
    return svc


# --- configure_ops_feature_guard ---


def test_configure_sets_module_service(mock_ff_service):
    import Agents.tools.ops_feature_guard as mod

    configure_ops_feature_guard(mock_ff_service)
    assert mod._feature_flag_service is mock_ff_service


# --- check_ops_feature_flag: enabled tenant ---


@pytest.mark.asyncio
async def test_enabled_tenant_returns_none(mock_ff_service):
    configure_ops_feature_guard(mock_ff_service)
    mock_ff_service.is_enabled.return_value = True

    result = await check_ops_feature_flag("tenant-1")
    assert result is None
    mock_ff_service.is_enabled.assert_awaited_once_with("tenant-1")


# --- check_ops_feature_flag: disabled tenant ---


@pytest.mark.asyncio
async def test_disabled_tenant_returns_structured_response(mock_ff_service):
    configure_ops_feature_guard(mock_ff_service)
    mock_ff_service.is_enabled.return_value = False

    result = await check_ops_feature_flag("tenant-disabled")

    assert result is not None
    parsed = json.loads(result)
    assert parsed["status"] == "disabled"
    assert "not enabled" in parsed["message"].lower()


@pytest.mark.asyncio
async def test_disabled_response_matches_constant(mock_ff_service):
    configure_ops_feature_guard(mock_ff_service)
    mock_ff_service.is_enabled.return_value = False

    result = await check_ops_feature_flag("t1")
    assert result == DISABLED_RESPONSE


# --- check_ops_feature_flag: no tenant_id ---


@pytest.mark.asyncio
async def test_none_tenant_id_returns_none(mock_ff_service):
    configure_ops_feature_guard(mock_ff_service)

    result = await check_ops_feature_flag(None)
    assert result is None
    mock_ff_service.is_enabled.assert_not_awaited()


# --- check_ops_feature_flag: service not configured (fail-open) ---


@pytest.mark.asyncio
async def test_service_not_configured_returns_none():
    """When FeatureFlagService is not wired, fail-open."""
    import Agents.tools.ops_feature_guard as mod

    mod._feature_flag_service = None

    result = await check_ops_feature_flag("tenant-1")
    assert result is None


# --- check_ops_feature_flag: service raises (fail-open) ---


@pytest.mark.asyncio
async def test_service_exception_returns_none(mock_ff_service):
    """If Redis is down or any error occurs, fail-open."""
    configure_ops_feature_guard(mock_ff_service)
    mock_ff_service.is_enabled.side_effect = RuntimeError("Redis connection lost")

    result = await check_ops_feature_flag("tenant-1")
    assert result is None
