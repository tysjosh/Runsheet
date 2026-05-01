"""
Unit tests for bootstrap/core.py.

Requirements: 1.1, 1.2, 1.7
"""
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest

from bootstrap.container import ServiceContainer


@pytest.fixture
def container():
    return ServiceContainer()


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.state = MagicMock()
    return app


# We need to prevent the real elasticsearch_service from being imported
# (it tries to connect to ES). We mock the entire services module.
@pytest.fixture(autouse=True)
def _mock_external_services():
    """Mock external service modules to prevent real connections."""
    mock_es_mod = MagicMock()
    mock_es_mod.elasticsearch_service = MagicMock()
    mock_seeder_mod = MagicMock()
    mock_seeder_mod.data_seeder = MagicMock()
    mock_seeder_mod.data_seeder.seed_baseline_data = AsyncMock()

    saved = {}
    mods_to_mock = {
        "services.elasticsearch_service": mock_es_mod,
        "services.data_seeder": mock_seeder_mod,
    }
    for name, mock_mod in mods_to_mock.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mock_mod

    yield {
        "es_service": mock_es_mod.elasticsearch_service,
        "data_seeder": mock_seeder_mod.data_seeder,
    }

    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig

    # Clear cached bootstrap.core so next test gets fresh import
    sys.modules.pop("bootstrap.core", None)


class TestCoreBootstrap:
    """Tests for bootstrap/core.py initialize()."""

    @pytest.mark.asyncio
    async def test_registers_core_services(
        self, mock_app, container, _mock_external_services
    ):
        """Verify core services are registered in the container."""
        mock_settings = MagicMock()
        mock_telemetry = MagicMock()
        mock_health = MagicMock()
        mock_ingestion = MagicMock()
        mock_cm = MagicMock()

        with patch("config.settings.get_settings", return_value=mock_settings), \
             patch("telemetry.service.initialize_telemetry", return_value=mock_telemetry), \
             patch("health.service.HealthCheckService", return_value=mock_health), \
             patch("ingestion.service.DataIngestionService", return_value=mock_ingestion), \
             patch("websocket.connection_manager.ConnectionManager", return_value=mock_cm), \
             patch("websocket.connection_manager.bind_container"), \
             patch("errors.handlers.register_exception_handlers"):

            sys.modules.pop("bootstrap.core", None)
            from bootstrap.core import initialize
            await initialize(mock_app, container)

        assert container.settings is mock_settings
        assert container.telemetry_service is mock_telemetry
        assert container.health_check_service is mock_health
        assert container.data_ingestion_service is mock_ingestion
        assert container.fleet_ws_manager is mock_cm

    @pytest.mark.asyncio
    async def test_seed_failure_does_not_crash(
        self, mock_app, container, _mock_external_services
    ):
        """Seeding failure should be logged but not crash initialization."""
        _mock_external_services["data_seeder"].seed_baseline_data = AsyncMock(
            side_effect=RuntimeError("seed fail")
        )

        with patch("config.settings.get_settings", return_value=MagicMock()), \
             patch("telemetry.service.initialize_telemetry", return_value=MagicMock()), \
             patch("health.service.HealthCheckService", return_value=MagicMock()), \
             patch("ingestion.service.DataIngestionService", return_value=MagicMock()), \
             patch("websocket.connection_manager.ConnectionManager", return_value=MagicMock()), \
             patch("websocket.connection_manager.bind_container"), \
             patch("errors.handlers.register_exception_handlers"):

            sys.modules.pop("bootstrap.core", None)
            from bootstrap.core import initialize
            await initialize(mock_app, container)

        assert container.has("settings")
        assert container.has("es_service")
