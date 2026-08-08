"""Unit tests for bootstrap/compliance.py.

This module had no test coverage at all, which the changed-file coverage gate
surfaced the moment its two periodic sweeps were migrated onto the sweep leader.
The gap mattered: `bootstrap/compliance.py` wires two daily crons
(price-protection expiry, rack-price refresh) and three autonomous cron agents,
and nothing checked that any of them were scheduled.

The assertions here are about the wiring, not the domain logic each job performs
(that has its own tests). Specifically: both crons must be scheduled through
``run_periodic``, which is what makes them run on the elected sweep leader only.
A raw ``while True`` loop would run in every process and reintroduce the
duplicate-sweep hazard that pinned the API to one task.

Requirements: 1.5, 3.6, 11.6
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from bootstrap.container import ServiceContainer


@pytest.fixture(autouse=True)
def _mock_es_module():
    """Prevent real ES connections during import."""
    mock_es_mod = MagicMock()
    mock_es_mod.elasticsearch_service = MagicMock()
    saved = sys.modules.get("services.elasticsearch_service")
    sys.modules["services.elasticsearch_service"] = mock_es_mod
    yield
    if saved is None:
        sys.modules.pop("services.elasticsearch_service", None)
    else:
        sys.modules["services.elasticsearch_service"] = saved
    sys.modules.pop("bootstrap.compliance", None)


@pytest.fixture
def container():
    c = ServiceContainer()
    c.settings = MagicMock()
    c.es_service = MagicMock()
    # A scheduler that accepts registrations without starting real agent loops.
    scheduler = MagicMock()
    scheduler.register = MagicMock()

    async def _start_all():
        return None

    scheduler.start_all = _start_all
    c.agent_scheduler = scheduler
    return c


class _CapturedJob:
    def __init__(self, name, interval, cycle, run_immediately=False):
        self.name = name
        self.interval = interval
        self.cycle = cycle
        self.run_immediately = run_immediately


@pytest.fixture
def captured_jobs():
    """Replace ``run_periodic`` with a spy and swallow the created tasks.

    ``run_periodic`` is patched rather than ``asyncio.create_task`` so the test
    asserts on the job's identity — name, interval, cycle callable — instead of
    on an opaque coroutine object.
    """
    jobs: list[_CapturedJob] = []

    def _fake_run_periodic(name, interval, cycle, *, run_immediately=False):
        jobs.append(_CapturedJob(name, interval, cycle, run_immediately))

        async def _noop():
            return None

        return _noop()

    sys.modules.pop("bootstrap.compliance", None)
    import bootstrap.compliance as compliance_mod

    with patch.object(compliance_mod, "run_periodic", _fake_run_periodic):
        yield jobs, compliance_mod

    # The captured coroutines were never awaited by design; close them so the
    # event loop does not warn.
    for task_attr in ("_price_protection_expiry_task", "_rack_price_refresh_task"):
        task = getattr(compliance_mod, task_attr, None)
        if task is not None and not task.done():
            task.cancel()
        setattr(compliance_mod, task_attr, None)


class TestComplianceCronsAreLeaderGated:
    """Both daily crons must go through ``run_periodic``.

    That is the whole reason the API can run more than one task. A cron wired as
    a bare ``while True`` loop would transition the same terminal contracts, and
    refresh the same rack prices, once per replica.
    """

    @pytest.mark.asyncio
    async def test_both_crons_are_scheduled_through_run_periodic(
        self, mock_app_and_run, captured_jobs
    ):
        jobs, _mod = captured_jobs
        names = {job.name for job in jobs}

        assert "compliance.price-protection-expiry" in names, (
            f"the price-protection expiry cron is not leader-gated: {names}"
        )
        assert "compliance.rack-price-refresh" in names, (
            f"the rack-price refresh cron is not leader-gated: {names}"
        )

    @pytest.mark.asyncio
    async def test_the_crons_use_their_modules_declared_intervals(
        self, mock_app_and_run, captured_jobs
    ):
        from commerce.services.price_protection_expiry_job import (
            PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS,
        )
        from commerce.services.rack_price_refresh_job import (
            RACK_PRICE_REFRESH_INTERVAL_SECONDS,
        )

        jobs, _mod = captured_jobs
        by_name = {job.name: job for job in jobs}

        assert (
            by_name["compliance.price-protection-expiry"].interval
            == PRICE_PROTECTION_EXPIRY_INTERVAL_SECONDS
        )
        assert (
            by_name["compliance.rack-price-refresh"].interval
            == RACK_PRICE_REFRESH_INTERVAL_SECONDS
        )

    @pytest.mark.asyncio
    async def test_neither_cron_runs_at_boot(self, mock_app_and_run, captured_jobs):
        """Both are daily sweeps; running one during boot would delay startup for
        a job whose deadline is 24 hours away."""
        jobs, _mod = captured_jobs
        for job in jobs:
            assert job.run_immediately is False, job.name

    @pytest.mark.asyncio
    async def test_each_cycle_is_awaitable_and_independent(
        self, mock_app_and_run, captured_jobs
    ):
        """``run_periodic`` calls the cycle with no arguments, so a cycle that
        needs one would fail on the first tick rather than at wiring time."""
        jobs, _mod = captured_jobs
        for job in jobs:
            assert asyncio.iscoroutinefunction(job.cycle), job.name


class TestComplianceShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_both_cron_tasks(self):
        sys.modules.pop("bootstrap.compliance", None)
        import bootstrap.compliance as compliance_mod

        async def _noop():
            await asyncio.sleep(3600)

        price_task = asyncio.create_task(_noop())
        rack_task = asyncio.create_task(_noop())
        compliance_mod._price_protection_expiry_task = price_task
        compliance_mod._rack_price_refresh_task = rack_task

        await compliance_mod.shutdown(MagicMock(), ServiceContainer())

        assert price_task.cancelled()
        assert rack_task.cancelled()

        compliance_mod._price_protection_expiry_task = None
        compliance_mod._rack_price_refresh_task = None

    @pytest.mark.asyncio
    async def test_shutdown_tolerates_never_having_started(self):
        """Every wiring block is guarded, so a boot where compliance failed
        early must still shut down cleanly."""
        sys.modules.pop("bootstrap.compliance", None)
        import bootstrap.compliance as compliance_mod

        compliance_mod._price_protection_expiry_task = None
        compliance_mod._rack_price_refresh_task = None
        compliance_mod._driver_expiry_cron_agent = None
        compliance_mod._asset_cert_expiry_cron_agent = None
        compliance_mod._meter_calibration_cron_agent = None

        await compliance_mod.shutdown(MagicMock(), ServiceContainer())


@pytest.fixture
async def mock_app_and_run(container, captured_jobs):
    """Run ``compliance.initialize`` once with the spy in place.

    Every wiring block in ``initialize`` is individually guarded, so the blocks
    whose collaborators are absent from this container degrade with a warning
    and leave the cron wiring — the subject of these tests — intact.
    """
    _jobs, compliance_mod = captured_jobs
    app = MagicMock()
    await compliance_mod.initialize(app, container)
    return app
