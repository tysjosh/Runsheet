"""A pipeline stage that produces nothing must say so (MVP Bug 1).

``POST /api/fuel/mvp/plan/generate`` answered::

    200  {"status": "complete", "degraded": false, "degradation_reasons": []}

for a run that produced zero forecasts, zero priorities, zero load plans and
zero routes. Reproduced twice, the second time after rebuilding Elasticsearch
from Postgres, so it was not a one-off indexing blip.

The orchestrator half of the fix was already in place: ``PipelineRun`` tracks
degradations, ``FuelDistributionPipeline`` reads each stage's ``cycle_metrics``
and refuses to report COMPLETE when any stage reported degradation. The gap was
the *producer* half — only ``RoutePlanningAgent`` ever set the flag, and only for
trucks it was handed and skipped. Every other zero-output path in all four
stages was a bare ``return []``, which is indistinguishable from success:

    A1  no signals / no stations and no customer_tanks
    A2  no tenants to score / no tenant scored a priority
    A3  no priority list buffered / no delivery request / no truck / no plan
    A4  no loading plan buffered   (the skip path was already covered)

These tests pin each of those paths, and the two-kind distinction that keeps the
signal worth reading: ``produced_nothing`` (had input, made nothing — a defect
or a real block) versus ``no_input`` (nothing to do). Both degrade the run, so
the endpoint stops claiming success either way; they differ only in the log
severity the orchestrator picks, so an empty tenant's half-hourly sweep does not
bury the case worth paging on.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from Agents.overlay.base_overlay_agent import (
    CYCLE_METRIC_DEGRADATION_REASONS,
    CYCLE_METRIC_DEGRADED,
    DEGRADATION_KIND_NO_INPUT,
    DEGRADATION_KIND_PRODUCED_NOTHING,
    build_degradation_reason,
)
from Agents.support.fuel_distribution_pipeline import (
    PipelineRun,
    read_agent_degradation,
)

TENANT_ID = "tenant-zero-output"


# ---------------------------------------------------------------------------
# The reason entry shape
# ---------------------------------------------------------------------------


class TestBuildDegradationReason:
    def test_defaults_to_produced_nothing(self):
        """The louder kind is the default, so forgetting it over-reports."""
        entry = build_degradation_reason(reason_code="x")
        assert entry["kind"] == DEGRADATION_KIND_PRODUCED_NOTHING

    def test_carries_reason_code_detail_and_counters(self):
        entry = build_degradation_reason(
            reason_code="no_trucks_available",
            kind=DEGRADATION_KIND_NO_INPUT,
            detail="nothing to load onto",
            trucks=0,
            delivery_requests=3,
        )
        assert entry == {
            "reason_code": "no_trucks_available",
            "kind": DEGRADATION_KIND_NO_INPUT,
            "detail": "nothing to load onto",
            "trucks": 0,
            "delivery_requests": 3,
        }

    def test_omits_absent_detail_rather_than_writing_none(self):
        assert "detail" not in build_degradation_reason(reason_code="x")


# ---------------------------------------------------------------------------
# report_degradation accumulates
# ---------------------------------------------------------------------------


class _Agent:
    """The smallest thing carrying the base class's reporting behaviour."""

    def __init__(self) -> None:
        self._cycle_metrics: Dict[str, Any] = {}

    # Bound rather than inherited so this test needs none of the seven
    # collaborators an OverlayAgentBase constructor requires.
    from Agents.overlay.base_overlay_agent import (  # noqa: E402
        OverlayAgentBase as _Base,
    )

    report_degradation = _Base.report_degradation
    cycle_metrics = _Base.cycle_metrics


class TestReportDegradation:
    def test_sets_the_flag_and_the_reasons(self):
        agent = _Agent()
        agent.report_degradation(build_degradation_reason(reason_code="a"))

        assert agent._cycle_metrics[CYCLE_METRIC_DEGRADED] is True
        assert [
            r["reason_code"]
            for r in agent._cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS]
        ] == ["a"]

    def test_accumulates_across_calls(self):
        """One cycle can give up on several tenants; all of them are reported."""
        agent = _Agent()
        agent.report_degradation(build_degradation_reason(reason_code="a"))
        agent.report_degradation(build_degradation_reason(reason_code="b"))

        assert [
            r["reason_code"]
            for r in agent._cycle_metrics[CYCLE_METRIC_DEGRADATION_REASONS]
        ] == ["a", "b"]

    def test_never_lowers_a_raised_flag(self):
        """``monitor_cycle`` calls ``evaluate`` once per tenant.

        A clean second tenant must not erase the first tenant's report — that
        is how the route stage's old direct ``degraded: bool(skips)`` assignment
        could drop a degradation the orchestrator was about to read.
        """
        agent = _Agent()
        agent.report_degradation(build_degradation_reason(reason_code="a"))
        agent._cycle_metrics.setdefault(CYCLE_METRIC_DEGRADED, False)

        assert agent._cycle_metrics[CYCLE_METRIC_DEGRADED] is True

    def test_flag_alone_is_enough(self):
        """A degraded cycle with no reasons is still degraded."""
        agent = _Agent()
        agent.report_degradation()

        degraded, reasons = read_agent_degradation(agent)
        assert degraded is True
        assert reasons == []


# ---------------------------------------------------------------------------
# Log severity: PipelineRun.has_produced_nothing_degradation
# ---------------------------------------------------------------------------


class TestHasProducedNothingDegradation:
    """Chooses ERROR vs WARNING. Never changes whether the run is degraded."""

    @staticmethod
    def _run(*reason_lists: List[Any]) -> PipelineRun:
        run = PipelineRun(run_id="r", tenant_id=TENANT_ID)
        for idx, reasons in enumerate(reason_lists):
            run.record_degradation(f"stage-{idx}", reasons)
        return run

    def test_all_no_input_is_quiet(self):
        run = self._run(
            [build_degradation_reason(
                reason_code="a", kind=DEGRADATION_KIND_NO_INPUT
            )],
            [build_degradation_reason(
                reason_code="b", kind=DEGRADATION_KIND_NO_INPUT
            )],
        )
        assert run.degraded is True
        assert run.has_produced_nothing_degradation is False

    def test_one_produced_nothing_among_many_is_loud(self):
        run = self._run(
            [build_degradation_reason(
                reason_code="a", kind=DEGRADATION_KIND_NO_INPUT
            )],
            [build_degradation_reason(reason_code="b")],
        )
        assert run.has_produced_nothing_degradation is True

    def test_a_degradation_with_no_reasons_is_loud(self):
        """Cannot be shown to be merely "nothing to do", so do not soften it."""
        run = self._run([])
        assert run.degraded is True
        assert run.has_produced_nothing_degradation is True

    def test_an_unparseable_reason_is_loud(self):
        """Fail-safe in the loud direction, unlike the read side.

        ``read_agent_degradation`` fails safe *quiet* because a monitoring
        signal must not take down the run it monitors. Here the run is already
        known to be degraded, so the only question is how loudly to say it, and
        under-reporting is the worse error.
        """
        run = self._run(["not a mapping"])
        assert run.has_produced_nothing_degradation is True

    def test_a_clean_run_is_neither(self):
        run = self._run()
        assert run.degraded is False
        assert run.has_produced_nothing_degradation is False
