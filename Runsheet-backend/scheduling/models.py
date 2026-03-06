"""Pydantic models, enums, and constants for the Logistics Scheduling module."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# --- Enums ---


class JobType(str, Enum):
    CARGO_TRANSPORT = "cargo_transport"
    PASSENGER_TRANSPORT = "passenger_transport"
    VESSEL_MOVEMENT = "vessel_movement"
    AIRPORT_TRANSFER = "airport_transfer"
    CRANE_BOOKING = "crane_booking"


class JobStatus(str, Enum):
    SCHEDULED = "scheduled"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class CargoItemStatus(str, Enum):
    PENDING = "pending"
    LOADED = "loaded"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    DAMAGED = "damaged"


# --- Valid status transitions ---

VALID_TRANSITIONS: dict[JobStatus, list[JobStatus]] = {
    JobStatus.SCHEDULED: [JobStatus.ASSIGNED, JobStatus.CANCELLED],
    JobStatus.ASSIGNED: [JobStatus.IN_PROGRESS, JobStatus.CANCELLED],
    JobStatus.IN_PROGRESS: [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
    JobStatus.COMPLETED: [],
    JobStatus.CANCELLED: [],
    JobStatus.FAILED: [],
}


# --- Asset-type compatibility ---

JOB_ASSET_COMPATIBILITY: dict[JobType, list[str]] = {
    JobType.CARGO_TRANSPORT: ["vehicle"],
    JobType.PASSENGER_TRANSPORT: ["vehicle"],
    JobType.VESSEL_MOVEMENT: ["vessel"],
    JobType.AIRPORT_TRANSFER: ["vehicle"],
    JobType.CRANE_BOOKING: ["equipment"],
}


# --- Pydantic Models ---


class GeoPoint(BaseModel):
    lat: float
    lng: float


class CargoItem(BaseModel):
    item_id: Optional[str] = None
    description: str
    weight_kg: float = Field(gt=0)
    container_number: Optional[str] = None
    seal_number: Optional[str] = None
    item_status: CargoItemStatus = CargoItemStatus.PENDING


class CreateJob(BaseModel):
    job_type: JobType
    origin: str
    destination: str
    scheduled_time: str  # ISO 8601
    asset_assigned: Optional[str] = None
    cargo_manifest: Optional[list[CargoItem]] = None
    priority: Priority = Priority.NORMAL
    notes: Optional[str] = None
    created_by: Optional[str] = None
    origin_location: Optional[GeoPoint] = None
    destination_location: Optional[GeoPoint] = None

    @model_validator(mode="after")
    def validate_cargo_for_transport(self):
        if self.job_type == JobType.CARGO_TRANSPORT:
            if not self.cargo_manifest or len(self.cargo_manifest) == 0:
                raise ValueError(
                    "cargo_transport jobs require at least one cargo manifest item"
                )
        return self


class AssignAsset(BaseModel):
    asset_id: str


class StatusTransition(BaseModel):
    status: JobStatus
    failure_reason: Optional[str] = None

    @model_validator(mode="after")
    def require_failure_reason(self):
        if self.status == JobStatus.FAILED and not self.failure_reason:
            raise ValueError(
                "failure_reason is required when transitioning to failed"
            )
        return self


class UpdateCargoManifest(BaseModel):
    items: list[CargoItem]


class UpdateCargoItemStatus(BaseModel):
    item_id: str
    item_status: CargoItemStatus


class Job(BaseModel):
    job_id: str
    job_type: JobType
    status: JobStatus
    tenant_id: str
    asset_assigned: Optional[str] = None
    origin: str
    destination: str
    origin_location: Optional[GeoPoint] = None
    destination_location: Optional[GeoPoint] = None
    scheduled_time: str
    estimated_arrival: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    priority: Priority = Priority.NORMAL
    delayed: bool = False
    delay_duration_minutes: Optional[int] = None
    failure_reason: Optional[str] = None
    notes: Optional[str] = None
    cargo_manifest: Optional[list[CargoItem]] = None


class JobEvent(BaseModel):
    event_id: str
    job_id: str
    event_type: str
    tenant_id: str
    actor_id: Optional[str] = None
    event_timestamp: str
    event_payload: dict


class JobSummary(BaseModel):
    total_jobs: int
    scheduled: int
    assigned: int
    in_progress: int
    completed: int
    cancelled: int
    failed: int
    delayed: int


class SchedulingMetricsBucket(BaseModel):
    timestamp: str
    counts_by_status: dict[str, int]
    counts_by_type: dict[str, int]


class CompletionMetrics(BaseModel):
    job_type: str
    total: int
    completed: int
    completion_rate: float
    avg_completion_minutes: float


class AssetUtilizationMetric(BaseModel):
    asset_id: str
    asset_type: str
    total_jobs: int
    active_jobs: int
    completed_jobs: int
    total_active_hours: float
    idle_hours: float
