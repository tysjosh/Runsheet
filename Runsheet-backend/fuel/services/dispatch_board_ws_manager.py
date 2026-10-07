"""Dispatch Board socket: ``/ws/dispatch-board`` fan-out and presence (design K10).

Connections are keyed by ``(tenant_id, service_date)``: a board event reaches
only the boards open on that tenant and day. Messages use the shared
``{type, data, timestamp}`` envelope (``FuelPlanningWSManager``,
``AgentActivityWSManager``).

* Server -> client (K10.2): ``board_lanes_updated``, ``board_lane_stale``,
  ``board_presence``, ``board_publish_progress``, ``board_suggestions_changed``.
  A ``board_lanes_updated`` message over 256 KB carries only
  ``{truck_id, version}`` per lane (``lanes_truncated: true``) and clients
  fetch ``?lanes=``.
* Client -> server (K10.3): ``{"type": "presence", "focus_truck_id": ...}``
  every 15 s and on focus change; ``{"type": "ping"}``. Presence entries
  expire 45 s after the last message. Presence is in memory only (backlog B3);
  board correctness never depends on it.

Socket delivery is best effort: versions and refetch keep boards correct.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import WebSocket

from fuel.services.dispatch_board_models import ID_PATTERN
from websocket.base_ws_manager import BaseWSManager

logger = logging.getLogger(__name__)

#: K10.2 payload cap for ``board_lanes_updated``.
MAX_PAYLOAD_BYTES = 256 * 1024
#: K10.3 presence expiry.
PRESENCE_TTL_S = 45.0

_ID_RE = re.compile(ID_PATTERN)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class DispatchBoardWSManager(BaseWSManager):
    """Per ``(tenant, service_date)`` board events and presence (K10)."""

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None, max_payload_bytes: int = MAX_PAYLOAD_BYTES) -> None:
        super().__init__(manager_name="dispatch_board")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_payload_bytes = max_payload_bytes

    # -- connections -------------------------------------------------------

    async def connect_board(
        self,
        websocket: WebSocket,
        *,
        tenant_id: str,
        user_id: str,
        service_date: str,
        name: str,
    ) -> None:
        """Accept a board client and announce the new presence list."""
        await self.connect(
            websocket,
            tenant_id=tenant_id,
            metadata={
                "user_id": user_id,
                "service_date": service_date,
                "name": name,
                "focus_truck_id": None,
                "last_seen": self._clock(),
            },
        )
        await self.broadcast_presence(tenant_id, service_date)

    async def disconnect(self, websocket: WebSocket) -> None:
        meta = self._clients.get(websocket)
        await super().disconnect(websocket)
        if meta is not None:
            await self.broadcast_presence(meta.get("tenant_id") or "", meta.get("service_date") or "")

    def _scope(self, tenant_id: str, service_date: str) -> List[Tuple[WebSocket, Dict[str, Any]]]:
        if not tenant_id or not service_date:
            return []
        return [
            (ws, meta)
            for ws, meta in self._clients.items()
            if meta.get("tenant_id") == tenant_id and meta.get("service_date") == service_date
        ]

    def _envelope(self, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": event_type, "data": data, "timestamp": _iso(self._clock())}

    # -- server -> client (K10.2) ------------------------------------------

    async def broadcast_board_event(self, tenant_id: str, service_date: str, event_type: str, data: Dict[str, Any]) -> int:
        """Send one event to the boards open on ``(tenant_id, service_date)``."""
        async with self._lock:
            clients = self._scope(tenant_id, service_date)
        if not clients:
            return 0
        message = self._envelope(event_type, data)
        if event_type == "board_lanes_updated" and self._too_big(message):
            slim = dict(data)
            slim["lanes"] = [
                {"truck_id": lane.get("truck_id"), "version": lane.get("version")}
                for lane in data.get("lanes") or []
                if isinstance(lane, dict)
            ]
            slim["lanes_truncated"] = True
            message = self._envelope(event_type, slim)
        return await self._send_to(clients, message)

    def _too_big(self, message: Dict[str, Any]) -> bool:
        try:
            size = len(json.dumps(message, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return True
        return size > self.max_payload_bytes

    def presence(self, tenant_id: str, service_date: str) -> List[Dict[str, Any]]:
        """Live users on the day, one entry per user (latest message wins)."""
        now = self._clock()
        users: Dict[str, Dict[str, Any]] = {}
        for _ws, meta in self._scope(tenant_id, service_date):
            seen = meta.get("last_seen") or meta.get("connected_at")
            if seen is None or (now - seen).total_seconds() > PRESENCE_TTL_S:
                continue
            user_id = meta.get("user_id") or ""
            current = users.get(user_id)
            if current is not None and current["_seen"] >= seen:
                continue
            users[user_id] = {
                "user_id": user_id,
                "name": meta.get("name"),
                "focus_truck_id": meta.get("focus_truck_id"),
                "last_seen": _iso(seen),
                "_seen": seen,
            }
        out = []
        for entry in sorted(users.values(), key=lambda e: e["user_id"]):
            entry.pop("_seen")
            out.append(entry)
        return out

    async def broadcast_presence(self, tenant_id: str, service_date: str) -> int:
        return await self.broadcast_board_event(
            tenant_id,
            service_date,
            "board_presence",
            {"service_date": service_date, "users": self.presence(tenant_id, service_date)},
        )

    # -- client -> server (K10.3) ------------------------------------------

    async def handle_client_message(self, websocket: WebSocket, raw: str) -> None:
        """``presence`` and ``ping``; anything else gets an error frame."""
        meta = self._clients.get(websocket)
        if meta is None:
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            await self._send_to_client(websocket, {"type": "error", "message": "Invalid JSON"})
            return
        if not isinstance(msg, dict):
            await self._send_to_client(websocket, {"type": "error", "message": "Invalid message"})
            return
        kind = msg.get("type")
        if kind == "ping":
            await self._send_to_client(websocket, {"type": "pong", "timestamp": _iso(self._clock())})
            return
        if kind == "presence":
            focus = msg.get("focus_truck_id")
            if focus is not None and not (isinstance(focus, str) and _ID_RE.match(focus)):
                await self._send_to_client(websocket, {"type": "error", "message": "Invalid focus_truck_id"})
                return
            meta["focus_truck_id"] = focus
            meta["last_seen"] = self._clock()
            await self.broadcast_presence(meta.get("tenant_id") or "", meta.get("service_date") or "")
            return
        await self._send_to_client(websocket, {"type": "error", "message": "Unknown message type"})


_manager: Optional[DispatchBoardWSManager] = None


def get_dispatch_board_ws_manager() -> DispatchBoardWSManager:
    """The process-wide board socket manager."""
    global _manager
    if _manager is None:
        _manager = DispatchBoardWSManager()
    return _manager


__all__ = [
    "DispatchBoardWSManager",
    "get_dispatch_board_ws_manager",
    "MAX_PAYLOAD_BYTES",
    "PRESENCE_TTL_S",
]
