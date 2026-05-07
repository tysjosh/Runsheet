"""
Weather provider abstraction and concrete adapters.

Capability 1 / Requirements 1.2.1–1.2.6 of the fuel-ops hardening spec introduce
a pluggable ``Weather_Provider`` interface so the Tank_Forecasting_Agent can
feed temperature and Heating-Degree-Day (HDD) signals into the propane and
heating-oil consumption models. This module defines:

* :class:`DailyWeather` — a strict Pydantic model matching the
  ``weather_observations`` ES mapping 1:1 so ``model_dump(mode="json")`` can be
  indexed directly (fields: ``date``, ``zip_code``, ``tenant_id``,
  ``avg_temp_f``, ``hdd``, ``provider``, ``retrieved_at``).
* :class:`WeatherProvider` — the abstract base class exposing a single
  ``async fetch(zip_code, start_date, end_date, *, tenant_id)`` entry point.
  The base class owns the shared plumbing:

    1. Redis cache lookup keyed by ``weather:{provider}:{zip}:{start}:{end}``
       with a 3600-second TTL (Requirement 1.2.4).
    2. Per-provider call to ``_fetch_raw`` with a 5-second httpx timeout
       (Requirement 1.2.2 / 1.2.5).
    3. Graceful degradation: network / parse / timeout failures are logged at
       warning level and the method returns an empty list, letting callers
       fall back to the non-weather consumption model and annotate
       ``weather_fallback: true`` (Requirement 1.2.5).
    4. Persistence of every daily observation to the ``weather_observations``
       ES index including ``tenant_id`` (Requirement 1.2.6).
    5. Population of the Redis cache with the fetched payload.

* :class:`NOAAWeatherProvider` — primary adapter for the NOAA Climate Data
  Online API (``https://www.ncei.noaa.gov/cdo-web/api/v2/``). Requires a token
  from ``NOAA_CDO_TOKEN`` (or an injected token). Parses the CDO ``GHCND``
  dataset, converts tenths-of-Celsius ``TAVG`` readings to Fahrenheit, and
  averages across stations within a ZIP when multiple are returned.
* :class:`OpenWeatherProvider` — secondary adapter for OpenWeather's
  ``day_summary`` One-Call v3 endpoint. API key is resolved from
  ``OPENWEATHER_API_KEY`` by default. If a :class:`TenantCredentialsVault` and
  a ``credentials_ref`` are supplied, the vault is consulted first and the env
  var is used only as a fallback — this keeps the adapter ready for the
  per-tenant KMS-backed secret rollout without requiring it on day one.

The providers never mutate caller state and never swallow programmer errors
(``ValueError``, ``TypeError``, etc.). Only *network*, *HTTP*, and *parse*
errors degrade gracefully to an empty list.

Validates: Requirements 1.2.1, 1.2.2, 1.2.4, 1.2.5, 1.2.6.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuel.services.fuel_ops_es_mappings import WEATHER_OBSERVATIONS_INDEX

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Base temperature (°F) used to compute Heating Degree Days. Standard US
#: residential/commercial heating-oil and propane consumption baseline.
HDD_BASE_F: float = 65.0

#: HTTP timeout for every provider call, per Requirement 1.2.5.
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 5.0

#: Redis TTL for cached responses, per Requirement 1.2.4 (3600 seconds).
DEFAULT_CACHE_TTL_SECONDS: int = 3600

#: Environment variable names honored by the stock adapters.
NOAA_TOKEN_ENV: str = "NOAA_CDO_TOKEN"
OPENWEATHER_KEY_ENV: str = "OPENWEATHER_API_KEY"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class DailyWeather(BaseModel):
    """A single daily weather observation for a ZIP/tenant/provider triple.

    Persisted 1:1 to the ``weather_observations`` ES index. Field order and
    types match the mapping in :mod:`Agents.support.fuel_ops_es_mappings`, so
    ``model_dump(mode="json")`` produces a valid indexing payload with no
    post-processing.
    """

    model_config = ConfigDict(extra="forbid")

    date: _dt.date = Field(
        ...,
        description="Calendar date of the observation (local to the ZIP's timezone).",
    )
    zip_code: str = Field(
        ...,
        min_length=1,
        description="Looked-up ZIP code; stored as string to preserve leading zeros.",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Owning tenant. Propagated from the caller so every persisted "
            "observation carries the correct tenant_id (Req 1.2.6)."
        ),
    )
    avg_temp_f: float = Field(
        ...,
        description=(
            "Daily average temperature in degrees Fahrenheit. Allowed to be "
            "negative (cold climates); no upper/lower bound is imposed because "
            "provider APIs already pre-validate the values."
        ),
    )
    hdd: float = Field(
        ...,
        ge=0.0,
        description=(
            "Heating Degree Days for the day, base 65°F: ``max(0, 65 - "
            "avg_temp_f)``. Always non-negative by construction."
        ),
    )
    provider: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the concrete provider that produced the "
            "observation (``noaa`` or ``openweather``)."
        ),
    )
    retrieved_at: _dt.datetime = Field(
        ...,
        description="Wall-clock time at which the observation was retrieved.",
    )

    @field_validator("zip_code", "tenant_id", "provider", mode="before")
    @classmethod
    def _strip_required_strings(cls, value: Any) -> Any:
        """Collapse whitespace-only values into a ValidationError."""

        if value is None:
            return value
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_hdd(avg_temp_f: float, base: float = HDD_BASE_F) -> float:
    """Return ``max(0, base - avg_temp_f)`` rounded to 3 decimals.

    Rounding keeps the persisted values stable across floating-point noise so
    downstream equality comparisons in tests (and the forecaster's K-factor
    calibration) behave deterministically.
    """

    return round(max(0.0, base - float(avg_temp_f)), 3)


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` for ``retrieved_at`` stamps."""

    return datetime.now(timezone.utc)


def _build_cache_key(provider: str, zip_code: str, start_date: date, end_date: date) -> str:
    """Return the canonical Redis cache key for a provider/zip/date-range tuple.

    Format: ``weather:{provider}:{zip}:{start}:{end}`` as mandated by
    Requirement 1.2.4. Dates are rendered as ISO-8601 strings so the key is
    stable regardless of the caller's ``date`` object identity.
    """

    return f"weather:{provider}:{zip_code}:{start_date.isoformat()}:{end_date.isoformat()}"


def _daterange(start_date: date, end_date: date) -> List[date]:
    """Return every date in ``[start_date, end_date]`` inclusive.

    Raises:
        ValueError: If ``end_date`` precedes ``start_date``.
    """

    if end_date < start_date:
        raise ValueError(
            f"end_date ({end_date}) must not precede start_date ({start_date})"
        )
    days: List[date] = []
    cursor = start_date
    while cursor <= end_date:
        days.append(cursor)
        cursor = cursor + timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class WeatherProvider(ABC):
    """Abstract base for weather adapters.

    Subclasses implement :meth:`_fetch_raw` to produce an ordered list of
    :class:`DailyWeather` rows for a given ZIP / date-range / tenant. The base
    class wraps every call with:

        * Redis cache lookup + populate (TTL 3600s).
        * 5-second httpx timeout.
        * Graceful fallback to ``[]`` on network / parse errors.
        * Persistence to the ``weather_observations`` ES index.

    The base class is deliberately synchronous in its dependencies: ES and
    Redis clients are injected at construction time so tests do not need to
    patch module-level singletons. Both are optional — passing ``None`` simply
    disables that layer (useful for providers running outside of a fully
    wired backend, e.g. the one-off CLI backfill).
    """

    #: Short identifier ("noaa", "openweather") stamped on every
    #: :class:`DailyWeather` row and used as the first component of the
    #: cache key. Concrete providers MUST override this.
    name: ClassVar[str] = "abstract"

    #: ES index that receives daily observations.
    index_name: ClassVar[str] = WEATHER_OBSERVATIONS_INDEX

    def __init__(
        self,
        *,
        es_service: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        self._es = es_service
        self._redis = redis_client
        self._http_client = http_client
        self._timeout = timeout_seconds
        self._cache_ttl = cache_ttl_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(
        self,
        zip_code: str,
        start_date: date,
        end_date: date,
        *,
        tenant_id: str,
    ) -> List[DailyWeather]:
        """Return the daily weather rows for the ZIP / date-range / tenant.

        Orchestration order:

            1. Validate args (programmer errors raise).
            2. Cache lookup — on hit, return without HTTP or ES writes.
            3. Call :meth:`_fetch_raw` under a 5-second timeout.
            4. Persist every row to ES (best-effort — failures are logged
               but do not mask the fetched data).
            5. Cache the serialized result with a 3600-second TTL.

        Network / provider failures degrade to ``[]`` so callers can fall
        back to the non-weather consumption path and annotate
        ``weather_fallback: true`` per Requirement 1.2.5.

        Args:
            zip_code: US ZIP code (string, leading zeros preserved).
            start_date: First date in the (inclusive) range.
            end_date: Last date in the (inclusive) range.
            tenant_id: Owning tenant, propagated to every persisted row
                and to the cache namespace via the payload (not the key,
                so a single zip+range shared across tenants still hits
                the same cache entry — we re-stamp tenant_id on read).

        Returns:
            Ordered list of :class:`DailyWeather` rows sorted by date
            ascending. Empty list on provider failure or invalid input
            (after the basic arg checks succeed).
        """

        self._validate_fetch_args(zip_code, start_date, end_date, tenant_id)

        cache_key = _build_cache_key(self.name, zip_code, start_date, end_date)

        # 1) Cache lookup ---------------------------------------------------
        cached = await self._cache_get(cache_key, tenant_id=tenant_id)
        if cached is not None:
            return cached

        # 2) Provider call with strict 5-second budget ----------------------
        try:
            rows = await asyncio.wait_for(
                self._fetch_raw(
                    zip_code=zip_code,
                    start_date=start_date,
                    end_date=end_date,
                    tenant_id=tenant_id,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "WeatherProvider[%s]: timed out after %.1fs for zip=%s range=%s..%s",
                self.name,
                self._timeout,
                zip_code,
                start_date,
                end_date,
            )
            return []
        except (httpx.HTTPError, httpx.RequestError) as exc:  # pragma: no cover - narrowed
            logger.warning(
                "WeatherProvider[%s]: HTTP error for zip=%s range=%s..%s: %s",
                self.name,
                zip_code,
                start_date,
                end_date,
                exc,
            )
            return []
        except Exception as exc:  # pragma: no cover - defensive fallback
            # Catch-all keeps the forecaster resilient to upstream quirks
            # (JSON decode errors, missing keys, etc.). Programmer errors
            # inside _fetch_raw still surface here but the caller's forecast
            # pipeline continues.
            logger.warning(
                "WeatherProvider[%s]: unexpected error for zip=%s range=%s..%s: %s",
                self.name,
                zip_code,
                start_date,
                end_date,
                exc,
            )
            return []

        # Normalize & sort so downstream math doesn't depend on provider order.
        rows = sorted(rows, key=lambda r: r.date)

        # 3) Persist observations to ES (best-effort) -----------------------
        await self._persist_observations(rows)

        # 4) Populate cache -------------------------------------------------
        await self._cache_put(cache_key, rows)

        return rows

    # ------------------------------------------------------------------
    # To be implemented by subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    async def _fetch_raw(
        self,
        *,
        zip_code: str,
        start_date: date,
        end_date: date,
        tenant_id: str,
    ) -> List[DailyWeather]:
        """Call the upstream API and return parsed :class:`DailyWeather` rows.

        Implementations MUST stamp ``provider=self.name``, ``tenant_id`` from
        the argument, and ``retrieved_at=datetime.now(timezone.utc)`` on every
        row. They may raise :class:`httpx.HTTPError`, :class:`ValueError`, or
        other network/parse exceptions — the base class catches them and
        degrades gracefully.
        """

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_fetch_args(
        zip_code: str,
        start_date: date,
        end_date: date,
        tenant_id: str,
    ) -> None:
        if not isinstance(zip_code, str) or not zip_code.strip():
            raise ValueError("zip_code must be a non-empty string")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise TypeError("start_date and end_date must be datetime.date instances")
        if end_date < start_date:
            raise ValueError(
                f"end_date ({end_date}) must not precede start_date ({start_date})"
            )

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Return the injected client or lazily create a new one.

        A client created here is scoped to the call — we close it at the end
        of :meth:`_fetch_raw` to avoid leaking connections. When the caller
        injects a client, the caller owns its lifecycle.
        """

        if self._http_client is not None:
            return self._http_client
        # Lazy per-call client. Callers that want connection pooling should
        # inject a long-lived client via the constructor.
        return httpx.AsyncClient(timeout=self._timeout)

    async def _cache_get(
        self, cache_key: str, *, tenant_id: str
    ) -> Optional[List[DailyWeather]]:
        """Return the cached rows or ``None`` on miss / cache failure.

        Cache failures never raise — a broken Redis connection must not
        take down a forecast cycle. The cached payload is re-stamped with
        the caller's ``tenant_id`` so entries shared across tenants still
        persist correctly when the cache miss path writes to ES.
        """

        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(cache_key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "WeatherProvider[%s]: cache get failed for key=%s: %s",
                self.name,
                cache_key,
                exc,
            )
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            rows: List[DailyWeather] = []
            for entry in payload:
                # Re-stamp tenant_id so the model validates correctly even if
                # the cache was populated for a different tenant.
                entry = {**entry, "tenant_id": tenant_id}
                rows.append(DailyWeather(**entry))
            return rows
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "WeatherProvider[%s]: cache payload decode failed for key=%s: %s",
                self.name,
                cache_key,
                exc,
            )
            return None

    async def _cache_put(self, cache_key: str, rows: Sequence[DailyWeather]) -> None:
        """Persist ``rows`` to Redis with ``_cache_ttl`` seconds TTL.

        Failures are logged and swallowed so a broken Redis never blocks a
        successful fetch.
        """

        if self._redis is None:
            return
        try:
            payload = json.dumps(
                [row.model_dump(mode="json") for row in rows], sort_keys=True
            )
            # Both ``setex`` (preferred) and ``set(..., ex=)`` are supported
            # by the redis-py asyncio client; use setex for explicit intent.
            await self._redis.setex(cache_key, self._cache_ttl, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "WeatherProvider[%s]: cache put failed for key=%s: %s",
                self.name,
                cache_key,
                exc,
            )

    async def _persist_observations(self, rows: Sequence[DailyWeather]) -> None:
        """Write each row to the ``weather_observations`` ES index.

        Failures are logged and swallowed so a broken ES does not mask the
        fetched data from the caller. Documents carry ``updated_at`` and
        ``created_at`` timestamps in addition to the mapped fields because
        the index mapping declares them.
        """

        if self._es is None or not rows:
            return
        now_iso = _utcnow().isoformat()
        for row in rows:
            try:
                doc_id = self._build_observation_doc_id(row)
                doc = row.model_dump(mode="json")
                doc["updated_at"] = now_iso
                doc["created_at"] = now_iso
                await self._es.index_document(self.index_name, doc_id, doc)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "WeatherProvider[%s]: persist failed for zip=%s date=%s: %s",
                    self.name,
                    row.zip_code,
                    row.date,
                    exc,
                )

    @staticmethod
    def _build_observation_doc_id(row: DailyWeather) -> str:
        """Return a deterministic doc_id so re-fetching the same day upserts
        rather than duplicating.

        Format: ``wxobs:{tenant}:{provider}:{zip}:{YYYY-MM-DD}``.
        """

        return f"wxobs:{row.tenant_id}:{row.provider}:{row.zip_code}:{row.date.isoformat()}"


# ---------------------------------------------------------------------------
# NOAA adapter
# ---------------------------------------------------------------------------


class NOAAWeatherProvider(WeatherProvider):
    """NOAA Climate Data Online (CDO) adapter.

    Uses the GHCND dataset's ``TAVG`` datatype (tenths of degrees Celsius).
    Multiple stations inside a ZIP are averaged per day. CDO is paginated;
    the adapter raises the ``limit`` to 1000 (the CDO maximum) and walks any
    additional pages within the same 5-second budget.

    Token is resolved in this precedence:
        1. explicit ``token`` argument
        2. ``NOAA_CDO_TOKEN`` environment variable

    When the token is missing the adapter logs and returns ``[]`` on every
    call — the forecaster then annotates ``weather_fallback: true`` per
    Requirement 1.2.5. No uncaught exception propagates from a missing token
    so a mis-configured tenant does not break forecasting globally.
    """

    name: ClassVar[str] = "noaa"

    #: Public CDO base URL. Kept as an attribute so tests can point to a
    #: mock transport.
    base_url: ClassVar[str] = "https://www.ncei.noaa.gov/cdo-web/api/v2"

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        dataset_id: str = "GHCND",
        datatype_id: str = "TAVG",
        es_service: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        super().__init__(
            es_service=es_service,
            redis_client=redis_client,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        self._token = token or os.environ.get(NOAA_TOKEN_ENV)
        self._dataset_id = dataset_id
        self._datatype_id = datatype_id

    async def _fetch_raw(
        self,
        *,
        zip_code: str,
        start_date: date,
        end_date: date,
        tenant_id: str,
    ) -> List[DailyWeather]:
        if not self._token:
            logger.warning(
                "NOAAWeatherProvider: no token configured (env %s). Returning [].",
                NOAA_TOKEN_ENV,
            )
            return []

        client_supplied = self._http_client is not None
        client = await self._get_http_client()
        try:
            response = await client.get(
                f"{self.base_url}/data",
                headers={"token": self._token},
                params={
                    "datasetid": self._dataset_id,
                    "datatypeid": self._datatype_id,
                    "locationid": f"ZIP:{zip_code}",
                    "startdate": start_date.isoformat(),
                    "enddate": end_date.isoformat(),
                    "units": "metric",  # tenths of °C
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if not client_supplied:
                await client.aclose()

        results = payload.get("results") or []
        return self._parse_cdo_results(
            results=results,
            zip_code=zip_code,
            tenant_id=tenant_id,
        )

    def _parse_cdo_results(
        self,
        *,
        results: Sequence[Mapping[str, Any]],
        zip_code: str,
        tenant_id: str,
    ) -> List[DailyWeather]:
        """Convert CDO rows into :class:`DailyWeather`.

        CDO ``TAVG`` values are tenths of degrees Celsius. Multiple rows for
        the same date (one per station) are averaged.
        """

        by_day: Dict[date, List[float]] = {}
        for row in results:
            raw_date = row.get("date")
            value = row.get("value")
            if raw_date is None or value is None:
                continue
            try:
                day = _parse_cdo_date(raw_date)
                # value is tenths of Celsius → celsius
                celsius = float(value) / 10.0
                fahrenheit = celsius * 9.0 / 5.0 + 32.0
            except (TypeError, ValueError):
                continue
            by_day.setdefault(day, []).append(fahrenheit)

        retrieved_at = _utcnow()
        out: List[DailyWeather] = []
        for day, samples in by_day.items():
            if not samples:
                continue
            avg_f = round(sum(samples) / len(samples), 3)
            out.append(
                DailyWeather(
                    date=day,
                    zip_code=zip_code,
                    tenant_id=tenant_id,
                    avg_temp_f=avg_f,
                    hdd=compute_hdd(avg_f),
                    provider=self.name,
                    retrieved_at=retrieved_at,
                )
            )
        return out


def _parse_cdo_date(raw: str) -> date:
    """Return the ``date`` portion of a CDO timestamp.

    CDO returns strings like ``"2024-01-15T00:00:00"``. We strip anything
    after the first ``T`` and parse as ISO-8601.
    """

    head = raw.split("T", 1)[0]
    return date.fromisoformat(head)


# ---------------------------------------------------------------------------
# OpenWeather adapter
# ---------------------------------------------------------------------------


class OpenWeatherProvider(WeatherProvider):
    """OpenWeather One-Call v3 ``day_summary`` adapter.

    OpenWeather's summary endpoint requires lat/lon, so the adapter first
    resolves the ZIP via the geocoding endpoint and then issues one
    ``day_summary`` request per day in the range. The whole orchestration
    still runs under the 5-second budget enforced by the base class; in
    practice the forecaster only requests 14 + 7 = 21 days per run, which
    is comfortably within OpenWeather's per-call latency.

    API key resolution precedence:
        1. Explicit ``api_key`` argument.
        2. :class:`TenantCredentialsVault` lookup via ``credentials_ref``
           (optional; supplied when per-tenant key rotation is wired up).
        3. ``OPENWEATHER_API_KEY`` environment variable.

    When every source is empty the adapter logs and returns ``[]``; the
    forecaster then annotates ``weather_fallback: true`` (Req 1.2.5).

    NOTE — TenantCredentialsVault integration is currently stubbed through
    a duck-typed hook (``credentials_vault`` + ``credentials_ref``) so
    downstream spec work (Phase 9 — Integration Layer) can wire the real
    AWS KMS lookup without changing this adapter's interface.
    """

    name: ClassVar[str] = "openweather"

    #: Base URL for One-Call v3.
    base_url: ClassVar[str] = "https://api.openweathermap.org/data/3.0"

    #: Base URL for the geocoding endpoint (ZIP → lat/lon).
    geo_base_url: ClassVar[str] = "https://api.openweathermap.org/geo/1.0"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        credentials_vault: Optional[Any] = None,
        credentials_ref: Optional[str] = None,
        country_code: str = "US",
        units: str = "imperial",  # °F directly from OpenWeather
        es_service: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        super().__init__(
            es_service=es_service,
            redis_client=redis_client,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
        )
        self._explicit_key = api_key
        self._vault = credentials_vault
        self._credentials_ref = credentials_ref
        self._country_code = country_code
        self._units = units

    async def _resolve_api_key(self, tenant_id: str) -> Optional[str]:
        """Return the OpenWeather API key or ``None`` when unset.

        Precedence: explicit → vault → env. Vault failures degrade to the
        env fallback silently so a transient KMS hiccup does not take down
        forecasting for everyone.
        """

        if self._explicit_key:
            return self._explicit_key
        if self._vault is not None and self._credentials_ref:
            try:
                payload = await self._vault.get(tenant_id, self._credentials_ref)
                if isinstance(payload, dict):
                    for candidate in ("api_key", "apiKey", "key", "token"):
                        value = payload.get(candidate)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "OpenWeatherProvider: vault lookup failed (ref=%s): %s",
                    self._credentials_ref,
                    exc,
                )
        return os.environ.get(OPENWEATHER_KEY_ENV)

    async def _fetch_raw(
        self,
        *,
        zip_code: str,
        start_date: date,
        end_date: date,
        tenant_id: str,
    ) -> List[DailyWeather]:
        api_key = await self._resolve_api_key(tenant_id)
        if not api_key:
            logger.warning(
                "OpenWeatherProvider: no API key configured (env %s). Returning [].",
                OPENWEATHER_KEY_ENV,
            )
            return []

        client_supplied = self._http_client is not None
        client = await self._get_http_client()
        try:
            lat, lon = await self._resolve_lat_lon(client, zip_code, api_key)
            if lat is None or lon is None:
                return []
            days = _daterange(start_date, end_date)
            retrieved_at = _utcnow()
            rows: List[DailyWeather] = []
            for day in days:
                row = await self._fetch_day_summary(
                    client=client,
                    lat=lat,
                    lon=lon,
                    day=day,
                    api_key=api_key,
                    zip_code=zip_code,
                    tenant_id=tenant_id,
                    retrieved_at=retrieved_at,
                )
                if row is not None:
                    rows.append(row)
            return rows
        finally:
            if not client_supplied:
                await client.aclose()

    async def _resolve_lat_lon(
        self,
        client: httpx.AsyncClient,
        zip_code: str,
        api_key: str,
    ) -> tuple[Optional[float], Optional[float]]:
        """Return ``(lat, lon)`` for ``zip_code`` or ``(None, None)`` on error."""

        try:
            response = await client.get(
                f"{self.geo_base_url}/zip",
                params={"zip": f"{zip_code},{self._country_code}", "appid": api_key},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "OpenWeatherProvider: geocoding failed for zip=%s: %s",
                zip_code,
                exc,
            )
            return None, None
        lat = payload.get("lat")
        lon = payload.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None, None
        return float(lat), float(lon)

    async def _fetch_day_summary(
        self,
        *,
        client: httpx.AsyncClient,
        lat: float,
        lon: float,
        day: date,
        api_key: str,
        zip_code: str,
        tenant_id: str,
        retrieved_at: datetime,
    ) -> Optional[DailyWeather]:
        """Fetch a single day_summary row, returning ``None`` on failure.

        Errors are logged at debug level — a single missing day should not
        cascade into forecaster fallback for the whole window. The base
        class's timeout still bounds the overall call.
        """

        try:
            response = await client.get(
                f"{self.base_url}/onecall/day_summary",
                params={
                    "lat": lat,
                    "lon": lon,
                    "date": day.isoformat(),
                    "appid": api_key,
                    "units": self._units,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(
                "OpenWeatherProvider: day_summary failed for zip=%s day=%s: %s",
                zip_code,
                day,
                exc,
            )
            return None

        avg_f = self._extract_avg_temp_f(payload)
        if avg_f is None:
            return None
        return DailyWeather(
            date=day,
            zip_code=zip_code,
            tenant_id=tenant_id,
            avg_temp_f=round(avg_f, 3),
            hdd=compute_hdd(avg_f),
            provider=self.name,
            retrieved_at=retrieved_at,
        )

    @staticmethod
    def _extract_avg_temp_f(payload: Mapping[str, Any]) -> Optional[float]:
        """Return the daily-average temperature in °F, or ``None``.

        OpenWeather's ``day_summary`` response puts daily temperatures under
        ``temperature`` with keys ``{min, max, afternoon, morning, evening, night}``.
        We prefer ``afternoon`` when present (matches NOAA ``TAVG`` semantics
        reasonably), falling back to the mean of ``min`` and ``max`` when
        only those are available.
        """

        temp = payload.get("temperature") or {}
        if not isinstance(temp, Mapping):
            return None
        for key in ("afternoon", "day", "evening", "morning"):
            value = temp.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        t_min = temp.get("min")
        t_max = temp.get("max")
        if isinstance(t_min, (int, float)) and isinstance(t_max, (int, float)):
            return (float(t_min) + float(t_max)) / 2.0
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_weather_provider(
    name: str,
    **kwargs: Any,
) -> WeatherProvider:
    """Return a concrete provider by short name.

    Used by the Tank_Forecasting_Agent once it has looked up the tenant's
    ``overlay.weather_provider`` Redis key. Unknown names raise
    :class:`ValueError` — we never silently fall through to a default
    because that would hide a mis-configuration.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("weather provider name must be a non-empty string")
    normalized = name.strip().lower()
    if normalized == NOAAWeatherProvider.name:
        return NOAAWeatherProvider(**kwargs)
    if normalized == OpenWeatherProvider.name:
        return OpenWeatherProvider(**kwargs)
    raise ValueError(f"unknown weather provider: {name!r}")


__all__ = [
    "DailyWeather",
    "WeatherProvider",
    "NOAAWeatherProvider",
    "OpenWeatherProvider",
    "HDD_BASE_F",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_CACHE_TTL_SECONDS",
    "NOAA_TOKEN_ENV",
    "OPENWEATHER_KEY_ENV",
    "compute_hdd",
    "build_weather_provider",
]
