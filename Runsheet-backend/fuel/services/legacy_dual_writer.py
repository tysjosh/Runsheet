"""
Legacy Dual-Write Shim — deprecate in 1 minor release.

Mirrors every ``fuel_orders_current`` write to ``shipments_current`` and
every ``drivers_current`` write to ``riders_current`` for the deprecation
window. Field mapping is a 1:1 projection with the legacy shape.

Never raises — a legacy-mirror failure logs a warning, increments
``orders_legacy_mirror_errors_total{tenant_id, entity_type}``, and
enqueues the entity ID in ``pending_legacy_mirrors`` for background
retry so the main path is never blocked by a stale legacy index.

Validates: Requirement 1.3.2.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, Optional

from fuel.services.order_metrics import orders_legacy_mirror_errors_total
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = ["LegacyDualWriter"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PENDING_LEGACY_MIRRORS_INDEX = "pending_legacy_mirrors"


# ---------------------------------------------------------------------------
# LegacyDualWriter
# ---------------------------------------------------------------------------


class LegacyDualWriter:
    """Mirror fuel-order and driver writes to the legacy shipment/rider
    indices during the deprecation window.

    Both ``mirror_order`` and ``mirror_driver`` MUST never raise.
    Failures are logged, counted, and enqueued for background retry.
    """

    def __init__(
        self,
        ops_es_service: Any,
        es_service: Any,
        clock: Optional[Callable] = None,
    ) -> None:
        """
        Parameters
        ----------
        ops_es_service:
            The ``OpsElasticsearchService`` instance that owns
            ``upsert_shipment_current`` and ``upsert_rider_current``.
        es_service:
            The base ``ElasticsearchService`` used for indexing into
            ``pending_legacy_mirrors``.
        clock:
            Optional clock override for testing. Defaults to
            ``services.time_utils.utcnow``.
        """
        self._ops_es = ops_es_service
        self._es = es_service
        self._clock = clock or utcnow

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def mirror_order(
        self, order: Dict[str, Any], tenant_id: Optional[str] = None
    ) -> None:
        """Project a FuelOrder dict into the legacy shipment shape and
        upsert into ``shipments_current``.

        Never raises — failures log a warning, increment the error
        counter, and enqueue the order ID for background retry.
        """
        _tenant_id = tenant_id or order.get("tenant_id", "unknown")
        try:
            legacy_doc = self._project_order_to_shipment(order)
            await self._ops_es.upsert_shipment_current(legacy_doc)
        except Exception as exc:
            logger.warning(
                "Legacy shipment mirror failed for order=%s, tenant=%s: %s",
                order.get("order_id"),
                _tenant_id,
                exc,
            )
            orders_legacy_mirror_errors_total.labels(
                tenant_id=_tenant_id,
                entity_type="order",
            ).inc()
            await self._enqueue_pending_mirror(
                entity_id=order.get("order_id", "unknown"),
                entity_type="order",
                tenant_id=_tenant_id,
                failure_reason=str(exc),
            )

    async def mirror_driver(
        self, driver: Dict[str, Any], tenant_id: Optional[str] = None
    ) -> None:
        """Project a Driver dict into the legacy rider shape and upsert
        into ``riders_current``.

        Never raises — failures log a warning, increment the error
        counter, and enqueue the driver ID for background retry.
        """
        _tenant_id = tenant_id or driver.get("tenant_id", "unknown")
        try:
            legacy_doc = self._project_driver_to_rider(driver)
            await self._ops_es.upsert_rider_current(legacy_doc)
        except Exception as exc:
            logger.warning(
                "Legacy rider mirror failed for driver=%s, tenant=%s: %s",
                driver.get("driver_id"),
                _tenant_id,
                exc,
            )
            orders_legacy_mirror_errors_total.labels(
                tenant_id=_tenant_id,
                entity_type="driver",
            ).inc()
            await self._enqueue_pending_mirror(
                entity_id=driver.get("driver_id", "unknown"),
                entity_type="driver",
                tenant_id=_tenant_id,
                failure_reason=str(exc),
            )

    # ------------------------------------------------------------------
    # Projection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _project_order_to_shipment(order: Dict[str, Any]) -> Dict[str, Any]:
        """Map a FuelOrder document to the legacy ``shipments_current``
        shape.

        Preserves ``order.legacy_origin_snapshot`` as the legacy
        ``origin`` field. Orders that never existed on the legacy side
        (newly intake'd after ``active_auto``) carry a null snapshot
        and the writer falls back to the ``"depot"`` sentinel.
        """
        origin = order.get("legacy_origin_snapshot") or "depot"
        return {
            "shipment_id": order["order_id"],
            "status": order["status"],
            "tenant_id": order["tenant_id"],
            "rider_id": order.get("assigned_driver_id"),
            "origin": origin,
            "destination": order.get("ship_to_address", ""),
            "estimated_delivery": order.get("delivery_window_end"),
            "last_event_timestamp": order.get("last_event_timestamp"),
            "current_location": (
                {"lat": order["ship_to_lat"], "lon": order["ship_to_lon"]}
                if order.get("ship_to_lat") is not None
                and order.get("ship_to_lon") is not None
                else None
            ),
            "source_schema_version": order.get(
                "source_schema_version", "legacy"
            ),
            "trace_id": order.get("trace_id", ""),
            "created_at": order.get("created_at"),
            "updated_at": order.get("updated_at"),
            "ingested_at": order.get("updated_at"),
        }

    @staticmethod
    def _project_driver_to_rider(driver: Dict[str, Any]) -> Dict[str, Any]:
        """Map a Driver document to the legacy ``riders_current`` shape."""
        return {
            "rider_id": driver["driver_id"],
            "status": driver.get("status", "active"),
            "tenant_id": driver["tenant_id"],
            "availability": driver.get("availability"),
            "source_schema_version": driver.get(
                "source_schema_version", "legacy"
            ),
            "trace_id": driver.get("trace_id", ""),
            "last_seen": driver.get("last_seen"),
            "last_event_timestamp": driver.get("last_event_timestamp"),
            "ingested_at": driver.get("updated_at"),
            "current_location": driver.get("current_location"),
            "active_shipment_count": driver.get("active_order_count", 0),
            "completed_today": driver.get("completed_today", 0),
            "rider_name": driver.get("driver_name", ""),
        }

    # ------------------------------------------------------------------
    # Retry queue
    # ------------------------------------------------------------------

    async def _enqueue_pending_mirror(
        self,
        entity_id: str,
        entity_type: str,
        tenant_id: str,
        failure_reason: str,
    ) -> None:
        """Enqueue a failed mirror write in ``pending_legacy_mirrors``
        for background retry.

        This method itself MUST NOT raise — if the enqueue fails we log
        an error and move on so the main path is never blocked.
        """
        try:
            now = self._clock()
            entry_id = f"mirror_{entity_type}_{entity_id}_{uuid.uuid4().hex[:8]}"
            doc = {
                "entry_id": entry_id,
                "tenant_id": tenant_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "failure_reason": failure_reason,
                "retry_count": 0,
                "next_retry_at": now.isoformat(),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            }
            await self._es.index_document(
                PENDING_LEGACY_MIRRORS_INDEX,
                entry_id,
                doc,
            )
        except Exception as enqueue_exc:
            # Last resort — log and move on. The main path MUST NOT
            # be blocked by a failure to enqueue.
            logger.error(
                "Failed to enqueue pending legacy mirror for "
                "%s=%s, tenant=%s: %s",
                entity_type,
                entity_id,
                tenant_id,
                enqueue_exc,
            )
