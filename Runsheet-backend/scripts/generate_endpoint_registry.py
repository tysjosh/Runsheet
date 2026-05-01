#!/usr/bin/env python3
"""
Auto-generate endpoint registry documentation from the FastAPI app.

Introspects all registered routes (HTTP + WebSocket) and produces
a Markdown document at docs/endpoint-registry.md.

Auto-discovers new routers without manual script updates by walking
the FastAPI app's route list.

Requirements: 3.1–3.5
"""
import inspect
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

logger = logging.getLogger(__name__)

# Ensure the Runsheet-backend directory is on sys.path
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _setup_mock_env() -> None:
    """Set minimal environment variables needed for app import."""
    defaults = {
        "ELASTICSEARCH_URL": "http://localhost:9200",
        "ELASTIC_ENDPOINT": "http://localhost:9200",
        "ELASTIC_API_KEY": "mock-key-for-registry-gen",
        "ELASTICSEARCH_API_KEY": "mock-key-for-registry-gen",
        "REDIS_URL": "redis://localhost:6379",
        "JWT_SECRET": "mock-jwt-secret-for-registry-generation",
        "JWT_ALGORITHM": "HS256",
        "ENVIRONMENT": "development",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def _import_app_with_mocks() -> Any:
    """Import the FastAPI app with mocked external services.

    Patches Elasticsearch, Redis, and other external clients so the app
    object can be constructed without live connections.  Only the route
    tree is needed — the lifespan never runs.
    """
    _setup_mock_env()

    # Create a mock Elasticsearch client that passes ping()
    mock_es_client = MagicMock()
    mock_es_client.ping.return_value = True
    mock_es_client.indices.exists.return_value = False
    mock_es_client.indices.create.return_value = {"acknowledged": True}
    mock_es_client.indices.get_mapping.return_value = {}
    mock_es_client.indices.put_mapping.return_value = {"acknowledged": True}
    mock_es_client.ilm.get_lifecycle.side_effect = Exception("not found")
    mock_es_client.ilm.put_lifecycle.return_value = {"acknowledged": True}
    mock_es_client.indices.put_settings.return_value = {"acknowledged": True}
    mock_es_client.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}}

    # Patch the Elasticsearch constructor before any module imports it
    with patch("elasticsearch.Elasticsearch", return_value=mock_es_client):
        # Also patch Redis if used at module level
        mock_redis = MagicMock()
        mock_redis.ping = AsyncMock(return_value=True)

        with patch.dict("os.environ", {}, clear=False):
            from main import app

    return app


def _get_auth_policy(method: str, path: str) -> str:
    """Determine the auth policy for a given method + path."""
    try:
        from middleware.auth_policy import get_policy_for_route
        policy = get_policy_for_route(method, path)
        return policy.value
    except Exception:
        return "—"


def _get_rate_limit_for_route(route: Any) -> str:
    """Extract rate limit string from a route's endpoint function, if any."""
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return "—"

    # slowapi stores rate limit info on the endpoint function via _rate_limits
    rate_limits = getattr(endpoint, "_rate_limits", None)
    if rate_limits:
        try:
            parts = []
            for rl in rate_limits:
                limit_str = str(rl)
                if limit_str:
                    parts.append(limit_str)
            if parts:
                return ", ".join(parts)
        except Exception:
            pass
    return "—"


def _get_schema_names(route: Any) -> Tuple[str, str]:
    """Extract request body and response schema names from a route."""
    request_schema = "—"
    response_schema = "—"

    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return request_schema, response_schema

    # Check for response_model on the route
    response_model = getattr(route, "response_model", None)
    if response_model is not None:
        response_schema = getattr(response_model, "__name__", str(response_model))

    # Inspect endpoint signature for Pydantic body parameters
    try:
        sig = inspect.signature(endpoint)
        skip_names = {"request", "http_request", "websocket", "self", "return"}
        skip_types = {"Request", "WebSocket", "str", "int", "float", "bool",
                      "UploadFile", "File", "Form", "Depends"}
        for param_name, param in sig.parameters.items():
            if param_name in skip_names:
                continue
            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                continue
            ann_name = getattr(annotation, "__name__", str(annotation))
            if ann_name in skip_types:
                continue
            if hasattr(annotation, "model_fields") or hasattr(annotation, "__fields__"):
                request_schema = ann_name
                break
    except (ValueError, TypeError):
        pass

    return request_schema, response_schema


def _get_router_prefix(path: str) -> str:
    """Determine the router prefix for a route based on its path."""
    # Ordered longest-first so more specific prefixes match first
    prefixes = [
        "/api/scheduling",
        "/api/ops/admin",
        "/api/ops",
        "/api/fuel",
        "/api/agent",
        "/api/fleet",
        "/api/chat",
        "/api/data",
        "/api/upload",
        "/api/demo",
        "/api/locations",
        "/api/analytics",
        "/api/search",
        "/webhooks",
        "/ws",
        "/health",
    ]

    for prefix in sorted(prefixes, key=len, reverse=True):
        if path.startswith(prefix):
            return prefix

    if path in ("/", "/docs", "/openapi.json", "/redoc"):
        return path

    return "—"


def _is_websocket_route(route: Any) -> bool:
    """Check if a route is a WebSocket endpoint."""
    route_class = type(route).__name__
    if "WebSocket" in route_class:
        return True
    # WebSocket routes lack a 'methods' attribute
    if hasattr(route, "path") and not hasattr(route, "methods"):
        path = route.path
        if "/ws" in path or "live" in path.lower():
            return True
    return False


def _get_ws_subscription_types(path: str) -> str:
    """Determine subscription types for a WebSocket endpoint."""
    ws_subscriptions = {
        "/ws/ops": "shipment_update, rider_update, sla_breach",
        "/ws/scheduling": "job_created, status_changed, delay_alert, cargo_update",
        "/ws/agent-activity": "agent_activity, approval_event",
        "/api/fleet/live": "location_update, fleet_status",
    }
    return ws_subscriptions.get(path, "—")


def generate_registry() -> str:
    """Introspect the FastAPI app and return Markdown documentation.

    Returns:
        A Markdown string containing the full endpoint registry.
    """
    app = _import_app_with_mocks()

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: List[str] = [
        "# Endpoint Registry",
        "",
        f"> Auto-generated by `scripts/generate_endpoint_registry.py` on {timestamp}.",
        "> Do not edit manually. Re-generate with:",
        "> ```",
        "> cd Runsheet-backend && python3 scripts/generate_endpoint_registry.py",
        "> ```",
        "",
    ]

    # Collect routes
    http_routes: List[Tuple[str, str, Any]] = []
    ws_routes: List[Any] = []

    for route in app.routes:
        if _is_websocket_route(route):
            ws_routes.append(route)
        elif hasattr(route, "methods") and hasattr(route, "path"):
            for method in sorted(route.methods):
                if method == "HEAD":
                    continue  # Skip implicit HEAD methods
                http_routes.append((method, route.path, route))

    # Sort HTTP routes by path then method
    http_routes.sort(key=lambda x: (x[1], x[0]))

    # ---- HTTP Endpoints table ----
    lines.append("## HTTP Endpoints")
    lines.append("")
    lines.append(
        "| Method | Path | Router | Auth | Rate Limit | Request Schema | Response Schema |"
    )
    lines.append(
        "|--------|------|--------|------|------------|----------------|-----------------|"
    )

    for method, path, route in http_routes:
        auth = _get_auth_policy(method, path)
        rate_limit = _get_rate_limit_for_route(route)
        req_schema, resp_schema = _get_schema_names(route)
        router_prefix = _get_router_prefix(path)

        lines.append(
            f"| {method} | `{path}` | {router_prefix} | {auth} | {rate_limit} "
            f"| {req_schema} | {resp_schema} |"
        )

    # ---- WebSocket Endpoints table ----
    ws_routes.sort(key=lambda r: getattr(r, "path", ""))

    lines.append("")
    lines.append("## WebSocket Endpoints")
    lines.append("")
    lines.append("| Path | Subscriptions | Auth |")
    lines.append("|------|--------------|------|")

    for route in ws_routes:
        path = getattr(route, "path", "")
        subscriptions = _get_ws_subscription_types(path)
        auth = _get_auth_policy("GET", path)
        lines.append(f"| `{path}` | {subscriptions} | {auth} |")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Generate the endpoint registry and write to docs/endpoint-registry.md."""
    output_path = _BACKEND_DIR.parent / "docs" / "endpoint-registry.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    registry = generate_registry()
    output_path.write_text(registry, encoding="utf-8")

    # Count data rows (subtract header rows)
    http_count = sum(1 for line in registry.split("\n")
                     if line.startswith("| ") and not line.startswith("| Method")
                     and not line.startswith("|--") and not line.startswith("| Path"))
    print(f"✅ Endpoint registry written to {output_path}")
    print(f"   {http_count} endpoint entries generated")


if __name__ == "__main__":
    main()
