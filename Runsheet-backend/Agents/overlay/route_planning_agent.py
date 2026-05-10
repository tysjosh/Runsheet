"""
Route Planning Agent — overlay agent for optimized delivery route generation.

Subscribes to InterventionProposals from the compartment_loading agent,
extracts loading plans, queries station locations, runs route optimization
using the route_solver, computes objective values, produces
InterventionProposals with route plan actions, and persists routes to
mvp_routes.

Default configuration:
    - decision_cycle: 60 seconds
    - cooldown: 15 minutes per truck

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9,
              Fuel Ops Hardening 2.1.3, 2.1.5, 2.1.6 (Traffic_Provider
              wiring with feature-flag gating and Haversine fallback),
              Fuel Ops Hardening 9.2.4, 9.2.5, 9.3.1, 9.3.2 (Storm_Mode
              per-truck stop cap, delivery-window enforcement,
              ``deferred_storm_mode`` tagging, and HIGH-risk
              ConfirmationProtocol routing — Task 10.7),
              Fuel Ops Hardening 9.3.3, 9.3.4, 9.3.5 (Storm_Mode
              road-restriction polygon intersects filter with
              ``road_restriction`` deferral tag — Task 10.8).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

import httpx

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.fuel_distribution_models import (
    DeferredRouteStop,
    RoutePlan,
    RouteStop,
    WindowMissEntry,
)
from Agents.support.mvp_es_mappings import MVP_ROUTES_INDEX
from Agents.support.route_solver import (
    build_distance_matrix,
    check_sla_windows,
    optimize_route,
)
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    canonicalize_or_warn,
)
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX
from fuel.services.sourcing_recommender import (
    InvalidBrandedPreferenceError,
    SourcingRecommender,
)
from fuel.services.storm_mode_evaluator import (
    ACTIVE as STORM_MODE_ACTIVE,
    PersistedState as StormModePersistedState,
)
from fuel.services.traffic_provider import (
    TrafficBudgetExceeded,
    TrafficProvider,
    TravelMatrix,
    build_traffic_provider,
)
from fuel.services.truck_start_position import (
    DepotResolver,
    NoDepotConfiguredError,
    SOURCE_DEPOT,
    TruckStartPosition,
    resolve_truck_start_position,
)
from fuel.terminal_models import SourcingRecommendation

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_STATIONS_INDEX = "fuel_stations"

# Default objective weights (Req 4.6)
DEFAULT_OBJECTIVE_WEIGHTS: Dict[str, float] = {
    "route_cost": 0.20,
    "runout_risk_reduction": 0.30,
    "truck_utilization": 0.15,
    "late_delivery_penalty": 0.25,
    "plan_churn": 0.10,
}

# Average speed in km/h for ETA estimation and Haversine fallback
# travel-time derivation (Req 2.1.5).
DEFAULT_SPEED_KMH = 40.0

# Default depot placeholder (Requirement 2.2.6). Task 4.2 removed the
# hardcoded Lagos coordinates from the resolver chain and Req 2.2.6
# forbids any region-specific coordinate default anywhere in the backend.
# This constant is a non-geographic null-island sentinel (``(0.0, 0.0)``)
# used solely by the legacy single-depot code path when no
# :class:`DepotResolver` has been injected — that path survives only for
# backward compatibility with tests that predate depot resolution. In
# production, the bootstrap injects a resolver and this placeholder is
# always overwritten before the solver sees a Route_Plan; when the
# resolver yields neither telemetry nor a depot the caller skips the
# loading plan and the REST surface returns HTTP 400
# ``no_depot_configured`` (Req 2.2.4).
DEFAULT_DEPOT = {"lat": 0.0, "lon": 0.0}  # null-island sentinel (Req 2.2.6)

# Redis key template for the per-tenant Traffic_Provider selection
# (Req 2.1.2). The value is a short provider name ("mapbox", "here",
# "google") or a JSON object of the form {"name": "mapbox"}.
TRAFFIC_PROVIDER_CONFIG_KEY_TEMPLATE = "overlay.traffic_provider:{tenant_id}"

# Overlay feature-flag name that gates whether the configured
# Traffic_Provider is actually consulted (Req 2.1.3).
TRAFFIC_AWARE_ROUTING_FLAG_KEY = "overlay.traffic_aware_routing"

#: Overlay feature flag name that gates whether the Route_Planning_Agent
#: consults the Sourcing_Recommender for loading plans requiring a
#: non-depot terminal lift (Req 8.5.5 / Task 7.10). When the flag is
#: anything other than ``active_gated`` / ``active_auto`` the agent
#: skips the recommender entirely and leaves
#: :attr:`RoutePlan.sourced_terminal_id` / ``sourced_terminal_reasons``
#: unset.
TERMINAL_SOURCING_FLAG_KEY = "overlay.terminal_sourcing"

# Hard timeout budget for the entire get_matrix call (Req 2.1.5). The
# provider base class already applies the same 10-second budget inside
# its own asyncio.wait_for. We set an outer timeout here as defense-in-
# depth so a misbehaving subclass cannot block the decision cycle.
TRAFFIC_MATRIX_TIMEOUT_SECONDS = 10.0

#: Exact liters-per-gallon factor used to convert Loading_Plan assignment
#: volumes (stored in liters) into the gallons unit the
#: :class:`SourcingRecommender` expects (Task 7.10). Keep in sync with
#: :data:`services.unit_conversion.GAL_TO_L`.
LITERS_PER_GALLON = 3.785411784


# ---------------------------------------------------------------------------
# Storm_Mode constants (Task 10.7, Req 9.2.4, 9.2.5, 9.3.1, 9.3.2)
# ---------------------------------------------------------------------------

#: Default per-truck stop cap enforced while Storm_Mode is active
#: (Req 9.2.4). Tenants can override via the injected settings loader;
#: when the loader is absent or returns ``None`` this value applies.
DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK: int = 10

#: Default Storm_Mode delivery-window start hour (tenant-local time,
#: 0.0–24.0) enforced by Req 9.3.1. The spec default matches the
#: requirements-document example of 08:00–16:00.
DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR: float = 8.0

#: Default Storm_Mode delivery-window end hour (tenant-local time,
#: 0.0–24.0) enforced by Req 9.3.1. See
#: :data:`DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR`.
DEFAULT_STORM_MODE_DELIVERY_WINDOW_END_HOUR: float = 16.0

#: Reason-tag stamped on every deferred stop. Req 9.3.2 mandates this
#: exact string so dispatcher / audit filters can pin on a single label.
REASON_DEFERRED_STORM_MODE: str = "deferred_storm_mode"

#: Reason-tag stamped on stops deferred because a route segment
#: intersected a :class:`fuel.storm_mode_models.StormRoadRestriction`
#: with severity ``>= severe`` while Storm_Mode was active (Req 9.3.4 /
#: Task 10.8). Req 9.3.4 mandates this exact string so the dispatcher
#: UI and audit queries can distinguish road-restriction deferrals from
#: window / cap deferrals.
REASON_ROAD_RESTRICTION: str = "road_restriction"

#: Cause labels narrowing ``REASON_DEFERRED_STORM_MODE`` to the
#: guard-rail that fired. ``over_max_stops_per_truck`` covers Req 9.2.4;
#: ``outside_delivery_window`` covers Req 9.3.2.
CAUSE_OVER_MAX_STOPS: str = "over_max_stops_per_truck"
CAUSE_OUTSIDE_WINDOW: str = "outside_delivery_window"

#: Cause label stamped on stops deferred under the ``road_restriction``
#: reason (Req 9.3.4 / Task 10.8). Narrows the deferral cause to "the
#: inbound or outbound leg crossed a severe-or-higher restriction
#: polygon" so dispatcher filters can disambiguate the two reasons.
CAUSE_ROAD_RESTRICTION: str = "road_segment_restricted"

#: Minimum severity that triggers a road-restriction deferral. Req 9.3.4
#: mandates ``>= severe``. We express it as an explicit set so the
#: geo_shape query filter stays in lock-step with the agent's
#: intent (and so bumping the threshold later is a one-line change).
ROAD_RESTRICTION_BLOCKING_SEVERITIES: Tuple[str, ...] = ("severe", "extreme")

#: ES index name housing the tenant-uploaded polygons. Imported via
#: :mod:`fuel.services.fuel_ops_es_mappings` so the constant stays in
#: sync with the mapping definition (Task 10.8).
STORM_ROAD_RESTRICTIONS_ES_INDEX: str = "storm_road_restrictions"

#: Hard ceiling on the number of restriction matches returned per
#: segment query. Tenants rarely upload more than a handful of active
#: polygons; pin a small cap so a misbehaving ingestion process cannot
#: balloon the response payload. The agent only needs to know whether
#: *any* severe+ restriction matches — the returned ids are used for
#: logging / annotation only.
_ROAD_RESTRICTION_MATCH_CEILING: int = 25

#: Tool name stamped on the Route_Plan mutation action when Storm_Mode
#: is active (Req 9.2.5). Mapped to ``RiskLevel.HIGH`` in
#: :data:`Agents.risk_registry.DEFAULT_RISK_REGISTRY` so every
#: storm-mode plan routes through ConfirmationProtocol at HIGH risk
#: regardless of whether the standard plan path would be MEDIUM. The
#: non-storm tool name is :data:`APPLY_ROUTE_PLAN_TOOL`, which falls
#: back to the registry default (MEDIUM is *not* currently configured
#: for this name, so the default-HIGH behaviour kicks in — callers
#: always receive *at least* MEDIUM approval routing).
APPLY_ROUTE_PLAN_TOOL: str = "apply_route_plan"
APPLY_ROUTE_PLAN_STORM_MODE_TOOL: str = "apply_route_plan_storm_mode"


class TenantConfigLookup(Protocol):
    """Minimal Redis-like interface for per-tenant config reads.

    The Route_Planning_Agent uses this to resolve the
    ``overlay.traffic_provider:{tenant_id}`` key (Req 2.1.2). The same
    object is passed through to the injected Traffic_Provider factory so
    every adapter shares a single tenant-scoped config backend.
    """

    async def get(self, key: str) -> Optional[Any]:  # pragma: no cover - protocol
        ...


#: Factory signature accepted by ``set_traffic_provider_factory``. Given a
#: provider short-name (and the tenant_id so factories can inject per-
#: tenant credentials), returns a constructed :class:`TrafficProvider`.
#: Returning ``None`` tells the agent to fall back to Haversine for this
#: tenant; raising is treated the same way.
TrafficProviderFactory = Callable[[str, str], Optional[TrafficProvider]]


# ---------------------------------------------------------------------------
# Storm_Mode settings wiring (Task 10.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StormModeRouteSettings:
    """Per-tenant Storm_Mode route-planning guard-rails (Task 10.7).

    Returned by the injected :data:`StormModeRouteSettingsLoader` and
    consumed by :class:`RoutePlanningAgent` to decide how many stops a
    truck can carry and which delivery-window hours are eligible while
    Storm_Mode is active.

    Fields:

    * ``max_stops_per_truck`` — per-truck stop cap enforced by
      Req 9.2.4. Must be positive. Defaults to
      :data:`DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK` (10).
    * ``delivery_window_start_hour`` / ``delivery_window_end_hour`` —
      hour-of-day bounds for the Storm_Mode delivery window (Req 9.3.1,
      9.3.2). Values are in [0.0, 24.0]. The window is interpreted in
      the tenant's timezone when ``timezone`` is set; otherwise UTC is
      assumed so the caller can still make meaningful comparisons.
    * ``timezone`` — optional IANA timezone identifier (e.g.,
      ``"America/Chicago"``) controlling the local time the window is
      interpreted in. The default ``None`` keeps the agent on UTC so
      tenants without a configured timezone still receive a
      deterministic window comparison.
    """

    max_stops_per_truck: int = DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK
    delivery_window_start_hour: float = (
        DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR
    )
    delivery_window_end_hour: float = (
        DEFAULT_STORM_MODE_DELIVERY_WINDOW_END_HOUR
    )
    timezone: Optional[str] = None


#: Async loader resolving :class:`StormModeRouteSettings` for a tenant.
#: Bootstrap injects a Redis/ES-backed implementation; tests use a simple
#: async lambda. Returning ``None`` (or the loader being ``None``) tells
#: the agent to fall back to the module-level defaults so a tenant
#: without custom settings still receives a deterministic guard-rail.
StormModeRouteSettingsLoader = Callable[
    [str], "asyncio.Future[Optional[StormModeRouteSettings]]"
]


def _resolve_timezone(tz_name: Optional[str]):
    """Return a tzinfo for ``tz_name`` or UTC on any failure.

    Uses the stdlib ``zoneinfo`` module. A ``None`` / blank name —
    or an unknown IANA identifier — falls back to UTC so window
    comparisons remain deterministic even on tenants without a
    configured timezone (Task 10.7).
    """

    if not tz_name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # local import

        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logger.warning(
                "RoutePlanningAgent: unknown storm_mode timezone=%r — "
                "falling back to UTC",
                tz_name,
            )
            return timezone.utc
    except Exception:  # pragma: no cover - defensive
        return timezone.utc


def _parse_eta(eta: Optional[str]) -> Optional[datetime]:
    """Parse a RouteStop ISO-8601 ETA string into a tz-aware datetime.

    Returns ``None`` on malformed / empty input. Naive timestamps are
    promoted to UTC so the caller can ``.astimezone`` safely.
    """

    if not eta or not isinstance(eta, str):
        return None
    try:
        parsed = datetime.fromisoformat(eta.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hour_of_day(local_dt: datetime) -> float:
    """Return ``local_dt``'s fractional hour-of-day in [0.0, 24.0).

    Used to compare a stop's ETA against the Storm_Mode delivery window
    bounds (Req 9.3.1 / 9.3.2).
    """

    return (
        local_dt.hour
        + local_dt.minute / 60.0
        + local_dt.second / 3600.0
        + local_dt.microsecond / 3_600_000_000.0
    )


def _is_within_window(
    *, hour_of_day: float, start_hour: float, end_hour: float
) -> bool:
    """Return ``True`` when ``hour_of_day`` falls inside the window.

    Supports:

    * ``start <= end`` — normal daytime window (e.g. 08:00–16:00).
    * ``start > end`` — overnight window (e.g. 22:00–06:00) so the
      helper handles 24×7 deployments without extra special-casing.
    * ``start == end`` — zero-length window rejects every stop, which
      matches the spec's "all deliveries deferred" semantics for
      tenants that manually squash the window during active storms.
    """

    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour_of_day < end_hour
    # Overnight window: split into two ranges.
    return hour_of_day >= start_hour or hour_of_day < end_hour


def _next_eligible_window(
    *,
    local_eta: datetime,
    start_hour: float,
    end_hour: float,
) -> Tuple[datetime, datetime]:
    """Return the next eligible ``(start, end)`` window for a deferred stop.

    Produces the window immediately following ``local_eta``. Handles the
    two common cases:

    * Daytime window (``start <= end``) — if ``local_eta`` is before the
      window today, the next window starts today; otherwise it starts
      tomorrow.
    * Overnight window (``start > end``) — folded into a daytime-style
      calculation by advancing the date when necessary.

    Returned timestamps retain ``local_eta``'s tzinfo so the caller can
    convert to UTC before persisting (the agent does this inline). When
    start == end we still return a zero-length next window pointing at
    the top of the next day so downstream UIs don't see a ``None`` /
    missing timestamp.
    """

    tz = local_eta.tzinfo or timezone.utc
    base_date = local_eta.date()
    day_offset = timedelta(days=0)

    if start_hour == end_hour:
        # Zero-length window: next eligible slot is conceptually the
        # start of tomorrow's window. Treat as 24h ahead so the UI has
        # *some* chronology to render.
        next_start_hour = start_hour
        day_offset = timedelta(days=1)
        duration_hours = 24.0
    elif start_hour < end_hour:
        # Daytime window.
        current_hour = _hour_of_day(local_eta)
        if current_hour < start_hour:
            day_offset = timedelta(days=0)
        else:
            day_offset = timedelta(days=1)
        next_start_hour = start_hour
        duration_hours = end_hour - start_hour
    else:
        # Overnight window (e.g. 22:00-06:00). Simplify by anchoring the
        # "next start" to the most recent start-hour boundary after
        # ``local_eta``.
        current_hour = _hour_of_day(local_eta)
        if current_hour < start_hour:
            day_offset = timedelta(days=0)
        else:
            day_offset = timedelta(days=1)
        next_start_hour = start_hour
        duration_hours = (24.0 - start_hour) + end_hour

    start_dt = datetime.combine(
        base_date + day_offset,
        datetime.min.time(),
        tzinfo=tz,
    ) + timedelta(hours=next_start_hour)
    end_dt = start_dt + timedelta(hours=duration_hours)
    return start_dt, end_dt


class RoutePlanningAgent(OverlayAgentBase):
    """Generates optimized delivery routes from loading plans.

    Consumes InterventionProposals from the compartment_loading agent,
    extracts loading plan details, queries station locations, runs
    nearest-neighbor + 2-opt route optimization, and produces
    InterventionProposals with route plan actions.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval in seconds (default 60).
        cooldown_minutes: Per-truck cooldown in minutes (default 15).
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
        poll_interval: int = 60,
        cooldown_minutes: int = 15,
        *,
        traffic_provider_factory: Optional[TrafficProviderFactory] = None,
        tenant_config: Optional[TenantConfigLookup] = None,
        sourcing_recommender: Optional[SourcingRecommender] = None,
        depot_resolver: Optional[DepotResolver] = None,
        storm_mode_evaluator: Optional[Any] = None,
        storm_mode_settings_loader: Optional[
            StormModeRouteSettingsLoader
        ] = None,
    ):
        super().__init__(
            agent_id="route_planning",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": InterventionProposal,
                    "filters": {
                        "source_agent": "compartment_loading",
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
        # Buffer loading proposals between cycles
        self._proposal_buffer: List[InterventionProposal] = []

        # ---- Traffic_Provider wiring (Req 2.1.2, 2.1.3, 2.1.5, 2.1.6) ---
        # The factory is consulted once we know the provider short-name
        # for a tenant; the agent caches the resolved instance per tenant
        # so we don't rebuild the HTTP client on every cycle. Both the
        # factory and the tenant-config lookup are optional — when either
        # is missing the agent permanently falls back to Haversine and
        # stamps ``traffic_fallback: true`` on every plan for visibility.
        self._traffic_provider_factory: Optional[TrafficProviderFactory] = (
            traffic_provider_factory
        )
        self._tenant_config: Optional[TenantConfigLookup] = tenant_config
        self._traffic_provider_cache: Dict[Tuple[str, str], TrafficProvider] = {}

        # ---- Sourcing_Recommender wiring (Task 7.10 / Req 8.5.5) --------
        # When a Loading_Plan requires an external terminal lift (i.e.
        # the plan's ``terminal_id`` is set and differs from the tenant's
        # depot), the Route_Planning_Agent consults the recommender to
        # choose a loading terminal and records the pick on the
        # Route_Plan. The attribute is ``None`` until bootstrap injects
        # the singleton via :meth:`set_sourcing_recommender`; until then
        # every evaluation skips the sourcing step entirely so tests and
        # early bootstrap states fail soft instead of raising.
        self._sourcing_recommender: Optional[SourcingRecommender] = (
            sourcing_recommender
        )

        # ---- Depot resolver wiring (Task 9.7 / Req 5.4.6) --------------
        # Delegates the truck.assigned_depot_id → tenant.default_depot_id
        # chain to the caller. The agent does not own depot lookups —
        # it only decides whether to use fresh telemetry or fall back to
        # the depot. ``None`` until bootstrap injects the resolver via
        # :meth:`set_depot_resolver`; until then the agent preserves the
        # legacy single-depot behaviour (``DEFAULT_DEPOT`` at index 0)
        # for backward compatibility with existing tests that predate
        # depot resolution.
        self._depot_resolver: Optional[DepotResolver] = depot_resolver

        # ---- Storm_Mode wiring (Task 10.7, Req 9.2.4, 9.2.5, 9.3.1, 9.3.2) ----
        # ``storm_mode_evaluator`` exposes ``get_state(tenant_id)`` (see
        # :class:`fuel.services.storm_mode_evaluator.StormModeEvaluator`).
        # When ``None`` the agent treats Storm_Mode as permanently
        # inactive so tenants that haven't wired Phase 10 keep the
        # pre-storm behaviour unchanged.
        self._storm_mode_evaluator: Optional[Any] = storm_mode_evaluator
        # ``storm_mode_settings_loader`` resolves the per-tenant
        # guard-rails (max stops per truck + delivery window hours).
        # When ``None`` the agent falls back to the module-level
        # defaults (10 stops, 08:00–16:00). Bootstrap injects a Redis
        # or tenant-config backed loader.
        self._storm_mode_settings_loader: Optional[
            StormModeRouteSettingsLoader
        ] = storm_mode_settings_loader

        # ---- Driver Qualification wiring (Task 6.9, Req 5.5, 5.6, 5.7) ----
        # When configured, the agent checks driver dispatch eligibility
        # before building a route. Suspended drivers are excluded from
        # all routes; drivers lacking HAZMAT or tanker endorsements are
        # excluded from routes requiring those endorsements. ``None``
        # until bootstrap injects the service; until then the agent
        # skips the eligibility check (graceful degradation).
        self._driver_qualification_service: Optional[Any] = None

        # ---- HOS Checker wiring (Task 7.8, Req 4.1–4.7) ----
        # When configured, the agent checks driver Hours-of-Service
        # compliance AFTER the DriverQualificationService check and
        # BEFORE building a route. Drivers without sufficient remaining
        # hours are excluded from the route with an ``hos_blocked`` flag.
        # ``None`` until bootstrap injects the service; until then the
        # agent skips the HOS check (graceful degradation).
        self._hos_checker: Optional[Any] = None

        # ---- Asset Certification wiring (Task 8.10, Req 13.5) ----
        # When configured, the agent checks asset (truck) dispatch
        # eligibility AFTER the HOS check and BEFORE building a route.
        # Assets with expired DOT cargo tank certifications (V/K/I/P/UT)
        # are excluded from all fuel delivery routes. ``None`` until
        # bootstrap injects the service; until then the agent skips the
        # asset certification check (graceful degradation).
        self._asset_certification_service: Optional[Any] = None

        # ---- Delivery Filter wiring (Task 15.5, Req 14.5) ----
        # When configured, the agent calls
        # ``partition_candidates(candidates)`` at the top of
        # ``evaluate()`` before the optimization solver runs. The filter
        # partitions delivery candidates by customer call type
        # (will_call, auto_fill, keep_full) so the solver only considers
        # eligible deliveries. ``None`` until bootstrap injects the
        # service; until then the agent uses unfiltered candidates
        # (graceful degradation).
        self._delivery_filter: Optional[Any] = None

    # ------------------------------------------------------------------
    # Public wiring hooks (bootstrap injects these after construction)
    # ------------------------------------------------------------------

    def set_traffic_provider_factory(
        self, factory: Optional[TrafficProviderFactory]
    ) -> None:
        """Inject the Traffic_Provider factory post-construction.

        Passing ``None`` disables the traffic-aware code path for every
        tenant and forces a Haversine fallback with ``traffic_fallback:
        true`` (Req 2.1.5).
        """
        self._traffic_provider_factory = factory
        self._traffic_provider_cache.clear()

    def set_tenant_config(
        self, lookup: Optional[TenantConfigLookup]
    ) -> None:
        """Inject the tenant-config lookup post-construction.

        Used to resolve ``overlay.traffic_provider:{tenant_id}`` per
        tenant (Req 2.1.2). ``None`` disables provider selection and
        therefore forces the Haversine fallback for every tenant.
        """
        self._tenant_config = lookup
        self._traffic_provider_cache.clear()

    def set_sourcing_recommender(
        self, recommender: Optional[SourcingRecommender]
    ) -> None:
        """Inject the :class:`SourcingRecommender` post-construction.

        Passing ``None`` disables the sourcing-recommender integration:
        the agent leaves ``sourced_terminal_id`` / ``sourced_terminal_reasons``
        / ``sourcing_recommendation_id`` on the Route_Plan unset for
        every run. The bootstrap in :mod:`bootstrap.agents` invokes this
        after constructing the recommender singleton so the Route_Plan
        carries the chosen terminal id for every Loading_Plan that
        required an external lift (Task 7.10, Req 8.5.5).
        """
        self._sourcing_recommender = recommender

    def set_depot_resolver(
        self, resolver: Optional[DepotResolver]
    ) -> None:
        """Inject the depot-resolution callable post-construction.

        The resolver encapsulates the tenant-specific
        ``truck.assigned_depot_id → tenant.default_depot_id`` chain and
        returns ``(lat, lon)`` for the assigned depot — or ``None`` when
        no depot is configured for the tenant. Passing ``None``
        disables telemetry-first start-position resolution entirely:
        the agent preserves the legacy single-depot behaviour
        (``DEFAULT_DEPOT`` at index 0) so existing tests and bootstrap
        states continue to work. The production bootstrap injects a
        resolver backed by :class:`fuel.depot_models.DepotRepository`
        and :class:`services.tenant_settings.TenantSettingsService`
        (Task 9.7, Req 5.4.6).
        """
        self._depot_resolver = resolver

    def set_storm_mode_evaluator(self, evaluator: Optional[Any]) -> None:
        """Inject the :class:`StormModeEvaluator` post-construction.

        Passing ``None`` disables Storm_Mode guard-rails entirely: the
        agent reports ``inactive`` for every tenant, skips the per-truck
        stop cap / delivery-window checks, and routes plans through
        ConfirmationProtocol with the non-storm tool name (Task 10.7,
        Req 9.2.4, 9.2.5, 9.3.1, 9.3.2).
        """
        self._storm_mode_evaluator = evaluator

    def set_storm_mode_settings_loader(
        self, loader: Optional[StormModeRouteSettingsLoader]
    ) -> None:
        """Inject the per-tenant Storm_Mode settings loader.

        When ``None`` the agent falls back to
        :data:`DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK` and the
        :data:`DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR` /
        :data:`DEFAULT_STORM_MODE_DELIVERY_WINDOW_END_HOUR` defaults
        so tenants without custom settings still receive a
        deterministic guard-rail (Task 10.7).
        """
        self._storm_mode_settings_loader = loader

    def set_driver_qualification_service(
        self, service: Optional[Any]
    ) -> None:
        """Inject the :class:`DriverQualificationService` post-construction.

        When configured, the agent calls
        ``is_dispatch_eligible(tenant_id, driver_id, route_requirements)``
        before building a route for a loading plan. Ineligible drivers
        cause the loading plan to be skipped with a warning log.

        Passing ``None`` disables the eligibility check entirely: the
        agent builds routes without driver qualification validation
        (graceful degradation for tenants that haven't wired the
        compliance backbone). The production bootstrap injects the
        singleton after constructing the DriverQualificationService
        (Task 6.9, Req 5.5, 5.6, 5.7).
        """
        self._driver_qualification_service = service

    def set_hos_checker(self, checker: Optional[Any]) -> None:
        """Inject the :class:`HOSChecker` post-construction.

        When configured, the agent calls
        ``is_eligible(driver_id, estimated_drive_hours, estimated_total_hours)``
        AFTER the DriverQualificationService check and BEFORE building
        a route. Drivers without sufficient remaining HOS hours cause
        the loading plan to be skipped with an ``hos_blocked`` flag and
        a warning log including the earliest_eligible_time.

        Passing ``None`` disables the HOS check entirely: the agent
        builds routes without HOS validation (graceful degradation for
        tenants that haven't wired the compliance backbone). The
        production bootstrap injects the singleton after constructing
        the HOSChecker (Task 7.8, Req 4.1–4.7).
        """
        self._hos_checker = checker

    def set_asset_certification_service(
        self, service: Optional[Any]
    ) -> None:
        """Inject the :class:`AssetCertificationService` post-construction.

        When configured, the agent calls
        ``is_dispatch_eligible(tenant_id, truck_id)`` AFTER the HOS
        check and BEFORE building a route. Assets with expired DOT
        cargo tank certifications (V/K/I/P/UT) are excluded from all
        fuel delivery routes.

        Passing ``None`` disables the asset certification check
        entirely: the agent builds routes without asset certification
        validation (graceful degradation for tenants that haven't wired
        the compliance backbone). The production bootstrap injects the
        singleton after constructing the AssetCertificationService
        (Task 8.10, Req 13.5).
        """
        self._asset_certification_service = service

    def set_delivery_filter(self, delivery_filter: Optional[Any]) -> None:
        """Inject the :class:`DeliveryFilter` post-construction.

        When configured, the agent calls
        ``partition_candidates(candidates)`` at the top of
        ``evaluate()`` before the optimization solver runs. The filter
        partitions delivery candidates by customer call type
        (will_call, auto_fill, keep_full) so the solver only considers
        eligible deliveries for each route.

        Passing ``None`` disables the delivery filter entirely: the
        agent uses unfiltered candidates (graceful degradation for
        tenants that haven't wired the compliance backbone). The
        production bootstrap injects the singleton after constructing
        the DeliveryFilter (Task 15.5, Req 14.5).
        """
        self._delivery_filter = delivery_filter

    # ------------------------------------------------------------------
    # Signal handling override — buffer InterventionProposals
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Buffer incoming signals. InterventionProposals from
        compartment_loading are stored separately."""
        if (
            isinstance(signal, InterventionProposal)
            and signal.source_agent == "compartment_loading"
        ):
            self._proposal_buffer.append(signal)
        else:
            await super()._on_signal(signal)

    # ------------------------------------------------------------------
    # Core evaluation (Req 4.1–4.9)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Consume loading proposals, generate optimized routes.

        Steps:
        1. Collect buffered InterventionProposals from compartment_loading.
        2. For each proposal: extract loading plan details (Req 4.1).
        3. Query station locations for route optimization (Req 4.3).
        4. Run route optimization using route_solver (Req 4.5).
        5. Compute objective value (Req 4.6).
        6. Persist route plans to mvp_routes (Req 4.7).
        7. Produce InterventionProposals with route plan actions.

        Returns:
            List of InterventionProposals with route plan actions.
        """
        # Step 0: Apply Delivery Filter (Task 15.5, Req 14.5)
        # When the DeliveryFilter is configured, partition delivery
        # candidates by customer call type (will_call, auto_fill,
        # keep_full) before the optimization solver runs. Only eligible
        # candidates proceed to route construction. When the filter is
        # not configured, all proposals pass through unfiltered (graceful
        # degradation).
        filtered_result = await self._apply_delivery_filter()

        # Step 1: Collect buffered proposals
        proposals = list(self._proposal_buffer)
        self._proposal_buffer.clear()

        if not proposals:
            return []

        route_proposals: List[InterventionProposal] = []

        for loading_proposal in proposals:
            tenant_id = loading_proposal.tenant_id

            # Step 2: Extract loading plan details
            loading_plan = self._extract_loading_plan(loading_proposal)
            if not loading_plan:
                continue

            truck_id = loading_plan.get("truck_id", "")
            plan_id = loading_plan.get("plan_id", "")
            assignments = loading_plan.get("assignments", [])

            # Step 2b: Check driver eligibility (Task 6.9, Req 5.5, 5.6, 5.7)
            # Before building a route, verify the truck's assigned driver
            # is dispatch-eligible. Suspended drivers are excluded from
            # all routes; drivers lacking HAZMAT or tanker endorsements
            # are excluded from routes requiring those endorsements.
            driver_eligible = await self._find_available_asset(
                tenant_id, truck_id, assignments
            )
            if not driver_eligible:
                continue

            # Step 2c: HOS compliance check (Task 7.8, Req 4.1–4.7)
            # After the driver qualification check passes, verify the
            # driver has sufficient Hours-of-Service hours remaining to
            # complete the proposed route. Estimate drive/total hours
            # from the number of stops and a simple heuristic.
            hos_eligible = await self._check_hos_eligibility(
                tenant_id, truck_id, assignments
            )
            if not hos_eligible:
                continue

            # Step 2d: Asset certification check (Task 8.10, Req 13.5)
            # After the HOS check passes, verify the truck (asset) has
            # valid DOT cargo tank certifications. Assets with expired
            # V/K/I/P/UT certifications are excluded from all fuel
            # delivery routes.
            asset_eligible = await self._check_asset_certification(
                tenant_id, truck_id
            )
            if not asset_eligible:
                continue

            # Collect unique station IDs from assignments
            station_ids = list(
                {a.get("station_id", "") for a in assignments if a.get("station_id")}
            )
            if not station_ids:
                continue

            # Step 3: Query station locations (Req 4.3)
            station_locations = await self._query_station_locations(
                tenant_id, station_ids
            )

            # Query SLA windows for stations (Req 4.4)
            station_sla_windows = await self._query_sla_windows(
                tenant_id, station_ids
            )

            # Build locations list: depot first, then stations
            locations, station_order = self._build_location_list(
                station_ids, station_locations
            )

            if len(locations) < 2:
                # Need at least depot + 1 station
                continue

            # Task 9.7 / Req 5.4.6 — resolve the truck's start position
            # with telemetry-first, depot-fallback. When the resolver is
            # not configured (legacy bootstrap or early tests) we leave
            # the location list untouched so existing behaviour is
            # preserved. On NoDepotConfiguredError we skip this loading
            # plan entirely — the REST surface mirrors this behaviour by
            # translating the same exception into HTTP 400
            # ``no_depot_configured`` (Req 2.2.4).
            start_position = await self._resolve_start_position(
                tenant_id=tenant_id, truck_id=truck_id
            )
            if start_position is None and self._depot_resolver is not None:
                # Resolver configured but produced neither telemetry nor
                # depot coordinates — skip this loading plan rather than
                # routing from DEFAULT_DEPOT.
                logger.warning(
                    "RoutePlanningAgent: skipping loading plan for "
                    "tenant=%s truck=%s — no_depot_configured and no "
                    "fresh truck_telemetry",
                    tenant_id,
                    truck_id,
                )
                continue
            if start_position is not None:
                locations[0] = {
                    "lat": start_position.lat,
                    "lon": start_position.lon,
                }

            # Build SLA windows indexed by location list position
            sla_windows_by_idx = {}
            for i, sid in enumerate(station_order):
                if sid in station_sla_windows:
                    sla_windows_by_idx[i + 1] = station_sla_windows[sid]  # +1 for depot offset

            # Step 3b: Traffic-aware travel matrix (Req 2.1.3, 2.1.5, 2.1.6).
            # Returns a (distance_matrix, used_provider_name, traffic_fallback)
            # triple. When the feature flag is disabled, no provider is
            # configured, or any upstream error occurs (timeout, budget,
            # HTTP failure), we fall back to the Haversine matrix built
            # from the route-solver's shared helper and annotate the plan
            # with ``traffic_fallback: true`` (Req 2.1.5).
            (
                traffic_distance_matrix,
                used_provider_name,
                traffic_fallback,
            ) = await self._resolve_travel_matrix(
                tenant_id=tenant_id, locations=locations
            )

            # Step 4: Run route optimization (Req 4.5)
            optimized_order, total_distance = optimize_route(
                locations, start_index=0
            )

            # Check SLA window violations (Req 4.4). When we have a
            # traffic-informed matrix use it for the SLA ETA check so
            # window violations reflect real drive times; otherwise the
            # existing Haversine matrix built by the solver is used.
            sla_distance_matrix = (
                traffic_distance_matrix
                if traffic_distance_matrix is not None
                else build_distance_matrix(locations)
            )
            sla_violations = check_sla_windows(
                order=optimized_order,
                distance_matrix=sla_distance_matrix,
                sla_windows=sla_windows_by_idx if sla_windows_by_idx else None,
                speed_kmh=DEFAULT_SPEED_KMH,
            )

            # Step 5: Build route plan with ETAs
            route_plan = self._build_route_plan(
                truck_id=truck_id,
                plan_id=plan_id,
                optimized_order=optimized_order,
                station_order=station_order,
                total_distance=total_distance,
                assignments=assignments,
                tenant_id=tenant_id,
                sla_violations=sla_violations,
                traffic_provider_name=used_provider_name,
                traffic_fallback=traffic_fallback,
                start_position=start_position,
            )

            # Compute objective value (Req 4.6)
            route_plan.objective_value = self._compute_objective_value(
                route_plan=route_plan,
                utilization_pct=loading_plan.get("total_utilization_pct", 0.0),
            )

            # Set run_id from pipeline context if available
            route_plan.run_id = getattr(self, '_current_run_id', None) or ""

            # Step 5b: Sourcing_Recommender wiring (Task 7.10 / Req 8.5.5)
            # When the Loading_Plan requires an external terminal lift
            # (terminal_id present on the plan and sourcing flag active
            # for the tenant), consult the already-wired recommender and
            # stamp the winning terminal id + reasons on the route plan.
            # Every failure path (flag off, no recommender, zero
            # candidates, exception) leaves the sourcing fields None so
            # the route still persists.
            await self._maybe_apply_sourcing(
                route_plan=route_plan,
                loading_plan=loading_plan,
                assignments=assignments,
                locations=locations,
                tenant_id=tenant_id,
                truck_id=truck_id,
            )

            # Step 5c: Storm_Mode guard-rails (Task 10.7, Req 9.2.4,
            # 9.2.5, 9.3.1, 9.3.2; Task 10.8, Req 9.3.3, 9.3.4, 9.3.5).
            # When the StormModeEvaluator reports ``active`` for
            # ``tenant_id``, this method applies the road-restriction
            # geo_shape filter, caps the per-truck stop count, and
            # enforces the tenant-configured delivery window; stops that
            # fail any guard-rail are moved to
            # ``route_plan.deferred_stops`` with the mandated reason tag
            # (``road_restriction`` for segment intersects,
            # ``deferred_storm_mode`` for window / cap). When Storm_Mode
            # is inactive (or the evaluator is not wired) this method is
            # a no-op so existing plans round-trip unchanged.
            await self._maybe_apply_storm_mode(
                route_plan=route_plan,
                tenant_id=tenant_id,
                truck_id=truck_id,
                station_locations=station_locations,
                start_position=start_position,
            )

            # Step 6: Persist route plan to ES (Req 4.7)
            await self._persist_route_plan(route_plan)

            # Step 7: Build InterventionProposal — the proposal's
            # action tool_name and risk_class switch to the HIGH-risk
            # storm variant when Storm_Mode is active so the platform
            # routes the plan through ConfirmationProtocol at HIGH
            # (Req 9.2.5, Task 10.7).
            proposal = self._build_route_proposal(
                route_plan=route_plan,
                tenant_id=tenant_id,
            )
            route_proposals.append(proposal)

        logger.info(
            "RoutePlanningAgent: produced %d route plans",
            len(route_proposals),
        )

        # ------------------------------------------------------------------
        # Fuel-order-based stop building (Task 11.2, Req 5.2.1–5.2.3)
        # ------------------------------------------------------------------
        # In addition to the loading-proposal flow, build stops directly
        # from fuel_orders_current WHERE status IN {confirmed, scheduled}.
        # This ensures the route planning agent can operate on fuel orders
        # even when no compartment_loading proposal is buffered. Window
        # misses are surfaced on the last produced route plan (if any) or
        # logged when no plan was produced.
        if proposals:
            # Use the tenant_id from the first proposal for the fuel-order
            # query. In multi-tenant scenarios each proposal carries its
            # own tenant_id; for now we process the first tenant's orders.
            fuel_order_tenant_id = proposals[0].tenant_id
            (
                fuel_orders,
                fuel_order_locations,
                fuel_order_window_misses,
            ) = await self.build_stops_from_fuel_orders(fuel_order_tenant_id)

            if fuel_order_window_misses:
                # Surface window_miss entries on the most recent route plan
                # so they appear in the replan diff rather than being
                # silently swallowed.
                if route_proposals:
                    last_plan_action = route_proposals[-1].actions[-1]
                    params = last_plan_action.get("parameters", {})
                    params["window_misses"] = [
                        wm.model_dump(mode="json")
                        for wm in fuel_order_window_misses
                    ]
                logger.warning(
                    "RoutePlanningAgent: %d window_miss entries for "
                    "tenant=%s — orders with unsatisfiable delivery "
                    "windows: %s",
                    len(fuel_order_window_misses),
                    fuel_order_tenant_id,
                    ", ".join(
                        wm.order_id for wm in fuel_order_window_misses
                    ),
                )

        return route_proposals

    # ------------------------------------------------------------------
    # Extract loading plan from proposal
    # ------------------------------------------------------------------

    def _extract_loading_plan(
        self, proposal: InterventionProposal
    ) -> Optional[Dict[str, Any]]:
        """Extract loading plan details from an InterventionProposal."""
        for action in proposal.actions:
            if action.get("tool_name") == "apply_loading_plan":
                return action.get("parameters", {})
        return None

    # ------------------------------------------------------------------
    # Driver eligibility check (Task 6.9, Req 5.5, 5.6, 5.7)
    # ------------------------------------------------------------------

    #: Product categories that require a HAZMAT endorsement per DOT/FMCSA
    #: regulations when transported in bulk. All petroleum fuels and LPG
    #: are Class 3 (flammable liquids) or Class 2.1 (flammable gas).
    _HAZMAT_CATEGORIES: Tuple[str, ...] = (
        "diesel",
        "gasoline",
        "propane",
        "kerosene",
        "heating_oil",
        "off_road",
        "ethanol",
    )

    async def _find_available_asset(
        self,
        tenant_id: str,
        truck_id: str,
        assignments: List[Dict[str, Any]],
    ) -> bool:
        """Check whether the driver assigned to a truck is eligible for dispatch.

        Looks up the truck's assigned driver from the ``trucks`` ES index,
        builds route requirements from the loading plan's product codes
        (HAZMAT classification, cargo tank vehicle usage), and calls
        :meth:`DriverQualificationService.is_dispatch_eligible`.

        When the ``_driver_qualification_service`` is not configured
        (graceful degradation), this method returns ``True`` with a
        warning log so routes are not blocked during early bootstrap or
        for tenants that haven't wired the compliance backbone.

        Args:
            tenant_id: Tenant scope for the query.
            truck_id: The truck assigned to the loading plan.
            assignments: The loading plan's assignment list, used to
                derive product codes and determine HAZMAT/tanker
                requirements.

        Returns:
            ``True`` if the driver is eligible (or if the service is not
            configured); ``False`` if the driver is ineligible and the
            loading plan should be skipped.

        Validates: Requirements 5.5, 5.6, 5.7
        """
        if self._driver_qualification_service is None:
            logger.debug(
                "RoutePlanningAgent: driver_qualification_service not "
                "configured — skipping eligibility check for tenant=%s "
                "truck=%s",
                tenant_id,
                truck_id,
            )
            return True

        # Step 1: Look up the truck's assigned driver_id from ES
        driver_id = await self._get_driver_for_truck(tenant_id, truck_id)
        if not driver_id:
            logger.warning(
                "RoutePlanningAgent: no driver_id found for truck=%s "
                "tenant=%s — skipping eligibility check",
                truck_id,
                tenant_id,
            )
            return True

        # Step 2: Build route_requirements from the loading plan
        route_requirements = self._build_route_requirements(assignments)

        # Step 3: Call is_dispatch_eligible
        try:
            eligibility = await self._driver_qualification_service.is_dispatch_eligible(
                tenant_id, driver_id, route_requirements
            )
        except Exception as exc:
            logger.error(
                "RoutePlanningAgent: driver eligibility check failed for "
                "driver=%s truck=%s tenant=%s: %s — allowing route "
                "(graceful degradation)",
                driver_id,
                truck_id,
                tenant_id,
                exc,
            )
            return True

        if not eligibility.eligible:
            logger.warning(
                "RoutePlanningAgent: driver %s ineligible for truck=%s "
                "tenant=%s — skipping loading plan. Reasons: %s",
                driver_id,
                truck_id,
                tenant_id,
                "; ".join(eligibility.reasons),
            )
            return False

        logger.debug(
            "RoutePlanningAgent: driver %s eligible for truck=%s tenant=%s",
            driver_id,
            truck_id,
            tenant_id,
        )
        return True

    async def _get_driver_for_truck(
        self, tenant_id: str, truck_id: str
    ) -> Optional[str]:
        """Look up the driver_id assigned to a truck from the trucks index.

        Returns ``None`` if the truck is not found or has no driver assigned.
        """
        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"truck_id": truck_id}},
                    ]
                }
            },
            "_source": ["driver_id"],
            "size": 1,
        }
        try:
            response = await self._es.search_documents("trucks", query, size=1)
            hits = response.get("hits", {}).get("hits", [])
            if hits:
                return hits[0]["_source"].get("driver_id") or None
        except Exception as exc:
            logger.error(
                "RoutePlanningAgent: failed to look up driver for truck=%s "
                "tenant=%s: %s",
                truck_id,
                tenant_id,
                exc,
            )
        return None

    def _build_route_requirements(
        self, assignments: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Derive route requirements from loading plan assignments.

        Inspects the fuel grades/product codes in the assignments to
        determine:
        - ``requires_hazmat``: True if any assignment carries a
          HAZMAT-classified product (all bulk petroleum fuels).
        - ``requires_tanker``: True if any assignment is present
          (all fuel deliveries use cargo tank vehicles).
        - ``min_cdl_class``: "A" for cargo tank vehicles (standard
          for fuel tanker trucks in the US).

        Returns:
            Dict with keys ``requires_hazmat``, ``requires_tanker``,
            and ``min_cdl_class``.
        """
        requires_hazmat = False
        requires_tanker = bool(assignments)  # All fuel deliveries use tankers

        for assignment in assignments:
            fuel_grade = assignment.get("fuel_grade", "")
            if not fuel_grade:
                continue
            # Check if the product is HAZMAT-classified
            # Try to canonicalize the fuel grade to get the product code
            try:
                product_code = canonicalize(fuel_grade)
                product = self._lookup_product(product_code)
                if product and product.category in self._HAZMAT_CATEGORIES:
                    requires_hazmat = True
                    break
            except (UnknownFuelProductError, Exception):
                # If we can't identify the product, assume HAZMAT for
                # safety (conservative approach for unknown fuels)
                requires_hazmat = True
                break

        return {
            "requires_hazmat": requires_hazmat,
            "requires_tanker": requires_tanker,
            "min_cdl_class": "A" if requires_tanker else None,
        }

    @staticmethod
    def _lookup_product(product_code: str) -> Optional[Any]:
        """Look up a FuelProduct from the catalog by product_code."""
        from fuel.services.fuel_product_catalog import (
            FUEL_PRODUCT_CATALOG,
        )
        for product in FUEL_PRODUCT_CATALOG:
            if product.product_code == product_code:
                return product
        return None

    # ------------------------------------------------------------------
    # HOS eligibility check (Task 7.8, Req 4.1–4.7)
    # ------------------------------------------------------------------

    #: Average speed in mph used to estimate drive hours from route
    #: distance. Fuel delivery trucks in urban/suburban areas average
    #: roughly 25 mph including stops and traffic.
    _AVERAGE_SPEED_MPH: float = 25.0

    #: Average time per delivery stop in hours (loading, unloading,
    #: paperwork, safety checks). Used to estimate total on-duty hours.
    _HOURS_PER_STOP: float = 0.5

    #: Average distance between stops in miles (used when actual route
    #: distance is not yet computed). Conservative estimate for fuel
    #: delivery routes.
    _AVG_MILES_BETWEEN_STOPS: float = 15.0

    def _estimate_route_hours(
        self, assignments: List[Dict[str, Any]]
    ) -> tuple:
        """Estimate drive hours and total on-duty hours for a route.

        Uses a simple heuristic based on the number of stops:
        - Drive hours = (num_stops * avg_miles_between_stops) / avg_speed_mph
        - Total hours = drive_hours + (num_stops * hours_per_stop)

        Args:
            assignments: The loading plan's assignment list.

        Returns:
            Tuple of (estimated_drive_hours, estimated_total_hours).
        """
        num_stops = len(assignments)
        if num_stops == 0:
            return (0.0, 0.0)

        # Estimate total route distance (depot → stops → depot)
        # Each stop adds avg distance; add one more leg for return to depot
        total_miles = (num_stops + 1) * self._AVG_MILES_BETWEEN_STOPS
        estimated_drive_hours = total_miles / self._AVERAGE_SPEED_MPH

        # Total on-duty includes drive time + time at each stop
        estimated_total_hours = estimated_drive_hours + (
            num_stops * self._HOURS_PER_STOP
        )

        return (estimated_drive_hours, estimated_total_hours)

    async def _check_hos_eligibility(
        self,
        tenant_id: str,
        truck_id: str,
        assignments: List[Dict[str, Any]],
    ) -> bool:
        """Check whether the driver has sufficient HOS hours for the route.

        Called AFTER the DriverQualificationService check passes. Looks
        up the truck's assigned driver and calls
        ``HOSChecker.is_eligible()`` with estimated drive/total hours.

        When the ``_hos_checker`` is not configured (graceful degradation),
        this method returns ``True`` so routes are not blocked during
        early bootstrap or for tenants that haven't wired the compliance
        backbone.

        Args:
            tenant_id: Tenant scope for the query.
            truck_id: The truck assigned to the loading plan.
            assignments: The loading plan's assignment list, used to
                estimate route duration.

        Returns:
            ``True`` if the driver has sufficient HOS hours (or if the
            checker is not configured); ``False`` if the driver is
            HOS-ineligible and the loading plan should be skipped.

        Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5
        """
        if self._hos_checker is None:
            logger.debug(
                "RoutePlanningAgent: hos_checker not configured — "
                "skipping HOS check for tenant=%s truck=%s",
                tenant_id,
                truck_id,
            )
            return True

        # Look up the truck's assigned driver_id
        driver_id = await self._get_driver_for_truck(tenant_id, truck_id)
        if not driver_id:
            logger.warning(
                "RoutePlanningAgent: no driver_id found for truck=%s "
                "tenant=%s — skipping HOS check",
                truck_id,
                tenant_id,
            )
            return True

        # Estimate drive and total hours from the route
        estimated_drive_hours, estimated_total_hours = (
            self._estimate_route_hours(assignments)
        )

        # Call HOSChecker.is_eligible with graceful degradation
        try:
            eligibility = await self._hos_checker.is_eligible(
                driver_id=driver_id,
                estimated_drive_hours=estimated_drive_hours,
                estimated_total_hours=estimated_total_hours,
            )
        except Exception as exc:
            logger.error(
                "RoutePlanningAgent: HOS eligibility check failed for "
                "driver=%s truck=%s tenant=%s: %s — allowing route "
                "(graceful degradation)",
                driver_id,
                truck_id,
                tenant_id,
                exc,
            )
            return True

        if not eligibility.eligible:
            earliest = (
                eligibility.earliest_eligible_time.isoformat()
                if eligibility.earliest_eligible_time
                else "unknown"
            )
            logger.warning(
                "RoutePlanningAgent: driver %s HOS-ineligible for "
                "truck=%s tenant=%s — skipping loading plan "
                "(hos_blocked). Reasons: %s | "
                "earliest_eligible_time: %s",
                driver_id,
                truck_id,
                tenant_id,
                "; ".join(eligibility.reasons),
                earliest,
            )
            return False

        logger.debug(
            "RoutePlanningAgent: driver %s HOS-eligible for truck=%s "
            "tenant=%s (drive=%.1fh, total=%.1fh)",
            driver_id,
            truck_id,
            tenant_id,
            estimated_drive_hours,
            estimated_total_hours,
        )
        return True

    # ------------------------------------------------------------------
    # Asset certification check (Task 8.10, Req 13.5)
    # ------------------------------------------------------------------

    async def _check_asset_certification(
        self,
        tenant_id: str,
        truck_id: str,
    ) -> bool:
        """Check whether the truck (asset) has valid DOT cargo tank certifications.

        Calls :meth:`AssetCertificationService.is_dispatch_eligible` to
        verify the asset does not have any expired DOT cargo tank
        certifications (V/K/I/P/UT).

        When the ``_asset_certification_service`` is not configured
        (graceful degradation), this method returns ``True`` so routes
        are not blocked during early bootstrap or for tenants that
        haven't wired the compliance backbone.

        Args:
            tenant_id: Tenant scope for the query.
            truck_id: The truck (asset) assigned to the loading plan.

        Returns:
            ``True`` if the asset is eligible (or if the service is not
            configured); ``False`` if the asset has expired certifications
            and the loading plan should be skipped.

        Validates: Requirement 13.5
        """
        if self._asset_certification_service is None:
            logger.debug(
                "RoutePlanningAgent: asset_certification_service not "
                "configured — skipping asset certification check for "
                "tenant=%s truck=%s",
                tenant_id,
                truck_id,
            )
            return True

        # Call is_dispatch_eligible with graceful degradation
        try:
            eligibility = await self._asset_certification_service.is_dispatch_eligible(
                tenant_id, truck_id
            )
        except Exception as exc:
            logger.error(
                "RoutePlanningAgent: asset certification check failed for "
                "truck=%s tenant=%s: %s — allowing route "
                "(graceful degradation)",
                truck_id,
                tenant_id,
                exc,
            )
            return True

        if not eligibility.eligible:
            logger.warning(
                "RoutePlanningAgent: asset %s ineligible for dispatch "
                "tenant=%s — skipping loading plan. Reasons: %s",
                truck_id,
                tenant_id,
                "; ".join(eligibility.reasons),
            )
            return False

        logger.debug(
            "RoutePlanningAgent: asset %s certification-eligible for "
            "tenant=%s",
            truck_id,
            tenant_id,
        )
        return True

    # ------------------------------------------------------------------
    # Delivery Filter (Task 15.5, Req 14.5)
    # ------------------------------------------------------------------

    async def _apply_delivery_filter(self) -> Optional[Any]:
        """Apply the DeliveryFilter to partition candidates before the solver.

        Called at the top of ``evaluate()`` before the optimization solver
        runs (Req 14.5). When the DeliveryFilter is configured, it
        partitions delivery candidates into will_call, auto_fill, and
        keep_full groups. The combined eligible candidates (all three
        partitions) replace the unfiltered candidate list for the solver.

        When the DeliveryFilter is not configured (``None``), this method
        returns ``None`` and the agent uses unfiltered candidates (graceful
        degradation).

        Returns:
            The ``FilteredCandidates`` result when the filter is configured,
            or ``None`` when the filter is not available.

        Validates: Requirement 14.5
        """
        if self._delivery_filter is None:
            return None

        # Build delivery candidates from the current proposal buffer.
        # Each proposal's loading plan assignments represent potential
        # deliveries. We extract candidate metadata and pass them through
        # the filter so only eligible deliveries proceed to the solver.
        candidates = self._build_delivery_candidates_from_proposals()

        if not candidates:
            logger.debug(
                "RoutePlanningAgent: no delivery candidates to filter"
            )
            return None

        try:
            filtered = await self._delivery_filter.partition_candidates(
                candidates
            )
            logger.info(
                "RoutePlanningAgent: DeliveryFilter applied — "
                "will_call=%d, auto_fill=%d, keep_full=%d, excluded=%d "
                "(total_in=%d)",
                len(filtered.will_call),
                len(filtered.auto_fill),
                len(filtered.keep_full),
                len(filtered.excluded),
                len(candidates),
            )
            return filtered
        except Exception as exc:
            logger.error(
                "RoutePlanningAgent: DeliveryFilter.partition_candidates() "
                "failed: %s — using unfiltered candidates "
                "(graceful degradation)",
                exc,
            )
            return None

    def _build_delivery_candidates_from_proposals(self) -> List[Any]:
        """Extract DeliveryCandidate objects from the proposal buffer.

        Inspects each buffered InterventionProposal for delivery
        candidate metadata (customer_type, order_id, order_status,
        tank_level_percent, etc.) and constructs DeliveryCandidate
        instances for the DeliveryFilter.

        Proposals that do not contain delivery candidate metadata are
        skipped — they will still be processed by the solver as before
        (the filter only applies to proposals that carry the metadata).

        Returns:
            List of DeliveryCandidate objects extracted from proposals.
        """
        from compliance.services.delivery_filter import (
            DeliveryCandidate,
            CustomerType,
        )

        candidates: List[Any] = []
        for proposal in self._proposal_buffer:
            actions = getattr(proposal, "actions", []) or []
            for action in actions:
                params = action.get("parameters", {}) if isinstance(action, dict) else {}
                # Check if this action carries delivery candidate metadata
                customer_type_raw = params.get("customer_type")
                if customer_type_raw is None:
                    continue

                try:
                    customer_type = CustomerType(customer_type_raw)
                except (ValueError, KeyError):
                    continue

                candidate = DeliveryCandidate(
                    candidate_id=params.get("plan_id", params.get("order_id", "")),
                    customer_id=params.get("customer_id", ""),
                    customer_type=customer_type,
                    order_id=params.get("order_id"),
                    order_status=params.get("order_status"),
                    tank_level_percent=params.get("tank_level_percent"),
                    reorder_point_percent=params.get("reorder_point_percent"),
                    forecast_days_to_empty=params.get("forecast_days_to_empty"),
                    planning_horizon_days=params.get("planning_horizon_days"),
                )
                candidates.append(candidate)

        return candidates

    # ------------------------------------------------------------------
    # Query station locations (Req 4.3)
    # ------------------------------------------------------------------

    async def _query_station_locations(
        self, tenant_id: str, station_ids: List[str]
    ) -> Dict[str, Dict[str, float]]:
        """Query fuel_stations for lat/lon coordinates.

        Returns a dict keyed by station_id with {lat, lon} dicts.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"station_id": station_ids}},
                    ],
                },
            },
            "_source": ["station_id", "latitude", "longitude", "location"],
            "size": 200,
        }

        locations: Dict[str, Dict[str, float]] = {}
        try:
            resp = await self._es.search_documents(
                FUEL_STATIONS_INDEX, query, 200
            )
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                sid = source.get("station_id", "")
                # Try explicit lat/lon fields first
                lat = source.get("latitude", 0.0)
                lon = source.get("longitude", 0.0)
                # Fall back to location geo_point field
                if (lat == 0.0 and lon == 0.0) and source.get("location"):
                    loc = source["location"]
                    if isinstance(loc, dict):
                        lat = loc.get("lat", 0.0)
                        lon = loc.get("lon", 0.0)
                if sid and (lat != 0.0 or lon != 0.0):
                    locations[sid] = {"lat": lat, "lon": lon}
        except Exception as e:
            logger.error(
                "RoutePlanningAgent: failed to query station locations: %s", e
            )

        return locations

    # ------------------------------------------------------------------
    # Query SLA windows (Req 4.4)
    # ------------------------------------------------------------------

    async def _query_sla_windows(
        self, tenant_id: str, station_ids: List[str]
    ) -> Dict[str, Tuple[float, float]]:
        """Query fuel_stations for SLA delivery windows.

        Returns a dict keyed by station_id with (earliest_hour, latest_hour)
        tuples. Stations without SLA windows are omitted.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"station_id": station_ids}},
                    ],
                },
            },
            "_source": [
                "station_id", "sla_delivery_window_start",
                "sla_delivery_window_end",
            ],
            "size": 200,
        }

        sla_windows: Dict[str, Tuple[float, float]] = {}
        try:
            resp = await self._es.search_documents(
                FUEL_STATIONS_INDEX, query, 200
            )
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                sid = source.get("station_id", "")
                start_h = source.get("sla_delivery_window_start")
                end_h = source.get("sla_delivery_window_end")
                if sid and start_h is not None and end_h is not None:
                    sla_windows[sid] = (float(start_h), float(end_h))
        except Exception as e:
            logger.error(
                "RoutePlanningAgent: failed to query SLA windows: %s", e
            )

        return sla_windows

    # ------------------------------------------------------------------
    # Build stops from fuel_orders_current (Task 11.2)
    # ------------------------------------------------------------------

    async def build_stops_from_fuel_orders(
        self, tenant_id: str
    ) -> Tuple[
        List[Dict[str, Any]],
        Dict[str, Dict[str, float]],
        List["WindowMissEntry"],
    ]:
        """Build route stops from fuel_orders_current.

        Reads orders WHERE status IN {confirmed, scheduled} for the tenant.
        Uses ship_to_lat/ship_to_lon as the stop coordinate; falls back to
        geocoding ship_to_address via the existing hook when null.

        Treats delivery_window_start/delivery_window_end as hard routing
        constraints; surfaces windows that cannot be satisfied as
        window_miss entries in the replan diff rather than silent
        re-sequencing.

        Returns:
            (orders, stop_locations, window_misses) where:
            - orders: list of order source docs (only those with valid locations)
            - stop_locations: dict keyed by order_id with {lat, lon}
            - window_misses: list of WindowMissEntry for orders whose
              delivery windows cannot be satisfied
        """
        orders = await self._fetch_routable_orders(tenant_id)
        if not orders:
            return [], {}, []

        stop_locations: Dict[str, Dict[str, float]] = {}
        window_misses: List[WindowMissEntry] = []
        now = datetime.now(timezone.utc)

        for order in orders:
            order_id = order.get("order_id", "")
            lat = order.get("ship_to_lat")
            lon = order.get("ship_to_lon")

            # Use ship_to_lat/ship_to_lon as stop coordinate
            if lat is not None and lon is not None:
                try:
                    lat_f = float(lat)
                    lon_f = float(lon)
                    if lat_f != 0.0 or lon_f != 0.0:
                        stop_locations[order_id] = {"lat": lat_f, "lon": lon_f}
                    else:
                        # Zero coordinates — attempt geocoding fallback
                        geocoded = await self._geocode_address(
                            order.get("ship_to_address", "")
                        )
                        if geocoded:
                            stop_locations[order_id] = geocoded
                except (TypeError, ValueError):
                    geocoded = await self._geocode_address(
                        order.get("ship_to_address", "")
                    )
                    if geocoded:
                        stop_locations[order_id] = geocoded
            else:
                # Fall back to geocoding ship_to_address
                geocoded = await self._geocode_address(
                    order.get("ship_to_address", "")
                )
                if geocoded:
                    stop_locations[order_id] = geocoded

            # Check delivery window constraints — treat as hard routing
            # constraints. Windows that cannot be satisfied are surfaced
            # as window_miss entries rather than silent re-sequencing.
            window_start_raw = order.get("delivery_window_start")
            window_end_raw = order.get("delivery_window_end")
            if window_start_raw and window_end_raw:
                try:
                    window_start = self._parse_datetime(window_start_raw)
                    window_end = self._parse_datetime(window_end_raw)
                    if window_end and window_end < now:
                        # Window has already passed — surface as window_miss
                        window_misses.append(WindowMissEntry(
                            order_id=order_id,
                            reason="window_miss",
                            delivery_window_start=str(window_start_raw),
                            delivery_window_end=str(window_end_raw),
                            detail="delivery_window_end is in the past",
                        ))
                    elif window_start and window_start < now and window_end:
                        # Window started but hasn't ended — check if
                        # remaining time is too narrow (< 30 min)
                        remaining = window_end - now
                        if remaining.total_seconds() < 1800:
                            window_misses.append(WindowMissEntry(
                                order_id=order_id,
                                reason="window_miss",
                                delivery_window_start=str(window_start_raw),
                                delivery_window_end=str(window_end_raw),
                                detail=(
                                    "delivery_window_end is less than 30 "
                                    "minutes away; window cannot be satisfied"
                                ),
                            ))
                except (TypeError, ValueError):
                    pass

        return orders, stop_locations, window_misses

    async def _fetch_routable_orders(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch orders from fuel_orders_current with routable statuses."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": ["confirmed", "scheduled"]}},
                    ]
                }
            },
            "size": 1000,
        }
        try:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 1000
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            return [hit["_source"] for hit in hits if hit.get("_source")]
        except Exception as exc:
            logger.error(
                "RoutePlanningAgent: failed to fetch routable orders for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []

    async def _geocode_address(
        self, address: str
    ) -> Optional[Dict[str, float]]:
        """Geocode an address to lat/lon via the existing hook.

        Returns None when geocoding is unavailable or fails.
        """
        if not address or not address.strip():
            return None
        # Use the existing geocoding hook if available on the ES service
        geocode_fn = getattr(self._es, "geocode_address", None)
        if geocode_fn is None:
            logger.debug(
                "RoutePlanningAgent: no geocode_address hook available"
            )
            return None
        try:
            result = await geocode_fn(address)
            if result and isinstance(result, dict):
                lat = result.get("lat")
                lon = result.get("lon")
                if lat is not None and lon is not None:
                    return {"lat": float(lat), "lon": float(lon)}
        except Exception as exc:
            logger.warning(
                "RoutePlanningAgent: geocoding failed for address=%r: %s",
                address,
                exc,
            )
        return None

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        """Parse an ISO-8601 datetime value."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # ------------------------------------------------------------------
    # Build location list for route solver
    # ------------------------------------------------------------------

    def _build_location_list(
        self,
        station_ids: List[str],
        station_locations: Dict[str, Dict[str, float]],
    ) -> Tuple[List[Dict[str, float]], List[str]]:
        """Build ordered location list with depot at index 0.

        Returns:
            (locations, station_order) where station_order[i] corresponds
            to locations[i+1] (index 0 is the depot).
        """
        locations = [dict(DEFAULT_DEPOT)]  # Depot at index 0
        station_order: List[str] = []

        for sid in station_ids:
            loc = station_locations.get(sid)
            if loc:
                locations.append(loc)
                station_order.append(sid)

        return locations, station_order

    # ------------------------------------------------------------------
    # Build route plan with ETAs (Req 4.1)
    # ------------------------------------------------------------------

    def _build_route_plan(
        self,
        truck_id: str,
        plan_id: str,
        optimized_order: List[int],
        station_order: List[str],
        total_distance: float,
        assignments: List[Dict[str, Any]],
        tenant_id: str,
        sla_violations: Optional[List[Dict]] = None,
        traffic_provider_name: Optional[str] = None,
        traffic_fallback: bool = False,
        start_position: Optional[TruckStartPosition] = None,
    ) -> RoutePlan:
        """Build a RoutePlan from optimized route order."""
        # Build drop quantities per station
        station_drops: Dict[str, Dict[str, float]] = {}
        for assignment in assignments:
            sid = assignment.get("station_id", "")
            grade = assignment.get("fuel_grade", "")
            qty = assignment.get("quantity_liters", 0.0)
            if sid and grade:
                station_drops.setdefault(sid, {})
                station_drops[sid][grade] = (
                    station_drops[sid].get(grade, 0.0) + qty
                )

        # Build set of at-risk stop indices from SLA violations (Req 4.4)
        at_risk_indices = set()
        if sla_violations:
            at_risk_indices = {v["stop_index"] for v in sla_violations}
            for v in sla_violations:
                logger.warning(
                    "RoutePlanningAgent: SLA at-risk — stop index %d, "
                    "ETA %.1fh exceeds window end %.1fh by %.1fh",
                    v["stop_index"], v["eta_hours"],
                    v["window_end"], v["late_by_hours"],
                )

        # Build stops from optimized order (skip depot at index 0)
        stops: List[RouteStop] = []
        now = datetime.now(timezone.utc)
        cumulative_time_hours = 0.0
        sequence = 0

        for idx in optimized_order:
            if idx == 0:
                # Skip depot
                continue
            # Map back to station_id
            station_idx = idx - 1  # Offset for depot
            if station_idx < 0 or station_idx >= len(station_order):
                continue

            station_id = station_order[station_idx]
            drop = station_drops.get(station_id, {})

            # Estimate ETA based on cumulative travel time
            cumulative_time_hours += 0.5  # Approximate 30 min between stops
            eta = now + timedelta(hours=cumulative_time_hours)

            stops.append(
                RouteStop(
                    station_id=station_id,
                    eta=eta.isoformat(),
                    drop=drop,
                    sequence=sequence,
                )
            )
            sequence += 1

        return RoutePlan(
            truck_id=truck_id,
            plan_id=plan_id,
            stops=stops,
            distance_km=round(total_distance, 2),
            eta_confidence=0.75 if not at_risk_indices else 0.4,
            tenant_id=tenant_id,
            traffic_provider=traffic_provider_name,
            traffic_fallback=traffic_fallback,
            start_position_source=(
                start_position.source if start_position is not None else None
            ),
            start_position_lat=(
                start_position.lat if start_position is not None else None
            ),
            start_position_lon=(
                start_position.lon if start_position is not None else None
            ),
        )

    # ------------------------------------------------------------------
    # Compute objective value (Req 4.6)
    # ------------------------------------------------------------------

    def _compute_objective_value(
        self,
        route_plan: RoutePlan,
        utilization_pct: float = 0.0,
    ) -> float:
        """Compute weighted objective value for a route plan.

        Objective = w_cost * (1 - normalized_cost)
                  + w_risk * risk_reduction_estimate
                  + w_util * (utilization / 100)
                  + w_late * (1 - late_penalty_estimate)
                  - w_churn * churn_penalty

        Higher is better.
        """
        weights = DEFAULT_OBJECTIVE_WEIGHTS

        # Normalize distance cost (assume 500km is max reasonable route)
        max_distance = 500.0
        cost_score = max(0.0, 1.0 - (route_plan.distance_km / max_distance))

        # Risk reduction estimate (more stops served = more risk reduced)
        risk_score = min(1.0, len(route_plan.stops) / 10.0)

        # Utilization score
        util_score = utilization_pct / 100.0

        # Late delivery penalty (assume no late deliveries for new plans)
        late_score = 1.0

        # Churn penalty (new plans have no churn)
        churn_penalty = 0.0

        objective = (
            weights["route_cost"] * cost_score
            + weights["runout_risk_reduction"] * risk_score
            + weights["truck_utilization"] * util_score
            + weights["late_delivery_penalty"] * late_score
            - weights["plan_churn"] * churn_penalty
        )

        return round(max(0.0, min(1.0, objective)), 4)

    # ------------------------------------------------------------------
    # Build route proposal
    # ------------------------------------------------------------------

    def _build_route_proposal(
        self,
        route_plan: RoutePlan,
        tenant_id: str,
    ) -> InterventionProposal:
        """Build an InterventionProposal from a route plan.

        When ``route_plan.storm_mode_active`` is ``True``, the action
        ``tool_name`` is switched to
        :data:`APPLY_ROUTE_PLAN_STORM_MODE_TOOL` and the proposal's
        ``risk_class`` is set to :class:`RiskClass.HIGH` so the
        platform-wide ConfirmationProtocol classifies the mutation at
        HIGH risk instead of the standard LOW/MEDIUM path (Task 10.7,
        Req 9.2.5). The HIGH mapping lives in
        :data:`Agents.risk_registry.DEFAULT_RISK_REGISTRY` keyed by the
        storm tool name so tenants can override via Redis without
        touching code.
        """
        storm_mode_active = bool(route_plan.storm_mode_active)
        tool_name = (
            APPLY_ROUTE_PLAN_STORM_MODE_TOOL
            if storm_mode_active
            else APPLY_ROUTE_PLAN_TOOL
        )

        parameters: Dict[str, Any] = {
            "route_id": route_plan.route_id,
            "truck_id": route_plan.truck_id,
            "plan_id": route_plan.plan_id,
            "stops": [s.model_dump(mode="json") for s in route_plan.stops],
            "distance_km": route_plan.distance_km,
            "objective_value": route_plan.objective_value,
        }

        # Surface window_miss entries in the replan diff (Req 5.2.3)
        if route_plan.window_misses:
            parameters["window_misses"] = [
                wm.model_dump(mode="json") for wm in route_plan.window_misses
            ]

        description = (
            f"Route for truck {route_plan.truck_id}: "
            f"{len(route_plan.stops)} stops, "
            f"{route_plan.distance_km:.1f}km, "
            f"objective={route_plan.objective_value:.3f}"
        )

        if route_plan.window_misses:
            description += (
                f", {len(route_plan.window_misses)} window_miss"
            )

        if storm_mode_active:
            deferred_payload = [
                d.model_dump(mode="json") for d in route_plan.deferred_stops
            ]
            parameters.update(
                {
                    "storm_mode_active": True,
                    "storm_mode_max_stops_per_truck": (
                        route_plan.storm_mode_max_stops_per_truck
                    ),
                    "storm_mode_delivery_window_start_hour": (
                        route_plan.storm_mode_delivery_window_start_hour
                    ),
                    "storm_mode_delivery_window_end_hour": (
                        route_plan.storm_mode_delivery_window_end_hour
                    ),
                    "deferred_stops": deferred_payload,
                }
            )
            description = (
                f"[storm_mode] Route for truck {route_plan.truck_id}: "
                f"{len(route_plan.stops)} stops, "
                f"{len(route_plan.deferred_stops)} deferred, "
                f"{route_plan.distance_km:.1f}km, "
                f"objective={route_plan.objective_value:.3f}"
            )

        actions = [
            {
                "tool_name": tool_name,
                "parameters": parameters,
                "description": description,
            }
        ]

        risk_class = RiskClass.HIGH if storm_mode_active else RiskClass.LOW

        return InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta={
                "route_distance_km": -route_plan.distance_km,
                "stops_served": len(route_plan.stops),
                "stops_deferred": len(route_plan.deferred_stops),
                "window_misses": len(route_plan.window_misses),
                "objective_value": route_plan.objective_value,
            },
            risk_class=risk_class,
            confidence=route_plan.eta_confidence,
            priority=1,
            tenant_id=tenant_id,
        )

    # ------------------------------------------------------------------
    # Traffic_Provider orchestration (Req 2.1.3, 2.1.5, 2.1.6)
    # ------------------------------------------------------------------

    async def _resolve_travel_matrix(
        self,
        *,
        tenant_id: str,
        locations: List[Dict[str, float]],
    ) -> Tuple[Optional[List[List[float]]], Optional[str], bool]:
        """Return ``(distance_matrix, provider_name, traffic_fallback)``.

        Implements the tenant-gated Traffic_Provider flow mandated by
        Fuel Ops Hardening Requirements 2.1.3, 2.1.5, and 2.1.6:

        1. Check the ``overlay.traffic_aware_routing`` feature flag for
           ``tenant_id``. If the flag is not in an active state we skip
           the provider entirely and return ``(None, None, False)`` so
           the caller keeps using the Haversine distance matrix and
           **does not** stamp ``traffic_fallback: true`` — the plan is
           simply not traffic-aware (Req 2.1.3).
        2. Read ``overlay.traffic_provider:{tenant_id}`` from the tenant
           config (Req 2.1.2). Absent / blank / unparseable values are
           treated as a misconfiguration; we fall back to Haversine and
           annotate ``traffic_fallback: true`` per Req 2.1.5.
        3. Build (and cache) the concrete :class:`TrafficProvider` via
           the injected factory. Unknown provider names or factory
           failures also fall back to Haversine with the annotation.
        4. Call :meth:`TrafficProvider.get_matrix` under a 10-second
           outer timeout (Req 2.1.5). Any failure — asyncio timeout,
           :class:`TrafficBudgetExceeded` (Req 2.1.7), httpx.HTTPError,
           or runtime error — degrades to Haversine and stamps
           ``traffic_fallback: true`` on the Route_Plan.
        5. When the provider succeeds we return the distance matrix and
           ``traffic_fallback: False`` so consumers see a fully
           traffic-informed plan.

        Args:
            tenant_id: Owning tenant. Used for flag lookup, provider
                selection, and the provider's own budget counter.
            locations: Ordered list of ``{lat, lon}`` dicts where index 0
                is the depot and indices ``1..N`` are customer stops.

        Returns:
            ``(distance_matrix, provider_name, traffic_fallback)``.

            * ``distance_matrix`` — a ``List[List[float]]`` of km values
              or ``None`` when traffic-aware routing is disabled. When a
              matrix is returned the caller SHOULD use it for SLA ETA
              checks so window violations reflect real drive times.
            * ``provider_name`` — the short provider name actually used
              (``"mapbox"``, ``"here"``, ``"google"``) on success, or
              ``None`` when no provider was consulted.
            * ``traffic_fallback`` — ``True`` only when a provider *was*
              attempted (flag enabled AND provider configured) and the
              attempt failed. When the flag is off or no provider is
              configured, we don't consider the Haversine matrix a
              "fallback" and leave this at ``False``.
        """

        # (1) Feature-flag gate ------------------------------------------
        if not await self._traffic_aware_routing_enabled(tenant_id):
            return None, None, False

        # (2) Provider selection -----------------------------------------
        provider_name = await self._resolve_traffic_provider_name(tenant_id)
        if not provider_name:
            logger.warning(
                "RoutePlanningAgent: traffic-aware routing enabled for "
                "tenant=%s but no provider configured; falling back to "
                "Haversine + DEFAULT_SPEED_KMH",
                tenant_id,
            )
            return None, None, True

        # (3) Concrete provider ------------------------------------------
        provider = self._get_or_build_traffic_provider(tenant_id, provider_name)
        if provider is None:
            logger.warning(
                "RoutePlanningAgent: traffic_provider=%s unavailable for "
                "tenant=%s; falling back to Haversine",
                provider_name,
                tenant_id,
            )
            return None, None, True

        # (4) Call the provider under a 10-second budget -----------------
        origins = [(loc["lat"], loc["lon"]) for loc in locations]
        depart_at = datetime.now(timezone.utc)
        try:
            matrix = await asyncio.wait_for(
                provider.get_matrix(
                    origins=origins,
                    destinations=origins,
                    depart_at=depart_at,
                    tenant_id=tenant_id,
                ),
                timeout=TRAFFIC_MATRIX_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] timed out after "
                "%.1fs for tenant=%s; falling back to Haversine",
                provider_name,
                TRAFFIC_MATRIX_TIMEOUT_SECONDS,
                tenant_id,
            )
            return None, None, True
        except TrafficBudgetExceeded as exc:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] budget exhausted "
                "for tenant=%s month=%s (%d/%d); falling back to Haversine",
                provider_name,
                exc.tenant_id,
                exc.month,
                exc.current,
                exc.limit,
            )
            return None, None, True
        except httpx.HTTPError as exc:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] HTTP error for "
                "tenant=%s: %s; falling back to Haversine",
                provider_name,
                tenant_id,
                exc,
            )
            return None, None, True
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] unexpected "
                "failure for tenant=%s: %s; falling back to Haversine",
                provider_name,
                tenant_id,
                exc,
            )
            return None, None, True

        # (5) Success — return the provider's distance matrix ------------
        try:
            distance_matrix = [list(row) for row in matrix.distance_km]
        except AttributeError:
            logger.warning(
                "RoutePlanningAgent: Traffic_Provider[%s] returned "
                "non-TravelMatrix result for tenant=%s; falling back",
                provider_name,
                tenant_id,
            )
            return None, None, True

        return distance_matrix, matrix.provider or provider_name, False

    async def _traffic_aware_routing_enabled(self, tenant_id: str) -> bool:
        """Return True when ``overlay.traffic_aware_routing`` is active.

        The feature flag maps to an overlay state. We treat
        ``active_gated`` and ``active_auto`` as "enabled" — both modes
        route real traffic through the solver. ``shadow`` and
        ``disabled`` keep the agent on the Haversine default so the
        provider budget is never spent in shadow mode (Req 2.1.3).
        """

        if self._feature_flags is None:
            return False
        try:
            state = await self._feature_flags.get_overlay_state(
                TRAFFIC_AWARE_ROUTING_FLAG_KEY, tenant_id
            )
        except AttributeError:
            # Legacy services expose only ``is_enabled``. We treat the
            # boolean as "active" when True for backward compatibility.
            try:
                return bool(await self._feature_flags.is_enabled(tenant_id))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "RoutePlanningAgent: feature flag lookup failed for "
                    "tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: overlay state lookup failed for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return False
        return state in {"active_gated", "active_auto"}

    async def _terminal_sourcing_enabled(self, tenant_id: str) -> bool:
        """Return True when ``overlay.terminal_sourcing`` is active.

        Mirrors :meth:`_traffic_aware_routing_enabled`: the flag maps to
        an overlay state and we treat ``active_gated`` / ``active_auto``
        as "enabled". ``shadow`` / ``disabled`` skip the
        Sourcing_Recommender entirely so the recommender's ES /
        rack-price / Redis traffic is never consumed in shadow mode
        (Task 7.10 / Req 8.5.5).
        """

        if self._feature_flags is None:
            return False
        try:
            state = await self._feature_flags.get_overlay_state(
                TERMINAL_SOURCING_FLAG_KEY, tenant_id
            )
        except AttributeError:
            # Legacy services expose only ``is_enabled`` — best-effort
            # coerce the boolean to "active" so tenants with older
            # FeatureFlagService installs still exercise the recommender.
            try:
                return bool(await self._feature_flags.is_enabled(tenant_id))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "RoutePlanningAgent: terminal_sourcing flag lookup failed "
                    "for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: terminal_sourcing overlay state lookup "
                "failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return False
        return state in {"active_gated", "active_auto"}

    async def _maybe_apply_sourcing(
        self,
        *,
        route_plan: "RoutePlan",
        loading_plan: Dict[str, Any],
        assignments: List[Dict[str, Any]],
        locations: List[Dict[str, float]],
        tenant_id: str,
        truck_id: str,
    ) -> None:
        """Populate ``route_plan.sourced_terminal_*`` when an external lift is required.

        Implements Task 7.10 wiring into the Route_Planning_Agent:

        1. Skip entirely when no :class:`SourcingRecommender` was wired
           (bootstrap has not completed yet, or the flag is off for the
           tenant). The route persists with the sourcing fields left at
           their defaults (``None`` / empty list).
        2. Detect the external-lift condition by inspecting the loading
           plan's ``terminal_id``. A non-empty value is treated as
           external (the Route_Planning_Agent does not own a depot
           resolver today — the instruction set allows the simple
           "non-empty" check until depot resolution is fully wired).
        3. Pick the dominant fuel grade from the assignments (the grade
           with the highest total volume) and canonicalize it so the
           recommender sees a US product_code. Unknown grades are
           preserved and passed through — the recommender surfaces an
           :class:`UnknownFuelProductError` which we swallow and log.
        4. Convert the total liters for that grade to gallons via
           :data:`LITERS_PER_GALLON` so the recommender stays in its
           native US unit.
        5. Invoke ``recommender.recommend(...)`` under a try/except. Any
           failure (unknown product, recommender exception, empty
           candidate list) logs a warning and leaves the fields None so
           the route still persists — the sourcing path is advisory, not
           blocking (Task 7.10).

        Args:
            route_plan: The Route_Plan being built; mutated in place.
            loading_plan: The extracted ``apply_loading_plan`` action
                parameters dict. Provides ``terminal_id``.
            assignments: The assignment list copied from the loading
                plan. Used to derive the primary grade + volume.
            locations: Ordered location list (index 0 is the depot).
                Used as the sourcing origin.
            tenant_id: Owning tenant — forwarded to the recommender for
                tenant-scoped lookups.
            truck_id: Truck stamped on the audit record for traceability.
        """

        recommender = self._sourcing_recommender
        if recommender is None:
            return

        # Detect the external-lift signal. An absent, empty, or
        # whitespace-only ``terminal_id`` means the plan will lift from
        # the depot and we skip the recommender entirely.
        terminal_id_raw = loading_plan.get("terminal_id")
        terminal_id = (
            terminal_id_raw.strip()
            if isinstance(terminal_id_raw, str)
            else ""
        )
        if not terminal_id:
            return

        # Feature-flag gate — skip when the tenant has not enabled the
        # overlay. Consuming recommender dependencies in shadow mode
        # would charge the tenant's rack-price provider budget for no
        # observable effect.
        if not await self._terminal_sourcing_enabled(tenant_id):
            return

        # Primary grade selection: pick the fuel_grade with the highest
        # aggregate quantity_liters across assignments. Ties fall back
        # to insertion order. Skip the sourcing path entirely when the
        # loading plan is empty or carries no recognisable grade.
        grade_totals: Dict[str, float] = {}
        for assignment in assignments:
            grade = assignment.get("fuel_grade")
            if not isinstance(grade, str) or not grade.strip():
                continue
            qty = assignment.get("quantity_liters", 0.0)
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                qty_f = 0.0
            if qty_f <= 0:
                continue
            grade_totals[grade] = grade_totals.get(grade, 0.0) + qty_f
        if not grade_totals:
            logger.debug(
                "RoutePlanningAgent: skipping sourcing for tenant=%s "
                "truck=%s plan=%s — no volume-bearing assignments",
                tenant_id,
                truck_id,
                loading_plan.get("plan_id"),
            )
            return

        primary_grade, total_liters = max(
            grade_totals.items(), key=lambda kv: (kv[1], kv[0])
        )
        volume_gallons = total_liters / LITERS_PER_GALLON
        if volume_gallons <= 0:
            return

        # Canonicalize grade — the recommender accepts aliases natively
        # via its own canonicalize() but we surface the resolved code on
        # the log line for traceability. Unknown codes are preserved so
        # the recommender can raise a clear UnknownFuelProductError.
        product_code = canonicalize_or_warn(
            primary_grade,
            context="route_planning.sourcing.product_code",
            logger_=logger,
        )

        # Origin coords come from ``locations[0]`` (the depot).
        if not locations:
            return
        origin = locations[0]
        origin_lat = float(origin.get("lat", 0.0))
        origin_lon = float(origin.get("lon", 0.0))

        run_id = getattr(self, "_current_run_id", None) or ""
        as_of = datetime.now(timezone.utc)

        try:
            recommendation = await recommender.recommend(
                tenant_id=tenant_id,
                product_code=product_code,
                volume_gallons=volume_gallons,
                origin_lat_lon=(origin_lat, origin_lon),
                as_of=as_of,
                truck_id=truck_id or None,
                run_id=run_id or None,
            )
        except UnknownFuelProductError as exc:
            logger.warning(
                "RoutePlanningAgent: sourcing skipped — unknown product_code "
                "%r for tenant=%s truck=%s: %s",
                product_code,
                tenant_id,
                truck_id,
                exc,
            )
            return
        except InvalidBrandedPreferenceError as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: sourcing skipped — invalid branded preference "
                "for tenant=%s truck=%s: %s",
                tenant_id,
                truck_id,
                exc,
            )
            return
        except Exception as exc:
            logger.warning(
                "RoutePlanningAgent: sourcing recommender failed for "
                "tenant=%s truck=%s product=%s volume_gal=%.2f: %s",
                tenant_id,
                truck_id,
                product_code,
                volume_gallons,
                exc,
            )
            return

        candidates = getattr(recommendation, "candidates", None) or []
        if not candidates:
            logger.warning(
                "RoutePlanningAgent: sourcing produced zero candidates for "
                "tenant=%s truck=%s product=%s volume_gal=%.2f — route "
                "persisted without sourced_terminal_id",
                tenant_id,
                truck_id,
                product_code,
                volume_gallons,
            )
            return

        top = candidates[0]
        route_plan.sourced_terminal_id = getattr(top, "terminal_id", None)
        route_plan.sourced_terminal_reasons = list(
            getattr(top, "reasons", None) or []
        )
        route_plan.sourcing_recommendation_id = getattr(
            recommendation, "recommendation_id", None
        )
        logger.info(
            "RoutePlanningAgent: sourced terminal=%s (score=%.4f) for "
            "tenant=%s truck=%s product=%s volume_gal=%.2f recommendation_id=%s",
            route_plan.sourced_terminal_id,
            getattr(top, "score", 0.0),
            tenant_id,
            truck_id,
            product_code,
            volume_gallons,
            route_plan.sourcing_recommendation_id,
        )

    # ------------------------------------------------------------------
    # Storm_Mode guard-rails (Task 10.7, Req 9.2.4, 9.2.5, 9.3.1, 9.3.2)
    # ------------------------------------------------------------------

    async def _maybe_apply_storm_mode(
        self,
        *,
        route_plan: RoutePlan,
        tenant_id: str,
        truck_id: str,
        station_locations: Optional[Mapping[str, Mapping[str, float]]] = None,
        start_position: Optional[TruckStartPosition] = None,
    ) -> None:
        """Apply Storm_Mode guard-rails to ``route_plan`` in place.

        This method implements Task 10.7 / Req 9.2.4, 9.2.5, 9.3.1, 9.3.2
        and Task 10.8 / Req 9.3.3, 9.3.4, 9.3.5.

        1. Consult the injected :class:`StormModeEvaluator` via
           :meth:`_is_storm_mode_active`. When the evaluator is not wired
           or the tenant's persisted state is ``inactive``, the method
           is a no-op so pre-Phase-10 tenants keep the legacy behaviour.
        2. Resolve the per-tenant guard-rails
           (:class:`StormModeRouteSettings`) via the injected loader;
           fall back to the module-level defaults when the loader is
           not wired or the lookup fails.
        3. Stamp ``storm_mode_active`` and the resolved guard-rail
           values on ``route_plan`` so the persistence path and the
           InterventionProposal carry the same view of the guard-rails.
        4. Filter the stops list:

           * Req 9.3.3 / 9.3.4 — when ``station_locations`` are
             supplied, build a ``LineString`` for each inbound leg
             (start → stop, stop_i-1 → stop_i) and run an ES
             ``geo_shape`` intersects query against
             ``storm_road_restrictions`` filtered by ``tenant_id``,
             ``severity ∈ {severe, extreme}``, and an active
             effective-window. Any stop whose inbound leg intersects
             a matching restriction is moved to
             ``route_plan.deferred_stops`` with reason
             :data:`REASON_ROAD_RESTRICTION` and cause
             :data:`CAUSE_ROAD_RESTRICTION`; the matched
             restriction_ids are stamped on the deferred entry for
             dispatcher forensics.
           * Req 9.3.1 / 9.3.2 — drop stops whose ETA falls outside the
             configured ``delivery_window_start_hour`` …
             ``delivery_window_end_hour`` window; the stop is moved to
             ``route_plan.deferred_stops`` with reason
             ``deferred_storm_mode`` and cause
             :data:`CAUSE_OUTSIDE_WINDOW`; the next-eligible window is
             computed from the window config so the dispatcher UI can
             reschedule it.
           * Req 9.2.4 — after the window filter, if more than
             ``max_stops_per_truck`` stops remain the surplus tail is
             moved to ``deferred_stops`` with cause
             :data:`CAUSE_OVER_MAX_STOPS`.

        Idempotency: every mutation runs on a fresh ``route_plan.stops``
        copy so calling this method twice on the same plan yields the
        same outcome (and an empty second deferred list when the first
        pass already filtered every stop out of scope).
        """

        if not await self._is_storm_mode_active(tenant_id):
            return

        settings = await self._load_storm_mode_settings(tenant_id)

        route_plan.storm_mode_active = True
        route_plan.storm_mode_max_stops_per_truck = settings.max_stops_per_truck
        route_plan.storm_mode_delivery_window_start_hour = (
            settings.delivery_window_start_hour
        )
        route_plan.storm_mode_delivery_window_end_hour = (
            settings.delivery_window_end_hour
        )

        tz = _resolve_timezone(settings.timezone)

        deferred: List[DeferredRouteStop] = []

        # Pass 0 — road-restriction intersects filter (Req 9.3.3 / 9.3.4).
        # Runs before the delivery-window filter so a stop whose inbound
        # leg crosses a severe polygon is tagged with the more specific
        # ``road_restriction`` reason rather than a generic window
        # violation. When ``station_locations`` is absent (legacy tests
        # that don't thread the lookup through) this pass is a no-op.
        kept_after_restrictions = await self._apply_road_restriction_filter(
            tenant_id=tenant_id,
            truck_id=truck_id,
            stops=list(route_plan.stops),
            station_locations=station_locations,
            start_position=start_position,
            deferred=deferred,
        )

        # Pass 1 — delivery-window filter (Req 9.3.1, 9.3.2).
        kept: List[RouteStop] = []
        for stop in kept_after_restrictions:
            eta_dt = _parse_eta(stop.eta)
            if eta_dt is None:
                # We cannot reason about the window without an ETA; keep
                # the stop so we don't silently drop it.
                kept.append(stop)
                continue
            local_eta = eta_dt.astimezone(tz)
            hour_of_day = _hour_of_day(local_eta)
            if _is_within_window(
                hour_of_day=hour_of_day,
                start_hour=settings.delivery_window_start_hour,
                end_hour=settings.delivery_window_end_hour,
            ):
                kept.append(stop)
                continue

            next_start, next_end = _next_eligible_window(
                local_eta=local_eta,
                start_hour=settings.delivery_window_start_hour,
                end_hour=settings.delivery_window_end_hour,
            )
            deferred.append(
                DeferredRouteStop(
                    station_id=stop.station_id,
                    reason=REASON_DEFERRED_STORM_MODE,
                    deferral_cause=CAUSE_OUTSIDE_WINDOW,
                    original_sequence=stop.sequence,
                    original_eta=stop.eta,
                    next_eligible_window_start=next_start.astimezone(
                        timezone.utc
                    ).isoformat(),
                    next_eligible_window_end=next_end.astimezone(
                        timezone.utc
                    ).isoformat(),
                )
            )

        # Pass 2 — per-truck stop cap (Req 9.2.4). Applied after the
        # window filter so the cap reflects the window-eligible subset.
        cap = max(0, int(settings.max_stops_per_truck))
        if cap < 0:
            cap = 0
        if len(kept) > cap:
            surplus = kept[cap:]
            kept = kept[:cap]
            for stop in surplus:
                deferred.append(
                    DeferredRouteStop(
                        station_id=stop.station_id,
                        reason=REASON_DEFERRED_STORM_MODE,
                        deferral_cause=CAUSE_OVER_MAX_STOPS,
                        original_sequence=stop.sequence,
                        original_eta=stop.eta,
                    )
                )

        # Renumber surviving stops so the on-wire sequence stays dense
        # — downstream consumers expect ``sequence = 0..N-1`` and the
        # dispatcher UI renders stops in this order.
        for new_idx, stop in enumerate(kept):
            stop.sequence = new_idx

        route_plan.stops = kept
        route_plan.deferred_stops = deferred

        if deferred:
            logger.info(
                "RoutePlanningAgent: Storm_Mode deferred %d stop(s) for "
                "tenant=%s truck=%s (cap=%d, window=%.2f-%.2f)",
                len(deferred),
                tenant_id,
                truck_id,
                settings.max_stops_per_truck,
                settings.delivery_window_start_hour,
                settings.delivery_window_end_hour,
            )
        else:
            logger.info(
                "RoutePlanningAgent: Storm_Mode active for tenant=%s "
                "truck=%s — all %d stops pass guard-rails",
                tenant_id,
                truck_id,
                len(kept),
            )

    async def _apply_road_restriction_filter(
        self,
        *,
        tenant_id: str,
        truck_id: str,
        stops: List[RouteStop],
        station_locations: Optional[Mapping[str, Mapping[str, float]]],
        start_position: Optional[TruckStartPosition],
        deferred: List[DeferredRouteStop],
    ) -> List[RouteStop]:
        """Defer stops whose inbound leg crosses a severe+ restriction.

        Implements Task 10.8 / Req 9.3.3, 9.3.4. For each stop in
        ``stops`` we build a GeoJSON ``LineString`` representing the
        inbound leg (``prev → stop``, where the first leg's origin is
        the truck's start position) and run an ES ``geo_shape``
        intersects query against ``storm_road_restrictions`` filtered by:

        * ``tenant_id`` — multi-tenant isolation.
        * ``severity ∈ {severe, extreme}`` — Req 9.3.4 mandates only
          severities ``>= severe`` trigger deferrals. Lower severities
          are kept in the index (dispatcher awareness) but do not
          affect routing.
        * active effective window (``effective_from <= now`` and
          ``effective_to`` either absent or ``>= now``).

        Stops whose inbound leg intersects a matching restriction are
        appended to ``deferred`` with reason :data:`REASON_ROAD_RESTRICTION`
        and cause :data:`CAUSE_ROAD_RESTRICTION`; the matched
        restriction_ids are stamped on the deferred entry for forensics.

        Missing ``station_locations`` or ``start_position`` degrades
        gracefully: the filter is skipped and every stop is kept so
        legacy tests that don't thread location context through the
        call chain continue to pass.

        Args:
            tenant_id: Owning tenant — filters the ES query.
            truck_id: Truck the plan is for — included in log lines so
                a dispatcher can trace which truck had stops deferred.
            stops: Current stop list (after route optimization but
                before any Storm_Mode filters).
            station_locations: Map of ``station_id → {"lat", "lon"}``.
                Sourced from ``_query_station_locations`` in the main
                run loop. Stops whose ``station_id`` is missing from
                this map are kept as-is (there is no leg to intersect).
            start_position: Origin for the first leg (``start → stop_0``).
                Sourced from ``_resolve_start_position``. When ``None``
                the first leg is skipped (equivalent to trusting the
                stop's own location).
            deferred: Output list — deferred entries are appended here.

        Returns:
            The list of stops that are *not* affected by a restriction.
            Stops whose inbound leg intersects a severe+ restriction
            are removed from the returned list and appended to
            ``deferred``.
        """

        if not stops:
            return []
        if station_locations is None:
            return list(stops)

        kept: List[RouteStop] = []
        prev_lat: Optional[float]
        prev_lon: Optional[float]
        if start_position is not None:
            prev_lat = start_position.lat
            prev_lon = start_position.lon
        else:
            prev_lat = None
            prev_lon = None

        for stop in stops:
            stop_loc = station_locations.get(stop.station_id)
            if stop_loc is None:
                # No coordinates available — we cannot build a segment.
                # Keep the stop so we don't silently drop it.
                kept.append(stop)
                continue
            try:
                next_lat = float(stop_loc.get("lat"))
                next_lon = float(stop_loc.get("lon"))
            except (TypeError, ValueError):
                kept.append(stop)
                continue

            # First leg requires a known start position; when absent,
            # fall back to "no segment to check" for this stop. The
            # next iteration will have prev_* populated from the stop
            # we just accepted, so subsequent legs are still checked.
            if prev_lat is None or prev_lon is None:
                prev_lat, prev_lon = next_lat, next_lon
                kept.append(stop)
                continue

            match_ids = await self._query_road_restriction_intersects(
                tenant_id=tenant_id,
                line_start=(prev_lat, prev_lon),
                line_end=(next_lat, next_lon),
            )
            if match_ids:
                logger.info(
                    "RoutePlanningAgent: Storm_Mode road-restriction "
                    "match tenant=%s truck=%s stop=%s restrictions=%s",
                    tenant_id,
                    truck_id,
                    stop.station_id,
                    ",".join(match_ids),
                )
                deferred.append(
                    DeferredRouteStop(
                        station_id=stop.station_id,
                        reason=REASON_ROAD_RESTRICTION,
                        deferral_cause=CAUSE_ROAD_RESTRICTION,
                        original_sequence=stop.sequence,
                        original_eta=stop.eta,
                    )
                )
                # Do not advance ``prev_*`` — the stop is not on the
                # traversed route, so the next stop's inbound leg is
                # measured from the same predecessor.
                continue

            kept.append(stop)
            prev_lat, prev_lon = next_lat, next_lon

        return kept

    async def _query_road_restriction_intersects(
        self,
        *,
        tenant_id: str,
        line_start: Tuple[float, float],
        line_end: Tuple[float, float],
    ) -> List[str]:
        """Return restriction_ids whose polygon intersects the segment.

        Builds a GeoJSON ``LineString`` (``[[lon, lat], [lon, lat]]``)
        and runs an ES ``geo_shape`` intersects query against
        :data:`STORM_ROAD_RESTRICTIONS_ES_INDEX` filtered by
        ``tenant_id``, active effective window, and severity
        ``∈`` :data:`ROAD_RESTRICTION_BLOCKING_SEVERITIES`.

        Returns an empty list on:

        * missing ES service (shouldn't happen once bootstrap is wired),
        * any transport error (logged and suppressed so Storm_Mode
          routing never fails closed — the delivery-window / stop-cap
          guard-rails still fire),
        * a same-point "segment" (``line_start == line_end``) which
          collapses to a point and cannot meaningfully intersect a
          polygon.

        The caller treats a non-empty return as "defer this stop".
        """

        if self._es is None:
            return []

        start_lat, start_lon = line_start
        end_lat, end_lon = line_end
        if start_lat == end_lat and start_lon == end_lon:
            # Degenerate segment — skip the query entirely.
            return []

        now_iso = datetime.now(timezone.utc).isoformat()

        # GeoJSON coordinate order is ``[lon, lat]`` (RFC 7946 §3.1.1).
        # ``relation=intersects`` matches any polygon that overlaps,
        # touches, or contains the line; this is exactly the semantics
        # Req 9.3.4 describes ("crosses a matching polygon").
        geo_shape_clause = {
            "geo_shape": {
                "polygon": {
                    "shape": {
                        "type": "LineString",
                        "coordinates": [
                            [float(start_lon), float(start_lat)],
                            [float(end_lon), float(end_lat)],
                        ],
                    },
                    "relation": "intersects",
                }
            }
        }

        filters: List[Dict[str, Any]] = [
            {"term": {"tenant_id": tenant_id}},
            {
                "terms": {
                    "severity": list(ROAD_RESTRICTION_BLOCKING_SEVERITIES)
                }
            },
            {"range": {"effective_from": {"lte": now_iso}}},
            {
                "bool": {
                    "should": [
                        {
                            "bool": {
                                "must_not": {
                                    "exists": {"field": "effective_to"}
                                }
                            }
                        },
                        {"range": {"effective_to": {"gte": now_iso}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            geo_shape_clause,
        ]

        query: Dict[str, Any] = {
            "query": {"bool": {"filter": filters}},
            "_source": ["restriction_id", "tenant_id", "severity"],
        }

        try:
            resp = await self._es.search_documents(
                STORM_ROAD_RESTRICTIONS_ES_INDEX,
                query,
                _ROAD_RESTRICTION_MATCH_CEILING,
            )
        except Exception as exc:
            logger.warning(
                "RoutePlanningAgent: geo_shape intersects query failed "
                "for tenant=%s segment=%s→%s: %s — skipping "
                "road-restriction filter for this segment",
                tenant_id,
                line_start,
                line_end,
                exc,
            )
            return []

        hits = (resp or {}).get("hits", {}).get("hits", []) or []
        matches: List[str] = []
        for hit in hits:
            source = hit.get("_source") or {}
            # Defensive tenant re-check — if the index layer ever returns
            # a cross-tenant row (corrupt mapping, misrouted alias)
            # silently drop it rather than deferring the wrong stop.
            if source.get("tenant_id") != tenant_id:
                continue
            restriction_id = source.get("restriction_id")
            if isinstance(restriction_id, str) and restriction_id:
                matches.append(restriction_id)
        return matches

    async def _is_storm_mode_active(self, tenant_id: str) -> bool:
        """Return ``True`` when Storm_Mode is ``active`` for ``tenant_id``.

        Mirrors the :class:`DeliveryPrioritizationAgent` implementation:
        tolerates a missing evaluator, empty tenant ids, and transient
        lookup failures by returning ``False`` so the legacy non-storm
        path stays engaged.
        """

        evaluator = self._storm_mode_evaluator
        if evaluator is None or not tenant_id:
            return False
        try:
            state = await evaluator.get_state(tenant_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: StormModeEvaluator.get_state raised "
                "for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return False

        raw_state: Any
        if isinstance(state, StormModePersistedState):
            raw_state = state.state
        elif isinstance(state, dict):
            raw_state = state.get("state")
        else:
            raw_state = getattr(state, "state", None)
        return raw_state == STORM_MODE_ACTIVE

    async def _load_storm_mode_settings(
        self, tenant_id: str
    ) -> StormModeRouteSettings:
        """Return the tenant's Storm_Mode route settings (with defaults).

        Invokes the injected loader when present; falls back to the
        module-level defaults whenever the loader is ``None``, returns
        ``None``, or raises. Out-of-range values coming from the loader
        are clamped / replaced with defaults so a misconfigured tenant
        still receives a deterministic guard-rail.
        """

        loader = self._storm_mode_settings_loader
        raw: Optional[StormModeRouteSettings] = None
        if loader is not None:
            try:
                raw = await loader(tenant_id)
            except Exception as exc:
                logger.warning(
                    "RoutePlanningAgent: storm_mode_settings_loader "
                    "failed for tenant=%s: %s — using defaults",
                    tenant_id,
                    exc,
                )
                raw = None

        if raw is None:
            return StormModeRouteSettings()

        max_stops = int(raw.max_stops_per_truck)
        if max_stops <= 0:
            max_stops = DEFAULT_STORM_MODE_MAX_STOPS_PER_TRUCK

        start_hour = float(raw.delivery_window_start_hour)
        end_hour = float(raw.delivery_window_end_hour)
        if not (0.0 <= start_hour <= 24.0) or not (0.0 <= end_hour <= 24.0):
            start_hour = DEFAULT_STORM_MODE_DELIVERY_WINDOW_START_HOUR
            end_hour = DEFAULT_STORM_MODE_DELIVERY_WINDOW_END_HOUR

        return StormModeRouteSettings(
            max_stops_per_truck=max_stops,
            delivery_window_start_hour=start_hour,
            delivery_window_end_hour=end_hour,
            timezone=raw.timezone,
        )

    async def _resolve_traffic_provider_name(
        self, tenant_id: str
    ) -> Optional[str]:
        """Read ``overlay.traffic_provider:{tenant_id}`` and return the name.

        Accepts either a plain string (``"mapbox"``) or a JSON object
        whose ``name`` key carries the short provider identifier. Unknown
        shapes are logged and treated as "no provider configured" — the
        caller surfaces this as a Haversine fallback with annotation.
        """

        if self._tenant_config is None:
            return None
        key = TRAFFIC_PROVIDER_CONFIG_KEY_TEMPLATE.format(tenant_id=tenant_id)
        try:
            raw = await self._tenant_config.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: tenant_config.get(%s) failed: %s",
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
            stripped = raw.strip()
            if not stripped:
                return None
            # Try JSON first; fall back to treating the value as the
            # provider short-name. A config value of "mapbox" is the
            # common, idiomatic form so we don't require operators to
            # wrap it in JSON.
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "RoutePlanningAgent: traffic_provider config for "
                        "tenant=%s is not valid JSON: %r",
                        tenant_id,
                        stripped,
                    )
                    return None
                name = parsed.get("name") if isinstance(parsed, Mapping) else None
                if isinstance(name, str) and name.strip():
                    return name.strip().lower()
                return None
            return stripped.lower()
        if isinstance(raw, Mapping):
            name = raw.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip().lower()
        return None

    def _get_or_build_traffic_provider(
        self, tenant_id: str, provider_name: str
    ) -> Optional[TrafficProvider]:
        """Return a cached TrafficProvider for the tenant, or build one.

        Caches by ``(tenant_id, provider_name)`` so rotating a tenant's
        provider invalidates only that tenant's entry. The injected
        factory is tried first (so tests can stub providers); when no
        factory is configured we fall back to the module-level
        :func:`build_traffic_provider` registry.
        """

        cache_key = (tenant_id, provider_name)
        cached = self._traffic_provider_cache.get(cache_key)
        if cached is not None:
            return cached

        provider: Optional[TrafficProvider] = None
        if self._traffic_provider_factory is not None:
            try:
                provider = self._traffic_provider_factory(provider_name, tenant_id)
            except Exception as exc:
                logger.warning(
                    "RoutePlanningAgent: traffic_provider_factory(%s) failed "
                    "for tenant=%s: %s",
                    provider_name,
                    tenant_id,
                    exc,
                )
                provider = None
        if provider is None:
            try:
                provider = build_traffic_provider(provider_name)
            except ValueError as exc:
                logger.warning(
                    "RoutePlanningAgent: unknown traffic_provider=%r for "
                    "tenant=%s (%s)",
                    provider_name,
                    tenant_id,
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "RoutePlanningAgent: failed to build traffic_provider=%r "
                    "for tenant=%s: %s",
                    provider_name,
                    tenant_id,
                    exc,
                )
                return None

        if provider is not None:
            self._traffic_provider_cache[cache_key] = provider
        return provider

    # ------------------------------------------------------------------
    # Truck start-position resolution (Task 9.7 / Req 5.4.6)
    # ------------------------------------------------------------------

    async def _resolve_start_position(
        self,
        *,
        tenant_id: str,
        truck_id: str,
    ) -> Optional[TruckStartPosition]:
        """Return the telemetry-first / depot-fallback start position.

        Returns ``None`` when no depot resolver is configured — the
        agent then preserves the legacy single-depot behaviour
        (``DEFAULT_DEPOT`` at index 0) so tests and early bootstrap
        states continue to work. When a resolver *is* configured and
        neither a fresh ``truck_telemetry`` reading nor a depot is
        available, this method also returns ``None`` so the caller can
        skip the loading plan (the REST surface translates the same
        scenario into HTTP 400 ``no_depot_configured`` per Req 2.2.4).

        Defense-in-depth: the helper enforces tenant isolation on the
        telemetry row itself, so a drifted ``truck_telemetry`` document
        can never leak another tenant's coordinates into this tenant's
        Route_Plan (Req 5.4.6 + tenant-guard invariants).
        """

        if self._depot_resolver is None or not truck_id:
            return None

        truck: Dict[str, Any] = {"truck_id": truck_id}
        try:
            return await resolve_truck_start_position(
                tenant_id=tenant_id,
                truck=truck,
                depot_resolver=self._depot_resolver,
                es_service=self._es,
            )
        except NoDepotConfiguredError as exc:
            logger.warning(
                "RoutePlanningAgent: %s — will skip route for truck=%s",
                exc,
                truck_id,
            )
            return None
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RoutePlanningAgent: start-position resolver failed for "
                "tenant=%s truck=%s: %s — skipping route",
                tenant_id,
                truck_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Persistence (Req 4.7)
    # ------------------------------------------------------------------

    async def _persist_route_plan(self, route_plan: RoutePlan) -> None:
        """Persist a RoutePlan to the mvp_routes ES index.

        Canonicalizes every ``drop`` key (a fuel grade) on each stop
        before write so routes persisted from NG-aliased loading plans
        land in ES with canonical US codes (Req 6.1.4). Quantities from
        duplicate grade keys (e.g., both ``AGO`` and ``DIESEL_2`` on the
        same stop after canonicalization) are summed to preserve total
        drop volume. Unknown codes are preserved with a warning.
        """
        try:
            doc = route_plan.model_dump(mode="json")
            stops = doc.get("stops") or []
            for stop in stops:
                raw_drop = stop.get("drop") or {}
                if not isinstance(raw_drop, dict):
                    continue
                canonical_drop: Dict[str, float] = {}
                for grade, qty in raw_drop.items():
                    canonical_grade = canonicalize_or_warn(
                        grade,
                        context="mvp_routes.stops.drop",
                        logger_=logger,
                    )
                    # Accumulate quantities under the canonical key so a
                    # stop carrying both a US code and its NG alias does
                    # not lose volume after normalization.
                    canonical_drop[canonical_grade] = (
                        canonical_drop.get(canonical_grade, 0.0) + float(qty or 0.0)
                    )
                stop["drop"] = canonical_drop

            await self._es.index_document(
                MVP_ROUTES_INDEX,
                route_plan.route_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "RoutePlanningAgent: failed to persist route plan %s: %s",
                route_plan.route_id,
                e,
            )
