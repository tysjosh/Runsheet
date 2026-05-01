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
    Validates: Requirement 1.1
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


class DeliveryPriority(BaseModel):
    """Scored priority for a single station/grade.
    Validates: Requirement 2.1
    """
    station_id: str
    fuel_grade: FuelGrade
    priority_score: float = Field(ge=0.0, le=1.0)
    priority_bucket: PriorityBucket
    reasons: List[str] = Field(default_factory=list)


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
    Validates: Requirement 4.1
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
