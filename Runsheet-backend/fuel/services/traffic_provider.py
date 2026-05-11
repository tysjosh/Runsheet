"""
Traffic provider abstraction and concrete adapters.

Capability 2 / Requirements 2.1.1–2.1.7 of the fuel-ops hardening spec introduce
a pluggable ``Traffic_Provider`` interface so the Route_Planning_Agent can
build traffic-aware travel matrices when the ``overlay.traffic_aware_routing``
feature flag is enabled for a tenant.

This module provides:

* :class:`TravelMatrix` — a strict Pydantic model returned from every
  provider call. Matches the design.md Capability 2 contract: paired origin/
  destination lists with distance (km) and duration (minutes) matrices of
  shape ``len(origins) x len(destinations)`` plus the provider short name.

* :class:`TrafficProvider` — the abstract base class exposing a single
  ``async get_matrix(origins, destinations, depart_at, *, tenant_id)`` entry
  point. The base class owns the shared plumbing:

    1. Per-pair Redis cache lookup keyed by
       ``traffic:{provider}:{lat1}:{lon1}:{lat2}:{lon2}:{bucket_15min}`` with
       a 900-second TTL (Requirement 2.1.4). A fully-cached request skips
       the upstream HTTP call entirely.
    2. Per-tenant monthly budget counter in Redis key
       ``traffic_budget:{tenant_id}:{YYYY-MM}`` compared against the tenant-
       configurable limit stored at ``traffic_provider_budget:{tenant_id}``
       (Requirement 2.1.7). When the budget is exhausted, the base class
       raises :class:`TrafficBudgetExceeded` without issuing the HTTP call —
       the caller (Route_Planning_Agent) catches this and falls back to the
       Haversine + DEFAULT_SPEED_KMH matrix with ``traffic_fallback: true``
       per Requirement 2.1.5.
    3. Subclass call to :meth:`_fetch_raw` wrapped by ``asyncio.wait_for``
       with a 10-second timeout (Requirement 2.1.5). Network / HTTP / parse
       errors are not swallowed — they propagate so the caller can annotate
       ``traffic_fallback: true``. A separate exception type makes the
       budget exit distinguishable from a transient outage.
    4. Best-effort cache population for every pair returned by the provider.

* :class:`MapboxTrafficProvider` — primary adapter for the Mapbox
  Directions Matrix API (``driving-traffic`` profile). Access token is
  resolved from ``MAPBOX_ACCESS_TOKEN`` by default.

* :class:`HERETrafficProvider` — adapter for the HERE Matrix Routing v8
  API. API key is resolved from ``HERE_API_KEY`` by default.

* :class:`GoogleDirectionsTrafficProvider` — adapter for the Google
  Distance Matrix API (the "Directions" branding in design.md refers to
  the same Maps Platform family; Distance Matrix is the correct endpoint
  for an N-by-M request). API key from ``GOOGLE_MAPS_API_KEY`` by default.

Validates: Requirements 2.1.1, 2.1.2, 2.1.4, 2.1.7.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.external_call_tracing import (
    CircuitBreaker,
    CircuitOpenError,
    default_circuit_breaker,
    trace_external_call,
)
from services.metrics import fuelops_traffic_provider_calls_total

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: HTTP timeout for every provider call, per Requirement 2.1.5 (10 seconds).
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 10.0

#: Redis TTL for cached pair entries, per Requirement 2.1.4 (900 seconds).
DEFAULT_CACHE_TTL_SECONDS: int = 900

#: Width of each departure-time cache bucket, per Requirement 2.1.4.
BUCKET_SECONDS: int = 15 * 60

#: TTL on the monthly budget counter (32 days — slightly over a calendar
#: month so the counter naturally expires after the window closes).
BUDGET_COUNTER_TTL_SECONDS: int = 32 * 24 * 3600

#: Redis key templates (kept as module-level so tests can assert on them).
CACHE_KEY_TEMPLATE: str = "traffic:{provider}:{lat1}:{lon1}:{lat2}:{lon2}:{bucket}"
BUDGET_COUNTER_KEY_TEMPLATE: str = "traffic_budget:{tenant_id}:{month}"
BUDGET_LIMIT_KEY_TEMPLATE: str = "traffic_provider_budget:{tenant_id}"

#: Environment variable names honored by the stock adapters.
MAPBOX_TOKEN_ENV: str = "MAPBOX_ACCESS_TOKEN"
HERE_API_KEY_ENV: str = "HERE_API_KEY"
GOOGLE_API_KEY_ENV: str = "GOOGLE_MAPS_API_KEY"

#: Coordinate rounding precision for cache keys. 5 decimals ≈ 1.1 meters,
#: which is tighter than any consumer-grade GPS fix and comfortably below
#: the granularity of our routing stops.
COORD_ROUND_DIGITS: int = 5


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TrafficBudgetExceeded(RuntimeError):
    """Raised when a tenant's monthly Traffic_Provider budget is exhausted.

    The Route_Planning_Agent catches this and falls back to the Haversine +
    DEFAULT_SPEED_KMH matrix with ``traffic_fallback: true`` annotation per
    Requirement 2.1.7.
    """

    def __init__(
        self,
        tenant_id: str,
        month: str,
        current: int,
        limit: int,
    ) -> None:
        super().__init__(
            f"traffic provider budget exceeded for tenant={tenant_id} "
            f"month={month} current={current} limit={limit}"
        )
        self.tenant_id = tenant_id
        self.month = month
        self.current = current
        self.limit = limit


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


LatLon = Tuple[float, float]


class TravelMatrix(BaseModel):
    """Matrix of travel distances and durations for N origins × M destinations.

    Field order and shapes match the design.md Capability 2 contract:

    * ``origins`` — ordered list of ``(lat, lon)`` tuples.
    * ``destinations`` — ordered list of ``(lat, lon)`` tuples.
    * ``distance_km`` — shape ``[len(origins)][len(destinations)]`` in km.
    * ``duration_minutes`` — shape ``[len(origins)][len(destinations)]`` in minutes.
    * ``provider`` — short provider name (``"mapbox"``, ``"here"``, ``"google"``).
    """

    model_config = ConfigDict(extra="forbid")

    origins: List[LatLon] = Field(..., min_length=1)
    destinations: List[LatLon] = Field(..., min_length=1)
    distance_km: List[List[float]] = Field(...)
    duration_minutes: List[List[float]] = Field(...)
    provider: str = Field(..., min_length=1)

    @field_validator("origins", "destinations", mode="after")
    @classmethod
    def _validate_coordinates(cls, value: List[LatLon]) -> List[LatLon]:
        for lat, lon in value:
            if not -90.0 <= float(lat) <= 90.0:
                raise ValueError(f"latitude {lat} out of range [-90, 90]")
            if not -180.0 <= float(lon) <= 180.0:
                raise ValueError(f"longitude {lon} out of range [-180, 180]")
        return [(float(lat), float(lon)) for lat, lon in value]

    @field_validator("provider", mode="before")
    @classmethod
    def _strip_provider(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("provider must not be blank")
        return stripped

    def model_post_init(self, __context: Any) -> None:  # noqa: D401 - pydantic hook
        """Enforce matrix shape after field validation."""

        n_o = len(self.origins)
        n_d = len(self.destinations)
        if len(self.distance_km) != n_o or any(
            len(row) != n_d for row in self.distance_km
        ):
            raise ValueError(
                f"distance_km shape must be {n_o}x{n_d}; got "
                f"{len(self.distance_km)}x"
                f"{[len(r) for r in self.distance_km]}"
            )
        if len(self.duration_minutes) != n_o or any(
            len(row) != n_d for row in self.duration_minutes
        ):
            raise ValueError(
                f"duration_minutes shape must be {n_o}x{n_d}; got "
                f"{len(self.duration_minutes)}x"
                f"{[len(r) for r in self.duration_minutes]}"
            )
        for row in self.distance_km:
            for v in row:
                if v < 0:
                    raise ValueError(
                        f"distance_km must be non-negative; got {v}"
                    )
        for row in self.duration_minutes:
            for v in row:
                if v < 0:
                    raise ValueError(
                        f"duration_minutes must be non-negative; got {v}"
                    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_bucket_15min(depart_at: datetime) -> int:
    """Return a stable 15-minute integer bucket for ``depart_at``.

    The bucket is the number of 15-minute slots since the Unix epoch, which
    means a single integer fully identifies the cache window regardless of
    timezone representation of the input.

    Raises:
        TypeError: If ``depart_at`` is not a ``datetime``.
        ValueError: If ``depart_at`` is naive (no tzinfo) — we require UTC
            awareness to make cache keys deterministic.
    """

    if not isinstance(depart_at, datetime):
        raise TypeError("depart_at must be a datetime instance")
    if depart_at.tzinfo is None:
        raise ValueError("depart_at must be timezone-aware")
    return int(depart_at.timestamp()) // BUCKET_SECONDS


def _round_coord(value: float) -> float:
    """Round a coordinate to :data:`COORD_ROUND_DIGITS` decimals.

    Stable rounding gives us idempotent cache keys even when callers pass
    slightly different floating-point representations of the same stop.
    """

    return round(float(value), COORD_ROUND_DIGITS)


def build_cache_key(
    provider: str,
    origin: LatLon,
    destination: LatLon,
    bucket_15min: int,
) -> str:
    """Return the canonical Redis cache key for one matrix cell.

    Format: ``traffic:{provider}:{lat1}:{lon1}:{lat2}:{lon2}:{bucket}``
    as mandated by Requirement 2.1.4. Coordinates are rounded to a stable
    precision so a downstream caller's float jitter does not produce a new
    key per request.
    """

    lat1 = _round_coord(origin[0])
    lon1 = _round_coord(origin[1])
    lat2 = _round_coord(destination[0])
    lon2 = _round_coord(destination[1])
    return CACHE_KEY_TEMPLATE.format(
        provider=provider,
        lat1=lat1,
        lon1=lon1,
        lat2=lat2,
        lon2=lon2,
        bucket=bucket_15min,
    )


def _current_month_key(now: Optional[datetime] = None) -> str:
    """Return the ``YYYY-MM`` key used for the monthly budget counter."""

    stamp = now or datetime.now(timezone.utc)
    return f"{stamp.year:04d}-{stamp.month:02d}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class TrafficProvider(ABC):
    """Abstract base for traffic-matrix adapters.

    Subclasses implement :meth:`_fetch_raw` to produce ``(distance_km,
    duration_minutes)`` 2D lists. The base class wraps every call with:

        * Per-pair Redis cache lookup + populate (TTL 900s).
        * Per-tenant monthly budget check (raises :class:`TrafficBudgetExceeded`
          before any HTTP is issued when the limit is hit).
        * 10-second httpx timeout.
        * Budget increment only on successful HTTP responses.

    ES and Redis clients are injected at construction time so tests do not
    need to patch module-level singletons. Both are optional — passing
    ``None`` simply disables that layer.
    """

    #: Short identifier ("mapbox", "here", "google") stamped on every
    #: :class:`TravelMatrix` and used as the first component of the cache
    #: key. Concrete providers MUST override this.
    name: ClassVar[str] = "abstract"

    def __init__(
        self,
        *,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        self._redis = redis_client
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._timeout = timeout_seconds
        self._cache_ttl = cache_ttl_seconds
        # Share a process-wide circuit breaker by default so every
        # TrafficProvider subclass participates in the same
        # ``(tenant_id, provider)`` state machine. Tests pass a fresh
        # breaker per case so no state leaks across scenarios.
        self._circuit_breaker = circuit_breaker or default_circuit_breaker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_matrix(
        self,
        origins: Sequence[LatLon],
        destinations: Sequence[LatLon],
        depart_at: datetime,
        *,
        tenant_id: str,
    ) -> TravelMatrix:
        """Return a :class:`TravelMatrix` for ``origins`` → ``destinations``.

        Orchestration order:

            1. Validate args (programmer errors raise).
            2. Cache lookup for every ``(origin, destination)`` pair. Fully
               cached requests short-circuit without HTTP.
            3. Budget check — :class:`TrafficBudgetExceeded` raised when
               the tenant's monthly counter meets the configured limit.
            4. ``_fetch_raw`` called under a 10-second timeout.
            5. Budget counter incremented by 1 (per API call, not per pair).
            6. Cache populated for every returned pair.

        Args:
            origins: Ordered list of ``(lat, lon)`` tuples. Non-empty.
            destinations: Ordered list of ``(lat, lon)`` tuples. Non-empty.
            depart_at: Timezone-aware departure timestamp.
            tenant_id: Owning tenant, used for the budget counter.

        Returns:
            Populated :class:`TravelMatrix`.

        Raises:
            TrafficBudgetExceeded: If the tenant's monthly budget is
                exhausted.
            httpx.HTTPError / asyncio.TimeoutError: Propagated from the
                upstream provider so the caller can distinguish a traffic
                outage from a budget exit.
        """

        origins_list, destinations_list = self._validate_args(
            origins, destinations, depart_at, tenant_id
        )
        bucket = compute_bucket_15min(depart_at)

        # 1) Cache lookup ---------------------------------------------------
        cache_hits = await self._lookup_cache(origins_list, destinations_list, bucket)

        n_o = len(origins_list)
        n_d = len(destinations_list)
        distance = [[0.0] * n_d for _ in range(n_o)]
        duration = [[0.0] * n_d for _ in range(n_o)]
        missing = False
        for i in range(n_o):
            for j in range(n_d):
                hit = cache_hits.get((i, j))
                if hit is None:
                    missing = True
                else:
                    distance[i][j], duration[i][j] = hit

        if not missing:
            return TravelMatrix(
                origins=origins_list,
                destinations=destinations_list,
                distance_km=distance,
                duration_minutes=duration,
                provider=self.name,
            )

        # 2) Budget check BEFORE any HTTP ---------------------------------
        await self._assert_budget(tenant_id)

        # 3) Provider call under strict 10-second budget -------------------
        # Wrap the outbound HTTP call in ``trace_external_call`` so every
        # attempt emits a structured log event and feeds the per-
        # ``(tenant_id, provider)`` circuit breaker (Task 12.9 /
        # Requirement 10.4.1, 10.4.3). ``TrafficBudgetExceeded`` is a
        # client-side gate that runs before the wrapper, so it is not
        # counted as an upstream failure here.
        async with trace_external_call(
            tenant_id=tenant_id,
            provider=self.name,
            operation="get_matrix",
            circuit_breaker=self._circuit_breaker,
            metric=fuelops_traffic_provider_calls_total,
            extra={
                "origin_count": n_o,
                "destination_count": n_d,
                "depart_at": depart_at.isoformat(),
            },
        ):
            raw_distance, raw_duration = await asyncio.wait_for(
                self._fetch_raw(
                    origins=origins_list,
                    destinations=destinations_list,
                    depart_at=depart_at,
                ),
                timeout=self._timeout,
            )
            self._validate_raw_shape(raw_distance, raw_duration, n_o, n_d)

        # 4) Increment budget counter on success --------------------------
        await self._increment_budget(tenant_id)

        # 5) Cache fresh pairs --------------------------------------------
        await self._populate_cache(
            origins_list, destinations_list, bucket, raw_distance, raw_duration
        )

        return TravelMatrix(
            origins=origins_list,
            destinations=destinations_list,
            distance_km=raw_distance,
            duration_minutes=raw_duration,
            provider=self.name,
        )

    # ------------------------------------------------------------------
    # To be implemented by subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    async def _fetch_raw(
        self,
        *,
        origins: List[LatLon],
        destinations: List[LatLon],
        depart_at: datetime,
    ) -> Tuple[List[List[float]], List[List[float]]]:
        """Call the upstream API and return ``(distance_km, duration_minutes)``.

        Each matrix MUST have shape ``len(origins) x len(destinations)`` with
        non-negative entries. Implementations may raise ``httpx.HTTPError``
        or ``ValueError`` on parse failures — the base class does not catch
        these so the caller (Route_Planning_Agent) can annotate
        ``traffic_fallback: true``.
        """

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_args(
        origins: Sequence[LatLon],
        destinations: Sequence[LatLon],
        depart_at: datetime,
        tenant_id: str,
    ) -> Tuple[List[LatLon], List[LatLon]]:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(depart_at, datetime):
            raise TypeError("depart_at must be a datetime instance")
        if depart_at.tzinfo is None:
            raise ValueError("depart_at must be timezone-aware")
        origins_list = list(origins or [])
        destinations_list = list(destinations or [])
        if not origins_list:
            raise ValueError("origins must be a non-empty sequence")
        if not destinations_list:
            raise ValueError("destinations must be a non-empty sequence")
        for label, coords in (("origins", origins_list), ("destinations", destinations_list)):
            for idx, pair in enumerate(coords):
                if (
                    not isinstance(pair, (tuple, list))
                    or len(pair) != 2
                    or not all(isinstance(v, (int, float)) for v in pair)
                ):
                    raise TypeError(
                        f"{label}[{idx}] must be a (lat, lon) tuple; got {pair!r}"
                    )
                lat, lon = float(pair[0]), float(pair[1])
                if not -90.0 <= lat <= 90.0:
                    raise ValueError(
                        f"{label}[{idx}] latitude {lat} out of range [-90, 90]"
                    )
                if not -180.0 <= lon <= 180.0:
                    raise ValueError(
                        f"{label}[{idx}] longitude {lon} out of range [-180, 180]"
                    )
        return (
            [(float(lat), float(lon)) for lat, lon in origins_list],
            [(float(lat), float(lon)) for lat, lon in destinations_list],
        )

    @staticmethod
    def _validate_raw_shape(
        distance: List[List[float]],
        duration: List[List[float]],
        n_o: int,
        n_d: int,
    ) -> None:
        if len(distance) != n_o or any(len(row) != n_d for row in distance):
            raise ValueError(
                f"provider returned distance matrix of wrong shape "
                f"(expected {n_o}x{n_d})"
            )
        if len(duration) != n_o or any(len(row) != n_d for row in duration):
            raise ValueError(
                f"provider returned duration matrix of wrong shape "
                f"(expected {n_o}x{n_d})"
            )

    async def _get_http_client(self) -> Tuple[httpx.AsyncClient, bool]:
        """Return ``(client, owned_by_caller)``.

        When the adapter was constructed with an injected ``http_client`` we
        reuse it and do *not* close it. Otherwise we lazily create one
        instance-owned client so repeated calls reuse connection pooling.
        """

        if self._http_client is not None:
            return self._http_client, False
        self._http_client = httpx.AsyncClient(timeout=self._timeout)
        return self._http_client, False

    async def aclose(self) -> None:
        """Close the lazily owned HTTP client, if this provider created one."""

        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ---------- cache ----------

    async def _lookup_cache(
        self,
        origins: List[LatLon],
        destinations: List[LatLon],
        bucket: int,
    ) -> Dict[Tuple[int, int], Tuple[float, float]]:
        """Return a mapping ``(i, j) -> (distance_km, duration_minutes)``.

        Missing pairs (or any Redis failure) are simply omitted so the
        caller falls through to the upstream fetch. The cache layer never
        raises.
        """

        if self._redis is None:
            return {}
        out: Dict[Tuple[int, int], Tuple[float, float]] = {}
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                key = build_cache_key(self.name, o, d, bucket)
                try:
                    raw = await self._redis.get(key)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "TrafficProvider[%s]: cache get failed for key=%s: %s",
                        self.name,
                        key,
                        exc,
                    )
                    continue
                if raw is None:
                    continue
                try:
                    payload = json.loads(raw)
                    distance = float(payload["distance_km"])
                    duration = float(payload["duration_minutes"])
                except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "TrafficProvider[%s]: cache decode failed for key=%s: %s",
                        self.name,
                        key,
                        exc,
                    )
                    continue
                out[(i, j)] = (distance, duration)
        return out

    async def _populate_cache(
        self,
        origins: List[LatLon],
        destinations: List[LatLon],
        bucket: int,
        distance: List[List[float]],
        duration: List[List[float]],
    ) -> None:
        """Persist every pair to Redis with the configured TTL.

        Failures are logged and swallowed so a broken Redis never blocks a
        successful provider call.
        """

        if self._redis is None:
            return
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                key = build_cache_key(self.name, o, d, bucket)
                payload = json.dumps(
                    {
                        "distance_km": float(distance[i][j]),
                        "duration_minutes": float(duration[i][j]),
                    },
                    sort_keys=True,
                )
                try:
                    await self._redis.setex(key, self._cache_ttl, payload)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "TrafficProvider[%s]: cache put failed for key=%s: %s",
                        self.name,
                        key,
                        exc,
                    )

    # ---------- budget ----------

    async def _assert_budget(self, tenant_id: str) -> None:
        """Raise :class:`TrafficBudgetExceeded` when the tenant is out of budget.

        The check reads the tenant's limit from ``traffic_provider_budget:
        {tenant_id}`` and compares it to the current monthly counter. A
        missing / non-numeric limit is treated as *unlimited* — absence of
        config should not starve routing. A missing counter is treated as
        0 (fresh month).
        """

        if self._redis is None:
            return
        month = _current_month_key()
        limit = await self._load_budget_limit(tenant_id)
        if limit is None:
            return
        counter = await self._load_budget_counter(tenant_id, month)
        if counter >= limit:
            raise TrafficBudgetExceeded(
                tenant_id=tenant_id,
                month=month,
                current=counter,
                limit=limit,
            )

    async def _load_budget_limit(self, tenant_id: str) -> Optional[int]:
        key = BUDGET_LIMIT_KEY_TEMPLATE.format(tenant_id=tenant_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TrafficProvider[%s]: budget limit read failed for tenant=%s: %s",
                self.name,
                tenant_id,
                exc,
            )
            return None
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "TrafficProvider[%s]: non-numeric budget limit for tenant=%s: %r",
                self.name,
                tenant_id,
                raw,
            )
            return None

    async def _load_budget_counter(self, tenant_id: str, month: str) -> int:
        key = BUDGET_COUNTER_KEY_TEMPLATE.format(tenant_id=tenant_id, month=month)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TrafficProvider[%s]: budget counter read failed for tenant=%s: %s",
                self.name,
                tenant_id,
                exc,
            )
            return 0
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    async def _increment_budget(self, tenant_id: str) -> None:
        if self._redis is None:
            return
        month = _current_month_key()
        key = BUDGET_COUNTER_KEY_TEMPLATE.format(tenant_id=tenant_id, month=month)
        try:
            incr = getattr(self._redis, "incr", None)
            if incr is not None:
                current = await incr(key)
            else:  # pragma: no cover - alternative clients
                current = (await self._load_budget_counter(tenant_id, month)) + 1
                try:
                    await self._redis.set(
                        key, str(current), ex=BUDGET_COUNTER_TTL_SECONDS
                    )
                except TypeError:
                    await self._redis.set(key, str(current))
            # Always refresh TTL so the key rolls off after the month ends.
            expire = getattr(self._redis, "expire", None)
            if expire is not None:
                await expire(key, BUDGET_COUNTER_TTL_SECONDS)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "TrafficProvider[%s]: budget increment failed for tenant=%s: %s",
                self.name,
                tenant_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Mapbox adapter (primary)
# ---------------------------------------------------------------------------


class MapboxTrafficProvider(TrafficProvider):
    """Mapbox Directions Matrix API adapter.

    Endpoint: ``https://api.mapbox.com/directions-matrix/v1/mapbox/
    driving-traffic/{coords}`` where ``{coords}`` is ``lon,lat;lon,lat;...``
    (Mapbox uses lon/lat order). The request lists all origins and
    destinations together, using ``sources`` / ``destinations`` query
    parameters to pick them out by index.

    Mapbox returns distances in meters and durations in seconds; we convert
    to km and minutes respectively.

    Token resolution precedence:
        1. Explicit ``access_token`` argument.
        2. ``MAPBOX_ACCESS_TOKEN`` environment variable.

    Missing-token calls raise :class:`RuntimeError` — the Route_Planning_Agent
    treats this as a configuration error (not a transient failure) and
    deactivates traffic-aware routing for the tenant until the secret is
    provisioned.
    """

    name: ClassVar[str] = "mapbox"
    base_url: ClassVar[str] = (
        "https://api.mapbox.com/directions-matrix/v1/mapbox/driving-traffic"
    )

    def __init__(
        self,
        *,
        access_token: Optional[str] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        super().__init__(
            redis_client=redis_client,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        self._access_token = access_token or os.environ.get(MAPBOX_TOKEN_ENV)

    async def _fetch_raw(
        self,
        *,
        origins: List[LatLon],
        destinations: List[LatLon],
        depart_at: datetime,
    ) -> Tuple[List[List[float]], List[List[float]]]:
        if not self._access_token:
            raise RuntimeError(
                f"MapboxTrafficProvider: no access token (env {MAPBOX_TOKEN_ENV})"
            )

        # Build coordinate string with Mapbox's lon,lat order. Origins come
        # first, destinations second — we then reference them via source /
        # destination indices to avoid duplicating coincident points.
        coords = origins + destinations
        coord_str = ";".join(
            f"{_round_coord(lon)},{_round_coord(lat)}" for lat, lon in coords
        )
        n_o = len(origins)
        n_d = len(destinations)
        sources = ";".join(str(i) for i in range(n_o))
        dests = ";".join(str(n_o + j) for j in range(n_d))

        client, _ = await self._get_http_client()
        response = await client.get(
            f"{self.base_url}/{coord_str}",
            params={
                "access_token": self._access_token,
                "annotations": "distance,duration",
                "sources": sources,
                "destinations": dests,
                "depart_at": depart_at.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%MZ"
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()

        code = payload.get("code")
        if code and code != "Ok":
            raise httpx.HTTPError(f"Mapbox returned code={code!r}")
        distances_raw = payload.get("distances") or []
        durations_raw = payload.get("durations") or []

        distance_km = _convert_matrix(distances_raw, n_o, n_d, scale=1.0 / 1000.0)
        duration_minutes = _convert_matrix(durations_raw, n_o, n_d, scale=1.0 / 60.0)
        return distance_km, duration_minutes


# ---------------------------------------------------------------------------
# HERE adapter
# ---------------------------------------------------------------------------


class HERETrafficProvider(TrafficProvider):
    """HERE Matrix Routing v8 API adapter.

    Endpoint: ``https://matrix.router.hereapi.com/v8/matrix``. The request
    is a JSON POST with ``origins`` / ``destinations`` as ``{lat, lng}``
    objects and ``matrixAttributes`` of ``["travelTimes", "distances"]``.

    HERE returns distances in meters and travel times in seconds (flat
    row-major arrays); we convert to km and minutes.

    API key resolution precedence:
        1. Explicit ``api_key`` argument.
        2. ``HERE_API_KEY`` environment variable.
    """

    name: ClassVar[str] = "here"
    base_url: ClassVar[str] = "https://matrix.router.hereapi.com/v8/matrix"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        transport_mode: str = "truck",
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        super().__init__(
            redis_client=redis_client,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        self._api_key = api_key or os.environ.get(HERE_API_KEY_ENV)
        self._transport_mode = transport_mode

    async def _fetch_raw(
        self,
        *,
        origins: List[LatLon],
        destinations: List[LatLon],
        depart_at: datetime,
    ) -> Tuple[List[List[float]], List[List[float]]]:
        if not self._api_key:
            raise RuntimeError(
                f"HERETrafficProvider: no API key (env {HERE_API_KEY_ENV})"
            )

        body = {
            "origins": [
                {"lat": _round_coord(lat), "lng": _round_coord(lon)}
                for lat, lon in origins
            ],
            "destinations": [
                {"lat": _round_coord(lat), "lng": _round_coord(lon)}
                for lat, lon in destinations
            ],
            "regionDefinition": {"type": "world"},
            "transportMode": self._transport_mode,
            "departureTime": depart_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "matrixAttributes": ["travelTimes", "distances"],
        }

        client, _ = await self._get_http_client()
        response = await client.post(
            self.base_url,
            params={"apiKey": self._api_key, "async": "false"},
            json=body,
        )
        response.raise_for_status()
        payload = response.json()

        matrix = payload.get("matrix") or payload
        distances_flat = matrix.get("distances") or []
        travel_times_flat = matrix.get("travelTimes") or []
        n_o = len(origins)
        n_d = len(destinations)
        if (
            len(distances_flat) != n_o * n_d
            or len(travel_times_flat) != n_o * n_d
        ):
            raise httpx.HTTPError(
                f"HERE returned matrix of wrong size "
                f"(expected {n_o * n_d}, got "
                f"{len(distances_flat)} distances, "
                f"{len(travel_times_flat)} travelTimes)"
            )

        distance_km: List[List[float]] = []
        duration_minutes: List[List[float]] = []
        for i in range(n_o):
            dist_row: List[float] = []
            dur_row: List[float] = []
            for j in range(n_d):
                idx = i * n_d + j
                dist_row.append(max(0.0, float(distances_flat[idx]) / 1000.0))
                dur_row.append(max(0.0, float(travel_times_flat[idx]) / 60.0))
            distance_km.append(dist_row)
            duration_minutes.append(dur_row)
        return distance_km, duration_minutes


# ---------------------------------------------------------------------------
# Google Directions / Distance Matrix adapter
# ---------------------------------------------------------------------------


class GoogleDirectionsTrafficProvider(TrafficProvider):
    """Google Distance Matrix API adapter.

    Design.md refers to this as "Google Directions" — Google's Maps Platform
    bundles Directions and Distance Matrix under one umbrella. For N-by-M
    matrix requests the Distance Matrix endpoint is the correct surface,
    so we hit ``https://maps.googleapis.com/maps/api/distancematrix/json``.

    Google returns distances in meters and durations in seconds (rows of
    ``elements``). When ``departure_time`` is supplied, ``duration_in_traffic``
    is preferred over ``duration`` because it reflects real-time traffic.

    API key resolution precedence:
        1. Explicit ``api_key`` argument.
        2. ``GOOGLE_MAPS_API_KEY`` environment variable.
    """

    name: ClassVar[str] = "google"
    base_url: ClassVar[str] = "https://maps.googleapis.com/maps/api/distancematrix/json"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        mode: str = "driving",
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        super().__init__(
            redis_client=redis_client,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        self._api_key = api_key or os.environ.get(GOOGLE_API_KEY_ENV)
        self._mode = mode

    async def _fetch_raw(
        self,
        *,
        origins: List[LatLon],
        destinations: List[LatLon],
        depart_at: datetime,
    ) -> Tuple[List[List[float]], List[List[float]]]:
        if not self._api_key:
            raise RuntimeError(
                f"GoogleDirectionsTrafficProvider: no API key (env {GOOGLE_API_KEY_ENV})"
            )

        def _fmt(coord: LatLon) -> str:
            lat, lon = coord
            return f"{_round_coord(lat)},{_round_coord(lon)}"

        # Google accepts future departure_time as a Unix epoch seconds int;
        # "now" is also accepted but we always pass the explicit timestamp
        # so cache buckets line up with the provider's notion of departure.
        depart_epoch = int(depart_at.astimezone(timezone.utc).timestamp())
        params = {
            "origins": "|".join(_fmt(o) for o in origins),
            "destinations": "|".join(_fmt(d) for d in destinations),
            "mode": self._mode,
            "departure_time": depart_epoch,
            "traffic_model": "best_guess",
            "key": self._api_key,
        }

        client, _ = await self._get_http_client()
        response = await client.get(self.base_url, params=params)
        response.raise_for_status()
        payload = response.json()

        status = payload.get("status")
        if status and status != "OK":
            raise httpx.HTTPError(
                f"Google Distance Matrix returned status={status!r} "
                f"error={payload.get('error_message')!r}"
            )
        rows = payload.get("rows") or []
        n_o = len(origins)
        n_d = len(destinations)
        if len(rows) != n_o:
            raise httpx.HTTPError(
                f"Google returned {len(rows)} rows; expected {n_o}"
            )

        distance_km: List[List[float]] = []
        duration_minutes: List[List[float]] = []
        for i in range(n_o):
            elements = rows[i].get("elements") or []
            if len(elements) != n_d:
                raise httpx.HTTPError(
                    f"Google row {i} has {len(elements)} elements; expected {n_d}"
                )
            dist_row: List[float] = []
            dur_row: List[float] = []
            for el in elements:
                if el.get("status") != "OK":
                    raise httpx.HTTPError(
                        f"Google element status={el.get('status')!r}"
                    )
                dist_m = float(((el.get("distance") or {}).get("value")) or 0)
                # Prefer duration_in_traffic when available (live traffic).
                dur_obj = el.get("duration_in_traffic") or el.get("duration") or {}
                dur_s = float(dur_obj.get("value") or 0)
                dist_row.append(max(0.0, dist_m / 1000.0))
                dur_row.append(max(0.0, dur_s / 60.0))
            distance_km.append(dist_row)
            duration_minutes.append(dur_row)
        return distance_km, duration_minutes


# ---------------------------------------------------------------------------
# Matrix helper
# ---------------------------------------------------------------------------


def _convert_matrix(
    rows: Sequence[Sequence[Any]],
    n_o: int,
    n_d: int,
    *,
    scale: float,
) -> List[List[float]]:
    """Validate shape and scale an upstream matrix to a float 2D list.

    Mapbox / HERE return already-rectangular matrices. Any ``None`` entry
    (which Mapbox uses for unreachable pairs) is coerced to ``0.0`` so the
    caller still gets a full matrix; the Route_Planning_Agent treats those
    cells with its Haversine fallback if desired.
    """

    if len(rows) != n_o:
        raise httpx.HTTPError(
            f"provider matrix row count {len(rows)}; expected {n_o}"
        )
    out: List[List[float]] = []
    for i, row in enumerate(rows):
        if len(row) != n_d:
            raise httpx.HTTPError(
                f"provider matrix row {i} has {len(row)} cols; expected {n_d}"
            )
        converted: List[float] = []
        for v in row:
            if v is None:
                converted.append(0.0)
                continue
            try:
                converted.append(max(0.0, float(v) * scale))
            except (TypeError, ValueError) as exc:
                raise httpx.HTTPError(f"non-numeric matrix cell: {v!r}") from exc
        out.append(converted)
    return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_traffic_provider(name: str, **kwargs: Any) -> TrafficProvider:
    """Return a concrete provider by short name.

    Used by the Route_Planning_Agent once it has looked up the tenant's
    ``overlay.traffic_provider`` Redis key. Unknown names raise
    :class:`ValueError` — we never silently fall through to a default
    because that would hide a mis-configuration.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("traffic provider name must be a non-empty string")
    normalized = name.strip().lower()
    if normalized == MapboxTrafficProvider.name:
        return MapboxTrafficProvider(**kwargs)
    if normalized == HERETrafficProvider.name:
        return HERETrafficProvider(**kwargs)
    if normalized in (GoogleDirectionsTrafficProvider.name, "google_directions"):
        return GoogleDirectionsTrafficProvider(**kwargs)
    raise ValueError(f"unknown traffic provider: {name!r}")


__all__ = [
    "BUCKET_SECONDS",
    "BUDGET_COUNTER_KEY_TEMPLATE",
    "BUDGET_COUNTER_TTL_SECONDS",
    "BUDGET_LIMIT_KEY_TEMPLATE",
    "CACHE_KEY_TEMPLATE",
    "COORD_ROUND_DIGITS",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "GOOGLE_API_KEY_ENV",
    "GoogleDirectionsTrafficProvider",
    "HERE_API_KEY_ENV",
    "HERETrafficProvider",
    "LatLon",
    "MAPBOX_TOKEN_ENV",
    "MapboxTrafficProvider",
    "TrafficBudgetExceeded",
    "TrafficProvider",
    "TravelMatrix",
    "build_cache_key",
    "build_traffic_provider",
    "compute_bucket_15min",
]
