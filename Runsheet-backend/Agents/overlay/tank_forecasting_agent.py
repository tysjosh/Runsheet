"""
Tank Forecasting Agent — overlay agent for per-station and per-customer-tank
runout risk prediction.

Subscribes to RiskSignals from FuelManagementAgent, queries fuel_stations,
customer_tanks, and fuel_events indices, computes consumption rates using the
existing fuel_calculations logic for retail stations and the pluggable
``Consumption_Model`` strategies (``RetailStationModel``, ``PropaneKFactorModel``,
``HeatingOilHDDRegressionModel``, ``DieselRollingModel``, ``GeneratorRuntimeModel``)
for customer tanks, estimates probabilistic hours-to-runout (p50/p90), computes
``runout_risk_24h``, handles anomaly flags, folds in Scheduled_Delivery entries
within a 72-hour horizon, applies Customer_Type multipliers from tenant
config, persists forecasts to ``mvp_tank_forecasts``, and publishes
``TankForecast`` to the SignalBus.

Extensions for fuel-ops hardening Capability 1 (Requirements 1.1.2, 1.2.3,
1.2.5, 1.3.1–1.3.4, 1.4.2, 1.4.4, 1.5.3, 1.5.6):

* Iterate over both ``fuel_stations`` (legacy retail) and the new
  ``customer_tanks`` index.
* Select the ``Consumption_Model`` per tank by ``fuel_type`` with optional
  tenant-level overrides via a ``consumption_model_config:{tenant_id}`` Redis
  key (structured as a JSON map of ``fuel_type → model_name``).
* Call the injected ``Weather_Provider`` for propane and heating-oil tanks
  over a ``[-14, +7]`` day window keyed by ``zip_code``. Network or provider
  failures degrade gracefully to an empty weather list and annotate the
  forecast with ``weather_fallback: true``.
* Apply Customer_Type multipliers read from
  ``consumption_segmentation_config:{tenant_id}`` (JSON map) with the
  built-in defaults mandated by Req 1.3.1: residential 1.0, commercial 1.2,
  keep_full 1.1, will_call 0.8, auto_fill 1.05.
* Consult an injected ``ScheduledDeliveryQueryHelper`` that returns every
  delivery with ``status in {scheduled, in_transit}`` for the tank within
  the next 72 hours. The agent subtracts the projected consumption only up
  to ``hours_to_runout``; if a delivery arrives before runout, the tank is
  refilled and ``hours_to_runout`` extends out accordingly.
* Record ``model_name``, ``customer_type_multiplier``, ``baseline_source``,
  ``weather_fallback``, and ``scheduled_deliveries`` in each forecast for
  traceability (Req 1.3.4, 1.4.3, 1.5.6, 1.6.1).

Default configuration:
    - decision_cycle: 300 seconds (5 minutes)
    - cooldown: 15 minutes per station
    - forecast horizon: 72 hours for scheduled-delivery awareness

Requirements: 1.1, 1.1.2, 1.2, 1.2.3, 1.2.5, 1.3, 1.3.1, 1.3.2, 1.3.3,
1.3.4, 1.4, 1.4.2, 1.4.4, 1.5, 1.5.3, 1.5.6, 1.6, 1.7
"""

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

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
from fuel.customer_tank_models import CustomerTank, CustomerTankRepository
from fuel.services.consumption_models import (
    ConsumptionModel,
    ConsumptionPrediction,
    PropaneKFactorModel,
    build_consumption_model,
    select_consumption_model_for_tank,
)
from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX
from fuel.services.fuel_planning_ws_manager import FuelPlanningWSManager
from fuel.services.fuel_product_catalog import canonicalize_or_warn
from fuel.services.weather_provider import DailyWeather, WeatherProvider

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_STATIONS_INDEX = "fuel_stations"
FUEL_EVENTS_INDEX = "fuel_events"

# Default consumption rate (liters/hour) when no historical data exists.
# Retained unchanged for the legacy retail-station path (Req 1.7).
DEFAULT_CONSUMPTION_RATE = 50.0

# Variance multiplier for p90 estimate (pessimistic)
P90_VARIANCE_MULTIPLIER = 1.5

# Default hours horizon for risk calculation
RISK_HORIZON_HOURS = 24.0

# Scheduled-delivery look-ahead window (Req 1.4.1).
SCHEDULED_DELIVERY_HORIZON_HOURS = 72

# Weather-window bounds fed to the Consumption_Models (Req 1.2.3).
WEATHER_TRAILING_DAYS = 14
WEATHER_FORWARD_DAYS = 7

#: Fuel-family tags that require weather data for their Consumption_Model
#: (propane K-factor + heating-oil HDD regression). Other families
#: (diesel / generator / gasoline) run without weather input.
_WEATHER_REQUIRING_FUEL_TYPES = frozenset({"propane", "heating_oil"})

#: Default Customer_Type multipliers (Req 1.3.1). Overridden per tenant via
#: the Redis ``consumption_segmentation_config:{tenant_id}`` JSON map.
DEFAULT_CUSTOMER_TYPE_MULTIPLIERS: Dict[str, float] = {
    "residential": 1.0,
    "commercial": 1.2,
    "keep_full": 1.1,
    "will_call": 0.8,
    "auto_fill": 1.05,
}

#: Minimum prior deliveries for a Customer_Tank before the baseline is
#: treated as "history"-backed rather than "default" (Req 1.3.2, 1.3.3).
MIN_HISTORY_EVENTS_FOR_TANK_BASELINE = 3

#: Default reorder point as a percentage of tank capacity. When the
#: predicted tank level drops below this threshold, the
#: ``low_tank_autofill_alert`` notification fires (Req 12.5).
DEFAULT_REORDER_POINT_PERCENT = 25.0


# ---------------------------------------------------------------------------
# Injected helper protocols
# ---------------------------------------------------------------------------


class ScheduledDeliveryQueryHelper(Protocol):
    """Shape the forecaster relies on to retrieve scheduled deliveries.

    The helper is created by Task 3.4 of the fuel-ops hardening spec and
    unifies retail-station and customer-tank scheduled deliveries behind a
    single interface. The agent invokes it once per decision cycle with the
    tenant_id and horizon; the helper returns a dict keyed by
    ``(destination_type, destination_id)`` → list of scheduled-delivery
    entries shaped as
    ``{"delivery_id": str, "scheduled_eta": datetime, "planned_gallons": float}``.

    Implementations MUST return an empty list for a tank with no scheduled
    deliveries — ``None`` is not accepted because the forecaster treats
    missing keys as "no scheduled deliveries" (Req 1.4.4).
    """

    async def list_scheduled_deliveries(  # pragma: no cover - protocol
        self,
        tenant_id: str,
        *,
        horizon_hours: int = SCHEDULED_DELIVERY_HORIZON_HOURS,
    ) -> Dict[tuple, List[Dict[str, Any]]]:
        ...


class TenantConfigLookup(Protocol):
    """Minimal Redis-like interface for tenant config reads."""

    async def get(self, key: str) -> Optional[Any]:  # pragma: no cover - protocol
        ...


class TankForecastingAgent(OverlayAgentBase):
    """Predicts per-station and per-customer-tank runout risk for the next 24-72 hours.

    Consumes station inventory from ``fuel_stations`` AND customer-tank
    inventory from ``customer_tanks`` via :class:`CustomerTankRepository`,
    historical consumption rates from ``fuel_events``, and anomaly flags
    from ``RiskSignal``s published by :class:`FuelManagementAgent`. For
    each active entity it produces a :class:`TankForecast` message.

    For customer tanks the agent:

    1. Selects a Consumption_Model per ``fuel_type`` with a tenant override
       available via the ``consumption_model_config:{tenant_id}`` Redis
       key (Req 1.5.3).
    2. Calls the injected ``weather_provider`` for propane and heating_oil
       tanks using the tank's ``zip_code`` and a ``[-14, +7]`` day window.
       Failures degrade to an empty list and flag ``weather_fallback: true``
       on the forecast (Req 1.2.5).
    3. Applies the Customer_Type multiplier from
       ``consumption_segmentation_config:{tenant_id}`` or the built-in
       defaults (Req 1.3.1, 1.3.4).
    4. Folds scheduled deliveries into the projected level: if a scheduled
       delivery is expected within ``hours_to_runout`` then the tank is
       refilled at that ETA before runout, extending the projection
       (Req 1.4.2, 1.4.3).
    5. Persists ``model_name``, ``customer_type_multiplier``,
       ``baseline_source``, ``weather_fallback``, and
       ``scheduled_deliveries`` on every forecast (Req 1.3.4, 1.4.3,
       1.5.6, 1.6.1).

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        customer_tank_repository: Optional repository to list customer
            tanks; constructed lazily from ``es_service`` if not supplied.
        weather_provider: Optional WeatherProvider for HDD lookups.
        scheduled_delivery_helper: Optional helper that returns scheduled
            deliveries keyed by destination (see
            :class:`ScheduledDeliveryQueryHelper`).
        tenant_config: Optional Redis-like handle used to read
            ``consumption_model_config`` and
            ``consumption_segmentation_config`` keys.
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
        *,
        customer_tank_repository: Optional[CustomerTankRepository] = None,
        weather_provider: Optional[WeatherProvider] = None,
        scheduled_delivery_helper: Optional[ScheduledDeliveryQueryHelper] = None,
        tenant_config: Optional[TenantConfigLookup] = None,
        fuel_planning_ws_manager: Optional[FuelPlanningWSManager] = None,
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
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": "kfactor_calibration_service",
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

        # Customer_Tank repository — resolves lazily so agents that never
        # exercise the customer-tank path do not require a pre-built repo
        # at construction time. This keeps the existing bootstrap callers
        # (which only pass the seven base services) working unchanged.
        self._customer_tank_repo: Optional[CustomerTankRepository] = (
            customer_tank_repository
        )
        self._customer_tank_repo_auto_build: bool = (
            customer_tank_repository is None
        )

        # Optional extension hooks. When unwired (production default until
        # bootstrap registers them) the agent operates in "retail-only"
        # mode and emits the same forecast shape as before.
        self._weather_provider: Optional[WeatherProvider] = weather_provider
        self._scheduled_delivery_helper: Optional[
            ScheduledDeliveryQueryHelper
        ] = scheduled_delivery_helper
        self._tenant_config: Optional[TenantConfigLookup] = tenant_config

        #: Dedicated fuel-planning WS manager used to emit
        #: ``customer_tank_forecast_ready`` events on ``/ws/fuel-planning``
        #: (Req 1.6.4). Distinct from ``self._ws`` (the agent-activity
        #: manager from :class:`AutonomousAgentBase`) so planning events
        #: reach dispatcher UIs that subscribe to the fuel-planning
        #: channel rather than the generic agent-activity stream.
        self._fuel_planning_ws: Optional[FuelPlanningWSManager] = (
            fuel_planning_ws_manager
        )

        #: Optional NotificationService for firing low_tank_autofill_alert
        #: when a customer tank's predicted level drops below the reorder
        #: point (Req 12.5). Wired post-construction by bootstrap.
        self._notification_service: Optional[Any] = None

        #: Track tanks that have already been alerted in this cycle to
        #: avoid duplicate notifications within a single forecast run.
        self._alerted_tanks: set = set()

    # ------------------------------------------------------------------
    # Public wiring hooks (bootstrap injects these after construction)
    # ------------------------------------------------------------------

    def set_customer_tank_repository(
        self, repository: CustomerTankRepository
    ) -> None:
        """Inject the Customer_Tank repository post-construction."""
        self._customer_tank_repo = repository
        self._customer_tank_repo_auto_build = False

    def set_weather_provider(self, provider: Optional[WeatherProvider]) -> None:
        """Inject the WeatherProvider post-construction (``None`` disables)."""
        self._weather_provider = provider

    def set_scheduled_delivery_helper(
        self, helper: Optional[ScheduledDeliveryQueryHelper]
    ) -> None:
        """Inject the Scheduled_Delivery helper post-construction."""
        self._scheduled_delivery_helper = helper

    def set_tenant_config(
        self, lookup: Optional[TenantConfigLookup]
    ) -> None:
        """Inject the tenant-config lookup post-construction."""
        self._tenant_config = lookup

    def set_fuel_planning_ws_manager(
        self, manager: Optional[FuelPlanningWSManager]
    ) -> None:
        """Inject the fuel-planning WS manager post-construction.

        ``None`` disables the per-tank forecast broadcasts; the agent
        otherwise still persists forecasts and publishes SignalBus
        messages, so downstream consumers keep working unchanged.
        """
        self._fuel_planning_ws = manager

    def set_notification_service(self, notification_service) -> None:
        """Inject the NotificationService post-construction.

        When wired, the agent fires ``low_tank_autofill_alert``
        notifications for auto_fill customer tanks whose predicted level
        drops below the configured reorder point (Req 12.5).

        Passing ``None`` disables the notification hook.
        """
        self._notification_service = notification_service

    # ------------------------------------------------------------------
    # Core evaluation (Req 1.1–1.7 + Capability 1 extensions)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Produce TankForecast for each (station, grade) and (customer_tank) pair.

        Steps:
        1. Extract anomaly flags from incoming RiskSignals (Req 1.3).
        2. Query fuel_stations for current inventory (Req 1.2).
        3. Query customer_tanks via the repository (Req 1.1.2).
        4. Query fuel_events for historical consumption (Req 1.2).
        5. Look up scheduled deliveries within the 72-hour horizon (Req 1.4.1).
        6. Load per-tenant segmentation + model config from Redis (Req 1.3.1, 1.5.3).
        7. Produce legacy retail-station forecasts (existing code path).
        8. Produce customer-tank forecasts using the Consumption_Model
           strategies + Weather_Provider + Customer_Type multipliers +
           scheduled-delivery awareness.
        9. Persist forecasts to mvp_tank_forecasts (Req 1.4).
        10. Publish TankForecast messages to the SignalBus (Req 1.5).

        Returns:
            Empty list — forecasts are published directly to SignalBus
            rather than as InterventionProposals.
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id

        # Reset per-cycle deduplication for low_tank_autofill_alert (Req 12.5).
        self._alerted_tanks.clear()

        # Step 0: Process kfactor_changed signals (Req 9.5) — log that a
        # re-forecast will be triggered for affected tanks. The K-factor
        # has already been updated in ES by KFactorCalibrationService, so
        # the full forecast pass below will pick up the new value.
        self._process_kfactor_changed_signals(signals)

        # Step 1: Extract anomaly flags from RiskSignals (Req 1.3)
        self._process_anomaly_signals(signals)

        # Step 2: Query fuel stations (Req 1.2)
        stations = await self._query_fuel_stations(tenant_id)
        
        # Build station data cache for consumption rate lookups
        self._station_data_cache = {
            station.get("station_id"): station
            for station in stations
        }

        # Step 3: Query customer tanks (Req 1.1.2). Absence is fine: agents
        # without customer_tanks simply fall back to station-only output.
        customer_tanks = await self._query_customer_tanks(tenant_id)

        if not stations and not customer_tanks:
            logger.info(
                "TankForecastingAgent: no stations or customer_tanks for tenant %s",
                tenant_id,
            )
            return []

        # Step 4: Query historical consumption events (Req 1.2)
        consumption_data = await self._query_consumption_history(tenant_id)

        # Step 5: Scheduled deliveries for the whole tenant (Req 1.4.1).
        scheduled = await self._query_scheduled_deliveries(
            tenant_id, horizon_hours=SCHEDULED_DELIVERY_HORIZON_HOURS
        )

        # Step 6: Load per-tenant segmentation + model config (Req 1.3.1, 1.5.3).
        customer_type_multipliers = await self._load_customer_type_multipliers(
            tenant_id
        )
        model_overrides = await self._load_consumption_model_overrides(
            tenant_id
        )

        # Step 7–8: Forecast pass
        run_id = f"forecast_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        forecasts: List[TankForecast] = []

        for station in stations:
            station_id = station.get("station_id", "")
            fuel_grade_str = station.get("fuel_grade", "AGO")
            current_stock = station.get("current_stock_liters", 0.0)
            capacity = station.get("capacity_liters", 0.0)

            try:
                fuel_grade = FuelGrade(fuel_grade_str)
            except ValueError:
                fuel_grade = FuelGrade.AGO

            station_consumption = consumption_data.get(
                f"{station_id}_{fuel_grade.value}", []
            )

            forecast = self._compute_forecast(
                station_id=station_id,
                fuel_grade=fuel_grade,
                current_stock=current_stock,
                capacity=capacity,
                consumption_history=station_consumption,
                tenant_id=tenant_id,
                run_id=run_id,
            )
            # Attach any scheduled deliveries that apply to this retail
            # station so the forecast surface matches the customer-tank
            # path even when the legacy rolling model produced the core
            # runout estimate.
            station_scheduled = self._scheduled_for_destination(
                scheduled, "retail_station", station_id
            )
            if station_scheduled:
                forecast.scheduled_deliveries = [
                    self._serialize_scheduled_delivery(entry)
                    for entry in station_scheduled
                ]
            forecasts.append(forecast)

        for tank in customer_tanks:
            forecast = await self._compute_customer_tank_forecast(
                tank=tank,
                tenant_id=tenant_id,
                run_id=run_id,
                history=consumption_data,
                scheduled=scheduled,
                customer_type_multipliers=customer_type_multipliers,
                model_overrides=model_overrides,
            )
            forecasts.append(forecast)

        # Step 9: Persist forecasts to ES (Req 1.4)
        for forecast in forecasts:
            await self._persist_forecast(forecast)

        # Step 10: Publish forecasts to SignalBus (Req 1.5)
        for forecast in forecasts:
            await self._signal_bus.publish(forecast)

        # Step 11 (Req 1.6.4): Broadcast customer_tank_forecast_ready for
        # every per-tank forecast so dispatcher UIs subscribed to
        # /ws/fuel-planning see the update in real time. Only customer-tank
        # forecasts fire the event; retail-station forecasts do not carry
        # the customer_tank_id payload mandated by the requirement.
        for forecast in forecasts:
            if getattr(forecast, "customer_tank_id", None):
                await self._broadcast_customer_tank_forecast_ready(forecast)

        # Step 12 (Req 12.5): Fire low_tank_autofill_alert notifications
        # for auto_fill customer tanks whose current level is below the
        # configured reorder point. Uses the forecast data to populate
        # the notification template placeholders.
        for forecast, tank in zip(forecasts[len(stations):], customer_tanks):
            await self._check_low_tank_autofill_alert(tank, forecast)

        logger.info(
            "TankForecastingAgent: published %d forecasts for tenant %s "
            "(stations=%d, customer_tanks=%d, run_id=%s)",
            len(forecasts),
            tenant_id,
            len(stations),
            len(customer_tanks),
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
    # K-factor change processing (Req 9.5)
    # ------------------------------------------------------------------

    def _process_kfactor_changed_signals(self, signals: List[RiskSignal]) -> None:
        """Process kfactor_changed signals from KFactorCalibrationService.

        When a K-factor is adjusted by an operator, the
        KFactorCalibrationService publishes a RiskSignal with
        source_agent='kfactor_calibration_service' and
        context.event='kfactor_changed'. This method logs the affected
        tanks so the subsequent full forecast pass (which reads the
        updated K-factor from ES) produces an accurate re-forecast.

        Validates: Requirement 9.5
        """
        for signal in signals:
            context = signal.context or {}
            if (
                signal.source_agent == "kfactor_calibration_service"
                and context.get("event") == "kfactor_changed"
            ):
                tank_id = context.get("tank_id", signal.entity_id)
                old_kfactor = context.get("old_kfactor")
                new_kfactor = context.get("new_kfactor")
                operator_id = context.get("operator_id")
                logger.info(
                    "TankForecastingAgent: received kfactor_changed signal "
                    "for tank=%s (old=%.4f new=%.4f operator=%s tenant=%s) "
                    "— will re-forecast with updated K-factor",
                    tank_id,
                    old_kfactor or 0.0,
                    new_kfactor or 0.0,
                    operator_id,
                    signal.tenant_id,
                )

    # ------------------------------------------------------------------
    # ES queries (Req 1.2 + 1.1.2)
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

    async def _query_customer_tanks(self, tenant_id: str) -> List[CustomerTank]:
        """Query the customer_tanks index via :class:`CustomerTankRepository`.

        Lazily builds the repository if one was not injected at
        construction time so existing bootstrap callers keep working.
        Degrades to an empty list on query failure so a broken index
        never takes down the retail-station path.
        """
        repo = self._get_customer_tank_repository()
        if repo is None:
            return []
        try:
            tanks = await repo.list_for_tenant(tenant_id, status="active")
        except Exception as exc:
            logger.error(
                "TankForecastingAgent: failed to list customer_tanks for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []
        return tanks

    def _get_customer_tank_repository(
        self,
    ) -> Optional[CustomerTankRepository]:
        """Return the injected repository or lazily build one.

        Lazy construction uses ``self._es`` so the agent inherits the same
        ES handle as the rest of its ES queries. Failures are logged and
        swallowed so a missing repo degrades to "no customer_tanks".
        """
        if self._customer_tank_repo is not None:
            return self._customer_tank_repo
        if not self._customer_tank_repo_auto_build or self._es is None:
            return None
        try:
            self._customer_tank_repo = CustomerTankRepository(self._es)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TankForecastingAgent: failed to build CustomerTankRepository: %s",
                exc,
            )
            self._customer_tank_repo_auto_build = False
            return None
        return self._customer_tank_repo

    async def _query_consumption_history(
        self, tenant_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Query fuel_events for historical consumption data (last 7 days).

        Returns a dict keyed by '{station_id}_{fuel_grade}' *and*
        ``'customer_tank_id:{id}'`` with lists of consumption event
        records. Events with ``customer_tank_id`` populated (new US-market
        deliveries) are indexed under both a per-tank key (for the
        per-tank Consumption_Model input) and the legacy station key for
        retail flows.
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
                if station_id:
                    key = f"{station_id}_{fuel_grade}"
                    consumption_data.setdefault(key, []).append(event)
                tank_id = event.get("customer_tank_id")
                if tank_id:
                    tank_key = f"customer_tank_id:{tank_id}"
                    consumption_data.setdefault(tank_key, []).append(event)
        except Exception as e:
            logger.error(
                "TankForecastingAgent: failed to query fuel_events: %s", e
            )

        return consumption_data

    # ------------------------------------------------------------------
    # Scheduled_Delivery integration (Req 1.4.1, 1.4.2, 1.4.4)
    # ------------------------------------------------------------------

    async def _query_scheduled_deliveries(
        self,
        tenant_id: str,
        *,
        horizon_hours: int = SCHEDULED_DELIVERY_HORIZON_HOURS,
    ) -> Dict[tuple, List[Dict[str, Any]]]:
        """Return scheduled deliveries grouped by (destination_type, destination_id).

        Delegates to the injected
        :class:`ScheduledDeliveryQueryHelper` when one is wired; otherwise
        returns an empty mapping so the agent cleanly degrades to a
        "no scheduled deliveries known" state (Req 1.4.4: empty list is
        the expected result in that case).
        """
        if self._scheduled_delivery_helper is None:
            return {}
        try:
            return await self._scheduled_delivery_helper.list_scheduled_deliveries(
                tenant_id,
                horizon_hours=horizon_hours,
            )
        except Exception as exc:
            logger.warning(
                "TankForecastingAgent: scheduled-delivery lookup failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return {}

    @staticmethod
    def _scheduled_for_destination(
        scheduled: Mapping[tuple, List[Dict[str, Any]]],
        destination_type: str,
        destination_id: str,
    ) -> List[Dict[str, Any]]:
        """Safely pull the list of scheduled deliveries for a destination."""
        if not scheduled or not destination_id:
            return []
        return list(scheduled.get((destination_type, destination_id), []))

    @staticmethod
    def _serialize_scheduled_delivery(entry: Mapping[str, Any]) -> Dict[str, Any]:
        """Return a ``{delivery_id, scheduled_eta, planned_gallons}`` dict.

        ES mappings store ``scheduled_eta`` as a date; we emit an ISO-8601
        string with a trailing ``Z`` for timezone-aware values so the
        document round-trips cleanly through JSON.
        """
        eta = entry.get("scheduled_eta")
        if isinstance(eta, datetime):
            eta_iso = eta.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        elif isinstance(eta, date) and not isinstance(eta, datetime):
            eta_iso = eta.isoformat()
        elif eta is None:
            eta_iso = None
        else:
            eta_iso = str(eta)
        gallons = entry.get("planned_gallons")
        try:
            gallons_val = float(gallons) if gallons is not None else 0.0
        except (TypeError, ValueError):
            gallons_val = 0.0
        return {
            "delivery_id": str(entry.get("delivery_id", "")) or None,
            "scheduled_eta": eta_iso,
            "planned_gallons": max(0.0, gallons_val),
        }

    # ------------------------------------------------------------------
    # Customer_Type multiplier + Consumption_Model selection (Req 1.3, 1.5.3)
    # ------------------------------------------------------------------

    async def _load_customer_type_multipliers(
        self, tenant_id: str
    ) -> Dict[str, float]:
        """Return the Customer_Type multiplier map for the tenant.

        Merges the tenant-specific overrides from the Redis key
        ``consumption_segmentation_config:{tenant_id}`` on top of the
        built-in defaults (Req 1.3.1). Invalid values (non-numeric or
        negative) are dropped with a warning so one bad entry does not
        taint the rest.
        """
        merged = dict(DEFAULT_CUSTOMER_TYPE_MULTIPLIERS)
        payload = await self._load_tenant_config_json(
            f"consumption_segmentation_config:{tenant_id}"
        )
        if isinstance(payload, Mapping):
            for raw_key, raw_value in payload.items():
                try:
                    key = str(raw_key).strip().lower()
                    value = float(raw_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "TankForecastingAgent: ignoring invalid customer_type "
                        "multiplier entry %r=%r for tenant=%s",
                        raw_key,
                        raw_value,
                        tenant_id,
                    )
                    continue
                if not math.isfinite(value) or value < 0:
                    logger.warning(
                        "TankForecastingAgent: ignoring non-finite/negative "
                        "multiplier %s=%s for tenant=%s",
                        key,
                        value,
                        tenant_id,
                    )
                    continue
                merged[key] = value
        return merged

    async def _load_consumption_model_overrides(
        self, tenant_id: str
    ) -> Dict[str, str]:
        """Return the ``{fuel_type → model_name}`` override map, if any.

        Backed by Redis key ``consumption_model_config:{tenant_id}``. The
        payload is a JSON object mapping fuel-type strings to model
        short-names recognized by :func:`build_consumption_model`. Unknown
        entries are dropped with a warning — an unknown model name would
        otherwise raise at forecast time inside the decision cycle.
        """
        out: Dict[str, str] = {}
        payload = await self._load_tenant_config_json(
            f"consumption_model_config:{tenant_id}"
        )
        if not isinstance(payload, Mapping):
            return out
        for raw_key, raw_value in payload.items():
            key = str(raw_key).strip().lower()
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            out[key] = raw_value.strip()
        return out

    async def _load_tenant_config_json(self, key: str) -> Any:
        """Read ``key`` from the tenant-config backend and parse JSON.

        Returns ``None`` when the backend is unwired, the key is missing,
        or the payload fails to parse. Never raises — forecast cycles
        must not depend on Redis availability.
        """
        if self._tenant_config is None:
            return None
        try:
            raw = await self._tenant_config.get(key)
        except Exception as exc:
            logger.warning(
                "TankForecastingAgent: tenant config get(%s) failed: %s",
                key,
                exc,
            )
            return None
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "TankForecastingAgent: tenant config %s is not valid JSON",
                    key,
                )
                return None
        if isinstance(raw, Mapping):
            return raw
        return None

    @staticmethod
    def _resolve_customer_type_multiplier(
        customer_type: str,
        mapping: Mapping[str, float],
    ) -> float:
        """Return the multiplier for ``customer_type`` or 1.0 when absent."""
        normalized = (customer_type or "").strip().lower()
        if normalized in mapping:
            return float(mapping[normalized])
        # Unknown customer_type: neutral multiplier + log.
        return 1.0

    # ------------------------------------------------------------------
    # Weather_Provider integration (Req 1.2.3, 1.2.5)
    # ------------------------------------------------------------------

    async def _get_weather_for_tank(
        self,
        tank: CustomerTank,
    ) -> tuple[List[DailyWeather], bool]:
        """Return ``(weather_rows, weather_fallback)`` for a Customer_Tank.

        Short-circuits for fuel types that do not require weather: diesel,
        generator fuel, farm fuel and gasoline all fall under the rolling
        / runtime models and therefore do not consume HDD (Req 1.2.3 —
        "…when the tank's fuel_type is propane or heating_oil"). For
        unsupported types we return ``([], False)`` so the forecast is
        not flagged as a weather fallback; the forecaster treats absent
        weather as "not applicable" rather than "missing".
        """
        if tank.fuel_type not in _WEATHER_REQUIRING_FUEL_TYPES:
            return [], False
        if self._weather_provider is None:
            # No provider configured for this tenant — Req 1.2.5 mandates
            # the ``weather_fallback`` annotation.
            return [], True
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=WEATHER_TRAILING_DAYS)
        end = today + timedelta(days=WEATHER_FORWARD_DAYS)
        try:
            rows = await self._weather_provider.fetch(
                tank.zip_code,
                start,
                end,
                tenant_id=tank.tenant_id,
            )
        except Exception as exc:
            # WeatherProvider.fetch already swallows network errors and
            # returns []; a raised exception here is a programmer error
            # in a custom adapter. Log and degrade.
            logger.warning(
                "TankForecastingAgent: weather fetch for zip=%s tenant=%s "
                "raised %s",
                tank.zip_code,
                tank.tenant_id,
                exc,
            )
            return [], True
        if not rows:
            return [], True
        return list(rows), False

    # ------------------------------------------------------------------
    # Customer-tank forecast computation (Req 1.1.2, 1.3, 1.4, 1.5, 1.6)
    # ------------------------------------------------------------------

    async def _compute_customer_tank_forecast(
        self,
        *,
        tank: CustomerTank,
        tenant_id: str,
        run_id: str,
        history: Mapping[str, List[Dict[str, Any]]],
        scheduled: Mapping[tuple, List[Dict[str, Any]]],
        customer_type_multipliers: Mapping[str, float],
        model_overrides: Mapping[str, str],
    ) -> TankForecast:
        """Compute a :class:`TankForecast` for a single Customer_Tank.

        Pipeline: consumption model selection → weather lookup → customer
        type multiplier → scheduled-delivery folding → p50/p90 derivation →
        runout-risk + confidence + metadata stamping.
        """
        tank_history = list(
            history.get(f"customer_tank_id:{tank.customer_tank_id}", [])
        )
        weather_rows, weather_fallback = await self._get_weather_for_tank(tank)

        model = self._select_consumption_model(tank, model_overrides)
        prediction = await self._run_consumption_model(
            model=model,
            tank=tank,
            history=tank_history,
            weather=weather_rows,
        )

        # Customer_Type multiplier (Req 1.3.1–1.3.4).
        multiplier = self._resolve_customer_type_multiplier(
            tank.customer_type, customer_type_multipliers
        )
        adjusted_gpd = max(0.0, prediction.gallons_per_day * multiplier)

        baseline_source = (
            "history"
            if len(tank_history) >= MIN_HISTORY_EVENTS_FOR_TANK_BASELINE
            else "default"
        )

        # Scheduled deliveries for this tank (Req 1.4).
        tank_scheduled_raw = self._scheduled_for_destination(
            scheduled, "customer_tank", tank.customer_tank_id
        )
        scheduled_serialized = [
            self._serialize_scheduled_delivery(entry) for entry in tank_scheduled_raw
        ]

        # Core runout math (Req 1.4.2).
        hours_p50, hours_p90 = self._compute_hours_to_runout_with_schedule(
            current_level=float(tank.current_level_gallons),
            capacity=float(tank.capacity_gallons),
            gallons_per_day=adjusted_gpd,
            scheduled=tank_scheduled_raw,
            now=datetime.now(timezone.utc),
        )

        runout_risk_24h = self._compute_runout_risk(hours_p50, hours_p90)

        # Anomaly flags — merge the model's anomaly flags with any
        # tank-specific anomalies we want to carry through. Add a
        # "weather_fallback" flag so downstream consumers (Prioritization
        # Agent) can surface the provenance.
        anomaly_flags = list(prediction.anomaly_flags)
        if weather_fallback and "weather_fallback" not in anomaly_flags:
            anomaly_flags.append("weather_fallback")

        # Confidence: start from the model's confidence, dampen slightly
        # when no history was available to tune the baseline.
        confidence = float(prediction.confidence)
        if baseline_source == "default":
            confidence = min(confidence, 0.5)

        return TankForecast(
            station_id=tank.customer_tank_id,  # keep station_id non-empty for legacy consumers
            fuel_grade=self._fuel_grade_for_tank(tank),
            hours_to_runout_p50=round(hours_p50, 2),
            hours_to_runout_p90=round(hours_p90, 2),
            runout_risk_24h=round(min(1.0, max(0.0, runout_risk_24h)), 4),
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            feature_version="v1.0",
            anomaly_flags=anomaly_flags,
            tenant_id=tenant_id,
            run_id=run_id,
            customer_tank_id=tank.customer_tank_id,
            customer_id=tank.customer_id,
            customer_type=tank.customer_type,
            fuel_type=tank.fuel_type,
            model_name=prediction.model_name,
            customer_type_multiplier=multiplier,
            baseline_source=baseline_source,
            weather_fallback=weather_fallback,
            scheduled_deliveries=scheduled_serialized,
        )

    def _select_consumption_model(
        self,
        tank: CustomerTank,
        model_overrides: Mapping[str, str],
    ) -> ConsumptionModel:
        """Return the ``ConsumptionModel`` to use for ``tank``.

        Precedence:
            1. Tenant override keyed by ``fuel_type`` (Req 1.5.3).
            2. Built-in default via
               :func:`select_consumption_model_for_tank` (Req 1.5.1).
        """
        override = model_overrides.get((tank.fuel_type or "").strip().lower())
        if override:
            try:
                return build_consumption_model(override)
            except ValueError as exc:
                logger.warning(
                    "TankForecastingAgent: invalid consumption model "
                    "override %r for fuel_type=%s tenant=%s: %s",
                    override,
                    tank.fuel_type,
                    tank.tenant_id,
                    exc,
                )
        return select_consumption_model_for_tank(tank)

    async def _run_consumption_model(
        self,
        *,
        model: ConsumptionModel,
        tank: CustomerTank,
        history: Sequence[Dict[str, Any]],
        weather: Sequence[DailyWeather],
    ) -> ConsumptionPrediction:
        """Invoke ``model.predict`` and never raise.

        A model that bubbles up an exception (e.g. bad input from a new
        adapter) must not kill the entire forecast cycle. We degrade to a
        low-confidence "0 gpd" prediction with the offending flag so the
        forecaster can still emit a valid TankForecast for the tank.
        """
        try:
            # Thread the operator-approved calibrated K-factor into the
            # propane model so an approved calibration actually changes the
            # forecast (Req 9.5). Scoped to the propane K-factor model — the
            # calibration formula (gallons / ΣHDD) is the propane K; other
            # models do not consume a single K.
            extra: Dict[str, Any] = {}
            if (
                isinstance(model, PropaneKFactorModel)
                and tank.k_factor is not None
                and tank.k_factor > 0
            ):
                extra["calibrated_k_factor"] = tank.k_factor
            return await model.predict(
                tank.customer_tank_id,
                history,
                weather,
                customer_type=tank.customer_type,
                **extra,
            )
        except Exception as exc:
            logger.warning(
                "TankForecastingAgent: Consumption_Model %s raised for "
                "tank=%s tenant=%s: %s",
                getattr(model, "model_name", type(model).__name__),
                tank.customer_tank_id,
                tank.tenant_id,
                exc,
            )
            return ConsumptionPrediction(
                gallons_per_day=0.0,
                confidence=0.1,
                model_name=getattr(model, "model_name", "unknown_model"),
                features_used={"tank_id": tank.customer_tank_id, "error": str(exc)},
                anomaly_flags=["consumption_model_error"],
                as_of=datetime.now(timezone.utc),
            )

    @staticmethod
    def _fuel_grade_for_tank(tank: CustomerTank) -> FuelGrade:
        """Return a best-effort :class:`FuelGrade` for a Customer_Tank.

        The legacy NG-flavoured FuelGrade enum only carries four values,
        so we map the US catalog product codes to their closest NG alias
        for the compatibility field on the persisted TankForecast.
        Future-proofing the full US catalog is tracked by fuel-ops
        hardening Capability 6; until then we stamp the most reasonable
        grade so downstream queries keyed on this field keep working.
        """
        mapping = {
            "DIESEL_2": FuelGrade.AGO,
            "OFF_ROAD_DIESEL": FuelGrade.AGO,
            "GASOLINE_REG": FuelGrade.PMS,
            "GASOLINE_PREM": FuelGrade.PMS,
            "ETHANOL_E85": FuelGrade.PMS,
            "KEROSENE": FuelGrade.ATK,
            "HEATING_OIL": FuelGrade.ATK,
            "PROPANE": FuelGrade.LPG,
            "DEF": FuelGrade.AGO,
        }
        return mapping.get(tank.fuel_product_code, FuelGrade.AGO)

    # ------------------------------------------------------------------
    # Hours-to-runout math with scheduled-delivery folding (Req 1.4.2)
    # ------------------------------------------------------------------

    @classmethod
    def _compute_hours_to_runout_with_schedule(
        cls,
        *,
        current_level: float,
        capacity: float,
        gallons_per_day: float,
        scheduled: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> tuple[float, float]:
        """Return ``(hours_p50, hours_p90)`` accounting for scheduled refills.

        Integrates the projected consumption over time. At each scheduled
        delivery ETA the projected level is increased by that delivery's
        ``planned_gallons`` (capped at ``capacity``). Runout occurs at the
        first moment the running level reaches zero. ``p90`` uses the
        pessimistic multiplier (higher consumption rate).

        When ``gallons_per_day`` is zero the result is unbounded; we cap
        at 720 hours (30 days) to keep the output finite and compatible
        with the existing p50/p90 clamps.
        """
        if capacity <= 0 or not math.isfinite(capacity):
            capacity = float("inf")

        p50 = cls._simulate_runout(
            current_level=current_level,
            capacity=capacity,
            gallons_per_day=max(0.0, gallons_per_day),
            scheduled=scheduled,
            now=now,
        )
        p90 = cls._simulate_runout(
            current_level=current_level,
            capacity=capacity,
            gallons_per_day=max(0.0, gallons_per_day) * P90_VARIANCE_MULTIPLIER,
            scheduled=scheduled,
            now=now,
        )
        # Cap to match the legacy retail behavior.
        p50 = min(p50, 720.0)
        p90 = min(p90, 720.0)
        return p50, p90

    @staticmethod
    def _simulate_runout(
        *,
        current_level: float,
        capacity: float,
        gallons_per_day: float,
        scheduled: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> float:
        """Walk forward through scheduled deliveries to find the runout hour.

        If ``gallons_per_day`` is 0 the tank never runs out and we return
        ``inf`` (the caller caps the output). Scheduled deliveries are
        sorted by ETA; the running level is decreased at ``gpd/24`` per
        hour and increased by ``planned_gallons`` at each ETA (clamped to
        ``capacity``). The returned value is the elapsed hours since
        ``now`` when the level first reaches zero.
        """
        level = max(0.0, float(current_level))
        if gallons_per_day <= 0:
            return float("inf")
        gph = gallons_per_day / 24.0

        # Sort scheduled deliveries by ETA; drop any entries whose ETA
        # is in the past or unparseable.
        events: List[tuple[float, float]] = []
        for entry in scheduled:
            eta_hours = _parse_eta_hours_from_now(entry.get("scheduled_eta"), now)
            if eta_hours is None or eta_hours <= 0:
                continue
            try:
                gallons = float(entry.get("planned_gallons", 0.0) or 0.0)
            except (TypeError, ValueError):
                gallons = 0.0
            events.append((eta_hours, max(0.0, gallons)))
        events.sort(key=lambda pair: pair[0])

        elapsed = 0.0
        for eta_hours, gallons in events:
            hours_until_event = eta_hours - elapsed
            hours_to_empty = level / gph
            if hours_to_empty <= hours_until_event:
                # Tank runs out before the next scheduled delivery.
                return elapsed + hours_to_empty
            # Tank survives to the delivery; consume until then, then refill.
            level -= gph * hours_until_event
            elapsed = eta_hours
            level = min(capacity, level + gallons)
            if level <= 0:
                return elapsed

        # After all scheduled deliveries: consume until empty.
        if level <= 0:
            return elapsed
        return elapsed + (level / gph)

    # ------------------------------------------------------------------
    # Forecast computation (Req 1.1, 1.6, 1.7) — legacy retail station path
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
        estimation (Req 1.6).

        Zero-history behavior (see the branch below) DIVERGES from the original
        Req 1.7 wording, which specified a flat ``runout_risk_24h=0.5`` /
        ``confidence=0.1``. The current implementation instead projects a real
        forecast from ``DEFAULT_CONSUMPTION_RATE`` (or the station's
        ``daily_consumption_rate`` when cached), reports ``confidence=0.5``, and
        tags the forecast ``insufficient_data`` + ``using_default_rate``.

        ⚠️  Consequence: a tank with no consumption history whose projected p50
        exceeds the 24h horizon reports ``runout_risk_24h=0.0`` — i.e. "no
        risk" — which can de-prioritise it during dispatch even though the
        estimate is not evidence-based. Consumers should treat the
        ``insufficient_data`` / ``using_default_rate`` flags as the signal that
        the risk figure is not trustworthy. Reconciling this with Req 1.7 is an
        open product decision.
        """
        anomaly_flags = self._anomaly_cache.get(station_id, [])

        # Handle zero historical data (Req 1.7)
        # PERMANENT FIX: Use station's daily_consumption_rate and current_stock
        # from fuel_stations index instead of returning zeros
        if not consumption_history:
            # Try to get consumption rate from station data
            consumption_rate = DEFAULT_CONSUMPTION_RATE / 24.0  # Convert daily to hourly
            
            # If we have station data with daily_consumption_rate, use it
            if hasattr(self, '_station_data_cache') and station_id in self._station_data_cache:
                station = self._station_data_cache[station_id]
                if station.get('daily_consumption_rate'):
                    consumption_rate = station['daily_consumption_rate'] / 24.0  # liters/hour
                    
            # Calculate hours to runout using DEFAULT_CONSUMPTION_RATE if no history
            # This ensures we generate meaningful forecasts even without fuel_events
            hours_p50 = current_stock / consumption_rate if consumption_rate > 0 else 720.0
            hours_p90 = hours_p50 * P90_VARIANCE_MULTIPLIER
            
            # Cap at reasonable maximum
            hours_p50 = min(hours_p50, 720.0)
            hours_p90 = min(hours_p90, 720.0)
            
            # Compute risk based on hours to runout
            if hours_p90 <= RISK_HORIZON_HOURS:
                runout_risk_24h = 1.0
            elif hours_p50 >= RISK_HORIZON_HOURS:
                runout_risk_24h = 0.0
            else:
                runout_risk_24h = (RISK_HORIZON_HOURS - hours_p50) / (hours_p90 - hours_p50)
            
            return TankForecast(
                station_id=station_id,
                fuel_grade=fuel_grade,
                hours_to_runout_p50=round(hours_p50, 2),
                hours_to_runout_p90=round(hours_p90, 2),
                runout_risk_24h=round(min(1.0, max(0.0, runout_risk_24h)), 4),
                confidence=0.5,  # Medium confidence when using defaults
                feature_version="v1.0",
                anomaly_flags=anomaly_flags + ["insufficient_data", "using_default_rate"],
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
        """Persist a TankForecast to the mvp_tank_forecasts ES index.

        Canonicalizes ``fuel_grade`` before write so forecasts generated
        from NG-aliased stations (AGO/PMS/ATK/LPG) land in ES alongside
        the US canonical codes (DIESEL_2/GASOLINE_REG/KEROSENE/PROPANE)
        per Requirement 6.1.4. Uses the best-effort ``canonicalize_or_warn``
        helper because forecasts originate from historical station data
        that may predate the catalog migration — an unknown value is
        logged but not allowed to block the forecast cycle.
        """
        try:
            doc = forecast.model_dump(mode="json")
            raw_grade = doc.get("fuel_grade")
            if raw_grade is not None:
                doc["fuel_grade"] = canonicalize_or_warn(
                    raw_grade,
                    context="mvp_tank_forecasts.fuel_grade",
                    logger_=logger,
                )
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

    # ------------------------------------------------------------------
    # Low tank autofill alert (Req 12.5)
    # ------------------------------------------------------------------

    async def _check_low_tank_autofill_alert(
        self, tank: CustomerTank, forecast: TankForecast
    ) -> None:
        """Fire ``low_tank_autofill_alert`` when predicted level < reorder_point.

        Only fires for auto_fill customer tanks (will_call customers manage
        their own orders). The reorder point is configurable per tenant via
        the Redis key ``reorder_point_config:{tenant_id}`` (JSON with a
        ``reorder_point_percent`` field); defaults to
        :data:`DEFAULT_REORDER_POINT_PERCENT` (25%).

        Deduplication: the alert fires at most once per tank per forecast
        cycle (tracked via ``_alerted_tanks``). The set is cleared at the
        start of each ``evaluate()`` call.

        Validates: Requirement 12.5
        """
        if self._notification_service is None:
            return

        # Only fire for auto_fill customers — will_call customers order
        # explicitly and don't need proactive alerts.
        if tank.customer_type not in ("auto_fill", "keep_full"):
            return

        # Deduplicate within a single forecast run.
        if tank.customer_tank_id in self._alerted_tanks:
            return

        # Compute current level as a percentage of capacity.
        if tank.capacity_gallons <= 0:
            return
        current_level_percent = (
            tank.current_level_gallons / tank.capacity_gallons
        ) * 100.0

        # Load the reorder point threshold (default 25%).
        reorder_point = await self._get_reorder_point_percent(tank.tenant_id)

        if current_level_percent >= reorder_point:
            return

        # Tank is below reorder point — fire the notification.
        self._alerted_tanks.add(tank.customer_tank_id)

        # Compute estimated days to empty from the forecast.
        hours_to_empty = forecast.hours_to_runout_p50
        estimated_days_to_empty = round(hours_to_empty / 24.0, 1) if hours_to_empty > 0 else 0.0

        # Determine scheduled delivery date from the forecast metadata.
        scheduled_delivery_date = "TBD"
        scheduled_deliveries = getattr(forecast, "scheduled_deliveries", [])
        if scheduled_deliveries:
            first_delivery = scheduled_deliveries[0]
            eta = first_delivery.get("scheduled_eta")
            if eta:
                scheduled_delivery_date = str(eta)[:10]  # ISO date portion

        # Build the event_data payload matching the template placeholders.
        event_data = {
            "customer_id": tank.customer_id,
            "customer_name": tank.customer_id,  # Best available; real name resolved by preference_resolver
            "tank_location": f"{tank.zip_code}",
            "current_level_percent": round(current_level_percent, 1),
            "estimated_days_to_empty": estimated_days_to_empty,
            "scheduled_delivery_date": scheduled_delivery_date,
            "customer_tank_id": tank.customer_tank_id,
        }

        try:
            await self._notification_service.notify_event(
                event_type="low_tank_autofill_alert",
                event_data=event_data,
                tenant_id=tank.tenant_id,
            )
            logger.info(
                "TankForecastingAgent: fired low_tank_autofill_alert for "
                "tank=%s customer=%s level=%.1f%% (reorder_point=%.1f%%) "
                "tenant=%s",
                tank.customer_tank_id,
                tank.customer_id,
                current_level_percent,
                reorder_point,
                tank.tenant_id,
            )
        except Exception as exc:
            # Never let a notification failure break the forecast cycle.
            logger.warning(
                "TankForecastingAgent: low_tank_autofill_alert failed for "
                "tank=%s: %s",
                tank.customer_tank_id,
                exc,
            )

    async def _get_reorder_point_percent(self, tenant_id: str) -> float:
        """Return the reorder point percentage for the tenant.

        Reads from the Redis key ``reorder_point_config:{tenant_id}``
        (JSON with ``reorder_point_percent`` field). Falls back to
        :data:`DEFAULT_REORDER_POINT_PERCENT` when the key is missing,
        unparseable, or the tenant config backend is unwired.
        """
        payload = await self._load_tenant_config_json(
            f"reorder_point_config:{tenant_id}"
        )
        if isinstance(payload, Mapping):
            try:
                value = float(payload.get("reorder_point_percent", DEFAULT_REORDER_POINT_PERCENT))
                if 0 < value <= 100:
                    return value
            except (TypeError, ValueError):
                pass
        return DEFAULT_REORDER_POINT_PERCENT

    # ------------------------------------------------------------------
    # WebSocket broadcast (Req 1.6.4)
    # ------------------------------------------------------------------

    async def _broadcast_customer_tank_forecast_ready(
        self, forecast: TankForecast
    ) -> None:
        """Emit ``customer_tank_forecast_ready`` for a Customer_Tank forecast.

        Fires on ``/ws/fuel-planning`` via the injected
        :class:`FuelPlanningWSManager`. Payload fields follow the strict
        schema mandated by Requirement 1.6.4: ``run_id``, ``tenant_id``,
        ``customer_tank_id``, ``fuel_type``, ``runout_risk_24h``, and
        ``model_name``. Additional context (``customer_type``,
        ``weather_fallback``, ``hours_to_runout_p90``) is included via
        the ``extra`` parameter so dispatcher UIs can show richer tooltips
        without a follow-up REST query.

        Broadcast failures are logged and swallowed so a misbehaving WS
        manager cannot break the forecasting cycle.
        """
        if self._fuel_planning_ws is None:
            return
        tank_id = getattr(forecast, "customer_tank_id", None)
        if not tank_id:
            return
        try:
            await self._fuel_planning_ws.broadcast_customer_tank_forecast_ready(
                run_id=forecast.run_id or "",
                tenant_id=forecast.tenant_id or "",
                customer_tank_id=tank_id,
                fuel_type=getattr(forecast, "fuel_type", None) or "",
                runout_risk_24h=float(forecast.runout_risk_24h),
                model_name=getattr(forecast, "model_name", None) or "",
                extra={
                    "customer_id": getattr(forecast, "customer_id", None),
                    "customer_type": getattr(forecast, "customer_type", None),
                    "hours_to_runout_p50": forecast.hours_to_runout_p50,
                    "hours_to_runout_p90": forecast.hours_to_runout_p90,
                    "weather_fallback": getattr(
                        forecast, "weather_fallback", False
                    ),
                    "forecast_id": getattr(forecast, "forecast_id", None),
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TankForecastingAgent: customer_tank_forecast_ready "
                "broadcast failed for tank=%s: %s",
                tank_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_eta_hours_from_now(
    eta: Any, now: datetime
) -> Optional[float]:
    """Return the number of hours from ``now`` to ``eta``.

    Accepts datetimes, ISO-8601 strings (with or without ``Z``), and
    ``None``. Returns ``None`` for unparseable input. Naive datetimes
    are assumed UTC so schedule math stays timezone-consistent.
    """
    if eta is None:
        return None
    if isinstance(eta, datetime):
        target = eta
    elif isinstance(eta, str):
        raw = eta.strip()
        if not raw:
            return None
        try:
            target = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    delta = (target - reference).total_seconds() / 3600.0
    return delta
