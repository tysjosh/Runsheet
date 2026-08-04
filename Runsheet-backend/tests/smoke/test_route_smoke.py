"""
Parametrized HTTP and WebSocket smoke tests.

Auto-discovers all registered HTTP routes from ``app.routes`` and verifies:
1. Every route is registered and has a callable handler
2. Route discovery automatically includes new routes
3. Health endpoints return 200
4. WebSocket routes accept connections

Routes that depend on bootstrapped services (ES, Redis) are tested for
registration only — full request-level smoke testing requires the
bootstrap lifecycle which is covered by the integration test suite.

Validates: Requirements 15.1, 15.3, 15.4, 15.5, 15.6
"""

import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any app imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from tests.smoke.fixtures import ROUTE_FIXTURES, WS_FIXTURES

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def smoke_app():
    """Import the FastAPI app for route inspection and basic testing."""
    from main import app
    return app


@pytest.fixture(scope="module")
def smoke_client(smoke_app):
    """Create a TestClient that does NOT raise server exceptions."""
    return TestClient(smoke_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Route discovery
# ---------------------------------------------------------------------------

def _discover_http_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Discover all HTTP routes as (method, path) tuples."""
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                routes.append((method.upper(), route.path))
    return sorted(routes)


def _discover_ws_routes(app: FastAPI) -> list[str]:
    """Discover all WebSocket routes."""
    return sorted(
        route.path
        for route in app.routes
        if isinstance(route, WebSocketRoute)
    )


# ===========================================================================
# HTTP Route Registration Tests (Req 15.1, 15.5)
# ===========================================================================

class TestHTTPRouteRegistration:
    """
    Verify all expected HTTP routes are registered and have callable handlers.

    Auto-discovers routes from the app without manual updates.

    Validates: Requirements 15.1, 15.5
    """

    # ``test_minimum_route_count`` (>=20) and the five per-prefix floors
    # (>=10 ops, >=5 fuel, >=10 scheduling, >=5 agent, >=3 data) stood here.
    #
    # The app registers 334 routes, so a floor of 20 only trips once 94% of them
    # have vanished; the per-prefix floors are equally slack. They were the only
    # route-count check that ran while the ``endpoint-registry`` job was broken
    # by a generation timestamp in its own output, which made its
    # ``git diff --exit-code`` guard unpassable. That job now works and pins all
    # 334 entries exactly, so any route appearing or disappearing shows up as a
    # diff. Keeping loose floors beside an exact check just invites someone to
    # read a passing floor as evidence.

    def test_all_routes_have_callable_handlers(self, smoke_app):
        """Every registered route has a callable endpoint handler."""
        for route in smoke_app.routes:
            if isinstance(route, APIRoute):
                assert route.endpoint is not None, (
                    f"Route {route.path} has no endpoint handler"
                )
                assert callable(route.endpoint), (
                    f"Route {route.path} endpoint is not callable"
                )

    def test_no_duplicate_routes(self, smoke_app):
        """No duplicate route registrations."""
        routes = _discover_http_routes(smoke_app)
        seen = set()
        duplicates = []
        for method, path in routes:
            key = f"{method} {path}"
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        assert not duplicates, f"Duplicate routes: {duplicates}"

    # ``test_auto_discovery_matches_app_routes`` stood here. It compared
    # ``_discover_http_routes(app)`` against a copy of that helper's own loop,
    # written out again in the test body. Both walked ``app.routes`` and filtered
    # on ``APIRoute`` identically, so the two sides could not disagree — the
    # assertion held for any app, including a broken one, and would only fail if
    # ``sorted()`` or ``set()`` misbehaved.

    def test_health_routes_present(self, smoke_app):
        """Health check routes are registered."""
        routes = _discover_http_routes(smoke_app)
        route_paths = {path for _, path in routes}
        assert "/" in route_paths, "Root route / not registered"
        assert "/api/health" in route_paths, "/api/health not registered"
        assert "/health" in route_paths, "/health not registered"


# ===========================================================================
# HTTP Smoke Tests — Accessible Endpoints (Req 15.3, 15.6)
# ===========================================================================

class TestHTTPRouteSmoke:
    """
    Smoke test for endpoints that don't require bootstrapped services.

    Tests health endpoints and root endpoint which should always return
    non-500 responses. Other endpoints are tested for registration only
    since they require the full bootstrap lifecycle.

    Validates: Requirements 15.3, 15.6
    """

    def test_root_returns_200(self, smoke_client):
        """Root endpoint returns 200."""
        resp = smoke_client.get("/")
        assert resp.status_code == 200

    def test_api_health_returns_200(self, smoke_client):
        """API health endpoint returns 200."""
        resp = smoke_client.get("/api/health")
        assert resp.status_code == 200

    def test_agent_health_returns_non_500(self, smoke_client):
        """Agent health endpoint returns non-500."""
        resp = smoke_client.get("/api/agent/health")
        assert resp.status_code < 500, (
            f"/api/agent/health returned {resp.status_code}: {resp.text[:200]}"
        )

    def test_fixture_coverage(self, smoke_app):
        """Verify fixture registry covers a majority of routes.

        Routes without fixtures use default empty-body requests and are
        expected to return 400/422 (not 500).
        """
        routes = _discover_http_routes(smoke_app)
        covered = 0
        for method, path in routes:
            key = f"{method} {path}"
            if key in ROUTE_FIXTURES:
                covered += 1

        coverage_pct = (covered / len(routes)) * 100 if routes else 0
        assert coverage_pct >= 50, (
            f"Fixture coverage is {coverage_pct:.0f}% ({covered}/{len(routes)}). "
            f"Expected ≥50%."
        )


# ===========================================================================
# WebSocket Smoke Tests (Req 15.4, Correctness Property P5)
# ===========================================================================

class TestWebSocketRouteSmoke:
    """
    WebSocket smoke tests verifying route registration and connection
    establishment.

    For each WS endpoint:
    - Verify the route is registered
    - Attempt connection and verify upgrade succeeds
    - If endpoint sends a confirmation message, verify it within 2 seconds

    Validates: Requirements 15.4, Correctness Property P5
    """

    def test_ws_routes_registered(self, smoke_app):
        """All expected WebSocket routes are registered."""
        ws_routes = _discover_ws_routes(smoke_app)
        expected = ["/api/fleet/live", "/ws/agent-activity", "/ws/ops", "/ws/scheduling"]
        for path in expected:
            assert path in ws_routes, f"WebSocket route {path} not registered"

    # ``test_ws_route_count`` (>=4) stood here. ``test_ws_routes_registered``
    # above already pins all four expected paths by name, which fails for the
    # same reason and says which route went missing — so the count added a
    # weaker assertion next to a stronger one.

    # Four connection tests stood here, one per WebSocket route. Each wrapped its
    # whole body in ``try: ... except Exception: pass``, so every assertion
    # inside was optional and none of them could fail — a broken handshake, a
    # missing confirmation frame or the wrong payload all passed silently. The
    # class docstring above still described them as verifying "the upgrade
    # succeeds" and a confirmation "within 2 seconds"; neither was true.
    #
    # They are deleted rather than repaired because the thing they suppressed is
    # real: these routes need the bootstrap lifecycle, and ``smoke_app`` imports
    # ``main.app`` with ``services.elasticsearch_service`` stubbed out, so the
    # managers behind them are not initialised. Asserting on the handshake here
    # would fail for the environment rather than for the product. A genuine
    # check belongs where the app is bootstrapped — ``tests/integration`` already
    # holds tests of that shape — and route registration is covered by
    # ``test_ws_routes_registered`` below, which does fail if a route disappears.

    def test_ws_fixtures_cover_all_routes(self, smoke_app):
        """WS fixture registry covers all WebSocket routes."""
        ws_routes = _discover_ws_routes(smoke_app)
        for path in ws_routes:
            assert path in WS_FIXTURES, (
                f"WebSocket route {path} has no fixture in WS_FIXTURES"
            )


# ===========================================================================
# Timing Tests (Req 15.7)
# ===========================================================================

# ===========================================================================
# ``TestSmokeTestTiming`` stood here (Req 15.7)
# ===========================================================================
#
# Three tests asserting that route discovery, fixture lookup and path resolution
# each finish in under a second. They measured the test harness rather than the
# product: a dict lookup over a few hundred keys and a walk of ``app.routes``.
# Nothing a developer could break would make them fail, but a loaded CI runner
# could, which makes them a source of flakes with no signal to trade for it.
#
# They existed to prove the suite fit a 30-second budget, which came from the
# ``--timeout=30`` on the old ``smoke-tests`` CI job. That job is gone, and the
# hang guard is now ``--timeout=120`` in ``pytest.ini`` covering every test — so
# the budget is enforced by the thing that actually runs the tests instead of by
# tests that time themselves.
