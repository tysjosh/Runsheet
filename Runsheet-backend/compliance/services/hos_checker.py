"""HOS Checker — Hours-of-Service eligibility for route planning.

Implements the ``HOS_Checker`` described in design §4 of the Fuel Compliance
Backbone spec. This service retrieves driver HOS telemetry from Geotab,
caches it in Redis, and evaluates whether a driver has sufficient remaining
hours to complete a proposed route without violating FMCSA regulations:

* 11-hour drive limit (with 30-minute buffer)
* 14-hour on-duty window limit
* 60-hour/7-day or 70-hour/8-day cycle limit

Public methods:
* ``is_eligible()`` — checks all HOS rules for a driver/route combination.
* ``refresh_hos_data()`` — pulls fresh HOS data from Geotab and caches it.
* ``get_hos_status()`` — returns cached HOS data or refreshes on cache miss.

Validates: Requirement 4.1
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (FMCSA HOS Limits)
# ---------------------------------------------------------------------------

# Maximum consecutive driving hours before mandatory rest (Req 4.2)
DRIVE_LIMIT_HOURS: float = 11.0

# Safety buffer subtracted from available drive hours (Req 4.2)
BUFFER_HOURS: float = 0.5

# Maximum on-duty window from start of shift (Req 4.3)
WINDOW_LIMIT_HOURS: float = 14.0

# Cumulative cycle limits (Req 4.4)
CYCLE_7_DAY_LIMIT: float = 60.0
CYCLE_8_DAY_LIMIT: float = 70.0

# Redis cache TTL for HOS data in seconds (15 minutes) (Req 4.6)
CACHE_TTL_SECONDS: int = 900

# Redis key pattern for HOS data caching
_CACHE_KEY_PATTERN: str = "hos:{tenant_id}:{driver_id}"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class HOSStatus(BaseModel):
    """Current HOS status for a driver as retrieved from Geotab telemetry.

    Validates: Requirement 4.1
    """

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    available_drive_hours: float
    available_window_hours: float
    cumulative_cycle_hours: float
    cycle_type: str  # "7_day" | "8_day"
    last_updated: datetime
    source: str = "geotab"


class HOSEligibility(BaseModel):
    """Result of an HOS eligibility check for a driver/route combination.

    Validates: Requirements 4.2, 4.3, 4.4, 4.5
    """

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    eligible: bool
    reasons: List[str] = Field(default_factory=list)
    earliest_eligible_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_cache_key(tenant_id: str, driver_id: str) -> str:
    """Build the Redis cache key for a driver's HOS data."""
    return _CACHE_KEY_PATTERN.format(tenant_id=tenant_id, driver_id=driver_id)


def _parse_geotab_hos_response(
    driver_id: str, raw_data: Mapping[str, Any]
) -> HOSStatus:
    """Parse a Geotab HOS telemetry response into an HOSStatus model.

    The Geotab DutyStatusLog API returns fields like availableDriveTime,
    availableOnDutyTime, and cumulativeCycleHours. This function normalizes
    the response into our internal HOSStatus model.

    Args:
        driver_id: The driver identifier.
        raw_data: Raw response from the Geotab connector.

    Returns:
        Parsed HOSStatus instance.
    """
    available_drive = float(
        raw_data.get("availableDriveHours")
        or raw_data.get("available_drive_hours")
        or 0.0
    )
    available_window = float(
        raw_data.get("availableWindowHours")
        or raw_data.get("available_window_hours")
        or 0.0
    )
    cumulative_cycle = float(
        raw_data.get("cumulativeCycleHours")
        or raw_data.get("cumulative_cycle_hours")
        or 0.0
    )
    cycle_type = str(
        raw_data.get("cycleType")
        or raw_data.get("cycle_type")
        or "7_day"
    )
    # Normalize cycle_type to our internal format
    if cycle_type in ("8_day", "8day", "8-day"):
        cycle_type = "8_day"
    else:
        cycle_type = "7_day"

    return HOSStatus(
        driver_id=driver_id,
        available_drive_hours=available_drive,
        available_window_hours=available_window,
        cumulative_cycle_hours=cumulative_cycle,
        cycle_type=cycle_type,
        last_updated=utcnow(),
        source="geotab",
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class HOSChecker:
    """Service for checking driver Hours-of-Service compliance.

    Retrieves HOS telemetry from Geotab via the GeotabConnector, caches
    results in Redis with a 15-minute TTL, and evaluates driver eligibility
    against FMCSA HOS rules before route assignment.

    Args:
        es_service: Elasticsearch handle for audit logging.
        redis_client: Redis client for HOS data caching.
        geotab_connector: Connector for retrieving Geotab HOS telemetry.
        tenant_id: The tenant identifier for cache key scoping.

    Validates: Requirement 4.1
    """

    def __init__(
        self, es_service, redis_client, geotab_connector, tenant_id: str = ""
    ) -> None:
        self._es = es_service
        self._redis = redis_client
        self._geotab = geotab_connector
        self._tenant_id = tenant_id

    async def get_hos_status(
        self, driver_id: str, tenant_id: Optional[str] = None
    ) -> HOSStatus:
        """Return the driver's HOS status, using cache when available.

        First checks Redis for cached data. On cache miss, calls
        ``refresh_hos_data()`` to pull fresh data from Geotab.

        Args:
            driver_id: The driver whose HOS status to retrieve.
            tenant_id: Optional tenant override (uses instance default if None).

        Returns:
            HOSStatus with the driver's current hours availability.

        Validates: Requirements 4.1, 4.6
        """
        effective_tenant = tenant_id or self._tenant_id
        cached = await self._get_cached_hos_data(effective_tenant, driver_id)
        if cached is not None:
            return cached
        return await self.refresh_hos_data(driver_id, tenant_id=effective_tenant)

    async def is_eligible(
        self,
        driver_id: str,
        estimated_drive_hours: float,
        estimated_total_hours: float,
    ) -> HOSEligibility:
        """Check whether a driver has sufficient HOS hours for a route.

        Evaluates the driver's current HOS status against FMCSA limits:
        1. 11-hour drive limit minus 30-minute buffer (Req 4.2)
        2. 14-hour on-duty window (Req 4.3)
        3. 60/70-hour cycle limit (Req 4.4)

        If the driver is ineligible, returns reasons and optionally the
        earliest time they become eligible (Req 4.5).

        Args:
            driver_id: The driver to evaluate.
            estimated_drive_hours: Estimated driving time for the route.
            estimated_total_hours: Estimated total on-duty time for the route.

        Returns:
            HOSEligibility with eligible=True/False, reasons, and
            earliest_eligible_time if applicable.

        Validates: Requirements 4.2, 4.3, 4.4, 4.5
        """
        # Retrieve HOS data with graceful degradation
        try:
            hos_status = await self.get_hos_status(driver_id)
        except Exception as exc:
            logger.warning(
                "HOSChecker.is_eligible: failed to retrieve HOS data for "
                "driver=%s, defaulting to eligible (graceful degradation): %s",
                driver_id,
                exc,
            )
            return HOSEligibility(
                driver_id=driver_id,
                eligible=True,
                reasons=["HOS data unavailable — defaulting to eligible"],
            )

        reasons: List[str] = []

        # Check 1: 11-hour drive limit with 30-minute buffer (Req 4.2)
        required_drive_hours = estimated_drive_hours + BUFFER_HOURS
        if hos_status.available_drive_hours < required_drive_hours:
            reasons.append(
                f"Insufficient drive hours: {hos_status.available_drive_hours:.1f}h "
                f"available, need {required_drive_hours:.1f}h "
                f"(including {BUFFER_HOURS:.1f}h buffer)"
            )

        # Check 2: 14-hour on-duty window remaining (Req 4.3)
        if hos_status.available_window_hours < estimated_total_hours:
            reasons.append(
                f"Insufficient on-duty window: {hos_status.available_window_hours:.1f}h "
                f"available, need {estimated_total_hours:.1f}h total route duration"
            )

        # Check 3: 60/70-hour cycle limit (Req 4.4)
        cycle_limit = (
            CYCLE_7_DAY_LIMIT
            if hos_status.cycle_type == "7_day"
            else CYCLE_8_DAY_LIMIT
        )
        projected_cycle_hours = hos_status.cumulative_cycle_hours + estimated_total_hours
        if projected_cycle_hours > cycle_limit:
            excess = projected_cycle_hours - cycle_limit
            reasons.append(
                f"Cycle limit exceeded: {hos_status.cumulative_cycle_hours:.1f}h cumulative + "
                f"{estimated_total_hours:.1f}h route = {projected_cycle_hours:.1f}h, "
                f"exceeds {cycle_limit:.0f}h {hos_status.cycle_type} limit by {excess:.1f}h"
            )

        eligible = len(reasons) == 0

        # Compute earliest_eligible_time when ineligible (Req 4.5)
        earliest_eligible_time: Optional[datetime] = None
        if not eligible:
            now = utcnow()
            candidate_times: List[datetime] = []

            # Drive-hours failure: driver needs a 10-hour rest to reset drive clock
            if hos_status.available_drive_hours < required_drive_hours:
                candidate_times.append(now + timedelta(hours=10))

            # Window failure: driver needs a 10-hour off-duty period to reset 14-hour window
            if hos_status.available_window_hours < estimated_total_hours:
                candidate_times.append(now + timedelta(hours=10))

            # Cycle failure: driver needs a 34-hour restart to reset the cycle
            if projected_cycle_hours > cycle_limit:
                candidate_times.append(now + timedelta(hours=34))

            # Use the LATEST (most restrictive) of all computed earliest times
            if candidate_times:
                earliest_eligible_time = max(candidate_times)

        # Log projected post-route duty hours for audit trail (Req 4.7)
        if eligible:
            await self.log_route_assignment(
                driver_id=driver_id,
                estimated_drive_hours=estimated_drive_hours,
                estimated_total_hours=estimated_total_hours,
                hos_status=hos_status,
            )

        return HOSEligibility(
            driver_id=driver_id,
            eligible=eligible,
            reasons=reasons,
            earliest_eligible_time=earliest_eligible_time,
        )

    async def log_route_assignment(
        self,
        driver_id: str,
        estimated_drive_hours: float,
        estimated_total_hours: float,
        hos_status: HOSStatus,
    ) -> None:
        """Log projected post-route duty hours for audit trail purposes.

        Computes and logs the projected remaining hours after route completion:
        - projected_drive_hours_remaining = available_drive_hours - estimated_drive_hours
        - projected_window_hours_remaining = available_window_hours - estimated_total_hours
        - projected_cumulative_cycle_hours = cumulative_cycle_hours + estimated_total_hours

        Logs at INFO level for audit trail and optionally persists to ES.

        Args:
            driver_id: The driver being assigned.
            estimated_drive_hours: Estimated driving time for the route.
            estimated_total_hours: Estimated total on-duty time for the route.
            hos_status: The driver's current HOS status.

        Validates: Requirement 4.7
        """
        projected_drive_remaining = (
            hos_status.available_drive_hours - estimated_drive_hours
        )
        projected_window_remaining = (
            hos_status.available_window_hours - estimated_total_hours
        )
        projected_cumulative_cycle = (
            hos_status.cumulative_cycle_hours + estimated_total_hours
        )

        logger.info(
            "HOSChecker.log_route_assignment: driver=%s assigned route | "
            "projected_drive_hours_remaining=%.1fh, "
            "projected_window_hours_remaining=%.1fh, "
            "projected_cumulative_cycle_hours=%.1fh | "
            "estimated_drive=%.1fh, estimated_total=%.1fh | "
            "pre_assignment(drive=%.1fh, window=%.1fh, cycle=%.1fh, cycle_type=%s)",
            driver_id,
            projected_drive_remaining,
            projected_window_remaining,
            projected_cumulative_cycle,
            estimated_drive_hours,
            estimated_total_hours,
            hos_status.available_drive_hours,
            hos_status.available_window_hours,
            hos_status.cumulative_cycle_hours,
            hos_status.cycle_type,
        )

        # Persist to ES for queryable audit trail
        try:
            assignment_doc = {
                "driver_id": driver_id,
                "tenant_id": self._tenant_id,
                "estimated_drive_hours": estimated_drive_hours,
                "estimated_total_hours": estimated_total_hours,
                "pre_assignment_drive_hours": hos_status.available_drive_hours,
                "pre_assignment_window_hours": hos_status.available_window_hours,
                "pre_assignment_cumulative_cycle_hours": hos_status.cumulative_cycle_hours,
                "projected_drive_hours_remaining": projected_drive_remaining,
                "projected_window_hours_remaining": projected_window_remaining,
                "projected_cumulative_cycle_hours": projected_cumulative_cycle,
                "cycle_type": hos_status.cycle_type,
                "timestamp": utcnow().isoformat(),
            }
            await self._es.index(
                index="hos_assignment_log",
                body=assignment_doc,
            )
        except Exception as exc:
            # ES persistence failure is logged but never blocks the assignment
            logger.warning(
                "HOSChecker.log_route_assignment: ES persistence failed "
                "driver=%s: %s",
                driver_id,
                exc,
            )

    async def refresh_hos_data(
        self, driver_id: str, *, tenant_id: Optional[str] = None
    ) -> HOSStatus:
        """Retrieve fresh HOS data from Geotab and cache in Redis.

        Pulls the driver's current duty status from the Geotab HOS
        telemetry API and stores it in Redis with a 15-minute TTL at
        key ``hos:{tenant_id}:{driver_id}``.

        Args:
            driver_id: The driver whose HOS data to refresh.
            tenant_id: Optional tenant override (uses instance default if None).

        Returns:
            HOSStatus with the driver's current hours availability.

        Raises:
            RuntimeError: If the Geotab connector fails to return data.

        Validates: Requirements 4.1, 4.6
        """
        effective_tenant = tenant_id or self._tenant_id

        # Pull fresh HOS data from Geotab
        try:
            raw_data = await self._geotab.get_hos_status(driver_id)
        except Exception as exc:
            logger.error(
                "HOSChecker.refresh_hos_data: Geotab fetch failed "
                "tenant=%s driver=%s: %s",
                effective_tenant,
                driver_id,
                exc,
            )
            raise RuntimeError(
                f"Failed to retrieve HOS data from Geotab for driver "
                f"{driver_id}: {exc}"
            ) from exc

        # Parse the Geotab response into our internal model
        if not isinstance(raw_data, Mapping):
            logger.error(
                "HOSChecker.refresh_hos_data: Geotab returned non-mapping "
                "response for tenant=%s driver=%s",
                effective_tenant,
                driver_id,
            )
            raise RuntimeError(
                f"Geotab returned invalid response for driver {driver_id}"
            )

        hos_status = _parse_geotab_hos_response(driver_id, raw_data)

        # Cache in Redis with 15-minute TTL
        cache_key = _build_cache_key(effective_tenant, driver_id)
        try:
            payload = json.dumps(
                hos_status.model_dump(mode="json"), sort_keys=True
            )
            await self._redis.setex(cache_key, CACHE_TTL_SECONDS, payload)
        except Exception as exc:
            # Cache failures are logged but never block the response
            logger.warning(
                "HOSChecker.refresh_hos_data: Redis cache write failed "
                "key=%s: %s",
                cache_key,
                exc,
            )

        logger.info(
            "HOSChecker.refresh_hos_data: cached HOS data for "
            "tenant=%s driver=%s (drive=%.1fh, window=%.1fh, cycle=%.1fh)",
            effective_tenant,
            driver_id,
            hos_status.available_drive_hours,
            hos_status.available_window_hours,
            hos_status.cumulative_cycle_hours,
        )

        return hos_status

    async def _get_cached_hos_data(
        self, tenant_id: str, driver_id: str
    ) -> Optional[HOSStatus]:
        """Check Redis for cached HOS data.

        Args:
            tenant_id: The tenant identifier.
            driver_id: The driver identifier.

        Returns:
            HOSStatus if cached data exists and is valid, None otherwise.
        """
        cache_key = _build_cache_key(tenant_id, driver_id)
        try:
            raw = await self._redis.get(cache_key)
        except Exception as exc:
            logger.warning(
                "HOSChecker._get_cached_hos_data: Redis read failed "
                "key=%s: %s",
                cache_key,
                exc,
            )
            return None

        if raw is None:
            return None

        try:
            data: Dict[str, Any] = json.loads(raw)
            return HOSStatus(**data)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "HOSChecker._get_cached_hos_data: failed to parse cached "
                "data key=%s: %s",
                cache_key,
                exc,
            )
            return None
