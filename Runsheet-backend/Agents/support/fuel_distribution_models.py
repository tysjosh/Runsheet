"""
Shared data contracts for the Fuel Distribution MVP pipeline.

Defines TankForecast, DeliveryPriority, DeliveryPriorityList,
RoutePlan, RouteStop, and ReplanEvent models.

Validates: Requirements 1.1, 2.1, 4.1, 5.2
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class FuelGrade(str, Enum):
    AGO = "AGO"
    PMS = "PMS"
    ATK = "ATK"
    LPG = "LPG"


class PriorityBucket(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TankForecast(BaseModel):
    """Probabilistic runout forecast for a (station, grade) pair.

    Extended per fuel-ops hardening Requirements 1.1.2, 1.2.3, 1.3.4, 1.4.3,
    1.5.6, 1.6.1 to carry US-market customer-tank context (customer_tank_id,
    customer_id, customer_type, fuel_type), the selected Consumption_Model
    (``model_name``), segmentation metadata (``customer_type_multiplier``,
    ``baseline_source``), weather-fallback annotation
    (``weather_fallback``), and the scheduled-delivery entries the
    forecaster folded into the projected level.

    All new fields are optional so legacy retail-station forecasts continue
    to produce documents that round-trip through this model without
    requiring the added context; they default to ``None`` / empty list.

    Validates: Requirements 1.1, 1.1.2, 1.2.3, 1.3.4, 1.4.3, 1.5.6, 1.6.1
    """
    forecast_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    station_id: str
    fuel_grade: FuelGrade
    hours_to_runout_p50: float = Field(ge=0.0)
    hours_to_runout_p90: float = Field(ge=0.0)
    runout_risk_24h: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    feature_version: str = "v1.0"
    anomaly_flags: List[str] = Field(default_factory=list)
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # --- Customer-tank extensions (fuel-ops hardening Capability 1) -----
    customer_tank_id: Optional[str] = Field(
        default=None,
        description=(
            "Customer_Tank identifier when this forecast is for a "
            "customer tank; null for retail-station forecasts (Req 1.1.2)."
        ),
    )
    customer_id: Optional[str] = Field(
        default=None,
        description="Owning customer id for customer_tank forecasts (Req 1.6.1).",
    )
    customer_type: Optional[str] = Field(
        default=None,
        description=(
            "Customer segment (residential/commercial/keep_full/will_call/"
            "auto_fill) for customer_tank forecasts (Req 1.1.2, 1.6.1)."
        ),
    )
    fuel_type: Optional[str] = Field(
        default=None,
        description=(
            "Narrow fuel-family tag (propane/heating_oil/diesel/"
            "generator_fuel/farm_fuel/gasoline) used to pick a "
            "Consumption_Model strategy (Req 1.6.1)."
        ),
    )
    model_name: Optional[str] = Field(
        default=None,
        description=(
            "Identifier of the Consumption_Model that produced "
            "``gallons_per_day`` (Req 1.5.6, 1.6.1)."
        ),
    )
    customer_type_multiplier: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Customer-type multiplier applied to gpd (Req 1.3.4, 1.6.1).",
    )
    baseline_source: Optional[str] = Field(
        default=None,
        description=(
            "``history`` when the baseline came from ≥3 prior deliveries for "
            "this tank, else ``default`` (Req 1.3.4)."
        ),
    )
    weather_fallback: Optional[bool] = Field(
        default=None,
        description=(
            "True when the Weather_Provider was unavailable or returned "
            "no usable rows so the forecaster fell back to the non-weather "
            "model (Req 1.2.5, 1.6.1)."
        ),
    )
    scheduled_deliveries: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Scheduled deliveries within the 72-hour horizon folded into "
            "the projected level. Each entry is "
            "``{delivery_id, scheduled_eta, planned_gallons}`` (Req 1.4.3)."
        ),
    )


class DeliveryPriority(BaseModel):
    """Scored priority for a single station/grade.

    Extended per fuel-ops hardening Requirements 3.1.3, 3.3.3, 3.3.4, 3.4.2
    to carry the dispatcher-visible delay tolerance, the real business-
    impact score and its reasons, and the route-friendly DBSCAN cluster
    assignment. All new fields are optional so legacy forecasts produced
    before Phase 5 continue to round-trip through this model; they default
    to ``None`` / empty list.

    Validates: Requirements 2.1, 3.1.3, 3.3.3, 3.3.4, 3.4.2
    """
    station_id: str
    fuel_grade: FuelGrade
    priority_score: float = Field(ge=0.0, le=1.0)
    priority_bucket: PriorityBucket
    reasons: List[str] = Field(default_factory=list)

    # --- Phase 5 extensions (fuel-ops hardening Capability 3) -----------
    # Safe-to-delay tolerance (Req 3.1.3): populated from
    # ``fuel.services.prioritization_helpers.compute_safe_to_delay``.
    safe_to_delay_days: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Days the delivery can be safely postponed without causing a "
            "runout or SLA breach (Req 3.1.1). Null when the forecast did "
            "not supply ``hours_to_runout_p90``."
        ),
    )
    safe_to_delay_bucket: Optional[str] = Field(
        default=None,
        description=(
            "Bucket label: one of ``none`` / ``short`` / ``medium`` / "
            "``long`` per Req 3.1.2."
        ),
    )

    # Business-impact (Req 3.3.3, 3.3.4): replaces the placeholder
    # ``business_impact`` component; retains the existing 0.15 weight
    # on ``priority_score``.
    business_impact_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Normalized business-impact score [0.0, 1.0] produced by "
            "``compute_business_impact`` (Req 3.3.2)."
        ),
    )
    business_impact_reasons: List[str] = Field(
        default_factory=list,
        description=(
            "Human-readable reasons explaining which components drove "
            "the business-impact score (Req 3.3.4, 3.3.5). Entries like "
            "``missing_profile_field:annual_revenue_usd`` surface gaps "
            "that dispatchers can action."
        ),
    )

    # Route-friendly DBSCAN cluster (Req 3.4.2): populated by
    # ``compute_priority_clusters`` (Task 5.4). Null entries represent
    # either noise (DBSCAN label -1) or a not-yet-clustered run.
    cluster_id: Optional[str] = Field(
        default=None,
        description=(
            "DBSCAN cluster identifier the entry belongs to (Req 3.4.2). "
            "``noise`` indicates the entry is isolated per DBSCAN "
            "conventions; ``null`` indicates clustering has not been "
            "computed for this run."
        ),
    )
    cluster_size: Optional[int] = Field(
        default=None,
        ge=0,
        description="Number of entries in the DBSCAN cluster (Req 3.4.2).",
    )
    cluster_centroid: Optional[Dict[str, float]] = Field(
        default=None,
        description=(
            "DBSCAN cluster centroid as ``{\"lat\": <float>, \"lon\": "
            "<float>}`` in WGS84 degrees (Req 3.4.2). For dense "
            "clusters this is the arithmetic mean of member lat/lon; "
            "for noise rows (``cluster_id=\"noise\"``) the centroid is "
            "the entry's own location so downstream consumers can "
            "render a consistent shape. ``None`` when clustering has "
            "not been computed for the run."
        ),
    )


class DeliveryPriorityList(BaseModel):
    """Ranked list of delivery priorities for a pipeline run.
    Validates: Requirement 2.1
    """
    priority_list_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    priorities: List[DeliveryPriority]
    scoring_weights: Dict[str, float] = Field(default_factory=dict)
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class RouteStop(BaseModel):
    """A single stop in a delivery route."""
    station_id: str
    # One customer stop may contain more than one order.  Keeping the exact
    # identifiers on the route makes dispatcher approval deterministic while
    # remaining backward compatible with older route documents.
    order_ids: List[str] = Field(default_factory=list)
    eta: str  # ISO 8601
    drop: Dict[str, float]  # grade -> liters
    sequence: int = Field(ge=0)


class DeferredRouteStop(BaseModel):
    """A stop removed from the route plan because a Storm_Mode guard-rail
    fired (Req 9.2.4, 9.3.2 / Task 10.7).

    ``reason`` is always the spec-mandated tag ``deferred_storm_mode`` so
    downstream filters can pin on a single string. ``deferral_cause``
    narrows the cause to either ``over_max_stops_per_truck`` (Req 9.2.4
    — the per-truck cap was exceeded) or ``outside_delivery_window``
    (Req 9.3.2 — the stop's ETA fell outside the configured window).
    The ``next_eligible_window_*`` pair is populated only when the
    cause is ``outside_delivery_window`` and identifies the soonest
    window the stop could be rescheduled into.
    """

    station_id: str
    reason: str = "deferred_storm_mode"
    deferral_cause: str  # over_max_stops_per_truck | outside_delivery_window
    original_sequence: Optional[int] = Field(default=None, ge=0)
    original_eta: Optional[str] = None
    next_eligible_window_start: Optional[str] = None
    next_eligible_window_end: Optional[str] = None


class WindowMissEntry(BaseModel):
    """An order whose delivery window cannot be satisfied (Req 5.2.3).

    Surfaced in the replan diff so dispatchers see explicit window
    violations rather than silent re-sequencing. The Route_Planning_Agent
    populates these when ``delivery_window_end`` has already passed or
    when the window is too narrow to reach given the current route state.
    """

    order_id: str
    reason: str = "window_miss"
    delivery_window_start: Optional[str] = None
    delivery_window_end: Optional[str] = None
    detail: Optional[str] = None


class RoutePlan(BaseModel):
    """An optimized delivery route.
    Validates: Requirement 4.1, 2.1.5, 2.1.6, 8.5.5, 9.2.4, 9.2.5, 9.3.1, 9.3.2
    """
    route_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    truck_id: str
    plan_id: str  # References the loading plan
    stops: List[RouteStop]
    distance_km: float = Field(ge=0.0)
    eta_confidence: float = Field(ge=0.0, le=1.0)
    objective_value: float = 0.0
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: str = "proposed"
    # Capability 2 Requirement 2.1.5 / 2.1.6 — traffic-aware routing
    # annotations persisted alongside every Route_Plan so downstream
    # consumers can distinguish traffic-informed plans from Haversine
    # fallbacks. ``traffic_provider`` is ``None`` when traffic-aware
    # routing is disabled for the tenant; ``traffic_fallback`` is
    # ``True`` only when a configured provider call failed (timeout,
    # budget exhaustion, HTTP error) and the agent degraded to the
    # Haversine + DEFAULT_SPEED_KMH matrix.
    traffic_provider: Optional[str] = None
    traffic_fallback: bool = False
    #: Capability 8 Task 7.10 — when a Loading_Plan required an external
    #: terminal lift (i.e. the plan's ``terminal_id`` is not the tenant's
    #: depot), the Route_Planning_Agent consults the Sourcing_Recommender
    #: and records the chosen terminal id here alongside the top
    #: candidate's ``reasons`` list so audit / dispatcher UIs can trace
    #: why a specific terminal was selected. ``None`` when no external
    #: lift was required or the ``overlay.terminal_sourcing`` feature
    #: flag was off (Req 8.5.5).
    sourced_terminal_id: Optional[str] = None
    #: Human-readable reasons from the winning
    #: :class:`fuel.terminal_models.TerminalCandidate`. Surfaced on the
    #: dispatcher UI so operators can see why the Sourcing_Recommender
    #: picked this terminal (e.g. ``best_price``, ``shortest_wait``,
    #: ``contract_priority_boost:sc_123``).
    sourced_terminal_reasons: List[str] = Field(default_factory=list)
    #: Top-level ``recommendation_id`` of the persisted
    #: :class:`fuel.terminal_models.SourcingRecommendation` that drove
    #: the pick. Empty string when no sourcing was run.
    sourcing_recommendation_id: Optional[str] = None
    #: Fuel Ops Hardening Req 5.4.6 / Task 9.7 — provenance of the
    #: route's start coordinates. ``"telemetry"`` means a fresh (<300s)
    #: ``truck_telemetry`` reading was used; ``"depot"`` means the
    #: resolver fell back to the truck's assigned depot (or the
    #: tenant's default depot). ``None`` on legacy plans persisted
    #: before this annotation was added so consumers can distinguish
    #: "unknown" from "depot".
    start_position_source: Optional[str] = None
    #: Resolved start latitude (WGS84 degrees). Persisted alongside
    #: ``start_position_source`` so downstream audit tooling can show
    #: where the route actually started without re-querying the
    #: telemetry index.
    start_position_lat: Optional[float] = None
    #: Resolved start longitude (WGS84 degrees). See
    #: ``start_position_lat``.
    start_position_lon: Optional[float] = None

    # --- Storm_Mode annotations (Task 10.7, Req 9.2.4, 9.2.5, 9.3.1, 9.3.2) ---
    #: Whether Storm_Mode guard-rails were applied while building this
    #: plan. ``True`` when the :class:`StormModeEvaluator` reported
    #: ``active`` for the tenant at plan time; ``False`` for all plans
    #: built with the standard path. Surfaced on the persisted document
    #: so auditors can distinguish storm-time plans without re-reading
    #: the evaluator state.
    storm_mode_active: bool = False
    #: Per-truck stop cap enforced while this plan was built (Req 9.2.4).
    #: ``None`` when Storm_Mode was inactive.
    storm_mode_max_stops_per_truck: Optional[int] = None
    #: Delivery-window start hour (0.0–24.0, tenant local time) enforced
    #: while this plan was built (Req 9.3.1). ``None`` when Storm_Mode
    #: was inactive.
    storm_mode_delivery_window_start_hour: Optional[float] = None
    #: Delivery-window end hour (0.0–24.0, tenant local time) enforced
    #: while this plan was built (Req 9.3.1). ``None`` when Storm_Mode
    #: was inactive.
    storm_mode_delivery_window_end_hour: Optional[float] = None
    #: Stops removed from the planned route because a Storm_Mode
    #: guard-rail fired (Req 9.2.4, 9.3.2). Empty list for both
    #: non-storm plans and storm plans where every stop cleared the
    #: guard-rails.
    deferred_stops: List[DeferredRouteStop] = Field(default_factory=list)
    #: Orders whose delivery_window_start/delivery_window_end cannot be
    #: satisfied as hard routing constraints (Req 5.2.3). Surfaced in
    #: the replan diff rather than silent re-sequencing. Each entry
    #: carries the order_id, the window bounds, and a human-readable
    #: detail explaining why the window cannot be met.
    window_misses: List[WindowMissEntry] = Field(default_factory=list)


class ReplanDiff(BaseModel):
    """Describes changes made during replanning."""
    stops_reordered: List[str] = Field(default_factory=list)
    volumes_reallocated: Dict[str, float] = Field(default_factory=dict)
    truck_swapped: Optional[str] = None
    stations_deferred: List[str] = Field(default_factory=list)
    stations_added: List[str] = Field(default_factory=list)
    #: Orders whose delivery_window_start/delivery_window_end cannot be
    #: satisfied as hard routing constraints (Req 5.2.3). Surfaced
    #: explicitly so dispatchers see window violations rather than
    #: silent re-sequencing.
    window_misses: List[WindowMissEntry] = Field(default_factory=list)


class ReplanEvent(BaseModel):
    """A plan modification triggered by a disruption.
    Validates: Requirement 5.2
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    original_plan_id: str
    patched_plan_id: Optional[str] = None
    trigger_signal_id: str
    replan_type: str  # truck_swap | station_outage | demand_spike | delay
    diff: ReplanDiff = Field(default_factory=ReplanDiff)
    status: str = "applied"  # applied | failed | escalated
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
