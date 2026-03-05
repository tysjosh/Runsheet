"""
Fuel alert service for managing alert state and WebSocket broadcasting.

Handles threshold checking after stock changes and broadcasts fuel_alert
messages to connected dashboard clients via the OpsWebSocketManager.

Requirements covered:
- 4.1: GET /fuel/alerts returning active alerts
- 4.2: Status classification (normal, low, critical, empty)
- 4.3: WebSocket alert broadcasting within 5 seconds
- 4.4: Per-station threshold configuration
- 4.5: days_until_empty in alert data
- 4.6: Critical escalation when days_until_empty < 3
"""

import logging
from typing import Optional

from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class FuelAlertService:
    """
    Manages fuel alert state and WebSocket broadcasting.

    After every stock change (consumption or refill), ``check_thresholds``
    is called to determine whether a ``fuel_alert`` message should be
    broadcast to connected dashboard clients.
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        ws_manager: Optional[object] = None,
    ):
        """
        Args:
            es_service: Shared Elasticsearch service instance.
            ws_manager: Optional OpsWebSocketManager for broadcasting alerts.
                        When ``None``, alerts are logged but not broadcast.
        """
        self._es = es_service
        self._ws_manager = ws_manager

    async def check_thresholds(self, station_data: dict) -> None:
        """
        Evaluate a station's stock status and broadcast an alert if needed.

        Called after ``record_consumption`` and ``record_refill`` with the
        updated station data dict. If the station's status is anything other
        than ``"normal"``, a ``fuel_alert`` WebSocket message is broadcast.

        Args:
            station_data: Dict containing at minimum: station_id, name,
                fuel_type, status, current_stock_liters, capacity_liters,
                days_until_empty, and optionally location_name.
        """
        status = station_data.get("status", "normal")

        if status == "normal":
            logger.debug(
                "Station %s status is normal — no alert broadcast",
                station_data.get("station_id"),
            )
            return

        capacity = station_data.get("capacity_liters", 0.0)
        current_stock = station_data.get("current_stock_liters", 0.0)
        stock_pct = (current_stock / capacity * 100.0) if capacity > 0 else 0.0

        alert_data: dict = {
            "station_id": station_data.get("station_id", ""),
            "name": station_data.get("name", ""),
            "fuel_type": station_data.get("fuel_type", ""),
            "status": status,
            "current_stock_liters": current_stock,
            "capacity_liters": capacity,
            "stock_percentage": round(stock_pct, 2),
            "days_until_empty": station_data.get("days_until_empty", 0.0),
            "location_name": station_data.get("location_name"),
        }

        # Include tenant_id so the WS manager can scope the broadcast
        tenant_id = station_data.get("tenant_id")
        if tenant_id:
            alert_data["tenant_id"] = tenant_id

        logger.info(
            "Fuel alert: station=%s, status=%s, stock_pct=%.1f%%, days_until_empty=%.1f",
            alert_data["station_id"],
            status,
            stock_pct,
            alert_data["days_until_empty"],
        )

        if self._ws_manager is not None:
            try:
                await self._ws_manager.broadcast_fuel_alert(alert_data)
            except Exception as exc:
                logger.warning(
                    "Failed to broadcast fuel alert for station %s: %s",
                    alert_data["station_id"],
                    exc,
                )
