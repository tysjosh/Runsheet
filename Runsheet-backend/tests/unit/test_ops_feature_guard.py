"""
Unit tests for the ops AI tools feature guard.

Validates: Requirement 27.3 — AI tools return structured disabled response
for disabled tenants, never raise exceptions, and fail-closed on errors.
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
    SERVICE_UNAVAILABLE_RESPONSE,
    check_ops_feature_flag,
    configure_ops_feature_guard,
    get_feature_flag_errors_total,
)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the module-level service reference between tests."""
    import Agents.tools.ops_feature_guard as mod

    original = mod._feature_flag_service
    original_errors = mod._feature_flag_errors_total
    yield
    mod._feature_flag_service = original
    mod._feature_flag_errors_total = original_errors


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


# --- check_ops_feature_flag: service not configured (fail-closed) ---


@pytest.mark.asyncio
async def test_service_not_configured_returns_disabled():
    """When FeatureFlagService is not wired, fail-closed."""
    import Agents.tools.ops_feature_guard as mod

    mod._feature_flag_service = None
    errors_before = mod._feature_flag_errors_total

    result = await check_ops_feature_flag("tenant-1")
    assert result == SERVICE_UNAVAILABLE_RESPONSE
    assert mod._feature_flag_errors_total == errors_before + 1


# --- check_ops_feature_flag: service raises (fail-closed) ---


@pytest.mark.asyncio
async def test_service_exception_returns_disabled(mock_ff_service):
    """If Redis is down or any error occurs, fail-closed."""
    import Agents.tools.ops_feature_guard as mod

    configure_ops_feature_guard(mock_ff_service)
    mock_ff_service.is_enabled.side_effect = RuntimeError("Redis connection lost")
    errors_before = mod._feature_flag_errors_total

    result = await check_ops_feature_flag("tenant-1")
    assert result == SERVICE_UNAVAILABLE_RESPONSE
    assert mod._feature_flag_errors_total == errors_before + 1


# --- get_feature_flag_errors_total ---


@pytest.mark.asyncio
async def test_error_counter_accessible():
    """The error counter is exposed for observability."""
    count = get_feature_flag_errors_total()
    assert isinstance(count, int)
