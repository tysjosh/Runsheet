"""
Unit tests for :mod:`integrations.integration_scheduler`.

Covers Capability 5 / Task 9.2 / Requirements 5.1.5, 5.1.6 of the fuel-ops
hardening spec:

* A successful :meth:`IntegrationScheduler._execute_sync` call persists a
  terminal :class:`SyncRun` with ``status="success"`` and resets the
  owning :class:`IntegrationInstance`'s ``retry_count`` / ``last_error``
  fields.
* Transient failures are retried with exponential backoff up to
  ``max_retries`` (default 5). A success on the second attempt is
  recorded as ``status="success"`` and does NOT raise a RiskSignal.
* Retry exhaustion flips the instance to ``status="error"`` and
  publishes a :class:`RiskSignal` of type
  ``integration_sync_failed`` on the :class:`SignalBus`.
* Malformed cron expressions surface as an instance-error transition
  without raising an exhaustion alert (config error, not sync error).
* Disabled instances are never scheduled, and a mid-cycle disable
  unschedules the job cleanly.
* The scheduler never raises from the cron entry point so APScheduler
  keeps ticking even when the repository / ES layer explodes.

The APScheduler dependency is replaced with a recording fake so these
tests run with zero third-party network or subprocess surface.

Validates: Requirements 5.1.5, 5.1.6.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity
from fuel.services.fuel_ops_es_mappings import INTEGRATION_SYNC_RUNS_INDEX
from integrations.connector_base import (
    ConnectionResult,
    IntegrationConnector,
    IntegrationInstance,
    IntegrationInstanceRepository,
    SyncRun,
)
from integrations.integration_scheduler import (
    DEFAULT_MAX_RETRIES,
    INTEGRATION_INSTANCE_ENTITY_TYPE,
    INTEGRATION_SCHEDULER_AGENT_ID,
    INTEGRATION_SYNC_FAILED_SIGNAL_TYPE,
    IntegrationScheduler,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stand-in matching the surface used by the scheduler."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.search_calls: List[Dict[str, Any]] = []
        self.index_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []
        self.raise_on_index: bool = False

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        self.index_calls.append({"index": index, "id": doc_id, "doc": dict(document)})
        if self.raise_on_index:
            raise RuntimeError("simulated ES outage")
        self.docs[doc_id] = dict(document)
        return {"_id": doc_id}

    async def update_document(self, index: str, doc_id: str, partial_doc: Dict[str, Any]):
        self.update_calls.append({"index": index, "id": doc_id, "partial": dict(partial_doc)})
        existing = self.docs.get(doc_id, {})
        self.docs[doc_id] = {**existing, **partial_doc}
        return {"_id": doc_id}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        self.delete_calls.append(doc_id)
        return self.docs.pop(doc_id, None) is not None

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100):
        self.search_calls.append({"index": index, "query": query, "size": size})
        must = query.get("query", {}).get("bool", {}).get("must") or []
        if not must and "term" in query.get("query", {}):
            must = [query["query"]]
        matched = []
        for doc in self.docs.values():
            ok = True
            for clause in must:
                for field, expected in (clause.get("term") or {}).items():
                    if doc.get(field) != expected:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                matched.append(doc)
        return {"hits": {"hits": [{"_source": dict(d)} for d in matched[:size]]}}


class _FakeSignalBus:
    def __init__(self) -> None:
        self.published: List[Any] = []

    async def publish(self, message: Any) -> int:
        self.published.append(message)
        return 1


class _FakeAPScheduler:
    """Stand-in for ``apscheduler.schedulers.asyncio.AsyncIOScheduler``."""

    def __init__(self) -> None:
        self.started = False
        self.shutdown_called = False
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:  # noqa: D401
        self.shutdown_called = True
        self.started = False

    def add_job(self, func, *, trigger, args, id, replace_existing, coalesce, max_instances, misfire_grace_time):  # noqa: D401
        self.jobs[id] = {
            "func": func,
            "trigger": trigger,
            "args": list(args),
        }

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


class _DummyCronTrigger:
    def __init__(self, cron: str) -> None:
        self.cron = cron

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_DummyCronTrigger({self.cron!r})"


def _trigger_factory(cron: str) -> Any:
    if "bad" in cron:
        raise ValueError("malformed cron")
    return _DummyCronTrigger(cron)


class _RecordingConnector(IntegrationConnector):
    """Concrete connector whose sync_pull response is scripted per call."""

    category = "accounting"
    provider_name = "quickbooks_online"

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[datetime] = []

    async def connect(self, credentials):  # pragma: no cover - not exercised
        return ConnectionResult()

    async def sync_pull(self, since: datetime) -> SyncRun:
        self.calls.append(since)
        if not self._responses:
            raise RuntimeError("no more scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def sync_push(self, payload):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover - not exercised
        return None


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _base_instance(**overrides: Any) -> IntegrationInstance:
    payload: Dict[str, Any] = {
        "instance_id": "integration_001",
        "tenant_id": "tenant-A",
        "provider_name": "quickbooks_online",
        "category": "accounting",
        "enabled": True,
        "status": "connected",
        "schedule_cron": "0 */6 * * *",
        "retry_count": 0,
    }
    payload.update(overrides)
    return IntegrationInstance(**payload)


def _success_sync_run(instance: IntegrationInstance) -> SyncRun:
    return SyncRun(
        run_id=f"run_{instance.instance_id}",
        tenant_id=instance.tenant_id,
        instance_id=instance.instance_id,
        provider_name=instance.provider_name,
        operation="pull",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        status="success",
        record_counts={"invoices": 7},
    )


async def _sleep_noop(_: float) -> None:
    return None


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def repository(es: _FakeESService) -> IntegrationInstanceRepository:
    return IntegrationInstanceRepository(es_service=es)


@pytest.fixture
def signal_bus() -> _FakeSignalBus:
    return _FakeSignalBus()


@pytest.fixture
def aps() -> _FakeAPScheduler:
    return _FakeAPScheduler()


def _make_scheduler(
    repository: IntegrationInstanceRepository,
    es: _FakeESService,
    *,
    connector: IntegrationConnector,
    signal_bus: Optional[_FakeSignalBus] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    scheduler: Optional[_FakeAPScheduler] = None,
) -> IntegrationScheduler:
    aps_instance = scheduler or _FakeAPScheduler()

    def factory(_: IntegrationInstance) -> IntegrationConnector:
        return connector

    return IntegrationScheduler(
        repository=repository,
        es_service=es,
        connector_factory=factory,
        signal_bus=signal_bus,
        max_retries=max_retries,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
        backoff_base=2.0,
        scheduler_factory=lambda: aps_instance,
        trigger_factory=_trigger_factory,
        sleep=_sleep_noop,
    )


# ---------------------------------------------------------------------------
# Construction & lifecycle
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_none_repository(self, es: _FakeESService):
        with pytest.raises(ValueError):
            IntegrationScheduler(
                repository=None,  # type: ignore[arg-type]
                es_service=es,
                connector_factory=lambda _: _RecordingConnector([]),
            )

    def test_rejects_none_es(self, repository: IntegrationInstanceRepository):
        with pytest.raises(ValueError):
            IntegrationScheduler(
                repository=repository,
                es_service=None,
                connector_factory=lambda _: _RecordingConnector([]),
            )

    def test_rejects_negative_max_retries(
        self, repository: IntegrationInstanceRepository, es: _FakeESService
    ):
        with pytest.raises(ValueError):
            IntegrationScheduler(
                repository=repository,
                es_service=es,
                connector_factory=lambda _: _RecordingConnector([]),
                max_retries=-1,
            )

    def test_rejects_backoff_base_le_one(
        self, repository: IntegrationInstanceRepository, es: _FakeESService
    ):
        with pytest.raises(ValueError):
            IntegrationScheduler(
                repository=repository,
                es_service=es,
                connector_factory=lambda _: _RecordingConnector([]),
                backoff_base=1.0,
            )

    def test_push_operation_requires_payload_builder(
        self, repository: IntegrationInstanceRepository, es: _FakeESService
    ):
        with pytest.raises(ValueError):
            IntegrationScheduler(
                repository=repository,
                es_service=es,
                connector_factory=lambda _: _RecordingConnector([]),
                operation="push",
            )

    @pytest.mark.asyncio
    async def test_schedule_before_start_raises(
        self, repository: IntegrationInstanceRepository, es: _FakeESService
    ):
        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([])
        )
        with pytest.raises(RuntimeError):
            await scheduler.schedule_instance(_base_instance())


# ---------------------------------------------------------------------------
# Scheduling instances
# ---------------------------------------------------------------------------


class TestScheduling:
    @pytest.mark.asyncio
    async def test_start_loads_enabled_instances_and_registers_jobs(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        aps: _FakeAPScheduler,
    ):
        # Two enabled + one disabled instance across two tenants.
        await repository.create("tenant-A", _base_instance())
        await repository.create(
            "tenant-B",
            _base_instance(
                instance_id="integration_002",
                tenant_id="tenant-B",
                provider_name="veeder_root",
                category="tank_monitor",
            ),
        )
        await repository.create(
            "tenant-A",
            _base_instance(
                instance_id="integration_003",
                enabled=False,
            ),
        )

        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([]), scheduler=aps
        )
        await scheduler.start()

        assert aps.started is True
        assert set(aps.jobs.keys()) == {
            "integration:integration_001",
            "integration:integration_002",
        }
        # Job args must carry tenant_id + instance_id verbatim.
        args_001 = aps.jobs["integration:integration_001"]["args"]
        assert args_001 == ["tenant-A", "integration_001"]

    @pytest.mark.asyncio
    async def test_disabled_instance_is_not_scheduled_and_removes_existing(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        aps: _FakeAPScheduler,
    ):
        await repository.create("tenant-A", _base_instance())
        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([]), scheduler=aps
        )
        await scheduler.start()
        assert "integration:integration_001" in aps.jobs

        # Flip to disabled and reschedule → job should vanish.
        updated = await repository.update(
            "tenant-A", "integration_001", {"enabled": False}
        )
        assert updated is not None
        scheduled = await scheduler.schedule_instance(updated)
        assert scheduled is False
        assert "integration:integration_001" not in aps.jobs

    @pytest.mark.asyncio
    async def test_malformed_cron_marks_instance_error_without_signal(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        signal_bus: _FakeSignalBus,
        aps: _FakeAPScheduler,
    ):
        await repository.create(
            "tenant-A",
            _base_instance(schedule_cron="bad cron expression"),
        )
        scheduler = _make_scheduler(
            repository,
            es,
            connector=_RecordingConnector([]),
            signal_bus=signal_bus,
            scheduler=aps,
        )
        await scheduler.start()

        assert aps.jobs == {}
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None
        assert stored.status == "error"
        assert stored.last_error is not None
        # Config error must NOT emit the exhaustion RiskSignal
        # (that path is reserved for post-retry failures).
        assert signal_bus.published == []


# ---------------------------------------------------------------------------
# Execution / retry loop
# ---------------------------------------------------------------------------


class TestExecuteSync:
    @pytest.mark.asyncio
    async def test_successful_run_persists_syncrun_and_resets_retry_count(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        signal_bus: _FakeSignalBus,
    ):
        await repository.create(
            "tenant-A", _base_instance(retry_count=3, status="error")
        )
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None
        connector = _RecordingConnector([_success_sync_run(stored)])
        scheduler = _make_scheduler(
            repository, es, connector=connector, signal_bus=signal_bus
        )

        run = await scheduler._execute_sync(stored)
        assert run.status == "success"
        assert run.record_counts == {"invoices": 7}

        # SyncRun indexed into INTEGRATION_SYNC_RUNS_INDEX
        sync_run_index_calls = [
            c for c in es.index_calls if c["index"] == INTEGRATION_SYNC_RUNS_INDEX
        ]
        assert len(sync_run_index_calls) == 1
        assert sync_run_index_calls[0]["doc"]["status"] == "success"
        assert sync_run_index_calls[0]["doc"]["tenant_id"] == "tenant-A"

        # Instance health reset
        refreshed = await repository.get("tenant-A", "integration_001")
        assert refreshed is not None
        assert refreshed.status == "connected"
        assert refreshed.retry_count == 0
        assert refreshed.last_error is None
        assert refreshed.last_sync_at is not None

        # No alert raised
        assert signal_bus.published == []
        # Connector called exactly once
        assert len(connector.calls) == 1

    @pytest.mark.asyncio
    async def test_transient_failure_retries_and_eventually_succeeds(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        signal_bus: _FakeSignalBus,
    ):
        await repository.create("tenant-A", _base_instance())
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None

        connector = _RecordingConnector(
            [
                RuntimeError("transient 1"),
                RuntimeError("transient 2"),
                _success_sync_run(stored),
            ]
        )
        scheduler = _make_scheduler(
            repository, es, connector=connector, signal_bus=signal_bus
        )

        run = await scheduler._execute_sync(stored)
        assert run.status == "success"
        # 3 attempts: 2 failures + 1 success
        assert len(connector.calls) == 3
        # Still no alert because exhaustion did not occur
        assert signal_bus.published == []

    @pytest.mark.asyncio
    async def test_retry_exhaustion_marks_error_and_publishes_signal(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        signal_bus: _FakeSignalBus,
    ):
        await repository.create("tenant-A", _base_instance())
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None

        # max_retries=2 → 3 total attempts
        connector = _RecordingConnector(
            [
                RuntimeError("perma 1"),
                RuntimeError("perma 2"),
                RuntimeError("perma 3"),
            ]
        )
        scheduler = _make_scheduler(
            repository,
            es,
            connector=connector,
            signal_bus=signal_bus,
            max_retries=2,
        )

        run = await scheduler._execute_sync(stored)
        assert run.status == "error"
        assert run.error_details is not None and "perma 3" in run.error_details
        assert len(connector.calls) == 3

        # Instance flipped to error with retry_count == attempts (3)
        refreshed = await repository.get("tenant-A", "integration_001")
        assert refreshed is not None
        assert refreshed.status == "error"
        assert refreshed.retry_count == 3
        assert refreshed.last_error is not None

        # Exactly one RiskSignal published with the canonical shape
        assert len(signal_bus.published) == 1
        signal = signal_bus.published[0]
        assert isinstance(signal, RiskSignal)
        assert signal.source_agent == INTEGRATION_SCHEDULER_AGENT_ID
        assert signal.entity_id == "integration_001"
        assert signal.entity_type == INTEGRATION_INSTANCE_ENTITY_TYPE
        assert signal.severity == Severity.HIGH
        assert signal.tenant_id == "tenant-A"
        assert signal.context["signal_type"] == INTEGRATION_SYNC_FAILED_SIGNAL_TYPE
        assert signal.context["provider_name"] == "quickbooks_online"
        assert signal.context["retry_count"] == 3
        assert signal.context["max_retries"] == 2

    @pytest.mark.asyncio
    async def test_connector_reporting_error_status_triggers_retry(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        signal_bus: _FakeSignalBus,
    ):
        """A connector that returns ``status='error'`` should be retried."""
        await repository.create("tenant-A", _base_instance())
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None

        error_run = SyncRun(
            run_id="r1",
            tenant_id="tenant-A",
            instance_id="integration_001",
            provider_name="quickbooks_online",
            operation="pull",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            status="error",
            error_details="qbo 503",
        )
        connector = _RecordingConnector([error_run, _success_sync_run(stored)])
        scheduler = _make_scheduler(
            repository,
            es,
            connector=connector,
            signal_bus=signal_bus,
            max_retries=3,
        )

        run = await scheduler._execute_sync(stored)
        assert run.status == "success"
        assert len(connector.calls) == 2
        assert signal_bus.published == []

    @pytest.mark.asyncio
    async def test_exhaustion_without_signal_bus_does_not_raise(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
    ):
        await repository.create("tenant-A", _base_instance())
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None

        connector = _RecordingConnector(
            [RuntimeError("p1"), RuntimeError("p2")]
        )
        scheduler = _make_scheduler(
            repository,
            es,
            connector=connector,
            signal_bus=None,
            max_retries=1,
        )

        run = await scheduler._execute_sync(stored)
        assert run.status == "error"
        refreshed = await repository.get("tenant-A", "integration_001")
        assert refreshed is not None
        assert refreshed.status == "error"


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------


class TestJobEntryPoint:
    @pytest.mark.asyncio
    async def test_run_sync_job_unschedules_missing_instance(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        aps: _FakeAPScheduler,
    ):
        # Register a job, then delete the instance from ES so the tick
        # fires against a missing record.
        await repository.create("tenant-A", _base_instance())
        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([]), scheduler=aps
        )
        await scheduler.start()
        await repository.delete("tenant-A", "integration_001")

        await scheduler._run_sync_job("tenant-A", "integration_001")
        assert "integration:integration_001" not in aps.jobs

    @pytest.mark.asyncio
    async def test_run_sync_job_unschedules_disabled_instance(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
        aps: _FakeAPScheduler,
    ):
        await repository.create("tenant-A", _base_instance())
        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([]), scheduler=aps
        )
        await scheduler.start()
        await repository.update("tenant-A", "integration_001", {"enabled": False})

        await scheduler._run_sync_job("tenant-A", "integration_001")
        assert "integration:integration_001" not in aps.jobs

    @pytest.mark.asyncio
    async def test_sync_now_rejects_missing_instance(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
    ):
        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([])
        )
        with pytest.raises(LookupError):
            await scheduler.sync_now("tenant-A", "does-not-exist")

    @pytest.mark.asyncio
    async def test_sync_now_rejects_disabled_instance(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
    ):
        await repository.create(
            "tenant-A", _base_instance(enabled=False)
        )
        scheduler = _make_scheduler(
            repository, es, connector=_RecordingConnector([])
        )
        with pytest.raises(ValueError):
            await scheduler.sync_now("tenant-A", "integration_001")

    @pytest.mark.asyncio
    async def test_sync_now_returns_terminal_syncrun(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
    ):
        await repository.create("tenant-A", _base_instance())
        stored = await repository.get("tenant-A", "integration_001")
        assert stored is not None

        connector = _RecordingConnector([_success_sync_run(stored)])
        scheduler = _make_scheduler(
            repository, es, connector=connector
        )
        run = await scheduler.sync_now("tenant-A", "integration_001")
        assert run.status == "success"


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_exponential_backoff_respects_cap(
        self,
        repository: IntegrationInstanceRepository,
        es: _FakeESService,
    ):
        scheduler = IntegrationScheduler(
            repository=repository,
            es_service=es,
            connector_factory=lambda _: _RecordingConnector([]),
            initial_backoff_seconds=1.0,
            max_backoff_seconds=5.0,
            backoff_base=2.0,
            scheduler_factory=_FakeAPScheduler,
            trigger_factory=_trigger_factory,
            sleep=_sleep_noop,
        )
        # Sequence: 1 * 2**0, 1 * 2**1, 1 * 2**2, 1 * 2**3 clamped to 5.0.
        assert scheduler._backoff_for(0) == 1.0
        assert scheduler._backoff_for(1) == 2.0
        assert scheduler._backoff_for(2) == 4.0
        assert scheduler._backoff_for(3) == 5.0  # clamped
        assert scheduler._backoff_for(10) == 5.0  # clamped
