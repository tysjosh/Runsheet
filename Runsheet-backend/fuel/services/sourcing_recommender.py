"""
Sourcing_Recommender service — Task 7.9 of the fuel-ops hardening spec.

Capability 8 (Terminal / Rack Sourcing Intelligence) introduces a
request-response service that, given a (tenant, product, volume, origin,
as_of) tuple, returns a ranked list of loading terminals for the
Route_Planning_Agent (Requirement 8.5.1). Ranking balances four signals:

* **negative price per gallon** — cheaper racks rank higher.
* **negative avg_wait_minutes** — racks with shorter queues rank higher.
* **negative distance_km from origin** — closer racks rank higher.
* **contract_priority_boost** — racks covered by an active tenant
  Supplier_Contract for the requested product get a bonus so committed
  volume is honoured (Requirement 8.5.2).

Each signal is min-max normalised across the candidate set so the final
score lives in ``[0.0, 1.0]`` regardless of the absolute magnitudes the
day's rack sheet happens to produce. Tenant weights are fetched from
Redis (``sourcing_weights:{tenant_id}``) with the shipped default
``{"price": 0.4, "wait": 0.25, "distance": 0.2, "contract": 0.15}``.

Disqualification (Requirement 8.5.3) runs **before** ranking, never on
the scored set, so a blocked terminal is invisible to the dispatcher:

1. ``product_code not in terminal.supported_products`` → dropped with
   reason ``product_unsupported``.
2. Terminal is closed at the requested ``as_of`` (Terminal.operating_hours
   evaluated in the terminal's local timezone) → dropped with reason
   ``terminal_closed``.
3. ``branded`` preference conflicts with the terminal's ``branded`` flag
   → dropped with reason ``branded_mismatch`` /
   ``unbranded_required``.
4. No price is resolvable (no rack price and no contract price) →
   dropped with reason ``no_price_available``.

A matching Supplier_Contract with a fixed
``contract_price_per_gallon_usd`` overrides the live rack price for
that candidate (Requirement 8.3.3 / Task 7.9 ``contract_priority_boost``).

``avg_wait_minutes`` is looked up via the injected
:class:`WaitTimeResolver` which the caller wires to the Redis rolling
2-hour average maintained by Task 7.7. When no observation exists the
wait defaults to ``0.0`` so the terminal is not penalised for missing
telemetry. When the observed wait exceeds the tenant's configured
``terminal_wait_warning_minutes`` (Redis key
``terminal_wait_warning_minutes:{tenant_id}``, default 60) the candidate
is annotated with ``wait_warning=True`` (Requirement 8.4.5 / Task 7.11
— the recommender is the one place we consistently stamp the flag).

The recommender is a pure function of its dependencies; persistence of
the result to the ``sourcing_recommendations`` ES index and emission of
the ``sourcing_recommendation_ready`` WebSocket event are the
responsibility of the REST endpoint wired in Task 7.10.

Validates: Requirements 8.5.1, 8.5.2, 8.5.3.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

try:  # pragma: no cover - zoneinfo ships with Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):
        """Fallback stub when zoneinfo is unavailable."""


from driver.services.geo_utils import haversine_distance_meters
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)
from fuel.terminal_models import (
    OperatingHours,
    SourcingRecommendation,
    SupplierContract,
    SupplierContractRepository,
    Terminal,
    TerminalCandidate,
    TerminalRepository,
)
from integrations.rack_price_sync import (
    RackPriceSyncService,
    annotate_rack_price_fallback,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants & types
# ---------------------------------------------------------------------------


#: Default scoring weights. Sum to 1.0 so the final score lands in
#: [0, 1] after normalisation, but the formula does not require unit sum —
#: see :func:`_normalise_weights`.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "price": 0.40,
    "wait": 0.25,
    "distance": 0.20,
    "contract": 0.15,
}


#: Default wait-warning threshold in minutes. Terminals whose rolling
#: 2-hour average wait exceeds this value are flagged
#: ``wait_warning=True`` on the recommendation (Req 8.4.5).
DEFAULT_WAIT_WARNING_MINUTES: float = 60.0


#: Redis keys. Kept as format strings so tests can assert the exact layout.
WEIGHTS_REDIS_KEY: str = "sourcing_weights:{tenant_id}"
WAIT_WARNING_REDIS_KEY: str = "terminal_wait_warning_minutes:{tenant_id}"


#: Names of the three "lower-is-better" signals, in the order the
#: reasons list enumerates them. Keeping a tuple makes iteration and
#: test assertions explicit.
_NEGATIVE_SIGNALS: Tuple[str, ...] = ("price", "wait", "distance")


#: Short codes for terminal day-of-week lookup — must match
#: :data:`fuel.terminal_models.DayOfWeek`.
_DAY_OF_WEEK_CODES: Tuple[str, ...] = (
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
)


#: Callable the caller supplies to resolve the rolling avg_wait_minutes
#: for a (tenant_id, terminal_id) pair. Must return ``None`` when no
#: observation is available so the recommender can default the wait to
#: zero rather than bucketing a missing signal as "infinite wait".
WaitTimeResolver = Callable[[str, str], Awaitable[Optional[float]]]


#: Tenant-config backend. Matches the minimal contract used elsewhere
#: in the overlay agents (``async get(key) -> Optional[str]``).
class TenantConfigHandle:  # pragma: no cover - Protocol-like sentinel
    async def get(self, key: str) -> Optional[str]:  # noqa: D401
        ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourcingRecommenderError(RuntimeError):
    """Base class for Sourcing_Recommender programmer errors."""


class InvalidBrandedPreferenceError(SourcingRecommenderError):
    """Raised when the caller passes a branded preference that is not a bool/None."""


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------


class SourcingWeights(BaseModel):
    """Tenant-configurable scoring weights.

    Each weight must be non-negative. The weights do not have to sum to
    1.0 on input — :meth:`normalised` returns a unit-sum copy that keeps
    the final score in ``[0.0, 1.0]``. Rejecting all-zero weights up
    front surfaces a misconfigured tenant immediately instead of
    silently defaulting to uniform weights later.
    """

    model_config = ConfigDict(extra="forbid")

    price: float = Field(default=DEFAULT_WEIGHTS["price"], ge=0.0)
    wait: float = Field(default=DEFAULT_WEIGHTS["wait"], ge=0.0)
    distance: float = Field(default=DEFAULT_WEIGHTS["distance"], ge=0.0)
    contract: float = Field(default=DEFAULT_WEIGHTS["contract"], ge=0.0)

    @model_validator(mode="after")
    def _check_total_positive(self) -> "SourcingWeights":
        if self.total() <= 0.0:
            raise ValueError(
                "sourcing weights must contain at least one positive component; "
                "all-zero weights would leave score undefined"
            )
        return self

    def total(self) -> float:
        """Return the sum of all four weights."""

        return self.price + self.wait + self.distance + self.contract

    def normalised(self) -> "SourcingWeights":
        """Return a copy with weights summing to 1.0."""

        total = self.total()
        return SourcingWeights(
            price=self.price / total,
            wait=self.wait / total,
            distance=self.distance / total,
            contract=self.contract / total,
        )

    def as_dict(self) -> Dict[str, float]:
        return {
            "price": self.price,
            "wait": self.wait,
            "distance": self.distance,
            "contract": self.contract,
        }


# ---------------------------------------------------------------------------
# Rack-price lookup protocol
# ---------------------------------------------------------------------------


class RackPriceLookup:
    """Minimal shape the recommender expects from a rack-price provider.

    The production implementation is
    :class:`integrations.rack_price_provider_base.RackPriceProvider`;
    keeping the signature narrow here lets tests pass a simple stub
    without dragging in the full provider plumbing.
    """

    async def get_prices(  # pragma: no cover - Protocol-like sentinel
        self,
        terminal_ids: Sequence[str],
        product_codes: Sequence[str],
        as_of: datetime,
        *,
        tenant_id: str,
    ) -> List[Any]:
        ...


# ---------------------------------------------------------------------------
# Internal scoring dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScoringInputs:
    """Raw (pre-normalisation) signals collected for one terminal."""

    terminal: Terminal
    price: float
    wait: float
    distance_km: float
    contract: Optional[SupplierContract]


@dataclass(frozen=True)
class _Disqualification:
    """Reason + terminal_id pair captured when a terminal is dropped."""

    terminal_id: str
    reason: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SourcingRecommender:
    """Rank loading terminals for a (product, volume, origin, as_of) query.

    Dependencies are injected through the constructor so the service is
    trivially unit-testable without patching module-level singletons:

        * ``terminal_repo`` — :class:`TerminalRepository` used to list
          candidates for the tenant (repository already enforces tenant
          isolation).
        * ``contract_repo`` — :class:`SupplierContractRepository` used
          to find matching active Supplier_Contracts.
        * ``rack_price_provider`` — rack-price adapter conforming to the
          :class:`RackPriceLookup` protocol.
        * ``wait_time_resolver`` — async callable resolving
          ``(tenant_id, terminal_id) -> avg_wait_minutes``. When
          omitted every candidate defaults to 0 wait (the caller is
          signalling "no wait data — do not penalise").
        * ``tenant_config`` — optional Redis-like handle with an async
          ``get(key)`` used to read ``sourcing_weights:{tenant_id}`` and
          ``terminal_wait_warning_minutes:{tenant_id}`` overrides.
        * ``default_weights`` / ``default_wait_warning_minutes`` — allow
          unit tests to pin behaviour without standing up Redis.

    The recommender never mutates its inputs. Every call produces a
    fresh :class:`SourcingRecommendation` ready for the audit index.
    """

    def __init__(
        self,
        *,
        terminal_repo: TerminalRepository,
        contract_repo: SupplierContractRepository,
        rack_price_provider: RackPriceLookup,
        wait_time_resolver: Optional[WaitTimeResolver] = None,
        tenant_config: Optional[TenantConfigHandle] = None,
        default_weights: Optional[SourcingWeights] = None,
        default_wait_warning_minutes: float = DEFAULT_WAIT_WARNING_MINUTES,
        rack_price_sync: Optional[RackPriceSyncService] = None,
    ) -> None:
        if terminal_repo is None:
            raise ValueError("terminal_repo must not be None")
        if contract_repo is None:
            raise ValueError("contract_repo must not be None")
        if rack_price_provider is None:
            raise ValueError("rack_price_provider must not be None")
        if default_wait_warning_minutes < 0:
            raise ValueError("default_wait_warning_minutes must be >= 0")

        self._terminals = terminal_repo
        self._contracts = contract_repo
        self._rack_prices = rack_price_provider
        self._resolve_wait = wait_time_resolver
        self._tenant_config = tenant_config
        self._default_weights = default_weights or SourcingWeights()
        # Optional RackPriceSyncService (Task 7.4) — when provided, the
        # recommender routes its rack-price lookup through the sync
        # service so fresh rows are persisted to the ``rack_prices`` ES
        # index and missed rows fall back to the most-recent cached
        # observation within 24 hours. The resulting
        # ``rack_price_fallback`` flag is forwarded onto the persisted
        # :class:`SourcingRecommendation` via
        # :func:`integrations.rack_price_sync.annotate_rack_price_fallback`
        # (Requirement 8.2.5). Leaving it ``None`` preserves the legacy
        # call-path used by existing unit tests, where the
        # ``rack_price_provider`` is invoked directly with no persistence
        # or fallback annotation.
        self._rack_price_sync = rack_price_sync
        self._default_wait_warning = float(default_wait_warning_minutes)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def recommend(
        self,
        *,
        tenant_id: str,
        product_code: str,
        volume_gallons: float,
        origin_lat_lon: Tuple[float, float],
        as_of: datetime,
        branded: Optional[bool] = None,
        request_id: Optional[str] = None,
        truck_id: Optional[str] = None,
        run_id: Optional[str] = None,
        terminal_ids: Optional[Sequence[str]] = None,
    ) -> SourcingRecommendation:
        """Produce a :class:`SourcingRecommendation` for the given query.

        Parameters:
            tenant_id: tenant owning the request. Used for every
                downstream tenant-scoped lookup.
            product_code: canonical product_code or legacy alias. Aliases
                are canonicalised before any disqualification runs so
                ``"AGO"`` and ``"DIESEL_2"`` behave identically.
            volume_gallons: desired load volume (> 0). Not used in the
                ranking formula today but persisted on the audit record
                for traceability and future contract-coverage logic.
            origin_lat_lon: ``(lat, lon)`` origin tuple (depot or truck
                location) against which ``distance_km_from_start`` is
                computed.
            as_of: timestamp of the load. Used both to filter closed
                terminals and to fetch rack prices for that 15-minute
                bucket.
            branded: ``True`` → only branded terminals are eligible,
                ``False`` → only unbranded terminals, ``None`` → both.
            request_id: optional idempotency key. Minted from ``uuid4``
                when omitted so every recommendation has a stable
                request identifier even on fire-and-forget invocations.
            truck_id / run_id: optional traceability fields stamped on
                the audit record.
            terminal_ids: optional restriction to a specific set of
                terminal ids. Primarily used by the Route_Planning_Agent
                when it already has a candidate slate (e.g. from
                Loading_Plan constraints). When omitted the recommender
                consults every active terminal for the tenant.

        Returns:
            A :class:`SourcingRecommendation` with ``candidates`` sorted
            by ``score`` descending; ties broken deterministically by
            ``(price asc, wait asc, distance asc, terminal_id asc)`` so
            the ordering is reproducible under identical inputs.
        """

        self._validate_recommend_args(
            tenant_id=tenant_id,
            product_code=product_code,
            volume_gallons=volume_gallons,
            origin_lat_lon=origin_lat_lon,
            as_of=as_of,
            branded=branded,
        )

        canonical_product = canonicalize(product_code)
        effective_request_id = (request_id or f"req_{uuid4()}").strip() or f"req_{uuid4()}"

        # 1) Load weights + wait threshold (tenant overrides are best-effort).
        weights = await self._load_weights(tenant_id)
        wait_warning_threshold = await self._load_wait_warning_minutes(tenant_id)

        # 2) Resolve terminals, contracts.
        terminals = await self._load_candidate_terminals(tenant_id, terminal_ids)
        contracts = await self._contracts.list_for_tenant(
            tenant_id,
            status="active",
            product_code=canonical_product,
        )

        # 3) Disqualify — product, hours, branded. Price + wait come after.
        eligible, disqualified = self._apply_disqualifications(
            terminals=terminals,
            product_code=canonical_product,
            as_of=as_of,
            branded=branded,
        )
        if disqualified:
            logger.debug(
                "SourcingRecommender: dropped %d terminals for tenant=%s product=%s: %s",
                len(disqualified),
                tenant_id,
                canonical_product,
                [(d.terminal_id, d.reason) for d in disqualified],
            )

        # 4) Gather rack prices + waits for the remaining candidates.
        rack_prices, rack_price_fallback = await self._fetch_rack_prices(
            terminals=eligible,
            product_code=canonical_product,
            as_of=as_of,
            tenant_id=tenant_id,
        )
        waits = await self._fetch_waits(tenant_id, eligible)

        # 5) Build scoring inputs (apply contract-price override, drop no-price).
        scoring_inputs: List[_ScoringInputs] = []
        for terminal in eligible:
            contract = _find_matching_contract(
                terminal=terminal,
                contracts=contracts,
                as_of=as_of,
            )
            price = _resolve_price(
                terminal=terminal,
                contract=contract,
                rack_prices=rack_prices,
                product_code=canonical_product,
            )
            if price is None:
                continue
            distance_km = _haversine_km(origin_lat_lon, terminal)
            scoring_inputs.append(
                _ScoringInputs(
                    terminal=terminal,
                    price=price,
                    wait=waits.get(terminal.terminal_id, 0.0),
                    distance_km=distance_km,
                    contract=contract,
                )
            )

        # 6) Score + rank.
        candidates = _rank_candidates(
            inputs=scoring_inputs,
            weights=weights,
            wait_warning_threshold=wait_warning_threshold,
        )

        recommendation = SourcingRecommendation(
            recommendation_id=f"srec_{uuid4()}",
            request_id=effective_request_id,
            tenant_id=tenant_id,
            truck_id=truck_id,
            run_id=run_id,
            product_code=canonical_product,
            volume_gallons=float(volume_gallons),
            origin_lat=float(origin_lat_lon[0]),
            origin_lon=float(origin_lat_lon[1]),
            candidates=candidates,
            rack_price_fallback=False,
            generated_at=_ensure_utc(as_of),
        )
        # ``SourcingRecommendation`` auto-derives the top-level
        # ``wait_warning_terminal_ids`` list from every candidate whose
        # ``wait_warning`` flag is set above (Task 7.11 / Req 8.4.5), so
        # the dispatcher UI and the persisted audit record both see the
        # same wait-warning summary without scanning nested candidates.
        # Stamp the ``rack_price_fallback`` annotation only when the sync
        # service reported a fallback. The helper is a no-op for the
        # default case so tests that assert the field stays ``False``
        # keep working unchanged (Requirement 8.2.5).
        if rack_price_fallback:
            recommendation = annotate_rack_price_fallback(
                recommendation, True
            )
        return recommendation

    # ------------------------------------------------------------------
    # Tenant-config loaders
    # ------------------------------------------------------------------

    async def _load_weights(self, tenant_id: str) -> SourcingWeights:
        """Read tenant weights from Redis, falling back to defaults.

        Malformed JSON, unknown keys, or negative components revert to
        the shipped defaults so a fat-fingered Redis payload never
        blocks dispatch. Legitimate config errors are surfaced through
        a warning log so operators can still notice them.
        """

        if self._tenant_config is None:
            return self._default_weights

        raw = await self._safe_config_get(WEIGHTS_REDIS_KEY.format(tenant_id=tenant_id))
        if not raw:
            return self._default_weights
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "SourcingRecommender: ignoring non-JSON sourcing_weights payload for tenant=%s",
                tenant_id,
            )
            return self._default_weights
        if not isinstance(payload, Mapping):
            logger.warning(
                "SourcingRecommender: sourcing_weights payload must be an object for tenant=%s",
                tenant_id,
            )
            return self._default_weights
        try:
            return SourcingWeights(**{k: payload[k] for k in DEFAULT_WEIGHTS if k in payload})
        except Exception as exc:  # noqa: BLE001 — any validation error → defaults
            logger.warning(
                "SourcingRecommender: invalid sourcing_weights for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return self._default_weights

    async def _load_wait_warning_minutes(self, tenant_id: str) -> float:
        if self._tenant_config is None:
            return self._default_wait_warning

        raw = await self._safe_config_get(
            WAIT_WARNING_REDIS_KEY.format(tenant_id=tenant_id)
        )
        if not raw:
            return self._default_wait_warning
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "SourcingRecommender: non-numeric terminal_wait_warning_minutes for tenant=%s: %r",
                tenant_id,
                raw,
            )
            return self._default_wait_warning
        if value < 0:
            logger.warning(
                "SourcingRecommender: negative terminal_wait_warning_minutes for tenant=%s: %r",
                tenant_id,
                value,
            )
            return self._default_wait_warning
        return value

    async def _safe_config_get(self, key: str) -> Optional[str]:
        try:
            value = await self._tenant_config.get(key)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 — Redis outage → defaults
            logger.warning(
                "SourcingRecommender: tenant_config.get(%r) failed: %s", key, exc
            )
            return None
        if value is None:
            return None
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(value, str):
            return str(value)
        return value

    # ------------------------------------------------------------------
    # Repository loaders
    # ------------------------------------------------------------------

    async def _load_candidate_terminals(
        self,
        tenant_id: str,
        terminal_ids: Optional[Sequence[str]],
    ) -> List[Terminal]:
        """List the tenant's active terminals, optionally filtered by ids."""

        terminals = await self._terminals.list_for_tenant(
            tenant_id, status="active"
        )
        if not terminal_ids:
            return list(terminals)
        allow: set[str] = {tid for tid in terminal_ids if isinstance(tid, str) and tid.strip()}
        if not allow:
            return []
        return [t for t in terminals if t.terminal_id in allow]

    # ------------------------------------------------------------------
    # Rack prices + waits
    # ------------------------------------------------------------------

    async def _fetch_rack_prices(
        self,
        *,
        terminals: Sequence[Terminal],
        product_code: str,
        as_of: datetime,
        tenant_id: str,
    ) -> Tuple[Dict[str, float], bool]:
        """Fetch live prices for the remaining candidate set.

        Returns a ``(prices_map, fallback_flag)`` tuple:

        * ``prices_map`` — ``terminal_id → price_per_gallon_usd``. Only
          non-negative USD figures are recorded; anything else is
          treated as missing so :func:`_resolve_price` can fall through
          to the contract price or drop the candidate.
        * ``fallback_flag`` — ``True`` when the prices came from the
          24-hour ES cache via :class:`RackPriceSyncService` (Task 7.4)
          rather than a live provider call. Forwarded onto the returned
          :class:`SourcingRecommendation` via
          :func:`annotate_rack_price_fallback` (Requirement 8.2.5).

        Behaviour depends on whether a :class:`RackPriceSyncService` was
        injected at construction time:

        * **Sync service present** — ``sync.sync(...)`` is invoked. The
          service persists fresh rows to the ``rack_prices`` ES index
          and transparently falls back to the most-recent cached row
          within 24 hours when the provider is unavailable. Its result
          carries the canonical ``rack_price_fallback`` boolean, which
          we propagate verbatim.
        * **Sync service absent** — the legacy call path stays in place:
          the recommender invokes the provider directly with no
          persistence. The fallback flag is always ``False`` in that
          mode because the recommender has no historical cache to fall
          back on.
        """

        if not terminals:
            return ({}, False)
        terminal_ids = [t.terminal_id for t in terminals]

        rows: Sequence[Any]
        fallback = False
        if self._rack_price_sync is not None:
            try:
                result = await self._rack_price_sync.sync(
                    self._rack_prices,  # type: ignore[arg-type]
                    tenant_id=tenant_id,
                    terminal_ids=terminal_ids,
                    product_codes=[product_code],
                    as_of=as_of,
                )
            except Exception as exc:  # noqa: BLE001 — sync outage → empty map
                logger.warning(
                    "SourcingRecommender: rack_price_sync.sync failed for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return ({}, False)
            rows = result.prices
            fallback = bool(result.rack_price_fallback)
        else:
            try:
                rows = await self._rack_prices.get_prices(
                    terminal_ids,
                    [product_code],
                    as_of,
                    tenant_id=tenant_id,
                )
            except Exception as exc:  # noqa: BLE001 — provider outage → empty map
                logger.warning(
                    "SourcingRecommender: rack_price_provider.get_prices failed for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                return ({}, False)

        best: Dict[str, Tuple[datetime, float]] = {}
        for row in rows or ():
            terminal_id = getattr(row, "terminal_id", None)
            row_product = getattr(row, "product_code", None)
            price = getattr(row, "price_per_gallon_usd", None)
            effective_at = getattr(row, "effective_at", None)
            if not terminal_id or row_product != product_code:
                continue
            if price is None or price < 0:
                continue
            when = _ensure_utc(effective_at) if isinstance(effective_at, datetime) else _ensure_utc(as_of)
            current = best.get(terminal_id)
            # Keep the latest effective_at per terminal — the provider may
            # return multiple observations when its cache bucket happens
            # to straddle two prints.
            if current is None or when > current[0]:
                best[terminal_id] = (when, float(price))
        return ({tid: price for tid, (_, price) in best.items()}, fallback)

    async def _fetch_waits(
        self,
        tenant_id: str,
        terminals: Sequence[Terminal],
    ) -> Dict[str, float]:
        """Resolve rolling avg_wait_minutes per terminal.

        Missing observations default to ``0.0`` so terminals without
        telemetry are not penalised. Negative or non-finite values are
        clamped to 0 and logged — a broken reporter should never turn a
        good terminal into a last-place ranking.
        """

        if not terminals or self._resolve_wait is None:
            return {t.terminal_id: 0.0 for t in terminals}

        out: Dict[str, float] = {}
        for terminal in terminals:
            try:
                raw = await self._resolve_wait(tenant_id, terminal.terminal_id)
            except Exception as exc:  # noqa: BLE001 — resolver outage → 0
                logger.warning(
                    "SourcingRecommender: wait resolver failed for tenant=%s terminal=%s: %s",
                    tenant_id,
                    terminal.terminal_id,
                    exc,
                )
                out[terminal.terminal_id] = 0.0
                continue
            if raw is None:
                out[terminal.terminal_id] = 0.0
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "SourcingRecommender: non-numeric wait for tenant=%s terminal=%s: %r",
                    tenant_id,
                    terminal.terminal_id,
                    raw,
                )
                out[terminal.terminal_id] = 0.0
                continue
            if not math.isfinite(value) or value < 0:
                logger.warning(
                    "SourcingRecommender: discarding non-finite/negative wait %s for "
                    "tenant=%s terminal=%s",
                    value,
                    tenant_id,
                    terminal.terminal_id,
                )
                out[terminal.terminal_id] = 0.0
                continue
            out[terminal.terminal_id] = value
        return out

    # ------------------------------------------------------------------
    # Disqualification
    # ------------------------------------------------------------------

    def _apply_disqualifications(
        self,
        *,
        terminals: Sequence[Terminal],
        product_code: str,
        as_of: datetime,
        branded: Optional[bool],
    ) -> Tuple[List[Terminal], List[_Disqualification]]:
        """Filter ``terminals`` by product support, operating hours, and brand."""

        eligible: List[Terminal] = []
        disqualified: List[_Disqualification] = []

        for terminal in terminals:
            if product_code not in terminal.supported_products:
                disqualified.append(
                    _Disqualification(terminal.terminal_id, "product_unsupported")
                )
                continue
            if not terminal.is_open_at(as_of):
                disqualified.append(
                    _Disqualification(terminal.terminal_id, "terminal_closed")
                )
                continue
            if branded is True and not terminal.branded:
                disqualified.append(
                    _Disqualification(terminal.terminal_id, "unbranded_not_eligible")
                )
                continue
            if branded is False and terminal.branded:
                disqualified.append(
                    _Disqualification(terminal.terminal_id, "branded_not_eligible")
                )
                continue
            eligible.append(terminal)

        return eligible, disqualified

    # ------------------------------------------------------------------
    # Arg validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_recommend_args(
        *,
        tenant_id: str,
        product_code: str,
        volume_gallons: float,
        origin_lat_lon: Tuple[float, float],
        as_of: datetime,
        branded: Optional[bool],
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(product_code, str) or not product_code.strip():
            raise ValueError("product_code must be a non-empty string")
        if not isinstance(volume_gallons, (int, float)) or isinstance(volume_gallons, bool):
            raise TypeError("volume_gallons must be numeric")
        if not math.isfinite(float(volume_gallons)) or float(volume_gallons) <= 0:
            raise ValueError("volume_gallons must be a positive finite number")
        if (
            not isinstance(origin_lat_lon, (tuple, list))
            or len(origin_lat_lon) != 2
        ):
            raise TypeError("origin_lat_lon must be a (lat, lon) pair")
        lat, lon = origin_lat_lon
        if not (-90.0 <= float(lat) <= 90.0):
            raise ValueError("origin latitude out of range [-90, 90]")
        if not (-180.0 <= float(lon) <= 180.0):
            raise ValueError("origin longitude out of range [-180, 180]")
        if not isinstance(as_of, datetime):
            raise TypeError("as_of must be a datetime")
        if branded is not None and not isinstance(branded, bool):
            raise InvalidBrandedPreferenceError(
                f"branded must be bool or None, got {type(branded).__name__}"
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC so downstream math is consistent."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _haversine_km(origin: Tuple[float, float], terminal: Terminal) -> float:
    """Return great-circle distance from ``origin`` to ``terminal`` in km."""

    meters = haversine_distance_meters(
        float(origin[0]),
        float(origin[1]),
        terminal.location_lat,
        terminal.location_lon,
    )
    return meters / 1000.0


def _is_open_at(terminal: Terminal, as_of: datetime) -> bool:
    """Thin backward-compat wrapper over :meth:`Terminal.is_open_at`.

    The open/close window logic moved onto the :class:`Terminal` model
    (Task 7.2) so the ``proposed-load`` REST endpoint (Req 8.1.4) and
    the sourcing recommender share one implementation. This helper is
    retained for any out-of-tree call sites that imported the private
    function before the refactor — the recommender itself now calls
    ``terminal.is_open_at(as_of)`` directly.
    """

    return terminal.is_open_at(as_of)


def _to_local(value: datetime, tz_name: str) -> datetime:
    """Convert ``value`` to the named IANA timezone.

    Returns the value unchanged (assumed UTC-naive) when ZoneInfo is
    unavailable or the timezone string is unrecognised so the caller
    degrades gracefully on a misconfigured terminal.
    """

    utc_value = _ensure_utc(value)
    if ZoneInfo is None:  # pragma: no cover - Python <3.9 fallback
        return utc_value.replace(tzinfo=None)
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning(
            "SourcingRecommender: unknown timezone %r, assuming UTC", tz_name
        )
        return utc_value
    return utc_value.astimezone(tz)


def _find_matching_contract(
    *,
    terminal: Terminal,
    contracts: Sequence[SupplierContract],
    as_of: datetime,
) -> Optional[SupplierContract]:
    """Return the first active contract that covers ``terminal`` on ``as_of``."""

    as_of_date = _ensure_utc(as_of).date()
    for contract in contracts:
        if contract.status != "active":
            continue
        if terminal.terminal_id not in contract.preferred_terminal_ids:
            continue
        if contract.branded_required and not terminal.branded:
            continue
        if contract.effective_from > as_of_date:
            continue
        if contract.effective_to is not None and contract.effective_to < as_of_date:
            continue
        return contract
    return None


def _resolve_price(
    *,
    terminal: Terminal,
    contract: Optional[SupplierContract],
    rack_prices: Mapping[str, float],
    product_code: str,
) -> Optional[float]:
    """Return the effective price per gallon for a terminal.

    When a matching contract specifies ``contract_price_per_gallon_usd``
    the contract price wins (Requirement 8.3.3). Otherwise the live rack
    price is used. Returns ``None`` when neither is available so the
    caller can drop the terminal with reason ``no_price_available``.
    """

    if (
        contract is not None
        and contract.contract_price_per_gallon_usd is not None
        and contract.product_code == product_code
    ):
        return float(contract.contract_price_per_gallon_usd)
    price = rack_prices.get(terminal.terminal_id)
    if price is None:
        return None
    return float(price)


def _rank_candidates(
    *,
    inputs: Sequence[_ScoringInputs],
    weights: SourcingWeights,
    wait_warning_threshold: float,
) -> List[TerminalCandidate]:
    """Normalise signals, score each terminal, and return sorted candidates."""

    if not inputs:
        return []

    w = weights.normalised()
    prices = [i.price for i in inputs]
    waits = [i.wait for i in inputs]
    distances = [i.distance_km for i in inputs]

    # Single-candidate shortcut: all signals are uniformly "best" in a
    # one-horse race so norms collapse to 0 and only the contract bonus
    # differentiates the score.
    price_range = (min(prices), max(prices))
    wait_range = (min(waits), max(waits))
    distance_range = (min(distances), max(distances))

    candidates: List[TerminalCandidate] = []
    for item in inputs:
        norm_price = _normalise(item.price, price_range)
        norm_wait = _normalise(item.wait, wait_range)
        norm_distance = _normalise(item.distance_km, distance_range)
        contract_component = 1.0 if item.contract is not None else 0.0

        raw = (
            -w.price * norm_price
            + -w.wait * norm_wait
            + -w.distance * norm_distance
            + w.contract * contract_component
        )
        # Shift to [0, 1]: raw ∈ [-(w.price + w.wait + w.distance), +w.contract]
        shift = w.price + w.wait + w.distance
        span = shift + w.contract
        score = (raw + shift) / span if span > 0 else 0.0
        # Clamp defensively — floating-point noise can push a perfect
        # candidate to 1.0000000002 which Pydantic's le=1.0 would reject.
        score = max(0.0, min(1.0, score))

        wait_warning = item.wait > wait_warning_threshold
        reasons = _build_reasons(
            item=item,
            norm_price=norm_price,
            norm_wait=norm_wait,
            norm_distance=norm_distance,
            wait_warning=wait_warning,
        )
        candidates.append(
            TerminalCandidate(
                terminal_id=item.terminal.terminal_id,
                price_per_gallon_usd=round(item.price, 6),
                branded_flag=item.terminal.branded,
                contract_id=item.contract.contract_id if item.contract else None,
                avg_wait_minutes=round(item.wait, 2),
                distance_km_from_start=round(item.distance_km, 3),
                score=round(score, 6),
                reasons=reasons,
                wait_warning=wait_warning,
            )
        )

    # Deterministic ordering — primary by -score, secondary tiebreakers
    # make the ranking reproducible so the property test for ranking
    # stability in Requirement 8.5.7 has a concrete contract.
    candidates.sort(
        key=lambda c: (
            -c.score,
            c.price_per_gallon_usd,
            c.avg_wait_minutes,
            c.distance_km_from_start,
            c.terminal_id,
        )
    )
    return candidates


def _normalise(value: float, value_range: Tuple[float, float]) -> float:
    """Min-max normalise a value to ``[0, 1]``; returns 0 when min == max."""

    low, high = value_range
    if high <= low:
        return 0.0
    return (value - low) / (high - low)


def _build_reasons(
    *,
    item: _ScoringInputs,
    norm_price: float,
    norm_wait: float,
    norm_distance: float,
    wait_warning: bool,
) -> List[str]:
    """Human-readable reasoning breadcrumbs surfaced in the dispatcher UI."""

    reasons: List[str] = [
        f"price=${item.price:.4f}/gal",
        f"wait={item.wait:.1f}min",
        f"distance={item.distance_km:.1f}km",
    ]
    if item.contract is not None:
        # Priority boost reason — the dispatcher should see at a glance
        # why a pricier rack is outranking a cheaper one.
        label = f"contract_priority_boost:{item.contract.contract_id}"
        if item.contract.contract_price_per_gallon_usd is not None:
            label += f" (contract_price=${item.contract.contract_price_per_gallon_usd:.4f}/gal)"
        reasons.append(label)
    if norm_price == 0.0:
        reasons.append("best_price")
    if norm_wait == 0.0:
        reasons.append("shortest_wait")
    if norm_distance == 0.0:
        reasons.append("closest")
    if wait_warning:
        reasons.append("wait_warning")
    if item.terminal.branded and item.terminal.supplier_brand:
        reasons.append(f"branded:{item.terminal.supplier_brand}")
    return reasons


__all__ = [
    "DEFAULT_WAIT_WARNING_MINUTES",
    "DEFAULT_WEIGHTS",
    "InvalidBrandedPreferenceError",
    "RackPriceLookup",
    "SourcingRecommender",
    "SourcingRecommenderError",
    "SourcingWeights",
    "TenantConfigHandle",
    "WAIT_WARNING_REDIS_KEY",
    "WEIGHTS_REDIS_KEY",
    "WaitTimeResolver",
]
