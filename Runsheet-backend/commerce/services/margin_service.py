"""MarginService: two-phase margin computation, hooks, preview and summary.

Design sections: "Margin records (FR3)" (stage inputs, two-phase compute,
execution and isolation), "Publishing (margin service)", "Admin API"
(preview, summary stage preference) and Simplification 9.

* **Phase 1** (:func:`extract_inputs`) is a pure function over a deep-copied
  source. It returns :class:`Inputs` or a :class:`Skip`. The invoice stage
  reads the persisted ``line_items[i]`` (after the OI-14 split) and never
  calls a pricing engine (AC-14).
* **Phase 2** (:meth:`MarginService.compute`) resolves the terminal, loads the
  settings, resolves the cost basis and computes the money fields and flags.
  Only an exception here produces a ``computation_error`` record
  (``method=none``, ``missing_cost``), built from the phase-1 inputs.
* :class:`MarginHook` is what :class:`InvoiceService` and the order
  subscriber call. Its methods are synchronous: they deep-copy the source and
  schedule a tracked task, so margin work can never fail or block the invoice
  or order operation (AC-16). Every exception is caught and logged.

Margin values are logged only at DEBUG; INFO/ERROR lines carry ids (NFR5).
Missing cost is never 0: a ``none`` cost basis keeps every cost field null.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from commerce.models.margin import (
    DEFAULT_FLOOR_MICROS,
    DEFAULT_TIMEZONE,
    MAX_UNIT_COST_MICROS,
    MICROS_PER_CENT,
    CostMethod,
    MarginFlag,
    MarginPreviewRequest,
    MarginRecomputeRequest,
    MarginStage,
    MarginValueError,
    NoCostReason,
    WriteMode,
    compute_margin_amounts,
    doc_decimal,
    gallons_to_ugal,
    margin_bp,
    margin_pct,
    parse_gallons_milli,
    parse_usd_micros,
    quantize_half_up,
    ugal_to_gallons,
)
from commerce.services.margin_attribution import MarginAttribution
from commerce.services.margin_cost_basis import (
    CostBasis,
    CostBasisReaders,
    CostBasisResolver,
    ReaderCache,
    parse_doc_datetime,
)
from commerce.services.margin_repository import (
    MarginAlertNotFoundError,
    MarginAlertStateError,
    MarginCandidate,
    MarginRecomputeRunningError,
    MarginRepository,
    WriteResult,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize
from services.money import line_subtotal_cents, unit_price_micros_from_record
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hook backpressure: with this many margin tasks pending, a hook schedules
#: nothing and logs WARNING; the gap sweep repairs delivery and invoice keys.
MARGIN_HOOK_MAX_PENDING_TASKS = 2_000
#: Concurrent margin computations per process.
MARGIN_HOOK_CONCURRENCY = 8
#: ``drain`` default on shutdown.
DRAIN_TIMEOUT_SECONDS = 10.0

SIGNAL_SOURCE_AGENT = "margin_feed"
SIGNAL_TTL_SECONDS = 86_400
RECOMPUTE_SIGNAL_ENTITY = "margin_recompute_run"

SUMMARY_MAX_SPAN_DAYS = 92
SUMMARY_GROUPS = ("day", "customer", "product", "terminal")

SKIP_NO_INPUTS = "no_inputs"
SKIP_INVALID_INPUTS = "invalid_inputs"

WARNING_CONTRACT_SPLIT = "contract_split_not_modeled"

_INVOICE_DRAFT = "draft"
_INVOICE_VOID = "void"
_PRODUCT_MAX = 64

#: In-process ``margin_skips{reason}`` counter (phase-1 skips).
margin_skips: "Counter[str]" = Counter()


def order_source_key(order_id: str) -> str:
    return f"order:{order_id}"


def invoice_source_key(invoice_id: str, line_index: int) -> str:
    return f"invoice:{invoice_id}:line:{line_index}"


# ---------------------------------------------------------------------------
# Phase 1: inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    """Valid phase-1 inputs for one source key (ids, product, money, as-of)."""

    tenant_id: str
    stage: str
    source_key: str
    product_code: str
    gallons_ugal: int
    unit_price_micros: int
    revenue_cents: int
    as_of: datetime
    order_id: Optional[str] = None
    invoice_id: Optional[str] = None
    line_index: Optional[int] = None
    line_id: Optional[str] = None
    customer_id: Optional[str] = None
    account_id: Optional[str] = None
    #: True for an invoice that is no longer a draft (recompute freezes it).
    source_final: bool = False
    #: The order document, for terminal attribution (order stages only).
    order: Optional[Mapping[str, Any]] = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Skip:
    """No record for this key. ``reason`` is ``no_inputs`` or ``invalid_inputs``."""

    reason: str
    error_type: str
    tenant_id: str
    stage: str
    source_key: str
    order_id: Optional[str] = None
    invoice_id: Optional[str] = None
    line_index: Optional[int] = None


Phase1 = Union[Inputs, Skip]


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _product(value: Any) -> str:
    """Canonical code, else the raw value truncated to 64, else ``unknown``."""

    raw = _clean(value)
    if raw is None:
        return "unknown"
    try:
        return canonicalize(raw)
    except (UnknownFuelProductError, TypeError):
        return raw[:_PRODUCT_MAX]


def _gallons(value: Any) -> Union[int, str]:
    """Gallons -> micro-gallons, or an ``error_type`` string."""

    try:
        doc_decimal(value, field="gallons")
        ugal = gallons_to_ugal(value)
    except MarginValueError:
        return "gallons_invalid"
    if ugal <= 0:
        return "gallons_not_positive"
    return ugal


def source_keys(stage: str, source: Mapping[str, Any]) -> List[Tuple[str, Optional[int]]]:
    """Every ``(source_key, line_index)`` a source produces, without parsing values."""

    if stage == MarginStage.INVOICE.value:
        invoice_id = _clean(source.get("invoice_id")) or ""
        lines = source.get("line_items") or []
        if not isinstance(lines, (list, tuple)):
            return []
        return [(invoice_source_key(invoice_id, i), i) for i in range(len(lines))]
    return [(order_source_key(_clean(source.get("order_id")) or ""), None)]


def extract_inputs(
    stage: str,
    source: Mapping[str, Any],
    *,
    line_index: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Phase1:
    """Phase 1 for one key. Pure; never raises for bad source values."""

    moment = now or utcnow()
    if stage == MarginStage.INVOICE.value:
        if line_index is None:
            raise ValueError("the invoice stage needs a line_index")
        return _extract_invoice_line(source, line_index)
    if stage in (MarginStage.ORDER_ESTIMATE.value, MarginStage.DELIVERY.value):
        return _extract_order(stage, source, moment)
    raise ValueError(f"unknown margin stage {stage!r}")


def extract_all(stage: str, source: Mapping[str, Any], *, now: Optional[datetime] = None) -> List[Phase1]:
    """Phase 1 for every key of a source (one per invoice line)."""

    return [
        extract_inputs(stage, source, line_index=index, now=now)
        for _, index in source_keys(stage, source)
    ]


def _extract_order(stage: str, order: Mapping[str, Any], now: datetime) -> Phase1:
    tenant_id = _clean(order.get("tenant_id")) or ""
    order_id = _clean(order.get("order_id")) or ""
    key = order_source_key(order_id)

    def skip(reason: str, error_type: str) -> Skip:
        return Skip(reason, error_type, tenant_id, stage, key, order_id=order_id)

    if not tenant_id or not order_id:
        return skip(SKIP_INVALID_INPUTS, "ids_missing")
    try:
        price = unit_price_micros_from_record(order)
    except ValueError:
        return skip(SKIP_INVALID_INPUTS, "price_invalid")

    if stage == MarginStage.DELIVERY.value:
        result = order.get("delivery_result")
        if not result:
            return skip(SKIP_NO_INPUTS, "no_delivery_result")
        if not isinstance(result, Mapping):
            return skip(SKIP_INVALID_INPUTS, "delivery_result_invalid")
        gallons_raw = result.get("actual_gallons")
        if price is None or gallons_raw is None:
            return skip(SKIP_NO_INPUTS, "no_price_or_gallons")
        as_of = parse_doc_datetime(result.get("delivered_at"))
        if as_of is None:
            return skip(SKIP_INVALID_INPUTS, "delivered_at_missing")
    else:
        gallons_raw = order.get("gallons_requested")
        if price is None or gallons_raw is None:
            return skip(SKIP_NO_INPUTS, "no_price_or_gallons")
        as_of = parse_doc_datetime(order.get("delivery_window_start")) or parse_doc_datetime(
            order.get("created_at")
        )
        if as_of is None:
            return skip(SKIP_INVALID_INPUTS, "as_of_missing")
        as_of = min(as_of, now)

    ugal = _gallons(gallons_raw)
    if isinstance(ugal, str):
        return skip(SKIP_INVALID_INPUTS, ugal)
    try:
        revenue = line_subtotal_cents(ugal_to_gallons(ugal), price)
    except ValueError:
        return skip(SKIP_INVALID_INPUTS, "revenue_invalid")
    return Inputs(
        tenant_id=tenant_id,
        stage=stage,
        source_key=key,
        product_code=_product(order.get("product_code")),
        gallons_ugal=ugal,
        unit_price_micros=price,
        revenue_cents=revenue,
        as_of=as_of,
        order_id=order_id,
        customer_id=_clean(order.get("customer_id")),
        account_id=_clean(order.get("account_id")),
        order=order,
    )


def _extract_invoice_line(doc: Mapping[str, Any], index: int) -> Phase1:
    stage = MarginStage.INVOICE.value
    tenant_id = _clean(doc.get("tenant_id")) or ""
    invoice_id = _clean(doc.get("invoice_id")) or ""
    order_id = _clean(doc.get("order_id"))
    key = invoice_source_key(invoice_id, index)

    def skip(error_type: str) -> Skip:
        return Skip(
            SKIP_INVALID_INPUTS,
            error_type,
            tenant_id,
            stage,
            key,
            order_id=order_id,
            invoice_id=invoice_id,
            line_index=index,
        )

    if not tenant_id or not invoice_id:
        return skip("ids_missing")
    lines = doc.get("line_items") or []
    if index < 0 or index >= len(lines) or not isinstance(lines[index], Mapping):
        return skip("line_invalid")
    line = lines[index]
    try:
        price = unit_price_micros_from_record(line)
    except ValueError:
        return skip("price_invalid")
    if price is None:
        return skip("price_missing")
    gallons_raw = line.get("quantity_gallons")
    if gallons_raw is None:
        gallons_raw = line.get("quantity")
    if gallons_raw is None:
        return skip("gallons_missing")
    ugal = _gallons(gallons_raw)
    if isinstance(ugal, str):
        return skip(ugal)
    subtotal_raw = line.get("subtotal_cents")
    if subtotal_raw is None:
        return skip("subtotal_missing")
    try:
        subtotal = doc_decimal(subtotal_raw, field="subtotal_cents")
    except MarginValueError:
        return skip("subtotal_invalid")
    if subtotal < 0 or subtotal != subtotal.to_integral_value():
        return skip("subtotal_invalid")
    as_of = parse_doc_datetime(doc.get("delivered_at")) or parse_doc_datetime(doc.get("created_at"))
    if as_of is None:
        return skip("as_of_missing")
    return Inputs(
        tenant_id=tenant_id,
        stage=stage,
        source_key=key,
        product_code=_product(line.get("product_code")),
        gallons_ugal=ugal,
        unit_price_micros=price,
        revenue_cents=int(subtotal),
        as_of=as_of,
        order_id=order_id,
        invoice_id=invoice_id,
        line_index=index,
        line_id=_clean(line.get("line_id")),
        customer_id=_clean(doc.get("customer_id")),
        account_id=_clean(doc.get("account_id")),
        source_final=str(doc.get("status") or _INVOICE_DRAFT) != _INVOICE_DRAFT,
    )


# ---------------------------------------------------------------------------
# Phase 2 helpers: flags, floor, hash
# ---------------------------------------------------------------------------


def compute_flags(candidate: MarginCandidate, floor_micros: int) -> Dict[str, bool]:
    """FR4 flags; a pure function of the candidate (AC-15, FR4.5).

    ``below_floor`` is ``0 <= margin per gallon < floor``; ``negative_margin``
    is ``< 0``; ``missing_cost`` is ``method == none``.
    """

    per_gallon = candidate.margin_per_gallon_micros
    missing = candidate.method == CostMethod.NONE.value
    return {
        "flag_missing_cost": missing,
        "flag_negative_margin": per_gallon is not None and per_gallon < 0,
        "flag_below_floor": per_gallon is not None and 0 <= per_gallon < floor_micros,
        "flag_terminal_unattributed": candidate.terminal_id is None,
    }


def floor_for(settings: Mapping[str, Any], product_code: str) -> int:
    """Per-product floor, else the tenant floor."""

    floors = settings.get("product_floors") or {}
    if product_code in floors and floors[product_code] is not None:
        return int(floors[product_code])
    value = settings.get("floor_micros")
    return int(value) if value is not None else DEFAULT_FLOOR_MICROS


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def input_hash(candidate: MarginCandidate) -> str:
    """sha256 over the canonical JSON of the record's inputs (write protocol)."""

    snapshot = candidate.cost_snapshot or {}
    lot_ids = [lot.get("id") for lot in snapshot.get("lots") or [] if isinstance(lot, Mapping)]
    payload = {
        "stage": candidate.stage,
        "source_key": candidate.source_key,
        "gallons_ugal": candidate.gallons_ugal,
        "unit_price_micros": candidate.unit_price_micros,
        "revenue_cents": candidate.revenue_cents,
        "as_of": _iso_utc(candidate.as_of),
        "customer_id": candidate.customer_id,
        "product_code": candidate.product_code,
        "terminal_id": candidate.terminal_id,
        "method": candidate.method,
        "product_cost_micros": candidate.product_cost_micros,
        "adders_micros": candidate.adders_micros,
        "override_entry_id": snapshot.get("override_entry_id"),
        "lot_ids": lot_ids,
        "rack_price_id": snapshot.get("rack_price_id"),
        "floor_micros_used": candidate.floor_micros_used,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _zone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_midnight_utc(day: date, zone: ZoneInfo) -> datetime:
    """``day 00:00`` in ``zone``, as an aware UTC instant."""

    return datetime.combine(day, time(0), tzinfo=zone).astimezone(timezone.utc)


@dataclass
class _Progress:
    """How far phase 2 got, for the ``computation_error`` candidate."""

    terminal_id: Optional[str] = None
    floor_micros: Optional[int] = None
    settings_unavailable: bool = False


@dataclass(frozen=True)
class ItemResult:
    """Outcome of one key: ``written``, ``skipped``, ``conflict``, ``error``,
    ``no_inputs``, ``invalid_inputs`` or ``voided``."""

    outcome: str
    record: Optional[Dict[str, Any]] = None
    error_type: Optional[str] = None


# ---------------------------------------------------------------------------
# Summary aggregation (shared with the weekly report)
# ---------------------------------------------------------------------------


def apply_stage_preference(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """D12: invoice records count; a delivery only without an invoice record
    for its order; an estimate only with neither."""

    invoiced = {r["order_id"] for r in rows if r["stage"] == MarginStage.INVOICE.value and r.get("order_id")}
    delivered = {r["order_id"] for r in rows if r["stage"] == MarginStage.DELIVERY.value and r.get("order_id")}
    kept: List[Mapping[str, Any]] = []
    for row in rows:
        order_id = row.get("order_id")
        if row["stage"] == MarginStage.INVOICE.value:
            kept.append(row)
        elif row["stage"] == MarginStage.DELIVERY.value:
            if not order_id or order_id not in invoiced:
                kept.append(row)
        elif not order_id or (order_id not in invoiced and order_id not in delivered):
            kept.append(row)
    return kept


_ROW_FLAGS = (
    (MarginFlag.MISSING_COST.value, "flag_missing_cost"),
    (MarginFlag.NEGATIVE_MARGIN.value, "flag_negative_margin"),
    (MarginFlag.BELOW_FLOOR.value, "flag_below_floor"),
    (MarginFlag.TERMINAL_UNATTRIBUTED.value, "flag_terminal_unattributed"),
)


def missing_cost_share_bp(missing_records: int, records_total: int) -> Optional[int]:
    """``missing × 10000 / total`` half-up; ``None`` when there are no records."""

    if records_total <= 0:
        return None
    return quantize_half_up(
        Decimal(missing_records * 10_000) / Decimal(records_total)  # margin: decimal-division
    )


def aggregate_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Integer totals over counted records (design "Summary").

    ``cost_cents`` / ``margin_cents`` cover costed records only and pair with
    ``revenue_cents_with_cost``; the two revenue parts always sum to
    ``revenue_cents``.
    """

    totals: Dict[str, Any] = {
        "records": 0,
        "gallons_ugal": 0,
        "revenue_cents": 0,
        "revenue_cents_with_cost": 0,
        "revenue_cents_missing_cost": 0,
        "cost_cents": 0,
        "margin_cents": 0,
        "flag_counts": {name: {"records": 0, "gallons_ugal": 0} for name, _ in _ROW_FLAGS},
    }
    for row in rows:
        totals["records"] += 1
        totals["gallons_ugal"] += int(row["gallons_ugal"])
        revenue = int(row["revenue_cents"])
        totals["revenue_cents"] += revenue
        if row["method"] == CostMethod.NONE.value:
            totals["revenue_cents_missing_cost"] += revenue
        else:
            totals["revenue_cents_with_cost"] += revenue
            totals["cost_cents"] += int(row["cost_cents"])
            totals["margin_cents"] += int(row["margin_cents"])
        for name, column in _ROW_FLAGS:
            if row.get(column):
                totals["flag_counts"][name]["records"] += 1
                totals["flag_counts"][name]["gallons_ugal"] += int(row["gallons_ugal"])
    bp = (
        margin_bp(totals["margin_cents"], totals["revenue_cents_with_cost"])
        if totals["revenue_cents_with_cost"]
        else None
    )
    totals["margin_bp"] = bp
    totals["margin_pct"] = margin_pct(bp)
    totals["missing_cost_share_bp"] = missing_cost_share_bp(
        totals["flag_counts"][MarginFlag.MISSING_COST.value]["records"], totals["records"]
    )
    return totals


# ---------------------------------------------------------------------------
# Audit helper (same shape as the cost-entry service)
# ---------------------------------------------------------------------------


def _audit(
    telemetry: Any,
    *,
    event_type: str,
    actor: str,
    resource_type: str,
    resource_id: str,
    action: str,
    details: Dict[str, Any],
    tenant_id: str,
) -> None:
    if telemetry is None:
        from telemetry.service import get_telemetry_service

        telemetry = get_telemetry_service()
    if telemetry is not None:
        try:
            telemetry.log_audit_event(
                event_type=event_type,
                user_id=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - audit must never fail the operation
            logger.warning(
                "margin audit sink failed for %s %s: %s", event_type, resource_id, type(exc).__name__
            )
    logger.info(
        "margin %s tenant=%s %s=%s by=%s", event_type, tenant_id, resource_type, resource_id, actor
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MarginService:
    """Margin computation, the hook scheduler, preview, summary and recompute start."""

    def __init__(
        self,
        repository: MarginRepository,
        *,
        es_service: Any,
        readers: Optional[CostBasisReaders] = None,
        attribution: Optional[MarginAttribution] = None,
        orders: Any = None,
        terminals: Any = None,
        invoice_service: Any = None,
        signal_bus: Any = None,
        signal_bus_provider: Optional[Callable[[], Any]] = None,
        pricing_engine_factory: Optional[Callable[[str], Any]] = None,
        telemetry: Any = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repo = repository
        self._es = es_service
        self._readers = readers or CostBasisReaders.from_store(es_service, repository)
        self._attribution = attribution or MarginAttribution(es_service)
        if orders is None:
            from fuel.order_repository import FuelOrderRepository

            orders = FuelOrderRepository(es_service)
        self._orders = orders
        self._terminals = terminals
        self._invoice_service = invoice_service
        self._bus = signal_bus
        self._bus_provider = signal_bus_provider
        self._no_bus_logged = False
        self._pricing_engine_factory = pricing_engine_factory
        self._telemetry = telemetry
        self._clock = clock
        self._activated: Dict[str, datetime] = {}
        self._tasks: Set[asyncio.Task] = set()
        self._runs: Set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(MARGIN_HOOK_CONCURRENCY)
        self.hook = MarginHook(self)

    # -- wiring -------------------------------------------------------------

    @property
    def repository(self) -> MarginRepository:
        return self._repo

    @property
    def orders(self) -> Any:
        return self._orders

    @property
    def invoice_service(self) -> Any:
        return self._invoice_service

    @property
    def telemetry(self) -> Any:
        return self._telemetry

    def now(self) -> datetime:
        return self._clock()

    def set_invoice_service(self, invoice_service: Any) -> None:
        self._invoice_service = invoice_service

    def set_signal_bus(self, bus: Any) -> None:
        self._bus = bus

    @property
    def pending_tasks(self) -> int:
        return len(self._tasks)

    # -- scheduling (hook side) -------------------------------------------

    def schedule(self, stage: str, source: Mapping[str, Any], mode: str) -> bool:
        """Schedule ``_run`` as a tracked task; ``False`` under backpressure."""

        if len(self._tasks) >= MARGIN_HOOK_MAX_PENDING_TASKS:
            logger.warning(
                "margin hook backpressure: %d tasks pending, nothing scheduled "
                "tenant=%s stage=%s order_id=%s invoice_id=%s",
                len(self._tasks),
                source.get("tenant_id"),
                stage,
                source.get("order_id"),
                source.get("invoice_id"),
            )
            return False
        task = asyncio.get_running_loop().create_task(self._guarded_run(stage, source, mode))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _guarded_run(self, stage: str, source: Mapping[str, Any], mode: str) -> None:
        async with self._semaphore:
            try:
                await self._run(stage, source, mode)
            except Exception as exc:  # noqa: BLE001 - a margin task never escapes
                logger.error(
                    "margin task failed tenant=%s stage=%s order_id=%s invoice_id=%s error_type=%s",
                    source.get("tenant_id"),
                    stage,
                    source.get("order_id"),
                    source.get("invoice_id"),
                    type(exc).__name__,
                )

    async def drain(self, timeout: float = DRAIN_TIMEOUT_SECONDS) -> int:
        """Await pending margin tasks; cancel what is left after ``timeout``."""

        pending = set(self._tasks) | set(self._runs)
        if not pending:
            return 0
        _, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            logger.warning("margin drain: cancelled %d unfinished task(s)", len(still))
            await asyncio.gather(*still, return_exceptions=True)
        return len(still)

    # -- execution ----------------------------------------------------------

    async def ensure_activated_once(self, tenant_id: str) -> Optional[datetime]:
        """Live path: set the activation watermark once per process (memoized)."""

        cached = self._activated.get(tenant_id)
        if cached is not None:
            return cached
        try:
            value = await self._repo.ensure_activated(tenant_id)
        except Exception as exc:  # noqa: BLE001 - the write still proceeds
            logger.error(
                "margin ensure_activated failed tenant=%s error_type=%s", tenant_id, type(exc).__name__
            )
            return None
        if value is not None:
            self._activated[tenant_id] = value
        return value

    async def _run(self, stage: str, source: Mapping[str, Any], mode: str) -> List[ItemResult]:
        """One hook event: phase 1, then phase 2 and the write, per key."""

        tenant_id = _clean(source.get("tenant_id"))
        if tenant_id is None:
            logger.warning("margin hook source without tenant_id stage=%s", stage)
            return []
        await self.ensure_activated_once(tenant_id)
        now = self._clock()
        if mode == WriteMode.VOID.value:
            return [
                await self.void_key(
                    tenant_id,
                    stage,
                    key,
                    lambda index=index: extract_inputs(stage, source, line_index=index, now=now),
                )
                for key, index in source_keys(stage, source)
            ]
        results = []
        for item in extract_all(stage, source, now=now):
            results.append(await self.process_item(item, mode))
        return results

    async def process_item(
        self,
        item: Phase1,
        mode: str,
        *,
        cache: Optional[ReaderCache] = None,
        background: bool = False,
        recompute_run_id: Optional[str] = None,
    ) -> ItemResult:
        """Phase 2 and the versioned write for one key.

        ``background`` (gap sweep, recompute) logs no per-key ERROR for
        invalid inputs; the caller logs one WARNING with the cycle's count.
        """

        if isinstance(item, Skip):
            return await self._handle_skip(item, background=background)
        candidate = await self.candidate(item, cache)
        if recompute_run_id is not None:
            candidate = replace(candidate, recompute_run_id=recompute_run_id)
        return await self._write(item.tenant_id, candidate, mode)

    async def void_key(
        self,
        tenant_id: str,
        stage: str,
        source_key: str,
        make_inputs: Callable[[], Phase1],
        *,
        cache: Optional[ReaderCache] = None,
    ) -> ItemResult:
        """N8: void the latest row without a resolver read; tombstone only if none."""

        try:
            result = await self._repo.void_latest(tenant_id, stage, source_key)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "margin void failed tenant=%s stage=%s source_key=%s error_type=%s",
                tenant_id,
                stage,
                source_key,
                type(exc).__name__,
            )
            return ItemResult("error", error_type=type(exc).__name__)
        if result.outcome != "missing":
            logger.info(
                "margin void tenant=%s stage=%s source_key=%s outcome=%s",
                tenant_id,
                stage,
                source_key,
                result.outcome,
            )
            return ItemResult("voided" if result.written else "skipped", result.record)
        item = make_inputs()
        if isinstance(item, Skip):
            # Nothing to tombstone; a later replay is covered by the sweep.
            logger.debug(
                "margin void with no record and no inputs tenant=%s source_key=%s reason=%s",
                tenant_id,
                source_key,
                item.error_type,
            )
            return ItemResult(item.reason, error_type=item.error_type)
        candidate = await self.candidate(item, cache)
        return await self._write(tenant_id, candidate, WriteMode.VOID.value)

    async def _handle_skip(self, item: Skip, *, background: bool) -> ItemResult:
        margin_skips[item.reason] += 1
        if item.reason == SKIP_NO_INPUTS:
            logger.debug(
                "margin skip no_inputs tenant=%s stage=%s source_key=%s",
                item.tenant_id,
                item.stage,
                item.source_key,
            )
            return ItemResult(SKIP_NO_INPUTS, error_type=item.error_type)
        if not background:
            logger.error(
                "margin skip tenant=%s stage=%s source_key=%s error_type=%s reason=%s",
                item.tenant_id,
                item.stage,
                item.source_key,
                item.error_type,
                SKIP_INVALID_INPUTS,
            )
        if item.tenant_id:
            try:
                await self._repo.record_skip(
                    item.tenant_id,
                    stage=item.stage,
                    source_key=item.source_key,
                    error_type=item.error_type,
                    order_id=item.order_id,
                    invoice_id=item.invoice_id,
                    line_index=item.line_index,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "margin record_skip failed tenant=%s source_key=%s error_type=%s",
                    item.tenant_id,
                    item.source_key,
                    type(exc).__name__,
                )
        return ItemResult(SKIP_INVALID_INPUTS, error_type=item.error_type)

    async def _write(self, tenant_id: str, candidate: MarginCandidate, mode: str) -> ItemResult:
        try:
            result: WriteResult = await self._repo.write_record(tenant_id, candidate, mode)
        except Exception as exc:  # noqa: BLE001 - the sweep repairs it
            logger.error(
                "margin write failed tenant=%s stage=%s source_key=%s error_type=%s",
                tenant_id,
                candidate.stage,
                candidate.source_key,
                type(exc).__name__,
            )
            return ItemResult("error", error_type=type(exc).__name__)
        record = result.record or {}
        logger.info(
            "margin record tenant=%s stage=%s source_key=%s record_id=%s mode=%s outcome=%s",
            tenant_id,
            candidate.stage,
            candidate.source_key,
            record.get("record_id"),
            mode,
            "written" if result.written else result.outcome,
        )
        logger.debug(
            "margin values source_key=%s method=%s revenue_cents=%s cost_cents=%s margin_cents=%s",
            candidate.source_key,
            candidate.method,
            candidate.revenue_cents,
            candidate.cost_cents,
            candidate.margin_cents,
        )
        if result.written and record.get("alert_state") == "pending":
            await self._publish_record_signal(tenant_id, record)
        if result.written:
            return ItemResult("written", result.record)
        return ItemResult(result.outcome, result.record)

    # -- phase 2 ------------------------------------------------------------

    async def candidate(self, inputs: Inputs, cache: Optional[ReaderCache] = None) -> MarginCandidate:
        """:meth:`compute`, or the ``computation_error`` candidate if it raises."""

        progress = _Progress()
        try:
            return await self.compute(inputs, cache, _progress=progress)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "margin computation_error tenant=%s stage=%s source_key=%s error_type=%s",
                inputs.tenant_id,
                inputs.stage,
                inputs.source_key,
                type(exc).__name__,
            )
            return self._error_candidate(inputs, progress, exc)

    async def compute(
        self,
        inputs: Inputs,
        cache: Optional[ReaderCache] = None,
        *,
        _progress: Optional[_Progress] = None,
    ) -> MarginCandidate:
        """Phase 2. Raises on reader, settings or resolver failure."""

        progress = _progress if _progress is not None else _Progress()
        terminal_id = await self._terminal_for(inputs)
        progress.terminal_id = terminal_id
        try:
            settings = await self._repo.get_settings(inputs.tenant_id)
        except Exception:
            progress.settings_unavailable = True
            raise
        floor = floor_for(settings, inputs.product_code)
        progress.floor_micros = floor
        resolver = CostBasisResolver(self._readers, settings, cache=cache)
        basis = await resolver.resolve(inputs.tenant_id, inputs.product_code, terminal_id, inputs.as_of)
        return self._build_candidate(inputs, terminal_id, basis, settings, floor)

    def _build_candidate(
        self,
        inputs: Inputs,
        terminal_id: Optional[str],
        basis: CostBasis,
        settings: Mapping[str, Any],
        floor: int,
    ) -> MarginCandidate:
        amounts = compute_margin_amounts(
            gallons_ugal=inputs.gallons_ugal,
            unit_price_micros=inputs.unit_price_micros,
            revenue_cents=inputs.revenue_cents,
            landed_cost_micros=basis.landed_cost_micros,
        )
        snapshot = basis.to_snapshot()
        snapshot["floor_micros_used"] = floor
        snapshot["settings"] = {
            "wac_window_days": settings.get("wac_window_days"),
            "rack_staleness_days": settings.get("rack_staleness_days"),
            "floor_micros": settings.get("floor_micros"),
            "timezone": settings.get("timezone"),
        }
        candidate = MarginCandidate(
            stage=inputs.stage,
            source_key=inputs.source_key,
            product_code=inputs.product_code,
            gallons_ugal=inputs.gallons_ugal,
            unit_price_micros=inputs.unit_price_micros,
            revenue_cents=inputs.revenue_cents,
            method=basis.method,
            floor_micros_used=floor,
            cost_snapshot=snapshot,
            as_of=inputs.as_of,
            input_hash="",
            order_id=inputs.order_id,
            invoice_id=inputs.invoice_id,
            line_index=inputs.line_index,
            line_id=inputs.line_id,
            customer_id=inputs.customer_id,
            account_id=inputs.account_id,
            terminal_id=terminal_id,
            product_cost_micros=basis.product_cost_micros,
            adders_micros=basis.adders_micros,
            landed_cost_micros=basis.landed_cost_micros,
            cost_cents=amounts.cost_cents,
            margin_cents=amounts.margin_cents,
            margin_per_gallon_micros=amounts.margin_per_gallon_micros,
            margin_bp=amounts.margin_bp,
            no_cost_reason=basis.no_cost_reason,
            source_final=inputs.source_final,
        )
        candidate = replace(candidate, **compute_flags(candidate, floor))
        return replace(candidate, input_hash=input_hash(candidate))

    def _error_candidate(self, inputs: Inputs, progress: _Progress, exc: BaseException) -> MarginCandidate:
        floor = (
            DEFAULT_FLOOR_MICROS
            if progress.settings_unavailable or progress.floor_micros is None
            else progress.floor_micros
        )
        snapshot = {
            "method": CostMethod.NONE.value,
            "no_cost_reason": NoCostReason.COMPUTATION_ERROR.value,
            "error_type": type(exc).__name__,
            "settings_unavailable": progress.settings_unavailable,
            "terminal_id": progress.terminal_id,
            "terminal_unattributed": progress.terminal_id is None,
            "product_code": inputs.product_code,
            "as_of": _iso_utc(inputs.as_of),
            "floor_micros_used": floor,
            "lots": [],
        }
        candidate = MarginCandidate(
            stage=inputs.stage,
            source_key=inputs.source_key,
            product_code=inputs.product_code,
            gallons_ugal=inputs.gallons_ugal,
            unit_price_micros=inputs.unit_price_micros,
            revenue_cents=inputs.revenue_cents,
            method=CostMethod.NONE.value,
            floor_micros_used=floor,
            cost_snapshot=snapshot,
            as_of=inputs.as_of,
            input_hash="",
            order_id=inputs.order_id,
            invoice_id=inputs.invoice_id,
            line_index=inputs.line_index,
            line_id=inputs.line_id,
            customer_id=inputs.customer_id,
            account_id=inputs.account_id,
            terminal_id=progress.terminal_id,
            no_cost_reason=NoCostReason.COMPUTATION_ERROR.value,
            source_final=inputs.source_final,
        )
        candidate = replace(candidate, **compute_flags(candidate, floor))
        return replace(candidate, input_hash=input_hash(candidate))

    async def _terminal_for(self, inputs: Inputs) -> Optional[str]:
        """Attribution for orders; for invoices, via the order (missing -> None)."""

        order: Optional[Mapping[str, Any]] = inputs.order
        if order is None:
            if not inputs.order_id:
                return None
            fetched = await self._orders.get(inputs.tenant_id, inputs.order_id)
            if fetched is None:
                return None
            order = fetched.model_dump(mode="json") if hasattr(fetched, "model_dump") else dict(fetched)
        terminal_id, _reason = await self._attribution.terminal_for_order(inputs.tenant_id, order)
        return terminal_id

    # -- signals ------------------------------------------------------------

    def _signal_bus(self) -> Any:
        if self._bus is None and self._bus_provider is not None:
            try:
                self._bus = self._bus_provider()
            except Exception:  # noqa: BLE001
                self._bus = None
        return self._bus

    async def _publish(self, signal: Any) -> None:
        bus = self._signal_bus()
        if bus is None:
            if not self._no_bus_logged:
                logger.info("margin signals: no signal bus wired; alerts still come from alert_state")
                self._no_bus_logged = True
            return
        try:
            await bus.publish(signal)
        except Exception as exc:  # noqa: BLE001 - the bus signal is a hint only
            logger.warning("margin signal publish failed: %s", type(exc).__name__)

    async def _publish_record_signal(self, tenant_id: str, record: Mapping[str, Any]) -> None:
        """FR5.2: one ids-only RiskSignal for a row written with ``alert_state='pending'``."""

        from Agents.overlay.data_contracts import RiskSignal, Severity

        flags = [name for name, column in _ROW_FLAGS if record.get(column)]
        if record.get("flag_negative_margin"):
            severity = Severity.HIGH
        elif record.get("flag_missing_cost"):
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW
        try:
            signal = RiskSignal(
                source_agent=SIGNAL_SOURCE_AGENT,
                entity_id=record["record_id"],
                entity_type=record["stage"],
                severity=severity,
                confidence=1.0,
                ttl_seconds=SIGNAL_TTL_SECONDS,
                tenant_id=tenant_id,
                context={
                    "flags": flags,
                    "stage": record["stage"],
                    "record_id": record["record_id"],
                    "customer_id": record.get("customer_id"),
                    "product_code": record.get("product_code"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("margin signal build failed: %s", type(exc).__name__)
            return
        await self._publish(signal)

    async def publish_digest_hint(self, tenant_id: str, run_id: str, counts_by_flag: Mapping[str, int]) -> None:
        """One ids-only hint for a recompute run with a pending digest."""

        from Agents.overlay.data_contracts import RiskSignal, Severity

        signal = RiskSignal(
            source_agent=SIGNAL_SOURCE_AGENT,
            entity_id=run_id,
            entity_type=RECOMPUTE_SIGNAL_ENTITY,
            severity=Severity.LOW,
            confidence=1.0,
            ttl_seconds=SIGNAL_TTL_SECONDS,
            tenant_id=tenant_id,
            context={"run_id": run_id, "counts_by_flag": dict(counts_by_flag)},
        )
        await self._publish(signal)

    # -- preview ------------------------------------------------------------

    async def preview(self, tenant_id: str, request: MarginPreviewRequest) -> Dict[str, Any]:
        """FR6.2: a quote-stage candidate. Persists nothing; no signal or alert."""

        errors: List[Dict[str, Any]] = []
        gallons_milli: Optional[int] = None
        price_micros: Optional[int] = None
        try:
            gallons_milli = parse_gallons_milli(request.gallons, field="gallons")
        except MarginValueError as exc:
            errors.append(exc.as_error())
        if request.unit_price_usd is not None:
            try:
                price_micros = parse_usd_micros(
                    request.unit_price_usd, field="unit_price_usd", max_micros=MAX_UNIT_COST_MICROS
                )
            except MarginValueError as exc:
                errors.append(exc.as_error())
        terminal_id = _clean(request.terminal_id)
        if terminal_id is not None and self._terminals is not None:
            if await self._terminals.get(tenant_id, terminal_id) is None:
                errors.append(
                    {"loc": ["terminal_id"], "msg": "unknown terminal", "type": "unknown_terminal"}
                )
        if errors:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "Margin preview request is invalid",
                status_code=422,
                details={"errors": errors},
            )
        assert gallons_milli is not None
        gallons = Decimal(gallons_milli).scaleb(-3)
        gallons_ugal = gallons_milli * 1_000
        as_of = (request.as_of or self._clock()).astimezone(timezone.utc)
        product = _product(request.product_code)
        warnings: List[str] = []
        price_source = "request"
        if price_micros is None:
            price_source = "pricing_engine"
            price_micros, split = await self._resolve_sell_price(
                tenant_id, request, product, gallons, terminal_id, as_of
            )
            if split:
                warnings.append(WARNING_CONTRACT_SPLIT)
        revenue = line_subtotal_cents(gallons, price_micros)
        settings = await self._repo.get_settings(tenant_id)
        floor = floor_for(settings, product)
        basis = await CostBasisResolver(self._readers, settings).resolve(
            tenant_id, product, terminal_id, as_of
        )
        inputs = Inputs(
            tenant_id=tenant_id,
            stage="quote",
            source_key="preview",
            product_code=product,
            gallons_ugal=gallons_ugal,
            unit_price_micros=price_micros,
            revenue_cents=revenue,
            as_of=as_of,
            customer_id=_clean(request.customer_id),
            account_id=_clean(request.account_id),
        )
        candidate = self._build_candidate(inputs, terminal_id, basis, settings, floor)
        return {
            "stage": "quote",
            "product_code": candidate.product_code,
            "terminal_id": candidate.terminal_id,
            "customer_id": candidate.customer_id,
            "account_id": candidate.account_id,
            "as_of": _iso_utc(as_of),
            "gallons_ugal": candidate.gallons_ugal,
            "unit_price_micros": candidate.unit_price_micros,
            "price_source": price_source,
            "revenue_cents": candidate.revenue_cents,
            "method": candidate.method,
            "product_cost_micros": candidate.product_cost_micros,
            "adders_micros": candidate.adders_micros,
            "landed_cost_micros": candidate.landed_cost_micros,
            "cost_cents": candidate.cost_cents,
            "margin_cents": candidate.margin_cents,
            "margin_per_gallon_micros": candidate.margin_per_gallon_micros,
            "margin_bp": candidate.margin_bp,
            "margin_pct": margin_pct(candidate.margin_bp),
            "no_cost_reason": candidate.no_cost_reason,
            "flags": [name for name, column in _ROW_FLAGS if getattr(candidate, column)],
            "floor_micros_used": candidate.floor_micros_used,
            "cost_snapshot": dict(candidate.cost_snapshot),
            "warnings": warnings,
        }

    async def _resolve_sell_price(
        self,
        tenant_id: str,
        request: MarginPreviewRequest,
        product: str,
        gallons: Decimal,
        terminal_id: Optional[str],
        as_of: datetime,
    ) -> Tuple[int, bool]:
        from commerce.services.sales_pricing_engine import (
            PricingNoRuleMatchedError,
            PricingRackPriceUnavailableError,
            build_sales_pricing_engine,
        )

        factory = self._pricing_engine_factory or (lambda t: build_sales_pricing_engine(self._es, t))
        engine = factory(tenant_id)
        try:
            resolution = await engine.resolve_price(
                customer_id=request.customer_id,
                product_code=product,
                gallons=float(gallons),  # margin: float-ok (tier selection only)
                terminal_id=terminal_id or "",
                route_miles=0.0,
                effective_date=as_of.date(),
                market_price_cents=None,
                account_id=request.account_id,
            )
        except PricingNoRuleMatchedError as exc:
            raise AppException(ErrorCode.PRICING_NO_RULE_MATCHED, str(exc), status_code=422)
        except PricingRackPriceUnavailableError as exc:
            raise AppException(
                ErrorCode.PRICING_RACK_PRICE_UNAVAILABLE,
                str(exc),
                status_code=422,
                details={"terminal_id": exc.terminal_id, "product_code": exc.product_code},
            )
        except NotImplementedError as exc:
            raise AppException(ErrorCode.PRICING_NOT_IMPLEMENTED, str(exc), status_code=422)
        effective = getattr(resolution, "effective_price_micros", None)
        if effective is None:
            effective = int(resolution.effective_price_cents) * MICROS_PER_CENT
        market_gallons = getattr(resolution, "split_gallons_at_market_price", None)
        split = market_gallons is not None and market_gallons > 0
        return int(effective), split

    # -- cost basis (admin diagnostic) ----------------------------------------

    async def cost_basis(
        self,
        tenant_id: str,
        *,
        product_code: str,
        terminal_id: Optional[str] = None,
        as_of: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """``GET /cost-basis``: the resolver output for one product, fresh reads.

        422 on an unknown product or terminal; a reader failure is 503
        ``ELASTICSEARCH_UNAVAILABLE`` and logged at ERROR (design FR2).
        """

        errors: List[Dict[str, Any]] = []
        product: Optional[str] = None
        try:
            product = canonicalize(str(product_code).strip())
        except (UnknownFuelProductError, TypeError):
            errors.append({"loc": ["product_code"], "msg": "unknown product", "type": "unknown_product"})
        terminal = _clean(terminal_id)
        if terminal is not None and self._terminals is not None:
            if await self._terminals.get(tenant_id, terminal) is None:
                errors.append({"loc": ["terminal_id"], "msg": "unknown terminal", "type": "unknown_terminal"})
        if errors:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "Cost basis request is invalid",
                status_code=422,
                details={"errors": errors},
            )
        assert product is not None
        instant = (as_of or self._clock()).astimezone(timezone.utc)
        try:
            settings = await self._repo.get_settings(tenant_id)
            basis = await CostBasisResolver(self._readers, settings).resolve(
                tenant_id, product, terminal, instant
            )
        except Exception as exc:
            logger.error(
                "margin cost-basis read failed tenant=%s product=%s terminal=%s: %s",
                tenant_id,
                product,
                terminal or "*",
                type(exc).__name__,
            )
            raise AppException(
                ErrorCode.ELASTICSEARCH_UNAVAILABLE,
                "The cost basis could not be read. Try again.",
                status_code=503,
            ) from None
        return basis.to_snapshot()

    # -- summary ------------------------------------------------------------

    async def summary(
        self,
        tenant_id: str,
        *,
        start_date: date,
        end_date: date,
        group_by: str = "day",
    ) -> Dict[str, Any]:
        """``GET /summary``: stage-preferred totals over an ``as_of`` date range."""

        errors: List[Dict[str, Any]] = []
        if group_by not in SUMMARY_GROUPS:
            errors.append({"loc": ["group_by"], "msg": "unknown group_by", "type": "invalid_choice"})
        if end_date < start_date:
            errors.append({"loc": ["end_date"], "msg": "end_date is before start_date", "type": "invalid_range"})
        elif (end_date - start_date).days + 1 > SUMMARY_MAX_SPAN_DAYS:
            errors.append(
                {
                    "loc": ["end_date"],
                    "msg": f"the range may span at most {SUMMARY_MAX_SPAN_DAYS} days",
                    "type": "range_too_long",
                }
            )
        if errors:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "Margin summary request is invalid",
                status_code=422,
                details={"errors": errors},
            )
        settings = await self._repo.get_settings(tenant_id)
        zone = _zone(settings.get("timezone"))
        rows = await self.counted_rows(
            tenant_id,
            as_of_from=local_midnight_utc(start_date, zone),
            as_of_to=local_midnight_utc(end_date + timedelta(days=1), zone),
        )
        grouped: Dict[str, List[Mapping[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(self._group_key(row, group_by, zone), []).append(row)
        groups = [{"key": key, **aggregate_rows(items)} for key, items in sorted(grouped.items())]
        return {
            "group_by": group_by,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": str(zone.key),
            "groups": groups,
            "totals": aggregate_rows(rows),
            "skipped_sources": await self._repo.skipped_sources(tenant_id),
        }

    async def counted_rows(
        self, tenant_id: str, *, as_of_from: datetime, as_of_to: datetime
    ) -> List[Mapping[str, Any]]:
        """Active rows in ``[as_of_from, as_of_to)`` after the stage preference."""

        rows: List[Mapping[str, Any]] = []
        async for page in self._repo.iter_summary_rows(
            tenant_id, as_of_from=as_of_from, as_of_to=as_of_to
        ):
            rows.extend(page)
        return apply_stage_preference(rows)

    @staticmethod
    def _group_key(row: Mapping[str, Any], group_by: str, zone: ZoneInfo) -> str:
        if group_by == "day":
            return row["as_of"].astimezone(zone).date().isoformat()
        column = {"customer": "customer_id", "product": "product_code", "terminal": "terminal_id"}[group_by]
        return str(row.get(column) or "")

    # -- recompute start ----------------------------------------------------

    async def start_recompute(
        self, tenant_id: str, actor: str, request: MarginRecomputeRequest
    ) -> Dict[str, Any]:
        """Insert a ``running`` run and start the background task (202 ``{run_id}``)."""

        stages = list(dict.fromkeys(request.stages))
        try:
            run = await self._repo.start_run(
                tenant_id,
                requested_by=actor,
                start_date=request.start_date,
                end_date=request.end_date,
                stages=stages,
                only_missing=request.only_missing,
                reason=request.reason,
            )
        except MarginRecomputeRunningError as exc:
            raise AppException(
                ErrorCode.MARGIN_RECOMPUTE_RUNNING,
                "A margin recompute is already running for this tenant",
                status_code=409,
                details={"run_id": exc.run_id},
            ) from None
        run_id = run["run_id"]
        _audit(
            self._telemetry,
            event_type="margin_recompute_started",
            actor=actor,
            resource_type="margin_recompute_run",
            resource_id=run_id,
            action="start",
            details={
                "tenant_id": tenant_id,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "stages": stages,
                "only_missing": request.only_missing,
            },
            tenant_id=tenant_id,
        )
        from commerce.services.margin_jobs import run_margin_recompute

        task = asyncio.get_running_loop().create_task(
            run_margin_recompute(
                self,
                tenant_id,
                run_id,
                start_date=request.start_date,
                end_date=request.end_date,
                stages=stages,
                only_missing=request.only_missing,
                actor=actor,
            )
        )
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)
        return {"run_id": run_id}

    # -- alert resolution ---------------------------------------------------

    async def transition_alert(
        self,
        tenant_id: str,
        actor: str,
        alert_id: str,
        action: str,
        *,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Acknowledge / approve / dismiss an alert and audit it (``margin_alert_resolved``).

        Approval only records the decision; nothing executes (FR5.9). The
        audit carries ids and statuses, never the note or any amount.
        """

        try:
            transition = await self._repo.transition_alert(
                tenant_id, alert_id, action=action, actor=actor, note=note, now=self._clock()
            )
        except MarginAlertNotFoundError:
            raise AppException(
                ErrorCode.RESOURCE_NOT_FOUND, "Margin alert not found", status_code=404
            ) from None
        except MarginAlertStateError:
            raise AppException(
                ErrorCode.CONFLICT,
                f"The margin alert does not allow '{action}' in its current state",
                status_code=409,
            ) from None
        alert = transition.alert
        details: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "alert_type": alert["alert_type"],
            "from_status": transition.from_status,
            "to_status": alert["status"],
        }
        if alert.get("proposal_id"):
            details["proposal_id"] = alert["proposal_id"]
        _audit(
            self._telemetry,
            event_type="margin_alert_resolved",
            actor=actor,
            resource_type="margin_alert",
            resource_id=alert_id,
            action=action,
            details=details,
            tenant_id=tenant_id,
        )
        return alert

    def audit(self, **kwargs: Any) -> None:
        _audit(self._telemetry, **kwargs)


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

_ORDER_EVENTS: Dict[str, Tuple[str, str]] = {
    "order.dispatched": (MarginStage.ORDER_ESTIMATE.value, WriteMode.LIVE.value),
    "order.delivered": (MarginStage.DELIVERY.value, WriteMode.LIVE.value),
    "order.cancelled": (MarginStage.ORDER_ESTIMATE.value, WriteMode.VOID.value),
    "order.failed": (MarginStage.ORDER_ESTIMATE.value, WriteMode.VOID.value),
}
ORDER_EVENTS: Tuple[str, ...] = tuple(_ORDER_EVENTS)


def invoice_mode(doc: Mapping[str, Any]) -> str:
    """Write mode for an invoice doc: void, live (draft) or finalize."""

    status = str(doc.get("status") or _INVOICE_DRAFT)
    if status == _INVOICE_VOID:
        return WriteMode.VOID.value
    if status == _INVOICE_DRAFT:
        return WriteMode.LIVE.value
    return WriteMode.FINALIZE.value


def margin_feed_active() -> bool:
    """The flag and the persistence layer (AC-28)."""

    from config.settings import get_settings
    from persistence.database import is_persistence_enabled

    return bool(getattr(get_settings(), "commerce_margin_feed_enabled", False)) and is_persistence_enabled()


class MarginHook:
    """Synchronous entry points for InvoiceService and the order subscriber.

    Each method returns immediately: flag/persistence check, a deep copy
    taken now, then a tracked task. Every exception is caught and logged at
    ERROR, so the business operation never sees margin work fail.
    """

    def __init__(self, service: MarginService) -> None:
        self._service = service

    def invoice_generated(self, doc: Mapping[str, Any]) -> bool:
        return self._submit(MarginStage.INVOICE.value, doc, None)

    def invoice_finalized(self, doc: Mapping[str, Any]) -> bool:
        return self._submit(MarginStage.INVOICE.value, doc, WriteMode.FINALIZE.value)

    def invoice_voided(self, doc: Mapping[str, Any]) -> bool:
        return self._submit(MarginStage.INVOICE.value, doc, WriteMode.VOID.value)

    def order_event(self, order: Mapping[str, Any], event: str) -> bool:
        mapped = _ORDER_EVENTS.get(event)
        if mapped is None:
            return False
        stage, mode = mapped
        return self._submit(stage, order, mode)

    def _submit(self, stage: str, source: Mapping[str, Any], mode: Optional[str]) -> bool:
        try:
            if not margin_feed_active():
                return False
            copied = copy.deepcopy(dict(source))
            return self._service.schedule(stage, copied, mode or invoice_mode(copied))
        except Exception as exc:  # noqa: BLE001 - never fail the caller
            tenant_id = invoice_id = order_id = None
            try:
                tenant_id = source.get("tenant_id")
                invoice_id = source.get("invoice_id")
                order_id = source.get("order_id")
            except Exception:  # noqa: BLE001
                pass
            logger.error(
                "margin hook failed tenant=%s stage=%s order_id=%s invoice_id=%s error_type=%s",
                tenant_id,
                stage,
                order_id,
                invoice_id,
                type(exc).__name__,
            )
            return False


__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "Inputs",
    "ItemResult",
    "MARGIN_HOOK_CONCURRENCY",
    "MARGIN_HOOK_MAX_PENDING_TASKS",
    "MarginHook",
    "MarginService",
    "ORDER_EVENTS",
    "SIGNAL_SOURCE_AGENT",
    "SKIP_INVALID_INPUTS",
    "SKIP_NO_INPUTS",
    "SUMMARY_MAX_SPAN_DAYS",
    "Skip",
    "WARNING_CONTRACT_SPLIT",
    "aggregate_rows",
    "apply_stage_preference",
    "compute_flags",
    "extract_all",
    "extract_inputs",
    "floor_for",
    "input_hash",
    "invoice_mode",
    "invoice_source_key",
    "local_midnight_utc",
    "margin_feed_active",
    "margin_skips",
    "missing_cost_share_bp",
    "order_source_key",
    "source_keys",
]
