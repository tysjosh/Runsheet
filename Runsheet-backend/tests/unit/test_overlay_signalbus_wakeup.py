"""
The SignalBus path into each overlay agent, which previously had no coverage.

``OverlayAgentBase.monitor_cycle`` decides whether there is work to do by
looking at ``_signal_buffer`` alone. Two agents override ``_on_signal`` to file
the messages they care about in a *typed* buffer instead
(``CompartmentLoadingAgent._priority_buffer``,
``RoutePlanningAgent._proposal_buffer``), and a third
(``DeliveryPrioritizationAgent``) subscribed to nothing at all. All three
therefore left ``_signal_buffer`` permanently empty, ``monitor_cycle`` returned
``([], [])`` before reaching ``evaluate()``, and their background decision loops
ran on schedule and did nothing — no error, no log line, buffers growing.

Only the pipeline coordinator hid this, by seeding ``_signal_buffer`` directly.
These tests drive the agents the way the SignalBus does — through
``_on_signal`` — with no pipeline involved.

Validates: Requirements 3.1, 4.1, 5.1.1
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.compartment_loading_agent import CompartmentLoadingAgent
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.delivery_prioritization_agent import (
    DeliveryPrioritizationAgent,
)
from Agents.overlay.route_planning_agent import RoutePlanningAgent
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
    TankForecast,
)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _make_deps() -> Dict[str, Any]:
    """The seven collaborators every overlay agent constructor requires."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}, "aggregations": {}}
    )
    es_service.index_document = AsyncMock()
    es_service.update_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()
    ws_manager.broadcast_event = AsyncMock(return_value=0)

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    feature_flags = MagicMock()
    # "shadow" keeps proposals off the ConfirmationProtocol while still
    # exercising the full cycle — "disabled" would skip evaluate() for an
    # unrelated reason and mask what these tests are checking.
    feature_flags.get_overlay_state = AsyncMock(return_value="shadow")
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": feature_flags,
    }


def _priority_list(tenant_id: str = TENANT_A) -> DeliveryPriorityList:
    return DeliveryPriorityList(
        priorities=[
            DeliveryPriority(
                station_id="tank-1",
                fuel_grade=FuelGrade.AGO,
                priority_score=0.91,
                priority_bucket=PriorityBucket.CRITICAL,
                reasons=["runout_critical"],
            )
        ],
        tenant_id=tenant_id,
    )


def _loading_proposal(tenant_id: str = TENANT_A) -> InterventionProposal:
    return InterventionProposal(
        source_agent="compartment_loading",
        tenant_id=tenant_id,
        risk_class=RiskClass.MEDIUM,
        expected_kpi_delta={"load_plans": 1.0},
        confidence=0.9,
        priority=1,
        actions=[{"tool": "commit_loading_plan", "params": {}}],
    )


def _spy_evaluate(agent) -> AsyncMock:
    """Record ``evaluate`` calls without running the real decision logic."""
    spy = AsyncMock(return_value=[])
    agent.evaluate = spy
    return spy


# ---------------------------------------------------------------------------
# The base-class contract
# ---------------------------------------------------------------------------


class TestPendingWorkTenantsContract:
    """``monitor_cycle`` must consider both doors into an agent."""

    class _TypedBufferAgent(OverlayAgentBase):
        """Files everything in its own buffer, exactly like the real two."""

        def __init__(self, **deps):
            super().__init__(
                agent_id="typed_buffer_agent",
                subscriptions=[],
                poll_interval=60,
                cooldown_minutes=1,
                **deps,
            )
            self.own_buffer: List[Any] = []
            self.evaluated_with: List[List[Any]] = []

        async def _on_signal(self, signal) -> None:
            self.own_buffer.append(signal)

        def _pending_work_tenants(self) -> List[str]:
            return [
                s.tenant_id for s in self.own_buffer if getattr(s, "tenant_id", None)
            ]

        async def evaluate(self, signals):
            self.evaluated_with.append(list(signals))
            self.own_buffer.clear()
            return []

    def _signal(self, tenant_id: str = TENANT_A) -> RiskSignal:
        return RiskSignal(
            source_agent="test",
            entity_id="e-1",
            entity_type="test",
            severity=Severity.LOW,
            confidence=1.0,
            ttl_seconds=60,
            tenant_id=tenant_id,
        )

    @pytest.mark.asyncio
    async def test_typed_buffer_work_triggers_a_cycle(self):
        """An empty ``_signal_buffer`` no longer means "nothing to do"."""
        agent = self._TypedBufferAgent(**_make_deps())

        await agent._on_signal(self._signal())
        assert agent._signal_buffer == [], "precondition: nothing in _signal_buffer"

        await agent.monitor_cycle()

        assert agent.evaluated_with == [[]], (
            "evaluate() was not reached — monitor_cycle still gates the cycle "
            "on _signal_buffer alone"
        )

    @pytest.mark.asyncio
    async def test_an_idle_agent_still_short_circuits(self):
        """No signals and no pending work means no evaluate, as before."""
        agent = self._TypedBufferAgent(**_make_deps())

        signals, proposals = await agent.monitor_cycle()

        assert (signals, proposals) == ([], [])
        assert agent.evaluated_with == []

    @pytest.mark.asyncio
    async def test_pending_tenants_merge_with_signal_tenants(self):
        """Both doors contribute, and a tenant in both is evaluated once."""
        agent = self._TypedBufferAgent(**_make_deps())
        # Door 1: straight into _signal_buffer.
        async with agent._buffer_lock:
            agent._signal_buffer.append(self._signal(TENANT_A))
        # Door 2: the typed buffer, one overlapping tenant and one new.
        agent.own_buffer.extend(
            [self._signal(TENANT_A), self._signal(TENANT_B)]
        )

        await agent.monitor_cycle()

        assert len(agent.evaluated_with) == 2, (
            "expected one evaluate() per distinct tenant across both buffers, "
            f"got {len(agent.evaluated_with)}"
        )
        # The tenant that arrived through _signal_buffer carries its signal;
        # the typed-buffer-only tenant is evaluated with an empty list.
        assert sorted(len(batch) for batch in agent.evaluated_with) == [0, 1]

    @pytest.mark.asyncio
    async def test_default_hook_is_empty_so_existing_agents_are_unaffected(self):
        """Agents whose messages land in ``_signal_buffer`` need no override."""
        assert OverlayAgentBase._pending_work_tenants(MagicMock()) == []


# ---------------------------------------------------------------------------
# CompartmentLoadingAgent
# ---------------------------------------------------------------------------


class TestCompartmentLoadingWakesOnPriorityList:
    @pytest.mark.asyncio
    async def test_a_priority_list_arriving_by_signalbus_triggers_evaluate(self):
        """Validates: Requirement 3.1"""
        agent = CompartmentLoadingAgent(**_make_deps())
        spy = _spy_evaluate(agent)

        await agent._on_signal(_priority_list())
        assert agent._priority_buffer, "precondition: buffered in the typed buffer"
        assert agent._signal_buffer == [], "precondition: not in _signal_buffer"

        await agent.monitor_cycle()

        assert spy.await_count == 1, (
            "CompartmentLoadingAgent.evaluate() was not reached — a priority "
            "list delivered over the SignalBus is buffered and forgotten"
        )

    @pytest.mark.asyncio
    async def test_the_buffered_list_is_still_there_when_evaluate_runs(self):
        """Waking the cycle must not consume the payload on the way in."""
        agent = CompartmentLoadingAgent(**_make_deps())
        published = _priority_list()
        seen: List[Any] = []

        async def _capture(signals):
            seen.extend(agent._priority_buffer)
            return []

        agent.evaluate = AsyncMock(side_effect=_capture)

        await agent._on_signal(published)
        await agent.monitor_cycle()

        assert seen == [published]

    @pytest.mark.asyncio
    async def test_reported_tenants_come_from_the_buffered_lists(self):
        agent = CompartmentLoadingAgent(**_make_deps())

        await agent._on_signal(_priority_list(TENANT_A))
        await agent._on_signal(_priority_list(TENANT_B))
        await agent._on_signal(_priority_list(TENANT_A))

        assert agent._pending_work_tenants() == [TENANT_A, TENANT_B]

    @pytest.mark.asyncio
    async def test_dropping_another_tenants_list_is_logged(self, caplog):
        """``evaluate`` acts on ``[-1]``; the rest vanish, so say so."""
        agent = CompartmentLoadingAgent(**_make_deps())
        await agent._on_signal(_priority_list(TENANT_A))
        await agent._on_signal(_priority_list(TENANT_B))

        with caplog.at_level("WARNING"):
            await agent.evaluate([])

        assert any(
            TENANT_A in record.message and "discarding" in record.message.lower()
            for record in caplog.records
        ), "the discarded tenant's priority list disappeared without a warning"


# ---------------------------------------------------------------------------
# RoutePlanningAgent
# ---------------------------------------------------------------------------


class TestRoutePlanningWakesOnLoadingProposal:
    @pytest.mark.asyncio
    async def test_a_loading_proposal_arriving_by_signalbus_triggers_evaluate(self):
        """Validates: Requirement 4.1"""
        agent = RoutePlanningAgent(**_make_deps())
        spy = _spy_evaluate(agent)

        await agent._on_signal(_loading_proposal())
        assert agent._proposal_buffer, "precondition: buffered in the typed buffer"
        assert agent._signal_buffer == [], "precondition: not in _signal_buffer"

        await agent.monitor_cycle()

        assert spy.await_count == 1, (
            "RoutePlanningAgent.evaluate() was not reached — a loading "
            "proposal delivered over the SignalBus is buffered and forgotten"
        )

    @pytest.mark.asyncio
    async def test_the_buffered_proposal_is_still_there_when_evaluate_runs(self):
        agent = RoutePlanningAgent(**_make_deps())
        published = _loading_proposal()
        seen: List[Any] = []

        async def _capture(signals):
            seen.extend(agent._proposal_buffer)
            return []

        agent.evaluate = AsyncMock(side_effect=_capture)

        await agent._on_signal(published)
        await agent.monitor_cycle()

        assert seen == [published]

    @pytest.mark.asyncio
    async def test_a_proposal_from_another_agent_goes_to_the_signal_buffer(self):
        """The ``source_agent`` filter is unchanged by the wake-up fix."""
        agent = RoutePlanningAgent(**_make_deps())
        foreign = _loading_proposal()
        foreign.source_agent = "some_other_agent"

        await agent._on_signal(foreign)

        assert agent._proposal_buffer == []
        assert agent._signal_buffer == [foreign]


# ---------------------------------------------------------------------------
# DeliveryPrioritizationAgent
# ---------------------------------------------------------------------------


class TestPrioritizationSubscribesToForecasts:
    """It had ``subscriptions=[]``, so its background loop could never fire."""

    @pytest.mark.asyncio
    async def test_it_subscribes_to_tank_forecasts(self):
        """Validates: Requirement 5.1.1"""
        agent = DeliveryPrioritizationAgent(**_make_deps())

        subscribed_types = [
            spec["message_type"] for spec in agent._subscription_specs
        ]

        assert TankForecast in subscribed_types, (
            "DeliveryPrioritizationAgent subscribes to nothing, so nothing "
            "ever reaches _signal_buffer and its decision loop is a no-op"
        )

    @pytest.mark.asyncio
    async def test_start_registers_the_subscription_on_the_bus(self):
        """``start()`` must actually hand the callback to the bus."""
        from unittest.mock import patch

        deps = _make_deps()
        agent = DeliveryPrioritizationAgent(**deps)

        # Stub the polling loop the base class would launch; only the
        # subscription registration is under test here.
        with patch(
            "Agents.autonomous.base_agent.AutonomousAgentBase.start",
            new=AsyncMock(),
        ):
            await agent.start()

        registered = [
            call.kwargs["message_type"]
            for call in deps["signal_bus"].subscribe.await_args_list
        ]
        assert TankForecast in registered

    @pytest.mark.asyncio
    async def test_a_forecast_wakes_the_cycle(self):
        agent = DeliveryPrioritizationAgent(**_make_deps())
        spy = _spy_evaluate(agent)

        await agent._on_signal(
            TankForecast(
                station_id="tank-1",
                fuel_grade=FuelGrade.AGO,
                hours_to_runout_p50=8.0,
                hours_to_runout_p90=6.0,
                runout_risk_24h=0.9,
                confidence=0.8,
                tenant_id=TENANT_A,
            )
        )
        await agent.monitor_cycle()

        assert spy.await_count == 1
        # The forecast reaches evaluate as a plain signal — this agent reads
        # forecasts back from mvp_tank_forecasts, so it owns no typed buffer.
        (signals,) = spy.await_args[0]
        assert [s.tenant_id for s in signals] == [TENANT_A]


class TestPrioritizationScopesToTheWokenTenant:
    """A run for one tenant must not publish another tenant's priority list."""

    @staticmethod
    def _es_with_orders_for_both_tenants() -> MagicMock:
        from datetime import datetime, timedelta, timezone

        window_end = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()

        def _order(tenant_id: str, suffix: str) -> Dict[str, Any]:
            return {
                "order_id": f"order-{suffix}",
                "tenant_id": tenant_id,
                "status": "confirmed",
                "call_type": "will_call",
                "customer_tank_id": f"tank-{suffix}",
                "product_code": "AGO",
                "delivery_window_end": window_end,
            }

        async def _search(index, query, size=10, *args, **kwargs):
            if index != "fuel_orders_current":
                return {"hits": {"hits": []}, "aggregations": {}}
            if query.get("size") == 0 or "aggs" in query:
                return {
                    "hits": {"hits": []},
                    "aggregations": {
                        "tenants": {
                            "buckets": [
                                {"key": TENANT_A, "doc_count": 1},
                                {"key": TENANT_B, "doc_count": 1},
                            ]
                        }
                    },
                }
            # Per-tenant fetch: echo back whichever tenant was asked for.
            filters = query["query"]["bool"]["filter"]
            requested = next(
                f["term"]["tenant_id"] for f in filters if "term" in f
            )
            return {
                "hits": {
                    "hits": [{"_source": _order(requested, requested[-1])}]
                }
            }

        es = MagicMock()
        es.search_documents = AsyncMock(side_effect=_search)
        es.index_document = AsyncMock()
        es.update_document = AsyncMock()
        return es

    @pytest.mark.asyncio
    async def test_signals_scope_the_run_to_one_tenant(self):
        deps = _make_deps()
        deps["es_service"] = self._es_with_orders_for_both_tenants()
        agent = DeliveryPrioritizationAgent(**deps)

        await agent.evaluate(
            [
                RiskSignal(
                    source_agent="pipeline_injection",
                    entity_id="trigger",
                    entity_type="pipeline_trigger",
                    severity=Severity.LOW,
                    confidence=1.0,
                    ttl_seconds=60,
                    tenant_id=TENANT_A,
                )
            ]
        )

        published = [
            call.args[0] for call in deps["signal_bus"].publish.await_args_list
        ]
        assert [p.tenant_id for p in published] == [TENANT_A], (
            "a cycle woken for one tenant published priority lists for others "
            "— the loading stage acts on the last list it receives, so this "
            "leaks work across tenants"
        )

    @pytest.mark.asyncio
    async def test_no_signals_still_sweeps_every_tenant(self):
        """The periodic sweep and direct callers keep cross-tenant discovery."""
        deps = _make_deps()
        deps["es_service"] = self._es_with_orders_for_both_tenants()
        agent = DeliveryPrioritizationAgent(**deps)

        await agent.evaluate([])

        published = [
            call.args[0] for call in deps["signal_bus"].publish.await_args_list
        ]
        assert sorted(p.tenant_id for p in published) == [TENANT_A, TENANT_B]
