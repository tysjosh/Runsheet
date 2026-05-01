"""
Unit tests for bootstrap/ops.py.

Requirements: 1.1, 1.2, 1.7
"""
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest

from bootstrap.container import ServiceContainer


def _mock_modules():
    """Mock all external modules that bootstrap/ops.py imports."""
    mocks = {}
    modules_to_mock = [
        "services.elasticsearch_service",
        "Agents.tools",
        "Agents.tools.ops_feature_guard",
        "Agents.tools.ops_search_tools",
        "Agents.tools.ops_report_tools",
    ]
    for mod_name in modules_to_mock:
        mock_mod = MagicMock()
        mocks[mod_name] = mock_mod
    return mocks


@pytest.fixture(autouse=True)
def _mock_external():
    """Prevent real connections during import."""
    mocks = _mock_modules()
    saved = {}
    for name, mock_mod in mocks.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mock_mod
    yield mocks
    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
    sys.modules.pop("bootstrap.ops", None)


@pytest.fixture
def container():
    c = ServiceContainer()
    c.settings = MagicMock(
        redis_url="redis://localhost:6379",
        dinee_idempotency_ttl_hours=72,
        dinee_webhook_secret="test-secret",
        dinee_webhook_tenant_id="test-tenant",
        drift_threshold_pct=1.0,
        drift_schedule_interval_hours=6,
    )
    c.es_service = MagicMock()
    return c


@pytest.fixture
def mock_app():
    return MagicMock()


class TestOpsBootstrap:
    """Tests for bootstrap/ops.py initialize()."""

    @pytest.mark.asyncio
    async def test_registers_ops_services(self, mock_app, container):
        """Verify ops services are registered in the container."""
        mock_ops_es = MagicMock()
        mock_idemp = MagicMock()
        mock_idemp.connect = AsyncMock()
        mock_ff = MagicMock()
        mock_ff.connect = AsyncMock()
        mock_ws = MagicMock()

        with patch("ops.services.ops_es_service.OpsElasticsearchService", return_value=mock_ops_es), \
             patch("ops.ingestion.adapter.AdapterTransformer"), \
             patch("ops.ingestion.handlers.v1_0.V1SchemaHandler"), \
             patch("ops.ingestion.idempotency.IdempotencyService", return_value=mock_idemp), \
             patch("ops.ingestion.poison_queue.PoisonQueueService"), \
             patch("ops.services.feature_flags.FeatureFlagService", return_value=mock_ff), \
             patch("ops.websocket.ops_ws.OpsWebSocketManager", return_value=mock_ws), \
             patch("ops.websocket.ops_ws.bind_container") as mock_bind, \
             patch("ops.webhooks.receiver.configure_webhook_receiver") as mock_webhook, \
             patch("ops.api.endpoints.configure_ops_api") as mock_ops_api, \
             patch("ops.ingestion.replay.configure_replay_service"), \
             patch("ops.services.drift_detector.configure_drift_detector"):

            sys.modules.pop("bootstrap.ops", None)
            from bootstrap.ops import initialize
            await initialize(mock_app, container)

        assert container.ops_es_service is mock_ops_es
        assert container.ops_idempotency is mock_idemp
        assert container.ops_feature_flags is mock_ff
        assert container.ops_ws_manager is mock_ws
        mock_bind.assert_called_once_with(container)
        mock_webhook.assert_called_once()
        mock_ops_api.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_closes_services(self):
        """Verify shutdown disconnects idempotency and feature flags."""
        c = ServiceContainer()
        mock_ws = MagicMock()
        mock_ws.shutdown = AsyncMock()
        c.ops_ws_manager = mock_ws
        mock_idemp = MagicMock()
        mock_idemp.disconnect = AsyncMock()
        c.ops_idempotency = mock_idemp
        mock_ff = MagicMock()
        mock_ff.disconnect = AsyncMock()
        c.ops_feature_flags = mock_ff

        from bootstrap.ops import shutdown
        await shutdown(MagicMock(), c)

        mock_ws.shutdown.assert_awaited_once()
        mock_idemp.disconnect.assert_awaited_once()
        mock_ff.disconnect.assert_awaited_once()
