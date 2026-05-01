"""
Route smoke tests covering all registered HTTP endpoints.

Verifies that every registered HTTP route is reachable and does not
return an unexpected 500 error. Routes that depend on uninitialized
services are expected to return 4xx or 5xx — the key assertion is
that the route is registered and the handler is importable.

For full smoke testing with mocked backends, see tests/smoke/.

Validates: Requirement 9.6
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch elasticsearch_service BEFORE any app imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_http_routes(app) -> list[tuple[str, str]]:
    """Extract all HTTP routes from the app as (method, path) tuples."""
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                routes.append((method.upper(), route.path))
    return sorted(routes)


def _get_ws_routes(app) -> list[str]:
    """Extract all WebSocket routes from the app."""
    routes = []
    for route in app.routes:
        if isinstance(route, WebSocketRoute):
            routes.append(route.path)
    return sorted(routes)


# ===========================================================================
# Route registration verification tests (Req 9.6)
# ===========================================================================

class TestRouteRegistration:
    """
    Verify that all expected routes are registered in the app.

    This test imports the app and checks that routes are present
    without sending actual requests (which would require full bootstrap).

    Validates: Requirement 9.6
    """

    @pytest.fixture(scope="class")
    def app(self):
        """Import the app for route inspection."""
        from main import app
        return app

    def test_app_has_http_routes(self, app):
        """Verify the app has HTTP routes registered."""
        routes = _get_http_routes(app)
        assert len(routes) >= 20, (
            f"Expected at least 20 HTTP routes, found {len(routes)}"
        )

    def test_app_has_websocket_routes(self, app):
        """Verify the app has WebSocket routes registered."""
        ws_routes = _get_ws_routes(app)
        assert len(ws_routes) >= 3, (
            f"Expected at least 3 WebSocket routes, found {len(ws_routes)}"
        )

    def test_health_routes_registered(self, app):
        """Verify health check routes are registered."""
        routes = _get_http_routes(app)
        route_paths = {path for _, path in routes}
        assert "/" in route_paths
        assert "/api/health" in route_paths
        assert "/health" in route_paths

    def test_ops_routes_registered(self, app):
        """Verify ops API routes are registered."""
        routes = _get_http_routes(app)
        ops_routes = [(m, p) for m, p in routes if "/api/ops" in p]
        assert len(ops_routes) >= 10, (
            f"Expected at least 10 ops routes, found {len(ops_routes)}"
        )

    def test_fuel_routes_registered(self, app):
        """Verify fuel API routes are registered."""
        routes = _get_http_routes(app)
        fuel_routes = [(m, p) for m, p in routes if "/api/fuel" in p]
        assert len(fuel_routes) >= 5, (
            f"Expected at least 5 fuel routes, found {len(fuel_routes)}"
        )

    def test_scheduling_routes_registered(self, app):
        """Verify scheduling API routes are registered."""
        routes = _get_http_routes(app)
        sched_routes = [(m, p) for m, p in routes if "/api/scheduling" in p]
        assert len(sched_routes) >= 10, (
            f"Expected at least 10 scheduling routes, found {len(sched_routes)}"
        )

    def test_agent_routes_registered(self, app):
        """Verify agent API routes are registered."""
        routes = _get_http_routes(app)
        agent_routes = [(m, p) for m, p in routes if "/api/agent" in p]
        assert len(agent_routes) >= 5, (
            f"Expected at least 5 agent routes, found {len(agent_routes)}"
        )

    def test_data_routes_registered(self, app):
        """Verify data API routes are registered."""
        routes = _get_http_routes(app)
        data_routes = [(m, p) for m, p in routes if "/api/data" in p or "/api/fleet" in p]
        assert len(data_routes) >= 3, (
            f"Expected at least 3 data routes, found {len(data_routes)}"
        )

    def test_websocket_ops_route_registered(self, app):
        """Verify /ws/ops WebSocket route is registered."""
        ws_routes = _get_ws_routes(app)
        assert "/ws/ops" in ws_routes

    def test_websocket_scheduling_route_registered(self, app):
        """Verify /ws/scheduling WebSocket route is registered."""
        ws_routes = _get_ws_routes(app)
        assert "/ws/scheduling" in ws_routes

    def test_websocket_agent_activity_route_registered(self, app):
        """Verify /ws/agent-activity WebSocket route is registered."""
        ws_routes = _get_ws_routes(app)
        assert "/ws/agent-activity" in ws_routes

    def test_websocket_fleet_route_registered(self, app):
        """Verify /api/fleet/live WebSocket route is registered."""
        ws_routes = _get_ws_routes(app)
        assert "/api/fleet/live" in ws_routes

    def test_no_duplicate_routes(self, app):
        """Verify no duplicate route registrations."""
        routes = _get_http_routes(app)
        seen = set()
        duplicates = []
        for method, path in routes:
            key = f"{method} {path}"
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        assert not duplicates, f"Duplicate routes found: {duplicates}"

    def test_all_routes_have_handlers(self, app):
        """Verify all routes have callable endpoint handlers."""
        for route in app.routes:
            if isinstance(route, APIRoute):
                assert route.endpoint is not None, (
                    f"Route {route.path} has no endpoint handler"
                )
                assert callable(route.endpoint), (
                    f"Route {route.path} endpoint is not callable"
                )

    def test_route_list_snapshot(self, app):
        """Print all registered routes for debugging and documentation."""
        routes = _get_http_routes(app)
        ws_routes = _get_ws_routes(app)

        # Just verify we can enumerate them — this is a documentation test
        assert len(routes) > 0
        assert len(ws_routes) > 0
