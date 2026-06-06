"""
Bug Condition Exploration Property Tests — Production Readiness Hardening.

These tests encode the EXPECTED correct behavior for the production-readiness
defects identified in the bugfix spec.

Auth model after the SuperTokens hard cutover
---------------------------------------------
The homegrown HS256 JWT path was removed. HTTP endpoints are reached via the
Test_Auth_Path dependency-override seam (``tests/support/auth_seam.py``):
``install_test_auth(app)`` makes ``get_tenant_context`` yield a verified
``TenantContext`` derived from ``X-Test-*`` headers, and ``auth_headers(...)``
builds those headers (carrying tenant_id / roles / pii). WebSocket handshakes
are authenticated against a verified SuperTokens session via the WS
session-verifier seam (``bootstrap.websockets.configure_ws_session_verifier``);
an absent/invalid session yields a 4001 close.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 2.19, 2.20, 2.21, 2.22**

Bug categories covered:
1. Tenant Isolation on Data Endpoints (bug 1.3)
2. Tenant Spoofing on Agent Endpoints (bug 1.4)
3. Admin Privilege Escalation (bug 1.5)
4. WebSocket No-Auth Rejection (bug 1.1)
5. WebSocket Invalid Session (bug 1.2)
6. Error Envelope Consistency (bugs 1.20, 1.22)
7. WebSocket Exception Logging (bug 1.7)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis.strategies import from_regex

import bootstrap.websockets as ws
from tests.support.auth_seam import auth_headers, install_test_auth


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


def _ws_session(claims):
    """Return an async WS session verifier yielding ``claims`` (or None)."""

    async def _verify(access_token, anti_csrf):
        return claims

    return _verify


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
        # so that WebSocket handlers can access them via _container(app)
        container = ServiceContainer()
        container.settings = MagicMock()

        # Create mock WS managers that accept all connections
        container.ops_ws_manager = MockWSManager()
        container.scheduling_ws_manager = MockWSManager()
        container.agent_ws_manager = MockWSManager()
        container.fleet_ws_manager = MockWSManager()

        app.state.container = container

        # HTTP endpoints authenticate via the Test_Auth_Path seam.
        install_test_auth(app)

        from starlette.testclient import TestClient
        client = TestClient(app, raise_server_exceptions=False)
        try:
            yield {
                "client": client,
                "app": app,
                "mock_es": mock_es,
                "mock_approval_svc": mock_approval_svc,
                "mock_activity_svc": mock_activity_svc,
                "mock_autonomy_svc": mock_autonomy_svc,
                "container": container,
            }
        finally:
            app.dependency_overrides.clear()
            ws.configure_ws_session_verifier(None)


# ===========================================================================
# 1. Tenant Isolation on Data Endpoints (Bug 1.3)
# ===========================================================================
class TestTenantIsolationDataEndpoints:
    """
    **Validates: Requirements 2.3**

    For any tenant_id, calling GET /api/fleet/trucks while authenticated for
    that tenant should result in an ES query that includes a bool.filter
    clause matching tenant_id.
    """

    @given(tid=tenant_id_strategy)
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_fleet_trucks_includes_tenant_filter(self, tid: str, test_app):
        """
        Property: For all tenant_ids, GET /api/fleet/trucks for an authenticated
        tenant must produce an ES query containing a tenant_id filter.

        The fleet reads can be served from the Postgres source-of-truth (the
        read-cutover path) in environments where it is enabled; this test
        targets the Elasticsearch query shape, so it forces the ES read path by
        pinning ``read_from_postgres`` to False.
        """
        client = test_app["client"]
        mock_es = test_app["mock_es"]

        mock_es.search_documents.reset_mock()
        mock_es.search_documents.return_value = {
            "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}},
        }

        with patch(
            "commerce.services.commerce_persistence_bridge.read_from_postgres",
            return_value=False,
        ):
            resp = client.get("/api/fleet/trucks", headers=auth_headers(tid))

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

    Calling GET /api/agent/approvals?tenant_id=victim while authenticated for
    tenant_id=attacker should result in the service receiving 'attacker' (from
    the verified context), not 'victim' (from the query param).
    """

    @given(attacker_tid=tenant_id_strategy, victim_tid=tenant_id_strategy)
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_agent_approvals_uses_context_tenant_not_query_param(
        self, attacker_tid: str, victim_tid: str, test_app
    ):
        """
        Property: For all attacker/victim tenant_id pairs, the service must
        receive the verified-context tenant_id, not the query param tenant_id.
        """
        assume(attacker_tid != victim_tid)

        client = test_app["client"]
        mock_svc = test_app["mock_approval_svc"]
        mock_svc.list_pending.reset_mock()

        resp = client.get(
            f"/api/agent/approvals?tenant_id={victim_tid}",
            headers=auth_headers(attacker_tid),
        )

        assert mock_svc.list_pending.called, "list_pending was not called"

        call_kwargs = mock_svc.list_pending.call_args
        all_args_str = str(call_kwargs)

        assert attacker_tid in all_args_str, (
            f"Service was not called with the verified tenant_id '{attacker_tid}'. "
            f"Call args: {all_args_str}"
        )
        assert victim_tid not in all_args_str, (
            f"Service was called with spoofed query param tenant_id '{victim_tid}' "
            f"instead of the verified tenant_id '{attacker_tid}'. Call args: {all_args_str}"
        )


# ===========================================================================
# 3. Admin Privilege Escalation (Bug 1.5)
# ===========================================================================
class TestAdminPrivilegeEscalation:
    """
    **Validates: Requirements 2.5**

    Calling PATCH /api/agent/config/autonomy with header x-user-role: admin
    but a verified context WITHOUT the admin role should return 403 Forbidden.
    The admin check must trust the verified roles, not the spoofable header.
    """

    @given(tid=tenant_id_strategy)
    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_admin_escalation_via_header_rejected(self, tid: str, test_app):
        """
        Property: For all tenant_ids, a non-admin verified context with
        x-user-role: admin header must be rejected with 403.
        """
        client = test_app["client"]

        headers = auth_headers(tid, roles=["viewer"])
        headers["x-user-role"] = "admin"
        headers["Content-Type"] = "application/json"
        resp = client.patch(
            "/api/agent/config/autonomy",
            headers=headers,
            json={"level": "auto-low"},
        )

        assert resp.status_code == 403, (
            f"Expected 403 Forbidden for non-admin context with x-user-role header, "
            f"got {resp.status_code}. The admin check must trust the verified roles "
            f"instead of the header (bug 1.5). Response: {resp.text}"
        )


# ===========================================================================
# 4. WebSocket No-Auth Rejection (Bug 1.1)
# ===========================================================================
class TestWebSocketNoAuthRejection:
    """
    **Validates: Requirements 2.1**

    Connecting to /ws/scheduling, /ws/agent-activity, /api/fleet/live without
    a verifiable session should result in connection rejection with close code
    4001.
    """

    @pytest.mark.parametrize("ws_path", [
        "/ws/scheduling",
        "/ws/agent-activity",
        "/api/fleet/live",
    ])
    def test_websocket_rejects_unauthenticated_connection(self, ws_path, test_app):
        """
        For each WebSocket endpoint, connecting without a verifiable session
        must be rejected with close code 4001.
        """
        client = test_app["client"]

        # No session present on the handshake: the verifier yields None.
        ws.configure_ws_session_verifier(_ws_session(None))

        connection_accepted = False
        try:
            with client.websocket_connect(ws_path):
                connection_accepted = True
        except Exception:
            connection_accepted = False

        assert not connection_accepted, (
            f"WebSocket connection to {ws_path} was ACCEPTED without a verified "
            f"session. Expected rejection with close code 4001 (bug 1.1)."
        )


# ===========================================================================
# 5. WebSocket Invalid Session (Bug 1.2)
# ===========================================================================
class TestWebSocketInvalidSession:
    """
    **Validates: Requirements 2.2**

    Connecting to /ws/ops with an unverifiable session credential should result
    in connection rejection with close code 4001 (no silent accept with an
    empty tenant_id).
    """

    def test_ws_ops_rejects_unverifiable_session(self, test_app):
        """
        Connecting to /ws/ops with a credential that fails verification must be
        rejected.
        """
        client = test_app["client"]
        # An unverifiable credential: the verifier reports no session.
        ws.configure_ws_session_verifier(_ws_session(None))

        connection_accepted = False
        try:
            with client.websocket_connect("/ws/ops?token=unverifiable-token"):
                connection_accepted = True
        except Exception:
            connection_accepted = False

        assert not connection_accepted, (
            "WebSocket connection to /ws/ops was ACCEPTED with an unverifiable "
            "session. Expected rejection with close code 4001 (bug 1.2)."
        )

    def test_ws_ops_rejects_session_without_tenant(self, test_app):
        """
        A verified session whose claims lack a tenant_id must be rejected (no
        silent accept with an empty tenant_id).
        """
        client = test_app["client"]
        ws.configure_ws_session_verifier(_ws_session({"roles": ["admin"]}))

        connection_accepted = False
        try:
            with client.websocket_connect("/ws/ops?token=session-without-tenant"):
                connection_accepted = True
        except Exception:
            connection_accepted = False

        assert not connection_accepted, (
            "WebSocket connection to /ws/ops was ACCEPTED with a session lacking "
            "a tenant_id claim. Expected rejection with close code 4001 (bug 1.2)."
        )


# ===========================================================================
# 6. Error Envelope Consistency (Bugs 1.20, 1.22)
# ===========================================================================
class TestErrorEnvelopeConsistency:
    """
    **Validates: Requirements 2.19, 2.20, 2.21, 2.22**

    Error responses from data_endpoints and agent_endpoints must contain
    structured error envelopes with error_code, message, and request_id fields.
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

        # Force the ES read path (reads may otherwise be served from Postgres).
        with patch(
            "commerce.services.commerce_persistence_bridge.read_from_postgres",
            return_value=False,
        ):
            resp = client.get("/api/fleet/summary", headers=auth_headers("test-tenant"))

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

        resp = client.get(
            "/api/fleet/trucks/nonexistent-truck-id",
            headers=auth_headers("test-tenant"),
        )

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

    When an unexpected exception occurs in a WebSocket handler loop, it must be
    logged at ERROR level with structured context.
    """

    def test_ws_ops_exception_is_logged(self, test_app):
        """
        Simulating an exception in the /ws/ops handler loop must result in an
        ERROR-level log entry with structured context.
        """
        client = test_app["client"]

        # Authenticate the handshake with a verified session carrying a tenant.
        ws.configure_ws_session_verifier(_ws_session({"tenant_id": "test-tenant"}))

        # Patch the ops_ws_manager to raise an exception during message handling
        container = test_app["container"]
        original_handler = container.ops_ws_manager.handle_client_message

        async def raise_on_message(ws_conn, raw):
            raise RuntimeError("Simulated ES timeout in WebSocket handler")

        container.ops_ws_manager.handle_client_message = raise_on_message

        with patch("main.logger") as mock_logger:
            try:
                with client.websocket_connect("/ws/ops?token=session-token") as conn:
                    # Send a message that triggers the exception in the handler.
                    conn.send_text('{"type": "subscribe", "channel": "test"}')
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
