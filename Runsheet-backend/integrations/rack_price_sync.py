"""
Rack-price sync service — Capability 8 / Task 7.4 of the fuel-ops hardening
spec.

Task 7.4 wires the :class:`RackPriceProvider` abstractions (Task 7.3) into
the rest of the platform by:

1. Persisting every fetched :class:`RackPrice` row to the ``rack_prices``
   ES index with ``tenant_id``, ``terminal_id``, ``product_code``,
   ``price_per_gallon_usd``, ``branded_flag``, ``effective_at``,
   ``retrieved_at``, ``provider``, and ``supplier_brand`` (Requirement
   8.2.3).

2. On provider failure or 10-second timeout, falling back to the
   most-recent price per (terminal_id, product_code) in ``rack_prices``
   observed within the last 24 hours (Requirement 8.2.5).

3. Annotating the caller's :class:`SourcingRecommendation` with
   ``rack_price_fallback=True`` whenever the fallback path fired so
   dispatchers can see at a glance that a recommendation's pricing is
   derived from historical rather than live data.

The service is dependency-light: it takes a concrete
:class:`RackPriceProvider`, an ``ElasticsearchService``-compatible object
exposing ``index_document`` / ``search_documents``, and nothing else. It
never reaches back into Redis — the provider already owns cache lookup
and populate behaviour; this service focuses on durable persistence and
the ES-backed fallback path.

Design points worth calling out:

* **Source-of-truth ES writes.** Fetched rows are indexed with their
  ``rack_price_id`` as the ES document id so re-fetching the same row
  (identical uuid) is idempotent. The :class:`RackPrice` model already
  enforces a strict schema that matches the ``rack_prices`` ES mapping
  1:1, so ``model_dump(mode="json")`` is indexable without
  transformation.

* **Per-pair fallback selection.** The fallback query returns one row
  per (terminal_id, product_code) pair — the one with the latest
  ``effective_at`` within the 24-hour window. When multiple rows tie,
  the most recently retrieved wins. Pairs that have no matching row
  simply get no fallback; the sync result carries only the pairs we
  were able to service.

* **No exception leakage.** Network / ES failures degrade the sync to a
  fallback result rather than raising, because the Sourcing_Recommender
  must remain available even when upstream data sources are down. Only
  programmer errors (missing tenant_id, bad argument types) propagate.

* **Deterministic ordering.** The service always returns rows sorted by
  (terminal_id, product_code, effective_at) so callers get stable
  output regardless of ES hit ordering.

Validates: Requirements 8.2.3, 8.2.5.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Mapping as ABCMapping
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
)

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fuel.services.fuel_ops_es_mappings import RACK_PRICES_INDEX
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)
from integrations.rack_price_provider_base import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    RackPrice,
    RackPriceProvider,
)

if TYPE_CHECKING:
    # Only for type checkers; avoids a circular import with fuel.terminal_models
    # which imports service modules that may eventually import this file.
    from fuel.terminal_models import SourcingRecommendation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical flag field name carried on :class:`SourcingRecommendation`
#: when the fallback path fires (Requirement 8.2.5).
RACK_PRICE_FALLBACK_FIELD: str = "rack_price_fallback"

#: Maximum age (in hours) of a cached ES row eligible for fallback use.
#: Requirement 8.2.5: "fall back to the most recent cached price within
#: the last 24 hours".
DEFAULT_FALLBACK_WINDOW_HOURS: int = 24

#: Maximum wall-clock seconds the sync service waits on a provider
#: ``get_prices`` call before declaring failure. Matches the provider's
#: own internal budget so either end of the call chain can short-circuit
#: and fall back cleanly (Requirement 8.2.5).
DEFAULT_SYNC_TIMEOUT_SECONDS: float = DEFAULT_HTTP_TIMEOUT_SECONDS

#: Upper bound on the fallback-query ES ``size`` parameter. We request more
#: than (terminals × products) so we can dedupe to the latest row per pair
#: after receiving the response. A per-pair top_hits aggregation would be
#: cleaner but requires more ES-specific machinery than the mock-friendly
#: search API this codebase uses everywhere else.
_FALLBACK_QUERY_SIZE_CAP: int = 1000


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class RackPriceSyncResult(BaseModel):
    """Outcome of a :meth:`RackPriceSyncService.sync` invocation.

    ``prices`` carries the rows the sync settled on — either the fresh
    rows persisted this cycle or the fallback rows pulled from ES —
    sorted by (terminal_id, product_code, effective_at) so downstream
    consumers get a stable view regardless of ES hit ordering.

    ``rack_price_fallback`` is the boolean the Sourcing_Recommender
    forwards onto the persisted :class:`SourcingRecommendation` so the
    dispatcher UI can render a "historical price" badge when fresh
    data is unavailable.
    """

    model_config = ConfigDict(extra="forbid")

    prices: List[RackPrice] = Field(default_factory=list)
    rack_price_fallback: bool = Field(default=False)
    persisted_count: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    as_of: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` stamp."""

    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` coerced to UTC.

    Naive values are assumed to already represent UTC; this mirrors the
    rest of the rack-price modules so timestamps stay consistent across
    cache keys, ES writes, and fallback windows.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedupe_preserving_order(values: Iterable[str]) -> List[str]:
    """Return ``values`` with duplicates removed, preserving first-seen order."""

    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        out.append(stripped)
    return out


def _coerce_canonical_products(values: Iterable[str]) -> List[str]:
    """Canonicalize and deduplicate product codes, dropping unknowns.

    Mirrors the provider base class so sync callers can hand in legacy
    aliases (AGO, PMS, ATK, LPG) and still hit the right ES rows after
    persistence.
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
                "RackPriceSyncService: dropping unknown product_code %r",
                entry,
            )
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def _sort_prices(prices: Sequence[RackPrice]) -> List[RackPrice]:
    """Return ``prices`` sorted by (terminal_id, product_code, effective_at)."""

    return sorted(
        prices,
        key=lambda r: (r.terminal_id, r.product_code, r.effective_at),
    )


def _pair_key(row: RackPrice) -> Tuple[str, str]:
    return (row.terminal_id, row.product_code)


# ---------------------------------------------------------------------------
# Annotation helper
# ---------------------------------------------------------------------------


def annotate_rack_price_fallback(
    recommendation: "SourcingRecommendation",
    fallback: bool,
) -> "SourcingRecommendation":
    """Return a copy of ``recommendation`` with ``rack_price_fallback`` set.

    Returns a new :class:`SourcingRecommendation` rather than mutating
    the caller's input so the recommender can safely reuse the same
    base object across multiple sync cycles. When ``fallback`` is
    ``False`` the input is returned unchanged (no allocation).

    The annotation is always idempotent: calling this helper twice with
    the same ``fallback`` value yields a recommendation equal to the
    first result.

    Raises:
        TypeError: ``recommendation`` is not a :class:`SourcingRecommendation`.
    """

    # Imported lazily to avoid a circular import at module load time
    # (fuel.terminal_models pulls in a lot of Capability-8 helpers that
    # themselves may eventually import integrations packages).
    from fuel.terminal_models import SourcingRecommendation

    if not isinstance(recommendation, SourcingRecommendation):
        raise TypeError(
            "recommendation must be a SourcingRecommendation, got "
            f"{type(recommendation).__name__}"
        )
    if not isinstance(fallback, bool):
        raise TypeError(
            f"fallback must be a bool, got {type(fallback).__name__}"
        )

    if recommendation.rack_price_fallback is fallback:
        return recommendation

    return recommendation.model_copy(update={RACK_PRICE_FALLBACK_FIELD: fallback})


# ---------------------------------------------------------------------------
# Sync service
# ---------------------------------------------------------------------------


class RackPriceSyncService:
    """Orchestrates rack-price fetch, persistence, and fallback.

    Flow for each :meth:`sync` invocation:

        1. Validate arguments and canonicalize product codes. Bail out
           early with an empty result when the cross-product of
           terminals × products is empty.

        2. Invoke ``provider.get_prices`` under a wall-clock budget of
           ``timeout_seconds`` (default 10 s). Timeouts / exceptions are
           caught and treated as "no rows returned".

        3. If the provider returned rows:
             a. Bulk-persist every row to the ``rack_prices`` ES index
                keyed by ``rack_price_id``.
             b. Dedupe to the latest (terminal_id, product_code) per
                ``effective_at`` so the caller receives a single quote
                per pair.
             c. Return ``RackPriceSyncResult(rack_price_fallback=False, …)``.

        4. If the provider returned no rows (either by failure or by
           legitimately-empty upstream):
             a. Query ``rack_prices`` for rows whose ``retrieved_at``
                is within ``fallback_window_hours`` of ``as_of``.
             b. Reduce the result to one row per (terminal_id,
                product_code), preferring the latest ``effective_at``.
             c. Return ``RackPriceSyncResult(rack_price_fallback=True, …)``
                even when no fallback row was found — the flag captures
                the intent to use historical data, not merely its
                availability.

    Args:
        es_service: Any object exposing an async ``index_document``
            coroutine (for persisting fetched rows) and an async
            ``search_documents`` coroutine (for the fallback query).
        fallback_window_hours: Maximum age of an ES row eligible for
            fallback use. Defaults to 24 hours per Requirement 8.2.5.
        timeout_seconds: Wall-clock budget for each ``get_prices``
            call. Defaults to 10 seconds per Requirement 8.2.5.
    """

    INDEX: str = RACK_PRICES_INDEX

    def __init__(
        self,
        es_service: Any,
        *,
        fallback_window_hours: int = DEFAULT_FALLBACK_WINDOW_HOURS,
        timeout_seconds: float = DEFAULT_SYNC_TIMEOUT_SECONDS,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service is required")
        if fallback_window_hours <= 0:
            raise ValueError("fallback_window_hours must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._es = es_service
        self._fallback_window = timedelta(hours=int(fallback_window_hours))
        self._timeout = float(timeout_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def sync(
        self,
        provider: RackPriceProvider,
        *,
        tenant_id: str,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
    ) -> RackPriceSyncResult:
        """Fetch, persist, and (if needed) fall back to cached prices.

        Never raises on provider or ES failures — both degrade to the
        fallback path so the Sourcing_Recommender stays responsive
        during upstream incidents.
        """

        self._validate_args(provider, tenant_id, terminal_ids, product_codes, as_of)

        canonical_terminals = _dedupe_preserving_order(terminal_ids)
        canonical_products = _coerce_canonical_products(product_codes)
        as_of_utc = _ensure_utc(as_of)

        if not canonical_terminals or not canonical_products:
            return RackPriceSyncResult(
                prices=[],
                rack_price_fallback=False,
                persisted_count=0,
                fallback_count=0,
                as_of=as_of_utc,
            )

        fetched = await self._fetch_from_provider(
            provider=provider,
            terminal_ids=canonical_terminals,
            product_codes=canonical_products,
            as_of=as_of_utc,
            tenant_id=tenant_id,
        )

        if fetched:
            persisted = await self._persist(fetched)
            # Dedupe to one row per pair, keeping the latest effective_at
            # (then most recently retrieved if tied).
            latest = _select_latest_per_pair(fetched)
            return RackPriceSyncResult(
                prices=_sort_prices(latest),
                rack_price_fallback=False,
                persisted_count=persisted,
                fallback_count=0,
                as_of=as_of_utc,
            )

        fallback_rows = await self._load_fallback(
            tenant_id=tenant_id,
            terminal_ids=canonical_terminals,
            product_codes=canonical_products,
            as_of=as_of_utc,
        )
        return RackPriceSyncResult(
            prices=_sort_prices(fallback_rows),
            rack_price_fallback=True,
            persisted_count=0,
            fallback_count=len(fallback_rows),
            as_of=as_of_utc,
        )

    # ------------------------------------------------------------------
    # Provider call
    # ------------------------------------------------------------------

    async def _fetch_from_provider(
        self,
        *,
        provider: RackPriceProvider,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        tenant_id: str,
    ) -> List[RackPrice]:
        """Invoke ``provider.get_prices`` under the sync-level budget.

        The provider's base class already catches its own network /
        parse errors inside ``_fetch_with_timeout``, so we only expect
        to swallow a top-level ``asyncio.TimeoutError`` here in the
        pathological case where the provider's own budget is disabled.
        Programmer errors (``ValueError``, ``TypeError``) still
        propagate so misuse is loud and obvious.
        """

        try:
            rows = await asyncio.wait_for(
                provider.get_prices(
                    terminal_ids=terminal_ids,
                    product_codes=product_codes,
                    as_of=as_of,
                    tenant_id=tenant_id,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "RackPriceSyncService: provider %s timed out after %.1fs "
                "for tenant=%s terminals=%d products=%d — falling back",
                getattr(provider, "name", type(provider).__name__),
                self._timeout,
                tenant_id,
                len(terminal_ids),
                len(product_codes),
            )
            return []
        except (ValueError, TypeError):
            # Programmer error in the caller's arguments — re-raise so
            # the bug surfaces immediately in tests rather than being
            # hidden behind the fallback path.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "RackPriceSyncService: provider %s raised unexpectedly "
                "(tenant=%s): %s — falling back",
                getattr(provider, "name", type(provider).__name__),
                tenant_id,
                exc,
            )
            return []

        # Defensive re-stamp: drop any row whose tenant_id does not match
        # the caller so a buggy provider never cross-pollinates tenants.
        clean: List[RackPrice] = []
        for row in rows or []:
            if not isinstance(row, RackPrice):
                logger.warning(
                    "RackPriceSyncService: dropping non-RackPrice row from "
                    "provider %s: %r",
                    getattr(provider, "name", type(provider).__name__),
                    row,
                )
                continue
            if row.tenant_id != tenant_id:
                logger.warning(
                    "RackPriceSyncService: dropping row with foreign "
                    "tenant_id %s (expected %s)",
                    row.tenant_id,
                    tenant_id,
                )
                continue
            clean.append(row)
        return clean

    # ------------------------------------------------------------------
    # ES persistence
    # ------------------------------------------------------------------

    async def _persist(self, rows: Sequence[RackPrice]) -> int:
        """Index each fetched row into ``rack_prices``.

        Returns the count of successfully-indexed rows. Per-row failures
        are logged and swallowed so one malformed entry cannot block the
        rest of the batch — we would rather surface an incomplete cache
        than fail the whole sync cycle.
        """

        if not rows:
            return 0

        success = 0
        for row in rows:
            try:
                document = row.model_dump(mode="json")
                # Stamp created_at / updated_at so the ES mapping's common
                # date fields line up with the rest of the fuel-ops domain
                # (every other Capability-8 entity persists these).
                now_iso = _utcnow().isoformat()
                document.setdefault("created_at", now_iso)
                document["updated_at"] = now_iso
                await self._es.index_document(self.INDEX, row.rack_price_id, document)
                success += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "RackPriceSyncService: failed to persist rack_price_id=%s "
                    "tenant=%s terminal=%s product=%s: %s",
                    row.rack_price_id,
                    row.tenant_id,
                    row.terminal_id,
                    row.product_code,
                    exc,
                )
        return success

    # ------------------------------------------------------------------
    # Fallback query
    # ------------------------------------------------------------------

    async def _load_fallback(
        self,
        *,
        tenant_id: str,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
    ) -> List[RackPrice]:
        """Return the most-recent cached price per pair within 24 hours.

        Query shape:

            {
              "query": {"bool": {"must": [
                {"term":  {"tenant_id":    tenant_id}},
                {"terms": {"terminal_id":  [...]}},
                {"terms": {"product_code": [...]}},
                {"range": {"retrieved_at": {"gte": as_of - 24h}}},
              ]}},
              "sort": [{"effective_at": "desc"}, {"retrieved_at": "desc"}],
              "size": min(terminals * products * 4, 1000),
            }

        After the hits come back, we reduce to one row per pair,
        preferring the row with the latest ``effective_at`` (breaking
        ties on ``retrieved_at``). Rows whose ``_source.tenant_id`` does
        not match the caller are dropped defensively even though the ES
        ``term`` clause should already filter them out.
        """

        cutoff = as_of - self._fallback_window
        size = min(
            _FALLBACK_QUERY_SIZE_CAP,
            max(len(terminal_ids) * len(product_codes) * 4, 10),
        )

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"terminal_id": list(terminal_ids)}},
                        {"terms": {"product_code": list(product_codes)}},
                        {"range": {"retrieved_at": {"gte": cutoff.isoformat()}}},
                    ]
                }
            },
            "sort": [
                {"effective_at": {"order": "desc"}},
                {"retrieved_at": {"order": "desc"}},
            ],
            "size": size,
        }

        try:
            resp = await self._es.search_documents(self.INDEX, query, size)
        except Exception as exc:
            logger.warning(
                "RackPriceSyncService: fallback query failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []

        sources = _extract_sources(resp)
        best: Dict[Tuple[str, str], RackPrice] = {}
        terminal_allow = set(terminal_ids)
        product_allow = set(product_codes)

        for source in sources:
            if not isinstance(source, dict):
                continue
            # Defense-in-depth: the ES term filter must already scope to
            # the caller's tenant, but a mislabelled document must never
            # reach the Sourcing_Recommender.
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "RackPriceSyncService: dropping fallback row with "
                    "mismatched tenant_id=%s (expected %s)",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue

            # The `terms` clauses above already filter, but a caller
            # handing in legacy aliases before canonicalization could
            # still produce misses. Double-check after canonicalizing.
            terminal_id = source.get("terminal_id")
            if terminal_id not in terminal_allow:
                continue
            try:
                canonical_product = canonicalize(source.get("product_code", ""))
            except UnknownFuelProductError:
                continue
            if canonical_product not in product_allow:
                continue

            try:
                row = RackPrice(**source)
            except ValidationError as exc:
                logger.warning(
                    "RackPriceSyncService: dropping fallback row that "
                    "failed validation (terminal=%s product=%s): %s",
                    terminal_id,
                    source.get("product_code"),
                    exc,
                )
                continue

            # Hard re-check against the 24-hour window in case ES
            # returned a near-boundary row whose stored retrieved_at
            # predates the cutoff (clock skew between writers/readers).
            if _ensure_utc(row.retrieved_at) < cutoff:
                continue

            key = _pair_key(row)
            previous = best.get(key)
            if previous is None:
                best[key] = row
                continue
            # Prefer the row with the latest effective_at; break ties on
            # retrieved_at so the freshest observation wins.
            prev_effective = _ensure_utc(previous.effective_at)
            row_effective = _ensure_utc(row.effective_at)
            if row_effective > prev_effective:
                best[key] = row
            elif row_effective == prev_effective and _ensure_utc(row.retrieved_at) > _ensure_utc(
                previous.retrieved_at
            ):
                best[key] = row

        return list(best.values())

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_args(
        provider: Any,
        tenant_id: str,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
    ) -> None:
        if not isinstance(provider, RackPriceProvider):
            raise TypeError(
                "provider must be a RackPriceProvider instance, got "
                f"{type(provider).__name__}"
            )
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


# ---------------------------------------------------------------------------
# Helpers used by sync + tests
# ---------------------------------------------------------------------------


def _select_latest_per_pair(rows: Sequence[RackPrice]) -> List[RackPrice]:
    """Return one row per (terminal_id, product_code), newest first.

    The selection rule mirrors the fallback query's dedupe:

        1. Latest ``effective_at`` wins.
        2. Tie on ``effective_at`` → latest ``retrieved_at`` wins.
        3. Tie on both → stable order (first seen).
    """

    best: Dict[Tuple[str, str], RackPrice] = {}
    for row in rows:
        key = _pair_key(row)
        previous = best.get(key)
        if previous is None:
            best[key] = row
            continue
        prev_effective = _ensure_utc(previous.effective_at)
        row_effective = _ensure_utc(row.effective_at)
        if row_effective > prev_effective:
            best[key] = row
        elif row_effective == prev_effective and _ensure_utc(row.retrieved_at) > _ensure_utc(
            previous.retrieved_at
        ):
            best[key] = row
    return list(best.values())


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` dicts from an ES-shaped search response.

    Handles both the canonical nested shape and a variety of mock
    responses so tests can feed in minimal fakes.
    """

    if not resp or not isinstance(resp, ABCMapping):
        return []
    hits_outer = resp.get("hits")
    if not isinstance(hits_outer, ABCMapping):
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, ABCMapping):
            continue
        source = hit.get("_source")
        if isinstance(source, ABCMapping):
            # Defensive copy so callers can mutate without affecting the
            # underlying ES client's cached response.
            out.append(copy.deepcopy(dict(source)))
    return out


__all__ = [
    # Result model
    "RackPriceSyncResult",
    # Service
    "RackPriceSyncService",
    # Annotation helper
    "annotate_rack_price_fallback",
    # Constants
    "RACK_PRICE_FALLBACK_FIELD",
    "DEFAULT_FALLBACK_WINDOW_HOURS",
    "DEFAULT_SYNC_TIMEOUT_SECONDS",
]
