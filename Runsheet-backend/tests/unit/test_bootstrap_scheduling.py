"""
Unit tests for bootstrap/scheduling.py.

Requirements: 1.1, 1.2, 1.7
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest

from bootstrap.container import ServiceContainer


@pytest.fixture(autouse=True)
def _mock_es_module():
    """Prevent real ES connections during import."""
    mock_es_mod = MagicMock()
    mock_es_mod.elasticsearch_service = MagicMock()
    saved = sys.modules.get("services.elasticsearch_service")
    sys.modules["services.elasticsearch_service"] = mock_es_mod
    yield
    if saved is None:
        sys.modules.pop("services.elasticsearch_service", None)
    else:
        sys.modules["services.elasticsearch_service"] = saved
    sys.modules.pop("bootstrap.scheduling", None)


@pytest.fixture
def container():
    c = ServiceContainer()
    c.settings = MagicMock(
        redis_url="redis://localhost:6379",
        scheduling_delay_check_interval_seconds=60,
    )
    c.es_service = MagicMock()
    return c


@pytest.fixture
def mock_app():
    return MagicMock()


class TestSchedulingBootstrap:
    """Tests for bootstrap/scheduling.py initialize()."""

    @pytest.mark.asyncio
    async def test_registers_scheduling_services(self, mock_app, container):
        """Verify scheduling services are registered in the container."""
        mock_job = MagicMock()
        mock_cargo = MagicMock()
        mock_delay = MagicMock()
        mock_ws = MagicMock()

        with patch("scheduling.services.scheduling_es_mappings.setup_scheduling_indices"), \
             patch("scheduling.services.job_service.JobService", return_value=mock_job), \
             patch("scheduling.services.cargo_service.CargoService", return_value=mock_cargo), \
             patch("scheduling.services.delay_detection_service.DelayDetectionService", return_value=mock_delay), \
             patch("scheduling.websocket.scheduling_ws.SchedulingWebSocketManager", return_value=mock_ws), \
             patch("scheduling.websocket.scheduling_ws.bind_container") as mock_bind, \
             patch("scheduling.api.endpoints.configure_scheduling_api") as mock_configure, \
             patch("asyncio.create_task", return_value=MagicMock()) as mock_create_task:

            sys.modules.pop("bootstrap.scheduling", None)
            from bootstrap.scheduling import initialize
            await initialize(mock_app, container)

        assert container.job_service is mock_job
        assert container.cargo_service is mock_cargo
        assert container.delay_detection_service is mock_delay
        assert container.scheduling_ws_manager is mock_ws
        mock_bind.assert_called_once_with(container)
        mock_configure.assert_called_once()
        mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_task_and_ws(self):
        """Verify shutdown cancels the periodic delay check task."""
        # Import the module fresh
        sys.modules.pop("bootstrap.scheduling", None)
        import bootstrap.scheduling as sched_mod

        # Create a real asyncio task that we can cancel
        async def _noop():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_noop())
        sched_mod._delay_check_task = task

        c = ServiceContainer()
        mock_ws = MagicMock()
        mock_ws.shutdown = AsyncMock()
        c.scheduling_ws_manager = mock_ws

        await sched_mod.shutdown(MagicMock(), c)

        assert task.cancelled()
        mock_ws.shutdown.assert_awaited_once()

        # Cleanup
        sched_mod._delay_check_task = None
