"""IFTA Reporter — per-jurisdiction mileage recording and quarterly reporting.

Implements the ``IFTA_Reporter`` described in design §7 of the Fuel Compliance
Backbone spec. This service records trip segments when trucks cross state
boundaries (detected via Geotab GPS telemetry and the StateBoundaryDetector),
aggregates per-jurisdiction miles per truck per quarter, aggregates fuel
gallons purchased per jurisdiction per truck per quarter from terminal BOLs
and fuel card transactions, and generates quarterly IFTA reports.

Public methods:
    * ``record_trip_segment()`` — persists a trip segment to the
      ``ifta_mileage`` ES index when a state boundary crossing is detected.
    * ``get_mileage_by_jurisdiction()`` — queries aggregated miles per state
      for a given truck and quarter.
    * ``get_all_trucks_mileage()`` — queries per-truck, per-jurisdiction
      mileage for ALL trucks in the fleet for a given quarter.
    * ``get_fuel_gallons_by_jurisdiction()`` — aggregates total fuel gallons
      purchased per jurisdiction per truck per quarter from terminal BOLs
      and fuel card transactions.
    * ``generate_quarterly_report()`` — placeholder for full quarterly IFTA
      report generation (implemented in task 12.6).
    * ``compute_fleet_mpg()`` — computes fleet average MPG as
      total_miles / total_gallons across all qualified vehicles for the
      quarter (implemented in task 12.7).

All queries are tenant-scoped via ``inject_tenant_filter`` (Constraint C3).

    * ``record_manual_adjustment()`` — records a manual mileage adjustment
      with operator_id and reason for audit trail purposes.
    * ``get_adjustment_history()`` — queries all manual adjustment records
      for a given tenant/quarter, sorted by created_at descending.

Validates: Requirement 7.1, 7.2, 7.3, 7.7
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from compliance.services.compliance_es_mappings import (
    FUEL_CARD_TRANSACTIONS_INDEX,
    IFTA_MILEAGE_INDEX,
    TAX_JURISDICTIONS_INDEX,
    TERMINAL_BOLS_INDEX,
)
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


# 2-letter US state/territory code → 2-digit FIPS code. Used to resolve the
# state-level IFTA excise rate from the ``tax_jurisdictions`` index (which is
# keyed by FIPS). Covers the 50 states + DC.
_STATE_CODE_TO_FIPS: Dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}


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

    # ------------------------------------------------------------------
    # Uniform cross-module subject reference (cross-module-entity-linkage
    # task 10, Req 11.1). An IFTA trip segment is recorded against a truck,
    # which is a fleet **asset** (``truck_id == asset_id``); the uniform
    # ``subject_ref`` is a view over ``truck_id``.
    # ------------------------------------------------------------------
    @property
    def subject_ref(self) -> "SubjectRef":
        """The asset (truck) this segment was driven by, as a ``SubjectRef``."""
        from compliance.services.compliance_subject_ref import SubjectRef

        return SubjectRef(subject_type="asset", subject_id=self.truck_id)


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


class TruckJurisdictionMileage(BaseModel):
    """Per-truck, per-jurisdiction mileage breakdown for a quarter.

    Used by ``get_all_trucks_mileage`` to return each truck's state-by-state
    mileage for IFTA reporting across the entire fleet.

    Validates: Requirement 7.2
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str = Field(
        ..., description="Identifier of the truck"
    )
    jurisdictions: List[JurisdictionMileage] = Field(
        default_factory=list,
        description="Per-jurisdiction mileage breakdown for this truck",
    )
    total_miles: float = Field(
        default=0.0, description="Total miles across all jurisdictions for this truck"
    )


class JurisdictionFuelGallons(BaseModel):
    """Aggregated fuel gallons purchased in a single jurisdiction.

    Represents the total net gallons of fuel purchased in a specific
    jurisdiction from a single data source (terminal BOL or fuel card).
    Used by ``get_fuel_gallons_by_jurisdiction`` for IFTA fuel tax
    reporting.

    Validates: Requirement 7.3
    """

    model_config = ConfigDict(extra="forbid")

    jurisdiction: str = Field(
        ..., description="2-letter US state code where fuel was purchased"
    )
    total_gallons: float = Field(
        default=0.0, description="Total net gallons purchased in this jurisdiction"
    )
    source: str = Field(
        ..., description="Data source: terminal_bol | fuel_card"
    )
    truck_id: Optional[str] = Field(
        default=None,
        description="Truck identifier (when aggregated per-truck)",
    )
    transaction_count: int = Field(
        default=0, description="Number of transactions aggregated"
    )


class TruckFuelGallons(BaseModel):
    """Per-truck fuel gallons breakdown by jurisdiction for a quarter.

    Contains the per-jurisdiction fuel purchase totals for a single truck,
    combining data from terminal BOLs and fuel card transactions.

    Validates: Requirement 7.3
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str = Field(
        ..., description="Identifier of the truck"
    )
    jurisdictions: List[JurisdictionFuelGallons] = Field(
        default_factory=list,
        description="Per-jurisdiction fuel gallons breakdown for this truck",
    )
    total_gallons: float = Field(
        default=0.0,
        description="Total gallons across all jurisdictions for this truck",
    )


class JurisdictionIFTAEntry(BaseModel):
    """Per-jurisdiction IFTA entry for a single truck in a quarter.

    Contains the full IFTA breakdown for one state: total miles, taxable
    miles, fuel gallons paid in that state, net taxable gallons, tax rate,
    and computed tax due.

    Validates: Requirement 7.4
    """

    model_config = ConfigDict(extra="forbid")

    jurisdiction: str = Field(
        ..., description="2-letter US state code (e.g., 'TX', 'OK')"
    )
    total_miles: float = Field(
        default=0.0, description="Total miles driven in this jurisdiction"
    )
    taxable_miles: float = Field(
        default=0.0, description="Taxable miles in this jurisdiction"
    )
    tax_paid_gallons: float = Field(
        default=0.0,
        description="Gallons of fuel purchased (tax paid) in this jurisdiction",
    )
    net_taxable_gallons: float = Field(
        default=0.0,
        description="Net taxable gallons = (taxable_miles / fleet_mpg) - tax_paid_gallons",
    )
    tax_rate: float = Field(
        default=0.0,
        description="IFTA tax rate for this jurisdiction (cents per gallon), "
        "resolved from the tax_jurisdictions rate table. 0.0 when no "
        "state excise row is configured for the jurisdiction.",
    )
    tax_due: float = Field(
        default=0.0,
        description="Tax due = net_taxable_gallons × tax_rate (cents). "
        "May be negative when net_taxable_gallons is negative (a credit).",
    )


class TruckIFTASummary(BaseModel):
    """Per-truck IFTA summary for a quarter.

    Contains the truck's per-jurisdiction IFTA entries along with
    fleet-level totals for miles and gallons.

    Validates: Requirement 7.4
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str = Field(
        ..., description="Identifier of the truck"
    )
    jurisdictions: List[JurisdictionIFTAEntry] = Field(
        default_factory=list,
        description="Per-jurisdiction IFTA entries for this truck",
    )
    total_miles: float = Field(
        default=0.0, description="Total miles across all jurisdictions for this truck"
    )
    total_gallons: float = Field(
        default=0.0, description="Total fuel gallons purchased across all jurisdictions"
    )


class MileageAdjustment(BaseModel):
    """A manual mileage adjustment record with audit trail.

    Created when an operator manually adjusts mileage for a truck in a
    specific jurisdiction during quarterly review. Supports both positive
    (adding miles) and negative (subtracting miles) adjustments.

    Persisted to the ``ifta_mileage`` ES index with source="manual_adjustment"
    alongside automated records.

    Validates: Requirement 7.7
    """

    model_config = ConfigDict(extra="forbid")

    adjustment_id: str = Field(
        default_factory=lambda: f"adj_{uuid4()}",
        description="Server-assigned identifier of shape adj_<uuid4>",
    )
    tenant_id: str = Field(
        ..., description="Tenant identifier for data isolation"
    )
    truck_id: str = Field(
        ..., description="Identifier of the truck being adjusted"
    )
    jurisdiction: str = Field(
        ..., description="2-letter US state code (e.g., 'TX', 'OK')"
    )
    miles: float = Field(
        ..., description="Miles adjustment (positive to add, negative to subtract)"
    )
    quarter: str = Field(
        ..., description="Calendar quarter identifier, e.g. '2026-Q1'"
    )
    operator_id: str = Field(
        ..., description="Identifier of the operator making the adjustment"
    )
    reason: str = Field(
        ..., description="Reason for the manual adjustment (audit trail)"
    )
    created_at: datetime = Field(default_factory=utcnow)


class IncompleteDataFlag(BaseModel):
    """Flag indicating a truck has missing Geotab telemetry data for IFTA.

    Created when a truck in the fleet has no mileage data recorded in the
    ``ifta_mileage`` index for a given quarter. Flagged trucks are excluded
    from the automated IFTA return and the fleet manager is alerted.

    Validates: Requirement 7.6
    """

    model_config = ConfigDict(extra="forbid")

    truck_id: str = Field(
        ..., description="Identifier of the truck with missing data"
    )
    flag_type: str = Field(
        default="ifta_data_incomplete",
        description="Type of data completeness flag",
    )
    quarter: str = Field(
        ..., description="Calendar quarter with missing data, e.g. '2026-Q1'"
    )
    reason: str = Field(
        ..., description="Human-readable reason for the flag"
    )
    flagged_at: datetime = Field(
        default_factory=utcnow,
        description="Timestamp when the flag was created",
    )


class IFTAReport(BaseModel):
    """Quarterly IFTA report for a tenant's fleet.

    Contains per-truck, per-jurisdiction IFTA summaries for a given
    calendar quarter, including fleet MPG and per-truck breakdowns.

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
    trucks: List[TruckIFTASummary] = Field(
        default_factory=list,
        description="Per-truck IFTA summaries with jurisdiction breakdowns",
    )
    incomplete_trucks: List["IncompleteDataFlag"] = Field(
        default_factory=list,
        description="Trucks flagged as ifta_data_incomplete (excluded from report)",
    )
    jurisdictions: List[JurisdictionMileage] = Field(
        default_factory=list,
        description="Fleet-level per-jurisdiction mileage breakdown",
    )
    total_miles: float = Field(
        default=0.0, description="Total miles across all jurisdictions"
    )
    total_gallons: float = Field(
        default=0.0, description="Total fuel gallons across all trucks"
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


def _quarter_date_range(quarter: str) -> tuple:
    """Compute the start and end ISO date strings for a calendar quarter.

    Args:
        quarter: Quarter string in format "YYYY-QN" (e.g., "2026-Q1").

    Returns:
        Tuple of (start_date, end_date) as ISO date strings.
        start_date is inclusive, end_date is exclusive.
        E.g., ("2026-01-01", "2026-04-01") for Q1 2026.
    """
    parts = quarter.split("-Q")
    year = int(parts[0])
    quarter_num = int(parts[1])

    # Quarter start months: Q1=1, Q2=4, Q3=7, Q4=10
    start_month = (quarter_num - 1) * 3 + 1

    # End month is start of next quarter
    if quarter_num == 4:
        end_year = year + 1
        end_month = 1
    else:
        end_year = year
        end_month = start_month + 3

    start_date = f"{year:04d}-{start_month:02d}-01"
    end_date = f"{end_year:04d}-{end_month:02d}-01"

    return start_date, end_date


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
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"truck_id": truck_id}},
                        {"term": {"quarter": quarter}},
                    ]
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
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
        }

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
    # Get all trucks mileage (Task 12.4, Req 7.2)
    # ------------------------------------------------------------------

    async def get_all_trucks_mileage(
        self,
        tenant_id: str,
        quarter: str,
    ) -> List[TruckJurisdictionMileage]:
        """Query aggregated miles per jurisdiction for ALL trucks in a quarter.

        Uses a nested ES aggregation: first groups by ``truck_id``, then
        within each truck bucket, groups by ``jurisdiction`` with sum
        sub-aggregations on ``miles`` and ``taxable_miles``.

        This provides the per-truck, per-jurisdiction mileage breakdown
        needed for quarterly IFTA reporting across the entire fleet.

        Args:
            tenant_id: Tenant scope for the query.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            List of TruckJurisdictionMileage records, one per truck that
            has recorded mileage in the given quarter. Each entry contains
            the truck's per-state mileage breakdown.

        Validates: Requirement 7.2
        """
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"quarter": quarter}},
                    ]
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
            "by_truck": {
                "terms": {
                    "field": "truck_id",
                    "size": 500,  # Support large fleets
                },
                "aggs": {
                    "by_jurisdiction": {
                        "terms": {
                            "field": "jurisdiction",
                            "size": 60,  # All US states + territories
                        },
                        "aggs": {
                            "total_miles": {"sum": {"field": "miles"}},
                            "taxable_miles": {
                                "sum": {"field": "taxable_miles"}
                            },
                        },
                    },
                    "truck_total_miles": {"sum": {"field": "miles"}},
                },
            },
        }

        response = await self._es.search_documents(
            IFTA_MILEAGE_INDEX, query, size=0
        )

        # Parse nested aggregation results
        results: List[TruckJurisdictionMileage] = []
        aggs = response.get("aggregations", {})
        truck_buckets = aggs.get("by_truck", {}).get("buckets", [])

        for truck_bucket in truck_buckets:
            truck_id = truck_bucket["key"]
            truck_total = truck_bucket.get("truck_total_miles", {}).get(
                "value", 0.0
            )

            jurisdictions: List[JurisdictionMileage] = []
            jurisdiction_buckets = (
                truck_bucket.get("by_jurisdiction", {}).get("buckets", [])
            )

            for jur_bucket in jurisdiction_buckets:
                jurisdictions.append(
                    JurisdictionMileage(
                        jurisdiction=jur_bucket["key"],
                        total_miles=jur_bucket["total_miles"]["value"],
                        taxable_miles=jur_bucket["taxable_miles"]["value"],
                        segment_count=jur_bucket["doc_count"],
                    )
                )

            results.append(
                TruckJurisdictionMileage(
                    truck_id=truck_id,
                    jurisdictions=jurisdictions,
                    total_miles=truck_total,
                )
            )

        logger.info(
            "get_all_trucks_mileage: tenant=%s, quarter=%s, trucks=%d",
            tenant_id,
            quarter,
            len(results),
        )

        return results

    # ------------------------------------------------------------------
    # Get fuel gallons by jurisdiction (Task 12.5, Req 7.3)
    # ------------------------------------------------------------------

    async def get_fuel_gallons_by_jurisdiction(
        self,
        tenant_id: str,
        quarter: str,
    ) -> List[TruckFuelGallons]:
        """Aggregate total fuel gallons purchased per jurisdiction per truck per quarter.

        Combines data from two sources:
        1. **Terminal BOLs** — aggregates ``net_gallons`` from the
           ``terminal_bols`` index, grouped by ``truck_id`` and
           ``fuel_purchase_jurisdiction``. The quarter is determined by
           filtering on the ``issued_at`` timestamp field.
        2. **Fuel card transactions** — aggregates gallons from the
           ``fuel_card_transactions`` index (if it exists). Currently a
           placeholder that gracefully returns empty results when the
           index is not yet populated.

        The method merges results from both sources into a unified
        per-truck, per-jurisdiction breakdown.

        Args:
            tenant_id: Tenant scope for the query.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            List of TruckFuelGallons records, one per truck that has
            fuel purchase data in the given quarter. Each entry contains
            per-jurisdiction fuel gallons from all sources.

        Validates: Requirement 7.3
        """
        # Compute date range for the quarter
        start_date, end_date = _quarter_date_range(quarter)

        # --- Source 1: Terminal BOLs ---
        bol_results = await self._aggregate_terminal_bol_gallons(
            tenant_id, start_date, end_date
        )

        # --- Source 2: Fuel card transactions ---
        fuel_card_results = await self._aggregate_fuel_card_gallons(
            tenant_id, start_date, end_date
        )

        # --- Merge results from both sources ---
        merged = self._merge_fuel_gallons(bol_results, fuel_card_results)

        logger.info(
            "get_fuel_gallons_by_jurisdiction: tenant=%s, quarter=%s, "
            "trucks=%d, bol_records=%d, fuel_card_records=%d",
            tenant_id,
            quarter,
            len(merged),
            sum(len(t.jurisdictions) for t in bol_results),
            sum(len(t.jurisdictions) for t in fuel_card_results),
        )

        return merged

    async def _aggregate_terminal_bol_gallons(
        self,
        tenant_id: str,
        start_date: str,
        end_date: str,
    ) -> List[TruckFuelGallons]:
        """Aggregate net_gallons from terminal_bols by truck and jurisdiction.

        Queries the ``terminal_bols`` index for BOLs issued within the
        date range, groups by ``truck_id`` then by
        ``fuel_purchase_jurisdiction``, and sums ``net_gallons``.

        Args:
            tenant_id: Tenant scope.
            start_date: ISO date string for the start of the quarter.
            end_date: ISO date string for the end of the quarter.

        Returns:
            List of TruckFuelGallons from terminal BOL data.
        """
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "range": {
                                "issued_at": {
                                    "gte": start_date,
                                    "lt": end_date,
                                }
                            }
                        },
                    ],
                    "must_not": [
                        {"term": {"status": "rejected"}},
                    ],
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
            "by_truck": {
                "terms": {
                    "field": "truck_id",
                    "size": 500,
                },
                "aggs": {
                    "by_jurisdiction": {
                        "terms": {
                            "field": "fuel_purchase_jurisdiction",
                            "size": 60,
                        },
                        "aggs": {
                            "total_gallons": {
                                "sum": {"field": "net_gallons"}
                            },
                        },
                    },
                    "truck_total_gallons": {
                        "sum": {"field": "net_gallons"}
                    },
                },
            },
        }

        try:
            response = await self._es.search_documents(
                TERMINAL_BOLS_INDEX, query, size=0
            )
        except Exception as exc:
            logger.warning(
                "Failed to query terminal_bols for fuel gallons: %s", exc
            )
            return []

        # Parse nested aggregation results
        results: List[TruckFuelGallons] = []
        aggs = response.get("aggregations", {})
        truck_buckets = aggs.get("by_truck", {}).get("buckets", [])

        for truck_bucket in truck_buckets:
            truck_id = truck_bucket["key"]
            truck_total = truck_bucket.get("truck_total_gallons", {}).get(
                "value", 0.0
            )

            jurisdictions: List[JurisdictionFuelGallons] = []
            jur_buckets = (
                truck_bucket.get("by_jurisdiction", {}).get("buckets", [])
            )

            for jur_bucket in jur_buckets:
                jurisdictions.append(
                    JurisdictionFuelGallons(
                        jurisdiction=jur_bucket["key"],
                        total_gallons=jur_bucket["total_gallons"]["value"],
                        source="terminal_bol",
                        truck_id=truck_id,
                        transaction_count=jur_bucket["doc_count"],
                    )
                )

            results.append(
                TruckFuelGallons(
                    truck_id=truck_id,
                    jurisdictions=jurisdictions,
                    total_gallons=truck_total,
                )
            )

        return results

    async def _aggregate_fuel_card_gallons(
        self,
        tenant_id: str,
        start_date: str,
        end_date: str,
    ) -> List[TruckFuelGallons]:
        """Aggregate gallons from fuel_card_transactions by truck and jurisdiction.

        Queries the ``fuel_card_transactions`` index for transactions within
        the date range, groups by ``truck_id`` then by ``jurisdiction``,
        and sums ``gallons``.

        This is a placeholder that gracefully handles the case where the
        fuel_card_transactions index does not yet exist.

        Args:
            tenant_id: Tenant scope.
            start_date: ISO date string for the start of the quarter.
            end_date: ISO date string for the end of the quarter.

        Returns:
            List of TruckFuelGallons from fuel card data. Returns empty
            list if the index doesn't exist or query fails.
        """
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "range": {
                                "transaction_date": {
                                    "gte": start_date,
                                    "lt": end_date,
                                }
                            }
                        },
                    ],
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
            "by_truck": {
                "terms": {
                    "field": "truck_id",
                    "size": 500,
                },
                "aggs": {
                    "by_jurisdiction": {
                        "terms": {
                            "field": "jurisdiction",
                            "size": 60,
                        },
                        "aggs": {
                            "total_gallons": {
                                "sum": {"field": "gallons"}
                            },
                        },
                    },
                    "truck_total_gallons": {
                        "sum": {"field": "gallons"}
                    },
                },
            },
        }

        try:
            response = await self._es.search_documents(
                FUEL_CARD_TRANSACTIONS_INDEX, query, size=0
            )
        except Exception as exc:
            # Gracefully handle missing index or query failures
            logger.debug(
                "Fuel card transactions query failed (index may not exist): %s",
                exc,
            )
            return []

        # Parse nested aggregation results
        results: List[TruckFuelGallons] = []
        aggs = response.get("aggregations", {})
        truck_buckets = aggs.get("by_truck", {}).get("buckets", [])

        for truck_bucket in truck_buckets:
            truck_id = truck_bucket["key"]
            truck_total = truck_bucket.get("truck_total_gallons", {}).get(
                "value", 0.0
            )

            jurisdictions: List[JurisdictionFuelGallons] = []
            jur_buckets = (
                truck_bucket.get("by_jurisdiction", {}).get("buckets", [])
            )

            for jur_bucket in jur_buckets:
                jurisdictions.append(
                    JurisdictionFuelGallons(
                        jurisdiction=jur_bucket["key"],
                        total_gallons=jur_bucket["total_gallons"]["value"],
                        source="fuel_card",
                        truck_id=truck_id,
                        transaction_count=jur_bucket["doc_count"],
                    )
                )

            results.append(
                TruckFuelGallons(
                    truck_id=truck_id,
                    jurisdictions=jurisdictions,
                    total_gallons=truck_total,
                )
            )

        return results

    @staticmethod
    def _merge_fuel_gallons(
        bol_results: List[TruckFuelGallons],
        fuel_card_results: List[TruckFuelGallons],
    ) -> List[TruckFuelGallons]:
        """Merge fuel gallons from terminal BOLs and fuel card transactions.

        Combines per-truck results from both sources into a single list.
        If a truck appears in both sources, their jurisdiction entries are
        merged into one TruckFuelGallons record with combined totals.

        Args:
            bol_results: Fuel gallons from terminal BOLs.
            fuel_card_results: Fuel gallons from fuel card transactions.

        Returns:
            Merged list of TruckFuelGallons with combined data from both
            sources.
        """
        # Build a dict keyed by truck_id for merging
        truck_map: Dict[str, TruckFuelGallons] = {}

        for truck_data in bol_results:
            truck_map[truck_data.truck_id] = TruckFuelGallons(
                truck_id=truck_data.truck_id,
                jurisdictions=list(truck_data.jurisdictions),
                total_gallons=truck_data.total_gallons,
            )

        for truck_data in fuel_card_results:
            if truck_data.truck_id in truck_map:
                existing = truck_map[truck_data.truck_id]
                # Append fuel card jurisdictions to existing BOL jurisdictions
                merged_jurisdictions = list(existing.jurisdictions) + list(
                    truck_data.jurisdictions
                )
                merged_total = existing.total_gallons + truck_data.total_gallons
                truck_map[truck_data.truck_id] = TruckFuelGallons(
                    truck_id=truck_data.truck_id,
                    jurisdictions=merged_jurisdictions,
                    total_gallons=merged_total,
                )
            else:
                truck_map[truck_data.truck_id] = TruckFuelGallons(
                    truck_id=truck_data.truck_id,
                    jurisdictions=list(truck_data.jurisdictions),
                    total_gallons=truck_data.total_gallons,
                )

        return list(truck_map.values())

    # ------------------------------------------------------------------
    # Generate quarterly report (Task 12.6, Req 7.4)
    # ------------------------------------------------------------------

    async def _get_jurisdiction_tax_rates(
        self, tenant_id: str
    ) -> Dict[str, float]:
        """Return ``{state_code: rate_cents_per_gallon}`` for IFTA states.

        Resolves the state-level fuel excise rate for each jurisdiction
        from the ``tax_jurisdictions`` index (the same rate table the
        Tax_Engine uses), keyed by the 2-digit state FIPS code. Returns a
        mapping from the 2-letter state code (e.g. ``"TX"``) to the rate
        in cents per gallon so :meth:`generate_quarterly_report` can
        compute ``tax_due`` per jurisdiction.

        Rows that are not state-level excise rates are ignored. Failures
        are logged and yield an empty map so the report still renders
        (with zero tax) rather than failing outright.
        """
        fips_to_state = {fips: code for code, fips in _STATE_CODE_TO_FIPS.items()}

        base_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"jurisdiction_level": "state"}},
                        {"term": {"tax_type": "excise"}},
                    ]
                }
            },
            "size": 200,
        }
        query = inject_tenant_filter(base_query, tenant_id)

        rates: Dict[str, float] = {}
        try:
            resp = await self._es.search_documents(
                TAX_JURISDICTIONS_INDEX, query, 200
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            logger.warning(
                "IFTAReporter: failed to load jurisdiction tax rates for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return rates

        for hit in hits:
            source = hit.get("_source", {}) if isinstance(hit, dict) else {}
            fips = str(source.get("fips_code", "")).strip()
            state_code = fips_to_state.get(fips)
            rate = source.get("rate_cents_per_gallon")
            if state_code is None or rate is None:
                continue
            # Keep the highest excise rate seen for the state (defensive
            # against overlapping rows); typically there is exactly one.
            existing = rates.get(state_code)
            rate_val = float(rate)
            if existing is None or rate_val > existing:
                rates[state_code] = rate_val
        return rates

    async def generate_quarterly_report(
        self,
        tenant_id: str,
        quarter: str,
    ) -> IFTAReport:
        """Generate a quarterly IFTA report for the tenant's fleet.

        Produces a per-truck IFTA summary showing jurisdiction, total_miles,
        taxable_miles, tax_paid_gallons, net_taxable_gallons, tax_rate, and
        tax_due for each state.

        Trucks with missing Geotab data are flagged as
        ``ifta_data_incomplete`` and excluded from the automated return.
        The fleet manager is alerted when incomplete data is detected.

        Algorithm:
        1. Call ``check_data_completeness()`` to identify trucks with
           missing Geotab data and alert the fleet manager.
        2. Call ``get_all_trucks_mileage()`` to get per-truck, per-jurisdiction
           mileage data.
        3. Call ``get_fuel_gallons_by_jurisdiction()`` to get per-truck,
           per-jurisdiction fuel gallons purchased.
        4. Exclude flagged trucks from the report.
        5. Compute fleet MPG as total_miles / total_gallons across all
           qualified (non-flagged) trucks.
        6. For each truck, produce a per-jurisdiction summary with:
           - jurisdiction, total_miles, taxable_miles
           - tax_paid_gallons (fuel purchased in that state)
           - net_taxable_gallons = (taxable_miles / fleet_mpg) - tax_paid_gallons
           - tax_rate and tax_due (set to 0.0 until rate table integration)

        Args:
            tenant_id: Tenant scope for the report.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            IFTAReport with per-truck IFTA summaries and fleet-level totals.
            Includes incomplete_trucks list for flagged trucks.

        Validates: Requirement 7.4, 7.6
        """
        # Step 1: Check data completeness and flag trucks with missing data
        incomplete_flags = await self.check_data_completeness(
            tenant_id, quarter
        )
        flagged_truck_ids = {f.truck_id for f in incomplete_flags}

        # Step 2: Get per-truck, per-jurisdiction mileage
        trucks_mileage = await self.get_all_trucks_mileage(tenant_id, quarter)

        # Step 3: Get per-truck, per-jurisdiction fuel gallons
        trucks_fuel = await self.get_fuel_gallons_by_jurisdiction(
            tenant_id, quarter
        )

        # Step 3b: Load per-jurisdiction excise tax rates (cents/gallon)
        # so tax_due can be computed below. Empty when no rate table is
        # configured — the report still renders with zero tax.
        tax_rates = await self._get_jurisdiction_tax_rates(tenant_id)

        # Build a lookup: truck_id -> {jurisdiction -> total_gallons}
        # Exclude flagged trucks from fuel data
        fuel_lookup: Dict[str, Dict[str, float]] = {}
        total_fleet_gallons = 0.0
        for truck_fuel in trucks_fuel:
            if truck_fuel.truck_id in flagged_truck_ids:
                continue
            truck_jur_gallons: Dict[str, float] = {}
            for jur_fuel in truck_fuel.jurisdictions:
                existing = truck_jur_gallons.get(jur_fuel.jurisdiction, 0.0)
                truck_jur_gallons[jur_fuel.jurisdiction] = (
                    existing + jur_fuel.total_gallons
                )
            fuel_lookup[truck_fuel.truck_id] = truck_jur_gallons
            total_fleet_gallons += truck_fuel.total_gallons

        # Step 4: Filter out flagged trucks from mileage data
        qualified_trucks_mileage = [
            t for t in trucks_mileage if t.truck_id not in flagged_truck_ids
        ]

        # Step 5: Compute fleet MPG
        total_fleet_miles = sum(t.total_miles for t in qualified_trucks_mileage)
        fleet_mpg: Optional[float] = None
        if total_fleet_gallons > 0:
            fleet_mpg = total_fleet_miles / total_fleet_gallons

        # Step 6: Build per-truck IFTA summaries
        truck_summaries: List[TruckIFTASummary] = []
        fleet_jurisdictions: Dict[str, JurisdictionMileage] = {}

        for truck_mileage in qualified_trucks_mileage:
            truck_id = truck_mileage.truck_id
            truck_fuel_map = fuel_lookup.get(truck_id, {})
            truck_total_gallons = sum(truck_fuel_map.values())

            jurisdiction_entries: List[JurisdictionIFTAEntry] = []

            for jur_mileage in truck_mileage.jurisdictions:
                jurisdiction = jur_mileage.jurisdiction
                total_miles = jur_mileage.total_miles
                taxable_miles = jur_mileage.taxable_miles

                # Fuel purchased (tax paid) in this jurisdiction
                tax_paid_gallons = truck_fuel_map.get(jurisdiction, 0.0)

                # Net taxable gallons:
                # = gallons consumed in jurisdiction - gallons purchased there
                # = (taxable_miles / fleet_mpg) - tax_paid_gallons
                net_taxable_gallons = 0.0
                if fleet_mpg and fleet_mpg > 0:
                    gallons_consumed = taxable_miles / fleet_mpg
                    net_taxable_gallons = gallons_consumed - tax_paid_gallons

                # Tax rate (cents/gallon) resolved from the jurisdiction
                # rate table; tax_due is net_taxable_gallons × rate, in
                # cents, rounded to 2 dp. Negative net gallons (credits)
                # produce a negative tax_due, matching IFTA net-settlement.
                tax_rate = tax_rates.get(jurisdiction, 0.0)
                tax_due = round(net_taxable_gallons * tax_rate, 2)

                jurisdiction_entries.append(
                    JurisdictionIFTAEntry(
                        jurisdiction=jurisdiction,
                        total_miles=total_miles,
                        taxable_miles=taxable_miles,
                        tax_paid_gallons=tax_paid_gallons,
                        net_taxable_gallons=net_taxable_gallons,
                        tax_rate=tax_rate,
                        tax_due=tax_due,
                    )
                )

                # Accumulate fleet-level jurisdiction totals
                if jurisdiction in fleet_jurisdictions:
                    existing = fleet_jurisdictions[jurisdiction]
                    fleet_jurisdictions[jurisdiction] = JurisdictionMileage(
                        jurisdiction=jurisdiction,
                        total_miles=existing.total_miles + total_miles,
                        taxable_miles=existing.taxable_miles + taxable_miles,
                        segment_count=existing.segment_count
                        + jur_mileage.segment_count,
                    )
                else:
                    fleet_jurisdictions[jurisdiction] = JurisdictionMileage(
                        jurisdiction=jurisdiction,
                        total_miles=total_miles,
                        taxable_miles=taxable_miles,
                        segment_count=jur_mileage.segment_count,
                    )

            truck_summaries.append(
                TruckIFTASummary(
                    truck_id=truck_id,
                    jurisdictions=jurisdiction_entries,
                    total_miles=truck_mileage.total_miles,
                    total_gallons=truck_total_gallons,
                )
            )

        report = IFTAReport(
            tenant_id=tenant_id,
            quarter=quarter,
            fleet_mpg=fleet_mpg,
            trucks=truck_summaries,
            incomplete_trucks=incomplete_flags,
            jurisdictions=list(fleet_jurisdictions.values()),
            total_miles=total_fleet_miles,
            total_gallons=total_fleet_gallons,
            truck_count=len(qualified_trucks_mileage),
        )

        logger.info(
            "Generated IFTA quarterly report: tenant=%s, quarter=%s, "
            "trucks=%d, incomplete=%d, total_miles=%.1f, total_gallons=%.1f, "
            "fleet_mpg=%s",
            tenant_id,
            quarter,
            len(truck_summaries),
            len(incomplete_flags),
            total_fleet_miles,
            total_fleet_gallons,
            f"{fleet_mpg:.2f}" if fleet_mpg else "N/A",
        )

        return report

    # ------------------------------------------------------------------
    # Compute fleet MPG (Task 12.7, Req 7.5)
    # ------------------------------------------------------------------

    async def compute_fleet_mpg(
        self,
        tenant_id: str,
        quarter: str,
    ) -> float:
        """Compute the IFTA fleet average MPG for a quarter.

        Fleet MPG = total_miles / total_gallons across all qualified
        vehicles for the quarter.

        Aggregates total miles from the ``ifta_mileage`` index (sum of all
        miles for the tenant/quarter) and total fuel gallons from
        ``get_fuel_gallons_by_jurisdiction()`` (sum of all gallons from
        terminal BOLs and fuel card transactions).

        Args:
            tenant_id: Tenant scope for the computation.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            Fleet average MPG as a float. Returns 0.0 if total_gallons
            is zero (avoids division by zero).

        Validates: Requirement 7.5
        """
        # Step 1: Query total miles from ifta_mileage index
        total_miles = await self._get_total_miles(tenant_id, quarter)

        # Step 2: Query total fuel gallons from all sources
        trucks_fuel = await self.get_fuel_gallons_by_jurisdiction(
            tenant_id, quarter
        )
        total_gallons = sum(truck.total_gallons for truck in trucks_fuel)

        # Step 3: Compute fleet MPG (guard against division by zero)
        if total_gallons == 0:
            logger.info(
                "compute_fleet_mpg: tenant=%s, quarter=%s — "
                "total_gallons=0, returning 0.0",
                tenant_id,
                quarter,
            )
            return 0.0

        fleet_mpg = total_miles / total_gallons

        logger.info(
            "compute_fleet_mpg: tenant=%s, quarter=%s, "
            "total_miles=%.1f, total_gallons=%.1f, fleet_mpg=%.4f",
            tenant_id,
            quarter,
            total_miles,
            total_gallons,
            fleet_mpg,
        )

        return fleet_mpg

    async def _get_total_miles(
        self,
        tenant_id: str,
        quarter: str,
    ) -> float:
        """Query the total miles from the ifta_mileage index for a tenant/quarter.

        Uses an ES sum aggregation on the ``miles`` field, filtered by
        tenant and quarter.

        Args:
            tenant_id: Tenant scope for the query.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            Total miles as a float. Returns 0.0 if no data exists.
        """
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"quarter": quarter}},
                    ]
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
            "total_miles": {"sum": {"field": "miles"}},
        }

        response = await self._es.search_documents(
            IFTA_MILEAGE_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        total_miles = aggs.get("total_miles", {}).get("value", 0.0)

        return total_miles

    # ------------------------------------------------------------------
    # Check data completeness (Task 12.8, Req 7.6)
    # ------------------------------------------------------------------

    async def check_data_completeness(
        self,
        tenant_id: str,
        quarter: str,
    ) -> List[IncompleteDataFlag]:
        """Identify trucks with missing Geotab mileage data for a quarter.

        Compares the full list of trucks in the fleet (from the ``trucks``
        ES index) against trucks that have recorded mileage in the
        ``ifta_mileage`` index for the given quarter. Trucks with no
        mileage data are flagged as ``ifta_data_incomplete``.

        When flagged trucks are found and a notification_service is
        configured, an alert is sent to the fleet manager listing the
        trucks with missing data.

        Args:
            tenant_id: Tenant scope for the query.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            List of IncompleteDataFlag records for trucks missing mileage
            data. Returns an empty list if all trucks have data.

        Validates: Requirement 7.6
        """
        # Step 1: Get all truck IDs in the fleet
        all_truck_ids = await self._get_fleet_truck_ids(tenant_id)

        if not all_truck_ids:
            logger.info(
                "check_data_completeness: tenant=%s, quarter=%s — "
                "no trucks found in fleet, nothing to check",
                tenant_id,
                quarter,
            )
            return []

        # Step 2: Get truck IDs that have mileage data for this quarter
        trucks_with_data = await self._get_trucks_with_mileage_data(
            tenant_id, quarter
        )

        # Step 3: Identify trucks with NO mileage data
        missing_truck_ids = all_truck_ids - trucks_with_data

        if not missing_truck_ids:
            logger.info(
                "check_data_completeness: tenant=%s, quarter=%s — "
                "all %d trucks have mileage data",
                tenant_id,
                quarter,
                len(all_truck_ids),
            )
            return []

        # Step 4: Create IncompleteDataFlag records
        flags: List[IncompleteDataFlag] = []
        for truck_id in sorted(missing_truck_ids):
            flags.append(
                IncompleteDataFlag(
                    truck_id=truck_id,
                    flag_type="ifta_data_incomplete",
                    quarter=quarter,
                    reason=(
                        f"No Geotab odometer/mileage data recorded for "
                        f"truck {truck_id} during {quarter}"
                    ),
                )
            )

        logger.warning(
            "check_data_completeness: tenant=%s, quarter=%s — "
            "%d truck(s) flagged as ifta_data_incomplete: %s",
            tenant_id,
            quarter,
            len(flags),
            [f.truck_id for f in flags],
        )

        # Step 5: Alert fleet manager if notification service is configured
        if self._notification_service and flags:
            await self._alert_fleet_manager_incomplete_data(
                tenant_id, quarter, flags
            )

        return flags

    async def _get_fleet_truck_ids(self, tenant_id: str) -> set:
        """Get all truck IDs registered in the fleet for a tenant.

        Queries the ``trucks`` ES index to retrieve all truck_id values
        for the given tenant.

        Args:
            tenant_id: Tenant scope for the query.

        Returns:
            Set of truck_id strings.
        """
        inner_query: Dict[str, Any] = {
            "query": {"match_all": {}},
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
            "truck_ids": {
                "terms": {
                    "field": "truck_id",
                    "size": 1000,
                }
            }
        }

        try:
            response = await self._es.search_documents(
                "trucks", query, size=0
            )
        except Exception as exc:
            logger.warning(
                "Failed to query trucks index for fleet truck IDs: %s", exc
            )
            return set()

        aggs = response.get("aggregations", {})
        buckets = aggs.get("truck_ids", {}).get("buckets", [])

        return {bucket["key"] for bucket in buckets}

    async def _get_trucks_with_mileage_data(
        self, tenant_id: str, quarter: str
    ) -> set:
        """Get truck IDs that have mileage data for a given quarter.

        Queries the ``ifta_mileage`` index with a terms aggregation on
        ``truck_id`` to find which trucks have recorded trip segments.

        Args:
            tenant_id: Tenant scope for the query.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            Set of truck_id strings that have mileage data.
        """
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"quarter": quarter}},
                    ]
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["size"] = 0
        query["aggs"] = {
            "truck_ids": {
                "terms": {
                    "field": "truck_id",
                    "size": 1000,
                }
            }
        }

        response = await self._es.search_documents(
            IFTA_MILEAGE_INDEX, query, size=0
        )

        aggs = response.get("aggregations", {})
        buckets = aggs.get("truck_ids", {}).get("buckets", [])

        return {bucket["key"] for bucket in buckets}

    async def _alert_fleet_manager_incomplete_data(
        self,
        tenant_id: str,
        quarter: str,
        flags: List[IncompleteDataFlag],
    ) -> None:
        """Send an alert to the fleet manager about trucks with missing data.

        Uses the notification service to alert the fleet manager that
        certain trucks are missing Geotab telemetry data and have been
        excluded from the automated IFTA return.

        Args:
            tenant_id: Tenant scope.
            quarter: Calendar quarter with missing data.
            flags: List of IncompleteDataFlag records for flagged trucks.
        """
        try:
            truck_ids = [f.truck_id for f in flags]
            await self._notification_service.notify_event(
                event_type="ifta_data_incomplete",
                event_data={
                    "tenant_id": tenant_id,
                    "quarter": quarter,
                    "flagged_truck_ids": truck_ids,
                    "flagged_count": len(flags),
                    "message": (
                        f"{len(flags)} truck(s) have missing Geotab odometer "
                        f"data for {quarter} and have been excluded from the "
                        f"automated IFTA return: {', '.join(truck_ids)}"
                    ),
                },
                tenant_id=tenant_id,
            )
            logger.info(
                "Sent ifta_data_incomplete alert to fleet manager: "
                "tenant=%s, quarter=%s, trucks=%s",
                tenant_id,
                quarter,
                truck_ids,
            )
        except Exception as exc:
            logger.error(
                "Failed to send ifta_data_incomplete alert: tenant=%s, "
                "quarter=%s, error=%s",
                tenant_id,
                quarter,
                exc,
            )

    # ------------------------------------------------------------------
    # Manual mileage adjustment (Task 12.9, Req 7.7)
    # ------------------------------------------------------------------

    async def record_manual_adjustment(
        self,
        tenant_id: str,
        truck_id: str,
        jurisdiction: str,
        miles: float,
        quarter: str,
        operator_id: str,
        reason: str,
    ) -> MileageAdjustment:
        """Record a manual mileage adjustment with an audit trail.

        Creates a new record in the ``ifta_mileage`` ES index with
        ``source="manual_adjustment"`` alongside automated records.
        Supports both positive adjustments (adding miles) and negative
        adjustments (subtracting miles) for corrections identified during
        quarterly review.

        The operator_id and reason are persisted in the document for
        audit purposes, providing a complete trail of who made the
        adjustment and why.

        Args:
            tenant_id: Tenant scope for the record.
            truck_id: Identifier of the truck being adjusted.
            jurisdiction: 2-letter US state code (e.g., "TX", "OK").
            miles: Miles to adjust (positive to add, negative to subtract).
            quarter: Calendar quarter string (e.g., "2026-Q1").
            operator_id: Identifier of the operator making the adjustment.
            reason: Human-readable reason for the adjustment.

        Returns:
            The persisted MileageAdjustment record.

        Validates: Requirement 7.7
        """
        adjustment = MileageAdjustment(
            tenant_id=tenant_id,
            truck_id=truck_id,
            jurisdiction=jurisdiction.upper(),
            miles=miles,
            quarter=quarter,
            operator_id=operator_id,
            reason=reason,
        )

        # Serialize for ES — stored in ifta_mileage index alongside
        # automated trip segments
        doc: Dict[str, Any] = {
            "record_id": adjustment.adjustment_id,
            "tenant_id": adjustment.tenant_id,
            "truck_id": adjustment.truck_id,
            "jurisdiction": adjustment.jurisdiction,
            "miles": adjustment.miles,
            "quarter": adjustment.quarter,
            "source": "manual_adjustment",
            "operator_id": adjustment.operator_id,
            "reason": adjustment.reason,
            "timestamp": adjustment.created_at.isoformat(),
            "created_at": adjustment.created_at.isoformat(),
            "updated_at": adjustment.created_at.isoformat(),
            "taxable_miles": adjustment.miles,
        }

        await self._es.index_document(
            IFTA_MILEAGE_INDEX, adjustment.adjustment_id, doc
        )

        logger.info(
            "Recorded manual mileage adjustment: truck=%s, jurisdiction=%s, "
            "miles=%.1f, quarter=%s, operator=%s, reason='%s', tenant=%s",
            truck_id,
            jurisdiction,
            miles,
            quarter,
            operator_id,
            reason,
            tenant_id,
        )

        return adjustment

    async def get_adjustment_history(
        self,
        tenant_id: str,
        quarter: str,
    ) -> List[MileageAdjustment]:
        """Query all manual adjustment records for a tenant and quarter.

        Returns all records in the ``ifta_mileage`` index with
        ``source="manual_adjustment"`` for the given tenant and quarter,
        sorted by ``created_at`` descending (most recent first).

        Args:
            tenant_id: Tenant scope for the query.
            quarter: Calendar quarter string (e.g., "2026-Q1").

        Returns:
            List of MileageAdjustment records sorted by created_at
            descending.

        Validates: Requirement 7.7
        """
        inner_query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"quarter": quarter}},
                        {"term": {"source": "manual_adjustment"}},
                    ]
                }
            },
        }

        query = inject_tenant_filter(inner_query, tenant_id)
        query["sort"] = [{"created_at": {"order": "desc"}}]

        response = await self._es.search_documents(
            IFTA_MILEAGE_INDEX, query, size=_MAX_PAGE_LIMIT
        )

        hits = response.get("hits", {}).get("hits", [])
        adjustments: List[MileageAdjustment] = []

        for hit in hits:
            source = hit.get("_source", {})
            adjustments.append(
                MileageAdjustment(
                    adjustment_id=source.get("record_id", ""),
                    tenant_id=source.get("tenant_id", ""),
                    truck_id=source.get("truck_id", ""),
                    jurisdiction=source.get("jurisdiction", ""),
                    miles=source.get("miles", 0.0),
                    quarter=source.get("quarter", ""),
                    operator_id=source.get("operator_id", ""),
                    reason=source.get("reason", ""),
                    created_at=datetime.fromisoformat(
                        source.get("created_at", utcnow().isoformat())
                    ),
                )
            )

        logger.info(
            "get_adjustment_history: tenant=%s, quarter=%s, count=%d",
            tenant_id,
            quarter,
            len(adjustments),
        )

        return adjustments
