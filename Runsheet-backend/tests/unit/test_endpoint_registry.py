"""
Unit tests for the endpoint registry generator script.

Verifies that the generated registry includes all known routes from
the FastAPI app, covering both HTTP and WebSocket endpoints.

Requirements: 3.4, Correctness Property P15
"""
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure Runsheet-backend is on sys.path
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture(scope="module")
def mock_es_client():
    """Create a mock Elasticsearch client that passes ping()."""
    mock = MagicMock()
    mock.ping.return_value = True
    mock.indices.exists.return_value = False
    mock.indices.create.return_value = {"acknowledged": True}
    mock.indices.get_mapping.return_value = {}
    mock.indices.put_mapping.return_value = {"acknowledged": True}
    mock.ilm.get_lifecycle.side_effect = Exception("not found")
    mock.ilm.put_lifecycle.return_value = {"acknowledged": True}
    mock.indices.put_settings.return_value = {"acknowledged": True}
    mock.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}}
    return mock


@pytest.fixture(scope="module")
def app_and_registry(mock_es_client):
    """Import the app and generate the registry once for all tests."""
    env_defaults = {
        "ELASTICSEARCH_URL": "http://localhost:9200",
        "ELASTIC_ENDPOINT": "http://localhost:9200",
        "ELASTIC_API_KEY": "mock-key-for-test",
        "ELASTICSEARCH_API_KEY": "mock-key-for-test",
        "REDIS_URL": "redis://localhost:6379",
        "JWT_SECRET": "mock-jwt-secret-for-test",
        "JWT_ALGORITHM": "HS256",
        "ENVIRONMENT": "development",
    }
    for key, value in env_defaults.items():
        os.environ.setdefault(key, value)

    with patch("elasticsearch.Elasticsearch", return_value=mock_es_client):
        from main import app
        from scripts.generate_endpoint_registry import generate_registry

        registry_text = generate_registry()

    return app, registry_text


def _extract_http_paths_from_app(app) -> set:
    """Extract all HTTP (method, path) pairs from the app's routes."""
    pairs = set()
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            for method in route.methods:
                if method == "HEAD":
                    continue
                pairs.add((method, route.path))
    return pairs


def _extract_ws_paths_from_app(app) -> set:
    """Extract all WebSocket paths from the app's routes."""
    paths = set()
    for route in app.routes:
        route_class = type(route).__name__
        if "WebSocket" in route_class:
            paths.add(route.path)
        elif hasattr(route, "path") and not hasattr(route, "methods"):
            path = route.path
            if "/ws" in path or "live" in path.lower():
                paths.add(path)
    return paths


def _extract_http_paths_from_registry(registry_text: str) -> set:
    """Extract all (method, path) pairs from the generated Markdown."""
    pairs = set()
    in_http_section = False
    for line in registry_text.split("\n"):
        if line.startswith("## HTTP Endpoints"):
            in_http_section = True
            continue
        if line.startswith("## WebSocket Endpoints"):
            in_http_section = False
            continue
        if not in_http_section:
            continue
        if line.startswith("|--") or line.startswith("| Method"):
            continue
        # Parse table row: | METHOD | `path` | ...
        match = re.match(r"\|\s*(\w+)\s*\|\s*`([^`]+)`", line)
        if match:
            method = match.group(1)
            path = match.group(2)
            pairs.add((method, path))
    return pairs


def _extract_ws_paths_from_registry(registry_text: str) -> set:
    """Extract all WebSocket paths from the generated Markdown."""
    paths = set()
    in_ws_section = False
    for line in registry_text.split("\n"):
        if line.startswith("## WebSocket Endpoints"):
            in_ws_section = True
            continue
        if in_ws_section and line.startswith("## "):
            in_ws_section = False
            continue
        if not in_ws_section:
            continue
        if line.startswith("|--") or line.startswith("| Path"):
            continue
        match = re.match(r"\|\s*`([^`]+)`", line)
        if match:
            paths.add(match.group(1))
    return paths


class TestEndpointRegistryCompleteness:
    """Verify the generated registry includes all known routes."""

    def test_all_http_routes_present(self, app_and_registry):
        """Every HTTP route in app.routes appears in the registry."""
        app, registry_text = app_and_registry
        app_routes = _extract_http_paths_from_app(app)
        registry_routes = _extract_http_paths_from_registry(registry_text)

        missing = app_routes - registry_routes
        assert not missing, (
            f"HTTP routes missing from registry: {sorted(missing)}"
        )

    def test_all_ws_routes_present(self, app_and_registry):
        """Every WebSocket route in app.routes appears in the registry."""
        app, registry_text = app_and_registry
        app_ws = _extract_ws_paths_from_app(app)
        registry_ws = _extract_ws_paths_from_registry(registry_text)

        missing = app_ws - registry_ws
        assert not missing, (
            f"WebSocket routes missing from registry: {sorted(missing)}"
        )

    def test_no_extra_http_routes_in_registry(self, app_and_registry):
        """The registry does not contain HTTP routes not in the app."""
        app, registry_text = app_and_registry
        app_routes = _extract_http_paths_from_app(app)
        registry_routes = _extract_http_paths_from_registry(registry_text)

        extra = registry_routes - app_routes
        assert not extra, (
            f"Extra HTTP routes in registry not in app: {sorted(extra)}"
        )

    def test_no_extra_ws_routes_in_registry(self, app_and_registry):
        """The registry does not contain WS routes not in the app."""
        app, registry_text = app_and_registry
        app_ws = _extract_ws_paths_from_app(app)
        registry_ws = _extract_ws_paths_from_registry(registry_text)

        extra = registry_ws - app_ws
        assert not extra, (
            f"Extra WebSocket routes in registry not in app: {sorted(extra)}"
        )

    def test_registry_has_http_section(self, app_and_registry):
        """The registry contains an HTTP Endpoints section."""
        _, registry_text = app_and_registry
        assert "## HTTP Endpoints" in registry_text

    def test_registry_has_ws_section(self, app_and_registry):
        """The registry contains a WebSocket Endpoints section."""
        _, registry_text = app_and_registry
        assert "## WebSocket Endpoints" in registry_text

    def test_registry_has_header(self, app_and_registry):
        """The registry starts with the expected title."""
        _, registry_text = app_and_registry
        assert registry_text.startswith("# Endpoint Registry")

    def test_registry_has_auto_generated_notice(self, app_and_registry):
        """The registry contains the auto-generated notice."""
        _, registry_text = app_and_registry
        assert "Auto-generated by" in registry_text
        assert "Do not edit manually" in registry_text

    def test_auth_column_populated(self, app_and_registry):
        """Auth column is populated for at least some routes."""
        _, registry_text = app_and_registry
        # Check that at least some routes have jwt_required or public
        assert "jwt_required" in registry_text or "public" in registry_text

    def test_known_ws_endpoints_present(self, app_and_registry):
        """Known WebSocket endpoints are in the registry."""
        _, registry_text = app_and_registry
        known_ws = ["/ws/ops", "/ws/scheduling", "/ws/agent-activity", "/api/fleet/live"]
        for ws_path in known_ws:
            assert f"`{ws_path}`" in registry_text, (
                f"Known WebSocket endpoint {ws_path} not found in registry"
            )


class TestEndpointRegistryFreshness:
    """CI freshness check: the committed registry matches a fresh generation.

    This test regenerates the registry and compares it against the committed
    ``docs/endpoint-registry.md``. If they differ, the committed file is stale
    and needs to be regenerated.

    Requirements: 3.4, Correctness Property P15
    """

    def test_registry_is_up_to_date(self, app_and_registry):
        """Committed docs/endpoint-registry.md matches freshly generated output.

        This is the equivalent of running:
            python3 scripts/generate_endpoint_registry.py
            git diff --exit-code docs/endpoint-registry.md
        """
        _, fresh_registry = app_and_registry

        registry_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "endpoint-registry.md"

        assert registry_path.exists(), (
            f"docs/endpoint-registry.md does not exist at {registry_path}. "
            "Run: cd Runsheet-backend && python3 scripts/generate_endpoint_registry.py"
        )

        committed_text = registry_path.read_text(encoding="utf-8")

        # Strip the timestamp line from both since it changes on every run
        def _strip_timestamp(text: str) -> str:
            lines = text.split("\n")
            return "\n".join(
                line for line in lines
                if not line.startswith("> Auto-generated by")
            )

        committed_stripped = _strip_timestamp(committed_text)
        fresh_stripped = _strip_timestamp(fresh_registry)

        if committed_stripped != fresh_stripped:
            # Find the first differing line for a helpful error message
            committed_lines = committed_stripped.split("\n")
            fresh_lines = fresh_stripped.split("\n")
            for i, (c, f) in enumerate(zip(committed_lines, fresh_lines)):
                if c != f:
                    pytest.fail(
                        f"docs/endpoint-registry.md is stale. "
                        f"First difference at line {i + 1}:\n"
                        f"  committed: {c!r}\n"
                        f"  expected:  {f!r}\n\n"
                        f"Regenerate with: cd Runsheet-backend && "
                        f"python3 scripts/generate_endpoint_registry.py"
                    )
            if len(committed_lines) != len(fresh_lines):
                pytest.fail(
                    f"docs/endpoint-registry.md is stale. "
                    f"Line count differs: committed={len(committed_lines)}, "
                    f"expected={len(fresh_lines)}.\n\n"
                    f"Regenerate with: cd Runsheet-backend && "
                    f"python3 scripts/generate_endpoint_registry.py"
                )
