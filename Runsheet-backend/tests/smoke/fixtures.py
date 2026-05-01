"""
Smoke test fixture registry.

Defines ROUTE_FIXTURES mapping route paths to minimal valid request payloads
(method, headers, json, params) for smoke testing. Also defines WS_FIXTURES
for WebSocket endpoint connection parameters.

For routes without explicit fixtures, a default empty-body request is used
(expecting 400/422, not 500).

Validates: Requirements 15.2, 15.5
"""

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Route fixture type
# ---------------------------------------------------------------------------

class RouteFixture:
    """Minimal valid request payload for a route."""

    def __init__(
        self,
        method: str = "GET",
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        path_params: Optional[Dict[str, str]] = None,
        content: Optional[str] = None,
        content_type: Optional[str] = None,
    ):
        self.method = method
        self.json = json
        self.params = params or {}
        self.headers = headers or {}
        self.path_params = path_params or {}
        self.content = content
        self.content_type = content_type


class WSFixture:
    """Connection parameters for a WebSocket endpoint."""

    def __init__(
        self,
        params: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        expects_confirmation: bool = True,
    ):
        self.params = params or {}
        self.headers = headers or {}
        self.expects_confirmation = expects_confirmation


# ---------------------------------------------------------------------------
# HTTP Route Fixtures
# ---------------------------------------------------------------------------

ROUTE_FIXTURES: Dict[str, RouteFixture] = {
    # ---- Health endpoints ----
    "GET /": RouteFixture(),
    "GET /api/health": RouteFixture(),
    "GET /health": RouteFixture(),
    "GET /health/ready": RouteFixture(),
    "GET /health/live": RouteFixture(),

    # ---- Chat endpoints ----
    "POST /api/chat": RouteFixture(
        method="POST",
        json={"message": "hello", "session_id": "smoke-test"},
    ),
    "POST /api/chat/fallback": RouteFixture(
        method="POST",
        json={"message": "hello", "session_id": "smoke-test"},
    ),
    "POST /api/chat/clear": RouteFixture(
        method="POST",
        json={"session_id": "smoke-test"},
    ),

    # ---- Demo endpoints ----
    "GET /api/demo/status": RouteFixture(),
    "POST /api/demo/reset": RouteFixture(method="POST"),

    # ---- Fleet / Data endpoints ----
    "GET /api/fleet/summary": RouteFixture(),
    "GET /api/fleet/trucks": RouteFixture(),
    "GET /api/fleet/trucks/{truck_id}": RouteFixture(
        path_params={"truck_id": "TRUCK-001"},
    ),
    "GET /api/fleet/assets": RouteFixture(),
    "POST /api/fleet/assets": RouteFixture(
        method="POST",
        json={
            "asset_type": "truck",
            "asset_subtype": "flatbed",
            "name": "Smoke Test Truck",
            "status": "active",
        },
    ),
    "GET /api/fleet/assets/{asset_id}": RouteFixture(
        path_params={"asset_id": "ASSET-001"},
    ),
    "PATCH /api/fleet/assets/{asset_id}": RouteFixture(
        method="PATCH",
        path_params={"asset_id": "ASSET-001"},
        json={"name": "Updated"},
    ),
    "GET /api/inventory": RouteFixture(),
    "GET /api/orders": RouteFixture(),
    "GET /api/support/tickets": RouteFixture(),

    # ---- Analytics endpoints ----
    "GET /api/analytics/metrics": RouteFixture(),
    "GET /api/analytics/routes": RouteFixture(),
    "GET /api/analytics/delay-causes": RouteFixture(),
    "GET /api/analytics/regional": RouteFixture(),
    "GET /api/analytics/time-series": RouteFixture(),
    "GET /api/search": RouteFixture(params={"q": "test"}),

    # ---- Upload endpoints ----
    "POST /api/upload/batch": RouteFixture(
        method="POST",
        json={"data_type": "trucks", "records": []},
    ),
    "POST /api/upload/selective": RouteFixture(
        method="POST",
        json={"data_type": "trucks", "records": [], "fields": ["name"]},
    ),
    "POST /api/upload/sheets": RouteFixture(
        method="POST",
        json={"data_type": "trucks", "records": []},
    ),
    # POST /api/upload/csv — multipart, skip (will get 422)
    # POST /api/data/upload/csv — multipart, skip
    "POST /api/data/upload/sheets": RouteFixture(
        method="POST",
        json={"data_type": "trucks", "records": []},
    ),
    "POST /api/data/cleanup": RouteFixture(method="POST"),

    # ---- Location endpoints ----
    "POST /api/locations/webhook": RouteFixture(
        method="POST",
        json={"truck_id": "TRUCK-001", "latitude": 37.7, "longitude": -122.4},
    ),
    "POST /api/locations/batch": RouteFixture(
        method="POST",
        json=[{"truck_id": "TRUCK-001", "latitude": 37.7, "longitude": -122.4}],
    ),

    # ---- Ops endpoints ----
    "GET /api/ops/shipments": RouteFixture(),
    "GET /api/ops/shipments/{shipment_id}": RouteFixture(
        path_params={"shipment_id": "SHP-001"},
    ),
    "GET /api/ops/shipments/sla-breaches": RouteFixture(),
    "GET /api/ops/shipments/failures": RouteFixture(),
    "GET /api/ops/riders": RouteFixture(),
    "GET /api/ops/riders/utilization": RouteFixture(),
    "GET /api/ops/riders/{rider_id}": RouteFixture(
        path_params={"rider_id": "RDR-001"},
    ),
    "GET /api/ops/events": RouteFixture(),
    "GET /api/ops/metrics/shipments": RouteFixture(),
    "GET /api/ops/metrics/sla": RouteFixture(),
    "GET /api/ops/metrics/riders": RouteFixture(),
    "GET /api/ops/metrics/failures": RouteFixture(),
    "GET /api/ops/metrics/prometheus": RouteFixture(),
    "GET /api/ops/monitoring/ingestion": RouteFixture(),
    "GET /api/ops/monitoring/indexing": RouteFixture(),
    "GET /api/ops/monitoring/poison-queue": RouteFixture(),
    "GET /api/ops/replay/status/{job_id}": RouteFixture(
        path_params={"job_id": "JOB-001"},
    ),
    "POST /api/ops/replay/trigger": RouteFixture(
        method="POST",
        json={"tenant_id": "smoke-tenant"},
    ),
    "POST /api/ops/drift/run": RouteFixture(
        method="POST",
        json={},
    ),
    "POST /api/ops/admin/feature-flags/{tenant_id}/enable": RouteFixture(
        method="POST",
        path_params={"tenant_id": "smoke-tenant"},
        json={"user_id": "smoke-user"},
    ),
    "POST /api/ops/admin/feature-flags/{tenant_id}/disable": RouteFixture(
        method="POST",
        path_params={"tenant_id": "smoke-tenant"},
        json={"user_id": "smoke-user"},
    ),
    "POST /api/ops/admin/feature-flags/{tenant_id}/rollback": RouteFixture(
        method="POST",
        path_params={"tenant_id": "smoke-tenant"},
        json={"user_id": "smoke-user"},
    ),

    # ---- Webhook endpoint ----
    "POST /webhooks/dinee": RouteFixture(
        method="POST",
        json={"event_type": "test"},
        headers={"X-Dinee-Signature": "invalid-sig"},
    ),

    # ---- Fuel endpoints ----
    "GET /api/fuel/stations": RouteFixture(),
    "POST /api/fuel/stations": RouteFixture(
        method="POST",
        json={"station_id": "S-001", "name": "Smoke Station"},
    ),
    "GET /api/fuel/stations/{station_id}": RouteFixture(
        path_params={"station_id": "S-001"},
    ),
    "PATCH /api/fuel/stations/{station_id}": RouteFixture(
        method="PATCH",
        path_params={"station_id": "S-001"},
        json={"name": "Updated Station"},
    ),
    "PATCH /api/fuel/stations/{station_id}/threshold": RouteFixture(
        method="PATCH",
        path_params={"station_id": "S-001"},
        json={"threshold_percent": 20},
    ),
    "POST /api/fuel/consumption": RouteFixture(
        method="POST",
        json={"station_id": "S-001", "amount_liters": 100},
    ),
    "POST /api/fuel/consumption/batch": RouteFixture(
        method="POST",
        json=[{"station_id": "S-001", "amount_liters": 100}],
    ),
    "POST /api/fuel/refill": RouteFixture(
        method="POST",
        json={"station_id": "S-001", "amount_liters": 500},
    ),
    "GET /api/fuel/alerts": RouteFixture(),
    "GET /api/fuel/metrics/consumption": RouteFixture(),
    "GET /api/fuel/metrics/efficiency": RouteFixture(),
    "GET /api/fuel/metrics/summary": RouteFixture(),

    # ---- Scheduling endpoints ----
    "POST /api/scheduling/jobs": RouteFixture(
        method="POST",
        json={"job_type": "delivery", "origin": "A", "destination": "B"},
    ),
    "GET /api/scheduling/jobs": RouteFixture(),
    "GET /api/scheduling/jobs/active": RouteFixture(),
    "GET /api/scheduling/jobs/delayed": RouteFixture(),
    "GET /api/scheduling/jobs/{job_id}": RouteFixture(
        path_params={"job_id": "JOB-001"},
    ),
    "GET /api/scheduling/jobs/{job_id}/events": RouteFixture(
        path_params={"job_id": "JOB-001"},
    ),
    "PATCH /api/scheduling/jobs/{job_id}/assign": RouteFixture(
        method="PATCH",
        path_params={"job_id": "JOB-001"},
        json={"asset_id": "TRUCK-001"},
    ),
    "PATCH /api/scheduling/jobs/{job_id}/reassign": RouteFixture(
        method="PATCH",
        path_params={"job_id": "JOB-001"},
        json={"asset_id": "TRUCK-002"},
    ),
    "PATCH /api/scheduling/jobs/{job_id}/status": RouteFixture(
        method="PATCH",
        path_params={"job_id": "JOB-001"},
        json={"status": "in_progress"},
    ),
    "GET /api/scheduling/jobs/{job_id}/cargo": RouteFixture(
        path_params={"job_id": "JOB-001"},
    ),
    "PATCH /api/scheduling/jobs/{job_id}/cargo": RouteFixture(
        method="PATCH",
        path_params={"job_id": "JOB-001"},
        json={"notes": "test"},
    ),
    "PATCH /api/scheduling/jobs/{job_id}/cargo/{item_id}/status": RouteFixture(
        method="PATCH",
        path_params={"job_id": "JOB-001", "item_id": "ITEM-001"},
        json={"status": "loaded"},
    ),
    "GET /api/scheduling/jobs/{job_id}/eta": RouteFixture(
        path_params={"job_id": "JOB-001"},
    ),
    "GET /api/scheduling/cargo/search": RouteFixture(),
    "GET /api/scheduling/metrics/jobs": RouteFixture(),
    "GET /api/scheduling/metrics/completion": RouteFixture(),
    "GET /api/scheduling/metrics/assets": RouteFixture(),
    "GET /api/scheduling/metrics/delays": RouteFixture(),

    # ---- Agent endpoints ----
    "GET /api/agent/approvals": RouteFixture(),
    "POST /api/agent/approvals/{action_id}/approve": RouteFixture(
        method="POST",
        path_params={"action_id": "APR-001"},
    ),
    "POST /api/agent/approvals/{action_id}/reject": RouteFixture(
        method="POST",
        path_params={"action_id": "APR-001"},
        json={"reason": "test rejection"},
    ),
    "GET /api/agent/activity": RouteFixture(),
    "GET /api/agent/activity/stats": RouteFixture(),
    "PATCH /api/agent/config/autonomy": RouteFixture(
        method="PATCH",
        json={"level": "supervised"},
    ),
    "GET /api/agent/memory": RouteFixture(),
    "DELETE /api/agent/memory/{memory_id}": RouteFixture(
        method="DELETE",
        path_params={"memory_id": "MEM-001"},
    ),
    "GET /api/agent/feedback": RouteFixture(),
    "GET /api/agent/feedback/stats": RouteFixture(),
    "GET /api/agent/health": RouteFixture(),
    "POST /api/agent/{agent_id}/pause": RouteFixture(
        method="POST",
        path_params={"agent_id": "delay_response"},
    ),
    "POST /api/agent/{agent_id}/resume": RouteFixture(
        method="POST",
        path_params={"agent_id": "delay_response"},
    ),
}


# ---------------------------------------------------------------------------
# WebSocket Fixtures
# ---------------------------------------------------------------------------

WS_FIXTURES: Dict[str, WSFixture] = {
    "/ws/ops": WSFixture(
        params={"token": ""},
        expects_confirmation=True,
    ),
    "/ws/scheduling": WSFixture(
        params={"subscriptions": ""},
        expects_confirmation=True,
    ),
    "/ws/agent-activity": WSFixture(
        expects_confirmation=True,
    ),
    "/api/fleet/live": WSFixture(
        expects_confirmation=True,
    ),
}


# ---------------------------------------------------------------------------
# Default path parameter replacements
# ---------------------------------------------------------------------------

DEFAULT_PATH_PARAMS: Dict[str, str] = {
    "truck_id": "TRUCK-001",
    "asset_id": "ASSET-001",
    "job_id": "JOB-001",
    "agent_id": "agent-001",
    "action_id": "APR-001",
    "approval_id": "APR-001",
    "station_id": "S-001",
    "shipment_id": "SHP-001",
    "rider_id": "RDR-001",
    "memory_id": "MEM-001",
    "tenant_id": "smoke-tenant",
    "item_id": "ITEM-001",
}


def resolve_path(path: str, fixture: Optional[RouteFixture] = None) -> str:
    """Replace path parameters with fixture values or defaults."""
    if "{" not in path:
        return path

    resolved = path
    params = {}
    if fixture and fixture.path_params:
        params.update(fixture.path_params)

    # Fill in any remaining params from defaults
    for param_name, default_value in DEFAULT_PATH_PARAMS.items():
        placeholder = "{" + param_name + "}"
        if placeholder in resolved:
            value = params.get(param_name, default_value)
            resolved = resolved.replace(placeholder, value)

    return resolved
