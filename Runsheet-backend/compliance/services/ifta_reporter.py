"""IFTA Reporter — per-jurisdiction mileage recording and quarterly reporting.

Implements the ``IFTA_Reporter`` described in design §7 of the Fuel Compliance
Backbone spec. This service records trip segments when trucks cross state
boundaries (detected via Geotab GPS telemetry and the StateBoundaryDetector),
aggregates per-jurisdiction miles per truck per quarter, and generates
quarterly IFTA reports.

Public methods:
    * ``record_trip_segment()`` — persists a trip segment to the
      ``ifta_mileage`` ES index when a state boundary crossing is detected.
    * ``get_mileage_by_jurisdiction()`` — queries aggregated miles per state
      for a given truck and quarter.
    * ``generate_quarterly_report()`` — placeholder for full quarterly IFTA
      report generation (implemented in task 12.6).
    * ``compute_fleet_mpg()`` — placeholder for fleet MPG computation
      (implemented in task 12.7).

All queries are tenant-scoped via ``inject_tenant_filter`` (Constraint C3).

Validates: Requirement 7.1
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from compliance.services.compliance_es_mappings import IFTA_MILEAGE_INDEX
from compliance.services.state_boundary_detector import StateBoundaryDetector
from ops.middleware.tenant_guard import inject_tenant_filter
from services.elasticsearch_service import ElasticsearchService
from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class TripSegment(BaseModel):
    """A single trip segment recording miles driven in a jurisdiction.

    Emitted when a truck crosses a state boundary. Each segment represents
    the miles driven in one state before crossing into the next.

    Validates: Requirement 7.1
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(
        default_factory=lambda: f"ifta_{uuid4()}",
        description="Server-assigned identifier of shape ifta_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    truck_id: str = Field(
        ..., description="Identifier of the truck that drove this segment"
    )
    jurisdiction: str = Field(
        ..., description="2-letter US state code (e.g., 'TX', 'OK')"
    )
    miles: float = Field(
        ..., ge=0, description="Miles driven in this jurisdiction segment"
    )
    quarter: str = Field(
        ..., description="Calendar quarter identifier, e.g. '2026-Q1'"
    )
    timestamp: datetime = Field(
        ..., description="Timestamp when the segment was recorded"
    )
    source: str = Field(
        default="geotab",
        description="Data source: geotab | fuel_card | manual_adjustment",
    )
    created_at: datetime = Field(default_factory=utcnow)


class JurisdictionMileage(BaseModel):
    """Aggregated mileage for a single jurisdiction within a quarter.

    Used in reporting to show total miles driven per state.

    Validates: Requirement 7.2
    """

    model_config = ConfigDict(extra="forbid")

    jurisdiction: str = Field(
        ..., description="2-letter US state code"
    )
    total_miles: float = Field(
        default=0.0, description="Total miles driven in this jurisdiction"
    )
    taxable_miles: float = Field(
        default=0.0, description="Taxable miles in this jurisdiction"
    )
    segment_count: int = Field(
        default=0, description="Number of trip segments recorded"
    )


class IFTAReport(BaseModel):
    """Quarterly IFTA report for a tenant's fleet.

    Contains per-truck, per-jurisdiction mileage summaries for a given
    calendar quarter.

    Validates: Requirement 7.4
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    quarter: str = Field(
        ..., description="Calendar quarter, e.g. '2026-Q1'"
    )
    fleet_mpg: Optional[float] = Field(
        default=None, description="Fleet average MPG for the quarter"
    )
    jurisdictions: List[JurisdictionMileage] = Field(
        default_factory=list,
        description="Per-jurisdiction mileage breakdown",
    )
    total_miles: float = Field(
        default=0.0, description="Total miles across all jurisdictions"
    )
    truck_count: int = Field(
        default=0, description="Number of trucks with mileage data"
    )
    generated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_quarter(timestamp: datetime) -> str:
    """Compute the calendar quarter string from a timestamp.

    Calendar quarters:
        Q1 = Jan–Mar, Q2 = Apr–Jun, Q3 = Jul–Sep, Q4 = Oct–Dec

    Args:
        timestamp: The datetime to compute the quarter for.

    Returns:
        Quarter string in format "YYYY-QN", e.g. "2026-Q1".
    """
    month = timestamp.month
    if month <= 3:
        quarter_num = 1
    elif month <= 6:
        quarter_num = 2
    elif month <= 9:
        quarter_num = 3
    else:
        quarter_num = 4
    return f"{timestamp.year}-Q{quarter_num}"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class IFTAReporter:
    """Service for IFTA interstate mileage recording and reporting.

    Records trip segments when trucks cross state boundaries (detected via
    Geotab GPS telemetry and the StateBoundaryDetector), persists them to
    the ``ifta_mileage`` ES index, and provides aggregation queries for
    quarterly IFTA reporting.

    Args:
        es_service: Elasticsearch handle for the ``ifta_mileage`` index.
        state_boundary_detector: Service for resolving GPS coordinates to
            US state codes and detecting boundary crossings.
        notification_service: Optional notification service for alerting
            fleet managers about incomplete data.

    Validates: Requirement 7.1
    """

    def __init__(
        self,
        es_service: ElasticsearchService,
        state_boundary_detector: StateBoundaryDetector,
        notification_service: Optional[Any] = None,
    ) -> None:
        self._es = es_service
        self._boundary_detector = state_boundary_detector
        self._notification_service = notification_service

    # ------------------------------------------------------------------
    # Record trip segment (Task 12.2, Req 7.1)
    # ------------------------------------------------------------------

    async def record_trip_segment(
        self,
        tenant_id: str,
        truck_id: str,
        from_state: str,
        to_state: str,
        miles: float,
        timestamp: datetime,
        source: str = "geotab",
    ) -> TripSegment:
        """Record a trip segment when a truck crosses a state boundary.

        Persists the miles driven in the origin jurisdiction (``from_state``)
        to the ``ifta_mileage`` ES index. The quarter is automatically
        computed from the provided timestamp.

        This method is called by the GeotabConnector sync_pull hook when
        a state boundary crossing is detected (task 12.3).

        Args:
            tenant_id: Tenant scope for the record.
            truck_id: Identifier of the truck.
            from_state: 2-letter state code of the jurisdiction where
                miles were driven (the state being exited).
            to_state: 2-letter state code of the jurisdiction being entered.
                Stored for audit context but the miles are attributed to
                ``from_state``.
            miles: Miles driven in the ``from_state`` jurisdiction.
            timestamp: Timestamp of the boundary crossing event.
            source: Data source identifier (default: "geotab").

        Returns:
            The persisted TripSegment record.

        Validates: Requirement 7.1
        """
        quarter = compute_quarter(timestamp)

        segment = TripSegment(
            tenant_id=tenant_id,
            truck_id=truck_id,
            jurisdiction=from_state.upper(),
            miles=miles,
            quarter=quarter,
            timestamp=timestamp,
            source=source,
        )

        # Serialize for ES
        doc: Dict[str, Any] = {
            "record_id": segment.record_id,
            "tenant_id": segment.tenant_id,
            "truck_id": segment.truck_id,
            "jurisdiction": segment.jurisdiction,
            "miles": segment.miles,
            "quarter": segment.quarter,
            "timestamp": segment.timestamp.isoformat(),
            "source": segment.source,
            "created_at": segment.created_at.isoformat(),
            # Additional context fields for the ES index
            "taxable_miles": segment.miles,  # Default: all miles are taxable
            "updated_at": segment.created_at.isoformat(),
        }

        await self._es.index_document(
            IFTA_MILEAGE_INDEX, segment.record_id, doc
        )

        logger.info(
            "Recorded IFTA trip segment: truck=%s, %s→%s, %.1f miles, "
            "quarter=%s, tenant=%s",
            truck_id,
            from_state,
            to_state,
            miles,
            quarter,
            tenant_id,
        )

        return segment

    # ------------------------------------------------------------------
    # Get mileage by jurisdiction (helper)
    # ------------------------------------------------------------------

    async def get_mileage_by_jurisdiction(
        self,
        tenant_id: str,
        truck_id: str,
        quarter: str,
    ) -> List[JurisdictionMileage]:
        """Query aggregated miles per jurisdiction for a truck and quarter.

        Uses an ES terms aggregation on the ``jurisdiction`` field with a
        sum sub-aggregation on ``miles`` to produce per-state totals.

        Args:
            tenant_id: Tenant scope for the query.
            truck_id: The truck to query mileage for.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            List of JurisdictionMileage records, one per state with
            recorded mileage.

        Validates: Requirement 7.2
        """
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"truck_id": truck_id}},
                        {"term": {"quarter": quarter}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "by_jurisdiction": {
                    "terms": {
                        "field": "jurisdiction",
                        "size": 60,  # All US states + territories
                    },
                    "aggs": {
                        "total_miles": {"sum": {"field": "miles"}},
                        "taxable_miles": {"sum": {"field": "taxable_miles"}},
                    },
                }
            },
        }

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            IFTA_MILEAGE_INDEX, query, size=0
        )

        # Parse aggregation results
        results: List[JurisdictionMileage] = []
        aggs = response.get("aggregations", {})
        buckets = aggs.get("by_jurisdiction", {}).get("buckets", [])

        for bucket in buckets:
            results.append(
                JurisdictionMileage(
                    jurisdiction=bucket["key"],
                    total_miles=bucket["total_miles"]["value"],
                    taxable_miles=bucket["taxable_miles"]["value"],
                    segment_count=bucket["doc_count"],
                )
            )

        return results

    # ------------------------------------------------------------------
    # Generate quarterly report (placeholder — Task 12.6)
    # ------------------------------------------------------------------

    async def generate_quarterly_report(
        self,
        tenant_id: str,
        quarter: str,
    ) -> IFTAReport:
        """Generate a quarterly IFTA report for the tenant's fleet.

        Aggregates per-jurisdiction miles across all trucks for the given
        quarter and produces a summary report.

        Note: Full implementation with per-truck breakdown, fuel gallons
        aggregation, and tax computation is in task 12.6. This skeleton
        provides the basic aggregation structure.

        Args:
            tenant_id: Tenant scope for the report.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            IFTAReport with per-jurisdiction mileage breakdown.

        Validates: Requirement 7.4
        """
        # Aggregate all miles across all trucks for this tenant/quarter
        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"quarter": quarter}},
                    ]
                }
            },
            "size": 0,
            "aggs": {
                "by_jurisdiction": {
                    "terms": {
                        "field": "jurisdiction",
                        "size": 60,
                    },
                    "aggs": {
                        "total_miles": {"sum": {"field": "miles"}},
                        "taxable_miles": {"sum": {"field": "taxable_miles"}},
                    },
                },
                "unique_trucks": {
                    "cardinality": {"field": "truck_id"},
                },
                "total_miles_all": {
                    "sum": {"field": "miles"},
                },
            },
        }

        query = inject_tenant_filter(base_query, tenant_id)

        response = await self._es.search_documents(
            IFTA_MILEAGE_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})

        # Parse jurisdiction buckets
        jurisdictions: List[JurisdictionMileage] = []
        buckets = aggs.get("by_jurisdiction", {}).get("buckets", [])
        for bucket in buckets:
            jurisdictions.append(
                JurisdictionMileage(
                    jurisdiction=bucket["key"],
                    total_miles=bucket["total_miles"]["value"],
                    taxable_miles=bucket["taxable_miles"]["value"],
                    segment_count=bucket["doc_count"],
                )
            )

        total_miles = aggs.get("total_miles_all", {}).get("value", 0.0)
        truck_count = aggs.get("unique_trucks", {}).get("value", 0)

        return IFTAReport(
            tenant_id=tenant_id,
            quarter=quarter,
            jurisdictions=jurisdictions,
            total_miles=total_miles,
            truck_count=truck_count,
        )

    # ------------------------------------------------------------------
    # Compute fleet MPG (placeholder — Task 12.7)
    # ------------------------------------------------------------------

    async def compute_fleet_mpg(
        self,
        tenant_id: str,
        quarter: str,
    ) -> float:
        """Compute the IFTA fleet average MPG for a quarter.

        Fleet MPG = total_miles / total_gallons across all qualified
        vehicles for the quarter.

        Note: Full implementation requiring fuel gallons aggregation from
        terminal BOLs and fuel card transactions is in task 12.7. This
        skeleton returns 0.0 until fuel data integration is complete.

        Args:
            tenant_id: Tenant scope for the computation.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            Fleet average MPG as a float. Returns 0.0 if insufficient data.

        Validates: Requirement 7.5
        """
        # Placeholder — full implementation in task 12.7
        # Will aggregate total_miles from ifta_mileage and total_gallons
        # from terminal_bols + fuel card transactions
        logger.info(
            "compute_fleet_mpg called for tenant=%s, quarter=%s "
            "(placeholder — full implementation in task 12.7)",
            tenant_id,
            quarter,
        )
        return 0.0
