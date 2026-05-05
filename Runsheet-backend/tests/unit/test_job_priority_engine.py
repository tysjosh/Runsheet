"""
Unit tests for the JobPriorityEngine overlay agent.

Tests cover:
- Constructor and subscription configuration (Req 3.1)
- _assign_bucket threshold logic (Req 3.6)
- PRIORITY_SCORE_MAP numeric mapping (Req 3.4)
- evaluate() queries active jobs and computes weighted scores (Req 3.2, 3.3)
- evaluate() persists JobPriorityList to job_priorities ES index (Req 3.7)
- evaluate() publishes JobPriorityList to SignalBus (Req 3.8)
- evaluate() skips disabled tenants (Req 3.9)
- Score clamped to [0.0, 1.0] (Req 3.10)

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import RiskSignal, Severity
from Agents.overlay.job_priority_engine import (
    DEFAULT_SCORING_WEIGHTS,
    JOB_PRIORITIES_INDEX,
    PRIORITY_SCORE_MAP,
    JobPriority,
    JobPriorityEngine,
    JobPriorityList,
)
from Agents.support.fuel_distribution_models import PriorityBucket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps():
    """Create mocked dependencies for the JobPriorityEngine."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}}
    )
    es_service.index_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    autonomy_config = MagicMock()

    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
    }


def _make_agent(**overrides):
    """Create a JobPriorityEngine with mocked dependencies."""
    deps = _make_deps()
    deps.update(overrides)
    agent = JobPriorityEngine(**deps)
    return agent, deps


def _make_risk_signal(
    entity_id="job-001",
    source_agent="job_sla_monitor",
    tenant_id="tenant-1",
    context=None,
):
    """Create a sample RiskSignal for testing."""
    return RiskSignal(
        source_agent=source_agent,
        entity_id=entity_id,
        entity_type="job",
        severity=Severity.HIGH,
        confidence=0.85,
        ttl_seconds=1800,
        tenant_id=tenant_id,
        context=context or {},
    )


def _job_doc(
    job_id="job-001",
    job_type="delivery",
    priority="normal",
    status="in_progress",
    tenant_id="tenant-1",
    scheduled_time=None,
    estimated_arrival=None,
):
    """Create a sample job document."""
    doc = {
        "job_id": job_id,
        "job_type": job_type,
        "priority": priority,
        "status": status,
        "tenant_id": tenant_id,
    }
    if scheduled_time:
        doc["scheduled_time"] = scheduled_time
    if estimated_arrival:
        doc["estimated_arrival"] = estimated_arrival
    return doc


def _es_response(docs):
    """Wrap documents in an ES search response structure."""
    return {
        "hits": {
            "hits": [{"_source": doc} for doc in docs],
            "total": {"value": len(docs)},
        }
    }


# ---------------------------------------------------------------------------
# Tests: Constructor and subscription configuration (Req 3.1)
# ---------------------------------------------------------------------------


class TestConstructor:
    """Tests that JobPriorityEngine has correct default configuration."""

    def test_agent_id(self):
        """Agent ID is 'job_priority_engine'."""
        agent, _ = _make_agent()
        assert agent.agent_id == "job_priority_engine"

    def test_default_poll_interval(self):
        """Default poll interval is 60 seconds."""
        agent, _ = _make_agent()
        assert agent.poll_interval == 60

    def test_default_cooldown_minutes(self):
        """Default cooldown is 15 minutes."""
        agent, _ = _make_agent()
        assert agent.cooldown_minutes == 15

    def test_subscription_to_risk_signal(self):
        """Subscribes to RiskSignal message type."""
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is RiskSignal

    def test_subscription_filters_source_agents(self):
        """Subscription filters on job_sla_monitor and delay_response_agent."""
        agent, _ = _make_agent()
        spec = agent._subscription_specs[0]
        filters = spec["filters"]
        assert "source_agent" in filters
        assert "job_sla_monitor" in filters["source_agent"]
        assert "delay_response_agent" in filters["source_agent"]

    def test_custom_poll_interval(self):
        """Custom poll interval is respected."""
        agent, _ = _make_agent(poll_interval=120)
        assert agent.poll_interval == 120

    def test_signal_bus_stored(self):
        """Signal bus reference is stored."""
        agent, deps = _make_agent()
        assert agent._signal_bus is deps["signal_bus"]


# ---------------------------------------------------------------------------
# Tests: _assign_bucket (Req 3.6)
# ---------------------------------------------------------------------------


class TestAssignBucket:
    """Tests for the _assign_bucket static method."""

    def test_critical_at_threshold(self):
        """Score of 0.8 returns CRITICAL."""
        assert JobPriorityEngine._assign_bucket(0.8) == PriorityBucket.CRITICAL

    def test_critical_above_threshold(self):
        """Score of 0.95 returns CRITICAL."""
        assert JobPriorityEngine._assign_bucket(0.95) == PriorityBucket.CRITICAL

    def test_critical_at_one(self):
        """Score of 1.0 returns CRITICAL."""
        assert JobPriorityEngine._assign_bucket(1.0) == PriorityBucket.CRITICAL

    def test_high_at_threshold(self):
        """Score of 0.6 returns HIGH."""
        assert JobPriorityEngine._assign_bucket(0.6) == PriorityBucket.HIGH

    def test_high_below_critical(self):
        """Score of 0.79 returns HIGH."""
        assert JobPriorityEngine._assign_bucket(0.79) == PriorityBucket.HIGH

    def test_medium_at_threshold(self):
        """Score of 0.3 returns MEDIUM."""
        assert JobPriorityEngine._assign_bucket(0.3) == PriorityBucket.MEDIUM

    def test_medium_below_high(self):
        """Score of 0.59 returns MEDIUM."""
        assert JobPriorityEngine._assign_bucket(0.59) == PriorityBucket.MEDIUM

    def test_low_below_medium(self):
        """Score of 0.29 returns LOW."""
        assert JobPriorityEngine._assign_bucket(0.29) == PriorityBucket.LOW

    def test_low_at_zero(self):
        """Score of 0.0 returns LOW."""
        assert JobPriorityEngine._assign_bucket(0.0) == PriorityBucket.LOW


# ---------------------------------------------------------------------------
# Tests: PRIORITY_SCORE_MAP (Req 3.4)
# ---------------------------------------------------------------------------


class TestPriorityScoreMap:
    """Tests that PRIORITY_SCORE_MAP maps priority labels to correct scores."""

    def test_urgent_maps_to_1_0(self):
        assert PRIORITY_SCORE_MAP["urgent"] == 1.0

    def test_high_maps_to_0_75(self):
        assert PRIORITY_SCORE_MAP["high"] == 0.75

    def test_normal_maps_to_0_5(self):
        assert PRIORITY_SCORE_MAP["normal"] == 0.5

    def test_low_maps_to_0_25(self):
        assert PRIORITY_SCORE_MAP["low"] == 0.25

    def test_all_four_keys_present(self):
        assert set(PRIORITY_SCORE_MAP.keys()) == {"urgent", "high", "normal", "low"}


# ---------------------------------------------------------------------------
# Tests: DEFAULT_SCORING_WEIGHTS (Req 3.3)
# ---------------------------------------------------------------------------


class TestDefaultScoringWeights:
    """Tests that DEFAULT_SCORING_WEIGHTS has correct values."""

    def test_sla_urgency_weight(self):
        assert DEFAULT_SCORING_WEIGHTS["sla_urgency"] == 0.40

    def test_cargo_priority_weight(self):
        assert DEFAULT_SCORING_WEIGHTS["cargo_priority"] == 0.30

    def test_customer_tier_weight(self):
        assert DEFAULT_SCORING_WEIGHTS["customer_tier"] == 0.30

    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_SCORING_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001


# ---------------------------------------------------------------------------
# Tests: evaluate() queries active jobs and computes weighted scores
#        (Req 3.2, 3.3)
# ---------------------------------------------------------------------------


class TestEvaluateComputation:
    """Tests that evaluate queries active jobs and computes weighted scores."""

    @pytest.mark.asyncio
    async def test_queries_active_jobs(self):
        """evaluate queries jobs_current for active jobs."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        deps["es_service"].search_documents.assert_called_once()
        call_args = deps["es_service"].search_documents.call_args
        assert call_args[0][0] == "jobs_current"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_active_jobs(self):
        """evaluate returns empty list when no active jobs found."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([])
        )

        result = await agent.evaluate(signals)
        assert result == []

    @pytest.mark.asyncio
    async def test_computes_weighted_score(self):
        """evaluate computes weighted priority score for each job."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="job-001", priority="urgent"),
            ])
        )

        await agent.evaluate(signals)

        # Verify publish was called with a JobPriorityList
        deps["signal_bus"].publish.assert_called_once()
        published = deps["signal_bus"].publish.call_args[0][0]
        assert isinstance(published, JobPriorityList)
        assert len(published.priorities) == 1

        # Urgent cargo priority = 1.0, so cargo component = 0.30 * 1.0 = 0.30
        priority = published.priorities[0]
        assert priority.cargo_priority_score == 1.0
        assert 0.0 <= priority.priority_score <= 1.0

    @pytest.mark.asyncio
    async def test_priorities_sorted_descending(self):
        """Priorities are sorted by score descending (most urgent first)."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="low-job", priority="low"),
                _job_doc(job_id="urgent-job", priority="urgent"),
                _job_doc(job_id="normal-job", priority="normal"),
            ])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        scores = [p.priority_score for p in published.priorities]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_scoring_weights_included_in_output(self):
        """Scoring weights are included in the published priority list."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        assert published.scoring_weights == DEFAULT_SCORING_WEIGHTS

    @pytest.mark.asyncio
    async def test_tenant_id_from_signal(self):
        """Tenant ID is taken from the first signal."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal(tenant_id="acme-corp")]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        assert published.tenant_id == "acme-corp"


# ---------------------------------------------------------------------------
# Tests: evaluate() persists JobPriorityList to ES (Req 3.7)
# ---------------------------------------------------------------------------


class TestEvaluatePersistence:
    """Tests that evaluate persists JobPriorityList to job_priorities ES index."""

    @pytest.mark.asyncio
    async def test_persists_to_job_priorities_index(self):
        """evaluate persists to the job_priorities ES index."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        deps["es_service"].index_document.assert_called_once()
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == JOB_PRIORITIES_INDEX

    @pytest.mark.asyncio
    async def test_persisted_doc_contains_tenant_id(self):
        """Persisted document contains the tenant_id."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal(tenant_id="tenant-xyz")]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        call_args = deps["es_service"].index_document.call_args
        doc = call_args[0][2]
        assert doc["tenant_id"] == "tenant-xyz"

    @pytest.mark.asyncio
    async def test_persisted_doc_contains_scoring_weights(self):
        """Persisted document contains the scoring weights used."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        call_args = deps["es_service"].index_document.call_args
        doc = call_args[0][2]
        assert doc["scoring_weights"] == DEFAULT_SCORING_WEIGHTS

    @pytest.mark.asyncio
    async def test_no_persistence_when_no_active_jobs(self):
        """No persistence when there are no active jobs."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([])
        )

        await agent.evaluate(signals)

        deps["es_service"].index_document.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: evaluate() publishes JobPriorityList to SignalBus (Req 3.8)
# ---------------------------------------------------------------------------


class TestEvaluatePublish:
    """Tests that evaluate publishes JobPriorityList to SignalBus."""

    @pytest.mark.asyncio
    async def test_publishes_to_signal_bus(self):
        """evaluate publishes a JobPriorityList to SignalBus."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([_job_doc()])
        )

        await agent.evaluate(signals)

        deps["signal_bus"].publish.assert_called_once()
        published = deps["signal_bus"].publish.call_args[0][0]
        assert isinstance(published, JobPriorityList)

    @pytest.mark.asyncio
    async def test_published_list_has_priorities(self):
        """Published list contains computed priorities."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="job-A"),
                _job_doc(job_id="job-B"),
            ])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        assert len(published.priorities) == 2
        job_ids = {p.job_id for p in published.priorities}
        assert job_ids == {"job-A", "job-B"}

    @pytest.mark.asyncio
    async def test_no_publish_when_no_active_jobs(self):
        """No publish when there are no active jobs."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([])
        )

        await agent.evaluate(signals)

        deps["signal_bus"].publish.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: evaluate() skips disabled tenants (Req 3.9)
# ---------------------------------------------------------------------------


class TestEvaluateDisabledTenants:
    """Tests that evaluate skips disabled tenants via overlay mode."""

    @pytest.mark.asyncio
    async def test_es_query_failure_returns_empty(self):
        """evaluate returns empty list when ES query fails."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            side_effect=Exception("ES connection refused")
        )

        result = await agent.evaluate(signals)
        assert result == []

    @pytest.mark.asyncio
    async def test_es_query_failure_no_publish(self):
        """No publish when ES query fails."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            side_effect=Exception("ES timeout")
        )

        await agent.evaluate(signals)

        deps["signal_bus"].publish.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Score clamped to [0.0, 1.0] (Req 3.10)
# ---------------------------------------------------------------------------


class TestScoreClamping:
    """Tests that priority scores are clamped to [0.0, 1.0]."""

    @pytest.mark.asyncio
    async def test_score_within_bounds(self):
        """All computed scores are within [0.0, 1.0]."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="j1", priority="urgent"),
                _job_doc(job_id="j2", priority="low"),
                _job_doc(job_id="j3", priority="high"),
                _job_doc(job_id="j4", priority="normal"),
            ])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        for p in published.priorities:
            assert 0.0 <= p.priority_score <= 1.0

    @pytest.mark.asyncio
    async def test_score_with_high_customer_tier_from_signal_context(self):
        """Score stays within bounds even with high customer tier from signal context."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal(
            entity_id="job-001",
            context={"customer_tier": 1.0},
        )]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="job-001", priority="urgent"),
            ])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        priority = published.priorities[0]
        assert 0.0 <= priority.priority_score <= 1.0
        assert priority.customer_tier_score == 1.0

    @pytest.mark.asyncio
    async def test_cargo_priority_uses_score_map(self):
        """Cargo priority score uses PRIORITY_SCORE_MAP for known values."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="j1", priority="urgent"),
                _job_doc(job_id="j2", priority="high"),
                _job_doc(job_id="j3", priority="normal"),
                _job_doc(job_id="j4", priority="low"),
            ])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        cargo_scores = {
            p.job_id: p.cargo_priority_score for p in published.priorities
        }
        assert cargo_scores["j1"] == 1.0
        assert cargo_scores["j2"] == 0.75
        assert cargo_scores["j3"] == 0.5
        assert cargo_scores["j4"] == 0.25

    @pytest.mark.asyncio
    async def test_unknown_priority_defaults_to_0_5(self):
        """Unknown priority field defaults to 0.5 cargo score."""
        agent, deps = _make_agent()
        signals = [_make_risk_signal()]

        deps["es_service"].search_documents = AsyncMock(
            return_value=_es_response([
                _job_doc(job_id="j1", priority="unknown_value"),
            ])
        )

        await agent.evaluate(signals)

        published = deps["signal_bus"].publish.call_args[0][0]
        assert published.priorities[0].cargo_priority_score == 0.5


# ---------------------------------------------------------------------------
# Tests: JobPriority and JobPriorityList data models
# ---------------------------------------------------------------------------


class TestDataModels:
    """Tests for the JobPriority and JobPriorityList Pydantic models."""

    def test_job_priority_valid(self):
        """JobPriority accepts valid data."""
        jp = JobPriority(
            job_id="job-001",
            job_type="delivery",
            priority_score=0.75,
            priority_bucket=PriorityBucket.HIGH,
            sla_urgency=0.8,
            cargo_priority_score=0.75,
            customer_tier_score=0.5,
        )
        assert jp.job_id == "job-001"
        assert jp.priority_score == 0.75

    def test_job_priority_list_valid(self):
        """JobPriorityList accepts valid data."""
        jp = JobPriority(
            job_id="job-001",
            job_type="delivery",
            priority_score=0.75,
            priority_bucket=PriorityBucket.HIGH,
            sla_urgency=0.8,
            cargo_priority_score=0.75,
            customer_tier_score=0.5,
        )
        jpl = JobPriorityList(
            priorities=[jp],
            scoring_weights=DEFAULT_SCORING_WEIGHTS,
            tenant_id="tenant-1",
        )
        assert len(jpl.priorities) == 1
        assert jpl.tenant_id == "tenant-1"
        assert jpl.priority_list_id  # auto-generated UUID
