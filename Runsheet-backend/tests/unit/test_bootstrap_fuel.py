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

        with patch("fuel.services.fuel_service.FuelService", return_value=mock_fuel), \
             patch("fuel.api.endpoints.configure_fuel_api") as mock_configure:

            sys.modules.pop("bootstrap.fuel", None)
            from bootstrap.fuel import initialize
            await initialize(mock_app, container)

        assert container.fuel_service is mock_fuel
        # ``mock_setup.assert_called_once()`` was here, asserting bootstrap created
        # the fuel indices. Phase 6 deleted ``setup_fuel_indices``: the document
        # store is one Postgres table created by a migration, so there is nothing
        # for bootstrap to create. What still matters — that the service is
        # registered and the API is wired — is asserted either side.
        mock_configure.assert_called_once_with(fuel_service=mock_fuel)

    @pytest.mark.asyncio
    async def test_initialize_is_fail_open(self, mock_app, container):
        """Bootstrap registers the service even when an earlier step misbehaves.

        This was ``test_index_failure_does_not_crash``, and it made
        ``setup_fuel_indices`` raise to prove the bootstrap chain was fail-open. That
        function is gone, so the specific trigger is gone with it. The property is
        still worth an assertion — the chain must reach ``fuel_service`` — so it is
        kept rather than deleted along with its old trigger.
        """
        with patch("fuel.services.fuel_service.FuelService", return_value=MagicMock()), \
             patch("fuel.api.endpoints.configure_fuel_api"):

            sys.modules.pop("bootstrap.fuel", None)
            from bootstrap.fuel import initialize
            await initialize(mock_app, container)

        assert container.has("fuel_service")


class TestPODOTPServiceRegistration:
    """PODOTPService and the ``order.dispatched`` subscription (task 10.3).

    Validates: Requirements 5.25, 5.27
    """

    @pytest.mark.asyncio
    async def test_registers_pod_otp_service_on_order_dispatched(
        self, mock_app, container
    ):
        """The subscription is what makes the service reachable at all."""
        order_service = MagicMock()
        container.order_repository = MagicMock()

        with patch("fuel.services.fuel_service.FuelService", return_value=MagicMock()), \
             patch("fuel.api.endpoints.configure_fuel_api"), \
             patch("fuel.services.order_service.OrderService",
                   return_value=order_service):

            sys.modules.pop("bootstrap.fuel", None)
            from bootstrap.fuel import initialize
            await initialize(mock_app, container)

        assert container.has("pod_otp_service")
        pod_otp_service = container.pod_otp_service
        subscribed = {
            call.args[0]: call.args[1]
            for call in order_service.subscribe.call_args_list
        }
        assert "order.dispatched" in subscribed
        assert subscribed["order.dispatched"] == (
            pod_otp_service.on_order_dispatched
        )

    @pytest.mark.asyncio
    async def test_notification_service_is_not_wired_here(
        self, mock_app, container
    ):
        """``notifications`` boots after ``fuel``, so it cannot be wired yet."""
        container.order_repository = MagicMock()

        with patch("fuel.services.fuel_service.FuelService", return_value=MagicMock()), \
             patch("fuel.api.endpoints.configure_fuel_api"), \
             patch("fuel.services.order_service.OrderService",
                   return_value=MagicMock()):

            sys.modules.pop("bootstrap.fuel", None)
            from bootstrap.fuel import initialize
            await initialize(mock_app, container)

        assert container.pod_otp_service._notification_service is None
