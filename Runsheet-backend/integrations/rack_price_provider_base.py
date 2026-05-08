"""
Rack-price provider abstraction and concrete adapters.

Capability 8 / Requirements 8.2.1–8.2.4 of the fuel-ops hardening spec introduce
a pluggable ``Rack_Price_Provider`` interface so the Sourcing_Recommender can
feed live rack prices (OPIS) or tenant-uploaded CSV fallback prices into its
ranking loop. This module defines:

* :class:`RackPrice` — a strict Pydantic model matching the ``rack_prices`` ES
  mapping 1:1 so ``model_dump(mode="json")`` can be indexed directly (fields:
  ``rack_price_id``, ``tenant_id``, ``terminal_id``, ``product_code``,
  ``price_per_gallon_usd``, ``branded_flag``, ``supplier_brand``, ``provider``,
  ``effective_at``, ``retrieved_at``). Legacy Nigerian aliases (AGO, PMS, ATK,
  LPG) are canonicalized on construction so persistence is always in the
  tenant-agnostic US catalog form (Requirement 6.1.4).

* :class:`RackPriceProvider` — the abstract base class exposing a single
  ``async get_prices(terminal_ids, product_codes, as_of, *, tenant_id)`` entry
  point. The base class owns the shared plumbing:

    1. Redis cache lookup per (provider, terminal_id, product_code,
       bucket_15min) with a 900-second TTL (Requirement 8.2.4).
    2. Cross-product of ``terminal_ids × product_codes``, serving cache hits
       directly and issuing a single provider call for any pairs that miss.
    3. Per-provider call to ``_fetch_raw`` with a 10-second httpx budget
       (Requirement 8.2.5 — enforced by the Sourcing_Recommender; the base
       class guards it here too so every adapter is uniformly bounded).
    4. Graceful degradation: network / parse / timeout failures are logged at
       warning level and the method returns an empty list for the missing
       pairs. The Sourcing_Recommender then falls back to the most-recent
       cached price per (Task 7.4) and annotates the recommendation with
       ``rack_price_fallback: true`` (Requirement 8.2.5).
    5. Population of the Redis cache with each fetched row.

* :class:`OPISRackPriceProvider` — primary adapter for the OPIS B2B rack-price
  feed. Authenticates via bearer token (``OPIS_API_KEY`` env var or an
  injected value), optionally HMAC-SHA256-signs outgoing requests when a
  secret is provided, and parses OPIS-style JSON responses.

* :class:`CSVFallbackRackPriceProvider` — secondary adapter that reads a
  tenant-uploaded CSV (stored in S3 via :class:`services.file_storage_service.
  FileStorageService`) and filters rows matching the request. Suitable for
  tenants who do not have an OPIS contract and instead upload a daily rack
  snapshot manually.

The providers never mutate caller state and never swallow programmer errors
(``ValueError``, ``TypeError``, etc.). Only *network*, *HTTP*, and *parse*
errors degrade gracefully to an empty list.

Validates: Requirements 8.2.1, 8.2.2, 8.2.4.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import datetime as _dt
import hashlib
import hmac
import io
import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)
from services.external_call_tracing import (
    CircuitBreaker,
    CircuitOpenError,
    default_circuit_breaker,
    trace_external_call,
)
from services.metrics import fuelops_rack_price_provider_calls_total

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: HTTP timeout for every provider call, per Requirement 8.2.5.
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 10.0

#: Redis TTL for cached prices, per Requirement 8.2.4 (900 seconds).
DEFAULT_CACHE_TTL_SECONDS: int = 900

#: Width of the as_of cache bucket, in minutes. 15-minute bucketing collapses
#: bursts of Sourcing_Recommender invocations within the same quarter hour
#: onto a shared cache entry (Requirement 8.2.4).
CACHE_BUCKET_MINUTES: int = 15

#: Environment variable names honored by the stock adapters.
OPIS_API_KEY_ENV: str = "OPIS_API_KEY"
OPIS_API_SECRET_ENV: str = "OPIS_API_SECRET"
OPIS_BASE_URL_ENV: str = "OPIS_BASE_URL"

#: Prefix for all Redis keys used by rack-price providers.
_CACHE_KEY_PREFIX: str = "rack_price"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class RackPrice(BaseModel):
    """A single rack-price observation for a (tenant, terminal, product) tuple.

    Persisted 1:1 to the ``rack_prices`` ES index. Field order and types match
    the mapping declared in :mod:`fuel.services.fuel_ops_es_mappings`, so
    ``model_dump(mode="json")`` produces a valid indexing payload with no
    post-processing.

    ``product_code`` is canonicalized at validation time so legacy aliases
    like ``"AGO"`` are normalized to ``"DIESEL_2"`` before the row ever
    touches Redis or ES (Requirement 6.1.4).
    """

    model_config = ConfigDict(extra="forbid")

    rack_price_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    terminal_id: str = Field(..., min_length=1)
    product_code: str = Field(..., min_length=1)
    price_per_gallon_usd: float = Field(..., ge=0.0)
    branded_flag: bool = Field(default=False)
    supplier_brand: Optional[str] = Field(default=None)
    provider: str = Field(..., min_length=1)
    effective_at: datetime = Field(...)
    retrieved_at: datetime = Field(...)

    @field_validator(
        "rack_price_id",
        "tenant_id",
        "terminal_id",
        "provider",
        mode="before",
    )
    @classmethod
    def _strip_required_strings(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("product_code", mode="before")
    @classmethod
    def _canonicalize_product_code(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        # Canonicalization happens before Pydantic's min_length check so
        # alias resolution never accidentally rejects a legacy code.
        return canonicalize(value)

    @field_validator("supplier_brand", mode="before")
    @classmethod
    def _strip_supplier_brand(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` stamp used for retrieved_at."""

    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` coerced to UTC.

    Naive datetimes are assumed to already represent UTC (this is what the
    ES mapping persists). Timezone-aware values are converted with
    ``astimezone`` so the bucket math is consistent regardless of caller
    input.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor_to_bucket(value: datetime, bucket_minutes: int = CACHE_BUCKET_MINUTES) -> datetime:
    """Floor ``value`` to the nearest ``bucket_minutes``-aligned UTC minute.

    Seconds and microseconds are zeroed so the bucket is stable across
    callers with sub-second clock drift.
    """

    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    utc = _ensure_utc(value)
    floored_minute = (utc.minute // bucket_minutes) * bucket_minutes
    return utc.replace(minute=floored_minute, second=0, microsecond=0)


def _bucket_label(as_of: datetime, bucket_minutes: int = CACHE_BUCKET_MINUTES) -> str:
    """Return the canonical cache-key bucket label for ``as_of``.

    Format: ``YYYY-MM-DDTHH:MM`` with UTC minutes floored to the bucket.
    """

    floored = _floor_to_bucket(as_of, bucket_minutes)
    return floored.strftime("%Y-%m-%dT%H:%M")


def _build_cache_key(
    provider: str,
    terminal_id: str,
    product_code: str,
    as_of: datetime,
    *,
    bucket_minutes: int = CACHE_BUCKET_MINUTES,
) -> str:
    """Return the canonical Redis cache key for a (provider, terminal, product,
    as_of bucket) tuple.

    Format: ``rack_price:{provider}:{terminal_id}:{product_code}:{bucket}``
    as mandated by Requirement 8.2.4.
    """

    bucket = _bucket_label(as_of, bucket_minutes)
    return f"{_CACHE_KEY_PREFIX}:{provider}:{terminal_id}:{product_code}:{bucket}"


def _dedupe_preserving_order(values: Iterable[str]) -> List[str]:
    """Return ``values`` with duplicates removed, preserving first-seen order."""

    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _coerce_canonical_products(values: Iterable[str]) -> List[str]:
    """Canonicalize and deduplicate product codes.

    Unknown codes are dropped with a debug log so a single misconfigured
    product in a caller's list does not blow up the whole request. Empty
    or non-string entries are also skipped.
    """

    out: List[str] = []
    seen: set[str] = set()
    for entry in values:
        if not isinstance(entry, str) or not entry.strip():
            continue
        try:
            canonical = canonicalize(entry)
        except UnknownFuelProductError:
            logger.debug(
                "RackPriceProvider: dropping unknown product_code %r from request",
                entry,
            )
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class RackPriceProvider(ABC):
    """Abstract base for rack-price adapters.

    Subclasses implement :meth:`_fetch_raw` to produce a list of
    :class:`RackPrice` rows for a cross-product of terminal ids and product
    codes at a given ``as_of`` timestamp. The base class wraps every call
    with:

        * Redis cache lookup + populate with 900-second TTL, keyed by
          ``rack_price:{provider}:{terminal_id}:{product_code}:{bucket_15min}``
          (Requirement 8.2.4).
        * Cross-product expansion of ``terminal_ids × product_codes`` so a
          sparse query can reuse cached entries from prior calls.
        * 10-second httpx budget on :meth:`_fetch_raw` (Requirement 8.2.5).
        * Graceful fallback to ``[]`` on network / parse / timeout errors so
          the recommender can fall through to its own fallback path.

    Dependencies (``redis_client``, ``http_client``) are injected at
    construction time so tests can run without patching module-level
    singletons. Both are optional — passing ``None`` simply disables that
    layer.
    """

    #: Short identifier (``"opis"``, ``"csv_fallback"``) stamped on every
    #: :class:`RackPrice` row and used as the first component of the cache
    #: key. Concrete providers MUST override this.
    name: ClassVar[str] = "abstract"

    def __init__(
        self,
        *,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        cache_bucket_minutes: int = CACHE_BUCKET_MINUTES,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        if cache_bucket_minutes <= 0:
            raise ValueError("cache_bucket_minutes must be positive")
        self._redis = redis_client
        self._http_client = http_client
        self._timeout = float(timeout_seconds)
        self._cache_ttl = int(cache_ttl_seconds)
        self._cache_bucket_minutes = int(cache_bucket_minutes)
        self._circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else default_circuit_breaker
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_prices(
        self,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        *,
        tenant_id: str,
    ) -> List[RackPrice]:
        """Return :class:`RackPrice` rows for the cross-product of
        ``terminal_ids`` and ``product_codes`` as of ``as_of``.

        Orchestration order:

            1. Validate args (programmer errors raise immediately).
            2. Canonicalize product codes; drop unknowns with a debug log.
            3. For every (terminal_id, product_code) pair: check the Redis
               cache. Accumulate misses.
            4. If any pairs missed, call :meth:`_fetch_raw` under a
               10-second budget with the miss set.
            5. Cache every fetched row with a 900-second TTL.
            6. Merge cached + freshly-fetched rows and return in a stable
               order (terminal_id asc, product_code asc).

        Network / provider failures degrade the miss set to no rows — cached
        hits still propagate back to the caller. The Sourcing_Recommender
        then falls back to its most-recent-cached-price path per Task 7.4.
        """

        self._validate_args(terminal_ids, product_codes, as_of, tenant_id)

        canonical_terminals = _dedupe_preserving_order(
            [t for t in terminal_ids if isinstance(t, str) and t.strip()]
        )
        canonical_products = _coerce_canonical_products(product_codes)

        if not canonical_terminals or not canonical_products:
            return []

        cached: List[RackPrice] = []
        misses_terminals: List[str] = []
        misses_products: List[str] = []
        seen_misses: set[Tuple[str, str]] = set()

        for terminal_id in canonical_terminals:
            for product_code in canonical_products:
                cached_row = await self._cache_get(
                    terminal_id=terminal_id,
                    product_code=product_code,
                    as_of=as_of,
                    tenant_id=tenant_id,
                )
                if cached_row is not None:
                    cached.append(cached_row)
                    continue
                pair = (terminal_id, product_code)
                if pair in seen_misses:
                    continue
                seen_misses.add(pair)
                misses_terminals.append(terminal_id)
                misses_products.append(product_code)

        fetched: List[RackPrice] = []
        if seen_misses:
            fetched = await self._fetch_with_timeout(
                terminal_ids=_dedupe_preserving_order(misses_terminals),
                product_codes=_dedupe_preserving_order(misses_products),
                as_of=as_of,
                tenant_id=tenant_id,
            )
            for row in fetched:
                # Re-stamp tenant_id defensively: subclasses are trusted but
                # a mis-stamped row would poison the cache across tenants.
                if row.tenant_id != tenant_id:
                    logger.warning(
                        "RackPriceProvider[%s]: dropping row with foreign tenant_id "
                        "%r (expected %r)",
                        self.name,
                        row.tenant_id,
                        tenant_id,
                    )
                    continue
                await self._cache_put(row=row, as_of=as_of)

        merged = cached + [
            row for row in fetched if row.tenant_id == tenant_id
        ]
        merged.sort(key=lambda r: (r.terminal_id, r.product_code, r.effective_at))
        return merged

    # ------------------------------------------------------------------
    # To be implemented by subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    async def _fetch_raw(
        self,
        *,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> List[RackPrice]:
        """Call the upstream source and return parsed :class:`RackPrice` rows.

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
    def _validate_args(
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime.datetime instance")
        if terminal_ids is None or product_codes is None:
            raise TypeError("terminal_ids and product_codes must be sequences")
        if isinstance(terminal_ids, (str, bytes)):
            raise TypeError("terminal_ids must be a sequence, not a string")
        if isinstance(product_codes, (str, bytes)):
            raise TypeError("product_codes must be a sequence, not a string")

    async def _fetch_with_timeout(
        self,
        *,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> List[RackPrice]:
        """Invoke :meth:`_fetch_raw` under the configured timeout.

        Wraps the call in :func:`trace_external_call` so every attempt
        emits a structured log event (``tenant_id``, ``provider``,
        ``operation=fetch_prices``, ``duration_ms``, ``status``, and
        — on failure — ``error_code``) and feeds the per-
        ``(tenant_id, provider)`` circuit breaker (Task 12.9 /
        Req 10.4.3). The breaker flips to OPEN after 5 consecutive
        upstream failures and resets 60s later; while OPEN the wrapper
        raises :class:`CircuitOpenError` before ``_fetch_raw`` is ever
        invoked, preserving upstream budget and keeping the
        recommender's ``rack_price_fallback: true`` path honest.
        """

        try:
            async with trace_external_call(
                tenant_id=tenant_id,
                provider=self.name,
                operation="fetch_prices",
                circuit_breaker=self._circuit_breaker,
                metric=fuelops_rack_price_provider_calls_total,
            ) as call:
                try:
                    return await asyncio.wait_for(
                        self._fetch_raw(
                            terminal_ids=terminal_ids,
                            product_codes=product_codes,
                            as_of=as_of,
                            tenant_id=tenant_id,
                        ),
                        timeout=self._timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "RackPriceProvider[%s]: timed out after %.1fs for "
                        "terminals=%s products=%s",
                        self.name,
                        self._timeout,
                        terminal_ids,
                        product_codes,
                    )
                    # Re-raise so the wrapper records a failure against
                    # the breaker and emits ``status="timeout"``.
                    raise
                except (httpx.HTTPError, httpx.RequestError) as exc:
                    logger.warning(
                        "RackPriceProvider[%s]: HTTP error "
                        "(terminals=%s products=%s): %s",
                        self.name,
                        terminal_ids,
                        product_codes,
                        exc,
                    )
                    call.set_error_code("http_error")
                    raise
                except Exception as exc:  # pragma: no cover - defensive catch-all
                    logger.warning(
                        "RackPriceProvider[%s]: unexpected error "
                        "(terminals=%s products=%s): %s",
                        self.name,
                        terminal_ids,
                        product_codes,
                        exc,
                    )
                    raise
        except CircuitOpenError:
            # The breaker is open for this (tenant, provider) pair.
            # Return an empty miss-list so the Sourcing_Recommender
            # falls back to its most-recent-cached path and annotates
            # the recommendation with ``rack_price_fallback: true``
            # (Req 8.2.5). The wrapper already emitted the
            # ``external_call_rejected`` structured log event.
            return []
        except asyncio.TimeoutError:
            return []
        except (httpx.HTTPError, httpx.RequestError):
            return []
        except Exception:  # pragma: no cover - defensive
            return []

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Return the injected client or lazily create a new one."""

        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=self._timeout)

    async def _cache_get(
        self,
        *,
        terminal_id: str,
        product_code: str,
        as_of: datetime,
        tenant_id: str,
    ) -> Optional[RackPrice]:
        """Return the cached :class:`RackPrice` or ``None`` on miss.

        Cache failures never raise — a broken Redis connection must not
        take down a recommender cycle. The cached payload is re-stamped
        with the caller's ``tenant_id`` so entries shared across tenants
        still validate correctly.
        """

        if self._redis is None:
            return None
        cache_key = _build_cache_key(
            self.name, terminal_id, product_code, as_of,
            bucket_minutes=self._cache_bucket_minutes,
        )
        try:
            raw = await self._redis.get(cache_key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RackPriceProvider[%s]: cache get failed for key=%s: %s",
                self.name,
                cache_key,
                exc,
            )
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(payload, Mapping):
                return None
            # Re-stamp tenant_id so the model validates correctly even if the
            # cache was populated for a different tenant in the same key
            # (we never key by tenant — product/terminal/bucket is enough).
            merged = {**payload, "tenant_id": tenant_id}
            return RackPrice(**merged)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RackPriceProvider[%s]: cache payload decode failed for key=%s: %s",
                self.name,
                cache_key,
                exc,
            )
            return None

    async def _cache_put(self, *, row: RackPrice, as_of: datetime) -> None:
        """Persist ``row`` to Redis under its canonical key.

        Failures are logged and swallowed so a broken Redis never blocks a
        successful fetch. The payload is serialized as JSON with sorted
        keys so test assertions have a stable representation.
        """

        if self._redis is None:
            return
        cache_key = _build_cache_key(
            self.name,
            row.terminal_id,
            row.product_code,
            as_of,
            bucket_minutes=self._cache_bucket_minutes,
        )
        try:
            payload = json.dumps(row.model_dump(mode="json"), sort_keys=True)
            # Both ``setex`` (preferred) and ``set(..., ex=)`` are supported
            # by the redis-py asyncio client; use setex for explicit intent.
            await self._redis.setex(cache_key, self._cache_ttl, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RackPriceProvider[%s]: cache put failed for key=%s: %s",
                self.name,
                cache_key,
                exc,
            )


# ---------------------------------------------------------------------------
# OPIS adapter
# ---------------------------------------------------------------------------


class OPISRackPriceProvider(RackPriceProvider):
    """OPIS B2B rack-price adapter.

    Uses a signed HTTP request to an OPIS-compatible JSON endpoint. The
    endpoint contract this adapter speaks is intentionally minimal:

        ``GET {base_url}/rack/prices``
            Query parameters:
                * ``terminal_ids``   — comma-separated list.
                * ``product_codes``  — comma-separated list (canonical form).
                * ``as_of``          — ISO-8601 timestamp.
            Headers:
                * ``Authorization: Bearer {api_key}``
                * ``X-OPIS-Signature: {hmac_sha256(base64)}`` when an
                  ``api_secret`` is configured.
            Response body (200 OK):

                .. code-block:: json

                    {
                      "prices": [
                        {
                          "terminal_id": "t_opis_01",
                          "product_code": "DIESEL_2",
                          "price_per_gallon_usd": 3.245,
                          "branded_flag": false,
                          "supplier_brand": null,
                          "effective_at": "2024-10-15T12:15:00Z"
                        }
                      ]
                    }

    Credentials resolution precedence:

        1. Explicit ``api_key`` / ``api_secret`` constructor arguments.
        2. :class:`services.credentials_vault.TenantCredentialsVault` lookup
           via ``credentials_ref`` (optional; wired up once Capability 5
           lands in production).
        3. ``OPIS_API_KEY`` / ``OPIS_API_SECRET`` environment variables.

    When the API key is missing the adapter logs and returns ``[]`` for every
    call — the Sourcing_Recommender then annotates the recommendation with
    ``rack_price_fallback: true`` (Requirement 8.2.5). No uncaught exception
    propagates from a missing credential so a mis-configured tenant does not
    break sourcing globally.
    """

    name: ClassVar[str] = "opis"

    #: Default OPIS base URL. Kept as an attribute so tests can point to a
    #: mock transport via ``base_url=...`` at construction time.
    DEFAULT_BASE_URL: ClassVar[str] = "https://api.opisnet.com/v1"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        credentials_vault: Optional[Any] = None,
        credentials_ref: Optional[str] = None,
        redis_client: Optional[Any] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        super().__init__(
            redis_client=redis_client,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
            circuit_breaker=circuit_breaker,
        )
        self._explicit_api_key = api_key
        self._explicit_api_secret = api_secret
        self._base_url = (base_url or os.environ.get(OPIS_BASE_URL_ENV)
                          or self.DEFAULT_BASE_URL).rstrip("/")
        self._vault = credentials_vault
        self._credentials_ref = credentials_ref

    async def _resolve_credentials(
        self, tenant_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return ``(api_key, api_secret)`` or ``(None, None)`` when absent."""

        api_key: Optional[str] = self._explicit_api_key
        api_secret: Optional[str] = self._explicit_api_secret

        if (not api_key or not api_secret) and self._vault is not None and self._credentials_ref:
            try:
                payload = await self._vault.get(tenant_id, self._credentials_ref)
                if isinstance(payload, Mapping):
                    if not api_key:
                        for candidate in ("api_key", "apiKey", "token"):
                            value = payload.get(candidate)
                            if isinstance(value, str) and value.strip():
                                api_key = value.strip()
                                break
                    if not api_secret:
                        for candidate in ("api_secret", "apiSecret", "secret"):
                            value = payload.get(candidate)
                            if isinstance(value, str) and value.strip():
                                api_secret = value.strip()
                                break
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "OPISRackPriceProvider: vault lookup failed (ref=%s): %s",
                    self._credentials_ref,
                    exc,
                )

        if not api_key:
            api_key = os.environ.get(OPIS_API_KEY_ENV)
        if not api_secret:
            api_secret = os.environ.get(OPIS_API_SECRET_ENV)

        return api_key, api_secret

    async def _fetch_raw(
        self,
        *,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> List[RackPrice]:
        api_key, api_secret = await self._resolve_credentials(tenant_id)
        if not api_key:
            logger.warning(
                "OPISRackPriceProvider: no API key configured (env %s). Returning [].",
                OPIS_API_KEY_ENV,
            )
            return []

        params = {
            "terminal_ids": ",".join(terminal_ids),
            "product_codes": ",".join(product_codes),
            "as_of": _ensure_utc(as_of).isoformat(),
        }
        headers = {"Authorization": f"Bearer {api_key}"}

        if api_secret:
            headers["X-OPIS-Signature"] = _hmac_sha256_sign(
                api_secret, params
            )

        client_supplied = self._http_client is not None
        client = await self._get_http_client()
        try:
            response = await client.get(
                f"{self._base_url}/rack/prices",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if not client_supplied:
                await client.aclose()

        return self._parse_opis_payload(
            payload=payload,
            tenant_id=tenant_id,
            retrieved_at=_utcnow(),
        )

    def _parse_opis_payload(
        self,
        *,
        payload: Any,
        tenant_id: str,
        retrieved_at: datetime,
    ) -> List[RackPrice]:
        """Convert an OPIS-shaped JSON body into :class:`RackPrice` rows.

        Malformed individual entries are skipped with a debug log so one bad
        row does not poison the whole batch. The caller's provider-level
        fallback still handles totally-empty results.
        """

        if not isinstance(payload, Mapping):
            return []
        rows = payload.get("prices")
        if not isinstance(rows, list):
            return []

        out: List[RackPrice] = []
        for entry in rows:
            if not isinstance(entry, Mapping):
                continue
            try:
                effective_at = _parse_iso8601(entry.get("effective_at"))
                row = RackPrice(
                    rack_price_id=f"rp_{uuid4()}",
                    tenant_id=tenant_id,
                    terminal_id=str(entry["terminal_id"]),
                    product_code=str(entry["product_code"]),
                    price_per_gallon_usd=float(entry["price_per_gallon_usd"]),
                    branded_flag=bool(entry.get("branded_flag", False)),
                    supplier_brand=entry.get("supplier_brand"),
                    provider=self.name,
                    effective_at=effective_at,
                    retrieved_at=retrieved_at,
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug(
                    "OPISRackPriceProvider: dropping malformed row %r: %s",
                    entry,
                    exc,
                )
                continue
            out.append(row)
        return out


def _hmac_sha256_sign(secret: str, params: Mapping[str, str]) -> str:
    """Return the HMAC-SHA256 signature over ``params`` as base64.

    Parameters are serialized as ``key=value`` pairs sorted by key and
    joined with ``&`` so the signature is deterministic regardless of
    dictionary iteration order.
    """

    message = "&".join(f"{key}={params[key]}" for key in sorted(params))
    digest = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _parse_iso8601(value: Any) -> datetime:
    """Parse ``value`` as an ISO-8601 timestamp with UTC fallback.

    Accepts the trailing ``Z`` shorthand that OPIS and many other APIs
    emit. Raises :class:`ValueError` on anything that isn't parseable so
    the OPIS row loop can skip the entry.
    """

    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected ISO-8601 string, got {value!r}")
    stripped = value.strip()
    # ``datetime.fromisoformat`` in Python 3.11+ handles "Z", but we support
    # older runtimes by normalizing here.
    if stripped.endswith("Z"):
        stripped = stripped[:-1] + "+00:00"
    parsed = datetime.fromisoformat(stripped)
    return _ensure_utc(parsed)


# ---------------------------------------------------------------------------
# CSV-fallback adapter
# ---------------------------------------------------------------------------


#: Required columns for the CSV fallback format. Extra columns are ignored.
CSV_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "terminal_id",
        "product_code",
        "price_per_gallon_usd",
        "effective_at",
    }
)

#: Truthy/falsy string markers accepted for the ``branded_flag`` column.
_CSV_TRUE = frozenset({"true", "t", "yes", "y", "1"})
_CSV_FALSE = frozenset({"false", "f", "no", "n", "0", ""})


#: Async loader signature used by :class:`CSVFallbackRackPriceProvider`.
#:
#: Callers wire this to :class:`services.file_storage_service.FileStorageService`
#: (which exposes a sync ``get`` method) by wrapping the call in a coroutine::
#:
#:     async def _load_csv(tenant_id: str) -> bytes:
#:         # Runs ``file_storage.get(tenant_id, csv_ref_for(tenant_id))`` in a
#:         # thread pool because FileStorageService is synchronous.
#:         return await asyncio.to_thread(
#:             file_storage.get, tenant_id, csv_ref_for(tenant_id)
#:         )
CSVLoader = Callable[[str], Awaitable[bytes]]


class CSVFallbackRackPriceProvider(RackPriceProvider):
    """Tenant-uploaded-CSV rack-price adapter.

    Reads a tenant-scoped CSV blob from S3 (via an injected async loader) and
    returns rows matching the requested terminal ids, product codes, and
    ``as_of``. Intended for tenants who do not have an OPIS subscription and
    upload a daily rack snapshot manually through the admin UI.

    CSV format (header row required; column order is flexible):

        ``terminal_id,product_code,price_per_gallon_usd,branded_flag,effective_at,supplier_brand``

    * ``terminal_id`` — string matching a :class:`fuel.terminal_models.Terminal`
      in the requesting tenant.
    * ``product_code`` — canonical or legacy-alias product code. Canonicalized
      on parse (Requirement 6.1.4).
    * ``price_per_gallon_usd`` — non-negative float.
    * ``branded_flag`` — optional boolean. Accepts ``true/false``, ``yes/no``,
      ``1/0``, or blank (defaults to ``false``).
    * ``effective_at`` — ISO-8601 timestamp. Rows with ``effective_at`` later
      than ``as_of`` are discarded.
    * ``supplier_brand`` — optional string, nullable when blank.

    Malformed rows are skipped with a debug log so a single typo does not
    drop the whole file. If the CSV is missing, empty, or entirely
    malformed, the provider returns ``[]`` and the Sourcing_Recommender
    falls back to the most-recent-cached path (Task 7.4).
    """

    name: ClassVar[str] = "csv_fallback"

    def __init__(
        self,
        *,
        csv_loader: CSVLoader,
        redis_client: Optional[Any] = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        encoding: str = "utf-8",
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        if not callable(csv_loader):
            raise TypeError("csv_loader must be an async callable")
        super().__init__(
            redis_client=redis_client,
            http_client=None,
            timeout_seconds=timeout_seconds,
            cache_ttl_seconds=cache_ttl_seconds,
            circuit_breaker=circuit_breaker,
        )
        self._csv_loader = csv_loader
        self._encoding = encoding

    async def _fetch_raw(
        self,
        *,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> List[RackPrice]:
        try:
            blob = await self._csv_loader(tenant_id)
        except FileNotFoundError:
            logger.info(
                "CSVFallbackRackPriceProvider: no rack-price CSV uploaded for tenant %s",
                tenant_id,
            )
            return []
        except PermissionError as exc:
            # A cross-tenant file_ref violation is a programmer error (the
            # loader should enforce tenant prefixing) but we don't want it
            # to crash sourcing — log loudly and return empty.
            logger.error(
                "CSVFallbackRackPriceProvider: permission denied loading CSV "
                "for tenant %s: %s",
                tenant_id,
                exc,
            )
            return []
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "CSVFallbackRackPriceProvider: CSV load failed for tenant %s: %s",
                tenant_id,
                exc,
            )
            return []

        if not blob:
            return []

        try:
            text = blob.decode(self._encoding)
        except UnicodeDecodeError as exc:
            logger.warning(
                "CSVFallbackRackPriceProvider: CSV decode failed for tenant %s: %s",
                tenant_id,
                exc,
            )
            return []

        return self._parse_csv(
            text=text,
            terminal_ids=set(terminal_ids),
            product_codes=set(product_codes),
            as_of=_ensure_utc(as_of),
            tenant_id=tenant_id,
            retrieved_at=_utcnow(),
        )

    def _parse_csv(
        self,
        *,
        text: str,
        terminal_ids: set[str],
        product_codes: set[str],
        as_of: datetime,
        tenant_id: str,
        retrieved_at: datetime,
    ) -> List[RackPrice]:
        """Return :class:`RackPrice` rows from a CSV body.

        Filters:
            * Row is dropped if ``terminal_id`` is not in ``terminal_ids``.
            * Row is dropped if canonical ``product_code`` is not in
              ``product_codes``.
            * Row is dropped if ``effective_at`` is later than ``as_of``.

        Among rows matching the same (terminal_id, product_code), the one
        with the latest ``effective_at`` wins — callers expect a single
        quote per pair.
        """

        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return []
        missing = CSV_REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            logger.warning(
                "CSVFallbackRackPriceProvider: CSV missing required columns %s",
                sorted(missing),
            )
            return []

        best: Dict[Tuple[str, str], RackPrice] = {}
        for row_number, raw in enumerate(reader, start=2):  # row 1 is the header
            try:
                terminal_id = (raw.get("terminal_id") or "").strip()
                product_raw = (raw.get("product_code") or "").strip()
                if not terminal_id or not product_raw:
                    continue
                if terminal_id not in terminal_ids:
                    continue
                try:
                    product_code = canonicalize(product_raw)
                except UnknownFuelProductError:
                    continue
                if product_code not in product_codes:
                    continue
                effective_at = _parse_iso8601(raw.get("effective_at"))
                if effective_at > as_of:
                    continue
                price_str = (raw.get("price_per_gallon_usd") or "").strip()
                if not price_str:
                    continue
                price = float(price_str)
                if price < 0:
                    raise ValueError(
                        f"price_per_gallon_usd must be >= 0, got {price}"
                    )
                branded_flag = _parse_bool(raw.get("branded_flag"))
                supplier_brand = (raw.get("supplier_brand") or "").strip() or None

                row = RackPrice(
                    rack_price_id=f"rp_{uuid4()}",
                    tenant_id=tenant_id,
                    terminal_id=terminal_id,
                    product_code=product_code,
                    price_per_gallon_usd=price,
                    branded_flag=branded_flag,
                    supplier_brand=supplier_brand,
                    provider=self.name,
                    effective_at=effective_at,
                    retrieved_at=retrieved_at,
                )
            except (ValueError, TypeError, KeyError) as exc:
                logger.debug(
                    "CSVFallbackRackPriceProvider: dropping malformed row %d: %s",
                    row_number,
                    exc,
                )
                continue

            key = (terminal_id, product_code)
            previous = best.get(key)
            if previous is None or row.effective_at > previous.effective_at:
                best[key] = row

        return list(best.values())


def _parse_bool(value: Any) -> bool:
    """Parse a flexible CSV truthiness value.

    Raises :class:`ValueError` when the value is unrecognized so the caller
    can log the row and skip it rather than silently persisting an
    incorrect default.
    """

    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if not isinstance(value, str):
        raise ValueError(f"expected string boolean, got {type(value).__name__}")
    lowered = value.strip().lower()
    if lowered in _CSV_TRUE:
        return True
    if lowered in _CSV_FALSE:
        return False
    raise ValueError(f"unrecognized boolean token: {value!r}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_rack_price_provider(
    name: str,
    **kwargs: Any,
) -> RackPriceProvider:
    """Return a concrete provider by short name.

    Used by the Sourcing_Recommender once it has looked up the tenant's
    ``overlay.rack_price_provider`` Redis key (Requirement 8.2.2). Unknown
    names raise :class:`ValueError` — we never silently fall through to a
    default because that would hide a misconfiguration.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("rack-price provider name must be a non-empty string")
    normalized = name.strip().lower()
    if normalized == OPISRackPriceProvider.name:
        return OPISRackPriceProvider(**kwargs)
    if normalized == CSVFallbackRackPriceProvider.name:
        return CSVFallbackRackPriceProvider(**kwargs)
    raise ValueError(f"unknown rack-price provider: {name!r}")


__all__ = [
    # Models
    "RackPrice",
    # Base class
    "RackPriceProvider",
    # Adapters
    "OPISRackPriceProvider",
    "CSVFallbackRackPriceProvider",
    # Factory
    "build_rack_price_provider",
    # Types
    "CSVLoader",
    # Constants
    "CACHE_BUCKET_MINUTES",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_CACHE_TTL_SECONDS",
    "OPIS_API_KEY_ENV",
    "OPIS_API_SECRET_ENV",
    "OPIS_BASE_URL_ENV",
    "CSV_REQUIRED_COLUMNS",
]
