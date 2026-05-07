"""
Terminal wait-time resolver — centralized accessor for the rolling
2-hour average wait persisted at
``terminal_wait:{tenant_id}:{terminal_id}`` by the Task 7.7 wait-summary
endpoint.

Why this module exists
----------------------

Task 7.7 writes a rolling 2-hour average wait time to Redis every time
the ``GET /api/fuel/terminals/{terminal_id}/wait-summary`` endpoint
computes a fresh summary (or accepts a new observation through the
``POST /wait-reports`` endpoint, which invalidates the cache). Multiple
callers need to read that same value:

* The Sourcing_Recommender (Task 7.9) scores terminals on ``avg_wait``
  via an injected ``wait_time_resolver`` async callable
  ``(tenant_id, terminal_id) -> Optional[float]``.
* The Task 7.11 wait-warning annotation reads the same rolling average
  plus the tenant-configured threshold.
* Future consumers (e.g. the Route_Planning_Agent's terminal-selection
  path) will want the same number.

Rather than have every caller duplicate the Redis-key layout and the
ES-aggregation fallback, this module exports a single
:class:`TerminalWaitResolver` that:

    1. Reads the Redis key mandated by the Task 7.7 brief
       (``terminal_wait:{tenant_id}:{terminal_id}``).
    2. Falls back to a direct aggregation over the
       ``terminal_wait_reports`` ES index when Redis is unavailable,
       the key is missing, or the cached payload fails identity
       validation.
    3. Returns ``None`` when no observations exist in the rolling
       window (so the consumer can default to 0 wait rather than
       treating missing data as "infinite wait").

The module also exports :func:`build_wait_time_resolver`, which returns
a bound async callable matching the Sourcing_Recommender's
``WaitTimeResolver`` protocol. The bootstrap wire-up in
:mod:`bootstrap.agents` injects this callable into the
Sourcing_Recommender so the REST endpoint and the recommender always
agree on the same rolling average.

Defense-in-depth
----------------

Cross-tenant cache poisoning is guarded the same way
:func:`fuel.api.fuel_ops_endpoints._load_wait_summary_from_cache`
guards it — any cached payload whose embedded ``tenant_id`` /
``terminal_id`` does not match the caller is discarded and the resolver
falls back to the ES aggregation. A Redis outage downgrades to the ES
path, which in turn never raises (failures log a warning and return
``None``) so a broken Redis never breaks sourcing.

Validates: Requirements 8.4.2, 8.4.4, 8.4.5.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from fuel.terminal_models import TerminalWaitReportRepository

logger = logging.getLogger(__name__)


#: Width of the rolling-window for Req 8.4.4. Kept here rather than
#: imported from :mod:`fuel.api.fuel_ops_endpoints` to avoid a circular
#: import between the endpoint module and the services layer.
WAIT_SUMMARY_WINDOW: timedelta = timedelta(hours=2)

#: Redis key template mandated by the Task 7.7 brief. Duplicated here
#: (rather than imported from the endpoints module) so the resolver
#: has zero cross-layer dependencies; both strings are covered by
#: assertion tests so a silent drift would fail CI.
TERMINAL_WAIT_CACHE_KEY_TEMPLATE: str = "terminal_wait:{tenant_id}:{terminal_id}"


#: Signature the Sourcing_Recommender's constructor accepts as
#: ``wait_time_resolver``. Re-exported here so bootstrap code can type-
#: annotate its wire-up without having to import from the recommender
#: module (which drags in rack-price provider plumbing).
WaitTimeResolver = Callable[[str, str], Awaitable[Optional[float]]]


def _cache_key(tenant_id: str, terminal_id: str) -> str:
    return TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
        tenant_id=tenant_id, terminal_id=terminal_id
    )


class TerminalWaitResolver:
    """Resolve the rolling 2-hour avg wait for a (tenant, terminal) pair.

    The resolver is constructed once at bootstrap and shared across all
    consumers that need the same average. Both dependencies are
    optional so the resolver degrades gracefully:

    * ``redis_client`` — Async Redis client. When ``None``, the
      resolver always falls back to the ES aggregation.
    * ``wait_report_repository`` — Tenant-scoped
      :class:`TerminalWaitReportRepository`. When ``None``, the
      resolver only reads from Redis and returns ``None`` on a cache
      miss (the Sourcing_Recommender then defaults to 0 wait).

    Either can be ``None`` for unit tests, but at least one must be
    provided for a non-trivial deployment.
    """

    def __init__(
        self,
        *,
        redis_client: Any = None,
        wait_report_repository: Optional[TerminalWaitReportRepository] = None,
        window: timedelta = WAIT_SUMMARY_WINDOW,
    ) -> None:
        if redis_client is None and wait_report_repository is None:
            # Explicitly reject the degenerate construction so a bug
            # wires the resolver into the Sourcing_Recommender with no
            # backing store. Tests that need an always-None resolver
            # should instead pass ``wait_time_resolver=None`` to the
            # recommender constructor directly.
            raise ValueError(
                "TerminalWaitResolver requires at least one of "
                "redis_client or wait_report_repository"
            )
        if window <= timedelta(0):
            raise ValueError("window must be a positive timedelta")
        self._redis = redis_client
        self._repo = wait_report_repository
        self._window = window

    async def resolve(
        self, tenant_id: str, terminal_id: str
    ) -> Optional[float]:
        """Return the rolling avg wait in minutes, or ``None``.

        Resolution order:

            1. Redis ``terminal_wait:{tenant_id}:{terminal_id}`` —
               when present, well-formed, and tagged with matching
               tenant+terminal, returns the cached ``avg_wait_minutes``
               (unless ``sample_count == 0``, in which case we return
               ``None``).
            2. ES aggregation over ``terminal_wait_reports`` for the
               trailing ``self._window`` period — returns the mean of
               observed ``wait_minutes`` values or ``None`` when no
               observations exist.

        Any exception from the Redis or ES backend is logged and
        downgraded to "no observation" (return ``None``) so a flaky
        upstream never breaks the caller. The caller is expected to
        treat ``None`` as "no telemetry — do not penalise".
        """

        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(terminal_id, str) or not terminal_id.strip():
            raise ValueError("terminal_id must be a non-empty string")

        cached = await self._read_from_cache(tenant_id, terminal_id)
        if cached is not None:
            return cached

        return await self._aggregate_from_es(tenant_id, terminal_id)

    # Make the instance itself awaitable as ``resolver(tenant, term)``
    # so callers can pass it wherever a ``WaitTimeResolver`` callable
    # is expected without an explicit ``.resolve`` bound method.
    async def __call__(
        self, tenant_id: str, terminal_id: str
    ) -> Optional[float]:
        return await self.resolve(tenant_id, terminal_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_from_cache(
        self, tenant_id: str, terminal_id: str
    ) -> Optional[float]:
        if self._redis is None:
            return None
        key = _cache_key(tenant_id, terminal_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalWaitResolver: Redis GET failed tenant=%s terminal=%s: %s",
                tenant_id,
                terminal_id,
                exc,
            )
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning(
                    "TerminalWaitResolver: undecodable cache payload tenant=%s terminal=%s",
                    tenant_id,
                    terminal_id,
                )
                return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "TerminalWaitResolver: cache decode failed tenant=%s terminal=%s: %s",
                tenant_id,
                terminal_id,
                exc,
            )
            return None
        if not isinstance(payload, dict):
            return None
        if (
            payload.get("tenant_id") != tenant_id
            or payload.get("terminal_id") != terminal_id
        ):
            logger.warning(
                "TerminalWaitResolver: dropped cache with mismatched identity "
                "tenant=%s terminal=%s",
                tenant_id,
                terminal_id,
            )
            return None
        # An empty window is "no observation", not "0 wait". The
        # consumer decides how to treat the absence.
        try:
            sample_count = int(payload.get("sample_count", 0))
        except (TypeError, ValueError):
            sample_count = 0
        if sample_count <= 0:
            return None
        try:
            value = float(payload.get("avg_wait_minutes"))
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        return value

    async def _aggregate_from_es(
        self, tenant_id: str, terminal_id: str
    ) -> Optional[float]:
        if self._repo is None:
            return None
        now = datetime.now(timezone.utc)
        window_start = now - self._window
        try:
            reports = await self._repo.list_for_tenant(
                tenant_id,
                terminal_id=terminal_id,
                observed_since=window_start,
                size=500,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TerminalWaitResolver: ES aggregation failed tenant=%s terminal=%s: %s",
                tenant_id,
                terminal_id,
                exc,
            )
            return None
        # Belt-and-braces filter matches the endpoint's aggregation so
        # a time-skew corner case doesn't inflate the mean. Excluding
        # reports with ``observed_at > now`` is particularly important
        # when callers pass observed-at values in the future (spoof or
        # clock drift).
        filtered = [
            r for r in reports
            if r.observed_at >= window_start and r.observed_at <= now
        ]
        if not filtered:
            return None
        total = sum(float(r.wait_minutes) for r in filtered)
        return total / len(filtered)


def build_wait_time_resolver(
    *,
    redis_client: Any = None,
    wait_report_repository: Optional[TerminalWaitReportRepository] = None,
    window: timedelta = WAIT_SUMMARY_WINDOW,
) -> WaitTimeResolver:
    """Return a bound async callable that resolves terminal waits.

    The returned callable matches the signature the
    :class:`fuel.services.sourcing_recommender.SourcingRecommender`
    constructor expects as ``wait_time_resolver``:

        ``async (tenant_id: str, terminal_id: str) -> Optional[float]``

    Bootstrap wires this callable into both the Sourcing_Recommender
    and any other consumers so they all read from the same Redis key
    (``terminal_wait:{tenant_id}:{terminal_id}``) populated by the
    Task 7.7 wait-summary endpoint.
    """

    resolver = TerminalWaitResolver(
        redis_client=redis_client,
        wait_report_repository=wait_report_repository,
        window=window,
    )
    return resolver


__all__ = [
    "TERMINAL_WAIT_CACHE_KEY_TEMPLATE",
    "WAIT_SUMMARY_WINDOW",
    "TerminalWaitResolver",
    "WaitTimeResolver",
    "build_wait_time_resolver",
]
