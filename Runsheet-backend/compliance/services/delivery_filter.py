"""Delivery Filter Service — partitions delivery candidates by customer call type.

Implements the ``Delivery_Filter`` described in design §14 of the
Fuel Compliance Backbone spec. This service partitions delivery candidates
into three groups based on customer call type before route construction:

* ``will_call`` — customer-initiated orders with explicit ready_for_dispatch status
* ``auto_fill`` — system-triggered deliveries based on tank forecast
* ``keep_full`` — system-triggered deliveries where tank level is below threshold

The filter is called at the top of ``Route_Planning_Agent.monitor_cycle()``
before the optimization solver runs.

Validates: Requirement 14.1
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.time_utils import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default keep_full threshold (Req 14.4)
DEFAULT_KEEP_FULL_THRESHOLD_PERCENT = 30.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CustomerType(str, Enum):
    """Customer call type classification for delivery candidates."""

    WILL_CALL = "will_call"
    AUTO_FILL = "auto_fill"
    KEEP_FULL = "keep_full"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DeliveryCandidate(BaseModel):
    """A delivery candidate to be partitioned by the DeliveryFilter.

    Represents a potential delivery that the Route_Planning_Agent considers
    for inclusion in a daily route plan. The ``customer_type`` field
    determines which partition the candidate belongs to.

    Validates: Requirement 14.1
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    customer_id: str
    customer_type: CustomerType
    order_id: Optional[str] = None
    order_status: Optional[str] = None
    tank_level_percent: Optional[float] = None
    reorder_point_percent: Optional[float] = None
    forecast_days_to_empty: Optional[float] = None
    planning_horizon_days: Optional[float] = None


class ExcludedCandidate(BaseModel):
    """A candidate excluded from all partitions with a reason."""

    model_config = ConfigDict(extra="forbid")

    candidate: DeliveryCandidate
    reason: str


class FilteredCandidates(BaseModel):
    """Result of partitioning delivery candidates by customer call type.

    Contains three lists corresponding to the three call types, plus an
    ``excluded`` list for candidates that did not pass validation for
    their respective partition.

    Validates: Requirement 14.1
    """

    model_config = ConfigDict(extra="forbid")

    will_call: List[DeliveryCandidate] = Field(default_factory=list)
    auto_fill: List[DeliveryCandidate] = Field(default_factory=list)
    keep_full: List[DeliveryCandidate] = Field(default_factory=list)
    excluded: List[ExcludedCandidate] = Field(default_factory=list)
    partitioned_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DeliveryFilter:
    """Service that partitions delivery candidates by customer call type.

    Called at the top of ``Route_Planning_Agent.monitor_cycle()`` before
    the optimization solver runs. Replaces the current unfiltered candidate
    list with partitioned groups.

    The basic partitioning (Task 15.1) routes candidates to their respective
    lists based on ``customer_type``. Detailed validation logic for each
    partition (checking order status, tank levels, forecast data) is
    implemented in Tasks 15.2–15.4.

    Validates: Requirement 14.1
    """

    def __init__(self) -> None:
        """Initialize the DeliveryFilter service."""
        logger.info("DeliveryFilter initialized")

    async def partition_candidates(
        self, candidates: List[DeliveryCandidate]
    ) -> FilteredCandidates:
        """Partition delivery candidates into will_call, auto_fill, and keep_full groups.

        Routes each candidate to the appropriate list based on its
        ``customer_type`` field. Applies validation rules per partition:

        - **will_call**: Must have a non-empty ``order_id`` and
          ``order_status == "ready_for_dispatch"`` (Req 14.2, 14.6).
        - **auto_fill**: Tank forecast validation (Task 15.3).
        - **keep_full**: Tank level threshold validation (Task 15.4).

        Candidates that fail validation are placed in the ``excluded``
        list with a descriptive reason.

        Args:
            candidates: List of delivery candidates to partition.

        Returns:
            FilteredCandidates with will_call, auto_fill, keep_full,
            and excluded lists populated.

        Validates: Requirement 14.1, 14.2, 14.6
        """
        will_call: List[DeliveryCandidate] = []
        auto_fill: List[DeliveryCandidate] = []
        keep_full: List[DeliveryCandidate] = []
        excluded: List[ExcludedCandidate] = []

        for candidate in candidates:
            if candidate.customer_type == CustomerType.WILL_CALL:
                exclusion_reason = self._validate_will_call(candidate)
                if exclusion_reason is None:
                    will_call.append(candidate)
                else:
                    excluded.append(
                        ExcludedCandidate(
                            candidate=candidate,
                            reason=exclusion_reason,
                        )
                    )
            elif candidate.customer_type == CustomerType.AUTO_FILL:
                exclusion_reason = self._validate_auto_fill(candidate)
                if exclusion_reason is None:
                    auto_fill.append(candidate)
                else:
                    excluded.append(
                        ExcludedCandidate(
                            candidate=candidate,
                            reason=exclusion_reason,
                        )
                    )
            elif candidate.customer_type == CustomerType.KEEP_FULL:
                exclusion_reason = self._validate_keep_full(candidate)
                if exclusion_reason is None:
                    keep_full.append(candidate)
                else:
                    excluded.append(
                        ExcludedCandidate(
                            candidate=candidate,
                            reason=exclusion_reason,
                        )
                    )
            else:
                excluded.append(
                    ExcludedCandidate(
                        candidate=candidate,
                        reason=f"Unrecognized customer_type: {candidate.customer_type}",
                    )
                )

        result = FilteredCandidates(
            will_call=will_call,
            auto_fill=auto_fill,
            keep_full=keep_full,
            excluded=excluded,
        )

        # --- Per-call-type structured logging (Req 14.7) ---
        self._log_filtering_outcome(candidates, result)

        return result

    def _log_filtering_outcome(
        self,
        candidates: List[DeliveryCandidate],
        result: FilteredCandidates,
    ) -> None:
        """Log the filtering outcome per call type for dispatcher visibility and audit.

        Emits one structured log line per call type showing candidates_in,
        candidates_out, excluded_count, and exclusion reasons (if any).
        Also emits an overall summary line.

        Validates: Requirement 14.7
        """
        # Count candidates_in per call type
        will_call_in = sum(
            1 for c in candidates if c.customer_type == CustomerType.WILL_CALL
        )
        auto_fill_in = sum(
            1 for c in candidates if c.customer_type == CustomerType.AUTO_FILL
        )
        keep_full_in = sum(
            1 for c in candidates if c.customer_type == CustomerType.KEEP_FULL
        )

        # Count excluded per call type and collect reasons
        will_call_excluded = [
            e for e in result.excluded
            if e.candidate.customer_type == CustomerType.WILL_CALL
        ]
        auto_fill_excluded = [
            e for e in result.excluded
            if e.candidate.customer_type == CustomerType.AUTO_FILL
        ]
        keep_full_excluded = [
            e for e in result.excluded
            if e.candidate.customer_type == CustomerType.KEEP_FULL
        ]

        # Log per call type
        self._log_call_type_outcome(
            call_type="will_call",
            candidates_in=will_call_in,
            candidates_out=len(result.will_call),
            excluded_items=will_call_excluded,
        )
        self._log_call_type_outcome(
            call_type="auto_fill",
            candidates_in=auto_fill_in,
            candidates_out=len(result.auto_fill),
            excluded_items=auto_fill_excluded,
        )
        self._log_call_type_outcome(
            call_type="keep_full",
            candidates_in=keep_full_in,
            candidates_out=len(result.keep_full),
            excluded_items=keep_full_excluded,
        )

        # Overall summary
        total_in = len(candidates)
        total_out = len(result.will_call) + len(result.auto_fill) + len(result.keep_full)
        total_excluded = len(result.excluded)
        logger.info(
            "delivery_filter.summary "
            "total_in=%d total_out=%d total_excluded=%d",
            total_in,
            total_out,
            total_excluded,
        )

    def _log_call_type_outcome(
        self,
        *,
        call_type: str,
        candidates_in: int,
        candidates_out: int,
        excluded_items: List[ExcludedCandidate],
    ) -> None:
        """Emit a structured log line for a single call type's filtering outcome.

        Validates: Requirement 14.7
        """
        excluded_count = candidates_in - candidates_out
        # Summarize unique exclusion reasons
        reasons = list({e.reason.split(":")[0].strip() for e in excluded_items}) if excluded_items else []
        reason_summary = "; ".join(sorted(reasons)) if reasons else "none"

        logger.info(
            "delivery_filter.outcome "
            "call_type=%s candidates_in=%d candidates_out=%d "
            "excluded_count=%d reasons=%s",
            call_type,
            candidates_in,
            candidates_out,
            excluded_count,
            reason_summary,
        )

    # ------------------------------------------------------------------
    # Will-Call Validation (Req 14.2, 14.6)
    # ------------------------------------------------------------------

    def _validate_will_call(self, candidate: DeliveryCandidate) -> Optional[str]:
        """Validate a will_call candidate has an explicit ready_for_dispatch order.

        A will_call candidate is only eligible for route planning if it has:
        1. A non-empty ``order_id`` (explicit customer order exists)
        2. An ``order_status`` of ``"ready_for_dispatch"``

        Args:
            candidate: The will_call delivery candidate to validate.

        Returns:
            None if the candidate is valid, or a string reason for exclusion.

        Validates: Requirement 14.2, 14.6
        """
        if not candidate.order_id:
            return (
                "will_call candidate excluded: no explicit customer order "
                f"(order_id is missing) for candidate {candidate.candidate_id}"
            )

        if candidate.order_status != "ready_for_dispatch":
            return (
                "will_call candidate excluded: order_status is "
                f"'{candidate.order_status}', expected 'ready_for_dispatch' "
                f"for candidate {candidate.candidate_id}"
            )

        return None

    # ------------------------------------------------------------------
    # Keep-Full Validation (Req 14.4)
    # ------------------------------------------------------------------

    def _validate_keep_full(self, candidate: DeliveryCandidate) -> Optional[str]:
        """Validate a keep_full candidate has tank level below the keep_full threshold.

        A keep_full candidate is only eligible for route planning if:
        1. ``tank_level_percent`` is set (not None) — the current tank level
           is known.
        2. ``tank_level_percent < DEFAULT_KEEP_FULL_THRESHOLD_PERCENT`` (30%) —
           the tank level is below the keep_full threshold, indicating the tank
           needs to be topped up regardless of forecast.

        Args:
            candidate: The keep_full delivery candidate to validate.

        Returns:
            None if the candidate is valid, or a string reason for exclusion.

        Validates: Requirement 14.4
        """
        if candidate.tank_level_percent is None:
            return (
                "keep_full candidate excluded: tank_level_percent is not set "
                f"(current tank level unknown) for candidate {candidate.candidate_id}"
            )

        if candidate.tank_level_percent >= DEFAULT_KEEP_FULL_THRESHOLD_PERCENT:
            return (
                "keep_full candidate excluded: tank_level_percent "
                f"({candidate.tank_level_percent}%) is not below keep_full threshold "
                f"({DEFAULT_KEEP_FULL_THRESHOLD_PERCENT}%) "
                f"for candidate {candidate.candidate_id}"
            )

        return None

    # ------------------------------------------------------------------
    # Auto-Fill Validation (Req 14.3)
    # ------------------------------------------------------------------

    def _validate_auto_fill(self, candidate: DeliveryCandidate) -> Optional[str]:
        """Validate an auto_fill candidate has valid forecast data within planning horizon.

        An auto_fill candidate is only eligible for route planning if:
        1. ``forecast_days_to_empty`` is set (not None) — the tank forecasting
           agent has predicted when the tank level will drop below the reorder point.
        2. ``planning_horizon_days`` is set (not None) — the planning horizon
           is configured for this candidate.
        3. ``forecast_days_to_empty <= planning_horizon_days`` — the predicted
           tank level will drop below the reorder point within the planning horizon.

        Args:
            candidate: The auto_fill delivery candidate to validate.

        Returns:
            None if the candidate is valid, or a string reason for exclusion.

        Validates: Requirement 14.3
        """
        if candidate.forecast_days_to_empty is None:
            return (
                "auto_fill candidate excluded: forecast_days_to_empty is not set "
                f"(tank forecast data missing) for candidate {candidate.candidate_id}"
            )

        if candidate.planning_horizon_days is None:
            return (
                "auto_fill candidate excluded: planning_horizon_days is not set "
                f"(planning horizon not configured) for candidate {candidate.candidate_id}"
            )

        if candidate.forecast_days_to_empty > candidate.planning_horizon_days:
            return (
                "auto_fill candidate excluded: forecast_days_to_empty "
                f"({candidate.forecast_days_to_empty}) exceeds planning_horizon_days "
                f"({candidate.planning_horizon_days}) — tank will not drop below "
                f"reorder point within planning horizon for candidate {candidate.candidate_id}"
            )

        return None
