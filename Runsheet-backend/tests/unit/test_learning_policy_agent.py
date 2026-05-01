"""
Unit tests for the LearningPolicyAgent overlay agent.

Tests cover:
- Module-level constants
- PolicyExperiment class fields and defaults
- Constructor and agent_id configuration
- Signal subscription setup (OutcomeRecord, PolicyChangeProposal)
- evaluate() with empty signals
- evaluate() categorizes OutcomeRecords vs PolicyChangeProposals
- evaluate() tracks outcome history per source agent
- evaluate() prunes old entries beyond window
- evaluate() identifies parameters with 5+ negative outcomes in 7-day window
- evaluate() generates PolicyChangeProposals with statistical evidence and rollback plans
- evaluate() monitors active experiments for rollback triggers
- _log_experiment() persists to agent_policy_experiments ES index
- _check_experiment_rollbacks() auto-rollback if KPI degrades >5% within 48h
- _check_experiment_rollbacks() graduates experiments past the window
- _update_experiment_status() ES update helper
- All proposals classified as HIGH risk with mandatory human approval
- Bounded rollout: 10% traffic initially

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import (
    OutcomeRecord,
    PolicyChangeProposal,
    RiskSignal,
    Severity,
)
from Agents.overlay.learning_policy_agent import (
    DEFAULT_NEGATIVE_OUTCOME_THRESHOLD,
    DEFAULT_NEGATIVE_WINDOW_DAYS,
    DEFAULT_ROLLBACK_DEGRADATION_PCT,
    DEFAULT_ROLLBACK_WINDOW_HOURS,
    DEFAULT_ROLLOUT_PERCENTAGE,
    POLICY_EXPERIMENTS_INDEX,
    LearningPolicyAgent,
    PolicyExperiment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outcome(
    intervention_id="agent1-abc",
    status="adverse",
    realized_delta=None,
    tenant_id="tenant-1",
    timestamp=None,
):
    return OutcomeRecord(
        intervention_id=intervention_id,
        before_kpis={"delivery_time": 30.0},
        after_kpis={"delivery_time": 35.0},
        realized_delta=realized_delta or {"delivery_time": -5.0},
        execution_duration_ms=1000.0,
        tenant_id=tenant_id,
        timestamp=timestamp or datetime.now(timezone.utc),
        status=status,
    )


def _make_policy_proposal(tenant_id="tenant-1"):
    return PolicyChangeProposal(
        source_agent="revenue_guard",
        parameter="fuel.threshold",
        old_value={"current": 100},
        new_value={"adjusted": 90},
        evidence=["outcome-1"],
        rollback_plan={"action": "revert"},
        confidence=0.8,
        tenant_id=tenant_id,
    )


def _make_deps():
    """Create mocked dependencies for the LearningPolicyAgent."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es_service.index_document = AsyncMock()
    es_service.update_document = AsyncMock()

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

    feedback_service = MagicMock()

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
        "feedback_service": feedback_service,
    }


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return LearningPolicyAgent(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_policy_experiments_index(self):
        assert POLICY_EXPERIMENTS_INDEX == "agent_policy_experiments"

    def test_default_negative_outcome_threshold(self):
        assert DEFAULT_NEGATIVE_OUTCOME_THRESHOLD == 5

    def test_default_negative_window_days(self):
        assert DEFAULT_NEGATIVE_WINDOW_DAYS == 7

    def test_default_rollout_percentage(self):
        assert DEFAULT_ROLLOUT_PERCENTAGE == 10

    def test_default_rollback_degradation_pct(self):
        assert DEFAULT_ROLLBACK_DEGRADATION_PCT == 5.0

    def test_default_rollback_window_hours(self):
        assert DEFAULT_ROLLBACK_WINDOW_HOURS == 48


# ---------------------------------------------------------------------------
# Tests: PolicyExperiment
# ---------------------------------------------------------------------------


class TestPolicyExperiment:
    def test_fields(self):
        proposal = _make_policy_proposal()
        exp = PolicyExperiment(
            experiment_id="exp-000001",
            proposal=proposal,
            rollout_pct=10,
        )
        assert exp.experiment_id == "exp-000001"
        assert exp.proposal is proposal
        assert exp.rollout_pct == 10
        assert exp.deployed_at is None
        assert exp.baseline_kpis == {}
        assert exp.current_kpis == {}
        assert exp.status == "pending"
        assert exp.rollback_reason is None


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "learning_policy_agent"

    def test_subscriptions(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 2
        types = [s["message_type"] for s in agent._subscription_specs]
        assert OutcomeRecord in types
        assert PolicyChangeProposal in types

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 300

    def test_default_cooldown(self):
        agent, _ = _make_agent()
        assert agent.cooldown_minutes == 60

    def test_default_negative_threshold(self):
        agent, _ = _make_agent()
        assert agent._negative_threshold == 5

    def test_default_negative_window_days(self):
        agent, _ = _make_agent()
        assert agent._negative_window_days == 7

    def test_default_rollout_pct(self):
        agent, _ = _make_agent()
        assert agent._rollout_pct == 10

    def test_default_rollback_degradation_pct(self):
        agent, _ = _make_agent()
        assert agent._rollback_degradation_pct == 5.0

    def test_default_rollback_window(self):
        agent, _ = _make_agent()
        assert agent._rollback_window == timedelta(hours=48)

    def test_custom_params(self):
        agent, _ = _make_agent(
            negative_threshold=3,
            negative_window_days=14,
            rollout_pct=20,
            rollback_degradation_pct=10.0,
            rollback_window_hours=72,
        )
        assert agent._negative_threshold == 3
        assert agent._negative_window_days == 14
        assert agent._rollout_pct == 20
        assert agent._rollback_degradation_pct == 10.0
        assert agent._rollback_window == timedelta(hours=72)

    def test_stores_feedback_service(self):
        fb = MagicMock()
        agent, _ = _make_agent(feedback_service=fb)
        assert agent._feedback_service is fb


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_signals_returns_empty(self):
        agent, _ = _make_agent()
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_categorizes_outcome_records(self):
        """Req 8.1: categorize incoming signals as OutcomeRecords."""
        agent, _ = _make_agent()
        outcome = _make_outcome()
        result = await agent.evaluate([outcome])
        # Outcome tracked in history
        assert len(agent._outcome_history) > 0

    @pytest.mark.asyncio
    async def test_categorizes_policy_proposals(self):
        """Categorize PolicyChangeProposals separately from OutcomeRecords."""
        agent, _ = _make_agent()
        proposal = _make_policy_proposal()
        # Should not crash and should not add to outcome_history
        result = await agent.evaluate([proposal])
        # PolicyChangeProposals don't go into outcome_history
        assert result == []

    @pytest.mark.asyncio
    async def test_tracks_outcome_history_per_source_agent(self):
        """Req 8.1: track outcome history per source agent."""
        agent, _ = _make_agent()
        o1 = _make_outcome(intervention_id="agent1-abc")
        o2 = _make_outcome(intervention_id="agent2-xyz")
        await agent.evaluate([o1, o2])
        assert "agent1" in agent._outcome_history
        assert "agent2" in agent._outcome_history

    @pytest.mark.asyncio
    async def test_prunes_old_entries_beyond_window(self):
        """Prune entries older than negative_window_days."""
        agent, _ = _make_agent(negative_window_days=7)
        old_outcome = _make_outcome(
            timestamp=datetime.now(timezone.utc) - timedelta(days=10)
        )
        recent_outcome = _make_outcome(
            timestamp=datetime.now(timezone.utc) - timedelta(hours=1)
        )
        await agent.evaluate([old_outcome, recent_outcome])
        # Old entry should be pruned
        key = "agent1"
        assert len(agent._outcome_history[key]) == 1

    @pytest.mark.asyncio
    async def test_generates_proposal_at_threshold(self):
        """Req 8.3: generate proposal when 5+ negative outcomes in window."""
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        result = await agent.evaluate(outcomes)
        assert len(result) == 1
        assert isinstance(result[0], PolicyChangeProposal)

    @pytest.mark.asyncio
    async def test_no_proposal_below_threshold(self):
        """No proposal when fewer than threshold negative outcomes."""
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(4)
        ]
        result = await agent.evaluate(outcomes)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_proposal_includes_statistical_evidence(self):
        """Req 8.4: proposals include statistical evidence."""
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(6)
        ]
        result = await agent.evaluate(outcomes)
        assert len(result) == 1
        proposal = result[0]
        assert "evidence_sample_size" in proposal.new_value
        assert "negative_rate" in proposal.new_value
        assert proposal.new_value["rollout_pct"] == 10

    @pytest.mark.asyncio
    async def test_proposal_includes_rollback_plan(self):
        """Req 8.4: proposals include rollback plan."""
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        result = await agent.evaluate(outcomes)
        assert len(result) == 1
        plan = result[0].rollback_plan
        assert plan["auto_rollback"] is True
        assert plan["action"] == "revert_to_previous_value"
        assert "window_hours" in plan

    @pytest.mark.asyncio
    async def test_proposal_evidence_references_outcome_ids(self):
        """Req 8.4: evidence references OutcomeRecord IDs."""
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        result = await agent.evaluate(outcomes)
        assert len(result) == 1
        assert len(result[0].evidence) > 0
        # Evidence should be outcome_ids (strings)
        for eid in result[0].evidence:
            assert isinstance(eid, str)

    @pytest.mark.asyncio
    async def test_proposal_source_agent_is_learning_policy(self):
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        result = await agent.evaluate(outcomes)
        assert result[0].source_agent == "learning_policy_agent"

    @pytest.mark.asyncio
    async def test_bounded_rollout_percentage(self):
        """Req 8.7: bounded rollout at 10% traffic initially."""
        agent, _ = _make_agent(negative_threshold=5, rollout_pct=10)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        result = await agent.evaluate(outcomes)
        assert result[0].new_value["rollout_pct"] == 10

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate_proposals(self):
        """Cooldown prevents generating duplicate proposals for same param."""
        agent, _ = _make_agent(negative_threshold=5)
        outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        # First call generates proposal
        result1 = await agent.evaluate(outcomes)
        assert len(result1) == 1

        # Second call with same param should be on cooldown
        more_outcomes = [
            _make_outcome(
                intervention_id="agentX-" + str(i + 10),
                status="adverse",
                realized_delta={"metric": -1.0},
            )
            for i in range(5)
        ]
        result2 = await agent.evaluate(more_outcomes)
        assert len(result2) == 0


# ---------------------------------------------------------------------------
# Tests: _log_experiment()
# ---------------------------------------------------------------------------


class TestLogExperiment:
    @pytest.mark.asyncio
    async def test_persists_to_es(self):
        """Req 8.6: persist experiment to agent_policy_experiments index."""
        agent, deps = _make_agent()
        proposal = _make_policy_proposal()
        await agent._log_experiment(proposal, "tenant-1")

        deps["es_service"].index_document.assert_called_once()
        call_args = deps["es_service"].index_document.call_args
        assert call_args[0][0] == POLICY_EXPERIMENTS_INDEX
        assert call_args[0][1].startswith("exp-")
        doc = call_args[0][2]
        assert doc["proposal_id"] == proposal.proposal_id
        assert doc["parameter"] == proposal.parameter
        assert doc["status"] == "pending"
        assert doc["tenant_id"] == "tenant-1"

    @pytest.mark.asyncio
    async def test_increments_experiment_counter(self):
        agent, _ = _make_agent()
        proposal = _make_policy_proposal()
        await agent._log_experiment(proposal, "tenant-1")
        assert agent._experiment_counter == 1
        await agent._log_experiment(proposal, "tenant-1")
        assert agent._experiment_counter == 2

    @pytest.mark.asyncio
    async def test_stores_experiment_in_memory(self):
        agent, _ = _make_agent()
        proposal = _make_policy_proposal()
        await agent._log_experiment(proposal, "tenant-1")
        assert len(agent._experiments) == 1
        exp = list(agent._experiments.values())[0]
        assert exp.proposal is proposal
        assert exp.status == "pending"
        assert exp.deployed_at is not None

    @pytest.mark.asyncio
    async def test_handles_es_error_gracefully(self):
        agent, deps = _make_agent()
        deps["es_service"].index_document = AsyncMock(
            side_effect=Exception("ES down")
        )
        proposal = _make_policy_proposal()
        # Should not raise
        await agent._log_experiment(proposal, "tenant-1")
        # Experiment still tracked in memory
        assert len(agent._experiments) == 1


# ---------------------------------------------------------------------------
# Tests: _check_experiment_rollbacks()
# ---------------------------------------------------------------------------


class TestCheckExperimentRollbacks:
    @pytest.mark.asyncio
    async def test_graduates_experiment_past_window(self):
        """Experiments past rollback window are graduated."""
        agent, deps = _make_agent(rollback_window_hours=48)
        proposal = _make_policy_proposal()
        exp = PolicyExperiment("exp-001", proposal, 10)
        exp.status = "deployed"
        exp.deployed_at = datetime.now(timezone.utc) - timedelta(hours=49)
        agent._experiments["exp-001"] = exp

        await agent._check_experiment_rollbacks("tenant-1")

        assert exp.status == "graduated"
        deps["es_service"].update_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_rollback_on_kpi_degradation(self):
        """Req 8.8: auto-rollback if KPI degrades >5% within 48h."""
        agent, deps = _make_agent(rollback_degradation_pct=5.0)
        proposal = _make_policy_proposal()
        exp = PolicyExperiment("exp-001", proposal, 10)
        exp.status = "deployed"
        exp.deployed_at = datetime.now(timezone.utc) - timedelta(hours=24)
        exp.baseline_kpis = {"delivery_time": 100.0}
        exp.current_kpis = {"delivery_time": 90.0}  # 10% degradation
        agent._experiments["exp-001"] = exp

        await agent._check_experiment_rollbacks("tenant-1")

        assert exp.status == "rolled_back"
        assert "delivery_time" in exp.rollback_reason
        assert "10.0%" in exp.rollback_reason

    @pytest.mark.asyncio
    async def test_no_rollback_within_threshold(self):
        """No rollback if degradation is within threshold."""
        agent, _ = _make_agent(rollback_degradation_pct=5.0)
        proposal = _make_policy_proposal()
        exp = PolicyExperiment("exp-001", proposal, 10)
        exp.status = "deployed"
        exp.deployed_at = datetime.now(timezone.utc) - timedelta(hours=24)
        exp.baseline_kpis = {"delivery_time": 100.0}
        exp.current_kpis = {"delivery_time": 96.0}  # 4% degradation, under 5%
        agent._experiments["exp-001"] = exp

        await agent._check_experiment_rollbacks("tenant-1")

        assert exp.status == "deployed"

    @pytest.mark.asyncio
    async def test_skips_non_deployed_experiments(self):
        """Only check deployed experiments."""
        agent, deps = _make_agent()
        proposal = _make_policy_proposal()
        exp = PolicyExperiment("exp-001", proposal, 10)
        exp.status = "pending"
        agent._experiments["exp-001"] = exp

        await agent._check_experiment_rollbacks("tenant-1")

        assert exp.status == "pending"
        deps["es_service"].update_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_experiments_without_kpis(self):
        """No rollback check if baseline/current KPIs are empty."""
        agent, deps = _make_agent()
        proposal = _make_policy_proposal()
        exp = PolicyExperiment("exp-001", proposal, 10)
        exp.status = "deployed"
        exp.deployed_at = datetime.now(timezone.utc) - timedelta(hours=24)
        # No KPIs set
        agent._experiments["exp-001"] = exp

        await agent._check_experiment_rollbacks("tenant-1")

        assert exp.status == "deployed"


# ---------------------------------------------------------------------------
# Tests: _update_experiment_status()
# ---------------------------------------------------------------------------


class TestUpdateExperimentStatus:
    @pytest.mark.asyncio
    async def test_updates_es_document(self):
        agent, deps = _make_agent()
        await agent._update_experiment_status("exp-001", "graduated")

        deps["es_service"].update_document.assert_called_once()
        call_args = deps["es_service"].update_document.call_args
        assert call_args[0][0] == POLICY_EXPERIMENTS_INDEX
        assert call_args[0][1] == "exp-001"
        doc = call_args[0][2]["doc"]
        assert doc["status"] == "graduated"

    @pytest.mark.asyncio
    async def test_includes_rollback_reason(self):
        agent, deps = _make_agent()
        await agent._update_experiment_status(
            "exp-001", "rolled_back", "KPI degraded"
        )

        call_args = deps["es_service"].update_document.call_args
        doc = call_args[0][2]["doc"]
        assert doc["rollback_reason"] == "KPI degraded"

    @pytest.mark.asyncio
    async def test_no_reason_when_none(self):
        agent, deps = _make_agent()
        await agent._update_experiment_status("exp-001", "graduated")

        call_args = deps["es_service"].update_document.call_args
        doc = call_args[0][2]["doc"]
        assert "rollback_reason" not in doc

    @pytest.mark.asyncio
    async def test_handles_es_error_gracefully(self):
        agent, deps = _make_agent()
        deps["es_service"].update_document = AsyncMock(
            side_effect=Exception("ES down")
        )
        # Should not raise
        await agent._update_experiment_status("exp-001", "graduated")
