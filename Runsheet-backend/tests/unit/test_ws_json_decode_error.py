"""
Unit tests for JSON decode error handling in WebSocket handlers.

Verifies that /ws/agent-activity and /api/fleet/live send an error frame
and log a WARNING when they receive malformed (non-JSON) input, instead
of silently swallowing the JSONDecodeError.

Requirements: 2.9
"""
import time
import logging
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from jose import jwt as jose_jwt
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JWT_SECRET = "dev-jwt-secret-change-me-in-production"
JWT_ALGORITHM = "HS256"


def _make_jwt(tenant_id: str) -> str:
    """Create a signed JWT token for testing."""
    payload = {
        "tenant_id": tenant_id,
        "sub": "test-user",
        "user_id": "test-user",
    }
    return jose_jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Mock WebSocket Manager
# ---------------------------------------------------------------------------
class MockWSManager:
    """Minimal mock WebSocket manager that accepts all connections."""

    def __init__(self):
        self._clients = set()

    async def connect(self, websocket, **kwargs):
        await websocket.accept()
        self._clients.add(websocket)

    async def disconnect(self, websocket):
        self._clients.discard(websocket)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def test_client():
    """Create a FastAPI TestClient with mocked container for WebSocket tests."""
    mock_es = MagicMock()
    mock_es.get_all_documents = AsyncMock(return_value=[])
    mock_es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}},
    })

    with patch("services.elasticsearch_service.elasticsearch_service", mock_es), \
         patch("data_endpoints.elasticsearch_service", mock_es):

        from main import app
        from bootstrap.container import ServiceContainer

        container = ServiceContainer()
        container.settings = MagicMock()
        container.settings.jwt_secret = JWT_SECRET
        container.settings.jwt_algorithm = JWT_ALGORITHM

        container.ops_ws_manager = MockWSManager()
        container.scheduling_ws_manager = MockWSManager()
        container.agent_ws_manager = MockWSManager()
        container.fleet_ws_manager = MockWSManager()

        app.state.container = container

        client = TestClient(app, raise_server_exceptions=False)
        yield client


# ---------------------------------------------------------------------------
# Tests: /ws/agent-activity JSON decode error handling
# ---------------------------------------------------------------------------
class TestAgentActivityJsonDecode:
    """Tests for JSON decode error handling on /ws/agent-activity."""

    def test_malformed_json_returns_error_frame(self, test_client):
        """Sending non-JSON text should return an error frame, not be silently ignored."""
        token = _make_jwt("tenant-abc")
        with test_client.websocket_connect(f"/ws/agent-activity?token={token}") as ws:
            ws.send_text("this is not json")
            response = ws.receive_json(mode="text")
            assert response["type"] == "error"
            assert response["message"] == "Invalid JSON"

    def test_malformed_json_logs_warning(self, test_client):
        """Malformed JSON should be logged at WARNING level."""
        token = _make_jwt("tenant-abc")
        with patch("main.logger") as mock_logger:
            with test_client.websocket_connect(f"/ws/agent-activity?token={token}") as ws:
                ws.send_text("{broken json")
                ws.receive_json(mode="text")  # consume the error response

            mock_logger.warning.assert_called()
            warning_args = mock_logger.warning.call_args
            assert "/ws/agent-activity" in warning_args[0][0]

    def test_valid_json_still_works(self, test_client):
        """Valid JSON ping should still get a pong response."""
        token = _make_jwt("tenant-abc")
        with test_client.websocket_connect(f"/ws/agent-activity?token={token}") as ws:
            ws.send_json({"type": "ping"})
            response = ws.receive_json(mode="text")
            assert response["type"] == "pong"
            assert "timestamp" in response


# ---------------------------------------------------------------------------
# Tests: /api/fleet/live JSON decode error handling
# ---------------------------------------------------------------------------
class TestFleetLiveJsonDecode:
    """Tests for JSON decode error handling on /api/fleet/live."""

    def test_malformed_json_returns_error_frame(self, test_client):
        """Sending non-JSON text should return an error frame, not be silently ignored."""
        token = _make_jwt("tenant-xyz")
        with test_client.websocket_connect(f"/api/fleet/live?token={token}") as ws:
            ws.send_text("not valid json at all")
            response = ws.receive_json(mode="text")
            assert response["type"] == "error"
            assert response["message"] == "Invalid JSON"

    def test_malformed_json_logs_warning(self, test_client):
        """Malformed JSON should be logged at WARNING level."""
        token = _make_jwt("tenant-xyz")
        with patch("main.logger") as mock_logger:
            with test_client.websocket_connect(f"/api/fleet/live?token={token}") as ws:
                ws.send_text("<<<not json>>>")
                ws.receive_json(mode="text")  # consume the error response

            mock_logger.warning.assert_called()
            warning_args = mock_logger.warning.call_args
            assert "/api/fleet/live" in warning_args[0][0]

    def test_valid_json_ping_still_works(self, test_client):
        """Valid JSON ping should still get a pong response."""
        token = _make_jwt("tenant-xyz")
        with test_client.websocket_connect(f"/api/fleet/live?token={token}") as ws:
            ws.send_json({"type": "ping"})
            response = ws.receive_json(mode="text")
            assert response["type"] == "pong"
            assert "timestamp" in response

    def test_valid_json_subscribe_still_works(self, test_client):
        """Valid JSON subscribe should still get a subscribed response."""
        token = _make_jwt("tenant-xyz")
        with test_client.websocket_connect(f"/api/fleet/live?token={token}") as ws:
            ws.send_json({"type": "subscribe"})
            response = ws.receive_json(mode="text")
            assert response["type"] == "subscribed"
