"""
Unit tests for centralized auth/tenant policy middleware.

Tests the AuthPolicy enum, policy matrix, startup validation,
auth enforcement dependency, and tenant scoping dependency.

Validates: Requirements 5.1–5.7, Correctness Property P11
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.auth_policy import (
    AuthPolicy,
    POLICY_MATRIX,
    POLICY_EXCEPTIONS,
    TenantContext,
    validate_policy_matrix,
    get_policy_for_route,
    enforce_auth_policy,
    require_tenant,
)


# ---------------------------------------------------------------------------
# AuthPolicy enum tests (Req 5.1)
# ---------------------------------------------------------------------------

class TestAuthPolicyEnum:
    """Tests for the AuthPolicy enum definition."""

    def test_enum_has_jwt_required(self):
        assert AuthPolicy.JWT_REQUIRED == "jwt_required"

    def test_enum_has_api_key_required(self):
        assert AuthPolicy.API_KEY_REQUIRED == "api_key_required"

    def test_enum_has_webhook_hmac(self):
        assert AuthPolicy.WEBHOOK_HMAC == "webhook_hmac"

    def test_enum_has_public(self):
        assert AuthPolicy.PUBLIC == "public"

    def test_enum_has_exactly_four_values(self):
        assert len(AuthPolicy) == 4

    def test_enum_values_are_strings(self):
        for policy in AuthPolicy:
            assert isinstance(policy.value, str)


# ---------------------------------------------------------------------------
# Policy matrix tests (Req 5.6)
# ---------------------------------------------------------------------------

class TestPolicyMatrix:
    """Tests for the POLICY_MATRIX and POLICY_EXCEPTIONS dictionaries."""

    def test_scheduling_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/scheduling"] == AuthPolicy.JWT_REQUIRED

    def test_ops_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/ops"] == AuthPolicy.JWT_REQUIRED

    def test_ops_admin_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/ops/admin"] == AuthPolicy.JWT_REQUIRED

    def test_fuel_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/fuel"] == AuthPolicy.JWT_REQUIRED

    def test_agent_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/agent"] == AuthPolicy.JWT_REQUIRED

    def test_chat_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/chat"] == AuthPolicy.JWT_REQUIRED

    def test_data_routes_require_jwt(self):
        assert POLICY_MATRIX["/api/data"] == AuthPolicy.JWT_REQUIRED

    def test_ws_routes_require_jwt(self):
        assert POLICY_MATRIX["/ws"] == AuthPolicy.JWT_REQUIRED

    def test_health_routes_are_public(self):
        assert POLICY_MATRIX["/health"] == AuthPolicy.PUBLIC

    def test_docs_routes_are_public(self):
        assert POLICY_MATRIX["/docs"] == AuthPolicy.PUBLIC

    def test_openapi_json_is_public(self):
        assert POLICY_MATRIX["/openapi.json"] == AuthPolicy.PUBLIC

    def test_agent_health_exception_is_public(self):
        assert POLICY_EXCEPTIONS["GET /api/agent/health"] == AuthPolicy.PUBLIC

    def test_ws_agent_activity_exception_is_public(self):
        assert POLICY_EXCEPTIONS["GET /ws/agent-activity"] == AuthPolicy.PUBLIC


# ---------------------------------------------------------------------------
# get_policy_for_route tests
# ---------------------------------------------------------------------------

class TestGetPolicyForRoute:
    """Tests for the get_policy_for_route function."""

    def test_public_health_route(self):
        assert get_policy_for_route("GET", "/health") == AuthPolicy.PUBLIC

    def test_public_docs_route(self):
        assert get_policy_for_route("GET", "/docs") == AuthPolicy.PUBLIC

    def test_jwt_required_scheduling_route(self):
        assert get_policy_for_route("GET", "/api/scheduling/jobs") == AuthPolicy.JWT_REQUIRED

    def test_jwt_required_ops_route(self):
        assert get_policy_for_route("GET", "/api/ops/shipments") == AuthPolicy.JWT_REQUIRED

    def test_jwt_required_fuel_route(self):
        assert get_policy_for_route("GET", "/api/fuel/stations") == AuthPolicy.JWT_REQUIRED

    def test_agent_health_exception_overrides_default(self):
        """GET /api/agent/health should be PUBLIC despite /api/agent being JWT_REQUIRED."""
        assert get_policy_for_route("GET", "/api/agent/health") == AuthPolicy.PUBLIC

    def test_agent_non_health_requires_jwt(self):
        assert get_policy_for_route("GET", "/api/agent/approvals") == AuthPolicy.JWT_REQUIRED

    def test_ws_agent_activity_exception(self):
        assert get_policy_for_route("GET", "/ws/agent-activity") == AuthPolicy.PUBLIC

    def test_ws_ops_requires_jwt(self):
        assert get_policy_for_route("GET", "/ws/ops") == AuthPolicy.JWT_REQUIRED

    def test_unmatched_route_defaults_to_jwt_required(self):
        assert get_policy_for_route("GET", "/unknown/route") == AuthPolicy.JWT_REQUIRED

    def test_root_route_is_public(self):
        assert get_policy_for_route("GET", "/") == AuthPolicy.PUBLIC

    def test_ops_admin_matches_before_ops(self):
        """Longer prefix /api/ops/admin should match before /api/ops."""
        policy = get_policy_for_route("GET", "/api/ops/admin/users")
        assert policy == AuthPolicy.JWT_REQUIRED


# ---------------------------------------------------------------------------
# validate_policy_matrix tests (Req 5.5, 5.7)
# ---------------------------------------------------------------------------

class TestValidatePolicyMatrix:
    """Tests for the validate_policy_matrix startup check."""

    def _make_route(self, path: str, methods: set = None):
        """Create a mock route object."""
        route = MagicMock()
        route.path = path
        route.methods = methods or {"GET"}
        return route

    def test_all_routes_matched_returns_empty(self):
        """When all routes have policy matches, no warnings are logged."""
        app = MagicMock()
        app.routes = [
            self._make_route("/health"),
            self._make_route("/api/ops/shipments"),
            self._make_route("/api/fuel/stations"),
            self._make_route("/api/scheduling/jobs"),
        ]
        unmatched = validate_policy_matrix(app)
        assert unmatched == []

    def test_unmatched_route_is_reported(self):
        """Routes without policy matches are returned in the unmatched list."""
        app = MagicMock()
        app.routes = [
            self._make_route("/health"),
            self._make_route("/some/unknown/path"),
        ]
        unmatched = validate_policy_matrix(app)
        assert "/some/unknown/path" in unmatched

    def test_logs_warning_for_unmatched_routes(self):
        """Unmatched routes should trigger a warning log."""
        app = MagicMock()
        app.routes = [
            self._make_route("/some/unknown/path"),
        ]
        with patch("middleware.auth_policy.logger") as mock_logger:
            validate_policy_matrix(app)
            mock_logger.warning.assert_called()

    def test_logs_info_when_all_matched(self):
        """When all routes match, an info log is emitted."""
        app = MagicMock()
        app.routes = [
            self._make_route("/health"),
            self._make_route("/api/ops/shipments"),
        ]
        with patch("middleware.auth_policy.logger") as mock_logger:
            validate_policy_matrix(app)
            mock_logger.info.assert_called()

    def test_empty_routes_returns_empty(self):
        """An app with no routes should return empty unmatched list."""
        app = MagicMock()
        app.routes = []
        unmatched = validate_policy_matrix(app)
        assert unmatched == []

    def test_exception_routes_are_matched(self):
        """Routes in POLICY_EXCEPTIONS should be considered matched."""
        app = MagicMock()
        app.routes = [
            self._make_route("/api/agent/health", {"GET"}),
        ]
        unmatched = validate_policy_matrix(app)
        assert unmatched == []


# ---------------------------------------------------------------------------
# Auth enforcement tests (Req 5.3)
# ---------------------------------------------------------------------------

class TestAuthEnforcement:
    """Tests for auth enforcement using a real FastAPI test client."""

    @pytest.fixture
    def app(self):
        """Create a FastAPI app with auth-enforced endpoints."""
        from middleware.request_id import RequestIDMiddleware

        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/api/ops/test")
        async def ops_test(payload=pytest.importorskip("fastapi").Depends(enforce_auth_policy)):
            return {"status": "authenticated", "payload": payload}

        @app.get("/api/agent/health")
        async def agent_health(payload=pytest.importorskip("fastapi").Depends(enforce_auth_policy)):
            return {"status": "ok"}

        return app

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_public_route_allows_unauthenticated(self, client):
        """Unauthenticated request to PUBLIC route succeeds."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_agent_health_public_exception_allows_unauthenticated(self, client):
        """GET /api/agent/health is a PUBLIC exception — no auth needed."""
        response = client.get("/api/agent/health")
        assert response.status_code == 200

    def test_jwt_required_route_rejects_unauthenticated(self, client):
        """Unauthenticated request to JWT_REQUIRED route returns 401."""
        response = client.get("/api/ops/test")
        assert response.status_code == 401

    def test_jwt_required_route_rejects_invalid_bearer(self, client):
        """Invalid Bearer token returns 401."""
        response = client.get(
            "/api/ops/test",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401

    def test_jwt_required_route_rejects_missing_bearer_prefix(self, client):
        """Authorization header without 'Bearer ' prefix returns 401."""
        response = client.get(
            "/api/ops/test",
            headers={"Authorization": "some-token"},
        )
        assert response.status_code == 401

    def test_jwt_required_401_returns_error_response_shape(self, client):
        """401 response conforms to ErrorResponse schema."""
        response = client.get("/api/ops/test")
        assert response.status_code == 401
        data = response.json()["detail"]
        assert "error_code" in data
        assert "message" in data
        assert "request_id" in data

    @patch("config.settings.get_settings")
    def test_jwt_required_route_accepts_valid_token(self, mock_get_settings, client):
        """Authenticated request to JWT_REQUIRED route succeeds with valid JWT."""
        from jose import jwt as jose_jwt

        mock_get_settings.return_value = MagicMock(
            jwt_secret="test-secret",
            jwt_algorithm="HS256",
        )

        token = jose_jwt.encode(
            {"sub": "user-1", "tenant_id": "tenant-1", "roles": ["admin"]},
            "test-secret",
            algorithm="HS256",
        )

        response = client.get(
            "/api/ops/test",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tenant scoping tests (Req 5.4)
# ---------------------------------------------------------------------------

class TestTenantScoping:
    """Tests for the require_tenant dependency."""

    @pytest.fixture
    def app(self):
        """Create a FastAPI app with tenant-scoped endpoint."""
        from fastapi import Depends
        from middleware.request_id import RequestIDMiddleware

        app = FastAPI()
        app.add_middleware(RequestIDMiddleware)

        @app.get("/api/tenant-test")
        async def tenant_test(tenant: TenantContext = Depends(require_tenant)):
            return {
                "tenant_id": tenant.tenant_id,
                "user_id": tenant.user_id,
                "roles": tenant.roles,
            }

        return app

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_tenant_scoping_rejects_unauthenticated(self, client):
        """Request without JWT returns 401."""
        response = client.get("/api/tenant-test")
        assert response.status_code == 401

    @patch("config.settings.get_settings")
    def test_tenant_scoping_extracts_tenant_id(self, mock_get_settings, client):
        """Valid JWT with tenant_id returns correct TenantContext."""
        from jose import jwt as jose_jwt

        mock_get_settings.return_value = MagicMock(
            jwt_secret="test-secret",
            jwt_algorithm="HS256",
        )

        token = jose_jwt.encode(
            {"sub": "user-1", "tenant_id": "tenant-abc", "roles": ["viewer"]},
            "test-secret",
            algorithm="HS256",
        )

        response = client.get(
            "/api/tenant-test",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "tenant-abc"
        assert data["user_id"] == "user-1"
        assert data["roles"] == ["viewer"]

    @patch("config.settings.get_settings")
    def test_tenant_scoping_rejects_jwt_without_tenant_id(self, mock_get_settings, client):
        """JWT without tenant_id claim returns 401."""
        from jose import jwt as jose_jwt

        mock_get_settings.return_value = MagicMock(
            jwt_secret="test-secret",
            jwt_algorithm="HS256",
        )

        token = jose_jwt.encode(
            {"sub": "user-1", "roles": ["admin"]},
            "test-secret",
            algorithm="HS256",
        )

        response = client.get(
            "/api/tenant-test",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        data = response.json()["detail"]
        assert data["error_code"] == "TENANT_REQUIRED"


# ---------------------------------------------------------------------------
# TenantContext model tests
# ---------------------------------------------------------------------------

class TestTenantContext:
    """Tests for the TenantContext Pydantic model."""

    def test_create_with_all_fields(self):
        ctx = TenantContext(
            tenant_id="t-1",
            user_id="u-1",
            roles=["admin", "viewer"],
        )
        assert ctx.tenant_id == "t-1"
        assert ctx.user_id == "u-1"
        assert ctx.roles == ["admin", "viewer"]

    def test_create_with_defaults(self):
        ctx = TenantContext(tenant_id="t-1")
        assert ctx.tenant_id == "t-1"
        assert ctx.user_id is None
        assert ctx.roles == []

    def test_tenant_id_is_required(self):
        with pytest.raises(Exception):
            TenantContext()
