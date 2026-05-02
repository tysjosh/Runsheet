"""
Bug Condition Exploration Property Tests — Production Readiness Hardening.

These tests encode the EXPECTED correct behavior for the production-readiness
defects identified in the bugfix spec. They are designed to FAIL on unfixed
code, confirming the bugs exist. Once the fixes are applied, these tests
should PASS.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 2.19, 2.20, 2.21, 2.22**

Bug categories covered:
1. Tenant Isolation on Data Endpoints (bug 1.3)
2. Tenant Spoofing on Agent Endpoints (bug 1.4)
3. Admin Privilege Escalation (bug 1.5)
4. WebSocket No-Auth Rejection (bug 1.1)
5. WebSocket Ops Invalid JWT (bug 1.2)
6. Error Envelope Consistency (bugs 1.20, 1.22)
7. WebSocket Exception Logging (bug 1.7)
"""

import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis.strategies import from_regex

from jose import jwt as jose_jwt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JWT_SECRET = "dev-jwt-secret-change-me-in-production"
JWT_ALGORITHM = "HS256"


def _make_jwt(tenant_id: str, roles: list = None, expired: bool = False) -> str:
    """Create a signed JWT token for testing."""
    payload = {
        "tenant_id": tenant_id,
        "sub": "test-user",
        "user_id": "test-user",
        "has_pii_access": False,
    }
    if roles is not None:
        payload["roles"] = roles
    if expired:
        payload["exp"] = int(time.time()) - 3600  # expired 1 hour ago
    return jose_jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
tenant_id_strategy = from_regex(r"[a-zA-Z][a-zA-Z0-9_\-]{2,30}", fullmatch=True)


# ---------------------------------------------------------------------------
# Mock WebSocket Manager
# ---------------------------------------------------------------------------
class MockWSManager:
    """A minimal mock WebSocket manager that accepts all connections."""

    def __init__(self):
        self._clients = set()

    async def connect(self, websocket, **kwargs):
        await websocket.accept()
        self._clients.add(websocket)

    async def disconnect(self, websocket):
        self._clients.discard(websocket)

    async def handle_client_message(self, websocket, raw):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def test_app():
    """Create a FastAPI TestClient with mocked services and container."""
    # Mock the elasticsearch_service before importing main
    mock_es = MagicMock()
    mock_es.get_all_documents = AsyncMock(return_value=[])
    mock_es.search_documents = AsyncMock(return_value={
        "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}},
        "aggregations": {
            "by_type": {"buckets": []},
            "by_subtype": {"buckets": []},
            "active_count": {"doc_count": 0},
            "delayed_count": {"doc_count": 0},
        },
    })
    mock_es.get_document = AsyncMock(side_effect=Exception("Document not found"))

    # Mock services needed by agent_endpoints
    mock_approval_svc = MagicMock()
    mock_approval_svc.list_pending = AsyncMock(return_value={
        "data": [],
        "pagination": {"total": 0, "page": 1, "size": 20},
    })

    mock_activity_svc = MagicMock()
    mock_activity_svc.query = AsyncMock(return_value={
        "data": [],
        "pagination": {"total": 0, "page": 1, "size": 50},
    })
    mock_activity_svc.get_stats = AsyncMock(return_value={})
    mock_activity_svc.log = AsyncMock(return_value=None)

    mock_autonomy_svc = MagicMock()
    mock_autonomy_svc.get_level = AsyncMock(return_value="suggest-only")
    mock_autonomy_svc.set_level = AsyncMock(return_value="suggest-only")

    mock_memory_svc = MagicMock()
    mock_memory_svc.list_memories = AsyncMock(return_value={
        "data": [],
        "pagination": {"total": 0, "page": 1, "size": 20},
    })

    mock_feedback_svc = MagicMock()
    mock_feedback_svc.list_feedback = AsyncMock(return_value={
        "data": [],
        "pagination": {"total": 0, "page": 1, "size": 20},
    })
    mock_feedback_svc.get_stats = AsyncMock(return_value={})

    with patch("services.elasticsearch_service.elasticsearch_service", mock_es), \
         patch("data_endpoints.elasticsearch_service", mock_es):

        from main import app
        from agent_endpoints import configure_agent_endpoints
        from bootstrap.container import ServiceContainer

        configure_agent_endpoints(
            approval_queue_service=mock_approval_svc,
            activity_log_service=mock_activity_svc,
            autonomy_config_service=mock_autonomy_svc,
            memory_service=mock_memory_svc,
            feedback_service=mock_feedback_svc,
        )

        # Set up a minimal container with mock WebSocket managers
        # so that WebSocket handlers can access them via _c(app)
        container = ServiceContainer()
        container.settings = MagicMock()
        container.settings.jwt_secret = JWT_SECRET
        container.settings.jwt_algorithm = JWT_ALGORITHM

        # Create mock WS managers that accept all connections (simulating unfixed code)
        container.ops_ws_manager = MockWSManager()
        container.scheduling_ws_manager = MockWSManager()
        container.agent_ws_manager = MockWSManager()
        container.fleet_ws_manager = MockWSManager()

        app.state.container = container

        from starlette.testclient import TestClient
        client = TestClient(app, raise_server_exceptions=False)
        yield {
            "client": client,
            "app": app,
            "mock_es": mock_es,
            "mock_approval_svc": mock_approval_svc,
            "mock_activity_svc": mock_activity_svc,
            "mock_autonomy_svc": mock_autonomy_svc,
            "container": container,
        }


# ===========================================================================
# 1. Tenant Isolation on Data Endpoints (Bug 1.3)
# ===========================================================================
class TestTenantIsolationDataEndpoints:
    """
    **Validates: Requirements 2.3**

    For any tenant_id, calling GET /api/fleet/trucks with a JWT containing
    that tenant_id should result in an ES query that includes a bool.filter
    clause matching tenant_id. On unfixed code, the query has NO tenant
    filter → FAIL (confirms bug 1.3).
    """

    @given(tid=tenant_id_strategy)
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_fleet_trucks_includes_tenant_filter(self, tid: str, test_app):
        """
        Property: For all tenant_ids, GET /api/fleet/trucks with a valid JWT
        must produce an ES query containing a tenant_id filter.
        """
        client = test_app["client"]
        mock_es = test_app["mock_es"]

        mock_es.search_documents.reset_mock()
        mock_es.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}},
        }

        token = _make_jwt(tid)
        resp = client.get(
            "/api/fleet/trucks",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert mock_es.search_documents.called, "ES search_documents was not called"

        call_args = mock_es.search_documents.call_args
        query_body = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("body", {})

        query_str = json.dumps(query_body)
        assert "tenant_id" in query_str, (
            f"ES query for tenant '{tid}' does not contain a tenant_id filter. "
            f"Query: {query_str}"
        )

        bool_query = query_body.get("query", {}).get("bool", {})
        filter_clauses = bool_query.get("filter", [])
        if isinstance(filter_clauses, dict):
            filter_clauses = [filter_clauses]

        tenant_filter_found = any(
            clause.get("term", {}).get("tenant_id") == tid
            for clause in filter_clauses
            if isinstance(clause, dict)
        )
        assert tenant_filter_found, (
            f"ES query bool.filter does not contain term filter for tenant_id='{tid}'. "
            f"Filter clauses: {filter_clauses}"
        )


# ===========================================================================
# 2. Tenant Spoofing on Agent Endpoints (Bug 1.4)
# ===========================================================================
class TestTenantSpoofingAgentEndpoints:
    """
    **Validates: Requirements 2.4**

    Calling GET /api/agent/approvals?tenant_id=victim with a JWT for
    tenant_id=attacker should result in the service receiving 'attacker'
    (from JWT), not 'victim' (from query param). On unfixed code, the
    query param is used → FAIL (confirms bug 1.4).
    """

    @given(attacker_tid=tenant_id_strategy, victim_tid=tenant_id_strategy)
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_agent_approvals_uses_jwt_tenant_not_query_param(
        self, attacker_tid: str, victim_tid: str, test_app
    ):
        """
        Property: For all attacker/victim tenant_id pairs, the service
        must receive the JWT tenant_id, not the query param tenant_id.
        """
        assume(attacker_tid != victim_tid)

        client = test_app["client"]
        mock_svc = test_app["mock_approval_svc"]
        mock_svc.list_pending.reset_mock()

        token = _make_jwt(attacker_tid)
        resp = client.get(
            f"/api/agent/approvals?tenant_id={victim_tid}",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert mock_svc.list_pending.called, "list_pending was not called"

        call_kwargs = mock_svc.list_pending.call_args
        all_args_str = str(call_kwargs)

        assert attacker_tid in all_args_str, (
            f"Service was not called with JWT tenant_id '{attacker_tid}'. "
            f"Call args: {all_args_str}"
        )
        assert victim_tid not in all_args_str, (
            f"Service was called with spoofed query param tenant_id '{victim_tid}' "
            f"instead of JWT tenant_id '{attacker_tid}'. Call args: {all_args_str}"
        )


# ===========================================================================
# 3. Admin Privilege Escalation (Bug 1.5)
# ===========================================================================
class TestAdminPrivilegeEscalation:
    """
    **Validates: Requirements 2.5**

    Calling PATCH /api/agent/config/autonomy with header x-user-role: admin
    but a JWT without admin role should return 403 Forbidden. On unfixed
    code, the header is trusted → FAIL (confirms bug 1.5).
    """

    @given(tid=tenant_id_strategy)
    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_admin_escalation_via_header_rejected(self, tid: str, test_app):
        """
        Property: For all tenant_ids, a non-admin JWT with x-user-role: admin
        header must be rejected with 403.
        """
        client = test_app["client"]

        token = _make_jwt(tid, roles=["viewer"])
        resp = client.patch(
            "/api/agent/config/autonomy",
            headers={
                "Authorization": f"Bearer {token}",
                "x-user-role": "admin",
                "Content-Type": "application/json",
            },
            json={"level": "auto-low"},
        )

        assert resp.status_code == 403, (
            f"Expected 403 Forbidden for non-admin JWT with x-user-role header, "
            f"got {resp.status_code}. The admin check trusts the header instead "
            f"of JWT claims (bug 1.5). Response: {resp.text}"
        )


# ===========================================================================
# 4. WebSocket No-Auth Rejection (Bug 1.1)
# ===========================================================================
class TestWebSocketNoAuthRejection:
    """
    **Validates: Requirements 2.1**

    Connecting to /ws/scheduling, /ws/agent-activity, /api/fleet/live
    without a JWT token should result in connection rejection with close
    code 4001. On unfixed code, connection is accepted → FAIL (confirms
    bug 1.1).
    """

    @pytest.mark.parametrize("ws_path", [
        "/ws/scheduling",
        "/ws/agent-activity",
        "/api/fleet/live",
    ])
    def test_websocket_rejects_unauthenticated_connection(self, ws_path, test_app):
        """
        For each WebSocket endpoint, connecting without a JWT must be
        rejected with close code 4001 in non-development environments.
        """
        client = test_app["client"]

        # Simulate a non-development environment so the dev-tenant fallback
        # is not active (matching production behaviour).
        mock_settings = MagicMock()
        mock_settings.environment.value = "production"
        mock_settings.jwt_secret = JWT_SECRET
        mock_settings.jwt_algorithm = JWT_ALGORITHM

        with patch("config.settings.get_settings", return_value=mock_settings):
            # Attempt WebSocket connection without any token.
            connection_accepted = False
            try:
                with client.websocket_connect(ws_path) as ws:
                    connection_accepted = True
            except Exception:
                connection_accepted = False

            assert not connection_accepted, (
                f"WebSocket connection to {ws_path} was ACCEPTED without JWT auth. "
                f"Expected rejection with close code 4001 (bug 1.1). "
                f"The handler has no authentication check."
            )


# ===========================================================================
# 5. WebSocket Ops Invalid JWT (Bug 1.2)
# ===========================================================================
class TestWebSocketOpsInvalidJWT:
    """
    **Validates: Requirements 2.2**

    Connecting to /ws/ops with an expired/invalid JWT should result in
    connection rejection with close code 4001. On unfixed code, the
    connection is silently accepted with empty tenant_id → FAIL (confirms
    bug 1.2).
    """

    def test_ws_ops_rejects_expired_jwt(self, test_app):
        """
        Connecting to /ws/ops with an expired JWT must be rejected.
        """
        client = test_app["client"]
        expired_token = _make_jwt("some-tenant", expired=True)

        connection_accepted = False
        try:
            with client.websocket_connect(f"/ws/ops?token={expired_token}") as ws:
                connection_accepted = True
        except Exception:
            connection_accepted = False

        assert not connection_accepted, (
            "WebSocket connection to /ws/ops was ACCEPTED with an expired JWT. "
            "Expected rejection with close code 4001 (bug 1.2). "
            "The handler silently falls through JWTError with pass."
        )

    def test_ws_ops_rejects_invalid_jwt(self, test_app):
        """
        Connecting to /ws/ops with a completely invalid JWT must be rejected.
        """
        client = test_app["client"]

        connection_accepted = False
        try:
            with client.websocket_connect("/ws/ops?token=not-a-valid-jwt") as ws:
                connection_accepted = True
        except Exception:
            connection_accepted = False

        assert not connection_accepted, (
            "WebSocket connection to /ws/ops was ACCEPTED with an invalid JWT. "
            "Expected rejection with close code 4001 (bug 1.2). "
            "The handler silently falls through JWTError with pass."
        )


# ===========================================================================
# 6. Error Envelope Consistency (Bugs 1.20, 1.22)
# ===========================================================================
class TestErrorEnvelopeConsistency:
    """
    **Validates: Requirements 2.19, 2.20, 2.21, 2.22**

    Error responses from data_endpoints and agent_endpoints must contain
    structured error envelopes with error_code, message, and request_id
    fields. On unfixed code, returns plain {"detail": "..."} → FAIL
    (confirms bugs 1.20, 1.22).
    """

    def test_fleet_summary_500_has_structured_envelope(self, test_app):
        """
        Triggering a 500 on GET /api/fleet/summary must return a structured
        error envelope with error_code, message, and request_id.
        """
        client = test_app["client"]
        mock_es = test_app["mock_es"]

        # The refactored endpoint uses search_documents (tenant-scoped) instead of get_all_documents
        mock_es.search_documents.side_effect = Exception("ConnectionTimeout: simulated ES failure")

        token = _make_jwt("test-tenant")
        resp = client.get(
            "/api/fleet/summary",
            headers={"Authorization": f"Bearer {token}"},
        )

        mock_es.search_documents.side_effect = None
        # Restore the default return value for search_documents
        mock_es.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}},
            "aggregations": {
                "by_type": {"buckets": []},
                "by_subtype": {"buckets": []},
                "active_count": {"doc_count": 0},
                "delayed_count": {"doc_count": 0},
            },
        }

        body = resp.json()

        assert "error_code" in body, (
            f"500 response from /api/fleet/summary lacks 'error_code' field. "
            f"Got: {body} (bug 1.20 — plain detail string instead of structured envelope)"
        )
        assert "message" in body, (
            f"500 response from /api/fleet/summary lacks 'message' field. Got: {body}"
        )
        assert "request_id" in body, (
            f"500 response from /api/fleet/summary lacks 'request_id' field. Got: {body}"
        )

    def test_fleet_trucks_404_has_structured_envelope(self, test_app):
        """
        GET /api/fleet/trucks/nonexistent must return a structured error
        envelope with error_code, message, and request_id.
        """
        client = test_app["client"]
        mock_es = test_app["mock_es"]

        mock_es.get_document.side_effect = Exception("Document not found")

        token = _make_jwt("test-tenant")
        resp = client.get(
            "/api/fleet/trucks/nonexistent-truck-id",
            headers={"Authorization": f"Bearer {token}"},
        )

        mock_es.get_document.side_effect = Exception("Document not found")

        body = resp.json()

        assert "error_code" in body, (
            f"404 response from /api/fleet/trucks/nonexistent lacks 'error_code' field. "
            f"Got: {body} (bug 1.22 — plain detail string instead of structured envelope)"
        )
        assert "message" in body, (
            f"404 response from /api/fleet/trucks/nonexistent lacks 'message' field. Got: {body}"
        )
        assert "request_id" in body, (
            f"404 response from /api/fleet/trucks/nonexistent lacks 'request_id' field. Got: {body}"
        )


# ===========================================================================
# 7. WebSocket Exception Logging (Bug 1.7)
# ===========================================================================
class TestWebSocketExceptionLogging:
    """
    **Validates: Requirements 2.7**

    When an unexpected exception occurs in a WebSocket handler loop, it
    must be logged at ERROR level with structured context. On unfixed code,
    the exception is silently swallowed by bare `except ... pass` → FAIL
    (confirms bug 1.7).
    """

    def test_ws_ops_exception_is_logged(self, test_app):
        """
        Simulating an exception in the /ws/ops handler loop must result
        in an ERROR-level log entry with structured context.

        On unfixed code, the bare `except (WebSocketDisconnect, Exception): pass`
        swallows all exceptions without logging.
        """
        client = test_app["client"]

        # Create a valid token for connection
        token = _make_jwt("test-tenant")

        # Patch the ops_ws_manager to raise an exception during message handling
        container = test_app["container"]
        original_handler = container.ops_ws_manager.handle_client_message

        async def raise_on_message(ws, raw):
            raise RuntimeError("Simulated ES timeout in WebSocket handler")

        container.ops_ws_manager.handle_client_message = raise_on_message

        with patch("main.logger") as mock_logger:
            try:
                with client.websocket_connect(f"/ws/ops?token={token}") as ws:
                    # Send a message that triggers the exception in the handler
                    ws.send_text('{"type": "subscribe", "channel": "test"}')
                    # The handler should catch the exception and log it
                    # On unfixed code, it's caught by bare except...pass
            except Exception:
                pass

        # Restore original handler
        container.ops_ws_manager.handle_client_message = original_handler

        # Check if any ERROR-level logging occurred
        error_calls = mock_logger.error.call_args_list
        assert len(error_calls) > 0, (
            "No ERROR-level log entries found after WebSocket exception. "
            "The exception was silently swallowed by bare 'except ... pass' (bug 1.7)."
        )
