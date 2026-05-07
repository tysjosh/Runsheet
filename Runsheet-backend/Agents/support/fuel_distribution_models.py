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
    eta: str  # ISO 8601
    drop: Dict[str, float]  # grade -> liters
    sequence: int = Field(ge=0)


class RoutePlan(BaseModel):
    """An optimized delivery route.
    Validates: Requirement 4.1, 2.1.5, 2.1.6, 8.5.5
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


class ReplanDiff(BaseModel):
    """Describes changes made during replanning."""
    stops_reordered: List[str] = Field(default_factory=list)
    volumes_reallocated: Dict[str, float] = Field(default_factory=dict)
    truck_swapped: Optional[str] = None
    stations_deferred: List[str] = Field(default_factory=list)
    stations_added: List[str] = Field(default_factory=list)


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
