"""
Unit tests for bootstrap/fuel.py.

Requirements: 1.1, 1.2, 1.7
"""
from unittest.mock import MagicMock, patch
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
    sys.modules.pop("bootstrap.fuel", None)


@pytest.fixture
def container():
    c = ServiceContainer()
    c.settings = MagicMock()
    c.es_service = MagicMock()
    return c


@pytest.fixture
def mock_app():
    return MagicMock()


class TestFuelBootstrap:
    """Tests for bootstrap/fuel.py initialize()."""

    @pytest.mark.asyncio
    async def test_registers_fuel_service(self, mock_app, container):
        """Verify FuelService is registered in the container."""
        mock_fuel = MagicMock()

        with patch("fuel.services.fuel_es_mappings.setup_fuel_indices") as mock_setup, \
             patch("fuel.services.fuel_service.FuelService", return_value=mock_fuel), \
             patch("fuel.api.endpoints.configure_fuel_api") as mock_configure:

            sys.modules.pop("bootstrap.fuel", None)
            from bootstrap.fuel import initialize
            await initialize(mock_app, container)

        assert container.fuel_service is mock_fuel
        mock_setup.assert_called_once()
        mock_configure.assert_called_once_with(fuel_service=mock_fuel)

    @pytest.mark.asyncio
    async def test_index_failure_does_not_crash(self, mock_app, container):
        """Fuel index setup failure should not crash initialization."""
        with patch("fuel.services.fuel_es_mappings.setup_fuel_indices",
                   side_effect=RuntimeError("index fail")), \
             patch("fuel.services.fuel_service.FuelService", return_value=MagicMock()), \
             patch("fuel.api.endpoints.configure_fuel_api"):

            sys.modules.pop("bootstrap.fuel", None)
            from bootstrap.fuel import initialize
            await initialize(mock_app, container)

        assert container.has("fuel_service")
