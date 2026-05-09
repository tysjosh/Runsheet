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


def _is_midnight_window(tz_name: str, last_reset_date: Optional[str]) -> bool:
    """Return True if the current local date in ``tz_name`` is past midnight
    and we haven't already reset for today."""
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, Exception):
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    now_local = datetime.now(tz)
    today_str = now_local.strftime("%Y-%m-%d")

    # If we already reset for today, skip
    if last_reset_date == today_str:
        return False

    # We're in a new day — time to reset
    return True


# ---------------------------------------------------------------------------
# Job class
# ---------------------------------------------------------------------------


class DriverDailyResetJob:
    """Manages the daily reset of ``completed_today`` for all tenants.

    The job discovers all distinct tenant_ids from the ``drivers_current``
    index, checks each tenant's timezone to see if midnight has passed,
    and resets counters for those that have crossed into a new day.

    State:
        _last_reset_dates: Dict[tenant_id, date_str] tracking when each
            tenant was last reset to avoid double-resets.
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
        self._last_reset_dates: Dict[str, str] = {}

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

    async def run_cycle(self) -> None:
        """Run one check cycle: discover tenants, check midnight, reset."""
        tenant_ids = await self.discover_tenant_ids()

        for tenant_id in tenant_ids:
            settings = await self._get_tenant_settings(tenant_id)
            tz_name = _get_tenant_timezone(tenant_id, settings)
            last_reset = self._last_reset_dates.get(tenant_id)

            if _is_midnight_window(tz_name, last_reset):
                try:
                    await self.reset_for_tenant(tenant_id)
                    # Mark as reset for today in this timezone
                    try:
                        tz = ZoneInfo(tz_name)
                    except (KeyError, Exception):
                        tz = ZoneInfo(DEFAULT_TIMEZONE)
                    today_str = datetime.now(tz).strftime("%Y-%m-%d")
                    self._last_reset_dates[tenant_id] = today_str
                except Exception:
                    # Already logged in reset_for_tenant
                    pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_daily_reset_cycle(job: DriverDailyResetJob) -> None:
    """Execute one cycle of the daily reset job."""
    await job.run_cycle()
