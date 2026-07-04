"""
Surface B — Voice Read / Driver router.

Net-new REST endpoints the Dinee ``ws-server`` calls on behalf of a tenant to
read fuel-intake data, read order status, and drive driver operations. Every
endpoint is authenticated by the per-tenant API-key (Bearer) dependency
:func:`fuel.voice.voice_auth.get_voice_tenant` — deliberately distinct from the
SuperTokens ``get_tenant_context`` used elsewhere in the backend.

Tenant scoping discipline (Requirement 11): every handler passes
``voice.tenant_id`` (taken from the authenticated credential binding, never a
client-supplied header/query/path/body — Req 11.4) as the first argument to the
underlying repositories. Those repositories apply the ``inject_tenant_filter`` +
post-fetch re-validation pattern, so any cross-tenant identifier degrades to a
uniform HTTP 404 (Req 11.3) rather than leaking another tenant's data.

This module currently establishes the router scaffold and the credential-test
endpoint ``GET /auth/ping`` (Requirement 12). The customer/order/driver read and
write endpoints are added by later tasks (8.2 / 8.4 / 8.6) onto this same router;
the ``configure_voice_read_driver_router`` wiring below lets the bootstrap layer
inject the repositories those handlers will use.

Requirements: 11.1, 11.2, 11.3, 12.1, 12.2, 12.3
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from errors.exceptions import (
    invalid_request,
    resource_not_found,
    voice_payload_invalid,
)
from fuel.driver_report_models import DriverReport
from fuel.driver_report_repository import (
    DriverAssignmentNotFoundError,
    DriverReportCrossTenantAccessError,
)
from fuel.voice.voice_auth import VoiceTenantContext, get_voice_tenant

logger = logging.getLogger(__name__)

__all__ = [
    "router",
    "configure_voice_read_driver_router",
]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
#
# No prefix: the Dinee client posts to bare paths (``/auth/ping``,
# ``/customers/lookup``, ``/orders/{id}/status``, ...). The ``voice`` tag groups
# these endpoints in the OpenAPI schema.
router = APIRouter(tags=["voice-read-driver"])


# ---------------------------------------------------------------------------
# Module-level repository references, wired via
# configure_voice_read_driver_router() at bootstrap (task 10.1). Later tasks
# (8.2 / 8.4 / 8.6) read these to serve their handlers; task 8.1 only needs the
# authenticated ping, so all default to None.
# ---------------------------------------------------------------------------

_customer_service: Any = None
_delivery_destination_service: Any = None
_customer_tank_repository: Any = None
_fuel_order_repository: Any = None
_driver_repository: Any = None
_driver_report_repository: Any = None
_driver_pin_vault: Any = None
_product_catalog: Any = None


def configure_voice_read_driver_router(
    *,
    customer_service: Optional[Any] = None,
    delivery_destination_service: Optional[Any] = None,
    customer_tank_repository: Optional[Any] = None,
    fuel_order_repository: Optional[Any] = None,
    driver_repository: Optional[Any] = None,
    driver_report_repository: Optional[Any] = None,
    driver_pin_vault: Optional[Any] = None,
    product_catalog: Optional[Any] = None,
) -> None:
    """Wire the repositories/services the Surface B read/driver handlers use.

    Called from the bootstrap layer (task 10.1) once ES-backed repositories are
    available. Only the arguments supplied are updated so the wiring can be done
    incrementally as later tasks add their handlers.
    """
    global _customer_service, _delivery_destination_service
    global _customer_tank_repository, _fuel_order_repository
    global _driver_repository, _driver_report_repository, _driver_pin_vault
    global _product_catalog

    if customer_service is not None:
        _customer_service = customer_service
    if delivery_destination_service is not None:
        _delivery_destination_service = delivery_destination_service
    if customer_tank_repository is not None:
        _customer_tank_repository = customer_tank_repository
    if fuel_order_repository is not None:
        _fuel_order_repository = fuel_order_repository
    if driver_repository is not None:
        _driver_repository = driver_repository
    if driver_report_repository is not None:
        _driver_report_repository = driver_report_repository
    if driver_pin_vault is not None:
        _driver_pin_vault = driver_pin_vault
    if product_catalog is not None:
        _product_catalog = product_catalog


# ---------------------------------------------------------------------------
# Requirement 12 — credential test endpoint
# ---------------------------------------------------------------------------


@router.get("/auth/ping")
async def auth_ping(
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> dict:
    """Credential-test endpoint for the Dinee ``ws-server``.

    Returns HTTP 200 when the presented API key and ``X-Runsheet-Tenant`` header
    satisfy Requirement 10 (enforced by ``get_voice_tenant``). Authentication
    failures surface as 401/403 from the dependency (Req 12.2). No request body
    is required or read (Req 12.3); the response carries no tenant data or
    credential values.

    Validates: Requirements 12.1, 12.2, 12.3
    """
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Task 8.6 — Driver endpoints (verify, active-assignment, reports)
# ---------------------------------------------------------------------------
#
# Requirements: 19.1, 19.2, 19.3, 20.1, 20.2, 20.3, 21.1, 21.2, 21.3, 21.4, 21.5
#
# Every handler takes ``voice.tenant_id`` (from the credential binding, never a
# client-supplied value — Req 11.4) as the first argument to its repositories,
# so cross-tenant identifiers degrade to a uniform HTTP 404 (Req 11.3).

#: An active assignment is the tenant's order assigned to the driver whose
#: status is one of these (design "Active assignment", Req 20).
_ACTIVE_ASSIGNMENT_STATUSES = ("dispatched", "in_transit")

#: Closed set of accepted driver-report kinds (Req 21.1/21.2). Mirrors
#: ``fuel.driver_report_models.DriverReportKind``. Any other value — or an
#: absent value — is rejected with HTTP 422 before anything is persisted.
_VALID_REPORT_KINDS = ("delay", "terminal_wait", "exception", "note")


def _iso(value: Any) -> Any:
    """Return an ISO-8601 string for a datetime, else the value unchanged."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return value


class DriverReportRequest(BaseModel):
    """Request body for ``POST /drivers/{driverId}/assignments/{id}/reports``.

    Tolerant of extra Dinee fields. ``kind`` is validated against the closed
    set at the handler so an absent/invalid value returns a clean HTTP 422
    (Req 21.2) rather than a generic parse error, and nothing is persisted
    (Req 21.5). ``detail`` / ``etaMinutes`` are stored verbatim when supplied
    (Req 21.3).
    """

    model_config = ConfigDict(extra="ignore")

    kind: Optional[str] = None
    detail: Optional[str] = None
    etaMinutes: Optional[int] = Field(default=None, alias="etaMinutes")


# ---------------------------------------------------------------------------
# Requirement 19 — driver verification
# ---------------------------------------------------------------------------


@router.get("/drivers/verify")
async def verify_driver(
    voice: VoiceTenantContext = Depends(get_voice_tenant),
    phone: Optional[str] = Query(None),
    driverIdentifier: Optional[str] = Query(None),
    pin: Optional[str] = Query(None),
) -> dict:
    """Verify a driver's identity + PIN for the authenticated tenant.

    Resolves the driver within the tenant by ``driverIdentifier`` (driver id)
    and confirms the supplied ``phone`` matches, then constant-time-verifies the
    PIN via :class:`~fuel.voice.driver_pin.DriverPinVault`. Returns
    ``{driver, pinVerified: true}`` on a correct PIN (Req 19.1); returns
    ``{pinVerified: false}`` with **no** ``driver`` object on a wrong PIN
    (Req 19.2) or when no driver matches (Req 19.3). Always HTTP 200 — a
    non-match and a wrong PIN are indistinguishable, leaking nothing.

    Validates: Requirements 19.1, 19.2, 19.3
    """
    driver = None
    if driverIdentifier and _driver_repository is not None:
        driver = await _driver_repository.get(voice.tenant_id, driverIdentifier)

    # No matching driver, or the presented phone does not match the record →
    # uniform negative result with no driver leaked (Req 19.3).
    if driver is None or (phone is not None and driver.phone != phone):
        return {"pinVerified": False}

    verified = False
    if _driver_pin_vault is not None and pin:
        verified = await _driver_pin_vault.verify_pin(
            voice.tenant_id, driver.driver_id, pin
        )

    if not verified:
        # Wrong PIN (or no PIN on file) → omit the driver object (Req 19.2).
        return {"pinVerified": False}

    return {
        "driver": driver.model_dump(mode="json"),
        "pinVerified": True,
    }


# ---------------------------------------------------------------------------
# Requirement 20 — driver active assignment
# ---------------------------------------------------------------------------


@router.get("/drivers/{driver_id}/active-assignment")
async def get_active_assignment(
    driver_id: str,
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> dict:
    """Return the driver's active assignment, or ``null`` when there is none.

    A cross-tenant / unknown driver degrades to HTTP 404 (Req 20.3). When the
    driver is owned by the tenant, the active assignment is the tenant's order
    assigned to the driver whose status is ``dispatched`` or ``in_transit``
    (Req 20.1); absent one, ``{assignment: null}`` is returned with HTTP 200
    (Req 20.2). The assignment projects ``orderId``/``runId``/``status`` and the
    delivery window.

    Validates: Requirements 20.1, 20.2, 20.3
    """
    driver = None
    if _driver_repository is not None:
        driver = await _driver_repository.get(voice.tenant_id, driver_id)
    if driver is None:
        raise resource_not_found("Driver not found")

    orders: List[Any] = []
    if _fuel_order_repository is not None:
        result = await _fuel_order_repository.search(
            voice.tenant_id,
            driver_id=driver_id,
            sort="created_at:desc",
        )
        orders = result.get("orders", []) if isinstance(result, dict) else []

    active = next(
        (o for o in orders if getattr(o, "status", None) in _ACTIVE_ASSIGNMENT_STATUSES),
        None,
    )
    if active is None:
        return {"assignment": None}

    return {
        "assignment": {
            "orderId": active.order_id,
            "runId": active.assigned_run_id,
            "status": active.status,
            "deliveryWindowStart": _iso(active.delivery_window_start),
            "deliveryWindowEnd": _iso(active.delivery_window_end),
        }
    }


# ---------------------------------------------------------------------------
# Requirement 21 — driver report submission
# ---------------------------------------------------------------------------


@router.post("/drivers/{driver_id}/assignments/{assignment_id}/reports")
async def submit_driver_report(
    driver_id: str,
    assignment_id: str,
    body: DriverReportRequest,
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> dict:
    """Record a driver report against an assignment for the authenticated tenant.

    Validation order (nothing is persisted until every check passes, Req 21.5):
        1. ``kind`` present and in the closed set → else HTTP 422 (Req 21.2).
        2. The assignment references an order owned by the tenant *and* assigned
           to the driver → else HTTP 404 (Req 21.4), enforced by the repository
           raising :class:`DriverAssignmentNotFoundError`.

    On success persists the report scoped to the tenant and returns
    ``{recorded: true, reportId}`` (Req 21.1); ``detail`` / ``etaMinutes`` are
    stored verbatim when supplied (Req 21.3).

    Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5
    """
    # (1) kind validation — absent or out-of-set → 422, persist nothing.
    if not body.kind or body.kind not in _VALID_REPORT_KINDS:
        raise voice_payload_invalid(missing_fields=["kind"])

    report = DriverReport(
        report_id=uuid.uuid4().hex,
        tenant_id=voice.tenant_id,
        driver_id=driver_id,
        assignment_id=assignment_id,
        kind=body.kind,
        detail=body.detail,
        eta_minutes=body.etaMinutes,
    )

    # (2) assignment ownership — the repository validates the assignment is
    # owned by this tenant AND assigned to this driver before any write; a
    # miss raises DriverAssignmentNotFoundError → uniform 404 (Req 21.4/21.5).
    if _driver_report_repository is None:
        raise resource_not_found("Assignment not found")
    try:
        created = await _driver_report_repository.create(voice.tenant_id, report)
    except (DriverAssignmentNotFoundError, DriverReportCrossTenantAccessError):
        raise resource_not_found("Assignment not found")

    return {"recorded": True, "reportId": created.report_id}


# ===========================================================================
# Task 8.2 — Customer read endpoints (Fuel_Intake_Read_API)
#
# Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 14.1, 14.2, 14.3, 14.4,
#               15.1, 15.2, 15.3
#
# Every handler derives the tenant scope from ``voice.tenant_id`` (the
# credential binding, Req 11.4) and passes it as the first argument to the
# underlying service/repository so ``inject_tenant_filter`` + post-fetch
# re-validation applies. Cross-tenant/unknown customers therefore degrade to a
# uniform HTTP 404 (Req 14.3 / Req 11.3) rather than leaking another tenant's
# data.
# ===========================================================================


# ---------------------------------------------------------------------------
# Response models (design "Surface B response models")
# ---------------------------------------------------------------------------


class VoiceCustomer(BaseModel):
    """A customer projection returned by ``GET /customers/lookup`` (Req 13.3)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    phone: Optional[str] = None
    accountId: Optional[str] = None


class CustomerLookupResponse(BaseModel):
    """Envelope for ``GET /customers/lookup`` (Req 13.1/13.2/13.4)."""

    customers: List[VoiceCustomer]


class SitesResponse(BaseModel):
    """Envelope for ``GET /customers/{id}/sites`` (Req 14.1)."""

    sites: List[dict]


class TanksResponse(BaseModel):
    """Envelope for ``GET /customers/{id}/tanks`` (Req 14.2)."""

    tanks: List[dict]


class ProductValidateResponse(BaseModel):
    """Envelope for ``GET /products/validate`` (Req 15.1/15.2)."""

    valid: bool


# ---------------------------------------------------------------------------
# Wiring guard
# ---------------------------------------------------------------------------


def _require(dependency: Any, name: str) -> Any:
    """Return a wired dependency or raise a clean 500 if bootstrap missed it.

    Handlers depend on module-level references wired by
    :func:`configure_voice_read_driver_router`. Failing closed with a clear
    internal error (rather than an ``AttributeError`` on ``None``) keeps the
    rejection envelope free of tenant data or credential values (Req 10.6).
    """
    if dependency is None:
        from errors.exceptions import internal_error

        logger.error("voice_read_driver_router dependency %s is not wired", name)
        raise internal_error()
    return dependency


def _project_customer(source: dict) -> VoiceCustomer:
    """Map a ``customers_current`` source doc onto the response projection.

    ``id`` / ``name`` come from ``customer_id`` / ``display_name`` and are
    always present; ``phone`` / ``accountId`` are included only when the
    projected lookup fields carry a value (Req 13.3).
    """
    phone = source.get("phone")
    account_id = source.get("account_id")
    return VoiceCustomer(
        id=source.get("customer_id", ""),
        name=source.get("display_name", ""),
        phone=phone if phone else None,
        accountId=account_id if account_id else None,
    )


# ---------------------------------------------------------------------------
# Requirement 13 — customer lookup by phone or account
# ---------------------------------------------------------------------------


@router.get("/customers/lookup", response_model=CustomerLookupResponse)
async def customers_lookup(
    phone: Optional[str] = Query(None),
    accountId: Optional[str] = Query(None),
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> CustomerLookupResponse:
    """Look up the authenticated tenant's customers by phone or account.

    Supplying neither ``phone`` nor ``accountId`` is a client error (Req 13.5).
    A query that matches no customers returns an empty array with HTTP 200
    (Req 13.4). Each returned customer carries ``id``/``name`` and includes
    ``phone``/``accountId`` when stored (Req 13.3). Results are restricted to
    the authenticated tenant via the credential-bound ``voice.tenant_id``
    (Req 13.1/13.2, Req 11.1/11.4).

    Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5
    """
    phone_val = phone.strip() if phone and phone.strip() else None
    account_val = accountId.strip() if accountId and accountId.strip() else None

    if phone_val is None and account_val is None:
        # Req 13.5 — neither selector supplied.
        raise invalid_request(
            "customer lookup requires a phone or accountId query parameter",
        )

    service = _require(_customer_service, "customer_service")
    rows = await service.lookup_by_phone_or_account(
        voice.tenant_id,
        phone=phone_val,
        account_id=account_val,
    )

    return CustomerLookupResponse(
        customers=[_project_customer(row) for row in rows],
    )


# ---------------------------------------------------------------------------
# Requirement 14 — customer sites and tanks
# ---------------------------------------------------------------------------


@router.get("/customers/{customer_id}/sites", response_model=SitesResponse)
async def customer_sites(
    customer_id: str,
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> SitesResponse:
    """Return the delivery sites for a customer of the authenticated tenant.

    The customer is first resolved through the tenant-scoped
    :class:`CustomerService`; an unknown or cross-tenant customer raises a
    uniform HTTP 404 (Req 14.3). Sites are the tenant's delivery destinations
    scoped to this customer; a customer with no sites yields an empty array and
    HTTP 200 (Req 14.4).

    Validates: Requirements 14.1, 14.3, 14.4
    """
    service = _require(_customer_service, "customer_service")
    # Resolves within the tenant scope; raises resource_not_found (404) for an
    # unknown or cross-tenant customer.
    await service.get(voice.tenant_id, customer_id)

    destinations_service = _require(
        _delivery_destination_service, "delivery_destination_service"
    )
    destinations = await destinations_service.list(voice.tenant_id)
    sites = [
        dest.model_dump(mode="json")
        for dest in destinations
        if getattr(dest, "customer_id", None) == customer_id
    ]
    return SitesResponse(sites=sites)


@router.get("/customers/{customer_id}/tanks", response_model=TanksResponse)
async def customer_tanks(
    customer_id: str,
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> TanksResponse:
    """Return the tanks for a customer of the authenticated tenant.

    The customer is first resolved through the tenant-scoped
    :class:`CustomerService`; an unknown or cross-tenant customer raises a
    uniform HTTP 404 (Req 14.3). A customer with no tanks yields an empty array
    and HTTP 200 (Req 14.4).

    Validates: Requirements 14.2, 14.3, 14.4
    """
    service = _require(_customer_service, "customer_service")
    await service.get(voice.tenant_id, customer_id)

    tank_repository = _require(_customer_tank_repository, "customer_tank_repository")
    tanks = await tank_repository.list_for_tenant(
        voice.tenant_id, customer_id=customer_id
    )
    return TanksResponse(
        tanks=[tank.model_dump(mode="json") for tank in tanks],
    )


# ---------------------------------------------------------------------------
# Requirement 15 — product validation
# ---------------------------------------------------------------------------


@router.get("/products/validate", response_model=ProductValidateResponse)
async def products_validate(
    code: str = Query(...),
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> ProductValidateResponse:
    """Validate a product ``code`` for the authenticated tenant.

    The code is canonicalized through the fuel-product catalog; a code that
    resolves to an orderable catalog product returns ``{"valid": true}`` and
    any other value returns ``{"valid": false}`` — both HTTP 200 (Req 15.1/
    15.2). A missing ``code`` query parameter is rejected with HTTP 422 by the
    framework's required-parameter validation (Req 15.3).

    Validates: Requirements 15.1, 15.2, 15.3
    """
    catalog = _product_catalog
    if catalog is None:
        # Fall back to the module when bootstrap has not wired an override.
        from fuel.services import fuel_product_catalog as catalog  # type: ignore

    valid = bool(catalog.is_known_product(code))
    return ProductValidateResponse(valid=valid)


# ===========================================================================
# Task 8.4 — Order status endpoints (Order_Status_Read_API)
#
# Requirements: 16.1, 16.2, 16.3, 17.1, 17.2, 17.3, 18.1, 18.2, 18.3
#
# Every handler derives the tenant scope from ``voice.tenant_id`` (the
# credential binding, Req 11.4) and passes it as the first argument to
# ``FuelOrderRepository`` (module ref ``_fuel_order_repository``) so
# ``inject_tenant_filter`` + post-fetch re-validation applies. A cross-tenant
# order/customer therefore degrades to a uniform HTTP 404 (Req 11.3 / 17.3 /
# 18.3) rather than leaking another tenant's data.
# ===========================================================================


#: The terminal order status treated as a completed delivery (Req 18.1). Mirrors
#: ``fuel.order_models.OrderStatus``.
_DELIVERED_STATUS = "delivered"

#: Upper bound on orders fetched when assembling a customer's delivery history
#: before the delivered subset is filtered and the caller's ``limit`` applied.
_DELIVERIES_FETCH_SIZE = 200


# ---------------------------------------------------------------------------
# Response models (design "Surface B response models")
# ---------------------------------------------------------------------------


class OrderSummary(BaseModel):
    """A single order projection returned by ``GET /orders/lookup`` (Req 16.1)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    createdAt: str
    productCode: Optional[str] = None
    gallons: Optional[float] = None


class OrdersLookupResponse(BaseModel):
    """Envelope for ``GET /orders/lookup`` — most-recent-first (Req 16.2)."""

    orders: List[OrderSummary]


class DeliveriesResponse(BaseModel):
    """Envelope for ``GET /customers/{id}/deliveries`` (Req 18.1)."""

    deliveries: List[dict]


def _project_order_summary(order: Any) -> OrderSummary:
    """Map a :class:`~fuel.order_models.FuelOrder` onto the lookup projection."""
    return OrderSummary(
        id=order.order_id,
        status=order.status,
        createdAt=_iso(order.created_at),
        productCode=order.product_code,
        gallons=order.gallons_requested,
    )


# ---------------------------------------------------------------------------
# Requirement 16 — order lookup by phone
# ---------------------------------------------------------------------------


@router.get("/orders/lookup", response_model=OrdersLookupResponse)
async def orders_lookup(
    phone: Optional[str] = Query(None),
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> OrdersLookupResponse:
    """Look up the authenticated tenant's orders by caller phone.

    Orders are matched on ``customer_phone`` within the credential-bound
    ``voice.tenant_id`` (Req 16.1, Req 11.1/11.4) and returned most-recent-first
    (``created_at`` descending, Req 16.2). A blank/absent ``phone`` or a phone
    that matches no orders yields an empty array with HTTP 200 (Req 16.3).

    Validates: Requirements 16.1, 16.2, 16.3
    """
    phone_val = phone.strip() if phone and phone.strip() else None
    if phone_val is None:
        return OrdersLookupResponse(orders=[])

    repository = _require(_fuel_order_repository, "fuel_order_repository")
    result = await repository.search(
        voice.tenant_id,
        customer_phone=phone_val,
        sort="created_at:desc",
    )
    orders = result.get("orders", []) if isinstance(result, dict) else []
    return OrdersLookupResponse(
        orders=[_project_order_summary(order) for order in orders],
    )


# ---------------------------------------------------------------------------
# Requirement 17 — order status and ETA
# ---------------------------------------------------------------------------


@router.get("/orders/{order_id}/status")
async def order_status(
    order_id: str,
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> dict:
    """Return an order's status for the authenticated tenant.

    The order is resolved through the tenant-scoped repository; an unknown or
    cross-tenant order degrades to a uniform HTTP 404 with the not-found details
    (Req 17.3). On a match, ``status`` is always present and ``updatedAt`` /
    ``note`` are included only when the order carries those values (Req 17.1).

    Validates: Requirements 17.1, 17.3
    """
    repository = _require(_fuel_order_repository, "fuel_order_repository")
    order = await repository.get(voice.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            "Order not found", details={"order_id": order_id}
        )

    payload: dict = {"status": order.status}
    updated_at = _iso(order.updated_at)
    if updated_at:
        payload["updatedAt"] = updated_at
    note = order.special_instructions
    if note:
        payload["note"] = note
    return payload


@router.get("/orders/{order_id}/eta")
async def order_eta(
    order_id: str,
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> dict:
    """Return an order's ETA for the authenticated tenant.

    The order is resolved through the tenant-scoped repository; an unknown or
    cross-tenant order degrades to a uniform HTTP 404 with the not-found details
    (Req 17.3). On a match the ETA is projected from the delivery window:
    ``etaWindow`` (``start/end`` ISO-8601) and ``etaAt`` (the window start) are
    included only when the underlying values exist, and ``status`` is always
    included (Req 17.2).

    Validates: Requirements 17.2, 17.3
    """
    repository = _require(_fuel_order_repository, "fuel_order_repository")
    order = await repository.get(voice.tenant_id, order_id)
    if order is None:
        raise resource_not_found(
            "Order not found", details={"order_id": order_id}
        )

    payload: dict = {"status": order.status}
    window_start = _iso(order.delivery_window_start)
    window_end = _iso(order.delivery_window_end)
    if window_start and window_end:
        payload["etaWindow"] = f"{window_start}/{window_end}"
    if window_start:
        payload["etaAt"] = window_start
    return payload


# ---------------------------------------------------------------------------
# Requirement 18 — customer delivery history
# ---------------------------------------------------------------------------


@router.get(
    "/customers/{customer_id}/deliveries", response_model=DeliveriesResponse
)
async def customer_deliveries(
    customer_id: str,
    limit: Optional[int] = Query(None, ge=0),
    voice: VoiceTenantContext = Depends(get_voice_tenant),
) -> DeliveriesResponse:
    """Return a customer's recent deliveries for the authenticated tenant.

    Orders are resolved through the tenant-scoped repository. Because the only
    view of a customer is the tenant's orders for them, a customer with zero
    orders in this tenant — which covers every cross-tenant/unknown customer —
    degrades to a uniform HTTP 404 (Req 18.3). Otherwise the ``delivered``
    orders are projected most-recent-first and capped at ``limit`` when supplied
    (Req 18.1/18.2); a known customer with no delivered orders yields an empty
    array with HTTP 200.

    Validates: Requirements 18.1, 18.2, 18.3
    """
    repository = _require(_fuel_order_repository, "fuel_order_repository")
    result = await repository.search(
        voice.tenant_id,
        customer_id=customer_id,
        sort="created_at:desc",
        size=_DELIVERIES_FETCH_SIZE,
    )
    orders = result.get("orders", []) if isinstance(result, dict) else []
    if not orders:
        # No orders for this customer in the tenant scope — treat an unknown or
        # cross-tenant customer as not found (Req 18.3).
        raise resource_not_found(
            "Customer not found", details={"customer_id": customer_id}
        )

    deliveries = [
        {
            "id": order.order_id,
            "status": order.status,
            "createdAt": _iso(order.created_at),
            "deliveredAt": _iso(order.updated_at),
            "productCode": order.product_code,
            "gallons": order.gallons_requested,
        }
        for order in orders
        if order.status == _DELIVERED_STATUS
    ]
    if limit is not None:
        deliveries = deliveries[:limit]
    return DeliveriesResponse(deliveries=deliveries)
