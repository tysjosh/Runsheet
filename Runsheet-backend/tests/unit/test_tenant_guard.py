"""
Unit tests for the Tenant Guard middleware.

Tests cover:
- Valid JWT with tenant_id claim extracts TenantContext correctly
- Missing JWT / missing tenant_id claim returns 403
- Spoofed query param tenant_id is ignored (JWT claim is authoritative)
- pii_access permission extraction from JWT claims
- inject_tenant_filter wraps ES queries with tenant_id filter

Validates: Requirements 9.1-9.8
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from jose import jwt

# ---------------------------------------------------------------------------
# Patch the ElasticsearchService singleton BEFORE any ops imports so that
# importing ops modules doesn't trigger a real ES connection.
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from errors.exceptions import AppException
from ops.middleware.tenant_guard import (
    TenantContext,
    get_tenant_context,
    inject_tenant_filter,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWT_SECRET = "test-jwt-secret"
JWT_ALGORITHM = "HS256"


def _make_token(claims: dict) -> str:
    """Create a signed JWT with the given claims."""
    return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _build_app() -> tuple[FastAPI, TestClient]:
    """Create a minimal FastAPI app with a test endpoint using the tenant guard."""
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint(
        request: Request,
        tenant: TenantContext = Depends(get_tenant_context),
    ):
        return {
            "tenant_id": tenant.tenant_id,
            "user_id": tenant.user_id,
            "has_pii_access": tenant.has_pii_access,
        }

    # Register the AppException handler so 403s come back as JSON
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )

    client = TestClient(app)
    return app, client


# Shared mock for settings
_SETTINGS_PATCH = patch(
    "ops.middleware.tenant_guard.get_settings",
    return_value=MagicMock(jwt_secret=JWT_SECRET, jwt_algorithm=JWT_ALGORITHM),
)


# ---------------------------------------------------------------------------
# Valid JWT Tests — Validates: Req 9.1, 9.6
# ---------------------------------------------------------------------------


class TestValidJWT:
    """Verify that a valid JWT with tenant_id claim is accepted."""

    def test_valid_jwt_returns_tenant_context(self):
        """A properly signed JWT with tenant_id returns 200 with correct context."""
        _, client = _build_app()
        token = _make_token({"tenant_id": "t-100", "sub": "user-42", "has_pii_access": False})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t-100"
        assert body["user_id"] == "user-42"
        assert body["has_pii_access"] is False

    def test_user_id_falls_back_to_user_id_claim(self):
        """When 'sub' is absent, user_id is read from 'user_id' claim."""
        _, client = _build_app()
        token = _make_token({"tenant_id": "t-200", "user_id": "uid-7"})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json()["user_id"] == "uid-7"

    def test_user_id_defaults_to_unknown(self):
        """When neither 'sub' nor 'user_id' is present, user_id defaults to 'unknown'."""
        _, client = _build_app()
        token = _make_token({"tenant_id": "t-300"})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json()["user_id"] == "unknown"


# ---------------------------------------------------------------------------
# Missing / Invalid JWT Tests — Validates: Req 9.3, 9.6
# ---------------------------------------------------------------------------


class TestMissingOrInvalidJWT:
    """Requests without a valid JWT or missing tenant_id claim get 403."""

    def test_no_authorization_header_returns_403(self):
        _, client = _build_app()

        with _SETTINGS_PATCH:
            resp = client.get("/test")

        assert resp.status_code == 403

    def test_empty_bearer_token_returns_403(self):
        _, client = _build_app()

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": "Bearer "})

        assert resp.status_code == 403

    def test_non_bearer_scheme_returns_403(self):
        _, client = _build_app()

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": "Basic abc123"})

        assert resp.status_code == 403

    def test_invalid_jwt_signature_returns_403(self):
        """JWT signed with a different secret is rejected."""
        _, client = _build_app()
        token = jwt.encode(
            {"tenant_id": "t-evil", "sub": "hacker"},
            "wrong-secret",
            algorithm=JWT_ALGORITHM,
        )

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403

    def test_jwt_missing_tenant_id_claim_returns_403(self):
        """A valid JWT that lacks the tenant_id claim is rejected."""
        _, client = _build_app()
        token = _make_token({"sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403

    def test_jwt_with_empty_tenant_id_returns_403(self):
        """A JWT with an empty-string tenant_id is rejected."""
        _, client = _build_app()
        token = _make_token({"tenant_id": "", "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Spoofed Query Param Tests — Validates: Req 9.8
# ---------------------------------------------------------------------------


class TestSpoofedTenantId:
    """Tenant_id from query params or extra headers is ignored; JWT is authoritative."""

    def test_query_param_tenant_id_is_ignored(self):
        """Even if tenant_id is passed as a query param, the JWT claim wins."""
        _, client = _build_app()
        token = _make_token({"tenant_id": "real-tenant", "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get(
                "/test?tenant_id=spoofed-tenant",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == "real-tenant"

    def test_header_tenant_id_is_ignored(self):
        """A custom X-Tenant-Id header does not override the JWT claim."""
        _, client = _build_app()
        token = _make_token({"tenant_id": "real-tenant", "sub": "user-1"})

        with _SETTINGS_PATCH:
            resp = client.get(
                "/test",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Tenant-Id": "spoofed-tenant",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == "real-tenant"


# ---------------------------------------------------------------------------
# PII Access Permission Tests — Validates: Req 9.6 (pii_access extraction)
# ---------------------------------------------------------------------------


class TestPIIAccessExtraction:
    """Verify has_pii_access is correctly extracted from JWT claims."""

    def test_pii_access_true(self):
        _, client = _build_app()
        token = _make_token({"tenant_id": "t-1", "sub": "u-1", "has_pii_access": True})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json()["has_pii_access"] is True

    def test_pii_access_false(self):
        _, client = _build_app()
        token = _make_token({"tenant_id": "t-1", "sub": "u-1", "has_pii_access": False})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json()["has_pii_access"] is False

    def test_pii_access_defaults_to_false_when_absent(self):
        _, client = _build_app()
        token = _make_token({"tenant_id": "t-1", "sub": "u-1"})

        with _SETTINGS_PATCH:
            resp = client.get("/test", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json()["has_pii_access"] is False


# ---------------------------------------------------------------------------
# inject_tenant_filter Tests — Validates: Req 9.2, 9.4
# ---------------------------------------------------------------------------


class TestInjectTenantFilter:
    """Verify inject_tenant_filter wraps ES queries with a tenant_id filter."""

    def test_wraps_match_all_query(self):
        original = {"query": {"match_all": {}}}
        result = inject_tenant_filter(original, "t-abc")

        assert result == {
            "query": {
                "bool": {
                    "must": [{"match_all": {}}],
                    "filter": [{"term": {"tenant_id": "t-abc"}}],
                }
            }
        }

    def test_wraps_existing_query(self):
        original = {"query": {"term": {"status": "delivered"}}}
        result = inject_tenant_filter(original, "t-xyz")

        assert result["query"]["bool"]["must"] == [{"term": {"status": "delivered"}}]
        assert result["query"]["bool"]["filter"] == [{"term": {"tenant_id": "t-xyz"}}]

    def test_empty_query_defaults_to_match_all(self):
        result = inject_tenant_filter({}, "t-empty")

        assert result["query"]["bool"]["must"] == [{"match_all": {}}]
        assert result["query"]["bool"]["filter"] == [{"term": {"tenant_id": "t-empty"}}]
