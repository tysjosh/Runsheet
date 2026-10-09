"""``OverlayAgentBase._route_proposal`` must not queue unreadable approvals (F11).

The prioritization agent's proposal used ``tool``/``params`` keys while the
router read ``tool_name``/``parameters``, so every pipeline run queued an
``overlay_action`` approval with ``{}`` — something a dispatcher could neither
understand nor execute. Two rules now hold for every overlay agent:

* a non-mutating action (``NON_MUTATING_OVERLAY_TOOLS``) is published on the
  Signal Bus and never sent to the ConfirmationProtocol;
* an action without a ``tool_name`` or with empty ``parameters`` is never
  submitted, and the drop is logged at ERROR naming the agent and the keys.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.base_overlay_agent import NON_MUTATING_OVERLAY_TOOLS
from Agents.overlay.data_contracts import InterventionProposal, RiskClass
from Agents.overlay.delivery_prioritization_agent import DeliveryPrioritizationAgent
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)


def _agent():
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)
    confirmation = MagicMock()
    confirmation.process_mutation = AsyncMock()
    agent = DeliveryPrioritizationAgent(
        signal_bus=signal_bus,
        es_service=MagicMock(),
        activity_log_service=MagicMock(),
        ws_manager=MagicMock(),
        confirmation_protocol=confirmation,
        autonomy_config_service=MagicMock(),
        feature_flag_service=MagicMock(),
    )
    return agent, signal_bus, confirmation


def _proposal(*actions):
    return InterventionProposal(
        source_agent="delivery_prioritization",
        tenant_id="tenant-1",
        risk_class=RiskClass.MEDIUM,
        expected_kpi_delta={},
        confidence=0.9,
        priority=1,
        actions=list(actions),
    )


def test_publish_priority_list_is_registered_as_non_mutating():
    assert "publish_priority_list" in NON_MUTATING_OVERLAY_TOOLS


async def test_the_prioritization_proposal_is_published_not_queued():
    agent, signal_bus, confirmation = _agent()
    priority_list = DeliveryPriorityList(
        priorities=[
            DeliveryPriority(
                station_id="tank-1",
                fuel_grade=FuelGrade.AGO,
                priority_score=0.9,
                priority_bucket=PriorityBucket.CRITICAL,
            )
        ],
        tenant_id="tenant-1",
        run_id="run_X",
    )
    proposal = agent._build_proposal(priority_list, "tenant-1")

    await agent._route_proposal(proposal, "active_auto")

    confirmation.process_mutation.assert_not_awaited()
    signal_bus.publish.assert_awaited_once_with(proposal)


@pytest.mark.parametrize(
    "action",
    [
        {"tool": "x", "params": {"a": 1}},
        {"tool_name": "y", "parameters": {}},
        {"tool_name": "y"},
        {"parameters": {"a": 1}},
    ],
)
async def test_an_action_without_tool_name_or_parameters_is_never_submitted(
    action, caplog
):
    agent, signal_bus, confirmation = _agent()
    proposal = _proposal(action)

    with caplog.at_level(logging.ERROR):
        await agent._route_proposal(proposal, "active_gated")

    confirmation.process_mutation.assert_not_awaited()
    signal_bus.publish.assert_awaited_once_with(proposal)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "a dropped action must be logged at ERROR"
    message = errors[0].getMessage()
    assert "delivery_prioritization" in message
    for key in action:
        assert key in message


async def test_a_well_formed_action_is_still_submitted():
    agent, _signal_bus, confirmation = _agent()
    proposal = _proposal(
        {"tool_name": "y", "parameters": {}},
        {"tool_name": "apply_loading_plan", "parameters": {"plan_id": "p1"}},
    )

    await agent._route_proposal(proposal, "active_gated")

    confirmation.process_mutation.assert_awaited_once()
    request = confirmation.process_mutation.await_args.args[0]
    assert request.tool_name == "apply_loading_plan"
    assert request.parameters == {"plan_id": "p1"}
    assert request.tenant_id == "tenant-1"
    assert request.agent_id == "delivery_prioritization"
