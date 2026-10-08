"""Customer-portal order requests: submit and cancel (design §4.1, §4.4).

``submit`` sends a request through ``OrderIntakePipeline.ingest_portal`` on
the reserved ``web_portal`` channel; the pipeline puts it on the portal review
hold. A replay of a ``client_event_id`` answers with the original order and
never calls the pipeline (R4.2). ``cancel`` is a compare-and-set through
``fuel.services.order_actions.cancel_order`` (FREEZE F3).

Configured from bootstrap with :func:`configure_portal_orders`; unconfigured,
the routes answer 503 ``PORTAL_UNAVAILABLE``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from commerce.services.pricing_engine import PricingError
from compliance.hooks.dyed_diesel_intake_hook import DyedDieselOrderRejected
from errors.codes import ErrorCode
from errors.exceptions import (
    AppException,
    idempotency_conflict,
    order_intake_disabled,
    order_not_cancellable,
    order_request_rejected,
    portal_unavailable,
)
from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from fuel.services import order_actions
from fuel.services.order_intake_pipeline import portal_order_id
from portal.models import (
    PortalOrder,
    PortalOrderRequest,
    PortalOrderTank,
    PortalQuantityGallons,
)
from portal.services.projection import AWAITING_CONFIRMATION, project_order, tank_label
from portal.services.scoped_readers import (
    PortalReaders,
    get_portal_readers,
    order_not_found,
    tank_not_found,
)

logger = logging.getLogger(__name__)

#: The one message a customer sees for any hook or intake rejection (ORD-6).
REJECTED_MESSAGE = "We couldn't accept this request online. Please contact your supplier."
REPLAY_LOST_MESSAGE = "Please submit the request again with a new reference."
CANCEL_REASON = "cancelled_by_customer"


@dataclass
class SubmitResult:
    """``status_code`` 201 (created) or 200 (replay of an existing request)."""

    status_code: int
    order: PortalOrder


def _gallons_invalid() -> AppException:
    return AppException(
        ErrorCode.VALIDATION_ERROR,
        "Requested gallons exceed the tank's capacity",
        status_code=422,
        details={"fields": ["quantity.gallons"]},
    )


class PortalOrderService:
    def __init__(
        self,
        *,
        pipeline: Any,
        order_repository: Any,
        customer_service: Any,
        readers: Optional[PortalReaders] = None,
    ) -> None:
        self._pipeline = pipeline
        self._repo = order_repository
        self._customers = customer_service
        self._readers = readers

    @property
    def readers(self) -> PortalReaders:
        return self._readers or get_portal_readers()

    async def _labels(self, scope: Any) -> Dict[str, str]:
        tanks = await self.readers.tanks.list(scope)
        return {
            t.customer_tank_id: tank_label(
                t.customer_tank_id,
                t.external_tank_id,
                getattr(t, "display_name", None),
            )
            for t in tanks
        }

    async def project(self, scope: Any, order: Any) -> PortalOrder:
        return project_order(order, tank_labels=await self._labels(scope))

    async def project_all(self, scope: Any, orders: Any) -> list:
        """Project a page with one tank-label read."""
        labels = await self._labels(scope) if orders else {}
        return [project_order(o, tank_labels=labels) for o in orders]

    async def _replay(self, scope: Any, order_id: str) -> Optional[SubmitResult]:
        existing = await self.readers.orders.get_or_none(scope, order_id)
        if existing is None:
            return None
        return SubmitResult(200, await self.project(scope, existing))

    # ------------------------------------------------------------------
    # submit (design §4.1 steps 1-7)
    # ------------------------------------------------------------------

    async def submit(
        self, scope: Any, body: PortalOrderRequest, request_id: str
    ) -> SubmitResult:
        oid = portal_order_id(scope.tenant_id, scope.user_id, body.client_event_id)

        # 1. Intake off: only a replay read, never a write (R4.2, R4.5).
        if await self._pipeline.get_ordering_state(scope.tenant_id) == "disabled":
            replay = await self._replay(scope, oid)
            if replay is not None:
                return replay
            raise order_intake_disabled()

        # 2. The customer's own active tank (R4.3).
        tank = await self.readers.tanks.get(scope, body.customer_tank_id)

        # 3. Capacity.
        gallons: Optional[float] = None
        if isinstance(body.quantity, PortalQuantityGallons):
            gallons = float(body.quantity.gallons)
            if gallons > float(tank.capacity_gallons):
                raise _gallons_invalid()

        # 4. Customer name.
        customer = await self._customers.get(scope.tenant_id, scope.customer_id)
        display_name = str((customer or {}).get("display_name") or scope.customer_id)

        # 5. Ship-to (D4): coordinates from the tank; address from the tank's
        #    latest order, else a synthesized label.
        latest = await self.readers.orders.latest_for_tank(scope, tank.customer_tank_id)
        ship_to_address = getattr(latest, "ship_to_address", None) if latest else None
        if not ship_to_address:
            ref = tank.external_tank_id or tank.customer_tank_id[-8:]
            ship_to_address = f"{display_name} — tank {ref}, ZIP {tank.zip_code}"

        # 6. Adapter payload.
        payload: Dict[str, Any] = {
            "schema_version": "1.0",
            "customer_id": scope.customer_id,
            "customer_name": display_name,
            "ship_to_address": ship_to_address,
            "ship_to_lat": tank.location_lat,
            "ship_to_lon": tank.location_lon,
            "customer_tank_id": tank.customer_tank_id,
            "product_code": tank.fuel_product_code,
            "gallons_requested": gallons,
            "fill_to_full": gallons is None,
            "call_type": "will_call",
            "delivery_window_start": body.window_start.isoformat(),
            "delivery_window_end": body.window_end.isoformat(),
            "po_number": body.po_number,
            "special_instructions": body.notes,
        }

        # 6a. Replay pre-check (review H3): independent of the Redis marker.
        replay = await self._replay(scope, oid)
        if replay is not None:
            return replay

        # 7. Pipeline, and the result mapping table.
        try:
            result = await self._pipeline.ingest_portal(
                scope=scope,
                payload=payload,
                request_id=request_id,
                client_event_id=body.client_event_id,
            )
        except AppException as exc:
            code = getattr(exc.error_code, "value", exc.error_code)
            if code == ErrorCode.ORDER_PAYLOAD_INVALID.value:
                raise
            if code == ErrorCode.INVALID_CUSTOMER_TANK_REF.value:
                raise tank_not_found(body.customer_tank_id) from None
            raise self._rejected(exc) from None
        except (PricingError, DyedDieselOrderRejected) as exc:
            raise self._rejected(exc) from None

        status = result.status
        if status == "legacy_passthrough":
            raise order_intake_disabled()
        if status == "duplicate":
            replay = await self._replay(scope, oid)
            if replay is None:
                raise idempotency_conflict(REPLAY_LOST_MESSAGE)
            return replay
        if status == "processed":
            stored = await self.readers.orders.get_or_none(scope, result.order_id or oid)
            if stored is not None:
                return SubmitResult(201, await self.project(scope, stored))
            # Not readable yet: answer from what was submitted.
            code, label = AWAITING_CONFIRMATION
            return SubmitResult(
                201,
                PortalOrder(
                    order_id=result.order_id or oid,
                    status_code=code,
                    status_label=label,
                    product_code=tank.fuel_product_code,
                    gallons_requested=gallons,
                    fill_to_full=gallons is None,
                    window_start=body.window_start,
                    window_end=body.window_end,
                    po_number=body.po_number,
                    tank=PortalOrderTank(
                        customer_tank_id=tank.customer_tank_id,
                        label=tank_label(tank.customer_tank_id, tank.external_tank_id),
                    ),
                    cancellable=True,
                ),
            )
        # queued_for_review (an adapter error) or anything unexpected.
        logger.error(
            "portal order: intake returned %s for tenant=%s request_id=%s",
            status,
            scope.tenant_id,
            request_id,
        )
        raise order_request_rejected(REJECTED_MESSAGE)

    @staticmethod
    def _rejected(exc: BaseException) -> AppException:
        """422 with the fixed message; the cause is logged, never returned."""
        logger.warning(
            "portal order rejected by intake: %s error_code=%s",
            type(exc).__name__,
            getattr(getattr(exc, "error_code", None), "value", getattr(exc, "error_code", None)),
        )
        return order_request_rejected(REJECTED_MESSAGE)

    # ------------------------------------------------------------------
    # cancel (design §4.4)
    # ------------------------------------------------------------------

    async def cancel(self, scope: Any, order_id: str) -> PortalOrder:
        order = await self.readers.orders.get(scope, order_id)
        if order.intake_channel != "web_portal":
            raise order_not_cancellable()
        cancelled = await order_actions.cancel_order(
            self._repo,
            scope.tenant_id,
            order_id,
            actor_user_id=scope.user_id,
            reason=CANCEL_REASON,
            notes=None,
            expected_status="on_hold",
            expected_hold_reason=PORTAL_REVIEW_HOLD_REASON,
            counter_service=None,
            clear_hold_reason=True,
        )
        if cancelled is None:
            raise order_not_cancellable()
        updated = await self.readers.orders.get_or_none(scope, order_id)
        if updated is None:
            raise order_not_found(order_id)
        return await self.project(scope, updated)


_service: Optional[PortalOrderService] = None


def configure_portal_orders(service: Optional[PortalOrderService]) -> None:
    global _service
    _service = service


def get_configured_order_service() -> Optional[PortalOrderService]:
    return _service


def get_portal_order_service() -> PortalOrderService:
    if _service is None:
        raise portal_unavailable()
    return _service


def wire_portal_orders(
    *,
    es_service: Any,
    order_repository: Any,
    tank_repository: Any,
    pipeline: Any,
    customer_service: Any,
) -> PortalOrderService:
    """Build the scoped readers and the service, and register both."""
    from portal.services.scoped_readers import (
        PortalForecastReader,
        PortalOrderReader,
        PortalTankReader,
        configure_portal_readers,
    )

    readers = PortalReaders(
        orders=PortalOrderReader(order_repository),
        tanks=PortalTankReader(tank_repository),
        forecasts=PortalForecastReader(es_service),
    )
    configure_portal_readers(readers)
    service = PortalOrderService(
        pipeline=pipeline,
        order_repository=order_repository,
        customer_service=customer_service,
        readers=readers,
    )
    configure_portal_orders(service)
    return service


__all__ = [
    "CANCEL_REASON",
    "PortalOrderService",
    "REJECTED_MESSAGE",
    "SubmitResult",
    "configure_portal_orders",
    "get_configured_order_service",
    "get_portal_order_service",
    "wire_portal_orders",
]
