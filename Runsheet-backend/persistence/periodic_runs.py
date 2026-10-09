"""Persisted last-run times for long-interval background jobs.

A daily job that keeps its schedule in process memory restarts its clock on
every deploy. With leader election that cuts both ways:

* A follower that sleeps a whole interval before its first run never runs on
  an environment redeployed more often than daily (staging, 2026-10-07).
* A new leader that runs as soon as it takes over runs the job a second time
  inside the interval, because the previous leader's run is not visible to it.

So the start of each run is written here, keyed by job name, and the next
leader schedules from it. Only the sweep leader reads or writes the ledger, and
the leader lock already serializes leaders, so there is no compare-and-set: the
previous leader holds the lock until its process exits, and it records a run
when the run starts, so its successor always sees it.

Short-interval jobs (under :data:`PERSISTED_SCHEDULE_MIN_INTERVAL_SECONDS`)
don't use the ledger. Running one of them early after a leader change is
harmless, and writing a row every 30 s per agent is not worth it.

Semantics: **at most once per interval.** The run is claimed (written here)
before the job body starts, so a cycle that is interrupted (SIGTERM during a
deploy, a crash, or the agent stopping between the claim and the cycle) counts
as done and the next run is a full interval later. That is the deliberate side
of the trade-off: the daily sweeps this serves (asset-cert expiry, contract
expiry) send alerts, and a skipped day is recoverable where a duplicate burst
of critical alerts is not. Two edges follow from scheduling off the stored
time: a ``last_run_at`` in the future (clock skew, a hand-edited row) delays
the job until that time plus the interval, and a ledger read or write that
fails open falls back to the in-process schedule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Protocol

logger = logging.getLogger(__name__)

#: Jobs with an interval at least this long schedule from the ledger.
PERSISTED_SCHEDULE_MIN_INTERVAL_SECONDS = 3600.0

#: Document-store index holding one row per job.
PERIODIC_RUNS_INDEX = "periodic_job_runs"


class RunLedger(Protocol):
    async def last_run(self, job: str) -> Optional[datetime]: ...

    async def record_run(self, job: str, at: datetime, *, seeded: bool = False) -> None: ...


class InMemoryRunLedger:
    """A ledger shared only within one process. Used by tests to stand in for
    the shared store across two simulated processes."""

    def __init__(self) -> None:
        self.runs: Dict[str, datetime] = {}

    async def last_run(self, job: str) -> Optional[datetime]:
        return self.runs.get(job)

    async def record_run(self, job: str, at: datetime, *, seeded: bool = False) -> None:
        self.runs[job] = at


class DocumentStoreRunLedger:
    """Ledger over the Postgres document store (``es_documents``)."""

    def __init__(self, store=None) -> None:
        if store is None:
            from persistence.document_store import PostgresDocumentStore

            store = PostgresDocumentStore()
        self._store = store

    async def last_run(self, job: str) -> Optional[datetime]:
        doc = await self._store.get_document(PERIODIC_RUNS_INDEX, job)
        if not doc or not doc.get("last_run_at"):
            return None
        at = datetime.fromisoformat(str(doc["last_run_at"]))
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    async def record_run(self, job: str, at: datetime, *, seeded: bool = False) -> None:
        await self._store.index_document(
            PERIODIC_RUNS_INDEX,
            job,
            {"job": job, "last_run_at": at.isoformat(), "seeded": seeded},
        )


_ledger: Optional[RunLedger] = None
_ledger_resolved = False


def set_run_ledger(ledger: Optional[RunLedger]) -> None:
    """Install a ledger (tests). ``None`` re-enables the default lookup."""
    global _ledger, _ledger_resolved
    _ledger = ledger
    _ledger_resolved = ledger is not None


def get_run_ledger() -> Optional[RunLedger]:
    """The process ledger: the document store when Postgres is configured,
    otherwise ``None`` (one process, so in-memory scheduling is enough)."""
    global _ledger, _ledger_resolved
    if not _ledger_resolved:
        from persistence.database import is_persistence_enabled

        _ledger = DocumentStoreRunLedger() if is_persistence_enabled() else None
        _ledger_resolved = True
    return _ledger


async def read_last_run(job: str) -> Optional[datetime]:
    """Persisted last run, or ``None`` when unknown or unreadable.

    A read failure fails open (the job may run) with a warning: a missed
    compliance sweep is worse than a repeated one, and the cycle itself
    reads the same database, so it would most likely fail too.
    """
    ledger = get_run_ledger()
    if ledger is None:
        return None
    try:
        return await ledger.last_run(job)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the last run of %r; scheduling from memory", job,
                       exc_info=True)
        return None


async def write_last_run(job: str, at: datetime, *, seeded: bool = False) -> None:
    ledger = get_run_ledger()
    if ledger is None:
        return
    try:
        await ledger.record_run(job, at, seeded=seeded)
    except Exception:  # noqa: BLE001
        logger.warning("Could not record the run of %r", job, exc_info=True)


__all__ = [
    "PERIODIC_RUNS_INDEX",
    "PERSISTED_SCHEDULE_MIN_INTERVAL_SECONDS",
    "DocumentStoreRunLedger",
    "InMemoryRunLedger",
    "RunLedger",
    "get_run_ledger",
    "read_last_run",
    "set_run_ledger",
    "write_last_run",
]
