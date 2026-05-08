"""
Integration_Scheduler — cron-driven sync orchestrator for Capability 5 / Task 9.2.

Task 9.2 / Requirements 5.1.5, 5.1.6 of the fuel-ops hardening spec:

    > THE Platform SHALL execute scheduled Sync_Runs via an
    > Integration_Scheduler that reads schedule_cron per instance and
    > respects the instance's enabled flag.
    >
    > IF a Sync_Run fails, THEN the Integration_Scheduler SHALL retry
    > with exponential backoff up to the tenant-configured ``max_retries``
    > (default 5) before marking the instance status as "error" and
    > surfacing an alert.

This module wraps APScheduler's :class:`AsyncIOScheduler` with per-
:class:`IntegrationInstance` :class:`CronTrigger` jobs. Each job:

1. Reloads the latest :class:`IntegrationInstance` snapshot from the
   :class:`IntegrationInstanceRepository` so disabled / deleted / rotated
   instances stop the run immediately.
2. Resolves a fresh :class:`IntegrationConnector` via the injected
   ``connector_factory`` so the Tenant_Credentials_Vault lookup happens
   at call time rather than at scheduler-start time.
3. Invokes the connector's ``sync_pull`` (or ``sync_push``) under a
   retry loop with exponential backoff ``initial_backoff_seconds * base ** attempt``
   up to ``max_backoff_seconds`` and ``max_retries`` attempts.
4. Persists a single terminal :class:`SyncRun` to the
   ``integration_sync_runs`` ES index capturing status, record counts,
   error details, and duration.
5. Updates the owning :class:`IntegrationInstance`'s rolling health
   fields (``status``, ``last_sync_at``, ``last_error``, ``retry_count``).
   Success resets ``retry_count`` to 0 and sets ``status="connected"``;
   exhaustion flips ``status="error"`` and publishes a
   :class:`RiskSignal` of type ``integration_sync_failed`` on the
   :class:`Agents.overlay.signal_bus.SignalBus`.

Design points:

* **Per-instance isolation.** Every job is keyed by ``instance_id`` so
  rescheduling an instance replaces its job atomically
  (``replace_existing=True``). A failure in one tenant's Veeder-Root
  instance cannot take down another tenant's QuickBooks instance.
* **Cron parsing delegated to APScheduler.** Requirement 5.1.5 says
  "reads schedule_cron per instance"; this module accepts the standard
  5-field cron expression APScheduler's :meth:`CronTrigger.from_crontab`
  parses. Malformed cron strings are caught, logged, and the offending
  instance is marked ``status="error"`` with a descriptive
  ``last_error`` so the admin UI can surface the typo.
* **Lazy APScheduler import.** The ``apscheduler`` dependency is imported
  inside the default scheduler factory so unit tests can inject a
  recording mock scheduler without pulling in APScheduler at all. This
  mirrors how :class:`integrations.rack_price_sync.RackPriceSyncService`
  defers its ES dependency to injection.
* **No exception leakage from the job.** An unhandled exception inside
  a cron job would prevent APScheduler from firing the next tick, so
  every failure path is logged, funnelled into the SyncRun record, and
  swallowed at the top-level ``_run_sync_job`` method.
* **Tenant scoping preserved end-to-end.** SyncRun rows carry
  ``tenant_id`` and every repository call is re-scoped on the
  instance's tenant; cross-tenant access is categorically impossible
  because the scheduler only ever addresses an instance through its
  owning tenant.

Validates: Requirements 5.1.5, 5.1.6.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from uuid import uuid4

from Agents.overlay.data_contracts import RiskSignal, Severity
from fuel.services.fuel_ops_es_mappings import INTEGRATION_SYNC_RUNS_INDEX
from integrations.connector_base import (
    IntegrationConnector,
    IntegrationInstance,
    IntegrationInstanceRepository,
    SyncOperation,
    SyncRun,
)
from services.external_call_tracing import (
    CircuitBreaker,
    CircuitOpenError,
    default_circuit_breaker,
    trace_external_call,
)
from services.metrics import fuelops_integration_sync_runs_total

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default tenant-configurable retry ceiling per Requirement 5.1.6.
DEFAULT_MAX_RETRIES: int = 5

#: Base of the exponential backoff. Mirrors the Python idiom
#: ``initial * base ** attempt``. We keep ``base=2.0`` so a failing run
#: sleeps 2s, 4s, 8s, 16s, 32s between attempts (with the default
#: initial value of 2.0 and max cap of 60.0). Tunable per tenant.
DEFAULT_BACKOFF_BASE: float = 2.0

#: Seconds to wait before the first retry. Intentionally short so
#: transient network hiccups don't stretch a 5-minute cron interval into
#: a 10-minute observed gap.
DEFAULT_INITIAL_BACKOFF_SECONDS: float = 2.0

#: Upper bound on a single backoff sleep. Prevents runaway waits on
#: large ``max_retries`` values without sacrificing exponential growth
#: for small ones.
DEFAULT_MAX_BACKOFF_SECONDS: float = 60.0

#: TTL (seconds) carried on the :class:`RiskSignal` emitted when an
#: instance's retries are exhausted. Matches the 30-minute default used
#: elsewhere in the SLA / delay pipeline so downstream subscribers see a
#: consistent window.
DEFAULT_ALERT_TTL_SECONDS: int = 1800

#: Confidence reported on the :class:`RiskSignal`. Exhaustion after
#: every retry attempt is an unambiguous failure, so we report 1.0.
DEFAULT_ALERT_CONFIDENCE: float = 1.0

#: :class:`RiskSignal.entity_type` value used for the integration
#: exhaustion alert. The alert consumer (admin UI / Storm_Mode /
#: operator dashboard) keys off this string.
INTEGRATION_INSTANCE_ENTITY_TYPE: str = "integration_instance"

#: :class:`RiskSignal.context` tag used by the alert consumer to route
#: the signal to the integration-health widget.
INTEGRATION_SYNC_FAILED_SIGNAL_TYPE: str = "integration_sync_failed"

#: :class:`RiskSignal.source_agent` string used on exhaustion alerts.
INTEGRATION_SCHEDULER_AGENT_ID: str = "integration_scheduler"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Factory invoked on every job tick to produce a
#: :class:`IntegrationConnector` for the given instance. The factory
#: owns credential-vault dereferencing and per-call HTTP client setup so
#: this scheduler stays provider-agnostic. May be sync or async.
ConnectorFactory = Callable[
    [IntegrationInstance],
    "Awaitable[IntegrationConnector] | IntegrationConnector",
]

#: Factory for the underlying scheduler. Injected so tests can pass a
#: recording fake without depending on APScheduler. The default
#: :func:`_default_scheduler_factory` imports APScheduler lazily.
SchedulerFactory = Callable[[], Any]

#: Sync-operation payload builder used when running push-mode jobs.
#: For ``operation="pull"`` this is ignored; for ``operation="push"``
#: the scheduler passes the returned dict to ``connector.sync_push``.
PushPayloadBuilder = Callable[[IntegrationInstance], "Awaitable[Dict[str, Any]] | Dict[str, Any]"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_scheduler_factory() -> Any:
    """Construct the default APScheduler AsyncIOScheduler.

    The import is deferred so module-import of this file does not
    require APScheduler — unit tests that inject a recording fake
    scheduler never pay that cost, and the error message when the
    dependency is missing is actionable (it points at
    ``requirements.txt``).
    """

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:  # pragma: no cover - dependency surface
        raise RuntimeError(
            "APScheduler is not installed. Add `APScheduler>=3.10.0` to "
            "Runsheet-backend/requirements.txt and re-run `pip install "
            "-r requirements.txt`."
        ) from exc
    # UTC throughout so cron expressions evaluate deterministically and
    # don't drift with DST.
    return AsyncIOScheduler(timezone=timezone.utc)


def _default_cron_trigger(cron: str) -> Any:
    """Parse a 5-field crontab expression via APScheduler's CronTrigger."""

    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover - dependency surface
        raise RuntimeError(
            "APScheduler is not installed; see requirements.txt for the "
            "pinned version."
        ) from exc
    return CronTrigger.from_crontab(cron, timezone=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class IntegrationScheduler:
    """APScheduler-backed orchestrator for per-instance Sync_Runs.

    The scheduler owns:

    * The :class:`AsyncIOScheduler` singleton (or whatever the
      ``scheduler_factory`` returned).
    * The mapping from ``instance_id`` to APScheduler ``job_id``.
    * The retry loop and the SyncRun / IntegrationInstance bookkeeping
      around each tick.

    Responsibilities explicitly NOT owned here:

    * Credential-vault access — the caller supplies a
      :data:`ConnectorFactory` that reads from the vault and constructs
      a ready-to-call :class:`IntegrationConnector`.
    * REST surface — ``POST /api/integrations/{id}/sync-now``,
      ``/enable``, ``/disable`` are wired in the endpoints module
      (Task 9.3); they call :meth:`sync_now` / :meth:`schedule_instance`
      / :meth:`unschedule_instance` on this scheduler.
    * Feature-flag gating — the caller's endpoint handler is expected to
      check ``overlay.integration.{provider_name}`` before calling
      :meth:`schedule_instance`.

    Args:
        repository: Tenant-scoped repository for
            :class:`IntegrationInstance` CRUD.
        es_service: Elasticsearch service used to persist
            :class:`SyncRun` rows to :data:`INTEGRATION_SYNC_RUNS_INDEX`.
        connector_factory: Callable that produces a connector instance
            for the given :class:`IntegrationInstance`. Called once per
            job tick so credential rotation takes effect immediately.
        signal_bus: Optional :class:`Agents.overlay.signal_bus.SignalBus`.
            When wired, retry exhaustion publishes a ``RiskSignal`` of
            severity ``HIGH`` with entity_type
            :data:`INTEGRATION_INSTANCE_ENTITY_TYPE`.
        max_retries: Tenant-configurable retry ceiling (Requirement
            5.1.6 default 5).
        initial_backoff_seconds: Wait before the first retry.
        max_backoff_seconds: Upper bound on any single backoff sleep.
        backoff_base: Exponential growth factor (``initial * base**n``).
        scheduler_factory: Override for the underlying scheduler
            constructor. Defaults to :func:`_default_scheduler_factory`.
        trigger_factory: Override for cron-string → trigger conversion
            so tests can inject deterministic triggers.
        operation: ``"pull"`` (default) or ``"push"``. Each instance is
            scheduled in a single direction — a provider that needs
            both should register two scheduler instances or rely on
            manual :meth:`sync_now` calls.
        push_payload_builder: Required when ``operation="push"``.
        clock: Optional zero-arg callable returning the current UTC
            datetime; injected for deterministic unit tests.
        sleep: Optional awaitable sleep used inside the retry loop;
            injected for deterministic unit tests so they don't actually
            wait on exponential backoff.
    """

    def __init__(
        self,
        repository: IntegrationInstanceRepository,
        es_service: Any,
        connector_factory: ConnectorFactory,
        signal_bus: Optional[Any] = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        scheduler_factory: SchedulerFactory = _default_scheduler_factory,
        trigger_factory: Callable[[str], Any] = _default_cron_trigger,
        operation: SyncOperation = "pull",
        push_payload_builder: Optional[PushPayloadBuilder] = None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        alert_ttl_seconds: int = DEFAULT_ALERT_TTL_SECONDS,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        if repository is None:
            raise ValueError("repository must not be None")
        if es_service is None:
            raise ValueError("es_service must not be None")
        if connector_factory is None:
            raise ValueError("connector_factory must not be None")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must be >= 0")
        if max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds must be >= 0")
        if backoff_base <= 1.0:
            # Exponential backoff with base <= 1 degenerates into linear
            # / decreasing waits. We hard-fail so a misconfigured
            # tenant doesn't silently DDoS the upstream.
            raise ValueError("backoff_base must be > 1.0")
        if operation == "push" and push_payload_builder is None:
            raise ValueError(
                "push_payload_builder is required when operation='push'"
            )
        if alert_ttl_seconds <= 0:
            raise ValueError("alert_ttl_seconds must be positive")

        self._repository = repository
        self._es = es_service
        self._connector_factory = connector_factory
        self._signal_bus = signal_bus
        self._max_retries = max_retries
        self._initial_backoff = float(initial_backoff_seconds)
        self._max_backoff = float(max_backoff_seconds)
        self._backoff_base = float(backoff_base)
        self._scheduler_factory = scheduler_factory
        self._trigger_factory = trigger_factory
        self._operation: SyncOperation = operation
        self._push_payload_builder = push_payload_builder
        self._clock = clock
        self._sleep = sleep
        self._alert_ttl_seconds = alert_ttl_seconds
        self._circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else default_circuit_breaker
        )

        # Lazy-constructed on ``start()`` so unit tests that only
        # exercise ``_run_sync_job`` never touch APScheduler at all.
        self._scheduler: Optional[Any] = None
        self._started: bool = False

        # ``instance_id → job_id`` map. APScheduler uses the job id as
        # its primary key; we mirror ``instance_id`` so
        # :meth:`unschedule_instance` / :meth:`reschedule_instance` can
        # locate the job without an O(n) scan.
        self._job_ids: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        """Return True when the underlying scheduler is running."""
        return self._started

    def _ensure_scheduler(self) -> Any:
        """Lazily construct the underlying APScheduler."""
        if self._scheduler is None:
            self._scheduler = self._scheduler_factory()
        return self._scheduler

    async def start(self) -> None:
        """Start the underlying scheduler and register every enabled instance.

        Loads ``IntegrationInstance`` rows across all tenants via the
        repository's internal ES query and schedules the ones that are
        both ``enabled=True`` and have a non-empty ``schedule_cron``.
        Instances with malformed cron expressions are marked
        ``status="error"`` (with a clear ``last_error``) but never raise
        so a single typo does not prevent the rest of the fleet from
        scheduling.
        """

        scheduler = self._ensure_scheduler()
        if self._started:
            logger.debug("IntegrationScheduler.start: already running, no-op")
            return

        scheduler.start()
        self._started = True
        logger.info("IntegrationScheduler started")

        await self._load_and_schedule_all()

    async def shutdown(self, wait: bool = True) -> None:
        """Stop the underlying scheduler gracefully."""

        if not self._started or self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntegrationScheduler: shutdown error: %s", exc
            )
        finally:
            self._started = False
            self._job_ids.clear()

    # ------------------------------------------------------------------
    # Instance registration
    # ------------------------------------------------------------------

    async def _load_and_schedule_all(self) -> None:
        """Fan out across every tenant + instance at startup."""

        instances = await self._load_all_enabled_instances()
        for instance in instances:
            await self._register_job(instance)

    async def _load_all_enabled_instances(self) -> "list[IntegrationInstance]":
        """Return every ``enabled=True`` instance across all tenants.

        The :class:`IntegrationInstanceRepository` is tenant-scoped by
        design so it cannot answer cross-tenant queries itself. This
        helper talks directly to the ES service with a tenant-less
        ``enabled`` filter because the scheduler is a platform-level
        worker that must service every tenant uniformly. Every
        returned row still carries its ``tenant_id`` — the retry loop
        and the :class:`SyncRun` persistence re-scope on that id so
        tenant isolation is preserved downstream.
        """

        try:
            resp = await self._es.search_documents(
                self._repository._index,  # type: ignore[attr-defined]
                {
                    "query": {"term": {"enabled": True}},
                    "size": self._repository.DEFAULT_LIST_SIZE,
                },
                self._repository.DEFAULT_LIST_SIZE,
            )
        except Exception as exc:
            logger.warning(
                "IntegrationScheduler: failed to load enabled instances: %s",
                exc,
            )
            return []

        hits_outer = resp.get("hits") if isinstance(resp, dict) else None
        hits = (hits_outer or {}).get("hits") or []
        out: list[IntegrationInstance] = []
        for hit in hits:
            source = (hit or {}).get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                out.append(IntegrationInstance(**source))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "IntegrationScheduler: dropping malformed instance "
                    "doc (instance_id=%s): %s",
                    source.get("instance_id"),
                    exc,
                )
        return out

    async def schedule_instance(self, instance: IntegrationInstance) -> bool:
        """Register or replace a cron job for the given instance.

        Returns ``True`` when the instance was scheduled, ``False`` when
        it was skipped (disabled, no cron, or malformed cron). The
        scheduler must be started; attempting to call this before
        :meth:`start` raises a :class:`RuntimeError`.
        """

        if not self._started:
            raise RuntimeError(
                "IntegrationScheduler.schedule_instance called before start()"
            )
        return await self._register_job(instance)

    async def reschedule_instance(self, instance: IntegrationInstance) -> bool:
        """Replace the existing job for this instance with a fresh one.

        Simply delegates to :meth:`schedule_instance`; APScheduler's
        ``replace_existing=True`` handles the atomic swap.
        """
        return await self.schedule_instance(instance)

    async def unschedule_instance(self, instance_id: str) -> bool:
        """Remove any job registered for ``instance_id``. Returns True when removed."""

        if not instance_id:
            raise ValueError("instance_id must be non-empty")
        job_id = self._job_ids.pop(instance_id, None)
        if job_id is None or self._scheduler is None:
            return False
        try:
            self._scheduler.remove_job(job_id)
        except Exception as exc:
            logger.warning(
                "IntegrationScheduler: remove_job(%s) failed: %s",
                job_id,
                exc,
            )
            return False
        return True

    async def _register_job(self, instance: IntegrationInstance) -> bool:
        """Internal: build a trigger and register the APScheduler job."""

        scheduler = self._ensure_scheduler()

        if not instance.enabled:
            # Disabled instances explicitly have any existing job
            # cancelled so the scheduler matches the repository state.
            await self.unschedule_instance(instance.instance_id)
            return False

        cron = instance.schedule_cron
        if not cron or not cron.strip():
            logger.info(
                "IntegrationScheduler: instance=%s has no schedule_cron; "
                "skipping (can still be invoked via sync_now)",
                instance.instance_id,
            )
            return False

        try:
            trigger = self._trigger_factory(cron.strip())
        except Exception as exc:
            # A malformed cron is operator error; we mark the instance
            # error'd so the admin UI surfaces it but do NOT publish an
            # exhaustion RiskSignal (Requirement 5.1.6 scopes that to
            # post-retry failures, not configuration errors).
            logger.warning(
                "IntegrationScheduler: instance=%s has malformed cron %r: %s",
                instance.instance_id,
                cron,
                exc,
            )
            await self._mark_instance_error(
                instance,
                last_error=f"invalid schedule_cron: {exc}",
                retry_count=instance.retry_count,
                raise_signal=False,
            )
            return False

        job_id = self._job_id_for(instance.instance_id)
        try:
            scheduler.add_job(
                self._run_sync_job,
                trigger=trigger,
                args=[instance.tenant_id, instance.instance_id],
                id=job_id,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
            )
        except Exception as exc:
            logger.error(
                "IntegrationScheduler: add_job failed for instance=%s: %s",
                instance.instance_id,
                exc,
            )
            return False

        self._job_ids[instance.instance_id] = job_id
        logger.info(
            "IntegrationScheduler: scheduled instance=%s provider=%s cron=%r",
            instance.instance_id,
            instance.provider_name,
            cron,
        )
        return True

    @staticmethod
    def _job_id_for(instance_id: str) -> str:
        """Build the APScheduler job id used for this instance.

        The ``integration:`` prefix namespaces the id so coexisting
        scheduled tasks (rack-price sync, weather ingester, …) cannot
        collide with an integration instance id.
        """

        return f"integration:{instance_id}"

    # ------------------------------------------------------------------
    # Manual / on-demand sync
    # ------------------------------------------------------------------

    async def sync_now(self, tenant_id: str, instance_id: str) -> SyncRun:
        """Trigger a synchronous run outside the cron schedule.

        Used by the ``POST /api/integrations/{id}/sync-now`` endpoint.
        Returns the terminal :class:`SyncRun` so the REST layer can
        return it directly. Raises :class:`LookupError` when the
        instance is missing for the given tenant, and :class:`ValueError`
        when the instance is disabled — callers translate these into
        HTTP 404 / HTTP 400 respectively.
        """

        instance = await self._repository.get(tenant_id, instance_id)
        if instance is None:
            raise LookupError(
                f"integration instance {instance_id!r} not found for tenant {tenant_id!r}"
            )
        if not instance.enabled:
            raise ValueError(
                f"integration instance {instance_id!r} is disabled"
            )
        return await self._execute_sync(instance)

    async def _run_sync_job(self, tenant_id: str, instance_id: str) -> None:
        """APScheduler tick entry point.

        A raised exception here would prevent APScheduler from
        scheduling the next tick; every failure path is therefore
        logged and swallowed. The :class:`SyncRun` persisted by
        :meth:`_execute_sync` carries the terminal state for
        operator review.
        """

        try:
            instance = await self._repository.get(tenant_id, instance_id)
        except Exception as exc:
            logger.error(
                "IntegrationScheduler: failed to reload instance=%s: %s",
                instance_id,
                exc,
            )
            return

        if instance is None:
            # Instance was deleted between scheduling and firing — drop
            # the orphan job so subsequent ticks are cheap.
            logger.info(
                "IntegrationScheduler: instance=%s no longer exists, "
                "unscheduling",
                instance_id,
            )
            await self.unschedule_instance(instance_id)
            return

        if not instance.enabled:
            logger.info(
                "IntegrationScheduler: instance=%s disabled mid-cycle, "
                "unscheduling",
                instance_id,
            )
            await self.unschedule_instance(instance_id)
            return

        try:
            await self._execute_sync(instance)
        except Exception as exc:  # pragma: no cover - defensive
            # _execute_sync is already defensive; this is a belt-and-
            # -braces net so a bug in the scheduler itself never
            # breaks APScheduler's own loop.
            logger.exception(
                "IntegrationScheduler: unhandled failure for instance=%s: %s",
                instance_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Retry loop + execution
    # ------------------------------------------------------------------

    async def _execute_sync(self, instance: IntegrationInstance) -> SyncRun:
        """Run the retry loop for a single instance and persist the SyncRun.

        Always returns a terminal :class:`SyncRun` — the caller can
        infer success vs. exhaustion from ``run.status``.
        """

        run_id = f"syncrun_{uuid4()}"
        started_at = self._clock()
        started_monotonic = time.monotonic()

        attempts = self._max_retries + 1  # initial attempt + retries
        last_exc: Optional[BaseException] = None
        record_counts: Dict[str, int] = {}
        last_status: str = "error"

        for attempt in range(attempts):
            try:
                connector = await self._resolve_connector(instance)
                produced = await self._invoke_connector(connector, instance)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "IntegrationScheduler: instance=%s attempt %d/%d failed: %s",
                    instance.instance_id,
                    attempt + 1,
                    attempts,
                    exc,
                )
                # Sleep only between attempts, not after the final failure.
                if attempt < attempts - 1:
                    await self._sleep(self._backoff_for(attempt))
                continue

            # Connector returned a SyncRun — project its status and
            # record counts into this scheduler-owned run. A connector
            # that reports ``status="error"`` is treated like a raised
            # exception for retry purposes so transient upstream
            # failures with structured responses (e.g. QBO 5xx) still
            # honour backoff.
            if isinstance(produced, SyncRun):
                record_counts = dict(produced.record_counts or {})
                if produced.status in ("success", "partial"):
                    last_status = produced.status
                    last_exc = None
                    break
                # ``running`` is not a terminal value; normalize to error.
                last_status = "error"
                last_exc = RuntimeError(
                    produced.error_details
                    or f"connector reported status={produced.status!r}"
                )
            else:
                # Connector ignored the contract. Surface as error.
                last_exc = TypeError(
                    "connector returned non-SyncRun "
                    f"({type(produced).__name__}) — see SyncRun contract"
                )

            if attempt < attempts - 1:
                await self._sleep(self._backoff_for(attempt))

        finished_at = self._clock()
        duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))

        if last_exc is None and last_status in ("success", "partial"):
            run = SyncRun(
                run_id=run_id,
                tenant_id=instance.tenant_id,
                instance_id=instance.instance_id,
                provider_name=instance.provider_name,
                operation=self._operation,
                started_at=started_at,
                finished_at=finished_at,
                status=last_status,  # type: ignore[arg-type]
                record_counts=record_counts,
                duration_ms=duration_ms,
            )
            await self._persist_sync_run(run)
            await self._mark_instance_success(
                instance, finished_at=finished_at
            )
            return run

        # Retries exhausted (or a contract violation surfaced). Persist
        # the error SyncRun, flip the instance to ``status="error"``,
        # and raise an alert RiskSignal.
        err_text = (
            str(last_exc)
            if last_exc is not None
            else "unknown failure (no exception captured)"
        )
        run = SyncRun(
            run_id=run_id,
            tenant_id=instance.tenant_id,
            instance_id=instance.instance_id,
            provider_name=instance.provider_name,
            operation=self._operation,
            started_at=started_at,
            finished_at=finished_at,
            status="error",
            record_counts=record_counts,
            error_details=err_text[:1000],  # bound unbounded tracebacks
            duration_ms=duration_ms,
        )
        await self._persist_sync_run(run)
        await self._mark_instance_error(
            instance,
            last_error=err_text[:500],
            retry_count=attempts,
            raise_signal=True,
        )
        return run

    def _backoff_for(self, attempt: int) -> float:
        """Compute the exponential-backoff sleep for a given attempt index.

        ``attempt`` is the zero-indexed attempt that just failed. So
        ``attempt=0`` is the first failure and controls the wait before
        the first retry. The formula is ``initial * base ** attempt``
        capped at ``max_backoff``.
        """

        wait = self._initial_backoff * (self._backoff_base ** attempt)
        if wait < 0:
            wait = 0.0
        if wait > self._max_backoff:
            wait = self._max_backoff
        return wait

    async def _resolve_connector(
        self, instance: IntegrationInstance
    ) -> IntegrationConnector:
        """Call the injected factory, handling both sync and async variants."""

        produced = self._connector_factory(instance)
        if asyncio.iscoroutine(produced):
            produced = await produced
        if not isinstance(produced, IntegrationConnector):
            raise TypeError(
                "connector_factory must return an IntegrationConnector, "
                f"got {type(produced).__name__}"
            )
        return produced

    async def _invoke_connector(
        self,
        connector: IntegrationConnector,
        instance: IntegrationInstance,
    ) -> SyncRun:
        """Dispatch to ``sync_pull`` or ``sync_push`` depending on mode.

        Every call is wrapped in :func:`trace_external_call` so the
        structured-log surface (``tenant_id``, ``provider`` =
        :attr:`IntegrationConnector.provider_name`, ``operation`` =
        ``sync_pull`` / ``sync_push``, ``duration_ms``, ``status``,
        ``error_code``) is uniform across QuickBooks Online,
        Veeder-Root, Geotab, Stripe, and any future connector. The
        per-``(tenant_id, provider)`` circuit breaker trips after 5
        consecutive failures and resets 60s later (Task 12.9 /
        Req 10.4.3), sparing the upstream while an outage persists.

        :class:`CircuitOpenError` is re-raised so the retry loop in
        :meth:`_execute_sync` treats it as a failure attempt and honors
        the exponential backoff before the next cycle. The metric
        increment is driven by the :class:`SyncRun.status` the
        connector returned, because the scheduler-owned metric maps to
        the run's terminal state (``success`` / ``partial`` /
        ``error``) rather than the raw call outcome.
        """

        async with trace_external_call(
            tenant_id=instance.tenant_id,
            provider=connector.provider_name,
            operation=f"sync_{self._operation}",
            circuit_breaker=self._circuit_breaker,
            # The metric is incremented separately once the SyncRun
            # terminal status is known (``success`` / ``partial`` /
            # ``error``) so the label matches the SyncRun outcome
            # rather than the raw wrapper status.
            metric=None,
            extra={"instance_id": instance.instance_id},
        ) as call:
            if self._operation == "pull":
                since = instance.last_sync_at or datetime.fromtimestamp(
                    0, tz=timezone.utc
                )
                run = await connector.sync_pull(since)
            else:
                assert self._push_payload_builder is not None
                payload = self._push_payload_builder(instance)
                if asyncio.iscoroutine(payload):
                    payload = await payload
                if not isinstance(payload, dict):
                    raise TypeError(
                        "push_payload_builder must return a dict, got "
                        f"{type(payload).__name__}"
                    )
                run = await connector.sync_push(payload)

            # A connector that reports a non-success SyncRun surfaces
            # here as a soft failure so the breaker still records it.
            if isinstance(run, SyncRun) and run.status == "error":
                call.set_status("error")
                if run.error_details:
                    call.set_error_code("sync_run_error")

            try:
                fuelops_integration_sync_runs_total.labels(
                    tenant_id=instance.tenant_id,
                    provider=connector.provider_name,
                    status=(
                        run.status if isinstance(run, SyncRun) else "error"
                    ),
                ).inc()
            except Exception:  # pragma: no cover - defensive
                pass

            return run

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _persist_sync_run(self, run: SyncRun) -> None:
        """Index the SyncRun into :data:`INTEGRATION_SYNC_RUNS_INDEX`.

        Failures here are logged but never raised — the scheduler's
        job loop MUST NOT be torn down by an ES outage.
        """

        try:
            payload = run.model_dump(mode="json")
            await self._es.index_document(
                INTEGRATION_SYNC_RUNS_INDEX, run.run_id, payload
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "IntegrationScheduler: failed to persist SyncRun %s: %s",
                run.run_id,
                exc,
            )

    async def _mark_instance_success(
        self,
        instance: IntegrationInstance,
        *,
        finished_at: datetime,
    ) -> None:
        """Reset retry_count / last_error on the instance after a good run."""

        patch: Dict[str, Any] = {
            "status": "connected",
            "last_sync_at": finished_at.isoformat(),
            "last_error": None,
            "retry_count": 0,
        }
        try:
            await self._repository.update(
                instance.tenant_id, instance.instance_id, patch
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntegrationScheduler: failed to mark instance=%s connected: %s",
                instance.instance_id,
                exc,
            )

    async def _mark_instance_error(
        self,
        instance: IntegrationInstance,
        *,
        last_error: str,
        retry_count: int,
        raise_signal: bool,
    ) -> None:
        """Flip the instance to ``status="error"`` and (optionally) raise an alert."""

        patch: Dict[str, Any] = {
            "status": "error",
            "last_error": last_error,
            "retry_count": retry_count,
        }
        try:
            await self._repository.update(
                instance.tenant_id, instance.instance_id, patch
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "IntegrationScheduler: failed to mark instance=%s errored: %s",
                instance.instance_id,
                exc,
            )

        if raise_signal:
            await self._publish_exhaustion_signal(
                instance, last_error=last_error, retry_count=retry_count
            )

    async def _publish_exhaustion_signal(
        self,
        instance: IntegrationInstance,
        *,
        last_error: str,
        retry_count: int,
    ) -> None:
        """Publish the :class:`RiskSignal` that Req 5.1.6 mandates on exhaustion."""

        if self._signal_bus is None:
            logger.info(
                "IntegrationScheduler: skipping exhaustion alert for "
                "instance=%s (no SignalBus wired)",
                instance.instance_id,
            )
            return

        try:
            signal = RiskSignal(
                source_agent=INTEGRATION_SCHEDULER_AGENT_ID,
                entity_id=instance.instance_id,
                entity_type=INTEGRATION_INSTANCE_ENTITY_TYPE,
                severity=Severity.HIGH,
                confidence=DEFAULT_ALERT_CONFIDENCE,
                ttl_seconds=self._alert_ttl_seconds,
                tenant_id=instance.tenant_id,
                context={
                    "signal_type": INTEGRATION_SYNC_FAILED_SIGNAL_TYPE,
                    "provider_name": instance.provider_name,
                    "category": instance.category,
                    "operation": self._operation,
                    "retry_count": retry_count,
                    "max_retries": self._max_retries,
                    "last_error": last_error,
                },
            )
            await self._signal_bus.publish(signal)
        except Exception as exc:
            logger.error(
                "IntegrationScheduler: failed to publish RiskSignal for "
                "instance=%s: %s",
                instance.instance_id,
                exc,
            )


__all__ = [
    "DEFAULT_ALERT_CONFIDENCE",
    "DEFAULT_ALERT_TTL_SECONDS",
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_INITIAL_BACKOFF_SECONDS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "INTEGRATION_INSTANCE_ENTITY_TYPE",
    "INTEGRATION_SCHEDULER_AGENT_ID",
    "INTEGRATION_SYNC_FAILED_SIGNAL_TYPE",
    "IntegrationScheduler",
]
