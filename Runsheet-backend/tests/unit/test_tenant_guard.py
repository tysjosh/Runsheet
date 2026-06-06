"""
Unit tests for the Tenant Guard / Session_Verifier middleware.

After the SuperTokens hard cutover ``get_tenant_context`` verifies a
SuperTokens session only — the homegrown HS256 JWT path was removed. These
tests exercise the verifier via the :class:`SessionVerifier` seam
(``configure_session_verifier``) so no live managed core is required.

Tests cover:
- A verified session with a tenant_id claim yields the correct TenantContext
- A missing session / missing tenant_id claim is rejected with 401
- A spoofed query-param / header tenant_id is ignored (the claim is authoritative)
- has_pii_access extraction from the verified claims
- inject_tenant_filter wraps ES queries with a tenant_id filter

Validates: Requirements 2.6, 3.1, 3.2, 3.3, 3.5, 5.1, 5.3, 5.4
"""

import sys
from unittest.mock import MagicMock

import pytest

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

from errors.exceptions import AppException, unauthorized
from ops.middleware.tenant_guard import (
    TenantContext,
    VerifiedSession,
    configure_session_verifier,
    configure_tenant_guard,
    get_tenant_context,
    inject_tenant_filter,
)
from services.tenant_settings import (
    MeasurementUnits,
    TenantSettings,
    TenantSettingsService,
)


# ---------------------------------------------------------------------------
# SessionVerifier fakes
# ---------------------------------------------------------------------------


class _ClaimsVerifier:
    """A verifier that returns a fixed :class:`VerifiedSession` from claims."""

    def __init__(self, claims: dict):
        self._claims = claims

    async def verify(self, request: Request):
        user_id = self._claims.get("sub") or self._claims.get("user_id") or "unknown"
        return VerifiedSession(user_id=user_id, claims=dict(self._claims))


class _NoSessionVerifier:
    """A verifier that reports no SuperTokens session on the request."""

    async def verify(self, request: Request):
        return None


def _install_verifier(claims: dict) -> None:
    configure_session_verifier(_ClaimsVerifier(claims))


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
            "region": tenant.region,
            "measurement_units": tenant.measurement_units,
        }

    # Register the AppException handler so 401s come back as JSON
    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )

    client = TestClient(app)
    return app, client


@pytest.fixture(autouse=True)
def _reset_verifier():
    """Reset the verifier seam after every test."""
    yield
    configure_session_verifier(None)


# ---------------------------------------------------------------------------
# Valid session Tests — Validates: Req 3.1, 3.3, 3.5
# ---------------------------------------------------------------------------


class TestValidSession:
    """Verify that a verified session with a tenant_id claim is accepted."""

    def test_valid_session_returns_tenant_context(self):
        """A verified session with tenant_id returns 200 with correct context."""
        _, client = _build_app()
        _install_verifier({"tenant_id": "t-100", "sub": "user-42", "has_pii_access": False})

        resp = client.get("/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t-100"
        assert body["user_id"] == "user-42"
        assert body["has_pii_access"] is False

    def test_user_id_falls_back_to_user_id_claim(self):
        """When 'sub' is absent, user_id is read from the verified user id."""
        _, client = _build_app()
        _install_verifier({"tenant_id": "t-200", "user_id": "uid-7"})

        resp = client.get("/test")

        assert resp.status_code == 200
        assert resp.json()["user_id"] == "uid-7"


# ---------------------------------------------------------------------------
# Missing / Invalid session Tests — Validates: Req 2.6, 5.3
# ---------------------------------------------------------------------------


class TestMissingOrInvalidSession:
    """Requests without a verified session or missing tenant_id claim get 401."""

    def test_no_session_returns_401(self):
        _, client = _build_app()
        configure_session_verifier(_NoSessionVerifier())

        resp = client.get("/test")

        assert resp.status_code == 401

    def test_invalid_session_returns_401(self):
        """A present-but-invalid session (verifier raises) is rejected with 401."""

        class _RaisingVerifier:
            async def verify(self, request: Request):
                raise unauthorized(
                    message="Invalid or expired session",
                    details={"reason": "SuperTokens session verification failed"},
                )

        _, client = _build_app()
        configure_session_verifier(_RaisingVerifier())

        resp = client.get("/test")

        assert resp.status_code == 401

    def test_session_missing_tenant_id_claim_returns_401(self):
        """A verified session that lacks the tenant_id claim is rejected."""
        _, client = _build_app()
        _install_verifier({"sub": "user-1"})

        resp = client.get("/test")

        assert resp.status_code == 401

    def test_session_with_empty_tenant_id_returns_401(self):
        """A verified session with an empty-string tenant_id is rejected."""
        _, client = _build_app()
        _install_verifier({"tenant_id": "", "sub": "user-1"})

        resp = client.get("/test")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Spoofed Query Param Tests — Validates: Req 5.1, 5.2
# ---------------------------------------------------------------------------


class TestSpoofedTenantId:
    """Tenant_id from query params or extra headers is ignored; the claim wins."""

    def test_query_param_tenant_id_is_ignored(self):
        """Even if tenant_id is passed as a query param, the verified claim wins."""
        _, client = _build_app()
        _install_verifier({"tenant_id": "real-tenant", "sub": "user-1"})

        resp = client.get("/test?tenant_id=spoofed-tenant")

        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == "real-tenant"

    def test_header_tenant_id_is_ignored(self):
        """A custom X-Tenant-Id header does not override the verified claim."""
        _, client = _build_app()
        _install_verifier({"tenant_id": "real-tenant", "sub": "user-1"})

        resp = client.get("/test", headers={"X-Tenant-Id": "spoofed-tenant"})

        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == "real-tenant"


# ---------------------------------------------------------------------------
# PII Access Permission Tests — Validates: Req 3.5 (pii_access extraction)
# ---------------------------------------------------------------------------


class TestPIIAccessExtraction:
    """Verify has_pii_access is correctly extracted from the verified claims."""

    def test_pii_access_true(self):
        _, client = _build_app()
        _install_verifier({"tenant_id": "t-1", "sub": "u-1", "has_pii_access": True})

        resp = client.get("/test")

        assert resp.status_code == 200
        assert resp.json()["has_pii_access"] is True

    def test_pii_access_false(self):
        _, client = _build_app()
        _install_verifier({"tenant_id": "t-1", "sub": "u-1", "has_pii_access": False})

        resp = client.get("/test")

        assert resp.status_code == 200
        assert resp.json()["has_pii_access"] is False

    def test_pii_access_defaults_to_false_when_absent(self):
        _, client = _build_app()
        _install_verifier({"tenant_id": "t-1", "sub": "u-1"})

        resp = client.get("/test")

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


# ---------------------------------------------------------------------------
# Tenant Defaults (Region + measurement_units) — Validates: Req 5.4
# ---------------------------------------------------------------------------


class _StubTenantSettingsService:
    """Minimal TenantSettingsService stand-in for context hydration tests."""

    def __init__(self, mapping: dict[str, TenantSettings]):
        self._mapping = mapping
        self.calls: list[str] = []

    async def get(self, tenant_id: str) -> TenantSettings:
        self.calls.append(tenant_id)
        return self._mapping.get(
            tenant_id,
            TenantSettings(
                region="US",
                measurement_units=MeasurementUnits(volume="gal", distance="mi"),
            ),
        )


class TestTenantDefaultsInContext:
    """Tenant guard should expose Region + measurement_units on the context."""

    def teardown_method(self, method):
        # Always clear wiring to avoid leaking between tests.
        configure_tenant_guard(None)

    def test_context_falls_back_to_us_defaults_without_service(self):
        """When no settings service is wired, context carries US/imperial defaults."""
        configure_tenant_guard(None)
        _, client = _build_app()
        _install_verifier({"tenant_id": "tenant-new", "sub": "user-1"})

        resp = client.get("/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["region"] == "US"
        assert body["measurement_units"] == {"volume": "gal", "distance": "mi"}

    def test_context_reflects_us_tenant_from_service(self):
        """Req 5.4: US tenants carry gallons + miles."""
        service = _StubTenantSettingsService(
            {
                "tenant-us": TenantSettings(
                    region="US",
                    measurement_units=MeasurementUnits(volume="gal", distance="mi"),
                )
            }
        )
        configure_tenant_guard(service)

        _, client = _build_app()
        _install_verifier({"tenant_id": "tenant-us", "sub": "user-1"})

        resp = client.get("/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "tenant-us"
        assert body["region"] == "US"
        assert body["measurement_units"] == {"volume": "gal", "distance": "mi"}
        assert service.calls == ["tenant-us"]

    def test_context_reflects_ng_tenant_from_service(self):
        """Req 5.4: pre-pivot NG tenants carry liters + kilometers."""
        service = _StubTenantSettingsService(
            {
                "tenant-ng": TenantSettings(
                    region="NG",
                    measurement_units=MeasurementUnits(volume="l", distance="km"),
                )
            }
        )
        configure_tenant_guard(service)

        _, client = _build_app()
        _install_verifier({"tenant_id": "tenant-ng", "sub": "user-1"})

        resp = client.get("/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["region"] == "NG"
        assert body["measurement_units"] == {"volume": "l", "distance": "km"}

    def test_context_defaults_when_service_raises(self):
        """A failing settings service must not break the request path."""

        class _BoomService:
            async def get(self, tenant_id: str) -> TenantSettings:
                raise RuntimeError("redis is down")

        configure_tenant_guard(_BoomService())

        _, client = _build_app()
        _install_verifier({"tenant_id": "tenant-xyz", "sub": "user-1"})

        resp = client.get("/test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["region"] == "US"
        assert body["measurement_units"] == {"volume": "gal", "distance": "mi"}

    def test_configure_tenant_guard_accepts_real_service(self):
        """configure_tenant_guard should accept a real TenantSettingsService."""
        service = TenantSettingsService(redis_client=None)
        configure_tenant_guard(service)

        # Exercise the public getter to confirm wiring without a live call.
        from ops.middleware.tenant_guard import get_tenant_settings_service

        assert get_tenant_settings_service() is service
