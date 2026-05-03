"""
Unit tests for the DriverNudgeAgent overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Default and custom timeout/poll_interval values
- Module-level constants
- evaluate() returns empty (not used directly)
- monitor_cycle() with no unacked jobs
- monitor_cycle() generates nudge proposals past nudge_timeout
- monitor_cycle() generates escalation proposals past escalation_timeout
- Per-job nudge cooldown prevents duplicate nudges
- Per-job escalation cooldown prevents duplicate escalations
- _query_unacked_jobs() ES query structure
- _create_nudge_proposal() proposal structure
- _create_escalation_proposal() proposal structure
- ES query failure handling

Requirements: 15.1, 15.2, 15.3, 15.4
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.driver_nudge_agent import (
    DEFAULT_ESCALATION_TIMEOUT_MINUTES,
    DEFAULT_NUDGE_TIMEOUT_MINUTES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DriverNudgeAgent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps():
    """Create mocked dependencies for the DriverNudgeAgent."""
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
    activity_log.log = AsyncMock()

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
    deps = _make_deps()
    deps.update(overrides)
    return DriverNudgeAgent(**deps), deps


def _make_unacked_job(
    job_id="job-1",
    tenant_id="tenant-1",
    driver_id="driver-1",
    minutes_ago=12,
):
    """Create a mock unacked job document as returned from ES."""
    assigned_at = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat()
    return {
        "job_id": job_id,
        "tenant_id": tenant_id,
        "asset_assigned": driver_id,
        "status": "assigned",
        "assigned_at": assigned_at,
        "driver_acked": False,
    }


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_default_nudge_timeout(self):
        assert DEFAULT_NUDGE_TIMEOUT_MINUTES == 10

    def test_default_escalation_timeout(self):
        assert DEFAULT_ESCALATION_TIMEOUT_MINUTES == 15

    def test_default_poll_interval(self):
        assert DEFAULT_POLL_INTERVAL_SECONDS == 60


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "driver_nudge_agent"

    def test_no_signal_subscriptions(self):
        """DriverNudgeAgent polls ES directly, no signal subscriptions."""
        agent, _ = _make_agent()
        assert agent._subscription_specs == []

    def test_default_nudge_timeout(self):
        agent, _ = _make_agent()
        assert agent.nudge_timeout_minutes == DEFAULT_NUDGE_TIMEOUT_MINUTES

    def test_default_escalation_timeout(self):
        agent, _ = _make_agent()
        assert agent.escalation_timeout_minutes == DEFAULT_ESCALATION_TIMEOUT_MINUTES

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == DEFAULT_POLL_INTERVAL_SECONDS

    def test_custom_nudge_timeout(self):
        agent, _ = _make_agent(nudge_timeout_minutes=5)
        assert agent.nudge_timeout_minutes == 5

    def test_custom_escalation_timeout(self):
        agent, _ = _make_agent(escalation_timeout_minutes=20)
        assert agent.escalation_timeout_minutes == 20

    def test_custom_poll_interval(self):
        agent, _ = _make_agent(poll_interval=30)
        assert agent.poll_interval == 30

    def test_cooldown_dicts_initially_empty(self):
        agent, _ = _make_agent()
        assert agent._nudge_cooldowns == {}
        assert agent._escalation_cooldowns == {}


# ---------------------------------------------------------------------------
# Tests: evaluate() — not used directly
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_evaluate_returns_empty(self):
        """evaluate() is not used; returns empty list."""
        agent, _ = _make_agent()
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_evaluate_with_signals_returns_empty(self):
        agent, _ = _make_agent()
        signal = RiskSignal(
            source_agent="test",
            entity_id="job-1",
            entity_type="job",
            severity=Severity.HIGH,
            confidence=0.9,
            ttl_seconds=300,
            tenant_id="tenant-1",
        )
        result = await agent.evaluate([signal])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: monitor_cycle()
# ---------------------------------------------------------------------------


class TestMonitorCycle:
    @pytest.mark.asyncio
    async def test_no_unacked_jobs_returns_empty(self):
        """Req 15.3: When no unacked jobs, no proposals generated."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        detections, proposals = await agent.monitor_cycle()
        assert detections == []
        assert proposals == []

    @pytest.mark.asyncio
    async def test_generates_nudge_proposal(self):
        """Req 15.1: Nudge proposal generated when past nudge_timeout."""
        agent, deps = _make_agent()
        job = _make_unacked_job(job_id="job-1", minutes_ago=12)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        detections, proposals = await agent.monitor_cycle()

        assert len(detections) == 1
        # Should have a nudge proposal (12 min > 10 min nudge timeout)
        nudge_proposals = [
            p for p in proposals
            if p.actions[0]["parameters"]["notification_type"] == "assignment_reminder"
        ]
        assert len(nudge_proposals) == 1
        assert nudge_proposals[0].source_agent == "driver_nudge_agent"
        assert nudge_proposals[0].risk_class == RiskClass.LOW

    @pytest.mark.asyncio
    async def test_generates_escalation_proposal(self):
        """Req 15.2: Escalation proposal generated when past escalation_timeout."""
        agent, deps = _make_agent()
        job = _make_unacked_job(job_id="job-2", minutes_ago=16)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        detections, proposals = await agent.monitor_cycle()

        assert len(detections) == 1
        # Should have both nudge and escalation (16 min > 15 min escalation)
        escalation_proposals = [
            p for p in proposals
            if p.actions[0]["parameters"]["notification_type"] == "dispatcher_escalation"
        ]
        assert len(escalation_proposals) == 1
        assert escalation_proposals[0].risk_class == RiskClass.MEDIUM

    @pytest.mark.asyncio
    async def test_both_nudge_and_escalation_for_long_wait(self):
        """When past both timeouts, both nudge and escalation proposals generated."""
        agent, deps = _make_agent()
        job = _make_unacked_job(job_id="job-3", minutes_ago=20)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        detections, proposals = await agent.monitor_cycle()

        assert len(proposals) == 2
        types = {p.actions[0]["parameters"]["notification_type"] for p in proposals}
        assert types == {"assignment_reminder", "dispatcher_escalation"}

    @pytest.mark.asyncio
    async def test_no_proposal_before_nudge_timeout(self):
        """No proposals when assignment is within nudge_timeout."""
        agent, deps = _make_agent()
        # Job assigned 5 minutes ago — below 10 min nudge timeout
        # But the ES query filters by assigned_at, so this job wouldn't
        # be returned by ES. Simulate ES returning it anyway to test
        # the _generate_proposals logic.
        job = _make_unacked_job(job_id="job-4", minutes_ago=5)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        detections, proposals = await agent.monitor_cycle()

        # Job is returned by ES but below nudge timeout
        assert len(detections) == 1
        assert len(proposals) == 0

    @pytest.mark.asyncio
    async def test_multiple_unacked_jobs(self):
        """Multiple unacked jobs generate proposals for each."""
        agent, deps = _make_agent()
        jobs = [
            _make_unacked_job(job_id="job-a", minutes_ago=12),
            _make_unacked_job(job_id="job-b", minutes_ago=18),
        ]
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {"hits": [{"_source": j} for j in jobs]}
            }
        )

        detections, proposals = await agent.monitor_cycle()

        assert len(detections) == 2
        # job-a: nudge only (12 min), job-b: nudge + escalation (18 min)
        assert len(proposals) == 3

    @pytest.mark.asyncio
    async def test_es_query_failure_returns_empty(self):
        """ES query failure is handled gracefully."""
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=Exception("ES connection error")
        )

        detections, proposals = await agent.monitor_cycle()
        assert detections == []
        assert proposals == []

    @pytest.mark.asyncio
    async def test_cycle_metrics_updated(self):
        """Cycle metrics are updated after each cycle."""
        agent, deps = _make_agent()
        job = _make_unacked_job(job_id="job-m", minutes_ago=12)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        await agent.monitor_cycle()

        metrics = agent.cycle_metrics
        assert metrics["signals_consumed"] == 1
        assert metrics["proposals_generated"] >= 1
        assert metrics["cycle_duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Tests: Per-job cooldown (Req 15.4)
# ---------------------------------------------------------------------------


class TestCooldown:
    @pytest.mark.asyncio
    async def test_nudge_cooldown_prevents_duplicate(self):
        """Req 15.4: No duplicate nudges within cooldown period."""
        agent, deps = _make_agent()
        job = _make_unacked_job(job_id="job-cd", minutes_ago=12)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        # First cycle — should generate nudge
        _, proposals1 = await agent.monitor_cycle()
        nudge_count_1 = sum(
            1 for p in proposals1
            if p.actions[0]["parameters"]["notification_type"] == "assignment_reminder"
        )
        assert nudge_count_1 == 1

        # Second cycle — nudge should be suppressed by cooldown
        _, proposals2 = await agent.monitor_cycle()
        nudge_count_2 = sum(
            1 for p in proposals2
            if p.actions[0]["parameters"]["notification_type"] == "assignment_reminder"
        )
        assert nudge_count_2 == 0

    @pytest.mark.asyncio
    async def test_escalation_cooldown_prevents_duplicate(self):
        """Req 15.4: No duplicate escalations within cooldown period."""
        agent, deps = _make_agent()
        job = _make_unacked_job(job_id="job-esc", minutes_ago=16)
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        # First cycle — should generate escalation
        _, proposals1 = await agent.monitor_cycle()
        esc_count_1 = sum(
            1 for p in proposals1
            if p.actions[0]["parameters"]["notification_type"] == "dispatcher_escalation"
        )
        assert esc_count_1 == 1

        # Second cycle — escalation should be suppressed by cooldown
        _, proposals2 = await agent.monitor_cycle()
        esc_count_2 = sum(
            1 for p in proposals2
            if p.actions[0]["parameters"]["notification_type"] == "dispatcher_escalation"
        )
        assert esc_count_2 == 0

    def test_nudge_cooldown_check_no_entry(self):
        agent, _ = _make_agent()
        assert agent._is_on_nudge_cooldown("job-new") is False

    def test_nudge_cooldown_check_recent(self):
        agent, _ = _make_agent()
        agent._set_nudge_cooldown("job-recent")
        assert agent._is_on_nudge_cooldown("job-recent") is True

    def test_nudge_cooldown_check_expired(self):
        agent, _ = _make_agent()
        agent._nudge_cooldowns["job-old"] = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        )
        assert agent._is_on_nudge_cooldown("job-old") is False

    def test_escalation_cooldown_check_no_entry(self):
        agent, _ = _make_agent()
        assert agent._is_on_escalation_cooldown("job-new") is False

    def test_escalation_cooldown_check_recent(self):
        agent, _ = _make_agent()
        agent._set_escalation_cooldown("job-recent")
        assert agent._is_on_escalation_cooldown("job-recent") is True

    def test_escalation_cooldown_check_expired(self):
        agent, _ = _make_agent()
        agent._escalation_cooldowns["job-old"] = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        )
        assert agent._is_on_escalation_cooldown("job-old") is False


# ---------------------------------------------------------------------------
# Tests: Proposal structure
# ---------------------------------------------------------------------------


class TestProposalStructure:
    def test_nudge_proposal_structure(self):
        """Req 15.1: Nudge proposal has correct structure."""
        agent, _ = _make_agent()
        proposal = agent._create_nudge_proposal(
            job_id="job-1",
            tenant_id="tenant-1",
            driver_id="driver-1",
            minutes_waiting=12.5,
        )

        assert isinstance(proposal, InterventionProposal)
        assert proposal.source_agent == "driver_nudge_agent"
        assert proposal.tenant_id == "tenant-1"
        assert proposal.risk_class == RiskClass.LOW
        assert proposal.confidence == 0.9
        assert proposal.priority == 2
        assert len(proposal.actions) == 1

        action = proposal.actions[0]
        assert action["tool_name"] == "send_driver_nudge"
        assert action["parameters"]["job_id"] == "job-1"
        assert action["parameters"]["driver_id"] == "driver-1"
        assert action["parameters"]["notification_type"] == "assignment_reminder"
        assert action["parameters"]["message_template"] == "driver_nudge_reminder"
        assert action["parameters"]["minutes_waiting"] == 12.5

    def test_escalation_proposal_structure(self):
        """Req 15.2: Escalation proposal has correct structure."""
        agent, _ = _make_agent()
        proposal = agent._create_escalation_proposal(
            job_id="job-2",
            tenant_id="tenant-2",
            driver_id="driver-2",
            minutes_waiting=16.3,
        )

        assert isinstance(proposal, InterventionProposal)
        assert proposal.source_agent == "driver_nudge_agent"
        assert proposal.tenant_id == "tenant-2"
        assert proposal.risk_class == RiskClass.MEDIUM
        assert proposal.confidence == 0.95
        assert proposal.priority == 5
        assert len(proposal.actions) == 1

        action = proposal.actions[0]
        assert action["tool_name"] == "escalate_unresponsive_driver"
        assert action["parameters"]["job_id"] == "job-2"
        assert action["parameters"]["driver_id"] == "driver-2"
        assert action["parameters"]["notification_type"] == "dispatcher_escalation"
        assert action["parameters"]["message_template"] == "driver_escalation_alert"
        assert action["parameters"]["minutes_waiting"] == 16.3

    def test_nudge_proposal_has_expected_kpi_delta(self):
        agent, _ = _make_agent()
        proposal = agent._create_nudge_proposal(
            job_id="j", tenant_id="t", driver_id="d", minutes_waiting=11.0
        )
        assert "driver_ack_rate" in proposal.expected_kpi_delta
        assert "assignment_response_time" in proposal.expected_kpi_delta

    def test_escalation_proposal_has_expected_kpi_delta(self):
        agent, _ = _make_agent()
        proposal = agent._create_escalation_proposal(
            job_id="j", tenant_id="t", driver_id="d", minutes_waiting=16.0
        )
        assert "unresponsive_driver_resolution_time" in proposal.expected_kpi_delta
        assert "dispatcher_escalation_count" in proposal.expected_kpi_delta


# ---------------------------------------------------------------------------
# Tests: _query_unacked_jobs()
# ---------------------------------------------------------------------------


class TestQueryUnackedJobs:
    @pytest.mark.asyncio
    async def test_returns_job_sources(self):
        agent, deps = _make_agent()
        job = _make_unacked_job()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": [{"_source": job}]}}
        )

        result = await agent._query_unacked_jobs()
        assert len(result) == 1
        assert result[0]["job_id"] == "job-1"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_hits(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        result = await agent._query_unacked_jobs()
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_es_exception(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=Exception("Connection refused")
        )

        result = await agent._query_unacked_jobs()
        assert result == []

    @pytest.mark.asyncio
    async def test_query_uses_correct_index(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent._query_unacked_jobs()

        call_args = deps["es_service"].search_documents.call_args
        assert call_args[0][0] == "jobs_current"

    @pytest.mark.asyncio
    async def test_query_filters_assigned_status(self):
        agent, deps = _make_agent()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await agent._query_unacked_jobs()

        call_args = deps["es_service"].search_documents.call_args
        query = call_args[0][1]
        must_clauses = query["query"]["bool"]["must"]
        status_filter = next(
            (c for c in must_clauses if "term" in c and "status" in c["term"]),
            None,
        )
        assert status_filter is not None
        assert status_filter["term"]["status"] == "assigned"


# ---------------------------------------------------------------------------
# Tests: Edge cases in _generate_proposals
# ---------------------------------------------------------------------------


class TestGenerateProposalsEdgeCases:
    def test_skips_job_without_job_id(self):
        agent, _ = _make_agent()
        job = {"tenant_id": "t", "assigned_at": datetime.now(timezone.utc).isoformat()}
        proposals = agent._generate_proposals([job])
        assert proposals == []

    def test_skips_job_without_assigned_at(self):
        agent, _ = _make_agent()
        job = {"job_id": "j", "tenant_id": "t"}
        proposals = agent._generate_proposals([job])
        assert proposals == []

    def test_skips_job_with_invalid_assigned_at(self):
        agent, _ = _make_agent()
        job = {"job_id": "j", "tenant_id": "t", "assigned_at": "not-a-date"}
        proposals = agent._generate_proposals([job])
        assert proposals == []

    def test_uses_driver_id_fallback(self):
        """Falls back to driver_id field when asset_assigned is missing."""
        agent, _ = _make_agent()
        assigned_at = (
            datetime.now(timezone.utc) - timedelta(minutes=12)
        ).isoformat()
        job = {
            "job_id": "j",
            "tenant_id": "t",
            "driver_id": "fallback-driver",
            "assigned_at": assigned_at,
        }
        proposals = agent._generate_proposals([job])
        assert len(proposals) >= 1
        assert proposals[0].actions[0]["parameters"]["driver_id"] == "fallback-driver"
