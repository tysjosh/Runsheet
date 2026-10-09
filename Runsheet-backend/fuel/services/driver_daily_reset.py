"""
Daily reset of ``completed_today`` counters for all drivers.

Registers a background task that fires at 00:00 in each tenant's
configured timezone (falling back to ``America/Chicago`` when unset).
Failures log ``logger.exception`` and increment
``fuelops_driver_daily_reset_errors_total{tenant_id}``.

Validates: Requirement 3.2.4.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from services.time_utils import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "DriverDailyResetJob",
    "run_daily_reset_cycle",
    "RESET_CHECK_INTERVAL_SECONDS",
    "get_tenant_timezone",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How often the background loop checks whether any tenant has crossed
#: midnight. 60 seconds is frequent enough to catch the boundary within
#: a minute of the actual midnight.
RESET_CHECK_INTERVAL_SECONDS: int = 60

#: Default timezone when a tenant has no configured timezone.
DEFAULT_TIMEZONE: str = "America/Chicago"

#: Prometheus metric name for reset errors.
METRIC_RESET_ERRORS = "fuelops_driver_daily_reset_errors_total"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tenant_timezone(tenant_id: str, tenant_settings: Optional[Any] = None) -> str:
    """Return the IANA timezone for a tenant, defaulting to America/Chicago."""
    if tenant_settings is not None:
        tz = getattr(tenant_settings, "timezone", None)
        if tz and isinstance(tz, str):
            return tz
    return DEFAULT_TIMEZONE


#: Public name for the tenant time-zone rule, used by the Dispatch Board
#: (dispatch-board K2.5) so both surfaces resolve the same zone.
get_tenant_timezone = _get_tenant_timezone


#: Ledger key prefix (``persistence.periodic_runs``) holding each tenant's
#: last reset time, so a restart does not count as a new day.
LEDGER_KEY_PREFIX = "driver.daily-reset:"


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (KeyError, Exception):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _local_date(at: datetime, tz_name: str) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(_zone(tz_name)).strftime("%Y-%m-%d")


def _needs_reset(tz_name: str, last_reset_at: datetime, now: datetime) -> bool:
    """True when ``now`` falls on a later tenant-local date than the last
    reset (i.e. tenant-local midnight has passed since then)."""
    return _local_date(now, tz_name) > _local_date(last_reset_at, tz_name)


# ---------------------------------------------------------------------------
# Job class
# ---------------------------------------------------------------------------


class DriverDailyResetJob:
    """Manages the daily reset of ``completed_today`` for all tenants.

    The job discovers all distinct tenant_ids from the ``drivers_current``
    index, checks each tenant's timezone to see if midnight has passed,
    and resets counters for those that have crossed into a new day.

    State:
        The last reset time per tenant is persisted in the
        ``periodic_job_runs`` ledger under ``driver.daily-reset:<tenant>``
        and cached in ``_last_reset_at``. The first time a tenant is seen
        with no record, the job seeds the record and does NOT reset: a
        process restart is not midnight (staging reset counters on every
        deploy before this). Without a ledger (single process, no
        Postgres) the same rule applies to the in-memory cache only.
    """

    def __init__(
        self,
        *,
        es_service: Any,
        driver_repository: Any,
        tenant_settings_service: Optional[Any] = None,
        metrics_registry: Optional[Any] = None,
    ) -> None:
        self._es = es_service
        self._driver_repo = driver_repository
        self._tenant_settings_service = tenant_settings_service
        self._metrics_registry = metrics_registry
        self._last_reset_at: Dict[str, datetime] = {}

    async def discover_tenant_ids(self) -> List[str]:
        """Discover all distinct tenant_ids from drivers_current."""
        from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX

        try:
            resp = await self._es.search_documents(
                DRIVERS_CURRENT_INDEX,
                {
                    "size": 0,
                    "aggs": {
                        "tenant_ids": {
                            "terms": {"field": "tenant_id", "size": 10_000}
                        }
                    },
                },
                0,
            )
            aggs = (resp or {}).get("aggregations") or {}
            buckets = (aggs.get("tenant_ids") or {}).get("buckets") or []
            return [b["key"] for b in buckets if b.get("key")]
        except Exception as exc:
            logger.warning(
                "DriverDailyResetJob: failed to discover tenant_ids: %s", exc
            )
            return []

    async def _get_tenant_settings(self, tenant_id: str) -> Optional[Any]:
        """Fetch tenant settings if the service is available."""
        if self._tenant_settings_service is None:
            return None
        try:
            return await self._tenant_settings_service.get(tenant_id)
        except Exception:
            return None

    async def reset_for_tenant(self, tenant_id: str) -> None:
        """Reset completed_today for a single tenant."""
        try:
            updated = await self._driver_repo.reset_completed_today(tenant_id)
            logger.info(
                "DriverDailyResetJob: reset %d drivers for tenant=%s",
                updated,
                tenant_id,
            )
        except Exception as exc:
            logger.exception(
                "DriverDailyResetJob: reset failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            self._increment_error_metric(tenant_id)
            raise

    def _increment_error_metric(self, tenant_id: str) -> None:
        """Increment the fuelops_driver_daily_reset_errors_total metric."""
        try:
            from fuel.services.order_intake_metrics import (
                fuelops_driver_daily_reset_errors_total,
            )

            fuelops_driver_daily_reset_errors_total.labels(
                tenant_id=tenant_id
            ).inc()
        except Exception:
            pass  # Metrics failures must not propagate

    async def _read_persisted(self, tenant_id: str) -> tuple[bool, Optional[datetime]]:
        """Return ``(ok, last_reset_at)`` from the ledger.

        ``ok`` is False when the ledger read failed: the caller skips the
        tenant this cycle rather than seeding over a record it could not see.
        Without a ledger, returns ``(True, None)``.
        """
        from persistence.periodic_runs import get_run_ledger

        ledger = get_run_ledger()
        if ledger is None:
            return True, None
        try:
            return True, await ledger.last_run(f"{LEDGER_KEY_PREFIX}{tenant_id}")
        except Exception:  # noqa: BLE001
            logger.warning(
                "DriverDailyResetJob: could not read last reset for tenant=%s",
                tenant_id,
                exc_info=True,
            )
            return False, None

    async def _record(self, tenant_id: str, at: datetime, *, seeded: bool) -> None:
        from persistence.periodic_runs import write_last_run

        self._last_reset_at[tenant_id] = at
        await write_last_run(f"{LEDGER_KEY_PREFIX}{tenant_id}", at, seeded=seeded)

    async def run_cycle(self, now: Optional[datetime] = None) -> None:
        """Run one check cycle: discover tenants, check midnight, reset.

        A tenant resets only when its tenant-local date has moved past the
        date of its last recorded reset. The first sighting of a tenant
        (no record anywhere) seeds the record without resetting.
        """
        now = now or utcnow()
        tenant_ids = await self.discover_tenant_ids()

        for tenant_id in tenant_ids:
            settings = await self._get_tenant_settings(tenant_id)
            tz_name = _get_tenant_timezone(tenant_id, settings)

            last = self._last_reset_at.get(tenant_id)
            if last is not None and not _needs_reset(tz_name, last, now):
                continue  # cached: already reset today, no DB round trip

            # First sighting, or the cache says a new day: confirm against
            # the ledger (another leader may already have reset today).
            ok, persisted = await self._read_persisted(tenant_id)
            if not ok:
                continue
            known = [t for t in (persisted, last) if t is not None]
            if not known:
                await self._record(tenant_id, now, seeded=True)
                logger.info(
                    "DriverDailyResetJob: no reset recorded for tenant=%s; "
                    "seeded baseline, not resetting",
                    tenant_id,
                )
                continue
            latest = max(known)
            if not _needs_reset(tz_name, latest, now):
                self._last_reset_at[tenant_id] = latest
                continue

            try:
                await self.reset_for_tenant(tenant_id)
            except Exception:
                continue  # Already logged in reset_for_tenant
            await self._record(tenant_id, now, seeded=False)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_daily_reset_cycle(job: DriverDailyResetJob) -> None:
    """Execute one cycle of the daily reset job."""
    await job.run_cycle()
