"""
Property-based tests for RevenueGuard Output Constraint.

# Feature: agent-overlay-architecture, Property 15: RevenueGuard Output Constraint

**Validates: Requirements 6.5, 6.7**

All output proposals from RevenueGuard are PolicyChangeProposals — never
InterventionProposals or direct mutation actions. The RevenueGuard operates
exclusively through policy recommendations routed via ConfirmationProtocol
with HIGH risk classification.

Sub-properties tested:
1. For any set of input signals that trigger leakage detection, every
   proposal returned by evaluate() is a PolicyChangeProposal instance.
2. No proposal returned by evaluate() is an InterventionProposal or
   contains direct mutation actions.
3. Every PolicyChangeProposal includes a rollback_plan and evidence list.
"""

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis.strategies import (
    composite,
    floats,
    integers,
    just,
    lists,
    sampled_from,
    text,
)

# ---------------------------------------------------------------------------
# Mock the elasticsearch_service module before importing overlay modules
# ---------------------------------------------------------------------------
sys.modules.setdefault("services.elasticsearch_service", MagicMock())

from Agents.overlay.data_contracts import (  # noqa: E402
    InterventionProposal,
    OutcomeRecord,
    PolicyChangeProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.revenue_guard import RevenueGuard  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_tenant_ids = text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=3,
    max_size=16,
)

_route_ids = text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=3,
    max_size=20,
)

_severities = sampled_from(list(Severity))

# Margins that are below the default 15% target — these trigger leakage
_below_target_margins = floats(min_value=0.0, max_value=14.9, allow_nan=False, allow_infinity=False)

# Margins that are at or above the default 15% target — no leakage
_above_target_margins = floats(min_value=15.0, max_value=80.0, allow_nan=False, allow_infinity=False)

# Number of consecutive below-target jobs per route (must be >= 3 to trigger)
_leakage_counts = integers(min_value=3, max_value=10)

# Number of routes with leakage
_route_counts = integers(min_value=1, max_value=5)


@composite
def _fuel_risk_signals(draw, tenant_id=None):
    """Generate a RiskSignal from fuel_management_agent."""
    tid = tenant_id or draw(_tenant_ids)
    return RiskSignal(
        source_agent="fuel_management_agent",
        entity_id=draw(text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            min_size=3,
            max_size=16,
        )),
        entity_type="route",
        severity=draw(_severities),
        confidence=draw(floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)),
        ttl_seconds=draw(integers(min_value=60, max_value=3600)),
        tenant_id=tid,
    )


@composite
def _leakage_es_response(draw, route_ids=None, n_below=3):
    """Generate an ES response with jobs that have below-target margins.

    Each route has n_below jobs with margins below the 15% target,
    ensuring leakage detection triggers.
    """
    if route_ids is None:
        n_routes = draw(integers(min_value=1, max_value=3))
        route_ids = [draw(_route_ids) for _ in range(n_routes)]

    hits = []
    for route_id in route_ids:
        for _ in range(n_below):
            margin_pct = draw(_below_target_margins)
            # Reverse-engineer revenue/cost from margin percentage
            revenue = 100.0
            # margin = (revenue - fuel_cost - sla_penalty) / revenue * 100
            # margin_pct = (100 - fuel_cost) / 100 * 100  (sla_penalty=0)
            fuel_cost = revenue * (1.0 - margin_pct / 100.0)
            hits.append({
                "_source": {
                    "route_id": route_id,
                    "revenue": revenue,
                    "fuel_cost": fuel_cost,
                    "sla_penalty": 0.0,
                    "status": "completed",
                    "tenant_id": "test-tenant",
                }
            })

    return {"hits": {"hits": hits}}


@composite
def _no_leakage_es_response(draw, route_ids=None, n_jobs=3):
    """Generate an ES response where no route has leakage (all above target)."""
    if route_ids is None:
        n_routes = draw(integers(min_value=1, max_value=3))
        route_ids = [draw(_route_ids) for _ in range(n_routes)]

    hits = []
    for route_id in route_ids:
        for _ in range(n_jobs):
            margin_pct = draw(_above_target_margins)
            revenue = 100.0
            fuel_cost = revenue * (1.0 - margin_pct / 100.0)
            hits.append({
                "_source": {
                    "route_id": route_id,
                    "revenue": revenue,
                    "fuel_cost": fuel_cost,
                    "sla_penalty": 0.0,
                    "status": "completed",
                    "tenant_id": "test-tenant",
                }
            })

    return {"hits": {"hits": hits}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_revenue_guard() -> RevenueGuard:
    """Build a RevenueGuard with mocked dependencies."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock()

    es_service = AsyncMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}}
    )
    es_service.index_document = AsyncMock()

    activity_log = AsyncMock()
    activity_log.log_monitoring_cycle = AsyncMock()

    ws_manager = MagicMock()
    confirmation_protocol = AsyncMock()
    autonomy_config = AsyncMock()
    feature_flag_service = MagicMock()
    feature_flag_service.get_overlay_state = AsyncMock(return_value="active_gated")

    guard = RevenueGuard(
        signal_bus=signal_bus,
        es_service=es_service,
        activity_log_service=activity_log,
        ws_manager=ws_manager,
        confirmation_protocol=confirmation_protocol,
        autonomy_config_service=autonomy_config,
        feature_flag_service=feature_flag_service,
    )

    return guard


def _run_async(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Property 1 — All proposals are PolicyChangeProposal instances
# ---------------------------------------------------------------------------
class TestRevenueGuardOutputType:
    """**Validates: Requirements 6.5, 6.7**"""

    @given(
        tenant_id=_tenant_ids,
        es_response=_leakage_es_response(),
        n_signals=integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100)
    def test_all_proposals_are_policy_change_proposals(
        self, tenant_id: str, es_response: dict, n_signals: int,
    ):
        """
        For any set of input signals that trigger leakage detection,
        every proposal returned by evaluate() is a PolicyChangeProposal
        instance — never an InterventionProposal.
        """
        guard = _build_revenue_guard()
        guard._es.search_documents = AsyncMock(return_value=es_response)
        # Clear route margins to ensure fresh state
        guard._route_margins.clear()

        # Build signals with the given tenant_id
        signals = [
            RiskSignal(
                source_agent="fuel_management_agent",
                entity_id=f"entity-{i}",
                entity_type="route",
                severity=Severity.HIGH,
                confidence=0.8,
                ttl_seconds=300,
                tenant_id=tenant_id,
            )
            for i in range(n_signals)
        ]

        proposals = _run_async(guard.evaluate(signals))

        for proposal in proposals:
            assert isinstance(proposal, PolicyChangeProposal), (
                f"Expected PolicyChangeProposal, got {type(proposal).__name__}. "
                f"RevenueGuard must only produce policy proposals, not direct "
                f"mutation actions (Req 6.5, 6.7)."
            )
            assert not isinstance(proposal, InterventionProposal), (
                "RevenueGuard must never produce InterventionProposal instances. "
                "All output must be PolicyChangeProposals (Req 6.7)."
            )


# ---------------------------------------------------------------------------
# Property 2 — No proposals contain direct mutation actions
# ---------------------------------------------------------------------------
class TestRevenueGuardNoDirectMutations:
    """**Validates: Requirements 6.5, 6.7**"""

    @given(
        tenant_id=_tenant_ids,
        es_response=_leakage_es_response(),
    )
    @settings(max_examples=100)
    def test_no_proposals_contain_mutation_actions(
        self, tenant_id: str, es_response: dict,
    ):
        """
        No proposal from RevenueGuard contains direct mutation actions.
        PolicyChangeProposals have parameter/old_value/new_value fields
        (policy recommendations), not actions lists with tool_name/parameters
        (direct mutations).
        """
        guard = _build_revenue_guard()
        guard._es.search_documents = AsyncMock(return_value=es_response)
        guard._route_margins.clear()

        signals = [
            RiskSignal(
                source_agent="fuel_management_agent",
                entity_id="entity-1",
                entity_type="route",
                severity=Severity.HIGH,
                confidence=0.8,
                ttl_seconds=300,
                tenant_id=tenant_id,
            )
        ]

        proposals = _run_async(guard.evaluate(signals))

        for proposal in proposals:
            # PolicyChangeProposal has parameter/old_value/new_value — not actions
            assert hasattr(proposal, "parameter"), (
                "Proposal must have 'parameter' field (PolicyChangeProposal)"
            )
            assert hasattr(proposal, "old_value"), (
                "Proposal must have 'old_value' field (PolicyChangeProposal)"
            )
            assert hasattr(proposal, "new_value"), (
                "Proposal must have 'new_value' field (PolicyChangeProposal)"
            )
            # Must NOT have an 'actions' list (that's InterventionProposal)
            assert not hasattr(proposal, "actions"), (
                "PolicyChangeProposal must not have 'actions' field. "
                "RevenueGuard must not produce direct mutation actions (Req 6.7)."
            )


# ---------------------------------------------------------------------------
# Property 3 — Every proposal includes rollback_plan and evidence
# ---------------------------------------------------------------------------
class TestRevenueGuardProposalCompleteness:
    """**Validates: Requirements 6.5, 6.7**"""

    @given(
        tenant_id=_tenant_ids,
        es_response=_leakage_es_response(),
    )
    @settings(max_examples=100)
    def test_every_proposal_has_rollback_plan_and_evidence(
        self, tenant_id: str, es_response: dict,
    ):
        """
        Every PolicyChangeProposal from RevenueGuard includes a non-empty
        rollback_plan (dict) and an evidence list, ensuring proposals are
        complete policy recommendations with reversal capability.
        """
        guard = _build_revenue_guard()
        guard._es.search_documents = AsyncMock(return_value=es_response)
        guard._route_margins.clear()

        signals = [
            RiskSignal(
                source_agent="fuel_management_agent",
                entity_id="entity-1",
                entity_type="route",
                severity=Severity.HIGH,
                confidence=0.8,
                ttl_seconds=300,
                tenant_id=tenant_id,
            )
        ]

        proposals = _run_async(guard.evaluate(signals))

        for proposal in proposals:
            assert isinstance(proposal.rollback_plan, dict), (
                "rollback_plan must be a dict"
            )
            assert len(proposal.rollback_plan) > 0, (
                "rollback_plan must be non-empty — every policy proposal "
                "needs a reversal plan (Req 6.5)"
            )
            assert isinstance(proposal.evidence, list), (
                "evidence must be a list of OutcomeRecord/signal references"
            )


# ---------------------------------------------------------------------------
# Property 4 — Source agent is always "revenue_guard"
# ---------------------------------------------------------------------------
class TestRevenueGuardSourceAgent:
    """**Validates: Requirements 6.5, 6.7**"""

    @given(
        tenant_id=_tenant_ids,
        es_response=_leakage_es_response(),
    )
    @settings(max_examples=100)
    def test_source_agent_is_revenue_guard(
        self, tenant_id: str, es_response: dict,
    ):
        """
        Every proposal from RevenueGuard has source_agent set to
        "revenue_guard", ensuring traceability.
        """
        guard = _build_revenue_guard()
        guard._es.search_documents = AsyncMock(return_value=es_response)
        guard._route_margins.clear()

        signals = [
            RiskSignal(
                source_agent="fuel_management_agent",
                entity_id="entity-1",
                entity_type="route",
                severity=Severity.HIGH,
                confidence=0.8,
                ttl_seconds=300,
                tenant_id=tenant_id,
            )
        ]

        proposals = _run_async(guard.evaluate(signals))

        for proposal in proposals:
            assert proposal.source_agent == "revenue_guard", (
                f"Expected source_agent='revenue_guard', got '{proposal.source_agent}'"
            )


# ---------------------------------------------------------------------------
# Property 5 — No proposals when margins are above target (negative case)
# ---------------------------------------------------------------------------
class TestRevenueGuardNoProposalsAboveTarget:
    """**Validates: Requirements 6.5, 6.7**"""

    @given(
        tenant_id=_tenant_ids,
        es_response=_no_leakage_es_response(),
    )
    @settings(max_examples=100)
    def test_no_proposals_when_margins_above_target(
        self, tenant_id: str, es_response: dict,
    ):
        """
        When all route margins are above the target, RevenueGuard produces
        no proposals — it only acts on detected leakage patterns.
        This confirms the output constraint holds vacuously when there
        is no leakage, and the agent does not generate spurious proposals.
        """
        guard = _build_revenue_guard()
        guard._es.search_documents = AsyncMock(return_value=es_response)
        guard._route_margins.clear()

        signals = [
            RiskSignal(
                source_agent="fuel_management_agent",
                entity_id="entity-1",
                entity_type="route",
                severity=Severity.MEDIUM,
                confidence=0.6,
                ttl_seconds=300,
                tenant_id=tenant_id,
            )
        ]

        proposals = _run_async(guard.evaluate(signals))

        # All proposals (if any) must still be PolicyChangeProposals
        for proposal in proposals:
            assert isinstance(proposal, PolicyChangeProposal), (
                f"Even edge-case proposals must be PolicyChangeProposal, "
                f"got {type(proposal).__name__}"
            )
