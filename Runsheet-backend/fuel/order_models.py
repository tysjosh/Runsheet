"""
Fuel Order domain models — FuelOrder, FuelOrderEvent, Driver, IntakeMetadata.

This module defines the Pydantic models that back the ``fuel_orders_current``,
``fuel_order_events``, and ``drivers_current`` Elasticsearch indices. Every
model uses ``ConfigDict(extra="forbid")`` so unknown fields are rejected at
construction time, matching the strict ES mappings.

Key responsibilities:

* Expose :class:`FuelOrder` — the canonical unit of work produced by every
  intake channel and consumed by every downstream agent.
* Expose :class:`FuelOrderEvent` — an immutable timeline entry for a
  FuelOrder (append-only in ES).
* Expose :class:`Driver` — the operator who executes a FuelOrder (replaces
  the legacy ``rider`` entity).
* Expose :class:`IntakeMetadata` — the closed superset of per-channel
  metadata fields.
* Expose type aliases :data:`OrderStatus`, :data:`CallType`,
  :data:`IntakeChannelType` for use across the codebase.
* Canonicalize ``product_code`` through
  :func:`fuel.services.fuel_product_catalog.canonicalize` on every write so
  legacy aliases (AGO → DIESEL_2, PMS → GASOLINE_REG, ATK → KEROSENE,
  LPG → PROPANE) normalize at construction time.
* Enforce business rules via model validators:
  - Non-legacy channels MUST carry a non-null ``product_code``.
  - Non-legacy channels MUST carry either ``gallons_requested > 0`` or
    ``fill_to_full = True``.
  - ``one_off`` orders MUST carry a valid delivery window.
  - Window coherence: ``end > start`` whenever both are present.
  - ``on_hold`` orders MUST carry a non-empty ``hold_reason``.

Validates: Requirements 1.1, 1.1.7, 1.1.8, 1.1.9, 1.1.10, 1.1.11.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Type Aliases
# ---------------------------------------------------------------------------

OrderStatus = Literal[
    "placed", "confirmed", "scheduled", "dispatched",
    "in_transit", "delivered", "failed", "cancelled", "on_hold",
]

CallType = Literal["will_call", "auto_fill", "keep_full", "one_off"]

IntakeChannelType = Literal[
    "voice", "web_portal", "dispatcher", "csv", "edi", "api_partner", "legacy",
]


# ---------------------------------------------------------------------------
# Driver Status Types
# ---------------------------------------------------------------------------

DriverStatus = Literal["active", "inactive", "on_break", "off_duty"]


# ---------------------------------------------------------------------------
# IntakeMetadata
# ---------------------------------------------------------------------------


class IntakeMetadata(BaseModel):
    """Closed superset of per-channel intake metadata.

    Adapters populate only the fields relevant to their channel; the
    rest stay ``None``. The strict-mapping on the ES side enforces this
    closed set so adapters cannot smuggle arbitrary fields.
    """

    model_config = ConfigDict(extra="forbid")

    call_id: Optional[str] = None
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    agent_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    dispatcher_user_id: Optional[str] = None
    session_id: Optional[str] = None
    portal_session_id: Optional[str] = None
    user_agent: Optional[str] = None
    import_batch_id: Optional[str] = None
    csv_row_number: Optional[int] = Field(default=None, ge=1)
    edi_interchange_id: Optional[str] = None
    partner_ref: Optional[str] = None
    legacy_shipment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# FuelOrder
# ---------------------------------------------------------------------------


class FuelOrder(BaseModel):
    """The canonical fuel order entity.

    Produced by every intake channel and consumed by every downstream agent
    (Delivery_Prioritization, Route_Planning, Compartment_Loading, POD,
    Reconciliation). Persisted in the ``fuel_orders_current`` ES index.
    """

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)

    customer_id: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    ship_to_address: str = Field(..., min_length=1)
    # Coordinates are optional at the model level so voice orders (which capture
    # only a free-text address, no geocoding) can be accepted and reconciled
    # during review-hold. Every non-voice / non-legacy channel MUST still carry
    # them — enforced in _validate_coordinates.
    ship_to_lat: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    ship_to_lon: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    customer_tank_id: Optional[str] = None

    # product_code is nullable ONLY for legacy-channel orders during
    # the migration window. Every non-legacy intake MUST carry a
    # canonicalized product_code — enforced in _validate_product_code.
    product_code: Optional[str] = Field(default=None, min_length=1)
    gallons_requested: Optional[float] = Field(default=None, gt=0)
    fill_to_full: bool = False
    call_type: CallType
    delivery_window_start: Optional[datetime] = None
    delivery_window_end: Optional[datetime] = None
    hold_reason: Optional[str] = None
    po_number: Optional[str] = None
    special_instructions: Optional[str] = None

    intake_channel: IntakeChannelType
    intake_channel_id: str = Field(..., min_length=1)
    intake_metadata: IntakeMetadata = Field(default_factory=IntakeMetadata)

    status: OrderStatus = "placed"
    assigned_driver_id: Optional[str] = None
    # Optional reference to the fleet asset/truck carrying this order. Nullable so
    # existing records remain valid without backfill (Req 2.1, 6.1). Set
    # consistently with the assigned run/job's asset (Req 2.2); validated to an
    # existing same-tenant asset at write time (Req 2.3).
    assigned_asset_id: Optional[str] = None
    assigned_run_id: Optional[str] = None
    # POD one-time code provisioned at dispatch by ``PODOTPService`` and the
    # instant its validity window is measured from (driver-mobile-app R5.25,
    # R5.28-R5.30). Both nullable: absent means the tenant does not require a
    # code, which is the default. ``model_config`` is ``extra="forbid"`` and
    # the ES mapping is ``dynamic: strict``, so either half alone would reject
    # the write — and a stored document carrying an undeclared field would be
    # dropped by ``_safe_order_load``. Never leaves the server on a
    # ``/api/driver`` response: ``DriverWorkService`` strips ``pod_otp`` before
    # serialization (R5.26).
    pod_otp: Optional[str] = None
    pod_otp_generated_at: Optional[datetime] = None
    legacy_origin_snapshot: Optional[str] = None

    source_schema_version: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    created_at: datetime
    updated_at: datetime
    last_event_timestamp: datetime

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("product_code", mode="before")
    @classmethod
    def _canonicalize_product(cls, value: Any) -> Any:
        """Canonicalize product_code through the fuel product catalog.

        Reuses :func:`fuel.services.fuel_product_catalog.canonicalize` so
        legacy aliases (AGO / PMS / ATK / LPG) normalize to the US
        catalog entries at construction time. ``None`` passes through
        for legacy orders; ``_validate_product_code`` enforces the
        non-null rule for non-legacy channels.
        """
        if value is None:
            return None
        from fuel.services.fuel_product_catalog import canonicalize
        return canonicalize(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_product_code(self) -> "FuelOrder":
        """Non-legacy channels MUST carry a canonicalized product_code."""
        if self.product_code is None and self.intake_channel != "legacy":
            raise ValueError("missing_product_code")
        return self

    @model_validator(mode="after")
    def _validate_coordinates(self) -> "FuelOrder":
        """Require ship-to coordinates for every channel except voice/legacy.

        ``voice`` orders capture only a free-text delivery address (no
        geocoding); the coordinates are reconciled by a human during
        review-hold. ``legacy`` orders are exempt during the migration window.
        Every other channel MUST carry both coordinates so downstream routing
        has a geolocation at intake.
        """
        if self.intake_channel not in ("voice", "legacy"):
            if self.ship_to_lat is None or self.ship_to_lon is None:
                raise ValueError("missing_coordinates")
        return self

    @model_validator(mode="after")
    def _validate_volume(self) -> "FuelOrder":
        """Validate volume requirements for non-legacy channels.

        Legacy-channel orders MAY carry null gallons during migration —
        the forecaster attaches the value before dispatch.
        """
        if self.intake_channel == "legacy":
            return self
        if not self.fill_to_full and self.gallons_requested is None:
            raise ValueError("missing_volume")
        if self.gallons_requested is not None and self.gallons_requested <= 0:
            raise ValueError("missing_volume")
        return self

    @model_validator(mode="after")
    def _validate_window(self) -> "FuelOrder":
        """Validate delivery window requirements.

        Window is MANDATORY only for ``one_off`` orders. ``will_call`` /
        ``keep_full`` / ``auto_fill`` MAY omit it at intake — the forecaster
        (keep_full, auto_fill) and the dispatcher (will_call) attach
        the window before the ``placed → scheduled`` transition, which
        is enforced by ``OrderService.apply_status_transition``.
        """
        if self.call_type == "one_off":
            if self.delivery_window_start is None or self.delivery_window_end is None:
                raise ValueError("invalid_delivery_window")
        if self.delivery_window_start is not None and self.delivery_window_end is not None:
            if self.delivery_window_end <= self.delivery_window_start:
                raise ValueError("invalid_delivery_window")
        return self

    @model_validator(mode="after")
    def _validate_hold(self) -> "FuelOrder":
        """Every on_hold order MUST carry a non-empty hold_reason."""
        if self.status == "on_hold" and not (self.hold_reason and self.hold_reason.strip()):
            raise ValueError("missing_hold_reason")
        return self


# ---------------------------------------------------------------------------
# FuelOrderEvent
# ---------------------------------------------------------------------------


class FuelOrderEvent(BaseModel):
    """An immutable timeline entry for a FuelOrder.

    Persisted append-only in the ``fuel_order_events`` ES index. Event
    types correspond to the status transitions defined in the order state
    machine plus the inbound ``order_placed``.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1)
    order_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    event_payload: Optional[Dict[str, Any]] = None
    event_timestamp: datetime
    ingested_at: datetime
    source_schema_version: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    location: Optional[Dict[str, float]] = None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Driver(BaseModel):
    """The operator who executes a FuelOrder.

    Replaces the legacy ``rider`` entity. Persisted in the
    ``drivers_current`` ES index. Carries the driver's qualification
    summary (CDL class, HAZMAT endorsement, medical card expiry) when
    available.
    """

    model_config = ConfigDict(extra="forbid")

    driver_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    driver_name: str = Field(..., min_length=1)
    phone: Optional[str] = None
    status: DriverStatus
    # Duty-status projection bookkeeping. ``duty_status_event_id`` is the id of
    # the Duty_Status_Event_Log document ``status`` projects and
    # ``duty_status_updated_at`` is that event's ``server_received_at``. Both are
    # optional: absent means the record predates the duty-status event log.
    duty_status_event_id: Optional[str] = None
    duty_status_updated_at: Optional[datetime] = None
    availability: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    cdl_class: Optional[str] = None
    hazmat_endorsement: Optional[bool] = None
    medical_card_expiry: Optional[datetime] = None
    current_location: Optional[Dict[str, float]] = None
    last_seen: Optional[datetime] = None
    active_order_count: int = Field(default=0, ge=0)
    completed_today: int = Field(default=0, ge=0)
    last_event_timestamp: datetime
    source_schema_version: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "OrderStatus",
    "CallType",
    "IntakeChannelType",
    "DriverStatus",
    "IntakeMetadata",
    "FuelOrder",
    "FuelOrderEvent",
    "Driver",
]
