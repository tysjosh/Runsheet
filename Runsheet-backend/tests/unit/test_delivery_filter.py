"""Unit tests for DeliveryFilter service.

Tests cover:
- Service initialization
- Basic partitioning by customer_type (will_call, auto_fill, keep_full)
- Empty candidate list
- Mixed candidate types
- All candidates of a single type
- FilteredCandidates model structure

Validates: Requirement 14.1
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from compliance.services.delivery_filter import (
    CustomerType,
    DeliveryCandidate,
    DeliveryFilter,
    ExcludedCandidate,
    FilteredCandidates,
    DEFAULT_KEEP_FULL_THRESHOLD_PERCENT,
)


# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    *,
    candidate_id: str = "cand_001",
    customer_id: str = "cust_001",
    customer_type: str = "will_call",
    order_id: str | None = None,
    order_status: str | None = None,
    tank_level_percent: float | None = None,
    reorder_point_percent: float | None = None,
    forecast_days_to_empty: float | None = None,
    planning_horizon_days: float | None = None,
) -> DeliveryCandidate:
    """Build a DeliveryCandidate for testing."""
    return DeliveryCandidate(
        candidate_id=candidate_id,
        customer_id=customer_id,
        customer_type=customer_type,
        order_id=order_id,
        order_status=order_status,
        tank_level_percent=tank_level_percent,
        reorder_point_percent=reorder_point_percent,
        forecast_days_to_empty=forecast_days_to_empty,
        planning_horizon_days=planning_horizon_days,
    )


# ---------------------------------------------------------------------------
# Tests: Service Initialization
# ---------------------------------------------------------------------------


class TestDeliveryFilterInit:
    """Tests for DeliveryFilter initialization."""

    def test_instantiation(self):
        """DeliveryFilter can be instantiated without arguments."""
        service = DeliveryFilter()
        assert service is not None

    def test_has_partition_candidates_method(self):
        """DeliveryFilter exposes partition_candidates method."""
        service = DeliveryFilter()
        assert hasattr(service, "partition_candidates")
        assert callable(service.partition_candidates)


# ---------------------------------------------------------------------------
# Tests: Model Validation
# ---------------------------------------------------------------------------


class TestDeliveryCandidateModel:
    """Tests for the DeliveryCandidate Pydantic model."""

    def test_valid_will_call_candidate(self):
        """A will_call candidate with required fields is valid."""
        candidate = _make_candidate(
            customer_type="will_call",
            order_id="order_123",
            order_status="ready_for_dispatch",
        )
        assert candidate.customer_type == CustomerType.WILL_CALL
        assert candidate.order_id == "order_123"

    def test_valid_auto_fill_candidate(self):
        """An auto_fill candidate with forecast data is valid."""
        candidate = _make_candidate(
            customer_type="auto_fill",
            tank_level_percent=45.0,
            reorder_point_percent=25.0,
            forecast_days_to_empty=5.0,
            planning_horizon_days=7.0,
        )
        assert candidate.customer_type == CustomerType.AUTO_FILL
        assert candidate.tank_level_percent == 45.0

    def test_valid_keep_full_candidate(self):
        """A keep_full candidate with tank level is valid."""
        candidate = _make_candidate(
            customer_type="keep_full",
            tank_level_percent=20.0,
        )
        assert candidate.customer_type == CustomerType.KEEP_FULL
        assert candidate.tank_level_percent == 20.0

    def test_invalid_customer_type_rejected(self):
        """An invalid customer_type raises a validation error."""
        with pytest.raises(Exception):
            _make_candidate(customer_type="invalid_type")

    def test_optional_fields_default_to_none(self):
        """Optional fields default to None when not provided."""
        candidate = _make_candidate()
        assert candidate.order_id is None
        assert candidate.order_status is None
        assert candidate.tank_level_percent is None
        assert candidate.reorder_point_percent is None
        assert candidate.forecast_days_to_empty is None
        assert candidate.planning_horizon_days is None


class TestFilteredCandidatesModel:
    """Tests for the FilteredCandidates Pydantic model."""

    def test_default_empty_lists(self):
        """FilteredCandidates defaults to empty lists."""
        result = FilteredCandidates()
        assert result.will_call == []
        assert result.auto_fill == []
        assert result.keep_full == []
        assert result.excluded == []

    def test_partitioned_at_is_set(self):
        """FilteredCandidates has a partitioned_at timestamp."""
        result = FilteredCandidates()
        assert isinstance(result.partitioned_at, datetime)


# ---------------------------------------------------------------------------
# Tests: partition_candidates()
# ---------------------------------------------------------------------------


class TestPartitionCandidates:
    """Tests for DeliveryFilter.partition_candidates()."""

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_empty_result(self, service):
        """An empty candidate list returns empty partitions."""
        result = await service.partition_candidates([])
        assert result.will_call == []
        assert result.auto_fill == []
        assert result.keep_full == []
        assert result.excluded == []

    @pytest.mark.asyncio
    async def test_will_call_candidates_partitioned_correctly(self, service):
        """will_call candidates with valid orders go into the will_call list."""
        candidates = [
            _make_candidate(
                candidate_id="c1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="c2",
                customer_type="will_call",
                order_id="order_002",
                order_status="ready_for_dispatch",
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.will_call) == 2
        assert len(result.auto_fill) == 0
        assert len(result.keep_full) == 0
        assert result.will_call[0].candidate_id == "c1"
        assert result.will_call[1].candidate_id == "c2"

    @pytest.mark.asyncio
    async def test_auto_fill_candidates_partitioned_correctly(self, service):
        """auto_fill candidates go into the auto_fill list."""
        candidates = [
            _make_candidate(
                candidate_id="c1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="c2",
                customer_type="auto_fill",
                forecast_days_to_empty=5.0,
                planning_horizon_days=7.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.will_call) == 0
        assert len(result.auto_fill) == 2
        assert len(result.keep_full) == 0
        assert result.auto_fill[0].candidate_id == "c1"

    @pytest.mark.asyncio
    async def test_keep_full_candidates_partitioned_correctly(self, service):
        """keep_full candidates with valid tank level go into the keep_full list."""
        candidates = [
            _make_candidate(
                candidate_id="c1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.will_call) == 0
        assert len(result.auto_fill) == 0
        assert len(result.keep_full) == 1
        assert result.keep_full[0].candidate_id == "c1"

    @pytest.mark.asyncio
    async def test_mixed_candidates_partitioned_correctly(self, service):
        """Mixed candidate types are routed to their respective lists."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_010",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
            _make_candidate(
                candidate_id="wc2",
                customer_type="will_call",
                order_id="order_011",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af2",
                customer_type="auto_fill",
                forecast_days_to_empty=5.0,
                planning_horizon_days=7.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.will_call) == 2
        assert len(result.auto_fill) == 2
        assert len(result.keep_full) == 1
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_result_has_partitioned_at_timestamp(self, service):
        """The result includes a partitioned_at timestamp."""
        candidates = [
            _make_candidate(candidate_id="c1", customer_type="will_call"),
        ]
        result = await service.partition_candidates(candidates)
        assert isinstance(result.partitioned_at, datetime)

    @pytest.mark.asyncio
    async def test_candidate_data_preserved_in_partition(self, service):
        """All candidate fields are preserved after partitioning."""
        candidate = _make_candidate(
            candidate_id="c1",
            customer_id="cust_abc",
            customer_type="auto_fill",
            order_id="ord_123",
            order_status="pending",
            tank_level_percent=42.5,
            reorder_point_percent=25.0,
            forecast_days_to_empty=3.5,
            planning_horizon_days=7.0,
        )
        result = await service.partition_candidates([candidate])
        partitioned = result.auto_fill[0]
        assert partitioned.candidate_id == "c1"
        assert partitioned.customer_id == "cust_abc"
        assert partitioned.order_id == "ord_123"
        assert partitioned.order_status == "pending"
        assert partitioned.tank_level_percent == 42.5
        assert partitioned.reorder_point_percent == 25.0
        assert partitioned.forecast_days_to_empty == 3.5
        assert partitioned.planning_horizon_days == 7.0


# ---------------------------------------------------------------------------
# Tests: Will-Call Partition Validation (Req 14.2, 14.6)
# ---------------------------------------------------------------------------


class TestWillCallPartition:
    """Tests for will_call partition validation logic.

    Validates: Requirements 14.2, 14.6
    - will_call candidates MUST have an explicit customer order with
      status 'ready_for_dispatch' to be included.
    - will_call candidates without a valid order_id are excluded.
    - will_call candidates with order_status != 'ready_for_dispatch' are excluded.
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_will_call_with_ready_for_dispatch_included(self, service):
        """A will_call candidate with order_status='ready_for_dispatch' is included."""
        candidate = _make_candidate(
            candidate_id="wc_valid",
            customer_type="will_call",
            order_id="order_001",
            order_status="ready_for_dispatch",
        )
        result = await service.partition_candidates([candidate])
        assert len(result.will_call) == 1
        assert result.will_call[0].candidate_id == "wc_valid"
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_will_call_without_order_id_excluded(self, service):
        """A will_call candidate without order_id is excluded."""
        candidate = _make_candidate(
            candidate_id="wc_no_order",
            customer_type="will_call",
            order_id=None,
            order_status="ready_for_dispatch",
        )
        result = await service.partition_candidates([candidate])
        assert len(result.will_call) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "wc_no_order"
        assert "order_id is missing" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_will_call_with_empty_order_id_excluded(self, service):
        """A will_call candidate with empty string order_id is excluded."""
        candidate = _make_candidate(
            candidate_id="wc_empty_order",
            customer_type="will_call",
            order_id="",
            order_status="ready_for_dispatch",
        )
        result = await service.partition_candidates([candidate])
        assert len(result.will_call) == 0
        assert len(result.excluded) == 1
        assert "order_id is missing" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_will_call_with_pending_order_status_excluded(self, service):
        """A will_call candidate with order_status='pending' is excluded."""
        candidate = _make_candidate(
            candidate_id="wc_pending",
            customer_type="will_call",
            order_id="order_002",
            order_status="pending",
        )
        result = await service.partition_candidates([candidate])
        assert len(result.will_call) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "wc_pending"
        assert "pending" in result.excluded[0].reason
        assert "ready_for_dispatch" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_will_call_with_cancelled_order_status_excluded(self, service):
        """A will_call candidate with order_status='cancelled' is excluded."""
        candidate = _make_candidate(
            candidate_id="wc_cancelled",
            customer_type="will_call",
            order_id="order_003",
            order_status="cancelled",
        )
        result = await service.partition_candidates([candidate])
        assert len(result.will_call) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "wc_cancelled"
        assert "cancelled" in result.excluded[0].reason
        assert "ready_for_dispatch" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_will_call_with_none_order_status_excluded(self, service):
        """A will_call candidate with order_status=None is excluded."""
        candidate = _make_candidate(
            candidate_id="wc_no_status",
            customer_type="will_call",
            order_id="order_004",
            order_status=None,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.will_call) == 0
        assert len(result.excluded) == 1
        assert "None" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_mixed_will_call_valid_and_invalid(self, service):
        """Only valid will_call candidates pass; invalid ones are excluded."""
        candidates = [
            _make_candidate(
                candidate_id="valid_wc",
                customer_type="will_call",
                order_id="order_100",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="no_order_wc",
                customer_type="will_call",
                order_id=None,
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="wrong_status_wc",
                customer_type="will_call",
                order_id="order_101",
                order_status="pending",
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.will_call) == 1
        assert result.will_call[0].candidate_id == "valid_wc"
        assert len(result.excluded) == 2
        excluded_ids = [e.candidate.candidate_id for e in result.excluded]
        assert "no_order_wc" in excluded_ids
        assert "wrong_status_wc" in excluded_ids

    @pytest.mark.asyncio
    async def test_will_call_exclusion_does_not_affect_other_partitions(self, service):
        """Excluding invalid will_call candidates doesn't affect auto_fill/keep_full."""
        candidates = [
            _make_candidate(
                candidate_id="invalid_wc",
                customer_type="will_call",
                order_id=None,
                order_status=None,
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                tank_level_percent=40.0,
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.will_call) == 0
        assert len(result.auto_fill) == 1
        assert len(result.keep_full) == 1
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "invalid_wc"


# ---------------------------------------------------------------------------
# Tests: Auto-Fill Partition Validation (Req 14.3)
# ---------------------------------------------------------------------------


class TestAutoFillPartition:
    """Tests for auto_fill partition validation logic.

    Validates: Requirement 14.3
    - auto_fill candidates MUST have forecast_days_to_empty set.
    - auto_fill candidates MUST have planning_horizon_days set.
    - auto_fill candidates are included only when forecast_days_to_empty
      <= planning_horizon_days (tank will drop below reorder point within
      the planning horizon).
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_auto_fill_within_horizon_included(self, service):
        """auto_fill with forecast_days_to_empty < planning_horizon_days is included."""
        candidate = _make_candidate(
            candidate_id="af_valid",
            customer_type="auto_fill",
            forecast_days_to_empty=3.0,
            planning_horizon_days=7.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.auto_fill) == 1
        assert result.auto_fill[0].candidate_id == "af_valid"
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_auto_fill_at_boundary_included(self, service):
        """auto_fill with forecast_days_to_empty == planning_horizon_days is included (boundary)."""
        candidate = _make_candidate(
            candidate_id="af_boundary",
            customer_type="auto_fill",
            forecast_days_to_empty=7.0,
            planning_horizon_days=7.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.auto_fill) == 1
        assert result.auto_fill[0].candidate_id == "af_boundary"
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_auto_fill_beyond_horizon_excluded(self, service):
        """auto_fill with forecast_days_to_empty > planning_horizon_days is excluded."""
        candidate = _make_candidate(
            candidate_id="af_beyond",
            customer_type="auto_fill",
            forecast_days_to_empty=10.0,
            planning_horizon_days=7.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.auto_fill) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "af_beyond"
        assert "exceeds planning_horizon_days" in result.excluded[0].reason
        assert "10.0" in result.excluded[0].reason
        assert "7.0" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_auto_fill_without_forecast_days_excluded(self, service):
        """auto_fill without forecast_days_to_empty is excluded."""
        candidate = _make_candidate(
            candidate_id="af_no_forecast",
            customer_type="auto_fill",
            forecast_days_to_empty=None,
            planning_horizon_days=7.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.auto_fill) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "af_no_forecast"
        assert "forecast_days_to_empty is not set" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_auto_fill_without_planning_horizon_excluded(self, service):
        """auto_fill without planning_horizon_days is excluded."""
        candidate = _make_candidate(
            candidate_id="af_no_horizon",
            customer_type="auto_fill",
            forecast_days_to_empty=3.0,
            planning_horizon_days=None,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.auto_fill) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "af_no_horizon"
        assert "planning_horizon_days is not set" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_auto_fill_without_both_fields_excluded(self, service):
        """auto_fill without both forecast and horizon fields is excluded (first check fails)."""
        candidate = _make_candidate(
            candidate_id="af_no_both",
            customer_type="auto_fill",
            forecast_days_to_empty=None,
            planning_horizon_days=None,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.auto_fill) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "af_no_both"
        # First validation check (forecast_days_to_empty) fails first
        assert "forecast_days_to_empty is not set" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_mixed_auto_fill_valid_and_invalid(self, service):
        """Only valid auto_fill candidates pass; invalid ones are excluded."""
        candidates = [
            _make_candidate(
                candidate_id="af_valid",
                customer_type="auto_fill",
                forecast_days_to_empty=2.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="af_no_forecast",
                customer_type="auto_fill",
                forecast_days_to_empty=None,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="af_beyond",
                customer_type="auto_fill",
                forecast_days_to_empty=14.0,
                planning_horizon_days=7.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.auto_fill) == 1
        assert result.auto_fill[0].candidate_id == "af_valid"
        assert len(result.excluded) == 2
        excluded_ids = [e.candidate.candidate_id for e in result.excluded]
        assert "af_no_forecast" in excluded_ids
        assert "af_beyond" in excluded_ids

    @pytest.mark.asyncio
    async def test_auto_fill_exclusion_does_not_affect_other_partitions(self, service):
        """Excluding invalid auto_fill candidates doesn't affect will_call/keep_full."""
        candidates = [
            _make_candidate(
                candidate_id="af_invalid",
                customer_type="auto_fill",
                forecast_days_to_empty=None,
                planning_horizon_days=None,
            ),
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.auto_fill) == 0
        assert len(result.will_call) == 1
        assert len(result.keep_full) == 1
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "af_invalid"


# ---------------------------------------------------------------------------
# Tests: Keep-Full Partition Validation (Req 14.4)
# ---------------------------------------------------------------------------


class TestKeepFullPartition:
    """Tests for keep_full partition validation logic.

    Validates: Requirement 14.4
    - keep_full candidates MUST have tank_level_percent set.
    - keep_full candidates are included only when tank_level_percent
      is below the keep_full threshold (default 30%).
    - Candidates at or above the threshold are excluded.
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_keep_full_below_threshold_included(self, service):
        """keep_full with tank_level_percent < 30% is included."""
        candidate = _make_candidate(
            candidate_id="kf_valid",
            customer_type="keep_full",
            tank_level_percent=20.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.keep_full) == 1
        assert result.keep_full[0].candidate_id == "kf_valid"
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_keep_full_at_threshold_excluded(self, service):
        """keep_full with tank_level_percent == 30% is excluded (at threshold, not below)."""
        candidate = _make_candidate(
            candidate_id="kf_at_threshold",
            customer_type="keep_full",
            tank_level_percent=30.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.keep_full) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "kf_at_threshold"
        assert "not below keep_full threshold" in result.excluded[0].reason
        assert "30.0%" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_keep_full_above_threshold_excluded(self, service):
        """keep_full with tank_level_percent > 30% is excluded."""
        candidate = _make_candidate(
            candidate_id="kf_above",
            customer_type="keep_full",
            tank_level_percent=55.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.keep_full) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "kf_above"
        assert "not below keep_full threshold" in result.excluded[0].reason
        assert "55.0%" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_keep_full_without_tank_level_excluded(self, service):
        """keep_full without tank_level_percent is excluded."""
        candidate = _make_candidate(
            candidate_id="kf_no_level",
            customer_type="keep_full",
            tank_level_percent=None,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.keep_full) == 0
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "kf_no_level"
        assert "tank_level_percent is not set" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_keep_full_at_zero_percent_included(self, service):
        """keep_full with tank_level_percent = 0% is included (edge case — empty tank)."""
        candidate = _make_candidate(
            candidate_id="kf_empty",
            customer_type="keep_full",
            tank_level_percent=0.0,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.keep_full) == 1
        assert result.keep_full[0].candidate_id == "kf_empty"
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_keep_full_just_below_threshold_included(self, service):
        """keep_full with tank_level_percent = 29.9% is included (just below threshold)."""
        candidate = _make_candidate(
            candidate_id="kf_just_below",
            customer_type="keep_full",
            tank_level_percent=29.9,
        )
        result = await service.partition_candidates([candidate])
        assert len(result.keep_full) == 1
        assert result.keep_full[0].candidate_id == "kf_just_below"
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_mixed_keep_full_valid_and_invalid(self, service):
        """Only valid keep_full candidates pass; invalid ones are excluded."""
        candidates = [
            _make_candidate(
                candidate_id="kf_valid",
                customer_type="keep_full",
                tank_level_percent=15.0,
            ),
            _make_candidate(
                candidate_id="kf_no_level",
                customer_type="keep_full",
                tank_level_percent=None,
            ),
            _make_candidate(
                candidate_id="kf_above",
                customer_type="keep_full",
                tank_level_percent=50.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.keep_full) == 1
        assert result.keep_full[0].candidate_id == "kf_valid"
        assert len(result.excluded) == 2
        excluded_ids = [e.candidate.candidate_id for e in result.excluded]
        assert "kf_no_level" in excluded_ids
        assert "kf_above" in excluded_ids

    @pytest.mark.asyncio
    async def test_keep_full_exclusion_does_not_affect_other_partitions(self, service):
        """Excluding invalid keep_full candidates doesn't affect will_call/auto_fill."""
        candidates = [
            _make_candidate(
                candidate_id="kf_invalid",
                customer_type="keep_full",
                tank_level_percent=None,
            ),
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
        ]
        result = await service.partition_candidates(candidates)
        assert len(result.keep_full) == 0
        assert len(result.will_call) == 1
        assert len(result.auto_fill) == 1
        assert len(result.excluded) == 1
        assert result.excluded[0].candidate.candidate_id == "kf_invalid"


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for module-level constants."""

    def test_default_keep_full_threshold(self):
        """Default keep_full threshold is 30%."""
        assert DEFAULT_KEEP_FULL_THRESHOLD_PERCENT == 30.0

    def test_customer_type_enum_values(self):
        """CustomerType enum has the expected values."""
        assert CustomerType.WILL_CALL == "will_call"
        assert CustomerType.AUTO_FILL == "auto_fill"
        assert CustomerType.KEEP_FULL == "keep_full"


# ---------------------------------------------------------------------------
# Tests: Filtering Outcome Logging (Req 14.7)
# ---------------------------------------------------------------------------


class TestFilteringOutcomeLogging:
    """Tests for structured logging of filtering outcomes per call type.

    Validates: Requirement 14.7
    - THE Delivery_Filter SHALL log the filtering outcome (candidates_in,
      candidates_out, reason) per call type for dispatcher visibility and audit.
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_logs_per_call_type_outcome(self, service, caplog):
        """Each call type gets a structured log line with candidates_in/out."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        # Check will_call outcome log
        wc_logs = [r for r in caplog.records if "call_type=will_call" in r.message]
        assert len(wc_logs) == 1
        assert "candidates_in=1" in wc_logs[0].message
        assert "candidates_out=1" in wc_logs[0].message
        assert "excluded_count=0" in wc_logs[0].message

        # Check auto_fill outcome log
        af_logs = [r for r in caplog.records if "call_type=auto_fill" in r.message]
        assert len(af_logs) == 1
        assert "candidates_in=1" in af_logs[0].message
        assert "candidates_out=1" in af_logs[0].message
        assert "excluded_count=0" in af_logs[0].message

        # Check keep_full outcome log
        kf_logs = [r for r in caplog.records if "call_type=keep_full" in r.message]
        assert len(kf_logs) == 1
        assert "candidates_in=1" in kf_logs[0].message
        assert "candidates_out=1" in kf_logs[0].message
        assert "excluded_count=0" in kf_logs[0].message

    @pytest.mark.asyncio
    async def test_logs_overall_summary(self, service, caplog):
        """An overall summary log line is emitted with total_in/out/excluded."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        summary_logs = [r for r in caplog.records if "delivery_filter.summary" in r.message]
        assert len(summary_logs) == 1
        assert "total_in=2" in summary_logs[0].message
        assert "total_out=2" in summary_logs[0].message
        assert "total_excluded=0" in summary_logs[0].message

    @pytest.mark.asyncio
    async def test_logs_excluded_count_and_reasons(self, service, caplog):
        """Excluded candidates are reflected in the log with reasons."""
        candidates = [
            _make_candidate(
                candidate_id="wc_invalid",
                customer_type="will_call",
                order_id=None,
                order_status=None,
            ),
            _make_candidate(
                candidate_id="wc_valid",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af_invalid",
                customer_type="auto_fill",
                forecast_days_to_empty=None,
                planning_horizon_days=7.0,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        # will_call: 2 in, 1 out, 1 excluded
        wc_logs = [r for r in caplog.records if "call_type=will_call" in r.message]
        assert len(wc_logs) == 1
        assert "candidates_in=2" in wc_logs[0].message
        assert "candidates_out=1" in wc_logs[0].message
        assert "excluded_count=1" in wc_logs[0].message
        # Reason should not be "none"
        assert "reasons=none" not in wc_logs[0].message

        # auto_fill: 1 in, 0 out, 1 excluded
        af_logs = [r for r in caplog.records if "call_type=auto_fill" in r.message]
        assert len(af_logs) == 1
        assert "candidates_in=1" in af_logs[0].message
        assert "candidates_out=0" in af_logs[0].message
        assert "excluded_count=1" in af_logs[0].message

        # Overall summary
        summary_logs = [r for r in caplog.records if "delivery_filter.summary" in r.message]
        assert len(summary_logs) == 1
        assert "total_in=3" in summary_logs[0].message
        assert "total_out=1" in summary_logs[0].message
        assert "total_excluded=2" in summary_logs[0].message

    @pytest.mark.asyncio
    async def test_logs_empty_candidates(self, service, caplog):
        """Empty candidate list still produces per-type and summary logs."""
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates([])

        # All three call types should be logged with 0 in/out
        wc_logs = [r for r in caplog.records if "call_type=will_call" in r.message]
        assert len(wc_logs) == 1
        assert "candidates_in=0" in wc_logs[0].message
        assert "candidates_out=0" in wc_logs[0].message

        af_logs = [r for r in caplog.records if "call_type=auto_fill" in r.message]
        assert len(af_logs) == 1
        assert "candidates_in=0" in af_logs[0].message
        assert "candidates_out=0" in af_logs[0].message

        kf_logs = [r for r in caplog.records if "call_type=keep_full" in r.message]
        assert len(kf_logs) == 1
        assert "candidates_in=0" in kf_logs[0].message
        assert "candidates_out=0" in kf_logs[0].message

        # Summary
        summary_logs = [r for r in caplog.records if "delivery_filter.summary" in r.message]
        assert len(summary_logs) == 1
        assert "total_in=0" in summary_logs[0].message
        assert "total_out=0" in summary_logs[0].message
        assert "total_excluded=0" in summary_logs[0].message

    @pytest.mark.asyncio
    async def test_logs_reasons_none_when_no_exclusions(self, service, caplog):
        """When no candidates are excluded, reasons='none' is logged."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        wc_logs = [r for r in caplog.records if "call_type=will_call" in r.message]
        assert len(wc_logs) == 1
        assert "reasons=none" in wc_logs[0].message

    @pytest.mark.asyncio
    async def test_log_lines_are_structured_key_value(self, service, caplog):
        """Log lines use structured key=value format for parseability."""
        candidates = [
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        # Verify the outcome log uses the structured prefix
        outcome_logs = [r for r in caplog.records if "delivery_filter.outcome" in r.message]
        assert len(outcome_logs) >= 1
        # Each outcome log should have key=value pairs
        for log_record in outcome_logs:
            msg = log_record.message
            assert "call_type=" in msg
            assert "candidates_in=" in msg
            assert "candidates_out=" in msg
            assert "excluded_count=" in msg
            assert "reasons=" in msg


# ---------------------------------------------------------------------------
# Tests: End-to-End Mixed Scenarios
# ---------------------------------------------------------------------------


class TestEndToEndMixedScenarios:
    """End-to-end tests with all three partitions having both valid and invalid candidates.

    These tests verify the complete filtering pipeline when candidates from
    all three call types are present, with a mix of valid and invalid entries.
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_all_partitions_with_valid_and_invalid_candidates(self, service):
        """All three partitions have both valid and invalid candidates simultaneously."""
        candidates = [
            # will_call: 1 valid, 2 invalid
            _make_candidate(
                candidate_id="wc_valid_1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="wc_invalid_no_order",
                customer_type="will_call",
                order_id=None,
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="wc_invalid_wrong_status",
                customer_type="will_call",
                order_id="order_002",
                order_status="pending",
            ),
            # auto_fill: 2 valid, 1 invalid
            _make_candidate(
                candidate_id="af_valid_1",
                customer_type="auto_fill",
                forecast_days_to_empty=2.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="af_valid_2",
                customer_type="auto_fill",
                forecast_days_to_empty=6.5,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="af_invalid_beyond",
                customer_type="auto_fill",
                forecast_days_to_empty=14.0,
                planning_horizon_days=7.0,
            ),
            # keep_full: 1 valid, 1 invalid
            _make_candidate(
                candidate_id="kf_valid_1",
                customer_type="keep_full",
                tank_level_percent=15.0,
            ),
            _make_candidate(
                candidate_id="kf_invalid_above",
                customer_type="keep_full",
                tank_level_percent=50.0,
            ),
        ]
        result = await service.partition_candidates(candidates)

        # Verify valid candidates in correct partitions
        assert len(result.will_call) == 1
        assert result.will_call[0].candidate_id == "wc_valid_1"

        assert len(result.auto_fill) == 2
        af_ids = [c.candidate_id for c in result.auto_fill]
        assert "af_valid_1" in af_ids
        assert "af_valid_2" in af_ids

        assert len(result.keep_full) == 1
        assert result.keep_full[0].candidate_id == "kf_valid_1"

        # Verify excluded candidates
        assert len(result.excluded) == 4
        excluded_ids = [e.candidate.candidate_id for e in result.excluded]
        assert "wc_invalid_no_order" in excluded_ids
        assert "wc_invalid_wrong_status" in excluded_ids
        assert "af_invalid_beyond" in excluded_ids
        assert "kf_invalid_above" in excluded_ids

    @pytest.mark.asyncio
    async def test_all_candidates_excluded(self, service):
        """When all candidates fail validation, all partitions are empty and excluded is full."""
        candidates = [
            _make_candidate(
                candidate_id="wc_no_order",
                customer_type="will_call",
                order_id=None,
                order_status=None,
            ),
            _make_candidate(
                candidate_id="af_no_forecast",
                customer_type="auto_fill",
                forecast_days_to_empty=None,
                planning_horizon_days=None,
            ),
            _make_candidate(
                candidate_id="kf_no_level",
                customer_type="keep_full",
                tank_level_percent=None,
            ),
            _make_candidate(
                candidate_id="wc_wrong_status",
                customer_type="will_call",
                order_id="order_001",
                order_status="cancelled",
            ),
            _make_candidate(
                candidate_id="kf_above_threshold",
                customer_type="keep_full",
                tank_level_percent=80.0,
            ),
        ]
        result = await service.partition_candidates(candidates)

        assert len(result.will_call) == 0
        assert len(result.auto_fill) == 0
        assert len(result.keep_full) == 0
        assert len(result.excluded) == 5

    @pytest.mark.asyncio
    async def test_all_candidates_valid(self, service):
        """When all candidates pass validation, excluded list is empty."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="wc2",
                customer_type="will_call",
                order_id="order_002",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=1.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="af2",
                customer_type="auto_fill",
                forecast_days_to_empty=5.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=10.0,
            ),
            _make_candidate(
                candidate_id="kf2",
                customer_type="keep_full",
                tank_level_percent=25.0,
            ),
        ]
        result = await service.partition_candidates(candidates)

        assert len(result.will_call) == 2
        assert len(result.auto_fill) == 2
        assert len(result.keep_full) == 2
        assert len(result.excluded) == 0

    @pytest.mark.asyncio
    async def test_large_candidate_list(self, service):
        """Handles a large candidate list (100+ candidates) correctly."""
        candidates = []
        # 50 valid will_call
        for i in range(50):
            candidates.append(
                _make_candidate(
                    candidate_id=f"wc_{i}",
                    customer_type="will_call",
                    order_id=f"order_{i}",
                    order_status="ready_for_dispatch",
                )
            )
        # 30 valid auto_fill
        for i in range(30):
            candidates.append(
                _make_candidate(
                    candidate_id=f"af_{i}",
                    customer_type="auto_fill",
                    forecast_days_to_empty=float(i % 7),
                    planning_horizon_days=7.0,
                )
            )
        # 20 valid keep_full
        for i in range(20):
            candidates.append(
                _make_candidate(
                    candidate_id=f"kf_{i}",
                    customer_type="keep_full",
                    tank_level_percent=float(i),  # 0-19%, all below 30%
                )
            )
        # 10 invalid (mixed)
        for i in range(10):
            candidates.append(
                _make_candidate(
                    candidate_id=f"invalid_{i}",
                    customer_type="will_call",
                    order_id=None,
                    order_status=None,
                )
            )

        result = await service.partition_candidates(candidates)

        assert len(result.will_call) == 50
        assert len(result.auto_fill) == 30
        assert len(result.keep_full) == 20
        assert len(result.excluded) == 10
        # Total should match input
        total_output = (
            len(result.will_call)
            + len(result.auto_fill)
            + len(result.keep_full)
            + len(result.excluded)
        )
        assert total_output == 110


# ---------------------------------------------------------------------------
# Tests: ExcludedCandidate Object Verification
# ---------------------------------------------------------------------------


class TestExcludedCandidateObjects:
    """Tests verifying ExcludedCandidate objects contain correct data and reasons.

    Ensures the excluded list preserves the original candidate data and
    provides meaningful, parseable exclusion reasons.
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_excluded_candidate_preserves_original_data(self, service):
        """ExcludedCandidate.candidate contains the full original DeliveryCandidate."""
        candidate = _make_candidate(
            candidate_id="wc_test",
            customer_id="cust_xyz",
            customer_type="will_call",
            order_id=None,
            order_status="pending",
            tank_level_percent=55.0,
        )
        result = await service.partition_candidates([candidate])

        assert len(result.excluded) == 1
        excluded = result.excluded[0]
        assert isinstance(excluded, ExcludedCandidate)
        assert excluded.candidate.candidate_id == "wc_test"
        assert excluded.candidate.customer_id == "cust_xyz"
        assert excluded.candidate.customer_type == CustomerType.WILL_CALL
        assert excluded.candidate.tank_level_percent == 55.0

    @pytest.mark.asyncio
    async def test_will_call_excluded_reason_contains_candidate_id(self, service):
        """will_call exclusion reason references the candidate_id."""
        candidate = _make_candidate(
            candidate_id="wc_ref_check",
            customer_type="will_call",
            order_id=None,
            order_status=None,
        )
        result = await service.partition_candidates([candidate])
        assert "wc_ref_check" in result.excluded[0].reason

    @pytest.mark.asyncio
    async def test_auto_fill_excluded_reason_contains_values(self, service):
        """auto_fill exclusion reason includes the actual forecast and horizon values."""
        candidate = _make_candidate(
            candidate_id="af_values_check",
            customer_type="auto_fill",
            forecast_days_to_empty=12.5,
            planning_horizon_days=5.0,
        )
        result = await service.partition_candidates([candidate])
        reason = result.excluded[0].reason
        assert "12.5" in reason
        assert "5.0" in reason
        assert "af_values_check" in reason

    @pytest.mark.asyncio
    async def test_keep_full_excluded_reason_contains_threshold(self, service):
        """keep_full exclusion reason includes the actual level and threshold values."""
        candidate = _make_candidate(
            candidate_id="kf_threshold_check",
            customer_type="keep_full",
            tank_level_percent=45.0,
        )
        result = await service.partition_candidates([candidate])
        reason = result.excluded[0].reason
        assert "45.0%" in reason
        assert "30.0%" in reason
        assert "kf_threshold_check" in reason

    @pytest.mark.asyncio
    async def test_multiple_excluded_have_distinct_reasons(self, service):
        """Each excluded candidate has a unique, specific reason."""
        candidates = [
            _make_candidate(
                candidate_id="wc_no_order",
                customer_type="will_call",
                order_id=None,
                order_status=None,
            ),
            _make_candidate(
                candidate_id="wc_wrong_status",
                customer_type="will_call",
                order_id="order_001",
                order_status="in_progress",
            ),
            _make_candidate(
                candidate_id="af_missing_forecast",
                customer_type="auto_fill",
                forecast_days_to_empty=None,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf_above",
                customer_type="keep_full",
                tank_level_percent=60.0,
            ),
        ]
        result = await service.partition_candidates(candidates)

        assert len(result.excluded) == 4
        reasons = [e.reason for e in result.excluded]
        # All reasons should be distinct
        assert len(set(reasons)) == 4
        # Each reason references its own candidate_id
        for excluded in result.excluded:
            assert excluded.candidate.candidate_id in excluded.reason


# ---------------------------------------------------------------------------
# Tests: Dispatcher Log Output Format Verification
# ---------------------------------------------------------------------------


class TestDispatcherLogFormat:
    """Tests verifying the dispatcher log output format is parseable.

    Validates: Requirement 14.7
    Ensures log lines follow a consistent structured key=value format
    that can be parsed by log aggregation tools.
    """

    @pytest.fixture
    def service(self) -> DeliveryFilter:
        return DeliveryFilter()

    @pytest.mark.asyncio
    async def test_all_log_lines_have_consistent_prefix(self, service, caplog):
        """All outcome logs use 'delivery_filter.outcome' prefix; summary uses 'delivery_filter.summary'."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf1",
                customer_type="keep_full",
                tank_level_percent=20.0,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        outcome_logs = [r for r in caplog.records if "delivery_filter.outcome" in r.message]
        summary_logs = [r for r in caplog.records if "delivery_filter.summary" in r.message]

        # Exactly 3 outcome logs (one per call type) + 1 summary
        assert len(outcome_logs) == 3
        assert len(summary_logs) == 1

    @pytest.mark.asyncio
    async def test_log_values_are_parseable_integers(self, service, caplog):
        """Numeric values in log lines are parseable as integers."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="wc2",
                customer_type="will_call",
                order_id=None,
                order_status=None,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        wc_logs = [r for r in caplog.records if "call_type=will_call" in r.message]
        assert len(wc_logs) == 1
        msg = wc_logs[0].message

        # Extract and verify key=value pairs are parseable
        parts = msg.split()
        kv_pairs = {}
        for part in parts:
            if "=" in part:
                key, value = part.split("=", 1)
                kv_pairs[key] = value

        assert "candidates_in" in kv_pairs
        assert "candidates_out" in kv_pairs
        assert "excluded_count" in kv_pairs
        # Verify they parse as integers
        assert int(kv_pairs["candidates_in"]) == 2
        assert int(kv_pairs["candidates_out"]) == 1
        assert int(kv_pairs["excluded_count"]) == 1

    @pytest.mark.asyncio
    async def test_summary_log_values_are_parseable(self, service, caplog):
        """Summary log line values are parseable integers."""
        candidates = [
            _make_candidate(
                candidate_id="wc1",
                customer_type="will_call",
                order_id="order_001",
                order_status="ready_for_dispatch",
            ),
            _make_candidate(
                candidate_id="af1",
                customer_type="auto_fill",
                forecast_days_to_empty=3.0,
                planning_horizon_days=7.0,
            ),
            _make_candidate(
                candidate_id="kf_invalid",
                customer_type="keep_full",
                tank_level_percent=None,
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        summary_logs = [r for r in caplog.records if "delivery_filter.summary" in r.message]
        assert len(summary_logs) == 1
        msg = summary_logs[0].message

        parts = msg.split()
        kv_pairs = {}
        for part in parts:
            if "=" in part:
                key, value = part.split("=", 1)
                kv_pairs[key] = value

        assert int(kv_pairs["total_in"]) == 3
        assert int(kv_pairs["total_out"]) == 2
        assert int(kv_pairs["total_excluded"]) == 1

    @pytest.mark.asyncio
    async def test_log_with_exclusions_shows_reason_categories(self, service, caplog):
        """When candidates are excluded, the reasons field shows categorized reasons."""
        candidates = [
            _make_candidate(
                candidate_id="wc_no_order",
                customer_type="will_call",
                order_id=None,
                order_status=None,
            ),
            _make_candidate(
                candidate_id="wc_wrong_status",
                customer_type="will_call",
                order_id="order_001",
                order_status="pending",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="compliance.services.delivery_filter"):
            await service.partition_candidates(candidates)

        wc_logs = [r for r in caplog.records if "call_type=will_call" in r.message]
        assert len(wc_logs) == 1
        msg = wc_logs[0].message
        # reasons field should NOT be "none" since there are exclusions
        assert "reasons=none" not in msg
        # reasons field should contain some categorized text
        assert "reasons=" in msg
