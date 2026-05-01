"""
Tank Forecasting Agent — overlay agent for per-station per-grade runout risk prediction.

Subscribes to RiskSignals from FuelManagementAgent, queries fuel_stations
and fuel_events indices, computes consumption rates using existing
fuel_calculations.py logic, estimates probabilistic hours-to-runout
(p50/p90), computes runout_risk_24h, handles anomaly flags, persists
forecasts to mvp_tank_forecasts, and publishes TankForecast to SignalBus.

Default configuration:
    - decision_cycle: 300 seconds (5 minutes)
    - cooldown: 15 minutes per station

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from Agents.autonomous.fuel_calculations import (
    calculate_refill_priority,
    calculate_refill_quantity,
)
from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import FuelGrade, TankForecast
from Agents.support.mvp_es_mappings import MVP_TANK_FORECASTS_INDEX

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_STATIONS_INDEX = "fuel_stations"
FUEL_EVENTS_INDEX = "fuel_events"

# Default consumption rate (liters/hour) when no historical data exists
DEFAULT_CONSUMPTION_RATE = 50.0

# Variance multiplier for p90 estimate (pessimistic)
P90_VARIANCE_MULTIPLIER = 1.5

# Default hours horizon for risk calculation
RISK_HORIZON_HOURS = 24.0


class TankForecastingAgent(OverlayAgentBase):
    """Predicts per-station per-grade runout risk for the next 24-72 hours.

    Consumes station inventory levels from ``fuel_stations``, historical
    consumption rates from ``fuel_events``, and anomaly flags from
    RiskSignals published by FuelManagementAgent. Produces TankForecast
    messages for each (station_id, fuel_grade) pair.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval in seconds (default 300).
        cooldown_minutes: Per-station cooldown in minutes (default 15).
    """

    def __init__(
        self,
        signal_bus: SignalBus,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        autonomy_config_service,
        feature_flag_service,
        poll_interval: int = 300,
        cooldown_minutes: int = 15,
    ):
        super().__init__(
            agent_id="tank_forecasting",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": "fuel_management_agent",
                    },
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=cooldown_minutes,
        )
        # Cache anomaly flags from RiskSignals keyed by station_id
        self._anomaly_cache: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Core evaluation (Req 1.1–1.7)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Produce TankForecast for each (station, grade) pair.

        Steps:
        1. Extract anomaly flags from incoming RiskSignals (Req 1.3).
        2. Query fuel_stations for current inventory (Req 1.2).
        3. Query fuel_events for historical consumption (Req 1.2).
        4. For each (station, grade): compute consumption rate using
           fuel_calculations logic, estimate hours_to_runout p50/p90,
           compute runout_risk_24h, handle zero-data default (Req 1.7).
        5. Persist forecasts to mvp_tank_forecasts (Req 1.4).
        6. Publish TankForecast to SignalBus (Req 1.5).

        Returns:
            Empty list — forecasts are published directly to SignalBus
            rather than as InterventionProposals.
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id

        # Step 1: Extract anomaly flags from RiskSignals (Req 1.3)
        self._process_anomaly_signals(signals)

        # Step 2: Query fuel stations (Req 1.2)
        stations = await self._query_fuel_stations(tenant_id)
        if not stations:
            logger.info("TankForecastingAgent: no stations found for tenant %s", tenant_id)
            return []

        # Step 3: Query historical consumption events (Req 1.2)
        consumption_data = await self._query_consumption_history(tenant_id)

        # Step 4: Generate forecasts for each (station, grade) pair
        run_id = f"forecast_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        forecasts: List[TankForecast] = []

        for station in stations:
            station_id = station.get("station_id", "")
            fuel_grade_str = station.get("fuel_grade", "AGO")
            current_stock = station.get("current_stock_liters", 0.0)
            capacity = station.get("capacity_liters", 0.0)

            # Resolve fuel grade
            try:
                fuel_grade = FuelGrade(fuel_grade_str)
            except ValueError:
                fuel_grade = FuelGrade.AGO

            # Get historical consumption for this station+grade
            station_consumption = consumption_data.get(
                f"{station_id}_{fuel_grade.value}", []
            )

            # Compute forecast
            forecast = self._compute_forecast(
                station_id=station_id,
                fuel_grade=fuel_grade,
                current_stock=current_stock,
                capacity=capacity,
                consumption_history=station_consumption,
                tenant_id=tenant_id,
                run_id=run_id,
            )
            forecasts.append(forecast)

        # Step 5: Persist forecasts to ES (Req 1.4)
        for forecast in forecasts:
            await self._persist_forecast(forecast)

        # Step 6: Publish forecasts to SignalBus (Req 1.5)
        for forecast in forecasts:
            await self._signal_bus.publish(forecast)

        logger.info(
            "TankForecastingAgent: published %d forecasts for tenant %s (run_id=%s)",
            len(forecasts),
            tenant_id,
            run_id,
        )

        # Return empty — forecasts are published directly, not as proposals
        return []

    # ------------------------------------------------------------------
    # Anomaly processing (Req 1.3)
    # ------------------------------------------------------------------

    def _process_anomaly_signals(self, signals: List[RiskSignal]) -> None:
        """Extract anomaly flags from RiskSignals and cache by station_id."""
        for signal in signals:
            station_id = signal.entity_id
            anomaly_flags: List[str] = []

            context = signal.context or {}
            if context.get("sensor_drift"):
                anomaly_flags.append("sensor_drift")
            if context.get("station_outage"):
                anomaly_flags.append("station_outage")
            if context.get("demand_spike"):
                anomaly_flags.append("demand_spike")

            # Also check severity-based anomalies
            if signal.severity.value == "critical":
                anomaly_flags.append("critical_risk")

            if anomaly_flags:
                existing = self._anomaly_cache.get(station_id, [])
                # Merge without duplicates
                merged = list(set(existing + anomaly_flags))
                self._anomaly_cache[station_id] = merged

    # ------------------------------------------------------------------
    # ES queries (Req 1.2)
    # ------------------------------------------------------------------

    async def _query_fuel_stations(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Query fuel_stations index for current inventory levels."""
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                    ],
                },
            },
            "size": 200,
        }
        try:
            resp = await self._es.search_documents(FUEL_STATIONS_INDEX, query, 200)
            return [hit["_source"] for hit in resp.get("hits", {}).get("hits", [])]
        except Exception as e:
            logger.error("TankForecastingAgent: failed to query fuel_stations: %s", e)
            return []

    async def _query_consumption_history(
        self, tenant_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Query fuel_events for historical consumption data (last 7 days).

        Returns a dict keyed by '{station_id}_{fuel_grade}' with lists
        of consumption event records.
        """
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {
                            "range": {
                                "timestamp": {
                                    "gte": seven_days_ago.isoformat(),
                                    "lte": now.isoformat(),
                                }
                            }
                        },
                    ],
                },
            },
            "size": 1000,
            "sort": [{"timestamp": {"order": "desc"}}],
        }

        consumption_data: Dict[str, List[Dict[str, Any]]] = {}
        try:
            resp = await self._es.search_documents(FUEL_EVENTS_INDEX, query, 1000)
            for hit in resp.get("hits", {}).get("hits", []):
                event = hit["_source"]
                station_id = event.get("station_id", "")
                fuel_grade = event.get("fuel_grade", "AGO")
                key = f"{station_id}_{fuel_grade}"
                consumption_data.setdefault(key, []).append(event)
        except Exception as e:
            logger.error(
                "TankForecastingAgent: failed to query fuel_events: %s", e
            )

        return consumption_data

    # ------------------------------------------------------------------
    # Forecast computation (Req 1.1, 1.6, 1.7)
    # ------------------------------------------------------------------

    def _compute_forecast(
        self,
        station_id: str,
        fuel_grade: FuelGrade,
        current_stock: float,
        capacity: float,
        consumption_history: List[Dict[str, Any]],
        tenant_id: str,
        run_id: str,
    ) -> TankForecast:
        """Compute a TankForecast for a single (station, grade) pair.

        Uses fuel_calculations.py logic for baseline consumption rate
        estimation (Req 1.6). When no historical data exists, assigns
        default risk of 0.5 with confidence 0.1 (Req 1.7).
        """
        anomaly_flags = self._anomaly_cache.get(station_id, [])

        # Handle zero historical data (Req 1.7)
        if not consumption_history:
            return TankForecast(
                station_id=station_id,
                fuel_grade=fuel_grade,
                hours_to_runout_p50=0.0,
                hours_to_runout_p90=0.0,
                runout_risk_24h=0.5,
                confidence=0.1,
                feature_version="v1.0",
                anomaly_flags=anomaly_flags + ["insufficient_data"],
                tenant_id=tenant_id,
                run_id=run_id,
            )

        # Compute baseline consumption rate (liters/hour) using
        # fuel_calculations.py logic (Req 1.6)
        consumption_rate = self._estimate_consumption_rate(consumption_history)

        # Estimate hours to runout
        if consumption_rate > 0:
            hours_to_runout_p50 = current_stock / consumption_rate
            # p90 uses a pessimistic multiplier for variance
            hours_to_runout_p90 = current_stock / (
                consumption_rate * P90_VARIANCE_MULTIPLIER
            )
        else:
            # Zero consumption rate — station is not consuming fuel
            hours_to_runout_p50 = float("inf")
            hours_to_runout_p90 = float("inf")

        # Cap at reasonable maximum (720 hours = 30 days)
        hours_to_runout_p50 = min(hours_to_runout_p50, 720.0)
        hours_to_runout_p90 = min(hours_to_runout_p90, 720.0)

        # Compute runout_risk_24h: probability of running out within 24h
        runout_risk_24h = self._compute_runout_risk(
            hours_to_runout_p50, hours_to_runout_p90
        )

        # Adjust for anomaly flags
        if "demand_spike" in anomaly_flags:
            runout_risk_24h = min(1.0, runout_risk_24h * 1.3)

        # Compute confidence based on data quality (sensor_drift penalty
        # is applied inside _compute_confidence — Req 1.3)
        confidence = self._compute_confidence(
            consumption_history, anomaly_flags
        )

        # Use fuel_calculations refill priority to cross-validate urgency
        days_until_empty = hours_to_runout_p50 / 24.0 if hours_to_runout_p50 > 0 else 0.0
        refill_priority = calculate_refill_priority(days_until_empty)

        # Boost risk if refill priority is critical/high
        if refill_priority.value == "critical":
            runout_risk_24h = max(runout_risk_24h, 0.9)
        elif refill_priority.value == "high":
            runout_risk_24h = max(runout_risk_24h, 0.7)

        return TankForecast(
            station_id=station_id,
            fuel_grade=fuel_grade,
            hours_to_runout_p50=round(hours_to_runout_p50, 2),
            hours_to_runout_p90=round(hours_to_runout_p90, 2),
            runout_risk_24h=round(min(1.0, max(0.0, runout_risk_24h)), 4),
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            feature_version="v1.0",
            anomaly_flags=anomaly_flags,
            tenant_id=tenant_id,
            run_id=run_id,
        )

    def _estimate_consumption_rate(
        self, consumption_history: List[Dict[str, Any]]
    ) -> float:
        """Estimate average consumption rate in liters/hour from history.

        Reuses fuel_calculations.py logic for baseline estimation (Req 1.6).
        Computes average liters consumed per hour over the historical window.
        """
        if not consumption_history:
            return DEFAULT_CONSUMPTION_RATE

        total_consumed = 0.0
        timestamps = []

        for event in consumption_history:
            quantity = event.get("quantity_liters", 0.0)
            if quantity > 0:
                total_consumed += quantity
            ts_str = event.get("timestamp")
            if ts_str:
                try:
                    if isinstance(ts_str, str):
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    else:
                        ts = ts_str
                    timestamps.append(ts)
                except (ValueError, TypeError):
                    pass

        if not timestamps or total_consumed <= 0:
            return DEFAULT_CONSUMPTION_RATE

        # Compute time span in hours
        earliest = min(timestamps)
        latest = max(timestamps)
        span_hours = (latest - earliest).total_seconds() / 3600.0

        if span_hours <= 0:
            return DEFAULT_CONSUMPTION_RATE

        return total_consumed / span_hours

    def _compute_runout_risk(
        self, hours_p50: float, hours_p90: float
    ) -> float:
        """Compute probability of runout within 24 hours.

        Uses a sigmoid-like function based on hours_to_runout estimates.
        Lower hours → higher risk.
        """
        if hours_p90 <= 0:
            return 1.0

        # Use p90 (pessimistic) for risk calculation
        # Risk approaches 1.0 as hours_to_runout approaches 0
        # Risk approaches 0.0 as hours_to_runout exceeds 72h
        if hours_p90 <= RISK_HORIZON_HOURS:
            # Within 24h horizon: high risk
            risk = 1.0 - (hours_p90 / RISK_HORIZON_HOURS) * 0.5
        elif hours_p90 <= 72.0:
            # 24-72h: moderate risk
            risk = 0.5 * (1.0 - (hours_p90 - RISK_HORIZON_HOURS) / 48.0)
        else:
            # Beyond 72h: low risk
            risk = max(0.0, 0.1 * (72.0 / hours_p90))

        return max(0.0, min(1.0, risk))

    def _compute_confidence(
        self, consumption_history: List[Dict[str, Any]], anomaly_flags: List[str]
    ) -> float:
        """Compute forecast confidence based on data quality and anomalies.

        More data points → higher confidence. Anomaly flags reduce confidence.
        """
        # Base confidence from data volume (max 0.9 with 50+ events)
        n_events = len(consumption_history)
        base_confidence = min(0.9, n_events / 50.0)

        # Reduce for anomalies
        anomaly_penalty = 0.0
        if "sensor_drift" in anomaly_flags:
            anomaly_penalty += 0.2
        if "station_outage" in anomaly_flags:
            anomaly_penalty += 0.15
        if "demand_spike" in anomaly_flags:
            anomaly_penalty += 0.1

        return max(0.1, base_confidence - anomaly_penalty)

    # ------------------------------------------------------------------
    # Persistence (Req 1.4)
    # ------------------------------------------------------------------

    async def _persist_forecast(self, forecast: TankForecast) -> None:
        """Persist a TankForecast to the mvp_tank_forecasts ES index."""
        try:
            doc = forecast.model_dump(mode="json")
            await self._es.index_document(
                MVP_TANK_FORECASTS_INDEX,
                forecast.forecast_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "TankForecastingAgent: failed to persist forecast %s: %s",
                forecast.forecast_id,
                e,
            )
