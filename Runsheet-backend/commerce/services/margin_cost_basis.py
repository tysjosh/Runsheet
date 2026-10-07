"""CostBasisResolver: landed cost per gallon for one sale (design "Cost basis resolution (FR2)").

``CostBasisResolver(readers, settings, cache=None).resolve(tenant_id,
product_code, terminal_id | None, as_of) -> CostBasis``.

The method order is fixed and is the product rule:

1. unknown product -> ``none`` / ``product_unknown``;
2. adders (terminal-specific beats tenant-wide, per ``adder_type``);
3. an active override in effect (terminal-specific beats tenant-wide);
4. weighted average over the effective-dated lots in ``(as_of - window, as_of]``
   (standalone purchase entries plus BOL lots priced entry -> plan-linked
   contract -> rack at lift);
5. rack fallback when the terminal is attributed and the pick is fresh;
6. otherwise an explicit ``none`` with a reason.

A missing cost is never 0: ``method="none"`` always has
``landed_cost_micros=None``. Only an explicit cost entry may contribute a real
zero; a rack or contract price <= 0 counts as absent (FR2.6).

Simplification 8 (frozen) governs every read here: rack reads are per
(terminal, product) and sorted; the BOL read is per (terminal or ``*``,
window), covers all products and matches product in Python; every cap errors
instead of truncating; every rack pick goes through :func:`select_rack`;
"linked" means ``plan.contract_id`` and the contract dates govern (its
``status`` is ignored); batching and :class:`ReaderCache` are the only
performance tools.

Every store read is tenant-scoped (AC-24): ES-shaped reads go through
``inject_tenant_filter`` and any returned doc whose ``tenant_id`` differs is
dropped and logged at ERROR; entry reads go through the tenant-first
``MarginRepository`` methods.

Reader exceptions propagate. ``MarginService.compute`` turns them into a
``computation_error`` record; ``GET /cost-basis`` returns 503.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from commerce.models.margin import (
    DEFAULT_RACK_STALENESS_DAYS,
    DEFAULT_TIMEZONE,
    DEFAULT_WAC_WINDOW_DAYS,
    CostEntryKind,
    CostMethod,
    MarginValueError,
    NoCostReason,
    gallons_to_milli,
    usd_to_micros_half_up,
    weighted_average_micros,
)
from compliance.services.compliance_es_mappings import TERMINAL_BOLS_INDEX
from fuel.services.fuel_ops_es_mappings import RACK_PRICES_INDEX, SUPPLIER_CONTRACTS_INDEX
from fuel.services.fuel_product_catalog import UnknownFuelProductError, aliases_for, canonicalize
from fuel.terminal_models import SupplierContract
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

#: ``mvp_load_plans``; the literal ``PodService._resolve_loading_plan`` uses.
MVP_LOAD_PLANS_INDEX = "mvp_load_plans"

BOL_PAGE_SIZE = 200
#: More BOL docs (of any product) than this in one read -> computation_error.
BOL_SCAN_CAP = 5_000
RACK_PAGE_SIZE = 500
#: More rack rows than this for one (terminal, product, window) -> computation_error.
RACK_ROW_CAP = 10_000
#: ``ReaderCache`` holds at most this many rack keys and this many BOL keys (LRU).
CACHE_MAX_KEYS = 64
#: Stale-probe page size; the probe re-checks ``effective_at <= as_of`` in Python.
_PROBE_SIZE = 10
#: Store range bounds are widened by this much and re-checked exactly in
#: Python. Documents store ISO strings that a store may compare lexically,
#: and ``Z`` vs ``+00:00`` or missing microseconds would otherwise move a
#: boundary instant to the wrong side.
_BOUND_SLACK = timedelta(seconds=1)

SELECTION_BRAND_MATCH = "brand_match"
SELECTION_UNBRANDED_MAX = "unbranded_max"
SELECTION_BRANDED_MAX = "branded_max"

PRICED_BY_ENTRY = "entry"
PRICED_BY_CONTRACT = "contract"
PRICED_BY_RACK = "rack"

LOT_TYPE_PURCHASE = "purchase"
LOT_TYPE_BOL = "bol"

# BOL exclusion reasons, in check order (design FR2 step 4).
EXCLUDED_NEEDS_CONFIRMATION = "needs_confirmation"
EXCLUDED_NO_TERMINAL = "no_terminal"
EXCLUDED_PRODUCT_UNKNOWN = "product_unknown"
EXCLUDED_NON_POSITIVE_GALLONS = "non_positive_gallons"
EXCLUDED_UNPRICED = "unpriced"

_ANY_TERMINAL = "*"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("cost basis needs timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def parse_doc_datetime(value: Any) -> Optional[datetime]:
    """An ISO timestamp from a document -> aware UTC; naive means UTC; bad -> None."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return canonicalize(value)
    except UnknownFuelProductError:
        return None


def _clean_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Settings snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostBasisSettings:
    """The settings the resolver uses; built from ``MarginRepository.get_settings``."""

    wac_window_days: int = DEFAULT_WAC_WINDOW_DAYS
    rack_staleness_days: int = DEFAULT_RACK_STALENESS_DAYS
    timezone: str = DEFAULT_TIMEZONE

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "CostBasisSettings":
        return cls(
            wac_window_days=int(settings.get("wac_window_days") or DEFAULT_WAC_WINDOW_DAYS),
            rack_staleness_days=int(
                settings.get("rack_staleness_days") or DEFAULT_RACK_STALENESS_DAYS
            ),
            timezone=str(settings.get("timezone") or DEFAULT_TIMEZONE),
        )

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "margin cost basis: unknown settings timezone %r, using %s",
                self.timezone,
                DEFAULT_TIMEZONE,
            )
            return ZoneInfo(DEFAULT_TIMEZONE)


# ---------------------------------------------------------------------------
# Rack rows and the rack selection rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RackRow:
    """A parsed ``rack_prices`` row. ``price_micros`` is quantized once, half-up."""

    rack_price_id: str
    terminal_id: str
    product_code: str
    effective_at: datetime
    price_micros: int
    branded_flag: bool = False
    supplier_brand: Optional[str] = None

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> Optional["RackRow"]:
        """Parse one rack doc; ``None`` when it is unusable (logged at DEBUG)."""

        rack_price_id = _clean_id(doc.get("rack_price_id"))
        terminal_id = _clean_id(doc.get("terminal_id"))
        product = _canonical_or_none(doc.get("product_code"))
        effective_at = parse_doc_datetime(doc.get("effective_at"))
        if not rack_price_id or not terminal_id or product is None or effective_at is None:
            logger.debug("margin cost basis: dropping unusable rack row %s", rack_price_id)
            return None
        try:
            price = usd_to_micros_half_up(doc.get("price_per_gallon_usd"))
        except MarginValueError:
            logger.debug("margin cost basis: dropping rack row %s with no price", rack_price_id)
            return None
        brand = doc.get("supplier_brand")
        return cls(
            rack_price_id=rack_price_id,
            terminal_id=terminal_id,
            product_code=product,
            effective_at=effective_at,
            price_micros=price,
            branded_flag=bool(doc.get("branded_flag")),
            supplier_brand=brand.strip() if isinstance(brand, str) and brand.strip() else None,
        )


@dataclass(frozen=True)
class RackPick:
    row: RackRow
    selection: str
    fresh: bool


def select_rack(
    rows: Iterable[RackRow],
    t: datetime,
    supplier: Optional[str] = None,
    *,
    staleness_days: int = DEFAULT_RACK_STALENESS_DAYS,
) -> Optional[RackPick]:
    """The one rack selection rule (pure; store order never changes the pick).

    1. Candidates: ``effective_at <= t`` and a positive price.
    2. With a ``supplier``, prefer rows whose ``supplier_brand`` case-folds
       equal (``brand_match``); otherwise unbranded rows (``unbranded_max``);
       branded rows (``branded_max``) only if no unbranded row exists.
    3. Latest ``effective_at``; at that instant the highest price (never
       overstates margin); ties by ``rack_price_id`` ascending.
    4. Fresh iff ``t - effective_at <= staleness_days``.
    """

    at = _utc(t)
    candidates = [r for r in rows if r.effective_at <= at and r.price_micros > 0]
    if not candidates:
        return None
    chosen: List[RackRow] = []
    selection = SELECTION_UNBRANDED_MAX
    wanted = supplier.strip().casefold() if isinstance(supplier, str) and supplier.strip() else None
    if wanted is not None:
        chosen = [
            r for r in candidates
            if r.supplier_brand is not None and r.supplier_brand.casefold() == wanted
        ]
        selection = SELECTION_BRAND_MATCH
    if not chosen:
        chosen = [r for r in candidates if not r.branded_flag]
        selection = SELECTION_UNBRANDED_MAX
    if not chosen:
        chosen = candidates
        selection = SELECTION_BRANDED_MAX
    latest = max(r.effective_at for r in chosen)
    at_latest = [r for r in chosen if r.effective_at == latest]
    top = max(r.price_micros for r in at_latest)
    pick = min((r for r in at_latest if r.price_micros == top), key=lambda r: r.rack_price_id)
    fresh = at - pick.effective_at <= timedelta(days=staleness_days)
    return RackPick(row=pick, selection=selection, fresh=fresh)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocPage:
    """One page of an ES-shaped read.

    ``raw_count`` counts hits before the foreign-tenant drop, so paging and
    caps see what the store returned. ``total`` is set only when the store
    reports an exact total.
    """

    docs: List[Dict[str, Any]]
    raw_count: int
    last_sort: Optional[List[Any]]
    total: Optional[int] = None


class EntryReader(Protocol):
    """The tenant-first ``MarginRepository`` entry lookups the resolver uses."""

    async def active_effective_entries(
        self,
        tenant_id: str,
        *,
        kind: str,
        product_code: str,
        terminal_id: Optional[str],
        as_of: datetime,
    ) -> List[Dict[str, Any]]: ...

    async def active_purchase_lots(
        self,
        tenant_id: str,
        *,
        product_code: str,
        terminal_id: Optional[str],
        window_start: datetime,
        as_of: datetime,
    ) -> List[Dict[str, Any]]: ...

    async def active_entries_for_bols(
        self, tenant_id: str, bol_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]: ...


class BolReader(Protocol):
    async def page(
        self,
        tenant_id: str,
        *,
        terminal_id: Optional[str],
        start: datetime,
        end: datetime,
        size: int,
        search_after: Optional[List[Any]],
    ) -> DocPage: ...


class RackReader(Protocol):
    async def page(
        self,
        tenant_id: str,
        *,
        terminal_id: str,
        product_codes: Sequence[str],
        start: datetime,
        end: datetime,
        size: int,
        search_after: Optional[List[Any]],
    ) -> DocPage: ...

    async def latest_positive(
        self,
        tenant_id: str,
        *,
        terminal_id: str,
        product_codes: Sequence[str],
        end: datetime,
    ) -> List[Dict[str, Any]]: ...


class DocsByIdReader(Protocol):
    async def by_ids(self, tenant_id: str, ids: Sequence[str]) -> List[Dict[str, Any]]: ...


def _total_of(response: Mapping[str, Any]) -> Optional[int]:
    total = ((response or {}).get("hits") or {}).get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    if isinstance(total, Mapping) and total.get("relation", "eq") == "eq":
        value = total.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


class _StoreSearch:
    """Tenant-scoped ``search_documents`` with the defensive tenant check."""

    def __init__(self, es_service: Any) -> None:
        self._es = es_service

    async def search(
        self,
        tenant_id: str,
        index: str,
        body: Dict[str, Any],
        *,
        size: int,
        sort_fields: Sequence[str] = (),
    ) -> DocPage:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        wrapped = inject_tenant_filter(dict(body, size=size), tenant_id)
        response = await self._es.search_documents(index, wrapped, size)
        hits = ((response or {}).get("hits") or {}).get("hits") or []
        docs: List[Dict[str, Any]] = []
        for hit in hits:
            source = hit.get("_source") or {}
            if source.get("tenant_id") != tenant_id:
                logger.error(
                    "margin cost basis: dropping %s doc %s from another tenant "
                    "(owner=%s, requester=%s)",
                    index,
                    hit.get("_id"),
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            docs.append(source)
        last_sort: Optional[List[Any]] = None
        if hits:
            last = hits[-1]
            if last.get("sort") is not None:
                last_sort = list(last["sort"])
            elif sort_fields:
                src = last.get("_source") or {}
                last_sort = [src.get(name) for name in sort_fields]
        return DocPage(docs=docs, raw_count=len(hits), last_sort=last_sort, total=_total_of(response))


class StoreBolReader:
    """``terminal_bols`` for (terminal or any terminal, window), all products."""

    def __init__(self, es_service: Any) -> None:
        self._search = _StoreSearch(es_service)

    async def page(
        self,
        tenant_id: str,
        *,
        terminal_id: Optional[str],
        start: datetime,
        end: datetime,
        size: int,
        search_after: Optional[List[Any]],
    ) -> DocPage:
        scope = (
            {"term": {"terminal_id": terminal_id}}
            if terminal_id is not None
            else {"exists": {"field": "terminal_id"}}
        )
        body: Dict[str, Any] = {
            "query": {
                "bool": {
                    "filter": [
                        scope,
                        {
                            "range": {
                                "timestamp": {
                                    "gte": _iso(start - _BOUND_SLACK),
                                    "lte": _iso(end + _BOUND_SLACK),
                                }
                            }
                        },
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "asc"}}, {"bol_id": {"order": "asc"}}],
        }
        if search_after is not None:
            body["search_after"] = list(search_after)
        return await self._search.search(
            tenant_id, TERMINAL_BOLS_INDEX, body, size=size, sort_fields=("timestamp", "bol_id")
        )


class StoreRackReader:
    """``rack_prices`` per (tenant, terminal, canonical product aliases)."""

    def __init__(self, es_service: Any) -> None:
        self._search = _StoreSearch(es_service)

    @staticmethod
    def _filters(terminal_id: str, product_codes: Sequence[str]) -> List[Dict[str, Any]]:
        return [
            {"term": {"terminal_id": terminal_id}},
            {"terms": {"product_code": sorted(product_codes)}},
        ]

    async def page(
        self,
        tenant_id: str,
        *,
        terminal_id: str,
        product_codes: Sequence[str],
        start: datetime,
        end: datetime,
        size: int,
        search_after: Optional[List[Any]],
    ) -> DocPage:
        filters = self._filters(terminal_id, product_codes)
        filters.append(
            {
                "range": {
                    "effective_at": {
                        "gte": _iso(start - _BOUND_SLACK),
                        "lte": _iso(end + _BOUND_SLACK),
                    }
                }
            }
        )
        body: Dict[str, Any] = {
            "query": {"bool": {"filter": filters}},
            "sort": [{"effective_at": {"order": "desc"}}, {"rack_price_id": {"order": "desc"}}],
        }
        if search_after is not None:
            body["search_after"] = list(search_after)
        return await self._search.search(
            tenant_id,
            RACK_PRICES_INDEX,
            body,
            size=size,
            sort_fields=("effective_at", "rack_price_id"),
        )

    async def latest_positive(
        self,
        tenant_id: str,
        *,
        terminal_id: str,
        product_codes: Sequence[str],
        end: datetime,
    ) -> List[Dict[str, Any]]:
        filters = self._filters(terminal_id, product_codes)
        filters.append({"range": {"effective_at": {"lte": _iso(end + _BOUND_SLACK)}}})
        filters.append({"range": {"price_per_gallon_usd": {"gt": 0}}})
        body = {
            "query": {"bool": {"filter": filters}},
            "sort": [{"effective_at": {"order": "desc"}}, {"rack_price_id": {"order": "desc"}}],
        }
        page = await self._search.search(tenant_id, RACK_PRICES_INDEX, body, size=_PROBE_SIZE)
        return page.docs


class StoreDocsByIdReader:
    """Batched tenant-filtered ``terms`` read by id (plans, contracts)."""

    def __init__(self, es_service: Any, *, index: str, id_field: str) -> None:
        self._search = _StoreSearch(es_service)
        self._index = index
        self._id_field = id_field

    async def by_ids(self, tenant_id: str, ids: Sequence[str]) -> List[Dict[str, Any]]:
        unique = sorted({i for i in ids if i})
        if not unique:
            return []
        body = {"query": {"terms": {self._id_field: unique}}}
        page = await self._search.search(tenant_id, self._index, body, size=len(unique))
        return page.docs


@dataclass(frozen=True)
class CostBasisReaders:
    """The five injected readers (design: entries, bols, rack, plans, contracts)."""

    entries: EntryReader
    bols: BolReader
    rack: RackReader
    plans: DocsByIdReader
    contracts: DocsByIdReader

    @classmethod
    def from_store(cls, es_service: Any, entries: EntryReader) -> "CostBasisReaders":
        """Production readers over the document store and the margin repository."""

        return cls(
            entries=entries,
            bols=StoreBolReader(es_service),
            rack=StoreRackReader(es_service),
            plans=StoreDocsByIdReader(es_service, index=MVP_LOAD_PLANS_INDEX, id_field="plan_id"),
            contracts=StoreDocsByIdReader(
                es_service, index=SUPPLIER_CONTRACTS_INDEX, id_field="contract_id"
            ),
        )


# ---------------------------------------------------------------------------
# ReaderCache
# ---------------------------------------------------------------------------


@dataclass
class _RackRead:
    start: datetime
    end: datetime
    rows: List[RackRow]
    cap_exceeded: bool = False
    rows_read: int = 0

    def covers(self, start: datetime, end: datetime) -> bool:
        return self.start <= start and self.end >= end


@dataclass
class _BolRead:
    docs: List[Dict[str, Any]]
    entries_by_bol: Dict[str, Dict[str, Any]]
    scanned: int
    cap_exceeded: bool = False


class _Lru:
    def __init__(self, max_keys: int) -> None:
        self._max = max_keys
        self._data: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()

    def get(self, key: Tuple[Any, ...]) -> Any:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, key: Tuple[Any, ...], value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self) -> List[Tuple[Any, ...]]:
        return list(self._data)


class ReaderCache:
    """A per-run read cache: no TTL, never shared across runs.

    One is created per gap-sweep tenant pass and per recompute run. Live
    hooks, ``/cost-basis`` and preview pass none (the resolver then uses a
    private cache for the one ``resolve`` call, so a single resolution never
    reads the same thing twice). Holds plan docs by id, contract docs by id,
    rack reads by (terminal, product) with the widest window read, BOL reads
    by (terminal or ``*``, window) without product, and entry query results.
    Rack and BOL keys are LRU-bounded at 64 each.
    """

    def __init__(self, *, max_rack_keys: int = CACHE_MAX_KEYS, max_bol_keys: int = CACHE_MAX_KEYS) -> None:
        self.plans: Dict[str, Optional[Dict[str, Any]]] = {}
        self.contracts: Dict[str, Optional[SupplierContract]] = {}
        self.rack = _Lru(max_rack_keys)
        self.bols = _Lru(max_bol_keys)
        self.entries: Dict[Tuple[Any, ...], Any] = {}


# ---------------------------------------------------------------------------
# CostBasis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostBasis:
    """The resolver output. :meth:`to_snapshot` is the record's ``cost_snapshot``."""

    method: str
    product_code: str
    terminal_id: Optional[str]
    as_of: datetime
    window_start: Optional[datetime]
    wac_window_days: int
    rack_staleness_days: int
    product_cost_micros: Optional[int]
    adders_micros: Optional[int]
    adders: Tuple[Dict[str, Any], ...] = ()
    adders_configured: bool = False
    landed_cost_micros: Optional[int] = None
    override_entry_id: Optional[str] = None
    lots: Tuple[Dict[str, Any], ...] = ()
    rack_price_id: Optional[str] = None
    rack_selection: Optional[str] = None
    contract_ids: Tuple[str, ...] = ()
    no_cost_reason: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The product rule, enforced at construction: missing cost is never 0.
        is_none = self.method == CostMethod.NONE.value
        if is_none != (self.landed_cost_micros is None):
            raise ValueError("method='none' if and only if landed_cost_micros is None")
        if is_none != (self.product_cost_micros is None):
            raise ValueError("method='none' if and only if product_cost_micros is None")
        if is_none and not self.no_cost_reason:
            raise ValueError("method='none' needs a no_cost_reason")

    @property
    def terminal_unattributed(self) -> bool:
        return self.terminal_id is None

    @property
    def lot_gallons_milli(self) -> int:
        return sum(int(lot["gallons_milli"]) for lot in self.lots)

    def to_snapshot(self) -> Dict[str, Any]:
        """JSON-safe dict (design FR2 snapshot fields)."""

        return {
            "method": self.method,
            "product_code": self.product_code,
            "terminal_id": self.terminal_id,
            "product_cost_micros": self.product_cost_micros,
            "adders_micros": self.adders_micros,
            "adders": [dict(a) for a in self.adders],
            "adders_configured": self.adders_configured,
            "landed_cost_micros": self.landed_cost_micros,
            "override_entry_id": self.override_entry_id,
            "lots": [dict(lot) for lot in self.lots],
            "rack_price_id": self.rack_price_id,
            "rack_selection": self.rack_selection,
            "contract_ids": list(self.contract_ids),
            "wac_window_days": self.wac_window_days,
            "rack_staleness_days": self.rack_staleness_days,
            "window_start": _iso(self.window_start),
            "as_of": _iso(self.as_of),
            "terminal_unattributed": self.terminal_unattributed,
            "no_cost_reason": self.no_cost_reason,
            "diagnostics": {
                "excluded": dict(sorted((self.diagnostics.get("excluded") or {}).items())),
                "bols_scanned": int(self.diagnostics.get("bols_scanned") or 0),
                "lot_cap_exceeded": bool(self.diagnostics.get("lot_cap_exceeded")),
                "rack_cap_exceeded": bool(self.diagnostics.get("rack_cap_exceeded")),
                "zero_price_ignored": int(self.diagnostics.get("zero_price_ignored") or 0),
            },
        }


class _CapExceeded(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass
class _Work:
    """Mutable per-resolve state; frozen into a :class:`CostBasis` at the end."""

    tenant_id: str
    product_raw: str
    terminal_id: Optional[str]
    as_of: datetime
    product: Optional[str] = None
    window_start: Optional[datetime] = None
    adders: List[Dict[str, Any]] = field(default_factory=list)
    adders_micros: Optional[int] = None
    excluded: Dict[str, int] = field(default_factory=dict)
    bols_scanned: int = 0
    lot_cap_exceeded: bool = False
    rack_cap_exceeded: bool = False
    zero_price_ignored: int = 0
    zero_counted: set = field(default_factory=set)
    lots: List[Dict[str, Any]] = field(default_factory=list)
    contract_ids: List[str] = field(default_factory=list)

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class CostBasisResolver:
    """Pure cost-basis resolution over injected readers (design FR2, steps 1-7)."""

    def __init__(
        self,
        readers: CostBasisReaders,
        settings: CostBasisSettings | Mapping[str, Any] | None = None,
        *,
        cache: Optional[ReaderCache] = None,
    ) -> None:
        self._readers = readers
        if settings is None:
            settings = CostBasisSettings()
        elif not isinstance(settings, CostBasisSettings):
            settings = CostBasisSettings.from_settings(settings)
        self._settings = settings
        self._zone = settings.zone()
        self._cache = cache

    @property
    def settings(self) -> CostBasisSettings:
        return self._settings

    async def resolve(
        self,
        tenant_id: str,
        product_code: str,
        terminal_id: Optional[str],
        as_of: datetime,
    ) -> CostBasis:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        cache = self._cache if self._cache is not None else ReaderCache()
        work = _Work(
            tenant_id=tenant_id,
            product_raw=str(product_code or ""),
            terminal_id=_clean_id(terminal_id),
            as_of=_utc(as_of),
        )

        # 1. Product.
        work.product = _canonical_or_none(product_code)
        if work.product is None:
            return self._none(work, NoCostReason.PRODUCT_UNKNOWN)

        # 2. Adders (D8, AC-10).
        adder_entries = await self._effective_entries(cache, work, CostEntryKind.ADDER.value)
        chosen_adders = self._pick_per_type(adder_entries, work.terminal_id)
        work.adders = chosen_adders
        work.adders_micros = sum(int(a["micros"]) for a in chosen_adders)

        # 3. Override (AC-1).
        overrides = await self._effective_entries(cache, work, CostEntryKind.OVERRIDE.value)
        override = self._pick_one(overrides, work.terminal_id)
        if override is not None:
            return self._costed(
                work,
                CostMethod.OVERRIDE,
                int(override["unit_cost_micros"]),
                override_entry_id=override["entry_id"],
            )

        # 4. WAC (AC-2 to AC-6).
        work.window_start = work.as_of - timedelta(days=self._settings.wac_window_days)
        try:
            await self._collect_lots(cache, work)
            if sum(int(lot["gallons_milli"]) for lot in work.lots) > 0:
                wac = weighted_average_micros(
                    (int(lot["gallons_milli"]), int(lot["unit_cost_micros"])) for lot in work.lots
                )
                if wac is not None:
                    return self._costed(work, CostMethod.WAC, wac)

            # 6. Unattributed with nothing found (AC-11): no rack fallback.
            if work.terminal_id is None:
                return self._none(work, NoCostReason.TERMINAL_UNATTRIBUTED_NO_COST)

            # 5. Rack fallback (AC-7).
            return await self._rack_fallback(cache, work)
        except _CapExceeded as cap:
            logger.error(
                "margin cost basis: %s cap exceeded tenant=%s terminal=%s product=%s "
                "window=(%s, %s] bols_scanned=%d",
                cap.kind,
                tenant_id,
                work.terminal_id or _ANY_TERMINAL,
                work.product,
                _iso(work.window_start),
                _iso(work.as_of),
                work.bols_scanned,
            )
            work.lots = []
            work.contract_ids = []
            return self._none(work, NoCostReason.COMPUTATION_ERROR)

    # -- result builders ---------------------------------------------------

    def _base(self, work: _Work) -> Dict[str, Any]:
        return dict(
            product_code=work.product or work.product_raw,
            terminal_id=work.terminal_id,
            as_of=work.as_of,
            window_start=work.window_start,
            wac_window_days=self._settings.wac_window_days,
            rack_staleness_days=self._settings.rack_staleness_days,
            adders_micros=work.adders_micros,
            adders=tuple(work.adders),
            adders_configured=bool(work.adders),
            diagnostics={
                "excluded": dict(work.excluded),
                "bols_scanned": work.bols_scanned,
                "lot_cap_exceeded": work.lot_cap_exceeded,
                "rack_cap_exceeded": work.rack_cap_exceeded,
                "zero_price_ignored": work.zero_price_ignored,
            },
        )

    def _none(self, work: _Work, reason: NoCostReason) -> CostBasis:
        return CostBasis(
            method=CostMethod.NONE.value,
            product_cost_micros=None,
            landed_cost_micros=None,
            no_cost_reason=reason.value,
            lots=tuple(work.lots),
            contract_ids=tuple(sorted(set(work.contract_ids))),
            **self._base(work),
        )

    def _costed(
        self,
        work: _Work,
        method: CostMethod,
        product_cost_micros: int,
        *,
        override_entry_id: Optional[str] = None,
        rack: Optional[RackPick] = None,
    ) -> CostBasis:
        adders = work.adders_micros or 0
        return CostBasis(
            method=method.value,
            product_cost_micros=product_cost_micros,
            landed_cost_micros=product_cost_micros + adders,
            override_entry_id=override_entry_id,
            lots=tuple(work.lots),
            contract_ids=tuple(sorted(set(work.contract_ids))),
            rack_price_id=rack.row.rack_price_id if rack is not None else None,
            rack_selection=rack.selection if rack is not None else None,
            **self._base(work),
        )

    # -- entries -----------------------------------------------------------

    async def _effective_entries(
        self, cache: ReaderCache, work: _Work, kind: str
    ) -> List[Dict[str, Any]]:
        key = ("effective", kind, work.product, work.terminal_id, work.as_of)
        if key not in cache.entries:
            cache.entries[key] = await self._readers.entries.active_effective_entries(
                work.tenant_id,
                kind=kind,
                product_code=work.product,
                terminal_id=work.terminal_id,
                as_of=work.as_of,
            )
        return list(cache.entries[key])

    @staticmethod
    def _ordered(entries: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
        """Latest ``effective_at``, then latest ``created_at``, then highest ``entry_id``."""

        return sorted(
            entries,
            key=lambda e: (e["effective_at"], e.get("created_at") or e["effective_at"], e["entry_id"]),
            reverse=True,
        )

    def _eligible(
        self, entries: Iterable[Mapping[str, Any]], terminal_id: Optional[str]
    ) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
        """(terminal-specific, tenant-wide) entries in effect at the resolution instant."""

        terminal_scope: List[Mapping[str, Any]] = []
        tenant_scope: List[Mapping[str, Any]] = []
        for entry in entries:
            entry_terminal = entry.get("terminal_id")
            if entry_terminal is None:
                tenant_scope.append(entry)
            elif terminal_id is not None and entry_terminal == terminal_id:
                terminal_scope.append(entry)
        return self._ordered(terminal_scope), self._ordered(tenant_scope)

    def _pick_one(
        self, entries: Iterable[Mapping[str, Any]], terminal_id: Optional[str]
    ) -> Optional[Mapping[str, Any]]:
        terminal_scope, tenant_scope = self._eligible(entries, terminal_id)
        if terminal_scope:
            return terminal_scope[0]
        if tenant_scope:
            return tenant_scope[0]
        return None

    def _pick_per_type(
        self, entries: Iterable[Mapping[str, Any]], terminal_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        by_type: Dict[str, List[Mapping[str, Any]]] = {}
        for entry in entries:
            by_type.setdefault(str(entry.get("adder_type")), []).append(entry)
        chosen: List[Dict[str, Any]] = []
        for adder_type in sorted(by_type):
            terminal_scope, tenant_scope = self._eligible(by_type[adder_type], terminal_id)
            if terminal_scope:
                pick, scope = terminal_scope[0], "terminal"
            elif tenant_scope:
                pick, scope = tenant_scope[0], "tenant"
            else:
                continue
            chosen.append(
                {
                    "adder_type": adder_type,
                    "entry_id": pick["entry_id"],
                    "micros": int(pick["unit_cost_micros"]),
                    "scope": scope,
                }
            )
        return chosen

    # -- lots --------------------------------------------------------------

    async def _collect_lots(self, cache: ReaderCache, work: _Work) -> None:
        assert work.window_start is not None and work.product is not None
        key = ("purchase", work.product, work.terminal_id, work.window_start, work.as_of)
        if key not in cache.entries:
            cache.entries[key] = await self._readers.entries.active_purchase_lots(
                work.tenant_id,
                product_code=work.product,
                terminal_id=work.terminal_id,
                window_start=work.window_start,
                as_of=work.as_of,
            )
        for entry in cache.entries[key]:
            work.lots.append(
                {
                    "lot_type": LOT_TYPE_PURCHASE,
                    "id": entry["entry_id"],
                    "gallons_milli": int(entry["gallons_milli"]),
                    "unit_cost_micros": int(entry["unit_cost_micros"]),
                    "priced_by": PRICED_BY_ENTRY,
                    "price_ref_id": entry["entry_id"],
                }
            )

        bol_read = await self._bol_read(cache, work)
        work.bols_scanned = bol_read.scanned
        if bol_read.cap_exceeded:
            work.lot_cap_exceeded = True
            raise _CapExceeded("bol_scan")

        for doc in bol_read.docs:
            timestamp = parse_doc_datetime(doc.get("timestamp"))
            if timestamp is None or not (work.window_start < timestamp <= work.as_of):
                continue  # outside the exact window (store bounds are widened)
            if doc.get("needs_operator_confirmation") is True or doc.get("status") == "pending_confirmation":
                work.exclude(EXCLUDED_NEEDS_CONFIRMATION)
                continue
            bol_terminal = _clean_id(doc.get("terminal_id"))
            if bol_terminal is None:
                work.exclude(EXCLUDED_NO_TERMINAL)
                continue
            bol_product = _canonical_or_none(doc.get("product_code"))
            if bol_product is None:
                work.exclude(EXCLUDED_PRODUCT_UNKNOWN)
                continue
            if bol_product != work.product:
                continue  # another product: not an exclusion
            gallons_milli = _bol_gallons_milli(doc)
            if gallons_milli is None or gallons_milli <= 0:
                work.exclude(EXCLUDED_NON_POSITIVE_GALLONS)
                continue
            lot = await self._price_bol(cache, work, doc, bol_terminal, timestamp, bol_read)
            if lot is None:
                work.exclude(EXCLUDED_UNPRICED)
                continue
            lot["gallons_milli"] = gallons_milli
            work.lots.append(lot)

    async def _bol_read(self, cache: ReaderCache, work: _Work) -> _BolRead:
        assert work.window_start is not None
        key = (work.terminal_id or _ANY_TERMINAL, work.window_start, work.as_of)
        cached = cache.bols.get(key)
        if cached is not None:
            return cached

        readers = self._readers
        docs: List[Dict[str, Any]] = []
        entries_by_bol: Dict[str, Dict[str, Any]] = {}
        scanned = 0
        after: Optional[List[Any]] = None
        result: Optional[_BolRead] = None
        while True:
            page = await readers.bols.page(
                work.tenant_id,
                terminal_id=work.terminal_id,
                start=work.window_start,
                end=work.as_of,
                size=BOL_PAGE_SIZE,
                search_after=after,
            )
            scanned += page.raw_count
            if scanned > BOL_SCAN_CAP or (page.total is not None and page.total > BOL_SCAN_CAP):
                result = _BolRead(docs=[], entries_by_bol={}, scanned=scanned, cap_exceeded=True)
                break
            docs.extend(page.docs)
            await self._batch_for_page(cache, work, page.docs, entries_by_bol)
            if page.raw_count < BOL_PAGE_SIZE or page.last_sort is None:
                break
            if page.total is not None and scanned >= page.total:
                break
            after = page.last_sort
        if result is None:
            result = _BolRead(docs=docs, entries_by_bol=entries_by_bol, scanned=scanned)
        cache.bols.put(key, result)
        return result

    async def _batch_for_page(
        self,
        cache: ReaderCache,
        work: _Work,
        page_docs: Sequence[Mapping[str, Any]],
        entries_by_bol: Dict[str, Dict[str, Any]],
    ) -> None:
        """Per BOL page: one entry ``IN``, one plan ``terms``, one contract ``terms``."""

        readers = self._readers
        bol_ids = sorted({b for b in (_clean_id(d.get("bol_id")) for d in page_docs) if b})
        if bol_ids:
            found = await readers.entries.active_entries_for_bols(work.tenant_id, bol_ids)
            entries_by_bol.update(found)

        plan_ids = set()
        for doc in page_docs:
            bol_id = _clean_id(doc.get("bol_id"))
            plan_id = _clean_id(doc.get("load_plan_id"))
            if plan_id is None or (bol_id is not None and bol_id in entries_by_bol):
                continue
            if not _could_be_lot(doc):
                continue
            if plan_id not in cache.plans:
                plan_ids.add(plan_id)
        if plan_ids:
            plans = await readers.plans.by_ids(work.tenant_id, sorted(plan_ids))
            for plan in plans:
                plan_id = _clean_id(plan.get("plan_id"))
                if plan_id is not None and plan.get("tenant_id") == work.tenant_id:
                    cache.plans[plan_id] = plan
            for plan_id in plan_ids:
                cache.plans.setdefault(plan_id, None)

        contract_ids = set()
        for doc in page_docs:
            plan = cache.plans.get(_clean_id(doc.get("load_plan_id")) or "")
            contract_id = _clean_id((plan or {}).get("contract_id"))
            if contract_id is not None and contract_id not in cache.contracts:
                contract_ids.add(contract_id)
        if contract_ids:
            contracts = await readers.contracts.by_ids(work.tenant_id, sorted(contract_ids))
            for doc in contracts:
                if doc.get("tenant_id") != work.tenant_id:
                    logger.error(
                        "margin cost basis: dropping supplier contract %s from another tenant "
                        "(owner=%s, requester=%s)",
                        doc.get("contract_id"),
                        doc.get("tenant_id"),
                        work.tenant_id,
                    )
                    continue
                contract = _parse_contract(doc)
                if contract is not None:
                    cache.contracts[contract.contract_id] = contract
            for contract_id in contract_ids:
                cache.contracts.setdefault(contract_id, None)

    async def _price_bol(
        self,
        cache: ReaderCache,
        work: _Work,
        doc: Mapping[str, Any],
        bol_terminal: str,
        timestamp: datetime,
        bol_read: _BolRead,
    ) -> Optional[Dict[str, Any]]:
        """D5 pricing, first match wins: entry -> plan-linked contract -> rack at lift."""

        bol_id = _clean_id(doc.get("bol_id")) or ""
        entry = bol_read.entries_by_bol.get(bol_id)
        if entry is not None:
            return {
                "lot_type": LOT_TYPE_BOL,
                "id": bol_id,
                "unit_cost_micros": int(entry["unit_cost_micros"]),
                "priced_by": PRICED_BY_ENTRY,
                "price_ref_id": entry["entry_id"],
            }

        plan = cache.plans.get(_clean_id(doc.get("load_plan_id")) or "")
        contract_id = _clean_id((plan or {}).get("contract_id"))
        contract = cache.contracts.get(contract_id) if contract_id else None
        if contract is not None:
            lift_date: date = timestamp.astimezone(self._zone).date()
            in_dates = contract.effective_from <= lift_date and (
                contract.effective_to is None or lift_date <= contract.effective_to
            )
            if in_dates and contract.contract_price_per_gallon_usd is not None:
                try:
                    contract_micros = usd_to_micros_half_up(
                        contract.contract_price_per_gallon_usd,
                        field="contract_price_per_gallon_usd",
                    )
                except MarginValueError:
                    contract_micros = 0
                if contract_micros > 0:
                    work.contract_ids.append(contract.contract_id)
                    return {
                        "lot_type": LOT_TYPE_BOL,
                        "id": bol_id,
                        "unit_cost_micros": contract_micros,
                        "priced_by": PRICED_BY_CONTRACT,
                        "price_ref_id": contract.contract_id,
                        "contract_status_at_compute": contract.status,
                    }
                work.zero_price_ignored += 1

        rows = await self._rack_rows(cache, work, bol_terminal)
        supplier = doc.get("supplier_name") if isinstance(doc.get("supplier_name"), str) else None
        pick = select_rack(
            rows, timestamp, supplier, staleness_days=self._settings.rack_staleness_days
        )
        if pick is None or not pick.fresh:
            return None
        return {
            "lot_type": LOT_TYPE_BOL,
            "id": bol_id,
            "unit_cost_micros": pick.row.price_micros,
            "priced_by": PRICED_BY_RACK,
            "price_ref_id": pick.row.rack_price_id,
            "rack_selection": pick.selection,
        }

    # -- rack --------------------------------------------------------------

    def _rack_window(self, work: _Work) -> Tuple[datetime, datetime]:
        assert work.window_start is not None
        return (
            work.window_start - timedelta(days=self._settings.rack_staleness_days),
            work.as_of,
        )

    async def _rack_rows(self, cache: ReaderCache, work: _Work, terminal_id: str) -> List[RackRow]:
        """Positive rack rows for (terminal, product) in the rack window.

        Zero-price rows in the window are counted in ``zero_price_ignored``
        once per (resolve, terminal). Raises ``_CapExceeded`` past 10,000 rows.
        """

        assert work.product is not None
        start, end = self._rack_window(work)
        key = (terminal_id, work.product)
        cached: Optional[_RackRead] = cache.rack.get(key)
        if cached is None or not cached.covers(start, end) or (
            cached.cap_exceeded and (cached.start, cached.end) != (start, end)
        ):
            cached = await self._read_rack(work, terminal_id, start, end)
            cache.rack.put(key, cached)
        if cached.cap_exceeded:
            work.rack_cap_exceeded = True
            raise _CapExceeded("rack_rows")
        in_window = [r for r in cached.rows if start <= r.effective_at <= end]
        if key not in work.zero_counted:
            work.zero_price_ignored += sum(1 for r in in_window if r.price_micros <= 0)
            work.zero_counted.add(key)
        return [r for r in in_window if r.price_micros > 0]

    async def _read_rack(
        self, work: _Work, terminal_id: str, start: datetime, end: datetime
    ) -> _RackRead:
        assert work.product is not None
        aliases = sorted(aliases_for(work.product))
        rows: List[RackRow] = []
        read = 0
        after: Optional[List[Any]] = None
        while True:
            page = await self._readers.rack.page(
                work.tenant_id,
                terminal_id=terminal_id,
                product_codes=aliases,
                start=start,
                end=end,
                size=RACK_PAGE_SIZE,
                search_after=after,
            )
            read += page.raw_count
            if read > RACK_ROW_CAP or (page.total is not None and page.total > RACK_ROW_CAP):
                return _RackRead(start=start, end=end, rows=[], cap_exceeded=True, rows_read=read)
            for doc in page.docs:
                row = RackRow.from_doc(doc)
                # Re-check the product (alias terms only narrow the read).
                if row is None or row.product_code != work.product or row.terminal_id != terminal_id:
                    continue
                rows.append(row)
            if page.raw_count < RACK_PAGE_SIZE or page.last_sort is None:
                break
            if page.total is not None and read >= page.total:
                break
            after = page.last_sort
        return _RackRead(start=start, end=end, rows=rows, rows_read=read)

    async def _rack_fallback(self, cache: ReaderCache, work: _Work) -> CostBasis:
        assert work.terminal_id is not None and work.product is not None
        rows = await self._rack_rows(cache, work, work.terminal_id)
        pick = select_rack(
            rows, work.as_of, None, staleness_days=self._settings.rack_staleness_days
        )
        if pick is not None:
            if pick.fresh:
                return self._costed(work, CostMethod.RACK_FALLBACK, pick.row.price_micros, rack=pick)
            return self._none(work, NoCostReason.RACK_STALE)
        # No positive row in the window: one probe with no lower bound decides
        # between "a row exists but is old" and "no rack at all".
        probe = await self._readers.rack.latest_positive(
            work.tenant_id,
            terminal_id=work.terminal_id,
            product_codes=sorted(aliases_for(work.product)),
            end=work.as_of,
        )
        for doc in probe:
            row = RackRow.from_doc(doc)
            if (
                row is not None
                and row.product_code == work.product
                and row.price_micros > 0
                and row.effective_at <= work.as_of
            ):
                return self._none(work, NoCostReason.RACK_STALE)
        return self._none(work, NoCostReason.NO_LOTS_NO_RACK)


def _bol_gallons_milli(doc: Mapping[str, Any]) -> Optional[int]:
    try:
        return gallons_to_milli(doc.get("net_gallons"))
    except MarginValueError:
        return None


def _could_be_lot(doc: Mapping[str, Any]) -> bool:
    """Product-agnostic BOL checks, so plan/contract reads skip hopeless docs."""

    if doc.get("needs_operator_confirmation") is True or doc.get("status") == "pending_confirmation":
        return False
    if _clean_id(doc.get("terminal_id")) is None or _canonical_or_none(doc.get("product_code")) is None:
        return False
    gallons = _bol_gallons_milli(doc)
    return gallons is not None and gallons > 0


def _parse_contract(doc: Mapping[str, Any]) -> Optional[SupplierContract]:
    try:
        return SupplierContract(**{k: v for k, v in doc.items() if k in SupplierContract.model_fields})
    except Exception as exc:  # noqa: BLE001 - a bad doc prices nothing
        logger.warning(
            "margin cost basis: ignoring supplier contract %s that failed validation: %s",
            doc.get("contract_id"),
            type(exc).__name__,
        )
        return None


__all__ = [
    "BOL_PAGE_SIZE",
    "BOL_SCAN_CAP",
    "CACHE_MAX_KEYS",
    "CostBasis",
    "CostBasisReaders",
    "CostBasisResolver",
    "CostBasisSettings",
    "DocPage",
    "MVP_LOAD_PLANS_INDEX",
    "RACK_PAGE_SIZE",
    "RACK_ROW_CAP",
    "RackPick",
    "RackRow",
    "ReaderCache",
    "StoreBolReader",
    "StoreDocsByIdReader",
    "StoreRackReader",
    "parse_doc_datetime",
    "select_rack",
]
