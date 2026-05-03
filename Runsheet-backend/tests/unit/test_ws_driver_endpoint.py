"""
Unit tests for the /ws/driver WebSocket endpoint and related auth helpers.

Tests the _ws_authenticate_driver function and the /ws/driver endpoint
in main.py, as well as the DriverWSManager bootstrap wiring.

Validates: Requirements 9.1, 9.2
"""
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt_token(payload: dict, secret: str = "test-secret", algorithm: str = "HS256") -> str:
    """Create a JWT token for testing."""
    from jose import jwt as jose_jwt
    return jose_jwt.encode(payload, secret, algorithm=algorithm)


def _make_expired_jwt_token(payload: dict, secret: str = "test-secret") -> str:
    """Create an expired JWT token for testing."""
    from jose import jwt as jose_jwt
    payload["exp"] = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    return jose_jwt.encode(payload, secret, algorithm="HS256")


def _make_mock_settings(environment="development", jwt_secret="test-secret", jwt_algorithm="HS256"):
    """Create a mock Settings object."""
    settings = MagicMock()
    settings.environment = MagicMock()
    settings.environment.value = environment
    settings.jwt_secret = jwt_secret
    settings.jwt_algorithm = jwt_algorithm
    return settings


# ---------------------------------------------------------------------------
# Tests: _ws_authenticate_driver
# ---------------------------------------------------------------------------


class TestWsAuthenticateDriver:
    """Tests for the _ws_authenticate_driver helper. Validates: Req 9.1, 9.2"""

    def test_no_token_dev_mode_returns_defaults(self):
        """In development mode, missing token returns dev defaults."""
        from main import _ws_authenticate_driver

        ws = MagicMock()
        ws.query_params = {"token": ""}

        with patch("config.settings.get_settings", return_value=_make_mock_settings("development")):
            result = _ws_authenticate_driver(ws)

        assert result == ("dev-tenant", "dev-driver")

    def test_no_token_production_returns_none(self):
        """In production mode, missing token returns None."""
        from main import _ws_authenticate_driver

        ws = MagicMock()
        ws.query_params = {"token": ""}

        with patch("config.settings.get_settings", return_value=_make_mock_settings("production")):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_valid_token_returns_tenant_and_driver(self):
        """A valid JWT with tenant_id and driver_id returns both."""
        from main import _ws_authenticate_driver

        token = _make_jwt_token({"tenant_id": "t-1", "driver_id": "d-1"})
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result == ("t-1", "d-1")

    def test_valid_token_missing_driver_id_returns_none(self):
        """A JWT with tenant_id but no driver_id returns None."""
        from main import _ws_authenticate_driver

        token = _make_jwt_token({"tenant_id": "t-1"})
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_valid_token_missing_tenant_id_returns_none(self):
        """A JWT with driver_id but no tenant_id returns None."""
        from main import _ws_authenticate_driver

        token = _make_jwt_token({"driver_id": "d-1"})
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_expired_token_returns_none(self):
        """An expired JWT returns None."""
        from main import _ws_authenticate_driver

        token = _make_expired_jwt_token({"tenant_id": "t-1", "driver_id": "d-1"})
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_invalid_token_returns_none(self):
        """A malformed JWT returns None."""
        from main import _ws_authenticate_driver

        ws = MagicMock()
        ws.query_params = {"token": "not-a-valid-jwt"}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_wrong_secret_returns_none(self):
        """A JWT signed with a different secret returns None."""
        from main import _ws_authenticate_driver

        token = _make_jwt_token(
            {"tenant_id": "t-1", "driver_id": "d-1"},
            secret="different-secret",
        )
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_empty_tenant_id_returns_none(self):
        """A JWT with empty string tenant_id returns None."""
        from main import _ws_authenticate_driver

        token = _make_jwt_token({"tenant_id": "", "driver_id": "d-1"})
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

        assert result is None

    def test_empty_driver_id_returns_none(self):
        """A JWT with empty string driver_id returns None."""
        from main import _ws_authenticate_driver

        token = _make_jwt_token({"tenant_id": "t-1", "driver_id": ""})
        ws = MagicMock()
        ws.query_params = {"token": token}

        with patch("config.settings.get_settings", return_value=_make_mock_settings()):
            result = _ws_authenticate_driver(ws)

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
