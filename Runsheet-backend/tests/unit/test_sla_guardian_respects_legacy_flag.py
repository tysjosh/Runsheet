"""SLAGuardianAgent must not run while the legacy NG surface is disabled.

The agent monitors ``shipments_current`` — the pre-pivot Nigerian last-mile read
model. ``require_ops_enabled`` gates the entire ``/api/ops/*`` HTTP surface behind
``legacy_ng_delivery`` (default OFF), but this agent runs on the ``AgentScheduler``
with ``RestartPolicy.ALWAYS`` and never consulted that flag.

Observed on a running instance before the fix:

    shipments_current      0 docs
    agent_approval_queue  24 docs   tool_name: escalate_shipment
                                    shipment_id: SHP-0043, SHP-0029, ...
                                    status: expired

So it was filing ``escalate_shipment`` proposals for shipments that do not exist,
which then expired unactioned. The approval queue is the dispatcher's
human-in-the-loop surface for the whole agent overlay (``ApprovalQueuePanel`` in
``OperationsControlView``, and the pending count on ``DispatchCockpit``). Filling
it with un-actionable proposals trains dispatchers to ignore it, which defeats the
control rather than exercising it.

Note what this is *not*: the fix is not deletion. The flag works — the HTTP
surface 404s correctly and the frontend that called it is gone. This agent was the
one component that escaped the flag, so it is the one component that needed the
check.
"""

from __future__ import annotations

import pytest

from config.legacy_flags import LEGACY_NG_DELIVERY_ENV_VAR

from tests.unit.test_sla_guardian_agent import _breached_shipment, _make_agent


@pytest.fixture
def legacy_disabled(monkeypatch):
    monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "false")


@pytest.fixture
def legacy_enabled(monkeypatch):
    monkeypatch.setenv(LEGACY_NG_DELIVERY_ENV_VAR, "true")


class TestCycleIsSkippedWhenDisabled:
    @pytest.mark.asyncio
    async def test_returns_no_detections_or_actions(self, legacy_disabled):
        agent = _make_agent()
        detections, actions = await agent.monitor_cycle()
        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_elasticsearch_is_never_queried(self, legacy_disabled):
        """The sweep is cross-tenant and unbounded; it must not run at all."""
        agent = _make_agent()
        await agent.monitor_cycle()
        agent._es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_escalation_is_proposed(self, legacy_disabled):
        """The specific defect: nothing reaches the dispatcher's approval queue."""
        agent = _make_agent()
        agent._es.search_documents.return_value = {
            "hits": {"hits": [{"_source": _breached_shipment()}]}
        }
        await agent.monitor_cycle()
        agent._confirmation_protocol.request_mutation.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipping_does_not_raise(self, legacy_disabled):
        """It is registered with RestartPolicy.ALWAYS.

        Raising to opt out would read to the scheduler as a crash and restart in
        a loop, which is noisier than the behaviour being removed.
        """
        agent = _make_agent()
        await agent.monitor_cycle()  # must not raise


class TestCycleStillRunsWhenEnabled:
    @pytest.mark.asyncio
    async def test_the_guard_is_conditional_not_a_disable(self, legacy_enabled):
        """Guards the guard.

        If the check were unconditional this file would still pass while the
        agent had been silently turned off for everyone — including a tenant that
        deliberately re-enabled the legacy surface.
        """
        agent = _make_agent()
        # One shape serving both reads the cycle makes: the shipment sweep
        # (``hits.hits``) and the per-rider active count (``hits.total.value``).
        agent._es.search_documents.return_value = {
            "hits": {
                "hits": [{"_source": _breached_shipment()}],
                "total": {"value": 1},
            }
        }
        await agent.monitor_cycle()
        assert agent._es.search_documents.called, (
            "with legacy_ng_delivery=true the agent must still sweep; the flag "
            "check has become an unconditional disable"
        )
