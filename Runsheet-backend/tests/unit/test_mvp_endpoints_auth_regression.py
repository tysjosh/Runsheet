"""
Regression tests for the legacy ``/api/fuel/mvp`` router (Task 9.4).

These tests lock in the IDOR / missing-auth remediation applied to
:mod:`Agents.support.mvp_endpoints` so the historical vulnerabilities cannot
silently reappear:

* **Auth-required** — every route enumerated under ``/api/fuel/mvp`` returns
  **401** for an unauthenticated ``TestClient`` request (Req 6.4). The router
  is mounted with the real :func:`get_tenant_context` Session_Verifier in
  ``auth_provider="supertokens"`` mode and a fake verifier that models
  "no session present", so the dependency rejects the request before any
  handler body runs.
* **Signature assertion** — no ``/api/fuel/mvp`` handler declares a
  ``tenant_id`` query parameter, and every handler depends on
  :func:`get_tenant_context` (Req 6.5, 6.4). This guards against a future
  handler reintroducing the client-supplied ``tenant_id`` that caused the
  original IDOR.
* **No-client-tenant** — a session scoped to tenant A that passes
  ``?tenant_id=B`` is scoped to tenant A only; the Elasticsearch query is
  filtered by the session tenant and never the client-supplied one (Req 6.5).

Requirements: 6.4, 6.5
"""

import inspect
import re

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import fastapi.params
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.support.mvp_endpoints import configure_mvp_endpoints, router
from ops.middleware.tenant_guard import (
    VerifiedSession,
    configure_session_verifier,
    configure_tenant_guard,
    get_tenant_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION_TENANT = "tenant-A"
SESSION_USER = "user-A"


class _NoSessionVerifier:
    """A :class:`SessionVerifier` that models a request with no session.

    Returning ``None`` from ``verify`` makes the ``supertokens`` branch of
    :func:`get_tenant_context` reject the request with 401 (no context).
    """

    async def verify(self, request):  # noqa: ARG002 - request intentionally unused
        return None


class _FixedTenantVerifier:
    """A :class:`SessionVerifier` that always verifies a session for tenant-A.

    Identity comes solely from server-controlled claims; the request (which may
    carry a spoofed ``tenant_id``) is ignored, mirroring a signed access-token
    payload as the sole source of scope.
    """

    async def verify(self, request):  # noqa: ARG002 - request intentionally unused
        return VerifiedSession(
            user_id=SESSION_USER,
            claims={
                "tenant_id": SESSION_TENANT,
                "roles": ["dispatcher"],
                "has_pii_access": True,
            },
        )


def _make_mock_es():
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    return es


def _make_mock_pipeline():
    pipeline = MagicMock()
    pipeline.run = AsyncMock(return_value="run-1")
    pipeline.get_status = AsyncMock(return_value={"state": "complete"})
    return pipeline


def _build_app(es_service=None, pipeline=None):
    """Mount the legacy MVP router with the real tenant guard dependency."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)

    configure_tenant_guard(None)  # use in-process US/imperial defaults
    configure_mvp_endpoints(
        pipeline=pipeline or _make_mock_pipeline(),
        es_service=es_service or _make_mock_es(),
        exception_replanning_agent=MagicMock(
            _on_signal=AsyncMock(), monitor_cycle=AsyncMock(return_value=([], []))
        ),
        plan_execution_service=MagicMock(),
        plan_execution_ws_manager=MagicMock(),
    )
    return app


def _concrete_path(path: str) -> str:
    """Replace ``{param}`` path placeholders with a concrete value."""
    return re.sub(r"\{[^}]+\}", "x", path)


def _mvp_routes():
    """All concrete (path, method) pairs registered on the MVP router."""
    pairs = []
    for route in router.routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            pairs.append((route, method))
    return pairs


# ---------------------------------------------------------------------------
# Auth-required: every MVP route returns 401 when unauthenticated (Req 6.4)
# ---------------------------------------------------------------------------


class TestMvpRoutesRequireAuth:
    """**Validates: Requirements 6.4**"""

    def teardown_method(self, method):
        configure_session_verifier(None)
        configure_tenant_guard(None)

    def test_every_route_returns_401_without_session(self):
        app = _build_app()
        configure_session_verifier(_NoSessionVerifier())
        client = TestClient(app, raise_server_exceptions=False)

        routes = _mvp_routes()
        assert routes, "expected the MVP router to register at least one route"

        for route, method in routes:
            url = _concrete_path(route.path)
            resp = client.request(method, url, json={})
            assert resp.status_code == 401, (
                f"{method} {url} returned {resp.status_code}, expected 401 "
                f"for an unauthenticated request"
            )


# ---------------------------------------------------------------------------
# Signature assertion: no tenant_id query param; all depend on the guard
# (Req 6.5, 6.4)
# ---------------------------------------------------------------------------


class TestMvpHandlerSignatures:
    """**Validates: Requirements 6.4, 6.5**"""

    def test_no_handler_declares_tenant_id_parameter(self):
        offenders = []
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            sig = inspect.signature(endpoint)
            if "tenant_id" in sig.parameters:
                offenders.append(route.path)

        assert not offenders, (
            "no /api/fuel/mvp handler may declare a tenant_id parameter "
            f"(client-supplied tenant_id is forbidden); offenders: {offenders}"
        )

    def test_every_handler_depends_on_get_tenant_context(self):
        missing = []
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            sig = inspect.signature(endpoint)
            depends_on_guard = any(
                isinstance(param.default, fastapi.params.Depends)
                and param.default.dependency is get_tenant_context
                for param in sig.parameters.values()
            )
            if not depends_on_guard:
                missing.append(route.path)

        assert not missing, (
            "every /api/fuel/mvp handler must depend on get_tenant_context; "
            f"handlers missing the dependency: {missing}"
        )


# ---------------------------------------------------------------------------
# No-client-tenant: session tenant A wins over ?tenant_id=B (Req 6.5)
# ---------------------------------------------------------------------------


class TestMvpNoClientTenant:
    """**Validates: Requirements 6.5**"""

    def teardown_method(self, method):
        configure_session_verifier(None)
        configure_tenant_guard(None)

    def test_query_tenant_id_is_ignored_in_favor_of_session_tenant(self):
        es = _make_mock_es()
        app = _build_app(es_service=es)
        configure_session_verifier(_FixedTenantVerifier())
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/fuel/mvp/forecasts?tenant_id=tenant-B")

        assert resp.status_code == 200
        assert es.search_documents.await_count >= 1

        # The ES query must be scoped to the session tenant, never the
        # client-supplied tenant-B.
        query = es.search_documents.call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        tenant_terms = [
            c["term"]["tenant_id"]
            for c in must_clauses
            if "term" in c and "tenant_id" in c["term"]
        ]
        assert tenant_terms == [SESSION_TENANT], (
            f"expected ES query scoped to {SESSION_TENANT!r}, got {tenant_terms!r}"
        )
        assert "tenant-B" not in str(query), (
            "client-supplied tenant-B must never appear in the scoped query"
        )
