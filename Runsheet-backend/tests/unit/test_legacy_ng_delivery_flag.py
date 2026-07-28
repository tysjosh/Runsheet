"""
Tests for the ``legacy_ng_delivery`` feature flag.

Covers:
- Flag resolution (default OFF, env override, fail-closed parsing).
- The legacy NG last-mile ops read surface 404s when the flag is off.
- Ops platform monitoring and the per-tenant flag admin routes are NOT gated,
  so a disabled surface can still be observed and re-enabled.
- The legacy support-ticket surface 404s when the flag is off.
- The Dinee **voice** integration (Surface A ``POST /voice-intake`` and the
  ``/voice/*`` prefix) is untouched by the flag — the audit's "Dinee webhook"
  is the legacy NG webhook (``POST /webhooks/dinee``), not the voice bridge.

Audit reference: product-owner-audit-2026-05-08 recommendation #1.
"""

import sys
from unittest.mock import MagicMock

import pytest

# Patch the ElasticsearchService singleton before ops imports so importing
# ops_es_service does not open a real ES connection.
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.legacy_flags import (
    LEGACY_NG_DELIVERY_DISABLED_CODE,
    LEGACY_NG_DELIVERY_ENV_VAR,
    is_legacy_ng_delivery_enabled,
)
from ops.api.endpoints import configure_ops_api, require_ops_enabled, router
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from ops.services.ops_es_service import OpsElasticsearchService


# ---------------------------------------------------------------------------
# Gated route inventory
# ---------------------------------------------------------------------------

GATED_OPS_PATHS = [
    ("GET", "/api/ops/shipments"),
    ("GET", "/api/ops/shipments/sla-breaches"),
    ("GET", "/api/ops/shipments/failures"),
    ("GET", "/api/ops/shipments/SHP-001"),
    ("GET", "/api/ops/riders"),
    ("GET", "/api/ops/riders/utilization"),
    ("GET", "/api/ops/riders/RDR-001"),
    ("GET", "/api/ops/events"),
    ("GET", "/api/ops/metrics/shipments"),
    ("GET", "/api/ops/metrics/sla"),
    ("GET", "/api/ops/metrics/riders"),
    ("GET", "/api/ops/metrics/failures"),
]

# Routes that must stay reachable while the legacy surface is off: platform
# monitoring plus the per-tenant flag admin API.
UNGATED_OPS_PATHS = [
    "/api/ops/metrics/prometheus",
    "/api/ops/monitoring/ingestion",
    "/api/ops/monitoring/indexing",
    "/api/ops/monitoring/poison-queue",
    "/api/ops/admin/feature-flags/{tenant_id}/enable",
    "/api/ops/admin/feature-flags/{tenant_id}/disable",
    "/api/ops/admin/feature-flags/{tenant_id}/rollback",
]


def _es_search_response():
    return {"hits": {"hits": [], "total": {"value": 0}}}


@pytest.fixture()
def ops_client():
    """FastAPI app with the ops router, mocked ES, and no per-tenant flag
    service (so only the legacy_ng_delivery gate can block a request)."""
    app = FastAPI()

    mock_es_client = MagicMock()
    mock_es_client.search = MagicMock(return_value=_es_search_response())
    mock_ops_es = MagicMock(spec=OpsElasticsearchService)
    mock_ops_es.client = mock_es_client

    configure_ops_api(ops_es_service=mock_ops_es, feature_flag_service=None)

    async def _override_tenant():
        return TenantContext(
            tenant_id="tenant-1", user_id="user-1", has_pii_access=False
        )

    app.dependency_overrides[get_tenant_context] = _override_tenant

    from starlette.middleware.base import BaseHTTPMiddleware

    class FakeRequestID(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.request_id = "req-legacy-flag"
            return await call_next(request)

    app.add_middleware(FakeRequestID)

    # Register the structured handlers so AppException → ErrorResponse JSON.
    from errors.handlers import register_exception_handlers

    register_exception_handlers(app)

    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------


class TestFlagResolution:
    def test_defaults_to_disabled(self, monkeypatch):
        """With no env override the flag resolves from settings, which default
        the legacy surface OFF."""
        monkeypatch.delenv(LEGACY_NG_DELIVERY_ENV_VAR, raising=False)
        assert is_legacy_ng_delivery_enabled() is False

    @pytest.mark.parametrize("raw", ["true", "True", "1", "yes", "on", "ON"])
    def test_truthy_values_enable(self, monkeypatch, raw):
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, raw)
        assert is_legacy_ng_delivery_enabled() is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", "maybe", "  "])
    def test_non_truthy_values_disable(self, monkeypatch, raw):
        """Anything not explicitly truthy fails closed (blank falls through to
        the settings default, which is also off)."""
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, raw)
        assert is_legacy_ng_delivery_enabled() is False


# ---------------------------------------------------------------------------
# Ops read surface
# ---------------------------------------------------------------------------


class TestOpsSurfaceGated:
    @pytest.mark.parametrize("method,path", GATED_OPS_PATHS)
    def test_returns_404_when_flag_off(self, ops_client, monkeypatch, method, path):
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "false")
        resp = ops_client.request(method, path)
        assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"
        assert resp.json()["error_code"] == LEGACY_NG_DELIVERY_DISABLED_CODE

    @pytest.mark.parametrize("method,path", GATED_OPS_PATHS)
    def test_not_blocked_when_flag_on(self, ops_client, monkeypatch, method, path):
        """With the flag on, the gate stops blocking. Single-resource routes may
        still 404 for a missing document, but never with the flag error code."""
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "true")
        resp = ops_client.request(method, path)
        if resp.status_code == 404:
            assert (
                resp.json().get("error_code") != LEGACY_NG_DELIVERY_DISABLED_CODE
            )

    @pytest.mark.parametrize("path", UNGATED_OPS_PATHS)
    def test_monitoring_and_admin_routes_are_not_gated(self, path):
        """Platform monitoring and the flag admin API must not depend on the
        legacy gate, otherwise a disabled surface becomes unobservable and the
        per-tenant flag becomes unmanageable."""
        matches = [r for r in router.routes if r.path == path]
        assert matches, f"route {path} not registered"
        for route in matches:
            dependency_calls = [
                dep.call for dep in route.dependant.dependencies
            ]
            assert require_ops_enabled not in dependency_calls, (
                f"{path} unexpectedly depends on the legacy_ng_delivery gate"
            )


# ---------------------------------------------------------------------------
# Legacy support-ticket surface
# ---------------------------------------------------------------------------


def _build_data_app(tenant_id: str = "tenant-1"):
    import data_endpoints
    from ops.middleware.tenant_guard import TenantContext as TC

    class _FakeES:
        async def search_documents(self, index, query, size=100):
            return {"hits": {"hits": [], "total": {"value": 0}}}

        async def semantic_search(self, tenant_id, index, text, fields, size=10):
            return []

    data_endpoints.elasticsearch_service = _FakeES()

    async def _override_tenant():
        return TC(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
        )

    app = FastAPI()

    from errors.handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(data_endpoints.router)
    app.dependency_overrides[get_tenant_context] = _override_tenant
    return app


class TestSupportTicketsGated:
    def test_support_tickets_404_when_flag_off(self, monkeypatch):
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "false")
        with TestClient(_build_data_app()) as client:
            resp = client.get("/api/support/tickets")
        assert resp.status_code == 404
        assert resp.json()["error_code"] == LEGACY_NG_DELIVERY_DISABLED_CODE

    def test_support_tickets_served_when_flag_on(self, monkeypatch):
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "true")
        with TestClient(_build_data_app()) as client:
            resp = client.get("/api/support/tickets")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_support_ticket_search_404_when_flag_off(self, monkeypatch):
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "false")
        with TestClient(_build_data_app()) as client:
            resp = client.get(
                "/api/search", params={"q": "damaged", "index": "support_tickets"}
            )
        assert resp.status_code == 404
        assert resp.json()["error_code"] == LEGACY_NG_DELIVERY_DISABLED_CODE

    def test_truck_search_unaffected_when_flag_off(self, monkeypatch):
        """Only the ``support_tickets`` index is gated — ``trucks`` still works."""
        monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "false")
        with TestClient(_build_data_app()) as client:
            resp = client.get("/api/search", params={"q": "truck", "index": "trucks"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The Dinee VOICE surface must not be gated
# ---------------------------------------------------------------------------


class TestVoiceSurfaceNotGated:
    """The audit's "Dinee webhook" is the legacy NG webhook (POST /webhooks/dinee),
    not the shipped Dinee voice integration. The voice surface must stay live
    regardless of the flag."""

    def test_voice_modules_do_not_import_the_legacy_gate(self):
        import importlib
        import pkgutil

        import fuel.voice as voice_pkg

        modules = ["fuel.intake.voice_intake_adapter"] + [
            f"fuel.voice.{m.name}"
            for m in pkgutil.iter_modules(voice_pkg.__path__)
        ]
        for name in modules:
            mod = importlib.import_module(name)
            source_flag = getattr(mod, "is_legacy_ng_delivery_enabled", None)
            assert source_flag is None, (
                f"{name} references the legacy_ng_delivery gate — the voice "
                "integration must stay enabled independently of it"
            )

    def test_voice_routes_do_not_depend_on_the_legacy_gate(self):
        from fuel.voice.voice_read_driver_router import router as voice_read_router
        from fuel.voice.voice_submission_router import router as voice_submission_router

        for voice_router in (voice_read_router, voice_submission_router):
            for route in voice_router.routes:
                dependant = getattr(route, "dependant", None)
                if dependant is None:
                    continue
                calls = [dep.call for dep in dependant.dependencies]
                assert require_ops_enabled not in calls, (
                    f"voice route {route.path} depends on the legacy gate"
                )
