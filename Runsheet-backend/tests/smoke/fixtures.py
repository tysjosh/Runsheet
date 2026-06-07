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
    "GET /api/support/tickets": RouteFixture(),

    # ---- Analytics endpoints ----
    "GET /api/analytics/metrics": RouteFixture(),
    "GET /api/analytics/routes": RouteFixture(),
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
    # Legacy ``/api/data/upload/*`` handlers were removed — they ignored
    # the payload and returned random counts. Real ingestion is
    # ``/api/import/upload/*``.
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

    # ---- Webhook endpoints ----
    "POST /webhooks/dinee": RouteFixture(
        method="POST",
        json={"event_type": "test"},
        headers={"X-Dinee-Signature": "invalid-sig"},
    ),
    # POST /webhooks/orders/{channel_id} — skip (HMAC required, no JWT)

    # ---- Order intake pipeline endpoints ----
    "POST /api/orders": RouteFixture(
        method="POST",
        json={
            "client_event_id": "smoke-evt-001",
            "customer_id": "CUST-001",
            "customer_name": "Smoke Customer",
            "ship_to_address": "123 Main St",
            "ship_to_lat": 32.7767,
            "ship_to_lon": -96.7970,
            "product_code": "DIESEL_2",
            "gallons_requested": 500,
            "call_type": "will_call",
        },
    ),
    "POST /api/orders/bulk": RouteFixture(
        method="POST",
        json={
            "orders": [
                {
                    "customer_id": "CUST-001",
                    "customer_name": "Smoke Customer",
                    "ship_to_address": "123 Main St",
                    "ship_to_lat": 32.7767,
                    "ship_to_lon": -96.7970,
                    "product_code": "DIESEL_2",
                    "gallons_requested": 500,
                    "call_type": "will_call",
                }
            ],
            "dry_run": True,
        },
    ),
    "GET /api/orders": RouteFixture(),
    "GET /api/orders/{order_id}": RouteFixture(
        path_params={"order_id": "ord_00000000000000000000000000000001"},
    ),
    "GET /api/orders/{order_id}/events": RouteFixture(
        path_params={"order_id": "ord_00000000000000000000000000000001"},
    ),
    "PATCH /api/orders/{order_id}/status": RouteFixture(
        method="PATCH",
        path_params={"order_id": "ord_00000000000000000000000000000001"},
        json={"new_status": "confirmed"},
    ),
    "PATCH /api/orders/{order_id}/assign": RouteFixture(
        method="PATCH",
        path_params={"order_id": "ord_00000000000000000000000000000001"},
        json={"driver_id": "DRV-001"},
    ),
    "POST /api/orders/{order_id}/cancel": RouteFixture(
        method="POST",
        path_params={"order_id": "ord_00000000000000000000000000000001"},
        json={"reason": "smoke test cancellation"},
    ),
    "POST /api/orders/{order_id}/hold": RouteFixture(
        method="POST",
        path_params={"order_id": "ord_00000000000000000000000000000001"},
        json={"hold_reason": "smoke test hold"},
    ),
    "POST /api/orders/{order_id}/release-hold": RouteFixture(
        method="POST",
        path_params={"order_id": "ord_00000000000000000000000000000001"},
        json={},
    ),

    # ---- Driver endpoints (order-intake-pipeline) ----
    "GET /api/ops/drivers": RouteFixture(),
    "GET /api/ops/drivers/utilization": RouteFixture(),
    "GET /api/ops/drivers/{driver_id}": RouteFixture(
        path_params={"driver_id": "DRV-001"},
    ),
    "POST /api/ops/drivers": RouteFixture(
        method="POST",
        json={
            "driver_name": "Smoke Driver",
            "phone": "+15551234567",
            "status": "active",
            "availability": "on_duty",
        },
    ),
    "PATCH /api/ops/drivers/{driver_id}": RouteFixture(
        method="PATCH",
        path_params={"driver_id": "DRV-001"},
        json={"driver_name": "Updated Driver"},
    ),

    # ---- Intake channel admin endpoints ----
    "POST /api/integrations/intake-channels": RouteFixture(
        method="POST",
        json={
            "channel_id": "smoke-channel-01",
            "channel_type": "api_partner",
            "display_name": "Smoke Test Channel",
            "supported_schema_versions": ["1.0"],
        },
    ),
    "GET /api/integrations/intake-channels": RouteFixture(),
    "PATCH /api/integrations/intake-channels/{channel_id}": RouteFixture(
        method="PATCH",
        path_params={"channel_id": "smoke-channel-01"},
        json={"display_name": "Updated Channel"},
    ),
    "DELETE /api/integrations/intake-channels/{channel_id}": RouteFixture(
        method="DELETE",
        path_params={"channel_id": "smoke-channel-01"},
    ),
    "POST /api/integrations/intake-channels/{channel_id}/rotate-secret": RouteFixture(
        method="POST",
        path_params={"channel_id": "smoke-channel-01"},
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

    # ---- Read-only (GET) coverage for commerce / compliance / fuel /
    # inventory / notifications / integrations / import surfaces. These need
    # no request body; path-templated routes carry a placeholder id so
    # resolve_path produces a concrete URL. ----
    "GET /api/commerce/accounts": RouteFixture(),
    "GET /api/commerce/accounts/{account_id}": RouteFixture(
        path_params={"account_id": "acc_001"},
    ),
    "GET /api/commerce/accounts/{account_id}/aging": RouteFixture(
        path_params={"account_id": "acc_001"},
    ),
    "GET /api/commerce/ar-aging": RouteFixture(),
    "GET /api/commerce/ar-aging/history": RouteFixture(),
    "GET /api/commerce/customers": RouteFixture(),
    "GET /api/commerce/customers/{customer_id}": RouteFixture(
        path_params={"customer_id": "CUST-001"},
    ),
    "GET /api/commerce/invoices": RouteFixture(),
    "GET /api/commerce/invoices/{invoice_id}": RouteFixture(
        path_params={"invoice_id": "inv_001"},
    ),
    "GET /api/commerce/invoices/{invoice_id}/events": RouteFixture(
        path_params={"invoice_id": "inv_001"},
    ),
    "GET /api/commerce/payments": RouteFixture(),
    "GET /api/commerce/payments/{payment_id}": RouteFixture(
        path_params={"payment_id": "pay_001"},
    ),
    "GET /api/commerce/price-books": RouteFixture(),
    "GET /api/commerce/price-books/{price_book_id}": RouteFixture(
        path_params={"price_book_id": "pb_001"},
    ),
    "GET /api/commerce/pricing-rules": RouteFixture(),
    "GET /api/compliance/asset-certifications": RouteFixture(),
    "GET /api/compliance/asset-certifications/dashboard": RouteFixture(),
    "GET /api/compliance/asset-certifications/{cert_id}": RouteFixture(
        path_params={"cert_id": "CERT-001"},
    ),
    "GET /api/compliance/drivers": RouteFixture(),
    "GET /api/compliance/drivers/dashboard": RouteFixture(),
    "GET /api/compliance/drivers/{driver_id}": RouteFixture(
        path_params={"driver_id": "DRV-001"},
    ),
    "GET /api/compliance/ifta/report": RouteFixture(),
    "GET /api/compliance/ifta/completeness": RouteFixture(),
    "GET /api/compliance/kfactor/dashboard": RouteFixture(),
    "GET /api/compliance/meters": RouteFixture(),
    "GET /api/compliance/meters/{meter_id}": RouteFixture(
        path_params={"meter_id": "MTR-001"},
    ),
    "GET /api/compliance/tax-jurisdictions": RouteFixture(),
    "GET /api/compliance/terminal-bols": RouteFixture(),
    "GET /api/fuel/destinations": RouteFixture(),
    "GET /api/fuel/products": RouteFixture(),
    "GET /api/fuel/rack-prices": RouteFixture(),
    "GET /api/fuel/supplier-contracts": RouteFixture(),
    "GET /api/fuel/terminals": RouteFixture(),
    "GET /api/fuel/terminals/{terminal_id}": RouteFixture(
        path_params={"terminal_id": "TERM-001"},
    ),
    "GET /api/fuel/terminals/{terminal_id}/wait-summary": RouteFixture(
        path_params={"terminal_id": "TERM-001"},
    ),
    "GET /api/fuel/mvp/customer-tanks": RouteFixture(),
    "GET /api/fuel/mvp/customer-tanks/{customer_tank_id}": RouteFixture(
        path_params={"customer_tank_id": "TANK-001"},
    ),
    "GET /api/fuel/mvp/depots": RouteFixture(),
    "GET /api/fuel/mvp/depots/{depot_id}": RouteFixture(
        path_params={"depot_id": "DEP-001"},
    ),
    "GET /api/fuel/mvp/forecasts": RouteFixture(),
    "GET /api/fuel/mvp/plans": RouteFixture(),
    "GET /api/fuel/mvp/priorities": RouteFixture(),
    "GET /api/inventory/alerts": RouteFixture(),
    "GET /api/inventory/items": RouteFixture(),
    "GET /api/inventory/items/{item_id}": RouteFixture(
        path_params={"item_id": "ITEM-001"},
    ),
    "GET /api/inventory/summary": RouteFixture(),
    "GET /api/notifications": RouteFixture(),
    "GET /api/notifications/rules": RouteFixture(),
    "GET /api/notifications/summary": RouteFixture(),
    "GET /api/notifications/templates": RouteFixture(),
    "GET /api/integrations": RouteFixture(),
    "GET /api/integrations/providers": RouteFixture(),
    "GET /api/import/history": RouteFixture(),
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
    "/ws/notifications": WSFixture(
        expects_confirmation=True,
    ),
    "/ws/orders": WSFixture(
        params={"token": ""},
        expects_confirmation=True,
    ),
    "/ws/driver": WSFixture(
        params={"token": ""},
        expects_confirmation=False,
    ),
    # Plan-execution channel (Req 3.6, 3.9) — broadcasts driver check-ins
    # and stop completions. Connection confirmation is emitted by the
    # plan-execution WS manager only after the tenant guard accepts the
    # handshake, so we flag it as expected in this smoke test.
    "/ws/plan-execution": WSFixture(
        params={"token": ""},
        expects_confirmation=True,
    ),
    # Fuel-planning channel (Req 1.6.4) — customer_tank_forecast_ready,
    # emergency_stop_inserted, replan_diff_ready, and
    # sourcing_recommendation_ready events from the fuel-ops hardening
    # spec. Confirmation message is emitted by FuelPlanningWSManager on
    # connect, matching the envelope shape used by the other overlay
    # channels.
    "/ws/fuel-planning": WSFixture(
        params={"token": ""},
        expects_confirmation=True,
    ),
    # Tenant-scoped invoice state channel (Commerce design §6). Sends a
    # connection-confirmation envelope on connect, like the other channels.
    "/ws/commerce/invoices": WSFixture(
        params={"token": ""},
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
    "order_id": "ord_00000000000000000000000000000001",
    "driver_id": "DRV-001",
    "channel_id": "smoke-channel-01",
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
