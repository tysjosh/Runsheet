"""
Pipeline wiring tests that use **real** overlay agents.

``tests/unit/test_fuel_distribution_pipeline.py`` builds its stages from
``MagicMock()``, where ``hasattr`` answers ``True`` for every name. That is why a
stale ``_STAGE_BUFFER_MAP`` entry survived: the map pointed
``tank_forecasting`` at ``_forecast_buffer``, an attribute a refactor had removed
from ``DeliveryPrioritizationAgent``, so ``_capture_and_inject``'s ``hasattr``
guard skipped the injection and returned *before* seeding the cycle trigger.
``DeliveryPrioritizationAgent.evaluate()`` was therefore never awaited on any
production path, ``mvp_delivery_priorities`` was never written, and the loading
and routing stages ran with empty buffers — while the run still reported
``state: complete`` with every stage ``"completed"``.

Every test here instantiates the genuine agent classes so ``hasattr`` means what
it says. The assertions are on the transport mechanism rather than on scoring
output: that each stage is actually evaluated, that the payload reaches the
next stage's typed buffer, that the trigger carries the run's tenant, and that a
wiring fault now fails the run loudly instead of reporting success.

Validates: Requirements 1.1, 1.2, 1.3, 1.5, 6.1, 6.4, 6.5
"""

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

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
from Agents.support.fuel_distribution_pipeline import (
    PIPELINE_STAGES,
    FuelDistributionPipeline,
    PipelineState,
)

TENANT_ID = "tenant-pipeline-wiring"


# ---------------------------------------------------------------------------
# Fixtures — real agents over faked collaborators
# ---------------------------------------------------------------------------


def _make_deps() -> Dict[str, Any]:
    """The seven collaborators every overlay agent constructor requires."""
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
    ws_manager.broadcast_event = AsyncMock(return_value=0)

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    feature_flags = MagicMock()
    # Every overlay flag defaults to "disabled"; the pipeline is expected to
    # bypass it through _pipeline_mode_override rather than by flipping flags.
    feature_flags.get_overlay_state = AsyncMock(return_value="disabled")
    feature_flags.is_enabled = AsyncMock(return_value=False)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": MagicMock(),
        "feature_flag_service": feature_flags,
    }


class _PublishingFirstStage:
    """Stand-in for ``TankForecastingAgent`` that publishes a real forecast.

    The forecasting agent itself is not under test here; what matters is that
    the first stage publishes something and that its successor is a real agent,
    because the buffer map is checked against the *next* agent.
    """

    agent_id = "tank_forecasting"

    def __init__(self, signal_bus, *, publish: bool = True) -> None:
        self._signal_bus = signal_bus
        self._publish = publish
        self._signal_buffer: List[Any] = []
        self._pipeline_mode_override: Optional[str] = None
        self._current_run_id: Optional[str] = None
        self.cycles = 0

    async def monitor_cycle(self):
        self.cycles += 1
        self._signal_buffer.clear()
        if self._publish:
            await self._signal_bus.publish(
                TankForecast(
                    station_id="station-1",
                    fuel_grade=FuelGrade.AGO,
                    hours_to_runout_p50=8.0,
                    hours_to_runout_p90=6.0,
                    runout_risk_24h=0.9,
                    confidence=0.8,
                    tenant_id=TENANT_ID,
                )
            )
        return [], []


def _priority_list() -> DeliveryPriorityList:
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
        tenant_id=TENANT_ID,
    )


def _build_pipeline(*, first_stage_publishes: bool = True):
    """A pipeline whose three downstream stages are real agent instances."""
    deps = _make_deps()
    bus = deps["signal_bus"]

    agents = {
        "tank_forecasting": _PublishingFirstStage(
            bus, publish=first_stage_publishes
        ),
        "delivery_prioritization": DeliveryPrioritizationAgent(**deps),
        "compartment_loading": CompartmentLoadingAgent(**deps),
        "route_planning": RoutePlanningAgent(**deps),
    }

    pipeline = FuelDistributionPipeline(
        agents=agents,
        ws_manager=deps["ws_manager"],
        signal_bus=bus,
    )
    return pipeline, agents, deps


def _spy_evaluate(agent, *, publishes: Optional[Any] = None):
    """Replace ``evaluate`` with a recorder that optionally publishes.

    Returns the ``AsyncMock`` standing in for ``evaluate`` so a test can assert
    it was awaited. The real scoring logic has its own unit tests; what has
    never been covered is whether the pipeline reaches it at all.
    """

    async def _evaluate(signals):
        if publishes is not None:
            await agent._signal_bus.publish(publishes)
        return []

    spy = AsyncMock(side_effect=_evaluate)
    agent.evaluate = spy
    return spy


# ---------------------------------------------------------------------------
# The regression: the prioritization stage is actually evaluated
# ---------------------------------------------------------------------------


class TestEveryStageIsEvaluated:
    """The defect was ``evaluate`` awaited 0 times on three of four stages."""

    @pytest.mark.asyncio
    async def test_prioritization_evaluate_is_awaited(self):
        """Validates: Requirements 1.1, 1.2, 1.5"""
        pipeline, agents, _ = _build_pipeline()
        spy = _spy_evaluate(agents["delivery_prioritization"])

        run_id = await pipeline.run(tenant_id=TENANT_ID)

        assert spy.await_count == 1, (
            "DeliveryPrioritizationAgent.evaluate() was not awaited — the "
            "forecast → prioritization hop is broken again"
        )
        status = await pipeline.get_status(run_id)
        assert status["state"] == PipelineState.COMPLETE.value

    @pytest.mark.asyncio
    async def test_every_downstream_stage_is_evaluated(self):
        """All three successors run, not just the first (Req 6.1)."""
        pipeline, agents, _ = _build_pipeline()
        spies = {
            agent_id: _spy_evaluate(agents[agent_id])
            for agent_id in (
                "delivery_prioritization",
                "compartment_loading",
                "route_planning",
            )
        }

        await pipeline.run(tenant_id=TENANT_ID)

        assert {name: spy.await_count for name, spy in spies.items()} == {
            "delivery_prioritization": 1,
            "compartment_loading": 1,
            "route_planning": 1,
        }

    @pytest.mark.asyncio
    async def test_prioritization_needs_no_typed_buffer(self):
        """The stage reads ``mvp_tank_forecasts`` from ES, so it owns no buffer.

        Pins the reason ``tank_forecasting`` is absent from the buffer map: if a
        future change reintroduces a typed buffer on this agent, the map should
        be revisited deliberately rather than by a silent ``hasattr`` miss.
        """
        _, agents, _ = _build_pipeline()

        assert not hasattr(agents["delivery_prioritization"], "_forecast_buffer")
        assert (
            "tank_forecasting"
            not in FuelDistributionPipeline._STAGE_BUFFER_MAP
        )

    @pytest.mark.asyncio
    async def test_mapped_buffers_exist_on_their_target_agents(self):
        """Every map entry names a real attribute on the *next* agent."""
        _, agents, _ = _build_pipeline()
        stage_order = [agent_id for agent_id, _ in PIPELINE_STAGES]

        for stage, buffer_name in (
            FuelDistributionPipeline._STAGE_BUFFER_MAP.items()
        ):
            next_agent = agents[stage_order[stage_order.index(stage) + 1]]
            assert hasattr(next_agent, buffer_name), (
                f"stage '{stage}' is mapped to '{buffer_name}', which "
                f"{type(next_agent).__name__} does not have"
            )

    @pytest.mark.asyncio
    async def test_pipeline_payload_types_match_agent_routing(self):
        """The pipeline's declared payload type is what ``_on_signal`` routes.

        Direct injection bypasses ``_on_signal``, so the pipeline has to
        reproduce that method's isinstance check. Two copies of one predicate is
        how the original defect happened, so this asserts they agree: hand the
        receiving agent an instance of the declared type through its *own*
        handler and require it to land in the *same* buffer the pipeline writes.

        Validates: Requirements 1.2, 1.3
        """
        _, agents, _ = _build_pipeline()
        stage_order = [agent_id for agent_id, _ in PIPELINE_STAGES]
        samples = {
            DeliveryPriorityList: _priority_list(),
            InterventionProposal: InterventionProposal(
                source_agent="compartment_loading",
                tenant_id=TENANT_ID,
                risk_class=RiskClass.MEDIUM,
                expected_kpi_delta={"load_plans": 1.0},
                confidence=0.9,
                priority=1,
                actions=[{"tool": "noop", "params": {}}],
            ),
        }

        for stage, buffer_name in (
            FuelDistributionPipeline._STAGE_BUFFER_MAP.items()
        ):
            expected_type = FuelDistributionPipeline._STAGE_PAYLOAD_TYPES[stage]
            next_agent = agents[stage_order[stage_order.index(stage) + 1]]
            buffer = getattr(next_agent, buffer_name)
            buffer.clear()

            await next_agent._on_signal(samples[expected_type])

            assert buffer == [samples[expected_type]], (
                f"the pipeline injects {expected_type.__name__} into "
                f"{type(next_agent).__name__}.{buffer_name}, but that agent's "
                "_on_signal routes it elsewhere — the two filters have drifted"
            )


# ---------------------------------------------------------------------------
# Payload transfer
# ---------------------------------------------------------------------------


class TestPayloadTransfer:
    @pytest.mark.asyncio
    async def test_priority_list_reaches_the_loading_buffer(self):
        """The published DeliveryPriorityList lands in ``_priority_buffer``.

        Validates: Requirements 1.2, 1.3
        """
        pipeline, agents, _ = _build_pipeline()
        published = _priority_list()
        _spy_evaluate(
            agents["delivery_prioritization"], publishes=published
        )
        # Capture the buffer contents at the moment loading is evaluated —
        # ``evaluate`` drains it, so a post-run read would always be empty.
        seen: List[Any] = []

        async def _capture(signals):
            seen.extend(agents["compartment_loading"]._priority_buffer)
            return []

        agents["compartment_loading"].evaluate = AsyncMock(side_effect=_capture)

        await pipeline.run(tenant_id=TENANT_ID)

        assert seen == [published]

    @pytest.mark.asyncio
    async def test_a_stage_that_publishes_nothing_still_triggers_the_next(self):
        """Previously an early return killed the rest of the run.

        A stage with no output is a data condition — prioritization can still
        score will-call orders from delivery windows with no forecast at all.

        Validates: Requirements 1.5, 6.1
        """
        pipeline, agents, _ = _build_pipeline(first_stage_publishes=False)
        spy = _spy_evaluate(agents["delivery_prioritization"])

        await pipeline.run(tenant_id=TENANT_ID)

        assert spy.await_count == 1


# ---------------------------------------------------------------------------
# The cycle trigger
# ---------------------------------------------------------------------------


class TestCycleTrigger:
    @pytest.mark.asyncio
    async def test_trigger_carries_the_runs_tenant(self):
        """Not ``"unknown"`` — ``_group_by_tenant`` would strand the stage.

        The old implementation inferred the tenant from message contents and
        fell back to the literal ``"unknown"``, which reaches ``evaluate()`` as a
        tenant with no orders.
        """
        pipeline, agents, _ = _build_pipeline(first_stage_publishes=False)
        tenants: List[str] = []

        async def _record(signals):
            tenants.extend(s.tenant_id for s in signals)
            return []

        agents["delivery_prioritization"].evaluate = AsyncMock(
            side_effect=_record
        )

        await pipeline.run(tenant_id=TENANT_ID)

        assert tenants == [TENANT_ID]

    @pytest.mark.asyncio
    async def test_trigger_is_a_risk_signal_from_the_pipeline(self):
        """The trigger is attributable, so a stage can tell why it woke."""
        pipeline, agents, _ = _build_pipeline(first_stage_publishes=False)
        received: List[Any] = []

        async def _record(signals):
            received.extend(signals)
            return []

        agents["delivery_prioritization"].evaluate = AsyncMock(
            side_effect=_record
        )

        await pipeline.run(tenant_id=TENANT_ID)

        (trigger,) = received
        assert isinstance(trigger, RiskSignal)
        assert trigger.source_agent == "pipeline_injection"
        assert trigger.entity_type == "pipeline_trigger"
        assert trigger.severity == Severity.LOW
        assert trigger.context["source_stage"] == "tank_forecasting"


# ---------------------------------------------------------------------------
# A wiring fault must fail the run, not report success
# ---------------------------------------------------------------------------


class TestWiringFaultFailsLoudly:
    @pytest.mark.asyncio
    async def test_stale_buffer_map_entry_fails_the_run(self, monkeypatch):
        """A mapped buffer the agent lacks is a fault, not a skip.

        This is the assertion the original code could not make: it logged a
        warning, returned, and let the run finish ``complete``.

        Validates: Requirements 6.5
        """
        pipeline, agents, _ = _build_pipeline()
        _spy_evaluate(
            agents["delivery_prioritization"], publishes=_priority_list()
        )
        monkeypatch.setitem(
            FuelDistributionPipeline._STAGE_BUFFER_MAP,
            "delivery_prioritization",
            "_buffer_that_does_not_exist",
        )

        run_id = await pipeline.run(tenant_id=TENANT_ID)
        status = await pipeline.get_status(run_id)

        assert status["state"] == PipelineState.FAILED.value
        assert status["failed_agent"] == "delivery_prioritization"
        assert "_buffer_that_does_not_exist" in status["error_message"]

    @pytest.mark.asyncio
    async def test_an_untriggerable_next_stage_fails_the_run(self):
        """A successor with no ``_signal_buffer`` can never be evaluated."""

        class _Untriggerable:
            """Has the mapped typed buffer, but no ``_signal_buffer``.

            The payload would land and ``evaluate()`` would still never run,
            which is the subtler half of the original defect.
            """

            agent_id = "compartment_loading"

            def __init__(self) -> None:
                self._priority_buffer: List[Any] = []

            async def monitor_cycle(self):
                return [], []

        pipeline, agents, deps = _build_pipeline()
        agents["compartment_loading"] = _Untriggerable()
        pipeline = FuelDistributionPipeline(
            agents=agents,
            ws_manager=deps["ws_manager"],
            signal_bus=deps["signal_bus"],
        )
        _spy_evaluate(agents["delivery_prioritization"])

        run_id = await pipeline.run(tenant_id=TENANT_ID)
        status = await pipeline.get_status(run_id)

        assert status["state"] == PipelineState.FAILED.value
        assert status["failed_agent"] == "delivery_prioritization"
        assert "_signal_buffer" in status["error_message"]


# ---------------------------------------------------------------------------
# A fully real run, no spies, no stand-ins
# ---------------------------------------------------------------------------


def _empty_es() -> MagicMock:
    """An ES service that answers every read with "nothing found".

    Covers the three shapes the four agents ask for: hit lists, terms-agg
    buckets, and single-document gets.
    """
    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"tenants": {"buckets": []}},
        }
    )
    es.get_document = AsyncMock(return_value=None)
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.bulk_index = AsyncMock(return_value={"errors": False, "items": []})
    es.count_documents = AsyncMock(return_value=0)
    es.delete_document = AsyncMock(return_value={"result": "deleted"})
    return es


def _count_real_evaluate(agent) -> AsyncMock:
    """Make ``evaluate`` countable while still running the genuine method.

    Distinct from ``_spy_evaluate``, which replaces the implementation. Here the
    real scoring code executes; the wrapper only records that the pipeline
    reached it.
    """
    real = agent.evaluate
    counter = AsyncMock(side_effect=real)
    agent.evaluate = counter
    return counter


class TestFullyRealRunCompletes:
    """No stage is faked, so errors the silent skip used to mask can surface.

    Every earlier test in this file substitutes the first stage and/or replaces
    ``evaluate``. That is deliberate — it isolates the transport. But it leaves
    one question open: now that three stages genuinely execute for the first
    time on any production path, does a real run still finish?

    Before the fix this was unanswerable. ``_capture_and_inject`` returned early
    on the stale ``tank_forecasting`` map entry, so stages 2–4 never reached
    their own code and any latent error inside them was invisible. The run
    reported ``complete`` either way, which is precisely why the break went
    unnoticed. This test pins the honest outcome: with no data, a real run
    completes rather than failing, and each stage is actually entered.
    """

    def _real_pipeline(self):
        from Agents.overlay.tank_forecasting_agent import TankForecastingAgent

        deps = _make_deps()
        deps["es_service"] = _empty_es()

        agents = {
            "tank_forecasting": TankForecastingAgent(**deps),
            "delivery_prioritization": DeliveryPrioritizationAgent(**deps),
            "compartment_loading": CompartmentLoadingAgent(**deps),
            "route_planning": RoutePlanningAgent(**deps),
        }
        pipeline = FuelDistributionPipeline(
            agents=agents,
            ws_manager=deps["ws_manager"],
            signal_bus=deps["signal_bus"],
        )
        return pipeline, agents, deps

    @pytest.mark.asyncio
    async def test_real_agents_with_no_data_complete_the_run(self):
        """Validates: Requirements 6.1, 6.4, 6.5"""
        pipeline, _, _ = self._real_pipeline()

        run_id = await pipeline.run(tenant_id=TENANT_ID)
        status = await pipeline.get_status(run_id)

        assert status["state"] == PipelineState.COMPLETE.value, (
            "a real run over empty data failed: "
            f"{status['failed_agent']} — {status['error_message']}"
        )
        assert status["failed_agent"] is None
        assert status["error_message"] is None

    @pytest.mark.asyncio
    async def test_every_real_stage_is_entered(self):
        """All four ``evaluate`` methods run their own code, not a substitute."""
        pipeline, agents, _ = self._real_pipeline()
        counters = {
            agent_id: _count_real_evaluate(agent)
            for agent_id, agent in agents.items()
        }

        await pipeline.run(tenant_id=TENANT_ID)

        assert {
            agent_id: counter.await_count
            for agent_id, counter in counters.items()
        } == {
            "tank_forecasting": 1,
            "delivery_prioritization": 1,
            "compartment_loading": 1,
            "route_planning": 1,
        }


# ---------------------------------------------------------------------------
# The stage produces real output, not just a completed status
# ---------------------------------------------------------------------------


class TestPrioritizationProducesOutputEndToEnd:
    """One pending order in, one scored priority out, reaching the next stage.

    ``TestFullyRealRunCompletes`` proves a real run does not crash. It cannot
    prove the pipeline does anything, because there is no data to act on — and
    "completes over no data" is exactly the false reassurance the original
    defect gave. This test supplies a single pending will-call order and follows
    it through the genuine ``DeliveryPrioritizationAgent`` scoring path to the
    genuine ``CompartmentLoadingAgent._priority_buffer``.

    Note what this pins about the design: ``evaluate()`` publishes the
    ``DeliveryPriorityList`` on the SignalBus and does **not** write
    ``mvp_delivery_priorities`` — ``_persist_priority_list`` is a legacy compat
    stub with no caller on the production path. The list reaching the loading
    stage's buffer is therefore the only observable handoff, which is precisely
    what the stale buffer map used to sever.

    Validates: Requirements 1.2, 1.3, 1.5
    """

    @staticmethod
    def _order() -> Dict[str, Any]:
        """A will-call order two hours from its window closing.

        Will-call is scored from ``delivery_window_end`` proximity rather than
        from a forecast, so the order needs no forecast document to produce a
        real (non-``scoring_input_missing``) score. That keeps this test about
        transport between stages rather than about forecast fidelity.
        """
        from datetime import datetime, timedelta, timezone

        return {
            "order_id": "order-wc-1",
            "tenant_id": TENANT_ID,
            "status": "confirmed",
            "call_type": "will_call",
            "customer_tank_id": "tank-wc-1",
            "product_code": "AGO",
            "delivery_window_end": (
                datetime.now(timezone.utc) + timedelta(hours=2)
            ).isoformat(),
        }

    def _es_with_one_pending_order(self) -> MagicMock:
        """Route ES reads by index, answering the two order queries with data."""
        order = self._order()

        async def _search(index, query, size=10, *args, **kwargs):
            if index == "fuel_orders_current":
                # size 0 + aggs is the cross-tenant discovery query;
                # size 1000 is the per-tenant order fetch.
                if query.get("size") == 0 or "aggs" in query:
                    return {
                        "hits": {"hits": []},
                        "aggregations": {
                            "tenants": {
                                "buckets": [{"key": TENANT_ID, "doc_count": 1}]
                            }
                        },
                    }
                return {"hits": {"hits": [{"_source": order}]}}
            return {"hits": {"hits": []}, "aggregations": {}}

        es = _empty_es()
        es.search_documents = AsyncMock(side_effect=_search)
        return es

    @pytest.mark.asyncio
    async def test_a_pending_order_becomes_a_priority_in_the_loading_buffer(self):
        deps = _make_deps()
        deps["es_service"] = self._es_with_one_pending_order()

        prioritization = DeliveryPrioritizationAgent(**deps)
        loading = CompartmentLoadingAgent(**deps)
        agents = {
            "tank_forecasting": _PublishingFirstStage(
                deps["signal_bus"], publish=False
            ),
            "delivery_prioritization": prioritization,
            "compartment_loading": loading,
            "route_planning": RoutePlanningAgent(**deps),
        }
        pipeline = FuelDistributionPipeline(
            agents=agents,
            ws_manager=deps["ws_manager"],
            signal_bus=deps["signal_bus"],
        )

        # ``evaluate`` drains the buffer, so read it at the moment loading runs.
        seen: List[Any] = []

        async def _capture(signals):
            seen.extend(loading._priority_buffer)
            return []

        loading.evaluate = AsyncMock(side_effect=_capture)

        run_id = await pipeline.run(tenant_id=TENANT_ID)

        status = await pipeline.get_status(run_id)
        assert status["state"] == PipelineState.COMPLETE.value

        assert len(seen) == 1, (
            "the loading stage's _priority_buffer should hold exactly the one "
            "DeliveryPriorityList; the stage also publishes an "
            "InterventionProposal, and because evaluate() reads "
            "priority_lists[-1] an unfiltered injection makes the proposal win "
            f"— got {[type(item).__name__ for item in seen]}"
        )
        (priority_list,) = seen
        assert isinstance(priority_list, DeliveryPriorityList)
        assert priority_list.tenant_id == TENANT_ID

        (priority,) = priority_list.priorities
        assert priority.station_id == "tank-wc-1"
        assert priority.priority_bucket in (
            PriorityBucket.CRITICAL,
            PriorityBucket.HIGH,
        )
        assert priority.priority_score > 0.6
        assert "scoring_input_missing" not in priority.reasons
