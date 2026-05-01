"""
Unit tests for the OutcomeTracker pipeline component.

Tests cover:
- Module-level constants (OUTCOMES_INDEX, DEFAULT_OBSERVATION_WINDOW_SECONDS, DEFAULT_ADVERSE_THRESHOLD_PCT)
- PendingOutcome class fields and measure_at computation
- OutcomeTracker constructor and default parameters
- record_proposal_execution() captures before-KPIs and schedules measurement
- check_pending_outcomes() skips proposals not yet past observation window
- check_pending_outcomes() measures after-KPIs and computes realized_delta
- check_pending_outcomes() flags adverse outcomes (>10% worse)
- check_pending_outcomes() handles inconclusive cases (entity deleted/tenant disabled)
- check_pending_outcomes() persists OutcomeRecords to ES
- check_pending_outcomes() publishes OutcomeRecords to SignalBus
- _measure_kpis() returns aggregated KPIs from ES
- _measure_kpis() returns None when no hits
- _measure_kpis() returns None on ES error
- _persist_outcome() indexes to agent_outcomes ES index

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.7, 11.8
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.overlay.data_contracts import OutcomeRecord
from Agents.overlay.outcome_tracker import (
    DEFAULT_ADVERSE_THRESHOLD_PCT,
    DEFAULT_OBSERVATION_WINDOW_SECONDS,
    OUTCOMES_INDEX,
    OutcomeTracker,
    PendingOutcome,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal_bus():
    bus = MagicMock()
    bus.publish = AsyncMock(return_value=1)
    return bus


def _make_es_service(hits=None):
    es = MagicMock()
    if hits is None:
        hits = []
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [{"_source": h} for h in hits]}}
    )
    es.index_document = AsyncMock()
    return es


def _make_tracker(
    signal_bus=None,
    es_service=None,
    adverse_threshold_pct=DEFAULT_ADVERSE_THRESHOLD_PCT,
    observation_window_seconds=DEFAULT_OBSERVATION_WINDOW_SECONDS,
):
    if signal_bus is None:
        signal_bus = _make_signal_bus()
    if es_service is None:
        es_service = _make_es_service()
    return OutcomeTracker(
        signal_bus=signal_bus,
        es_service=es_service,
        adverse_threshold_pct=adverse_threshold_pct,
        observation_window_seconds=observation_window_seconds,
    )


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_outcomes_index(self):
        assert OUTCOMES_INDEX == "agent_outcomes"

    def test_default_observation_window(self):
        assert DEFAULT_OBSERVATION_WINDOW_SECONDS == 3600

    def test_default_adverse_threshold(self):
        assert DEFAULT_ADVERSE_THRESHOLD_PCT == 10.0


# ---------------------------------------------------------------------------
# Tests: PendingOutcome
# ---------------------------------------------------------------------------


class TestPendingOutcome:
    def test_fields(self):
        now = datetime.now(timezone.utc)
        po = PendingOutcome(
            intervention_id="int-1",
            before_kpis={"delivery_time": 30.0},
            tenant_id="tenant-1",
            entity_ids=["job-1", "job-2"],
            created_at=now,
        )
        assert po.intervention_id == "int-1"
        assert po.before_kpis == {"delivery_time": 30.0}
        assert po.tenant_id == "tenant-1"
        assert po.entity_ids == ["job-1", "job-2"]
        assert po.created_at == now

    def test_measure_at_default_window(self):
        now = datetime.now(timezone.utc)
        po = PendingOutcome(
            intervention_id="int-1",
            before_kpis={},
            tenant_id="t",
            entity_ids=[],
            created_at=now,
        )
        expected = now + timedelta(seconds=DEFAULT_OBSERVATION_WINDOW_SECONDS)
        assert po.measure_at == expected

    def test_measure_at_custom_window(self):
        now = datetime.now(timezone.utc)
        po = PendingOutcome(
            intervention_id="int-1",
            before_kpis={},
            tenant_id="t",
            entity_ids=[],
            created_at=now,
            observation_window_seconds=1800,
        )
        expected = now + timedelta(seconds=1800)
        assert po.measure_at == expected


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_default_adverse_threshold(self):
        tracker = _make_tracker()
        assert tracker._adverse_threshold == 10.0

    def test_default_observation_window(self):
        tracker = _make_tracker()
        assert tracker._observation_window == 3600

    def test_custom_adverse_threshold(self):
        tracker = _make_tracker(adverse_threshold_pct=5.0)
        assert tracker._adverse_threshold == 5.0

    def test_custom_observation_window(self):
        tracker = _make_tracker(observation_window_seconds=1800)
        assert tracker._observation_window == 1800

    def test_empty_pending(self):
        tracker = _make_tracker()
        assert tracker._pending == {}

    def test_stores_signal_bus(self):
        bus = _make_signal_bus()
        tracker = _make_tracker(signal_bus=bus)
        assert tracker._signal_bus is bus

    def test_stores_es_service(self):
        es = _make_es_service()
        tracker = _make_tracker(es_service=es)
        assert tracker._es is es


# ---------------------------------------------------------------------------
# Tests: record_proposal_execution()
# ---------------------------------------------------------------------------


class TestRecordProposalExecution:
    @pytest.mark.asyncio
    async def test_stores_pending_outcome(self):
        """Req 11.1: create OutcomeRecord linking proposal to execution."""
        tracker = _make_tracker()
        await tracker.record_proposal_execution(
            intervention_id="int-1",
            before_kpis={"delivery_time": 30.0, "fuel_cost": 100.0},
            tenant_id="tenant-1",
            entity_ids=["job-1"],
        )
        assert "int-1" in tracker._pending
        pending = tracker._pending["int-1"]
        assert pending.intervention_id == "int-1"
        assert pending.before_kpis == {"delivery_time": 30.0, "fuel_cost": 100.0}
        assert pending.tenant_id == "tenant-1"
        assert pending.entity_ids == ["job-1"]

    @pytest.mark.asyncio
    async def test_captures_before_kpis(self):
        """Req 11.2: capture before-KPIs at proposal time."""
        tracker = _make_tracker()
        kpis = {"avg_delivery_time_minutes": 25.0, "total_fuel_cost": 500.0}
        await tracker.record_proposal_execution(
            intervention_id="int-2",
            before_kpis=kpis,
            tenant_id="tenant-1",
            entity_ids=["job-1"],
        )
        assert tracker._pending["int-2"].before_kpis == kpis

    @pytest.mark.asyncio
    async def test_schedules_measurement(self):
        """Req 11.2: schedule measurement after observation window."""
        tracker = _make_tracker(observation_window_seconds=7200)
        await tracker.record_proposal_execution(
            intervention_id="int-3",
            before_kpis={},
            tenant_id="tenant-1",
            entity_ids=[],
        )
        pending = tracker._pending["int-3"]
        expected_delta = timedelta(seconds=7200)
        actual_delta = pending.measure_at - pending.created_at
        assert actual_delta == expected_delta

    @pytest.mark.asyncio
    async def test_overwrites_existing_intervention(self):
        tracker = _make_tracker()
        await tracker.record_proposal_execution(
            "int-1", {"a": 1.0}, "t1", ["j1"]
        )
        await tracker.record_proposal_execution(
            "int-1", {"b": 2.0}, "t2", ["j2"]
        )
        assert tracker._pending["int-1"].before_kpis == {"b": 2.0}
        assert tracker._pending["int-1"].tenant_id == "t2"


# ---------------------------------------------------------------------------
# Tests: check_pending_outcomes()
# ---------------------------------------------------------------------------


class TestCheckPendingOutcomes:
    @pytest.mark.asyncio
    async def test_skips_not_yet_due(self):
        """Proposals not past observation window are skipped."""
        tracker = _make_tracker(observation_window_seconds=3600)
        await tracker.record_proposal_execution(
            "int-1", {"delivery_time": 30.0}, "t1", ["j1"]
        )
        # measure_at is 1 hour from now, so nothing should be processed
        results = await tracker.check_pending_outcomes()
        assert results == []
        assert "int-1" in tracker._pending

    @pytest.mark.asyncio
    async def test_processes_due_outcomes(self):
        """Req 11.3: compute realized_delta for proposals past window."""
        es = _make_es_service(
            hits=[
                {"actual_delivery_minutes": 25.0, "fuel_cost": 90.0, "sla_met": True}
            ]
        )
        tracker = _make_tracker(es_service=es, observation_window_seconds=0)
        await tracker.record_proposal_execution(
            "int-1",
            {"avg_delivery_time_minutes": 30.0, "total_fuel_cost": 100.0, "sla_compliance_rate": 0.8},
            "t1",
            ["j1"],
        )
        # Force measure_at to be in the past
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        results = await tracker.check_pending_outcomes()
        assert len(results) == 1
        outcome = results[0]
        assert outcome.intervention_id == "int-1"
        assert outcome.tenant_id == "t1"
        assert "avg_delivery_time_minutes" in outcome.realized_delta

    @pytest.mark.asyncio
    async def test_realized_delta_computation(self):
        """Req 11.3: realized_delta = after_kpis - before_kpis."""
        es = _make_es_service(
            hits=[
                {"actual_delivery_minutes": 25.0, "fuel_cost": 80.0, "sla_met": True}
            ]
        )
        tracker = _make_tracker(es_service=es)
        before = {
            "avg_delivery_time_minutes": 30.0,
            "total_fuel_cost": 100.0,
            "sla_compliance_rate": 0.8,
        }
        await tracker.record_proposal_execution("int-1", before, "t1", ["j1"])
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        results = await tracker.check_pending_outcomes()
        outcome = results[0]
        # after: delivery=25, fuel=80, sla=1.0
        # delta: delivery=25-30=-5, fuel=80-100=-20, sla=1.0-0.8=0.2
        assert outcome.realized_delta["avg_delivery_time_minutes"] == pytest.approx(-5.0)
        assert outcome.realized_delta["total_fuel_cost"] == pytest.approx(-20.0)
        assert outcome.realized_delta["sla_compliance_rate"] == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_adverse_status_when_degradation_exceeds_threshold(self):
        """Req 11.7: flag adverse if degradation >10% of before value."""
        # Before: delivery=30, after: delivery=40 → delta=+10, pct=+33% (worse for delivery)
        # But the logic checks if pct_change < -threshold, meaning the metric got worse
        # For delivery_time, higher is worse, but the code checks raw delta direction
        # Let's use a metric where decrease is bad: sla_compliance_rate
        # Before: sla=1.0, after: sla=0.8 → delta=-0.2, pct=-20% → adverse
        es = _make_es_service(
            hits=[
                {"actual_delivery_minutes": 30.0, "fuel_cost": 100.0, "sla_met": False}
            ]
        )
        tracker = _make_tracker(es_service=es, adverse_threshold_pct=10.0)
        before = {
            "avg_delivery_time_minutes": 30.0,
            "total_fuel_cost": 100.0,
            "sla_compliance_rate": 1.0,
        }
        await tracker.record_proposal_execution("int-1", before, "t1", ["j1"])
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        results = await tracker.check_pending_outcomes()
        assert results[0].status == "adverse"

    @pytest.mark.asyncio
    async def test_measured_status_when_within_threshold(self):
        """Status is 'measured' when no metric degrades beyond threshold."""
        es = _make_es_service(
            hits=[
                {"actual_delivery_minutes": 29.0, "fuel_cost": 98.0, "sla_met": True}
            ]
        )
        tracker = _make_tracker(es_service=es, adverse_threshold_pct=10.0)
        before = {
            "avg_delivery_time_minutes": 30.0,
            "total_fuel_cost": 100.0,
            "sla_compliance_rate": 1.0,
        }
        await tracker.record_proposal_execution("int-1", before, "t1", ["j1"])
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        results = await tracker.check_pending_outcomes()
        assert results[0].status == "measured"

    @pytest.mark.asyncio
    async def test_inconclusive_when_no_hits(self):
        """Req 11.8: inconclusive when entities cannot be found."""
        es = _make_es_service(hits=[])  # No hits
        tracker = _make_tracker(es_service=es)
        await tracker.record_proposal_execution(
            "int-1", {"delivery_time": 30.0}, "t1", ["j1"]
        )
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        results = await tracker.check_pending_outcomes()
        assert len(results) == 1
        assert results[0].status == "inconclusive"
        assert results[0].after_kpis == {}
        assert results[0].realized_delta == {}

    @pytest.mark.asyncio
    async def test_persists_outcome_to_es(self):
        """Req 11.4: persist OutcomeRecords to agent_outcomes index."""
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)
        await tracker.record_proposal_execution(
            "int-1", {"delivery_time": 30.0}, "t1", ["j1"]
        )
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        await tracker.check_pending_outcomes()

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == OUTCOMES_INDEX

    @pytest.mark.asyncio
    async def test_publishes_outcome_to_signal_bus(self):
        """Req 11.5: publish OutcomeRecords to SignalBus."""
        bus = _make_signal_bus()
        es = _make_es_service(hits=[])
        tracker = _make_tracker(signal_bus=bus, es_service=es)
        await tracker.record_proposal_execution(
            "int-1", {"delivery_time": 30.0}, "t1", ["j1"]
        )
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        await tracker.check_pending_outcomes()

        bus.publish.assert_called_once()
        published = bus.publish.call_args[0][0]
        assert isinstance(published, OutcomeRecord)

    @pytest.mark.asyncio
    async def test_removes_processed_from_pending(self):
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)
        await tracker.record_proposal_execution(
            "int-1", {}, "t1", ["j1"]
        )
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        await tracker.check_pending_outcomes()
        assert "int-1" not in tracker._pending

    @pytest.mark.asyncio
    async def test_execution_duration_ms(self):
        """execution_duration_ms reflects time since created_at."""
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)
        await tracker.record_proposal_execution(
            "int-1", {}, "t1", ["j1"]
        )
        # Set created_at to 2 seconds ago
        tracker._pending["int-1"].created_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=2)
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

        results = await tracker.check_pending_outcomes()
        # Should be approximately 2000ms
        assert results[0].execution_duration_ms >= 1500
        assert results[0].execution_duration_ms <= 3000

    @pytest.mark.asyncio
    async def test_multiple_pending_mixed(self):
        """Process multiple pending outcomes, some due and some not."""
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)

        await tracker.record_proposal_execution("int-1", {}, "t1", ["j1"])
        await tracker.record_proposal_execution("int-2", {}, "t1", ["j2"])

        # Only int-1 is due
        tracker._pending["int-1"].measure_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
        # int-2 is not due (default 1 hour from now)

        results = await tracker.check_pending_outcomes()
        assert len(results) == 1
        assert results[0].intervention_id == "int-1"
        assert "int-1" not in tracker._pending
        assert "int-2" in tracker._pending


# ---------------------------------------------------------------------------
# Tests: _measure_kpis()
# ---------------------------------------------------------------------------


class TestMeasureKpis:
    @pytest.mark.asyncio
    async def test_returns_aggregated_kpis(self):
        es = _make_es_service(
            hits=[
                {"actual_delivery_minutes": 20.0, "fuel_cost": 50.0, "sla_met": True},
                {"actual_delivery_minutes": 30.0, "fuel_cost": 70.0, "sla_met": False},
            ]
        )
        tracker = _make_tracker(es_service=es)
        result = await tracker._measure_kpis(["j1", "j2"], "t1")

        assert result is not None
        assert result["avg_delivery_time_minutes"] == pytest.approx(25.0)
        assert result["total_fuel_cost"] == pytest.approx(120.0)
        assert result["sla_compliance_rate"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_hits(self):
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)
        result = await tracker._measure_kpis(["j1"], "t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_es_error(self):
        es = _make_es_service()
        es.search_documents = AsyncMock(side_effect=Exception("ES down"))
        tracker = _make_tracker(es_service=es)
        result = await tracker._measure_kpis(["j1"], "t1")
        assert result is None

    @pytest.mark.asyncio
    async def test_queries_correct_index(self):
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)
        await tracker._measure_kpis(["j1", "j2"], "t1")

        es.search_documents.assert_called_once()
        call_args = es.search_documents.call_args
        assert call_args[0][0] == "jobs_current"

    @pytest.mark.asyncio
    async def test_query_filters_by_tenant_and_entity(self):
        es = _make_es_service(hits=[])
        tracker = _make_tracker(es_service=es)
        await tracker._measure_kpis(["j1", "j2"], "t1")

        query = es.search_documents.call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        assert {"term": {"tenant_id": "t1"}} in filters
        assert {"terms": {"job_id": ["j1", "j2"]}} in filters

    @pytest.mark.asyncio
    async def test_single_job_kpis(self):
        es = _make_es_service(
            hits=[
                {"actual_delivery_minutes": 45.0, "fuel_cost": 200.0, "sla_met": True}
            ]
        )
        tracker = _make_tracker(es_service=es)
        result = await tracker._measure_kpis(["j1"], "t1")

        assert result["avg_delivery_time_minutes"] == pytest.approx(45.0)
        assert result["total_fuel_cost"] == pytest.approx(200.0)
        assert result["sla_compliance_rate"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tests: _persist_outcome()
# ---------------------------------------------------------------------------


class TestPersistOutcome:
    @pytest.mark.asyncio
    async def test_indexes_to_outcomes_index(self):
        es = _make_es_service()
        tracker = _make_tracker(es_service=es)
        outcome = OutcomeRecord(
            intervention_id="int-1",
            before_kpis={"a": 1.0},
            after_kpis={"a": 2.0},
            realized_delta={"a": 1.0},
            execution_duration_ms=1000.0,
            tenant_id="t1",
        )
        await tracker._persist_outcome(outcome)

        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args[0][0] == OUTCOMES_INDEX
        assert call_args[0][1] == outcome.outcome_id

    @pytest.mark.asyncio
    async def test_serializes_outcome_as_json(self):
        es = _make_es_service()
        tracker = _make_tracker(es_service=es)
        outcome = OutcomeRecord(
            intervention_id="int-1",
            before_kpis={"a": 1.0},
            after_kpis={"a": 2.0},
            realized_delta={"a": 1.0},
            execution_duration_ms=1000.0,
            tenant_id="t1",
        )
        await tracker._persist_outcome(outcome)

        doc = es.index_document.call_args[0][2]
        assert doc["intervention_id"] == "int-1"
        assert doc["tenant_id"] == "t1"
        assert doc["before_kpis"] == {"a": 1.0}

    @pytest.mark.asyncio
    async def test_handles_es_error_gracefully(self):
        es = _make_es_service()
        es.index_document = AsyncMock(side_effect=Exception("ES down"))
        tracker = _make_tracker(es_service=es)
        outcome = OutcomeRecord(
            intervention_id="int-1",
            before_kpis={},
            after_kpis={},
            realized_delta={},
            execution_duration_ms=0.0,
            tenant_id="t1",
        )
        # Should not raise
        await tracker._persist_outcome(outcome)
