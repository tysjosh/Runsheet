"""
``DriverRetentionJob`` — one ``delete_by_query`` per driver data class, per day.

R10.16 requires driver-data retention to be expressed as **one declared period
per data class** rather than one period covering every driver store, and R10.20
requires the data class to be named in every retention log record so a run
against one class is distinguishable from a run against another. Both live in
:data:`RETENTION_CLASSES` below, which is the single table this module executes:

===================== ======================= =========== =====================
Data class            Index                   Period      Anchor
===================== ======================= =========== =====================
``duty_status_event`` ``duty_status_events``  36 months   ``event_timestamp``
``breadcrumb_sample`` ``driver_breadcrumbs``  90 days     ``sample_timestamp``
``driver_presence``   ``driver_presence``     none        —
``inspection_report`` ``vehicle_inspections`` 15 months   ``inspection_timestamp``
``idempotency_key``   ``idempotency_keys``    24 hours    ``created_at``
===================== ======================= =========== =====================

Notes on the rows, because each one encodes a decision:

* ``driver_presence`` is **enumerated with no period** rather than omitted. The
  Driver_WS_Manager keeps exactly one current record per
  ``(tenant_id, driver_id)`` with no history (R10.19), so there is nothing to
  expire. It is listed so its absence from the sweep is a stated decision, and
  it still emits its own log record — a reader of the logs sees all five classes
  accounted for, and :meth:`DriverRetentionJob.run_cycle` issues no query for
  it.
* ``breadcrumb_sample`` filters on **``sample_timestamp``**, the client's stamp
  for when the fix was taken, not on the ``server_received_at`` stamp
  ``DriverTelemetryService`` writes beside it. R10.17 declares the period as 90
  days from ``sample_timestamp``, and the two stamps genuinely differ: an
  offline queue drain submits fixes up to
  ``telemetry_service.MAX_SAMPLE_AGE_HOURS`` old, so a sweep anchored on receipt
  would keep those samples alive past the declared window. Now that
  ``driver_breadcrumbs`` carries documents, this row deletes for real; it was a
  no-op sweep over an empty index until telemetry ingestion shipped.
* ``inspection_report`` filters on **``inspection_timestamp``**, not on the
  ``expires_at`` stamp ``InspectionService`` writes at ``inspection_timestamp``
  + 15 months (R8.9). The two agree by construction, but a range filter never
  matches a document that lacks the field, so anchoring the sweep on
  ``expires_at`` would silently leave behind every report indexed before that
  stamp existed. Anchoring on the client's ``inspection_timestamp`` also keeps
  the declared period in this table rather than splitting it across a writer
  that stamps and a sweep that trusts the stamp.
* ``duty_status_event`` at 36 months (R10.18) is the retention rule R13.13 names
  as the one exception to the append-only event log: the retention job is the
  only writer permitted to delete an event document.
* Month-based periods are calendar months, clamped to the target month's length,
  because 36 and 15 months are not fixed numbers of days.

Cadence and failure posture (R10.13): each class runs at least once every 24
hours. A class that fails logs and the cycle continues to the next class, and a
cycle that fails logs inside the scheduling loop so the task never dies — a
retention sweep is never worth taking the process down for.

Every sweep is cluster-wide, which is why each record carries
``tenant_scope=all``: the periods are platform policy, identical in every
tenant, so there is deliberately no per-tenant fan-out and no tenant filter.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Retention jobs and
§Bootstrap Wiring 6.

Validates: Requirements 10.13, 10.16, 10.17, 10.18, 10.20
- 10.13: one scheduled sweep per data class at least once every 24 hours
- 10.16: one declared period per data class, not one period for every store
- 10.17: breadcrumb samples at 90 days from ``sample_timestamp``
- 10.18: Duty_Status_Event_Log at 36 months from ``event_timestamp``
- 10.20: every retention log record names its data class
"""

from __future__ import annotations

import asyncio
import calendar
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from driver.services.driver_es_mappings import (
    DRIVER_BREADCRUMBS_INDEX,
    DRIVER_PRESENCE_INDEX,
    DUTY_STATUS_EVENTS_INDEX,
    IDEMPOTENCY_KEYS_INDEX,
    VEHICLE_INSPECTIONS_INDEX,
)

logger = logging.getLogger(__name__)

#: How often the scheduling loop runs one full cycle. R10.13 says "at least
#: once every 24 hours"; the loop sleeps first and then sweeps, matching the
#: ``DriverDailyResetJob`` loop shape.
RETENTION_INTERVAL_SECONDS: int = 24 * 60 * 60

#: The log prefix every record shares. The ``data_class`` token that follows it
#: is the R10.20 discriminator.
RETENTION_LOG_EVENT: str = "retention_run"

#: Sweeps are cluster-wide — the periods are platform policy, not tenant
#: policy — and every record says so rather than leaving it to be inferred.
TENANT_SCOPE: str = "all"


def _plus_months(moment: datetime, months: int) -> datetime:
    """Return ``moment`` shifted by ``months`` calendar months.

    ``months`` may be negative, which is how the cutoffs below are computed. The
    day of month is clamped to the target month's length, so a cutoff taken on
    31 March lands on 30 June rather than rolling into July. This mirrors
    ``driver.services.inspection_service._plus_months`` deliberately rather than
    importing it: that module owns the *stamp* and this one owns the *sweep*, and
    a private helper is not a seam between them.
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class RetentionClassSpec:
    """One row of the retention table: a data class and its declared period.

    Exactly one of ``period_months`` / ``period_days`` / ``period_hours`` is set,
    or none of them for a class with no retention period. The unit is kept as
    declared rather than normalised to days, because that is what the log record
    reports and because 36 months is not a fixed number of days.
    """

    data_class: str
    index: str
    anchor_field: Optional[str] = None
    period_months: Optional[int] = None
    period_days: Optional[int] = None
    period_hours: Optional[int] = None

    @property
    def has_retention_period(self) -> bool:
        """``True`` when this class expires; ``False`` for ``driver_presence``."""
        return (
            self.anchor_field is not None
            and (
                self.period_months is not None
                or self.period_days is not None
                or self.period_hours is not None
            )
        )

    @property
    def period_token(self) -> str:
        """The ``period_*`` token for the log record, in the declared unit."""
        if self.period_months is not None:
            return f"period_months={self.period_months}"
        if self.period_days is not None:
            return f"period_days={self.period_days}"
        if self.period_hours is not None:
            return f"period_hours={self.period_hours}"
        return "period=none"

    def cutoff(self, now: datetime) -> Optional[datetime]:
        """Return the cutoff instant for a sweep started at ``now``.

        Documents whose anchor field is strictly older than this instant are
        deleted. ``None`` for a class with no retention period.
        """
        if self.period_months is not None:
            return _plus_months(now, -self.period_months)
        if self.period_days is not None:
            return now - timedelta(days=self.period_days)
        if self.period_hours is not None:
            return now - timedelta(hours=self.period_hours)
        return None


#: The R10.16 table, in one place, executed by :class:`DriverRetentionJob`.
RETENTION_CLASSES: Tuple[RetentionClassSpec, ...] = (
    RetentionClassSpec(
        data_class="duty_status_event",
        index=DUTY_STATUS_EVENTS_INDEX,
        anchor_field="event_timestamp",
        period_months=36,
    ),
    RetentionClassSpec(
        data_class="breadcrumb_sample",
        index=DRIVER_BREADCRUMBS_INDEX,
        anchor_field="sample_timestamp",
        period_days=90,
    ),
    # No period, no anchor, no query — one current record per driver (R10.19).
    RetentionClassSpec(
        data_class="driver_presence",
        index=DRIVER_PRESENCE_INDEX,
    ),
    RetentionClassSpec(
        data_class="inspection_report",
        index=VEHICLE_INSPECTIONS_INDEX,
        anchor_field="inspection_timestamp",
        period_months=15,
    ),
    RetentionClassSpec(
        data_class="idempotency_key",
        index=IDEMPOTENCY_KEYS_INDEX,
        anchor_field="created_at",
        period_hours=24,
    ),
)


def _format_cutoff(cutoff: datetime) -> str:
    """Render a cutoff as the ``…Z`` UTC form the log record carries."""
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return (
        cutoff.astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S")
        + "Z"
    )


class DriverRetentionJob:
    """Runs one ``delete_by_query`` per data class and logs one record each.

    ``es_service`` is the shared :class:`ElasticsearchService`. The sweep goes
    through its underlying client because the service exposes no
    ``delete_by_query`` of its own — the same route
    ``ops/services/feature_flags.py`` and ``services/data_seeder.py`` take. The
    call is handed to a worker thread, because the client is synchronous and a
    retention sweep over months of data is exactly the kind of call that must
    not sit on the event loop.
    """

    def __init__(self, *, es_service: Any) -> None:
        self._es = es_service

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    async def run_cycle(
        self, now: Optional[datetime] = None
    ) -> Dict[str, Optional[int]]:
        """Sweep every data class once and return ``{data_class: deleted}``.

        ``now`` is injectable so the cutoff arithmetic is testable; it defaults
        to the job start time. One entry per class: the deleted count, ``0`` for
        a class with no retention period, and ``None`` for a class whose sweep
        failed. A failing class never stops the classes after it — retention on
        one index is independent of retention on another.
        """
        started_at = now or datetime.now(timezone.utc)
        results: Dict[str, Optional[int]] = {}

        for spec in RETENTION_CLASSES:
            try:
                results[spec.data_class] = await self.run_class(spec, started_at)
            except Exception as exc:
                results[spec.data_class] = None
                logger.exception(
                    "%s data_class=%s %s outcome=failed error=%s",
                    RETENTION_LOG_EVENT,
                    spec.data_class,
                    spec.period_token,
                    exc,
                )

        return results

    async def run_class(self, spec: RetentionClassSpec, now: datetime) -> int:
        """Sweep one data class and emit its single log record.

        Returns the number of documents deleted. A class with no declared period
        issues no query and returns ``0`` — it is logged all the same, so the
        record set names every class in the table (R10.20).
        """
        if not spec.has_retention_period:
            logger.info(
                "%s data_class=%s %s cutoff=none deleted=0 tenant_scope=%s",
                RETENTION_LOG_EVENT,
                spec.data_class,
                spec.period_token,
                TENANT_SCOPE,
            )
            return 0

        cutoff = spec.cutoff(now)
        assert cutoff is not None  # has_retention_period guarantees this
        cutoff_text = _format_cutoff(cutoff)

        deleted = await self._delete_older_than(
            index=spec.index,
            anchor_field=str(spec.anchor_field),
            cutoff_text=cutoff_text,
        )

        logger.info(
            "%s data_class=%s %s cutoff=%s deleted=%d tenant_scope=%s",
            RETENTION_LOG_EVENT,
            spec.data_class,
            spec.period_token,
            cutoff_text,
            deleted,
            TENANT_SCOPE,
        )
        return deleted

    # ------------------------------------------------------------------
    # The one Elasticsearch call
    # ------------------------------------------------------------------

    async def _delete_older_than(
        self, *, index: str, anchor_field: str, cutoff_text: str
    ) -> int:
        """Run one ``delete_by_query`` and return the deleted count.

        ``conflicts=proceed`` so a document rewritten mid-sweep is skipped
        rather than aborting the whole class, and ``ignore_unavailable`` so a
        deployment missing an optional index gets a zero-delete sweep instead of
        an error rather than failing the class for the whole cluster.
        """
        client = getattr(self._es, "client", None)
        if client is None:
            raise RuntimeError(
                "Elasticsearch client unavailable for the retention sweep"
            )

        body = {"query": {"range": {anchor_field: {"lt": cutoff_text}}}}

        def _call() -> Any:
            return client.delete_by_query(
                index=index,
                body=body,
                conflicts="proceed",
                ignore_unavailable=True,
                refresh=False,
            )

        response = await asyncio.to_thread(_call)
        # A test double or a future async client may hand back an awaitable.
        if inspect.isawaitable(response):
            response = await response

        if response is None:
            return 0
        try:
            return int(response.get("deleted") or 0)
        except (AttributeError, TypeError, ValueError):
            return 0


async def run_retention_cycle(job: DriverRetentionJob) -> None:
    """Execute one cycle of the retention job.

    The seam ``bootstrap/driver.py``'s loop calls, mirroring
    ``run_daily_reset_cycle`` for ``DriverDailyResetJob``.
    """
    await job.run_cycle()


__all__ = [
    "DriverRetentionJob",
    "RetentionClassSpec",
    "RETENTION_CLASSES",
    "RETENTION_INTERVAL_SECONDS",
    "RETENTION_LOG_EVENT",
    "TENANT_SCOPE",
    "run_retention_cycle",
]
