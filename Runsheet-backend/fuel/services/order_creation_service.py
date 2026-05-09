"""
Order Creation Service — shared "stamp + persist + emit + broadcast + dual-write" sequence.

Encapsulates the order creation lifecycle so both the
:class:`~fuel.services.order_intake_pipeline.OrderIntakePipeline` and
future agent-driven ``auto_fill`` callers share one implementation.

The sequence:
    1. Stamp platform-owned fields (``order_id``, ``tenant_id``,
       ``status="placed"``, ``created_at``, ``updated_at``,
       ``last_event_timestamp``, ``trace_id``).
    2. Stamp event documents with matching platform fields.
    3. Persist the order via
       ``FuelOrderRepository.upsert_with_last_event_timestamp``.
    4. Append each ``order_placed`` event via ``append_event``.
    5. Broadcast ``order_placed`` through ``OrdersWSManager``.
    6. Dual-write through ``LegacyDualWriter`` (if enabled and overlay
       state is ``shadow`` or ``active_gated``).

Constructor deps:
    order_repo, ws_manager, optional legacy_dual_writer,
    optional feature_flag_service, optional clock.

Validates: Requirements 2.3, 2.4.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fuel.services.order_id_generator import mint_event_id, mint_order_id
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = ["OrderCreationService"]


class OrderCreationService:
    """Owns the "stamp platform fields + persist + emit event + broadcast
    + dual-write" sequence for new order creation.

    This service is the single implementation used by both the
    ``OrderIntakePipeline`` (webhook/dispatcher/bulk paths) and future
    agent-driven callers (e.g. ``auto_fill`` from the Tank Forecasting
    Agent).

    Constructor dependencies:
        order_repo: ``FuelOrderRepository`` instance for persistence.
        ws_manager: ``OrdersWSManager`` (or compatible) for broadcasting.
        legacy_dual_writer: Optional ``LegacyDualWriter`` for mirroring
            to the legacy shipment surface during the deprecation window.
        feature_flag_service: Optional ``FeatureFlagService`` for checking
            overlay state (controls whether legacy dual-write is active).
        clock: Optional clock override for testing (defaults to
            ``services.time_utils.utcnow``).
    """

    def __init__(
        self,
        *,
        order_repo: Any,
        ws_manager: Any,
        legacy_dual_writer: Optional[Any] = None,
        feature_flag_service: Optional[Any] = None,
        clock: Optional[Callable] = None,
    ) -> None:
        self._order_repo = order_repo
        self._ws_manager = ws_manager
        self._legacy_dual_writer = legacy_dual_writer
        self._feature_flag_service = feature_flag_service
        self._clock = clock or utcnow

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_order(
        self,
        tenant_id: str,
        channel: str,
        adapter_result: Any,
        request_id: str,
    ) -> Dict[str, Any]:
        """Create a new order: stamp, persist, emit event, broadcast, dual-write.

        This is the single method that encapsulates the full creation
        lifecycle. Both the ``OrderIntakePipeline`` and future
        agent-driven ``auto_fill`` callers use this method.

        Args:
            tenant_id: The tenant that owns the order.
            channel: The intake channel identifier (e.g. ``"dispatcher"``,
                ``"voice"``, ``"api_partner"``). Used for logging/tracing.
            adapter_result: The result from an ``IntakeAdapter.transform``
                call. Must expose ``order_doc`` (dict) and ``event_docs``
                (list of dicts).
            request_id: A unique request/trace identifier used as the
                ``trace_id`` on the order and events.

        Returns:
            The completed order document (dict) with all platform-owned
            fields stamped and persisted.
        """
        # Extract adapter output
        order_doc = adapter_result.order_doc
        event_docs = adapter_result.event_docs

        # 1. Stamp platform-owned fields on the order document
        order_doc = self._stamp_platform_fields(
            order_doc, tenant_id, request_id
        )

        # 2. Stamp platform-owned fields on each event document
        event_docs = self._stamp_event_fields(
            event_docs, order_doc, tenant_id, request_id
        )

        # 3. Persist the order via scripted upsert
        await self._order_repo.upsert_with_last_event_timestamp(
            tenant_id, order_doc
        )

        # 4. Append each event
        for event in event_docs:
            await self._order_repo.append_event(tenant_id, event)

        # 5. Broadcast order_placed through WebSocket
        await self._broadcast_order_placed(order_doc)

        # 6. Dual-write to legacy surface (if enabled)
        await self._dual_write_legacy_if_enabled(order_doc, tenant_id)

        return order_doc

    # ------------------------------------------------------------------
    # Platform field stamping
    # ------------------------------------------------------------------

    def _stamp_platform_fields(
        self,
        adapter_output: Dict[str, Any],
        tenant_id: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Stamp platform-owned fields on the adapter's output.

        Adapters own the business shape (customer, product, address,
        intake_metadata) but MAY NOT set lifecycle metadata. This method
        overwrites any adapter-set values for the platform-owned fields:
        ``order_id``, ``tenant_id``, ``status``, ``created_at``,
        ``updated_at``, ``last_event_timestamp``, ``trace_id``.
        """
        now = self._clock()
        order_doc = dict(adapter_output)  # shallow copy
        order_doc["order_id"] = mint_order_id()
        order_doc["tenant_id"] = tenant_id
        order_doc["status"] = "placed"
        order_doc["created_at"] = now.isoformat()
        order_doc["updated_at"] = now.isoformat()
        order_doc["last_event_timestamp"] = now.isoformat()
        order_doc["trace_id"] = request_id
        return order_doc

    def _stamp_event_fields(
        self,
        adapter_events: List[Dict[str, Any]],
        order_doc: Dict[str, Any],
        tenant_id: str,
        request_id: str,
    ) -> List[Dict[str, Any]]:
        """Stamp platform-owned fields on each event document.

        Each event gets a fresh ``event_id``, the parent ``order_id``,
        the ``tenant_id``, timestamps, and the ``trace_id``.
        """
        completed: List[Dict[str, Any]] = []
        now = order_doc["last_event_timestamp"]
        for ev in adapter_events:
            ev = dict(ev)  # shallow copy
            ev["event_id"] = mint_event_id()
            ev["order_id"] = order_doc["order_id"]
            ev["tenant_id"] = tenant_id
            ev["event_timestamp"] = now
            ev["ingested_at"] = now
            ev["trace_id"] = request_id
            ev["source_schema_version"] = order_doc.get(
                "source_schema_version", "1.0"
            )
            completed.append(ev)
        return completed

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    async def _broadcast_order_placed(
        self, order_doc: Dict[str, Any]
    ) -> None:
        """Broadcast the order_placed event through the WebSocket manager.

        Broadcast failures MUST NOT block the main creation path.
        """
        try:
            await self._ws_manager.broadcast(
                event_type="order_placed",
                data=order_doc,
                tenant_id=order_doc["tenant_id"],
            )
        except Exception as exc:
            logger.warning(
                "OrderCreationService: WebSocket broadcast failed for "
                "order=%s: %s",
                order_doc.get("order_id"),
                exc,
            )

    # ------------------------------------------------------------------
    # Legacy dual-write
    # ------------------------------------------------------------------

    async def _dual_write_legacy_if_enabled(
        self,
        order_doc: Dict[str, Any],
        tenant_id: str,
    ) -> None:
        """Dual-write to the legacy surface if the feature flag allows.

        Failures are logged — they MUST NOT fail the main creation path.
        When the legacy dual-writer's ``mirror_order`` raises (which it
        should not by contract), we catch and log defensively.
        """
        if self._legacy_dual_writer is None:
            return
        if self._feature_flag_service is None:
            return

        try:
            overlay_state = await self._feature_flag_service.get_overlay_state(
                "order_intake_pipeline", tenant_id
            )
        except Exception as exc:
            logger.warning(
                "OrderCreationService: failed to read overlay state for "
                "tenant=%s: %s — skipping legacy mirror",
                tenant_id,
                exc,
            )
            return

        if overlay_state in ("shadow", "active_gated"):
            try:
                await self._legacy_dual_writer.mirror_order(
                    order_doc, tenant_id=tenant_id
                )
            except Exception as exc:
                # mirror_order should never raise by contract, but we
                # guard defensively so the main path is never blocked.
                logger.warning(
                    "OrderCreationService: LegacyDualWriter.mirror_order "
                    "raised unexpectedly for order=%s: %s",
                    order_doc.get("order_id"),
                    exc,
                )
