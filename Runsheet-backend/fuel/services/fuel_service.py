"""
Core fuel service handling station CRUD, consumption/refill recording,
alert logic, and consumption analytics.

Requirements covered:
- 1.1-1.7: Fuel station registry and stock tracking
- 2.1-2.7: Fuel consumption recording (stub)
- 3.1-3.5: Fuel refill recording (stub)
- 4.1-4.6: Fuel alerts and thresholds (stub)
- 5.1-5.5: Fuel consumption analytics (stub)
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config.settings import get_settings
from errors.exceptions import resource_not_found, validation_error
from fuel.models import (
    BatchResult,
    ConsumptionEvent,
    ConsumptionResult,
    EfficiencyMetric,
    FuelAlert,
    FuelNetworkSummary,
    FuelStation,
    FuelStationDetail,
    CreateFuelStation,
    MetricsBucket,
    PaginatedResponse,
    PaginationMeta,
    RefillEvent,
    RefillResult,
    UpdateFuelStation,
)
from fuel.services.fuel_es_mappings import FUEL_EVENTS_INDEX, FUEL_STATIONS_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


class FuelService:
    """Manages fuel station state, consumption/refill recording, and analytics."""

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _make_doc_id(self, station_id: str, fuel_type: str) -> str:
        """Build the composite document ID used in the fuel_stations index."""
        return f"{station_id}::{fuel_type}"

    def _calculate_daily_rate(self, events: list[dict]) -> float:
        """
        Calculate rolling average daily consumption from recent events.

        Args:
            events: List of consumption event dicts within the rolling window.

        Returns:
            Average daily consumption in liters. 0.0 when no events exist.
        """
        if not events:
            return 0.0

        window_days = self._settings.fuel_consumption_rolling_window_days
        total_liters = sum(e.get("quantity_liters", 0.0) for e in events)
        return total_liters / max(window_days, 1)

    def _calculate_days_until_empty(
        self, current_stock: float, daily_rate: float
    ) -> float:
        """
        Estimate days until stock reaches zero.

        Returns:
            Estimated days. ``float('inf')`` when daily_rate is zero.
        """
        if daily_rate <= 0:
            return float("inf")
        return current_stock / daily_rate

    def _determine_status(
        self,
        current_stock: float,
        capacity: float,
        threshold_pct: float,
        days_until_empty: float,
    ) -> str:
        """
        Classify stock status: normal, low, critical, empty.

        Rules (evaluated in order):
        1. empty  – stock is 0
        2. critical – stock below 10 % of capacity OR days_until_empty < critical threshold
        3. low – stock below alert threshold percentage
        4. normal – everything else
        """
        if current_stock <= 0:
            return "empty"

        stock_pct = (current_stock / capacity) * 100.0 if capacity > 0 else 0.0
        critical_days = self._settings.fuel_critical_days_threshold

        if stock_pct < 10.0 or days_until_empty < critical_days:
            return "critical"
        if stock_pct < threshold_pct:
            return "low"
        return "normal"

    # ------------------------------------------------------------------
    # Station CRUD  (Requirements 1.1 – 1.7)
    # ------------------------------------------------------------------

    async def list_stations(
        self,
        tenant_id: str,
        fuel_type: Optional[str] = None,
        status: Optional[str] = None,
        location: Optional[str] = None,
        page: int = 1,
        size: int = 50,
    ) -> PaginatedResponse[FuelStation]:
        """
        List fuel stations with optional filtering and pagination.

        Validates: Requirement 1.1, 1.6
        """
        filters: list[dict] = [{"term": {"tenant_id": tenant_id}}]

        if fuel_type:
            filters.append({"term": {"fuel_type": fuel_type}})
        if status:
            filters.append({"term": {"status": status}})
        if location:
            filters.append(
                {
                    "multi_match": {
                        "query": location,
                        "fields": ["location_name", "name"],
                    }
                }
            )

        from_offset = (page - 1) * size
        query: dict = {
            "query": {"bool": {"must": filters}},
            "from": from_offset,
            "size": size,
            "sort": [{"last_updated": {"order": "desc"}}],
        }

        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=size
        )

        total = response["hits"]["total"]["value"]
        stations = [
            FuelStation(**hit["_source"]) for hit in response["hits"]["hits"]
        ]

        return PaginatedResponse[FuelStation](
            data=stations,
            pagination=PaginationMeta.compute(page=page, size=size, total=total),
            request_id=str(uuid.uuid4()),
        )

    async def get_station(
        self, station_id: str, tenant_id: str
    ) -> FuelStationDetail:
        """
        Return a single station with recent consumption and refill events.

        Validates: Requirement 1.2
        """
        # Search for the station document(s) matching station_id + tenant
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": station_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Fuel station '{station_id}' not found",
                details={"station_id": station_id},
            )

        station = FuelStation(**hits[0]["_source"])

        # Fetch recent events for this station
        events_query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": station_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "sort": [{"event_timestamp": {"order": "desc"}}],
            "size": 20,
        }
        events_response = await self._es.search_documents(
            FUEL_EVENTS_INDEX, events_query, size=20
        )

        consumption_events = []
        refill_events = []
        for hit in events_response["hits"]["hits"]:
            src = hit["_source"]
            if src.get("event_type") == "consumption":
                consumption_events.append(
                    ConsumptionEvent(
                        station_id=src["station_id"],
                        fuel_type=src["fuel_type"],
                        quantity_liters=src["quantity_liters"],
                        asset_id=src.get("asset_id", ""),
                        operator_id=src.get("operator_id", ""),
                        odometer_reading=src.get("odometer_reading"),
                    )
                )
            elif src.get("event_type") == "refill":
                refill_events.append(
                    RefillEvent(
                        station_id=src["station_id"],
                        fuel_type=src["fuel_type"],
                        quantity_liters=src["quantity_liters"],
                        supplier=src.get("supplier", ""),
                        delivery_reference=src.get("delivery_reference"),
                        operator_id=src.get("operator_id", ""),
                    )
                )

        return FuelStationDetail(
            station=station,
            recent_consumption_events=consumption_events,
            recent_refill_events=refill_events,
        )

    async def create_station(
        self, station: CreateFuelStation, tenant_id: str
    ) -> FuelStation:
        """
        Register a new fuel station.

        Validates: Requirement 1.3, 1.5
        """
        # Pydantic already enforces capacity > 0 and initial_stock >= 0,
        # but we must also check initial_stock <= capacity.
        if station.initial_stock_liters > station.capacity_liters:
            raise validation_error(
                "initial_stock_liters cannot exceed capacity_liters",
                details={
                    "initial_stock_liters": station.initial_stock_liters,
                    "capacity_liters": station.capacity_liters,
                },
            )

        now = datetime.now(timezone.utc).isoformat()
        daily_rate = 0.0
        days_empty = self._calculate_days_until_empty(
            station.initial_stock_liters, daily_rate
        )
        status = self._determine_status(
            station.initial_stock_liters,
            station.capacity_liters,
            station.alert_threshold_pct,
            days_empty,
        )

        doc: dict = {
            "station_id": station.station_id,
            "name": station.name,
            "fuel_type": station.fuel_type,
            "capacity_liters": station.capacity_liters,
            "current_stock_liters": station.initial_stock_liters,
            "daily_consumption_rate": daily_rate,
            "days_until_empty": days_empty,
            "alert_threshold_pct": station.alert_threshold_pct,
            "status": status,
            "location": station.location.model_dump() if station.location else None,
            "location_name": station.location_name,
            "tenant_id": tenant_id,
            "created_at": now,
            "last_updated": now,
        }

        doc_id = self._make_doc_id(station.station_id, station.fuel_type)
        await self._es.index_document(FUEL_STATIONS_INDEX, doc_id, doc)

        logger.info(
            "Created fuel station %s (fuel_type=%s, tenant=%s)",
            station.station_id,
            station.fuel_type,
            tenant_id,
        )

        return FuelStation(**doc)

    async def update_station(
        self, station_id: str, update: UpdateFuelStation, tenant_id: str
    ) -> FuelStation:
        """
        Update station metadata (not stock levels).

        Validates: Requirement 1.4
        """
        # Find the existing station first
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": station_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Fuel station '{station_id}' not found",
                details={"station_id": station_id},
            )

        existing = hits[0]["_source"]
        doc_id = hits[0]["_id"]

        # Build partial update from non-None fields
        partial: dict = {}
        if update.name is not None:
            partial["name"] = update.name
        if update.capacity_liters is not None:
            partial["capacity_liters"] = update.capacity_liters
        if update.alert_threshold_pct is not None:
            partial["alert_threshold_pct"] = update.alert_threshold_pct
        if update.location is not None:
            partial["location"] = update.location.model_dump()
        if update.location_name is not None:
            partial["location_name"] = update.location_name

        if not partial:
            # Nothing to update – return current state
            return FuelStation(**existing)

        partial["last_updated"] = datetime.now(timezone.utc).isoformat()

        # If capacity changed, recalculate status
        capacity = partial.get("capacity_liters", existing["capacity_liters"])
        threshold = partial.get(
            "alert_threshold_pct", existing["alert_threshold_pct"]
        )
        current_stock = existing["current_stock_liters"]
        days_empty = existing.get("days_until_empty", float("inf"))

        partial["status"] = self._determine_status(
            current_stock, capacity, threshold, days_empty
        )

        await self._es.update_document(FUEL_STATIONS_INDEX, doc_id, partial)

        # Merge partial into existing to return the full updated station
        merged = {**existing, **partial}
        return FuelStation(**merged)

    # ------------------------------------------------------------------
    # Consumption recording  (Task 3.2 – Requirements 2.1-2.7)
    # ------------------------------------------------------------------

    async def record_consumption(
        self, event: ConsumptionEvent, tenant_id: str
    ) -> ConsumptionResult:
        """
        Record a fuel dispensing event.

        Validates: Requirements 2.1-2.6
        - Deducts quantity from current_stock_liters
        - Appends consumption event to fuel_events index
        - Rejects with 400 if insufficient stock
        - Recalculates daily_consumption_rate from rolling 7-day window
        - Recalculates days_until_empty
        - Updates station status based on new stock level
        """
        # 1. Find the station document
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": event.station_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Fuel station '{event.station_id}' not found",
                details={"station_id": event.station_id},
            )

        existing = hits[0]["_source"]
        doc_id = hits[0]["_id"]
        current_stock = existing["current_stock_liters"]

        # 2. Validate sufficient stock
        if current_stock < event.quantity_liters:
            raise validation_error(
                "Insufficient fuel stock for consumption",
                details={
                    "station_id": event.station_id,
                    "current_stock_liters": current_stock,
                    "requested_liters": event.quantity_liters,
                },
            )

        # 3. Deduct stock
        new_stock = current_stock - event.quantity_liters
        now = datetime.now(timezone.utc).isoformat()

        # 4. Append consumption event to fuel_events index
        event_id = str(uuid.uuid4())
        event_doc: dict = {
            "event_id": event_id,
            "station_id": event.station_id,
            "event_type": "consumption",
            "fuel_type": event.fuel_type,
            "quantity_liters": event.quantity_liters,
            "asset_id": event.asset_id,
            "operator_id": event.operator_id,
            "odometer_reading": event.odometer_reading,
            "tenant_id": tenant_id,
            "event_timestamp": now,
            "ingested_at": now,
        }
        await self._es.index_document(FUEL_EVENTS_INDEX, event_id, event_doc)

        # 5. Query last 7 days of consumption events to recalculate daily rate
        window_days = self._settings.fuel_consumption_rolling_window_days
        window_query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": event.station_id}},
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"event_type": "consumption"}},
                        {"range": {"event_timestamp": {"gte": f"now-{window_days}d"}}},
                    ]
                }
            },
            "size": 10000,
        }
        events_response = await self._es.search_documents(
            FUEL_EVENTS_INDEX, window_query, size=10000
        )
        recent_events = [h["_source"] for h in events_response["hits"]["hits"]]

        daily_rate = self._calculate_daily_rate(recent_events)
        days_until_empty = self._calculate_days_until_empty(new_stock, daily_rate)

        # 6. Determine new status
        capacity = existing["capacity_liters"]
        threshold_pct = existing["alert_threshold_pct"]
        new_status = self._determine_status(
            new_stock, capacity, threshold_pct, days_until_empty
        )

        # 7. Update station document
        partial_update: dict = {
            "current_stock_liters": new_stock,
            "daily_consumption_rate": daily_rate,
            "days_until_empty": days_until_empty,
            "status": new_status,
            "last_updated": now,
        }
        await self._es.update_document(FUEL_STATIONS_INDEX, doc_id, partial_update)

        logger.info(
            "Recorded consumption: station=%s, quantity=%.2f, new_stock=%.2f, status=%s",
            event.station_id,
            event.quantity_liters,
            new_stock,
            new_status,
        )

        return ConsumptionResult(
            event_id=event_id,
            station_id=event.station_id,
            new_stock_liters=new_stock,
            status=new_status,
        )

    async def record_consumption_batch(
        self, events: list[ConsumptionEvent], tenant_id: str
    ) -> BatchResult:
        """
        Batch consumption recording. Processes each event individually,
        collecting results and errors.

        Validates: Requirement 2.7
        """
        results: list[ConsumptionResult] = []
        errors: list[str] = []

        for event in events:
            try:
                result = await self.record_consumption(event, tenant_id)
                results.append(result)
            except Exception as exc:
                errors.append(
                    f"station={event.station_id}, fuel_type={event.fuel_type}: {exc}"
                )

        return BatchResult(
            processed=len(results),
            failed=len(errors),
            results=results,
            errors=errors,
        )


    # ------------------------------------------------------------------
    # Refill recording  (Task 3.3 – Requirements 3.1-3.5)
    # ------------------------------------------------------------------

    async def record_refill(
        self, event: RefillEvent, tenant_id: str
    ) -> RefillResult:
        """
        Record a fuel delivery event.

        Validates: Requirements 3.1-3.5
        - Adds quantity to current_stock_liters
        - Appends refill event to fuel_events index
        - Rejects with 400 if stock + quantity > capacity (overflow)
        - Updates station status based on new stock level
        - Clears active alerts if stock restored above threshold
        """
        # 1. Find the station document
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": event.station_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Fuel station '{event.station_id}' not found",
                details={"station_id": event.station_id},
            )

        existing = hits[0]["_source"]
        doc_id = hits[0]["_id"]
        current_stock = existing["current_stock_liters"]
        capacity = existing["capacity_liters"]

        # 2. Validate no overflow
        new_stock = current_stock + event.quantity_liters
        if new_stock > capacity:
            raise validation_error(
                "Refill would exceed station capacity",
                details={
                    "station_id": event.station_id,
                    "current_stock_liters": current_stock,
                    "refill_liters": event.quantity_liters,
                    "capacity_liters": capacity,
                },
            )

        now = datetime.now(timezone.utc).isoformat()

        # 3. Append refill event to fuel_events index
        event_id = str(uuid.uuid4())
        event_doc: dict = {
            "event_id": event_id,
            "station_id": event.station_id,
            "event_type": "refill",
            "fuel_type": event.fuel_type,
            "quantity_liters": event.quantity_liters,
            "supplier": event.supplier,
            "delivery_reference": event.delivery_reference,
            "operator_id": event.operator_id,
            "tenant_id": tenant_id,
            "event_timestamp": now,
            "ingested_at": now,
        }
        await self._es.index_document(FUEL_EVENTS_INDEX, event_id, event_doc)

        # 4. Recalculate days_until_empty using existing daily rate
        daily_rate = existing.get("daily_consumption_rate", 0.0)
        days_until_empty = self._calculate_days_until_empty(new_stock, daily_rate)

        # 5. Determine new status (clears alerts naturally if stock above threshold)
        threshold_pct = existing["alert_threshold_pct"]
        new_status = self._determine_status(
            new_stock, capacity, threshold_pct, days_until_empty
        )

        # 6. Update station document
        partial_update: dict = {
            "current_stock_liters": new_stock,
            "days_until_empty": days_until_empty,
            "status": new_status,
            "last_updated": now,
        }
        await self._es.update_document(FUEL_STATIONS_INDEX, doc_id, partial_update)

        logger.info(
            "Recorded refill: station=%s, quantity=%.2f, new_stock=%.2f, status=%s",
            event.station_id,
            event.quantity_liters,
            new_stock,
            new_status,
        )

        return RefillResult(
            event_id=event_id,
            station_id=event.station_id,
            new_stock_liters=new_stock,
            status=new_status,
        )

    # ------------------------------------------------------------------
    # Alerts and thresholds  (Task 3.4 – Requirements 4.1-4.6)
    # ------------------------------------------------------------------

    async def get_alerts(self, tenant_id: str) -> list[FuelAlert]:
        """
        Return all active fuel alerts (stations with status != normal).

        Validates: Requirement 4.1, 4.5
        """
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["low", "critical", "empty"]}},
                    ]
                }
            },
            "size": 1000,
            "sort": [{"status": {"order": "asc"}}, {"last_updated": {"order": "desc"}}],
        }

        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=1000
        )

        alerts: list[FuelAlert] = []
        for hit in response["hits"]["hits"]:
            src = hit["_source"]
            capacity = src.get("capacity_liters", 0.0)
            current_stock = src.get("current_stock_liters", 0.0)
            stock_pct = (current_stock / capacity * 100.0) if capacity > 0 else 0.0

            alerts.append(
                FuelAlert(
                    station_id=src["station_id"],
                    name=src.get("name", ""),
                    fuel_type=src.get("fuel_type", ""),
                    status=src["status"],
                    current_stock_liters=current_stock,
                    capacity_liters=capacity,
                    stock_percentage=round(stock_pct, 2),
                    days_until_empty=src.get("days_until_empty", 0.0),
                    location_name=src.get("location_name"),
                )
            )

        return alerts

    async def update_threshold(
        self, station_id: str, threshold_pct: float, tenant_id: str
    ) -> FuelStation:
        """
        Update per-station alert threshold and recalculate status.

        Validates: Requirement 4.4
        """
        if threshold_pct < 0 or threshold_pct > 100:
            raise validation_error(
                "alert_threshold_pct must be between 0 and 100",
                details={"alert_threshold_pct": threshold_pct},
            )

        # Find the station
        query: dict = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"station_id": station_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }
        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=1
        )

        hits = response["hits"]["hits"]
        if not hits:
            raise resource_not_found(
                f"Fuel station '{station_id}' not found",
                details={"station_id": station_id},
            )

        existing = hits[0]["_source"]
        doc_id = hits[0]["_id"]

        # Recalculate status with the new threshold
        current_stock = existing["current_stock_liters"]
        capacity = existing["capacity_liters"]
        days_until_empty = existing.get("days_until_empty", float("inf"))

        new_status = self._determine_status(
            current_stock, capacity, threshold_pct, days_until_empty
        )

        now = datetime.now(timezone.utc).isoformat()
        partial: dict = {
            "alert_threshold_pct": threshold_pct,
            "status": new_status,
            "last_updated": now,
        }

        await self._es.update_document(FUEL_STATIONS_INDEX, doc_id, partial)

        merged = {**existing, **partial}
        logger.info(
            "Updated threshold for station %s: %.1f%% -> status=%s",
            station_id,
            threshold_pct,
            new_status,
        )

        return FuelStation(**merged)

    # ------------------------------------------------------------------
    # Consumption analytics  (Task 3.5 – Requirements 5.1-5.5)
    # ------------------------------------------------------------------

    async def get_consumption_metrics(
        self,
        tenant_id: str,
        bucket: str = "daily",
        station_id: Optional[str] = None,
        fuel_type: Optional[str] = None,
        asset_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[MetricsBucket]:
        """
        Consumption aggregated by time bucket using ES date_histogram.

        Validates: Requirements 5.1, 5.3, 5.5
        - Supports hourly, daily, weekly buckets
        - Enforces daily bucket for time ranges > 90 days
        - Supports filtering by station_id, fuel_type, asset_id, date range
        """
        # Map bucket names to ES calendar intervals
        interval_map = {
            "hourly": "1h",
            "daily": "1d",
            "weekly": "1w",
        }

        # Enforce daily bucket for time ranges > 90 days (Requirement 5.5)
        if start_date and end_date:
            from datetime import datetime as dt
            try:
                start_dt = dt.fromisoformat(start_date.replace("Z", "+00:00"))
                end_dt = dt.fromisoformat(end_date.replace("Z", "+00:00"))
                if (end_dt - start_dt).days > 90:
                    bucket = "daily"
            except (ValueError, TypeError):
                pass

        calendar_interval = interval_map.get(bucket, "1d")

        # Build filter clauses
        filters: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
            {"term": {"event_type": "consumption"}},
        ]
        if station_id:
            filters.append({"term": {"station_id": station_id}})
        if fuel_type:
            filters.append({"term": {"fuel_type": fuel_type}})
        if asset_id:
            filters.append({"term": {"asset_id": asset_id}})

        # Date range filter
        date_range: dict = {}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        if date_range:
            filters.append({"range": {"event_timestamp": date_range}})

        query: dict = {
            "query": {"bool": {"must": filters}},
            "size": 0,
            "aggs": {
                "consumption_over_time": {
                    "date_histogram": {
                        "field": "event_timestamp",
                        "calendar_interval": calendar_interval,
                    },
                    "aggs": {
                        "total_liters": {"sum": {"field": "quantity_liters"}},
                    },
                }
            },
        }

        response = await self._es.search_documents(
            FUEL_EVENTS_INDEX, query, size=0
        )

        buckets = response.get("aggregations", {}).get(
            "consumption_over_time", {}
        ).get("buckets", [])

        return [
            MetricsBucket(
                timestamp=b["key_as_string"],
                total_liters=b["total_liters"]["value"],
                event_count=b["doc_count"],
                station_id=station_id,
                fuel_type=fuel_type,
            )
            for b in buckets
        ]

    async def get_efficiency_metrics(
        self,
        tenant_id: str,
        asset_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[EfficiencyMetric]:
        """
        Fuel efficiency per asset (liters per km) when odometer data available.

        Validates: Requirements 5.2, 5.3
        - Groups consumption events by asset_id using ES terms aggregation
        - Calculates total_liters, total_distance_km, liters_per_km
        - Supports filtering by asset_id and date range
        """
        filters: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
            {"term": {"event_type": "consumption"}},
        ]
        if asset_id:
            filters.append({"term": {"asset_id": asset_id}})

        date_range: dict = {}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        if date_range:
            filters.append({"range": {"event_timestamp": date_range}})

        query: dict = {
            "query": {"bool": {"must": filters}},
            "size": 0,
            "aggs": {
                "by_asset": {
                    "terms": {
                        "field": "asset_id",
                        "size": 10000,
                    },
                    "aggs": {
                        "total_liters": {"sum": {"field": "quantity_liters"}},
                        "min_odometer": {"min": {"field": "odometer_reading"}},
                        "max_odometer": {"max": {"field": "odometer_reading"}},
                    },
                }
            },
        }

        response = await self._es.search_documents(
            FUEL_EVENTS_INDEX, query, size=0
        )

        asset_buckets = response.get("aggregations", {}).get(
            "by_asset", {}
        ).get("buckets", [])

        results: list[EfficiencyMetric] = []
        for b in asset_buckets:
            total_liters = b["total_liters"]["value"]
            min_odo = b["min_odometer"]["value"]
            max_odo = b["max_odometer"]["value"]

            total_distance_km: Optional[float] = None
            liters_per_km: Optional[float] = None

            if min_odo is not None and max_odo is not None:
                total_distance_km = max_odo - min_odo
                if total_distance_km > 0:
                    liters_per_km = total_liters / total_distance_km

            results.append(
                EfficiencyMetric(
                    asset_id=b["key"],
                    total_liters=total_liters,
                    total_distance_km=total_distance_km,
                    liters_per_km=liters_per_km,
                    event_count=b["doc_count"],
                )
            )

        return results

    async def get_network_summary(self, tenant_id: str) -> FuelNetworkSummary:
        """
        Network-wide fuel summary aggregated across all stations.

        Validates: Requirement 5.4
        - Aggregates: total_stations, total_capacity, total_stock,
          total_daily_consumption, average_days_until_empty, count by status
        """
        query: dict = {
            "query": {
                "bool": {
                    "must": [{"term": {"tenant_id": tenant_id}}]
                }
            },
            "size": 0,
            "aggs": {
                "total_capacity": {"sum": {"field": "capacity_liters"}},
                "total_stock": {"sum": {"field": "current_stock_liters"}},
                "total_daily_consumption": {"sum": {"field": "daily_consumption_rate"}},
                "avg_days_until_empty": {"avg": {"field": "days_until_empty"}},
                "by_status": {
                    "terms": {
                        "field": "status",
                        "size": 10,
                    }
                },
            },
        }

        response = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        total_stations = response["hits"]["total"]["value"]

        # Parse status counts
        status_counts: dict[str, int] = {
            "normal": 0,
            "low": 0,
            "critical": 0,
            "empty": 0,
        }
        for b in aggs.get("by_status", {}).get("buckets", []):
            key = b["key"]
            if key in status_counts:
                status_counts[key] = b["doc_count"]

        active_alerts = (
            status_counts["low"]
            + status_counts["critical"]
            + status_counts["empty"]
        )

        avg_days = aggs.get("avg_days_until_empty", {}).get("value")
        if avg_days is None:
            avg_days = 0.0

        return FuelNetworkSummary(
            total_stations=total_stations,
            total_capacity_liters=aggs.get("total_capacity", {}).get("value", 0.0),
            total_current_stock_liters=aggs.get("total_stock", {}).get("value", 0.0),
            total_daily_consumption=aggs.get("total_daily_consumption", {}).get("value", 0.0),
            average_days_until_empty=avg_days,
            stations_normal=status_counts["normal"],
            stations_low=status_counts["low"],
            stations_critical=status_counts["critical"],
            stations_empty=status_counts["empty"],
            active_alerts=active_alerts,
        )

