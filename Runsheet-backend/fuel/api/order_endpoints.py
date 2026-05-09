"""
REST API endpoints for the Fuel Order surface.

Exposes tenant-scoped endpoints for order CRUD, status transitions,
driver assignment, hold/release-hold, cancel, and bulk intake:

* ``POST /api/orders`` — dispatcher keyboard create (JWT, dispatcher|admin).
* ``POST /api/orders/bulk`` — batch upload ≤ 1000 rows (JWT, dispatcher|admin).
* ``GET /api/orders`` — tenant-scoped list with filters (JWT, any role).
* ``GET /api/orders/{order_id}`` — single order (JWT, any role).
* ``GET /api/orders/{order_id}/events`` — event timeline (JWT, any role).
* ``PATCH /api/orders/{order_id}/status`` — state-machine-guarded transition
  (JWT, dispatcher|admin).
* ``PATCH /api/orders/{order_id}/assign`` — driver assignment
  (JWT, dispatcher|admin).
* ``POST /api/orders/{order_id}/cancel`` — terminal cancel with reason
  (JWT, dispatcher|admin).
* ``POST /api/orders/{order_id}/hold`` — move to on_hold with hold_reason
  (JWT, dispatcher|admin).
* ``POST /api/orders/{order_id}/release-hold`` — re-run intake hooks and
  transition back to placed (JWT, dispatcher|admin).

Every handler depends on :func:`get_tenant_context`, role-gates as per
design §7, and raises through :mod:`errors.exceptions` factories only
(no raw ``HTTPException``).

Validates: Requirements 2.4, 2.5, 2.5.7, 2.5.8, 10.1.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from errors.exceptions import (
    insufficient_role,
    missing_client_event_id,
    missing_hold_reason,
    resource_not_found,
    validation_error,
)
from fuel.order_models import FuelOrder, FuelOrderEvent, OrderStatus
from fuel.order_state_machine import (
    assert_transition,
    assert_window_present_for_transition,
    is_terminal_status,
)
from fuel.services.order_id_generator import mint_event_id
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/orders", tags=["orders"])

ROUTER_AUTH_POLICY = "jwt_required"

BULK_UPLOAD_MAX_ROWS = 1000

# ---------------------------------------------------------------------------
# Module-level service references (set during app wiring)
# ---------------------------------------------------------------------------

_order_intake_pipeline: Any = None
_order_repository: Any = None
_driver_repository: Any = None
_driver_counter_service: Any = None


def configure_order_endpoints(
    *,
    order_intake_pipeline: Any,
    order_repository: Any,
    driver_repository: Any = None,
    driver_counter_service: Any = None,
) -> None:
    """Wire service dependencies into the order endpoints module.

    Called once during application startup (from ``bootstrap/fuel.py``).
    Tests inject fakes so the router can be exercised without ES.
    """
    global _order_intake_pipeline, _order_repository, _driver_repository, _driver_counter_service
    _order_intake_pipeline = order_intake_pipeline
    _order_repository = order_repository
    _driver_repository = driver_repository
    _driver_counter_service = driver_counter_service


def _get_pipeline():
    if _order_intake_pipeline is None:
        raise RuntimeError(
            "Order endpoints not configured. "
            "Call configure_order_endpoints() during startup."
        )
    return _order_intake_pipeline


def _get_repository():
    if _order_repository is None:
        raise RuntimeError(
            "Order endpoints not configured. "
            "Call configure_order_endpoints() during startup."
        )
    return _order_repository


def _get_driver_repository():
    if _driver_repository is None:
        raise RuntimeError(
            "Order endpoints not configured (driver_repository missing). "
            "Call configure_order_endpoints() during startup."
        )
    return _driver_repository


def _get_driver_counter_service():
    """Return the configured DriverCounterService or None (optional dep)."""
    return _driver_counter_service


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _require_write_role(tenant: TenantContext) -> None:
    """Raise ``insufficient_role`` if the caller is not dispatcher or admin."""
    roles = tenant.roles or []
    if "dispatcher" not in roles and "admin" not in roles:
        raise insufficient_role(
            message="This operation requires the dispatcher or admin role",
            details={"required_roles": ["dispatcher", "admin"]},
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    """Body for ``POST /api/orders``."""
    model_config = ConfigDict(extra="forbid")
    client_event_id: Optional[str] = None
    customer_id: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    ship_to_address: str = Field(..., min_length=1)
    ship_to_lat: float = Field(..., ge=-90.0, le=90.0)
    ship_to_lon: float = Field(..., ge=-180.0, le=180.0)
    customer_tank_id: Optional[str] = None
    product_code: str = Field(..., min_length=1)
    gallons_requested: Optional[float] = Field(default=None, gt=0)
    fill_to_full: bool = False
    call_type: str = Field(..., min_length=1)
    delivery_window_start: Optional[str] = None
    delivery_window_end: Optional[str] = None
    po_number: Optional[str] = None
    special_instructions: Optional[str] = None
    schema_version: str = Field(default="1.0")


class BulkOrderRow(BaseModel):
    """A single row in a bulk upload request."""
    model_config = ConfigDict(extra="forbid")
    client_event_id: Optional[str] = None
    customer_id: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    ship_to_address: str = Field(..., min_length=1)
    ship_to_lat: float = Field(..., ge=-90.0, le=90.0)
    ship_to_lon: float = Field(..., ge=-180.0, le=180.0)
    customer_tank_id: Optional[str] = None
    product_code: str = Field(..., min_length=1)
    gallons_requested: Optional[float] = Field(default=None, gt=0)
    fill_to_full: bool = False
    call_type: str = Field(..., min_length=1)
    delivery_window_start: Optional[str] = None
    delivery_window_end: Optional[str] = None
    po_number: Optional[str] = None
    special_instructions: Optional[str] = None
    schema_version: str = Field(default="1.0")


class BulkOrderRequest(BaseModel):
    """Body for ``POST /api/orders/bulk``."""
    model_config = ConfigDict(extra="forbid")
    orders: List[BulkOrderRow] = Field(..., min_length=1)
    dry_run: bool = False


class BulkOrderResultItem(BaseModel):
    """Result for a single row in a bulk upload."""
    model_config = ConfigDict(extra="forbid")
    row_index: int
    order_id: Optional[str] = None
    event_id: Optional[str] = None
    status: str
    error: Optional[str] = None


class BulkOrderResponse(BaseModel):
    """Response for ``POST /api/orders/bulk``."""
    model_config = ConfigDict(extra="forbid")
    total: int
    processed: int
    duplicates: int
    errors: int
    dry_run: bool
    results: List[BulkOrderResultItem]


class IntakeResultResponse(BaseModel):
    """Response for POST /api/orders (mirrors pipeline IntakeResponse)."""
    model_config = ConfigDict(extra="forbid")
    event_id: str
    status: str
    order_id: Optional[str] = None


class OrderResponse(BaseModel):
    """Response shape for a single FuelOrder."""
    model_config = ConfigDict(extra="forbid")
    order_id: str
    tenant_id: str
    customer_id: str
    customer_name: str
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    ship_to_address: str
    ship_to_lat: float
    ship_to_lon: float
    customer_tank_id: Optional[str] = None
    product_code: Optional[str] = None
    gallons_requested: Optional[float] = None
    fill_to_full: bool = False
    call_type: str
    delivery_window_start: Optional[str] = None
    delivery_window_end: Optional[str] = None
    hold_reason: Optional[str] = None
    po_number: Optional[str] = None
    special_instructions: Optional[str] = None
    intake_channel: str
    intake_channel_id: str
    status: str
    assigned_driver_id: Optional[str] = None
    assigned_run_id: Optional[str] = None
    source_schema_version: str
    trace_id: str
    created_at: str
    updated_at: str
    last_event_timestamp: str

    @classmethod
    def from_model(cls, order: FuelOrder) -> "OrderResponse":
        dumped = order.model_dump(mode="json")
        return cls(
            order_id=dumped["order_id"],
            tenant_id=dumped["tenant_id"],
            customer_id=dumped["customer_id"],
            customer_name=dumped["customer_name"],
            customer_phone=dumped.get("customer_phone"),
            customer_email=dumped.get("customer_email"),
            ship_to_address=dumped["ship_to_address"],
            ship_to_lat=dumped["ship_to_lat"],
            ship_to_lon=dumped["ship_to_lon"],
            customer_tank_id=dumped.get("customer_tank_id"),
            product_code=dumped.get("product_code"),
            gallons_requested=dumped.get("gallons_requested"),
            fill_to_full=dumped.get("fill_to_full", False),
            call_type=dumped["call_type"],
            delivery_window_start=dumped.get("delivery_window_start"),
            delivery_window_end=dumped.get("delivery_window_end"),
            hold_reason=dumped.get("hold_reason"),
            po_number=dumped.get("po_number"),
            special_instructions=dumped.get("special_instructions"),
            intake_channel=dumped["intake_channel"],
            intake_channel_id=dumped["intake_channel_id"],
            status=dumped["status"],
            assigned_driver_id=dumped.get("assigned_driver_id"),
            assigned_run_id=dumped.get("assigned_run_id"),
            source_schema_version=dumped["source_schema_version"],
            trace_id=dumped["trace_id"],
            created_at=dumped["created_at"],
            updated_at=dumped["updated_at"],
            last_event_timestamp=dumped["last_event_timestamp"],
        )


class OrderListResponse(BaseModel):
    """Envelope for ``GET /api/orders``."""
    model_config = ConfigDict(extra="forbid")
    items: List[OrderResponse]
    total: int
    page: int
    size: int


class OrderEventResponse(BaseModel):
    """Response shape for a single FuelOrderEvent."""
    model_config = ConfigDict(extra="forbid")
    event_id: str
    order_id: str
    tenant_id: str
    event_type: str
    event_payload: Optional[Dict[str, Any]] = None
    event_timestamp: str
    ingested_at: str
    source_schema_version: str
    trace_id: str

    @classmethod
    def from_model(cls, event: FuelOrderEvent) -> "OrderEventResponse":
        dumped = event.model_dump(mode="json")
        return cls(
            event_id=dumped["event_id"],
            order_id=dumped["order_id"],
            tenant_id=dumped["tenant_id"],
            event_type=dumped["event_type"],
            event_payload=dumped.get("event_payload"),
            event_timestamp=dumped["event_timestamp"],
            ingested_at=dumped["ingested_at"],
            source_schema_version=dumped["source_schema_version"],
            trace_id=dumped["trace_id"],
        )


class OrderEventsListResponse(BaseModel):
    """Envelope for ``GET /api/orders/{order_id}/events``."""
    model_config = ConfigDict(extra="forbid")
    items: List[OrderEventResponse]
    total: int


class StatusTransitionRequest(BaseModel):
    """Body for ``PATCH /api/orders/{order_id}/status``."""
    model_config = ConfigDict(extra="forbid")
    new_status: str = Field(..., min_length=1)
    reason: Optional[str] = None
    notes: Optional[str] = None


class AssignDriverRequest(BaseModel):
    """Body for ``PATCH /api/orders/{order_id}/assign``."""
    model_config = ConfigDict(extra="forbid")
    driver_id: str = Field(..., min_length=1)


class CancelOrderRequest(BaseModel):
    """Body for ``POST /api/orders/{order_id}/cancel``."""
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=1)
    notes: Optional[str] = None


class HoldOrderRequest(BaseModel):
    """Body for ``POST /api/orders/{order_id}/hold``."""
    model_config = ConfigDict(extra="forbid")
    hold_reason: str = Field(..., min_length=1)


class ReleaseHoldRequest(BaseModel):
    """Body for ``POST /api/orders/{order_id}/release-hold``."""
    model_config = ConfigDict(extra="forbid")
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Release-hold hook registry
# ---------------------------------------------------------------------------

_release_hold_hooks: List[Any] = []


def register_release_hold_hook(hook) -> None:
    """Register an intake hook that runs on release-hold.

    Each hook is an async callable with signature:
        async def hook(order: dict, tenant: TenantContext) -> Optional[str]

    Returns None on pass, or a string hold_reason on failure.
    """
    _release_hold_hooks.append(hook)


# ---------------------------------------------------------------------------
# POST /api/orders (Req 2.4)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# POST /api/orders (Req 2.4)
# ---------------------------------------------------------------------------


@router.post("", response_model=IntakeResultResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    body: CreateOrderRequest,
    request: Request,
    response: Response,
    tenant: TenantContext = Depends(get_tenant_context),
) -> IntakeResultResponse:
    """Create a new fuel order via the dispatcher keyboard.

    Requires ``client_event_id`` in the body for idempotency. Rejects
    with 400 ``missing_client_event_id`` when missing.
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.4.
    """
    _require_write_role(tenant)

    if not body.client_event_id:
        raise missing_client_event_id(details={"field": "client_event_id"})

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    pipeline = _get_pipeline()
    payload = body.model_dump(exclude={"client_event_id"}, exclude_none=True)

    result = await pipeline.ingest_dispatcher(
        tenant=tenant,
        payload=payload,
        request_id=request_id,
        client_event_id=body.client_event_id,
    )

    if result.order_id:
        response.headers["Location"] = f"/api/orders/{result.order_id}"

    return IntakeResultResponse(
        event_id=result.event_id,
        status=result.status,
        order_id=result.order_id,
    )


# ---------------------------------------------------------------------------
# POST /api/orders/bulk (Req 2.4)
# ---------------------------------------------------------------------------


@router.post("/bulk", response_model=BulkOrderResponse, status_code=status.HTTP_200_OK)
async def create_orders_bulk(
    body: BulkOrderRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> BulkOrderResponse:
    """Bulk-create fuel orders (up to 1000 rows).

    Supports ``dry_run`` mode which validates all rows without persisting.
    Enforces the 1000-row cap — rejects with 400 when exceeded.
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.4.
    """
    _require_write_role(tenant)

    if len(body.orders) > BULK_UPLOAD_MAX_ROWS:
        raise validation_error(
            message=(
                f"Bulk upload exceeds the maximum of {BULK_UPLOAD_MAX_ROWS} rows "
                f"(received {len(body.orders)})"
            ),
            details={"max_rows": BULK_UPLOAD_MAX_ROWS, "received_rows": len(body.orders)},
        )

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    pipeline = _get_pipeline()
    results: List[BulkOrderResultItem] = []
    processed_count = 0
    duplicate_count = 0
    error_count = 0

    for idx, row in enumerate(body.orders):
        client_event_id = row.client_event_id or f"bulk_{request_id}_{idx}"

        if body.dry_run:
            try:
                row.model_dump(exclude={"client_event_id"}, exclude_none=True)
                results.append(BulkOrderResultItem(
                    row_index=idx, order_id=None, event_id=client_event_id,
                    status="dry_run_valid", error=None,
                ))
                processed_count += 1
            except Exception as exc:
                results.append(BulkOrderResultItem(
                    row_index=idx, order_id=None, event_id=client_event_id,
                    status="error", error=str(exc),
                ))
                error_count += 1
        else:
            try:
                payload = row.model_dump(exclude={"client_event_id"}, exclude_none=True)
                result = await pipeline.ingest_dispatcher(
                    tenant=tenant,
                    payload=payload,
                    request_id=f"{request_id}_row_{idx}",
                    client_event_id=client_event_id,
                )
                if result.status == "duplicate":
                    duplicate_count += 1
                    results.append(BulkOrderResultItem(
                        row_index=idx, order_id=None, event_id=result.event_id,
                        status="duplicate", error=None,
                    ))
                else:
                    processed_count += 1
                    results.append(BulkOrderResultItem(
                        row_index=idx, order_id=result.order_id,
                        event_id=result.event_id, status="processed", error=None,
                    ))
            except Exception as exc:
                error_count += 1
                results.append(BulkOrderResultItem(
                    row_index=idx, order_id=None, event_id=client_event_id,
                    status="error", error=str(exc),
                ))
                logger.warning(
                    "order_endpoints.bulk: row %d failed for tenant=%s: %s",
                    idx, tenant.tenant_id, exc,
                )

    return BulkOrderResponse(
        total=len(body.orders), processed=processed_count,
        duplicates=duplicate_count, errors=error_count,
        dry_run=body.dry_run, results=results,
    )


# ---------------------------------------------------------------------------
# GET /api/orders (Req 2.5)
# ---------------------------------------------------------------------------


@router.get("", response_model=OrderListResponse)
async def list_orders(
    tenant: TenantContext = Depends(get_tenant_context),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    customer_id: Optional[str] = Query(default=None),
    driver_id: Optional[str] = Query(default=None),
    call_type: Optional[str] = Query(default=None),
    product_code: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    intake_channel: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    sort: Optional[str] = Query(default=None),
) -> OrderListResponse:
    """List fuel orders for the tenant with optional filters.
    Any authenticated role can read orders.
    Validates: Requirement 2.5.
    """
    repo = _get_repository()
    result = await repo.search(
        tenant_id=tenant.tenant_id,
        status=status_filter,
        customer_id=customer_id,
        driver_id=driver_id,
        call_type=call_type,
        product_code=product_code,
        start_date=start_date,
        end_date=end_date,
        intake_channel=intake_channel,
        page=page,
        size=size,
        sort=sort,
    )
    items = [OrderResponse.from_model(o) for o in result["orders"]]
    return OrderListResponse(
        items=items, total=result["total"],
        page=result["page"], size=result["size"],
    )


# ---------------------------------------------------------------------------
# GET /api/orders/{order_id} (Req 2.5)
# ---------------------------------------------------------------------------


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderResponse:
    """Fetch a single fuel order by ID.
    Returns 404 on missing or cross-tenant access.
    Any authenticated role can read orders.
    Validates: Requirement 2.5.
    """
    repo = _get_repository()
    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )
    return OrderResponse.from_model(order)


# ---------------------------------------------------------------------------
# GET /api/orders/{order_id}/events (Req 2.5)
# ---------------------------------------------------------------------------


@router.get("/{order_id}/events", response_model=OrderEventsListResponse)
async def get_order_events(
    order_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderEventsListResponse:
    """Fetch the event timeline for an order.
    Returns 404 if the order does not exist or belongs to another tenant.
    Any authenticated role can read events.
    Validates: Requirement 2.5.
    """
    repo = _get_repository()
    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )
    events = await repo.get_events_for_order(tenant.tenant_id, order_id)
    items = [OrderEventResponse.from_model(ev) for ev in events]
    return OrderEventsListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# PATCH /api/orders/{order_id}/status (Req 2.5)
# ---------------------------------------------------------------------------


@router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: str,
    body: StatusTransitionRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderResponse:
    """Apply a state-machine-guarded status transition.

    Validates the transition against the order state machine. Rejects
    invalid transitions with 409 ``invalid_status_transition``.
    Rejects transitions to scheduled/dispatched/in_transit without a
    delivery window with 409 ``missing_delivery_window``.
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.5.
    """
    _require_write_role(tenant)
    repo = _get_repository()

    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )

    assert_transition(order.status, body.new_status)
    order_dict = order.model_dump(mode="json")
    assert_window_present_for_transition(order_dict, body.new_status)

    now = utcnow()
    update_fields: Dict[str, Any] = {
        "status": body.new_status,
        "updated_at": now.isoformat(),
        "last_event_timestamp": now.isoformat(),
    }
    if order.status == "on_hold" and body.new_status != "on_hold":
        update_fields["hold_reason"] = None

    await repo._es.update_document(repo._orders_index, order_id, update_fields)

    event_doc = {
        "event_id": mint_event_id(),
        "order_id": order_id,
        "tenant_id": tenant.tenant_id,
        "event_type": f"order_{body.new_status}",
        "event_payload": {
            "old_status": order.status,
            "new_status": body.new_status,
            "reason": body.reason,
            "notes": body.notes,
            "actor_user_id": tenant.user_id,
        },
        "event_timestamp": now.isoformat(),
        "ingested_at": now.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": str(uuid.uuid4()),
    }
    await repo.append_event(tenant.tenant_id, event_doc)

    # Update driver counters on transitions that affect workload
    # (dispatched|in_transit → delivered|failed|cancelled)
    counter_svc = _get_driver_counter_service()
    if counter_svc is not None and order.assigned_driver_id:
        old_status = order.status
        new_status = body.new_status
        _DECREMENT_ACTIVE = {
            ("dispatched", "delivered"),
            ("dispatched", "failed"),
            ("dispatched", "cancelled"),
            ("in_transit", "delivered"),
            ("in_transit", "failed"),
        }
        _INCREMENT_COMPLETED = {
            ("dispatched", "delivered"),
            ("in_transit", "delivered"),
        }
        transition = (old_status, new_status)
        delta_active = -1 if transition in _DECREMENT_ACTIVE else 0
        delta_completed = 1 if transition in _INCREMENT_COMPLETED else 0

        if delta_active != 0 or delta_completed != 0:
            try:
                await counter_svc.increment_counters(
                    driver_id=order.assigned_driver_id,
                    tenant_id=tenant.tenant_id,
                    delta_active=delta_active,
                    delta_completed=delta_completed,
                )
            except Exception as exc:
                # Counter failures MUST NOT block the main path
                logger.warning(
                    "order_endpoints.status: counter update failed for "
                    "driver=%s, order=%s: %s",
                    order.assigned_driver_id,
                    order_id,
                    exc,
                )

    updated_order = await repo.get(tenant.tenant_id, order_id)
    if updated_order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found after update",
            details={"order_id": order_id},
        )
    return OrderResponse.from_model(updated_order)


# ---------------------------------------------------------------------------
# PATCH /api/orders/{order_id}/assign (Req 2.5)
# ---------------------------------------------------------------------------


@router.patch("/{order_id}/assign", response_model=OrderResponse)
async def assign_driver(
    order_id: str,
    body: AssignDriverRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderResponse:
    """Assign a driver to an order.

    Validates that the driver exists and is available (not off_duty or
    inactive). Rejects with 409 ``driver_unavailable`` when the driver
    cannot be assigned.
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.5.
    """
    _require_write_role(tenant)
    repo = _get_repository()

    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )

    if is_terminal_status(order.status):
        from errors.exceptions import conflict
        raise conflict(
            message=f"Cannot assign driver to order in terminal status '{order.status}'",
            error_code="INVALID_STATUS_TRANSITION",
            details={"order_id": order_id, "status": order.status},
        )

    # Always validate driver availability before assignment (Req 3.1.5)
    driver_repo = _get_driver_repository()
    driver = await driver_repo.get(tenant.tenant_id, body.driver_id)
    if driver is None:
        raise resource_not_found(
            message=f"Driver '{body.driver_id}' not found",
            details={"driver_id": body.driver_id},
        )
    if driver.status in ("off_duty", "inactive"):
        from errors.exceptions import driver_unavailable
        raise driver_unavailable(
            message=f"Driver '{body.driver_id}' is {driver.status} and cannot be assigned",
            details={"driver_id": body.driver_id, "driver_status": driver.status},
        )

    now = utcnow()
    update_fields: Dict[str, Any] = {
        "assigned_driver_id": body.driver_id,
        "updated_at": now.isoformat(),
        "last_event_timestamp": now.isoformat(),
    }
    await repo._es.update_document(repo._orders_index, order_id, update_fields)

    event_doc = {
        "event_id": mint_event_id(),
        "order_id": order_id,
        "tenant_id": tenant.tenant_id,
        "event_type": "order_assigned",
        "event_payload": {"driver_id": body.driver_id, "actor_user_id": tenant.user_id},
        "event_timestamp": now.isoformat(),
        "ingested_at": now.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": str(uuid.uuid4()),
    }
    await repo.append_event(tenant.tenant_id, event_doc)

    # Increment driver's active_order_count on successful assignment
    counter_svc = _get_driver_counter_service()
    if counter_svc is not None:
        try:
            await counter_svc.increment_counters(
                driver_id=body.driver_id,
                tenant_id=tenant.tenant_id,
                delta_active=1,
            )
        except Exception as exc:
            # Counter failures MUST NOT block the main path
            logger.warning(
                "order_endpoints.assign: counter increment failed for "
                "driver=%s, order=%s: %s",
                body.driver_id,
                order_id,
                exc,
            )

    updated_order = await repo.get(tenant.tenant_id, order_id)
    if updated_order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found after update",
            details={"order_id": order_id},
        )
    return OrderResponse.from_model(updated_order)


# ---------------------------------------------------------------------------
# POST /api/orders/{order_id}/cancel (Req 2.5)
# ---------------------------------------------------------------------------


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: str,
    body: CancelOrderRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderResponse:
    """Cancel an order with a required reason.

    Validates the transition via the state machine (only non-terminal
    statuses that allow ``cancelled`` as a target are accepted).
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.5.
    """
    _require_write_role(tenant)
    repo = _get_repository()

    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )

    assert_transition(order.status, "cancelled")

    now = utcnow()
    update_fields: Dict[str, Any] = {
        "status": "cancelled",
        "updated_at": now.isoformat(),
        "last_event_timestamp": now.isoformat(),
    }
    await repo._es.update_document(repo._orders_index, order_id, update_fields)

    event_doc = {
        "event_id": mint_event_id(),
        "order_id": order_id,
        "tenant_id": tenant.tenant_id,
        "event_type": "order_cancelled",
        "event_payload": {
            "old_status": order.status,
            "reason": body.reason,
            "notes": body.notes,
            "actor_user_id": tenant.user_id,
        },
        "event_timestamp": now.isoformat(),
        "ingested_at": now.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": str(uuid.uuid4()),
    }
    await repo.append_event(tenant.tenant_id, event_doc)

    # Decrement driver's active_order_count on cancel from dispatched
    counter_svc = _get_driver_counter_service()
    if counter_svc is not None and order.assigned_driver_id:
        if order.status in ("dispatched",):
            try:
                await counter_svc.increment_counters(
                    driver_id=order.assigned_driver_id,
                    tenant_id=tenant.tenant_id,
                    delta_active=-1,
                )
            except Exception as exc:
                logger.warning(
                    "order_endpoints.cancel: counter decrement failed for "
                    "driver=%s, order=%s: %s",
                    order.assigned_driver_id,
                    order_id,
                    exc,
                )

    updated_order = await repo.get(tenant.tenant_id, order_id)
    if updated_order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found after update",
            details={"order_id": order_id},
        )
    return OrderResponse.from_model(updated_order)


# ---------------------------------------------------------------------------
# POST /api/orders/{order_id}/hold (Req 2.5.7)
# ---------------------------------------------------------------------------


@router.post("/{order_id}/hold", response_model=OrderResponse)
async def hold_order(
    order_id: str,
    body: HoldOrderRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderResponse:
    """Place an order on hold with a required hold_reason.

    Validates the transition via the state machine (only statuses that
    allow ``on_hold`` as a target are accepted). Rejects with 400
    ``missing_hold_reason`` when hold_reason is empty.
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.5.7.
    """
    _require_write_role(tenant)
    repo = _get_repository()

    if not body.hold_reason or not body.hold_reason.strip():
        raise missing_hold_reason(details={"field": "hold_reason"})

    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )

    assert_transition(order.status, "on_hold")

    now = utcnow()
    update_fields: Dict[str, Any] = {
        "status": "on_hold",
        "hold_reason": body.hold_reason.strip(),
        "updated_at": now.isoformat(),
        "last_event_timestamp": now.isoformat(),
    }
    await repo._es.update_document(repo._orders_index, order_id, update_fields)

    event_doc = {
        "event_id": mint_event_id(),
        "order_id": order_id,
        "tenant_id": tenant.tenant_id,
        "event_type": "order_on_hold",
        "event_payload": {
            "old_status": order.status,
            "hold_reason": body.hold_reason.strip(),
            "actor_user_id": tenant.user_id,
        },
        "event_timestamp": now.isoformat(),
        "ingested_at": now.isoformat(),
        "source_schema_version": "1.0",
        "trace_id": str(uuid.uuid4()),
    }
    await repo.append_event(tenant.tenant_id, event_doc)

    updated_order = await repo.get(tenant.tenant_id, order_id)
    if updated_order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found after update",
            details={"order_id": order_id},
        )
    return OrderResponse.from_model(updated_order)


# ---------------------------------------------------------------------------
# POST /api/orders/{order_id}/release-hold (Req 2.5.8)
# ---------------------------------------------------------------------------


@router.post("/{order_id}/release-hold", response_model=OrderResponse)
async def release_hold_order(
    order_id: str,
    body: ReleaseHoldRequest,
    tenant: TenantContext = Depends(get_tenant_context),
) -> OrderResponse:
    """Release an order from hold by re-running intake hooks.

    Re-runs registered intake hooks (pricing, credit-check, etc.).
    If all hooks pass, transitions the order back to ``placed``.
    If any hook fails, the order remains ``on_hold`` with an updated
    ``hold_reason`` reflecting the failing check.
    Role-gate: dispatcher or admin.
    Validates: Requirement 2.5.8.
    """
    _require_write_role(tenant)
    repo = _get_repository()

    order = await repo.get(tenant.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found",
            details={"order_id": order_id},
        )

    if order.status != "on_hold":
        from errors.exceptions import conflict
        raise conflict(
            message=f"Order '{order_id}' is not on hold (current status: '{order.status}')",
            error_code="INVALID_STATUS_TRANSITION",
            details={
                "order_id": order_id,
                "current_status": order.status,
                "expected_status": "on_hold",
            },
        )

    assert_transition(order.status, "placed")

    # Re-run intake hooks
    order_dict = order.model_dump(mode="json")
    hook_failure_reason: Optional[str] = None

    for hook in _release_hold_hooks:
        try:
            hook_result = await hook(order_dict, tenant)
            if hook_result is not None:
                hook_failure_reason = hook_result
                break
        except Exception as exc:
            logger.warning(
                "order_endpoints.release_hold: hook failed for order=%s: %s",
                order_id, exc,
            )
            hook_failure_reason = f"Hook failed: {exc}"
            break

    now = utcnow()

    if hook_failure_reason:
        # Hooks failed — remain on_hold with updated reason
        update_fields: Dict[str, Any] = {
            "hold_reason": hook_failure_reason,
            "updated_at": now.isoformat(),
            "last_event_timestamp": now.isoformat(),
        }
        await repo._es.update_document(repo._orders_index, order_id, update_fields)

        event_doc = {
            "event_id": mint_event_id(),
            "order_id": order_id,
            "tenant_id": tenant.tenant_id,
            "event_type": "order_release_hold_failed",
            "event_payload": {
                "hold_reason": hook_failure_reason,
                "actor_user_id": tenant.user_id,
                "notes": body.notes,
            },
            "event_timestamp": now.isoformat(),
            "ingested_at": now.isoformat(),
            "source_schema_version": "1.0",
            "trace_id": str(uuid.uuid4()),
        }
        await repo.append_event(tenant.tenant_id, event_doc)
    else:
        # All hooks passed — transition back to placed
        update_fields = {
            "status": "placed",
            "hold_reason": None,
            "updated_at": now.isoformat(),
            "last_event_timestamp": now.isoformat(),
        }
        await repo._es.update_document(repo._orders_index, order_id, update_fields)

        event_doc = {
            "event_id": mint_event_id(),
            "order_id": order_id,
            "tenant_id": tenant.tenant_id,
            "event_type": "order_released_from_hold",
            "event_payload": {
                "old_status": "on_hold",
                "new_status": "placed",
                "actor_user_id": tenant.user_id,
                "notes": body.notes,
            },
            "event_timestamp": now.isoformat(),
            "ingested_at": now.isoformat(),
            "source_schema_version": "1.0",
            "trace_id": str(uuid.uuid4()),
        }
        await repo.append_event(tenant.tenant_id, event_doc)

    updated_order = await repo.get(tenant.tenant_id, order_id)
    if updated_order is None:
        raise resource_not_found(
            message=f"Order '{order_id}' not found after update",
            details={"order_id": order_id},
        )
    return OrderResponse.from_model(updated_order)
