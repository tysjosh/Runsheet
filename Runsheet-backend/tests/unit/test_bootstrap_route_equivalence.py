"""
Test verifying startup route equivalence after decomposition.

Ensures no routes are lost or duplicated during the main.py refactoring.

Validates: Correctness Property P1
"""
import sys
from unittest.mock import MagicMock

import pytest


# Known routes that must exist in the refactored app.
EXPECTED_INLINE_ROUTES = {
    "/",
    "/api/health",
    "/health",
    "/health/ready",
    "/health/live",
    "/api/chat",
    "/api/chat/fallback",
    "/api/chat/clear",
    "/api/demo/reset",
    "/api/demo/status",
    "/api/upload/csv",
    "/api/upload/batch",
    "/api/upload/selective",
    "/api/upload/sheets",
    "/api/locations/webhook",
    "/api/locations/batch",
    "/ws/ops",
    "/ws/scheduling",
    "/ws/agent-activity",
    "/api/fleet/live",
}


@pytest.fixture(autouse=True)
def _mock_es():
    """Mock ES module to prevent real connections when importing main."""
    mock_es_mod = MagicMock()
    mock_es_mod.elasticsearch_service = MagicMock()
    mock_es_mod.ElasticsearchService = MagicMock

    saved = {}
    mods = {
        "services.elasticsearch_service": mock_es_mod,
        "services.data_seeder": MagicMock(),
    }
    for name, mock_mod in mods.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mock_mod

    yield

    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig


class TestRouteEquivalence:
    """Verify the refactored app exposes all expected routes."""

    def test_inline_routes_present(self):
        """All inline routes from the original main.py must be present."""
        from main import app

        registered_paths = set()
        for route in app.routes:
            path = getattr(route, "path", None)
            if path:
                registered_paths.add(path)

        missing = EXPECTED_INLINE_ROUTES - registered_paths
        assert not missing, f"Missing routes after refactoring: {missing}"

    def test_no_duplicate_routes(self):
        """No route path should be registered more than once for the same method."""
        from main import app

        seen = {}
        duplicates = []
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", set()) or {"WS"}
            if path:
                for method in methods:
                    key = (method, path)
                    if key in seen:
                        duplicates.append(key)
                    seen[key] = True

        assert not duplicates, f"Duplicate routes found: {duplicates}"
