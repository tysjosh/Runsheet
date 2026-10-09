"""Dispatch Board order listener (design K10.4).

Subscribes through ``OrderService.subscribe`` to the order statuses that change
what a board lane can do with an order. For each event it finds the drafts for
today and later (tenant time, K2.5) that hold the order (``term order_ids``,
keyword array), marks the owning lane ``checks_stale`` without bumping any
version, and broadcasts ``board_lane_stale`` so open boards fetch ``?lanes=``.

Handler errors are caught and logged at WARNING: order processing is never
affected (the ``OrderService`` subscriber contract). This module writes only
board drafts (I4).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fuel.services import dispatch_board_eta as eta
from fuel.services.dispatch_board_es_mappings import DISPATCH_BOARD_DRAFTS_INDEX
from fuel.services.dispatch_board_models import draft_doc_id

logger = logging.getLogger(__name__)

#: K10.4 statuses (``order.<status>``).
BOARD_ORDER_EVENTS = ("cancelled", "on_hold", "scheduled", "dispatched", "in_transit", "delivered", "failed")
#: Drafts per order: today .. today + 14 (K10.4 ``size 15``).
LOOKUP_SIZE = 15


class BoardOrderListener:
    """Marks lanes stale when an order on them changes status (K10.4)."""

    def __init__(
        self,
        *,
        es_service: Any,
        broadcast: Optional[Callable[[str, date, str, Dict[str, Any]], Awaitable[None]]] = None,
        timezone_for: Optional[Callable[[str], str]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._es = es_service
        self._broadcast = broadcast
        self._timezone_for = timezone_for or (lambda tenant_id: "America/Chicago")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def subscribe(self, order_service: Any) -> None:
        """Register one handler per K10.4 status."""
        for status in BOARD_ORDER_EVENTS:
            order_service.subscribe(f"order.{status}", self._handler(status))

    def _handler(self, status: str) -> Callable[[Dict[str, Any]], Awaitable[None]]:
        async def on_board_order_event(order: Dict[str, Any]) -> None:
            await self.on_order_event(order, status)

        on_board_order_event.__name__ = f"dispatch_board_on_{status}"
        return on_board_order_event

    async def on_order_event(self, order: Dict[str, Any], status: str) -> List[str]:
        """Handle one event; returns the ``service_date:truck_id`` keys marked. Never raises."""
        try:
            return await self._handle(order or {}, status)
        except Exception as exc:
            logger.warning("dispatch board order listener failed: status=%s error=%s", status, type(exc).__name__)
            return []

    async def _handle(self, order: Dict[str, Any], status: str) -> List[str]:
        tenant_id = order.get("tenant_id")
        order_id = order.get("order_id")
        if not tenant_id or not order_id:
            return []
        today = eta.today_in(self._timezone_for(tenant_id), self._clock())
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"range": {"service_date": {"gte": today.isoformat()}}},
                        {"term": {"order_ids": order_id}},
                    ]
                }
            },
            "_source": ["service_date", "order_index", "tenant_id"],
            "size": LOOKUP_SIZE,
        }
        resp = await self._es.search_documents(DISPATCH_BOARD_DRAFTS_INDEX, query, LOOKUP_SIZE)
        marked: List[str] = []
        for hit in ((resp or {}).get("hits") or {}).get("hits") or []:
            source = hit.get("_source") or {}
            if source.get("tenant_id") not in (None, tenant_id):
                continue
            try:
                service_date = date.fromisoformat(str(source.get("service_date"))[:10])
            except ValueError:
                continue
            truck_id = await self._mark_stale(tenant_id, service_date, order_id)
            if truck_id is None:
                continue
            marked.append(f"{service_date.isoformat()}:{truck_id}")
            if self._broadcast is not None:
                await self._broadcast(
                    tenant_id,
                    service_date,
                    "board_lane_stale",
                    {"service_date": service_date.isoformat(), "truck_ids": [truck_id], "reason": f"order_{status}"},
                )
        return marked

    async def _mark_stale(self, tenant_id: str, service_date: date, order_id: str) -> Optional[str]:
        """``checks_stale = true`` on the lane that holds the order; no version bump."""
        owner: Dict[str, str] = {}

        def transform(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if current.get("tenant_id") != tenant_id:
                return None
            truck_id = (current.get("order_index") or {}).get(order_id)
            lane = (current.get("lanes") or {}).get(truck_id) if truck_id else None
            if lane is None:
                return None
            owner["truck_id"] = truck_id
            if lane.get("checks_stale") is True:
                return None
            lane["checks_stale"] = True
            return current

        await self._es.atomic_update(DISPATCH_BOARD_DRAFTS_INDEX, draft_doc_id(tenant_id, service_date), transform)
        return owner.get("truck_id")


__all__ = ["BoardOrderListener", "BOARD_ORDER_EVENTS"]
