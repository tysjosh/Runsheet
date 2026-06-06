"""
Unit tests for the /ws/driver WebSocket endpoint and related auth helpers.

Tests the _ws_authenticate_driver function and the /ws/driver endpoint
in main.py, as well as the DriverWSManager bootstrap wiring.

After the SuperTokens hard cutover the driver handshake is authenticated
against a verified SuperTokens session only (the homegrown JWT path was
removed). These tests drive the WS session-verifier seam
(``bootstrap.websockets.configure_ws_session_verifier``) so no live managed
core is required.

Validates: Requirements 9.1, 9.2, 7.1-7.3
"""
import pytest
from unittest.mock import MagicMock

import bootstrap.websockets as ws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws(token: str = ""):
    """Build a fake WebSocket carrying ``token`` via the query-param fallback."""
    websocket = MagicMock()
    websocket.query_params = {"token": token}
    websocket.headers = {}
    return websocket


def _session_verifier(claims):
    """Return an async WS verifier yielding ``claims`` (or None)."""

    async def _verify(access_token, anti_csrf):
        return claims

    return _verify


@pytest.fixture(autouse=True)
def _reset_verifier():
    ws.configure_ws_session_verifier(None)
    yield
    ws.configure_ws_session_verifier(None)


# ---------------------------------------------------------------------------
# Tests: _ws_authenticate_driver (SuperTokens session path)
# ---------------------------------------------------------------------------


class TestWsAuthenticateDriver:
    """Tests for the _ws_authenticate_driver helper. Validates: Req 9.1, 9.2, 7.1-7.3

    The handshake is authenticated against a verified SuperTokens session; the
    helper is async, so each case awaits it. Verified claims are supplied via
    the WS session-verifier seam.
    """

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self):
        """A handshake with no verifiable session is rejected."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(_session_verifier(None))
        result = await _ws_authenticate_driver(_make_ws(token=""))

        assert result is None

    @pytest.mark.asyncio
    async def test_valid_session_returns_tenant_and_driver(self):
        """A verified session with tenant_id and driver_id returns both."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(
            _session_verifier({"tenant_id": "t-1", "driver_id": "d-1"})
        )
        result = await _ws_authenticate_driver(_make_ws(token="tok"))

        assert result == ("t-1", "d-1")

    @pytest.mark.asyncio
    async def test_session_missing_driver_id_returns_none(self):
        """A verified session with tenant_id but no driver_id returns None."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(
            _session_verifier({"tenant_id": "t-1"})
        )
        result = await _ws_authenticate_driver(_make_ws(token="tok"))

        assert result is None

    @pytest.mark.asyncio
    async def test_session_missing_tenant_id_returns_none(self):
        """A verified session with driver_id but no tenant_id returns None."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(
            _session_verifier({"driver_id": "d-1"})
        )
        result = await _ws_authenticate_driver(_make_ws(token="tok"))

        assert result is None

    @pytest.mark.asyncio
    async def test_unverifiable_session_returns_none(self):
        """When the verifier reports no session, the handshake is rejected."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(_session_verifier(None))
        result = await _ws_authenticate_driver(_make_ws(token="not-a-valid-token"))

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_tenant_id_returns_none(self):
        """A verified session with empty-string tenant_id returns None."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(
            _session_verifier({"tenant_id": "", "driver_id": "d-1"})
        )
        result = await _ws_authenticate_driver(_make_ws(token="tok"))

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_driver_id_returns_none(self):
        """A verified session with empty-string driver_id returns None."""
        from main import _ws_authenticate_driver

        ws.configure_ws_session_verifier(
            _session_verifier({"tenant_id": "t-1", "driver_id": ""})
        )
        result = await _ws_authenticate_driver(_make_ws(token="tok"))

        assert result is None


# ---------------------------------------------------------------------------
# Tests: Bootstrap wiring
# ---------------------------------------------------------------------------


class TestBootstrapDriverWSManagerWiring:
    """Tests for DriverWSManager bootstrap wiring in scheduling.py."""

    def test_driver_ws_manager_registered_on_container(self):
        """DriverWSManager should be stored on the ServiceContainer."""
        from bootstrap.container import ServiceContainer
        from driver.ws.driver_ws_manager import DriverWSManager

        container = ServiceContainer()
        manager = DriverWSManager()
        container.driver_ws_manager = manager

        assert container.has("driver_ws_manager")
        assert container.driver_ws_manager is manager

    def test_bind_container_wires_singleton(self):
        """bind_container should wire the container for get_driver_ws_manager."""
        from bootstrap.container import ServiceContainer
        from driver.ws.driver_ws_manager import (
            DriverWSManager,
            bind_container,
            get_driver_ws_manager,
        )

        container = ServiceContainer()
        manager = DriverWSManager()
        container.driver_ws_manager = manager

        bind_container(container)

        result = get_driver_ws_manager()
        assert result is manager

        # Clean up: unbind
        bind_container(None)

    def test_configure_driver_endpoints_accepts_driver_ws_manager(self):
        """configure_driver_endpoints should accept driver_ws_manager kwarg."""
        from scheduling.api.driver_endpoints import configure_driver_endpoints
        from driver.ws.driver_ws_manager import DriverWSManager

        mock_job_service = MagicMock()
        mock_sched_ws = MagicMock()
        mock_driver_ws = DriverWSManager()

        # Should not raise
        configure_driver_endpoints(
            job_service=mock_job_service,
            scheduling_ws_manager=mock_sched_ws,
            driver_ws_manager=mock_driver_ws,
        )

    def test_configure_message_endpoints_accepts_driver_ws_manager(self):
        """configure_message_endpoints should accept driver_ws_manager kwarg."""
        from driver.api.message_endpoints import configure_message_endpoints
        from driver.ws.driver_ws_manager import DriverWSManager

        mock_es = MagicMock()
        mock_driver_ws = DriverWSManager()

        # Should not raise
        configure_message_endpoints(
            es_service=mock_es,
            driver_ws_manager=mock_driver_ws,
        )

    def test_configure_exception_endpoints_accepts_driver_ws_manager(self):
        """configure_exception_endpoints should accept driver_ws_manager kwarg."""
        from driver.api.exception_endpoints import configure_exception_endpoints
        from driver.ws.driver_ws_manager import DriverWSManager

        mock_es = MagicMock()
        mock_driver_ws = DriverWSManager()

        # Should not raise
        configure_exception_endpoints(
            es_service=mock_es,
            driver_ws_manager=mock_driver_ws,
        )

    def test_configure_pod_endpoints_accepts_driver_ws_manager(self):
        """configure_pod_endpoints should accept driver_ws_manager kwarg."""
        from driver.api.pod_endpoints import configure_pod_endpoints
        from driver.ws.driver_ws_manager import DriverWSManager

        mock_es = MagicMock()
        mock_driver_ws = DriverWSManager()

        # Should not raise
        configure_pod_endpoints(
            es_service=mock_es,
            driver_ws_manager=mock_driver_ws,
        )
