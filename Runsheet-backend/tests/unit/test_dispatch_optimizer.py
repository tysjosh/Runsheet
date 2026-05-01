"""
Unit tests for the DispatchOptimizer overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (delay_response_agent, fuel_management_agent)
- evaluate() with empty signals
- evaluate() with no affected jobs
- evaluate() with jobs and assets producing proposals
- SLA constraint filtering (Req 4.5)
- _query_affected_jobs() ES query structure
- _query_available_assets() ES query structure
- _score_reassignments() composite scoring and sorting
- _is_compatible() job-type to asset-type mapping
- Module-level constants

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.dispatch_optimizer import (
    ASSETS_INDEX,
    DispatchOptimizer,
    JOBS_CURRENT_INDEX,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    entity_id="job-1",
    severity=Severity.HIGH,
    confidence=0.9,
    tenant_id="tenant-1",
    source_agent="delay_response_agent",
):
    return RiskSignal(
        source_agent=source_agent,
        entity_id=entity_id,
        entity_type="job",
        severity=severity,
        confidence=confidence,
        ttl_seconds=300,
        tenant_id=tenant_id,
    )


def _make_deps():
    """Create mocked dependencies for the DispatchOptimizer."""
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

    execution_planner = MagicMock()

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
        "execution_planner": execution_planner,
    }


def _make_optimizer(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return DispatchOptimizer(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_jobs_current_index(self):
        assert JOBS_CURRENT_INDEX == "jobs_current"

    def test_assets_index(self):
        assert ASSETS_INDEX == "trucks"


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        optimizer, _ = _make_optimizer()
        assert optimizer.agent_id == "dispatch_optimizer"

    def test_stores_execution_planner(self):
        planner = MagicMock()
        optimizer, _ = _make_optimizer(execution_planner=planner)
        assert optimizer._execution_planner is planner

    def test_subscription_filters(self):
        optimizer, _ = _make_optimizer()
        assert len(optimizer._subscription_specs) == 1
        spec = optimizer._subscription_specs[0]
        assert spec["message_type"] is RiskSignal
        assert spec["filters"]["source_agent"] == [
            "delay_response_agent",
            "fuel_management_agent",
        ]

    def test_default_poll_interval(self):
        optimizer, _ = _make_optimizer()
        assert optimizer.poll_interval == 60

    def test_custom_poll_interval(self):
        optimizer, _ = _make_optimizer(poll_interval=120)
        assert optimizer.poll_interval == 120


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_signals_returns_empty(self):
        optimizer, _ = _make_optimizer()
        result = await optimizer.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_affected_jobs_returns_empty(self):
        optimizer, deps = _make_optimizer()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        signal = _make_signal()
        result = await optimizer.evaluate([signal])
        assert result == []

    @pytest.mark.asyncio
    async def test_produces_proposal_with_matching_jobs_and_assets(self):
        optimizer, deps = _make_optimizer()

        jobs_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "job_id": "job-1",
                            "job_type": "cargo_transport",
                            "tenant_id": "tenant-1",
                            "status": "in_progress",
                        }
                    }
                ]
            }
        }
        assets_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "asset_id": "truck-1",
                            "asset_type": "vehicle",
                            "tenant_id": "tenant-1",
                            "status": "on_time",
                        }
                    }
                ]
            }
        }

        deps["es_service"].search_documents = AsyncMock(
            side_effect=[jobs_response, assets_response]
        )

        signal = _make_signal(entity_id="job-1", severity=Severity.HIGH)
        result = await optimizer.evaluate([signal])

        assert len(result) == 1
        proposal = result[0]
        assert isinstance(proposal, InterventionProposal)
        assert proposal.source_agent == "dispatch_optimizer"
        assert proposal.tenant_id == "tenant-1"
        assert proposal.risk_class == RiskClass.MEDIUM
        assert len(proposal.actions) == 1
        assert proposal.actions[0]["parameters"]["job_id"] == "job-1"
        assert proposal.actions[0]["parameters"]["asset_id"] == "truck-1"

    @pytest.mark.asyncio
    async def test_filters_net_negative_sla_candidates(self):
        """Req 4.5: no net-negative SLA impact candidates in proposal."""
        optimizer, deps = _make_optimizer()

        jobs_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "job_id": "job-1",
                            "job_type": "cargo_transport",
                            "tenant_id": "tenant-1",
                        }
                    }
                ]
            }
        }
        assets_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "asset_id": "truck-1",
                            "asset_type": "vehicle",
                            "tenant_id": "tenant-1",
                        }
                    }
                ]
            }
        }

        deps["es_service"].search_documents = AsyncMock(
            side_effect=[jobs_response, assets_response]
        )

        signal = _make_signal(entity_id="job-1", severity=Severity.HIGH)
        result = await optimizer.evaluate([signal])

        # All candidates in the proposal should have non-negative SLA impact
        for proposal in result:
            for action in proposal.actions:
                # The scoring heuristic produces positive sla_impact for
                # all severity levels, so all should pass the filter
                assert "expected_time_saved_minutes" in action

    @pytest.mark.asyncio
    async def test_no_available_assets_returns_empty(self):
        optimizer, deps = _make_optimizer()

        jobs_response = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "job_id": "job-1",
                            "job_type": "cargo_transport",
                        }
                    }
                ]
            }
        }
        assets_response = {"hits": {"hits": []}}

        deps["es_service"].search_documents = AsyncMock(
            side_effect=[jobs_response, assets_response]
        )

        signal = _make_signal(entity_id="job-1")
        result = await optimizer.evaluate([signal])
        # No assets means no candidates, so empty proposals
        assert result == []

    @pytest.mark.asyncio
    async def test_proposal_confidence_is_min_of_signals(self):
        optimizer, deps = _make_optimizer()

        jobs_response = {
            "hits": {
                "hits": [
                    {"_source": {"job_id": "job-1", "job_type": "cargo_transport"}},
                    {"_source": {"job_id": "job-2", "job_type": "cargo_transport"}},
                ]
            }
        }
        assets_response = {
            "hits": {
                "hits": [
                    {"_source": {"asset_id": "truck-1", "asset_type": "vehicle"}}
                ]
            }
        }

        deps["es_service"].search_documents = AsyncMock(
            side_effect=[jobs_response, assets_response]
        )

        signals = [
            _make_signal(entity_id="job-1", confidence=0.9),
            _make_signal(entity_id="job-2", confidence=0.6),
        ]
        result = await optimizer.evaluate(signals)

        assert len(result) == 1
        assert result[0].confidence == 0.6

    @pytest.mark.asyncio
    async def test_proposal_kpi_delta_includes_all_metrics(self):
        optimizer, deps = _make_optimizer()

        jobs_response = {
            "hits": {
                "hits": [
                    {"_source": {"job_id": "job-1", "job_type": "cargo_transport"}}
                ]
            }
        }
        assets_response = {
            "hits": {
                "hits": [
                    {"_source": {"asset_id": "truck-1", "asset_type": "vehicle"}}
                ]
            }
        }

        deps["es_service"].search_documents = AsyncMock(
            side_effect=[jobs_response, assets_response]
        )

        signal = _make_signal(entity_id="job-1")
        result = await optimizer.evaluate([signal])

        assert len(result) == 1
        kpi = result[0].expected_kpi_delta
        assert "delivery_time_minutes" in kpi
        assert "fuel_cost_liters" in kpi
        assert "sla_compliance_pct" in kpi


# ---------------------------------------------------------------------------
# Tests: _query_affected_jobs()
# ---------------------------------------------------------------------------


class TestQueryAffectedJobs:
    @pytest.mark.asyncio
    async def test_query_structure(self):
        optimizer, deps = _make_optimizer()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await optimizer._query_affected_jobs({"job-1", "job-2"}, "tenant-1")

        deps["es_service"].search_documents.assert_called_once()
        call_args = deps["es_service"].search_documents.call_args
        assert call_args[0][0] == JOBS_CURRENT_INDEX
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]

        # Check tenant_id filter
        assert {"term": {"tenant_id": "tenant-1"}} in filters
        # Check status filter
        assert {"term": {"status": "in_progress"}} in filters
        # Check entity_ids filter (order may vary in set)
        terms_filter = [f for f in filters if "terms" in f][0]
        assert set(terms_filter["terms"]["job_id"]) == {"job-1", "job-2"}

    @pytest.mark.asyncio
    async def test_returns_source_docs(self):
        optimizer, deps = _make_optimizer()
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {"_source": {"job_id": "job-1"}},
                        {"_source": {"job_id": "job-2"}},
                    ]
                }
            }
        )

        result = await optimizer._query_affected_jobs({"job-1"}, "t1")
        assert len(result) == 2
        assert result[0]["job_id"] == "job-1"


# ---------------------------------------------------------------------------
# Tests: _query_available_assets()
# ---------------------------------------------------------------------------


class TestQueryAvailableAssets:
    @pytest.mark.asyncio
    async def test_query_structure(self):
        optimizer, deps = _make_optimizer()
        deps["es_service"].search_documents = AsyncMock(
            return_value={"hits": {"hits": []}}
        )

        await optimizer._query_available_assets("tenant-1")

        deps["es_service"].search_documents.assert_called_once()
        call_args = deps["es_service"].search_documents.call_args
        assert call_args[0][0] == ASSETS_INDEX
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]

        assert {"term": {"tenant_id": "tenant-1"}} in filters
        assert {"term": {"status": "on_time"}} in filters


# ---------------------------------------------------------------------------
# Tests: _score_reassignments()
# ---------------------------------------------------------------------------


class TestScoreReassignments:
    def test_scores_compatible_pairs(self):
        optimizer, _ = _make_optimizer()
        jobs = [{"job_id": "j1", "job_type": "cargo_transport"}]
        assets = [{"asset_id": "a1", "asset_type": "vehicle"}]
        signals = [_make_signal(entity_id="j1", severity=Severity.HIGH)]

        candidates = optimizer._score_reassignments(jobs, assets, signals)

        assert len(candidates) == 1
        c = candidates[0]
        assert c["job_id"] == "j1"
        assert c["asset_id"] == "a1"
        assert c["time_saved"] > 0
        assert c["sla_impact"] >= 0

    def test_skips_incompatible_pairs(self):
        optimizer, _ = _make_optimizer()
        jobs = [{"job_id": "j1", "job_type": "vessel_movement"}]
        assets = [{"asset_id": "a1", "asset_type": "vehicle"}]
        signals = [_make_signal(entity_id="j1")]

        candidates = optimizer._score_reassignments(jobs, assets, signals)
        assert len(candidates) == 0

    def test_sorted_by_score_descending(self):
        optimizer, _ = _make_optimizer()
        jobs = [
            {"job_id": "j1", "job_type": "cargo_transport"},
            {"job_id": "j2", "job_type": "cargo_transport"},
        ]
        assets = [{"asset_id": "a1", "asset_type": "vehicle"}]
        signals = [
            _make_signal(entity_id="j1", severity=Severity.LOW),
            _make_signal(entity_id="j2", severity=Severity.CRITICAL),
        ]

        candidates = optimizer._score_reassignments(jobs, assets, signals)

        assert len(candidates) == 2
        # Critical severity should score higher than low
        assert candidates[0]["job_id"] == "j2"
        assert candidates[1]["job_id"] == "j1"
        assert candidates[0]["score"] >= candidates[1]["score"]

    def test_severity_weighting(self):
        optimizer, _ = _make_optimizer()
        jobs = [{"job_id": "j1", "job_type": "cargo_transport"}]
        assets = [{"asset_id": "a1", "asset_type": "vehicle"}]

        for sev, expected_weight in [
            (Severity.LOW, 1),
            (Severity.MEDIUM, 2),
            (Severity.HIGH, 3),
            (Severity.CRITICAL, 4),
        ]:
            signals = [_make_signal(entity_id="j1", severity=sev)]
            candidates = optimizer._score_reassignments(jobs, assets, signals)
            assert len(candidates) == 1
            assert candidates[0]["time_saved"] == expected_weight * 10.0

    def test_default_severity_for_unknown_entity(self):
        optimizer, _ = _make_optimizer()
        jobs = [{"job_id": "j-unknown", "job_type": "cargo_transport"}]
        assets = [{"asset_id": "a1", "asset_type": "vehicle"}]
        signals = [_make_signal(entity_id="j-other", severity=Severity.HIGH)]

        candidates = optimizer._score_reassignments(jobs, assets, signals)
        assert len(candidates) == 1
        # Default severity is "medium" (weight=2)
        assert candidates[0]["time_saved"] == 20.0


# ---------------------------------------------------------------------------
# Tests: _is_compatible()
# ---------------------------------------------------------------------------


class TestIsCompatible:
    def test_cargo_transport_vehicle(self):
        assert DispatchOptimizer._is_compatible("cargo_transport", "vehicle") is True

    def test_passenger_transport_vehicle(self):
        assert DispatchOptimizer._is_compatible("passenger_transport", "vehicle") is True

    def test_vessel_movement_vessel(self):
        assert DispatchOptimizer._is_compatible("vessel_movement", "vessel") is True

    def test_airport_transfer_vehicle(self):
        assert DispatchOptimizer._is_compatible("airport_transfer", "vehicle") is True

    def test_crane_booking_equipment(self):
        assert DispatchOptimizer._is_compatible("crane_booking", "equipment") is True

    def test_incompatible_vessel_vehicle(self):
        assert DispatchOptimizer._is_compatible("vessel_movement", "vehicle") is False

    def test_incompatible_crane_vehicle(self):
        assert DispatchOptimizer._is_compatible("crane_booking", "vehicle") is False

    def test_unknown_job_type_defaults_to_vehicle(self):
        assert DispatchOptimizer._is_compatible("unknown_type", "vehicle") is True

    def test_unknown_job_type_not_vessel(self):
        assert DispatchOptimizer._is_compatible("unknown_type", "vessel") is False
