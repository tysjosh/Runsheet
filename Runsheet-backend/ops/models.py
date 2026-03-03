"""
Shared Pydantic models for the Ops Intelligence Layer.

Provides the consistent JSON response envelope used by all ops endpoints,
along with domain models for shipments, riders, and metrics.

Response envelope format (Requirement 8.6):
{
    "data": [...],
    "pagination": {"page": 1, "size": 20, "total": 142, "total_pages": 8},
    "request_id": "req_abc123"
}
"""

import math
from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationMeta(BaseModel):
    """Pagination metadata included in every paginated response."""

    page: int = Field(..., ge=1, description="Current page number (1-indexed)")
    size: int = Field(..., ge=1, le=100, description="Number of items per page")
    total: int = Field(..., ge=0, description="Total number of matching items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")

    @classmethod
    def compute(cls, page: int, size: int, total: int) -> "PaginationMeta":
        """Create PaginationMeta with total_pages computed from total and size."""
        total_pages = math.ceil(total / size) if size > 0 else 0
        return cls(page=page, size=size, total=total, total_pages=total_pages)


class PaginatedResponse(BaseModel, Generic[T]):
    """
    Consistent JSON envelope for all paginated ops API responses.

    Validates: Requirement 8.6
    """

    data: list[T] = Field(default_factory=list, description="List of result items")
    pagination: PaginationMeta = Field(..., description="Pagination metadata")
    request_id: str = Field(..., description="Unique request identifier for tracing")


class ShipmentDetail(BaseModel):
    """Shipment record from the shipments_current index."""

    shipment_id: str = Field(..., description="Unique shipment identifier")
    status: str = Field(..., description="Current shipment status (pending, in_transit, delivered, failed, returned)")
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    rider_id: Optional[str] = Field(default=None, description="Assigned rider identifier")
    origin: Optional[str] = Field(default=None, description="Shipment origin location")
    destination: Optional[str] = Field(default=None, description="Shipment destination location")
    current_location: Optional[dict[str, float]] = Field(default=None, description="Current geo location {lat, lon}")
    created_at: Optional[datetime] = Field(default=None, description="Shipment creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Last update timestamp")
    estimated_delivery: Optional[datetime] = Field(default=None, description="Estimated delivery time")
    last_event_timestamp: Optional[datetime] = Field(default=None, description="Timestamp of the most recent event")
    failure_reason: Optional[str] = Field(default=None, description="Reason for failure if status is failed")
    source_schema_version: Optional[str] = Field(default=None, description="Schema version of the source event")
    trace_id: Optional[str] = Field(default=None, description="Request trace identifier")
    ingested_at: Optional[datetime] = Field(default=None, description="Timestamp when the record was ingested")


class RiderDetail(BaseModel):
    """Rider record from the riders_current index."""

    rider_id: str = Field(..., description="Unique rider identifier")
    rider_name: Optional[str] = Field(default=None, description="Rider display name")
    status: str = Field(..., description="Current rider status (active, idle, offline)")
    tenant_id: str = Field(..., description="Tenant identifier for data isolation")
    availability: Optional[str] = Field(default=None, description="Rider availability status")
    current_location: Optional[dict[str, float]] = Field(default=None, description="Current geo location {lat, lon}")
    active_shipment_count: Optional[int] = Field(default=None, description="Number of active shipments assigned")
    completed_today: Optional[int] = Field(default=None, description="Number of shipments completed today")
    last_seen: Optional[datetime] = Field(default=None, description="Last activity timestamp")
    last_event_timestamp: Optional[datetime] = Field(default=None, description="Timestamp of the most recent event")
    source_schema_version: Optional[str] = Field(default=None, description="Schema version of the source event")
    trace_id: Optional[str] = Field(default=None, description="Request trace identifier")
    ingested_at: Optional[datetime] = Field(default=None, description="Timestamp when the record was ingested")


class MetricsBucket(BaseModel):
    """A single time bucket in an aggregated metrics response."""

    timestamp: datetime = Field(..., description="Start of the time bucket")
    values: dict[str, Any] = Field(default_factory=dict, description="Metric key-value pairs for this bucket")


class MetricsResponse(BaseModel):
    """Response envelope for aggregated metrics endpoints."""

    data: list[MetricsBucket] = Field(default_factory=list, description="Time-bucketed metric data")
    bucket: str = Field(default="hourly", description="Bucket granularity (hourly or daily)")
    start_date: Optional[str] = Field(default=None, description="Start of the queried time range")
    end_date: Optional[str] = Field(default=None, description="End of the queried time range")
    request_id: str = Field(..., description="Unique request identifier for tracing")
