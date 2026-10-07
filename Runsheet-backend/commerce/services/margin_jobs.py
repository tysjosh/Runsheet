"""Margin background writers: gap sweep, recompute and the weekly report.

Design sections "Gap sweep (durability)", "Activation watermark",
"Recompute", "Weekly report" and Simplifications 10 and 13 (frozen):

* The **gap sweep** reconciles state with the same rule table as live writes,
  driven by ``updated_at`` for invoices. It only *creates* a record for a
  source whose hook-equivalent event time is at or after
  ``feed_activated_at``; older sources are recompute-only.
* **Recompute** takes ``as_of`` dates in the settings timezone. Its range is
  the half-open ``[start 00:00, (end + 1 day) 00:00)``. Enumeration is
  widened by fixed margins (orders ``start_date=(start_utc_date - 31 days)``,
  ``end_date=(end + 2 days)``; invoices ``created_from=start - 14 d``,
  ``created_before=min(now, end + 15 d)``); the phase-1 ``as_of`` decides
  membership and anything outside is counted in ``counts.out_of_range``.
* The **weekly report** writes one row per tenant per previous ISO week.

Every job returns before touching any store when the flag or the
persistence layer is off (AC-28).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

from commerce.models.margin import ALERTING_FLAGS, MarginStage, RecordStatus, WriteMode
from commerce.services.commerce_es_mappings import INVOICES_CURRENT_INDEX
from commerce.services.margin_cost_basis import ReaderCache, parse_doc_datetime
from commerce.services.margin_service import (
    SKIP_INVALID_INPUTS,
    ItemResult,
    Inputs,
    MarginService,
    Skip,
    _zone,
    aggregate_rows,
    extract_inputs,
    invoice_mode,
    local_midnight_utc,
    margin_feed_active,
    order_source_key,
    source_keys,
)
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

logger = logging.getLogger(__name__)

MARGIN_GAP_SWEEP_INTERVAL_SECONDS = 21_600  # 6 hours
MARGIN_WEEKLY_REPORT_INTERVAL_SECONDS = 86_400  # daily

SWEEP_INVOICE_LOOKBACK = timedelta(hours=72)
SWEEP_ORDER_LOOKBACK = timedelta(days=14)
SWEEP_SOURCE_CAP = 5_000
PAGE_SIZE = 200

RECOMPUTE_HEARTBEAT_EVERY = 100
RECOMPUTE_ORDER_LEAD_DAYS = 31
RECOMPUTE_ORDER_END_PAD_DAYS = 2
RECOMPUTE_INVOICE_LEAD_DAYS = 14
RECOMPUTE_INVOICE_END_PAD_DAYS = 15

_MAX_TENANTS_PER_SWEEP = 10_000
_DRAFT = "draft"
_VOID = "void"

INVOICE = MarginStage.INVOICE.value
DELIVERY = MarginStage.DELIVERY.value
ORDER_ESTIMATE = MarginStage.ORDER_ESTIMATE.value


def _order_doc(order: Any) -> Dict[str, Any]:
    return order.model_dump(mode="json") if hasattr(order, "model_dump") else dict(order)


async def _order_pages(
    orders: Any, tenant_id: str, *, status: str, start_date: str, end_date: Optional[str] = None
):
    """Keyset pages of order docs, oldest ``created_at`` first."""

    after = None
    while True:
        result = await orders.search(
            tenant_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            size=PAGE_SIZE,
            sort="created_at:asc",
            keyset=True,
            after=after,
            with_total=False,
        )
        yield [_order_doc(o) for o in result.get("orders") or []]
        last_key = result.get("last_key")
        if int(result.get("raw_count") or 0) < PAGE_SIZE or not last_key:
            return
        after = tuple(last_key)


async def _invoice_pages(invoice_service: Any, tenant_id: str, **filters: Any):
    cursor = None
    while True:
        page = await invoice_service.list(tenant_id=tenant_id, cursor=cursor, limit=PAGE_SIZE, **filters)
        yield list(page.get("items") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            return


def _tally(counts: Counter, result: ItemResult) -> None:
    counts[result.outcome] += 1


# ---------------------------------------------------------------------------
# Gap sweep
# ---------------------------------------------------------------------------


async def discover_sweep_tenants(es_service: Any) -> List[str]:
    """Tenants with invoices or fuel orders (``terms`` aggregation, ids only)."""

    tenants: set = set()
    for index in (INVOICES_CURRENT_INDEX, FUEL_ORDERS_CURRENT_INDEX):
        query = {
            "size": 0,
            "aggs": {"tenants": {"terms": {"field": "tenant_id", "size": _MAX_TENANTS_PER_SWEEP}}},
        }
        try:
            response = await es_service.search_documents(index, query, size=0)
        except Exception as exc:  # noqa: BLE001
            logger.error("margin gap sweep tenant scan failed index=%s: %s", index, type(exc).__name__)
            continue
        for bucket in (response.get("aggregations") or {}).get("tenants", {}).get("buckets", []):
            if bucket.get("key"):
                tenants.add(str(bucket["key"]))
    return sorted(tenants)


class _Budget:
    def __init__(self, cap: int, tenant_id: str) -> None:
        self.left = cap
        self.tenant_id = tenant_id
        self.warned = False

    def take(self) -> bool:
        if self.left <= 0:
            if not self.warned:
                logger.warning(
                    "margin gap sweep: source cap %d reached tenant=%s; the rest waits for the next cycle",
                    SWEEP_SOURCE_CAP,
                    self.tenant_id,
                )
                self.warned = True
            return False
        self.left -= 1
        return True


def invoice_event_time(doc: Mapping[str, Any]) -> Optional[datetime]:
    """What the hook would have seen: ``voided_at`` (void), else ``finalized_at``, else ``created_at``."""

    if str(doc.get("status") or "") == _VOID and doc.get("voided_at"):
        return parse_doc_datetime(doc.get("voided_at"))
    return parse_doc_datetime(doc.get("finalized_at")) or parse_doc_datetime(doc.get("created_at"))


def invoice_repair_mode(
    doc: Mapping[str, Any], latest: Optional[Mapping[str, Any]], activated_at: datetime
) -> Optional[str]:
    """The sweep's per-line decision (design "Gap sweep"); ``None`` = no action."""

    status = str(doc.get("status") or _DRAFT)
    if latest is None:
        event_time = invoice_event_time(doc)
        if event_time is None or event_time < activated_at:
            return None  # pre-activation source: recompute only
        return invoice_mode(doc)
    if status == _VOID and latest.get("status") != RecordStatus.VOID.value:
        return WriteMode.VOID.value
    if (
        status not in (_DRAFT, _VOID)
        and latest.get("status") == RecordStatus.ACTIVE.value
        and latest.get("frozen_at") is None
    ):
        return WriteMode.FINALIZE.value
    return None


def delivered_after_activation(order: Mapping[str, Any], activated_at: datetime) -> bool:
    result = order.get("delivery_result") or {}
    delivered_at = parse_doc_datetime(result.get("delivered_at")) if isinstance(result, Mapping) else None
    if delivered_at is not None:
        return delivered_at >= activated_at
    updated_at = parse_doc_datetime(order.get("updated_at"))
    return updated_at is not None and updated_at >= activated_at


async def run_margin_gap_sweep_cycle(
    service: MarginService,
    *,
    es_service: Any,
    tenants: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """One sweep over every tenant; returns outcome counts."""

    if not margin_feed_active():
        return {}
    moment = now or service.now()
    tenant_ids = list(tenants) if tenants is not None else await discover_sweep_tenants(es_service)
    counts: Counter = Counter()
    for tenant_id in tenant_ids:
        try:
            activated_at = await service.repository.ensure_activated(tenant_id, now=moment)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "margin gap sweep: ensure_activated failed tenant=%s error_type=%s; tenant skipped",
                tenant_id,
                type(exc).__name__,
            )
            counts["tenants_failed"] += 1
            continue
        try:
            await _sweep_tenant(service, tenant_id, activated_at, moment, counts)
            counts["tenants"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "margin gap sweep failed tenant=%s error_type=%s", tenant_id, type(exc).__name__
            )
            counts["tenants_failed"] += 1
    if counts[SKIP_INVALID_INPUTS]:
        logger.warning(
            "margin gap sweep: %d source(s) skipped with invalid_inputs", counts[SKIP_INVALID_INPUTS]
        )
    return dict(counts)


async def _sweep_tenant(
    service: MarginService,
    tenant_id: str,
    activated_at: datetime,
    moment: datetime,
    counts: Counter,
) -> None:
    budget = _Budget(SWEEP_SOURCE_CAP, tenant_id)
    cache = ReaderCache()
    if service.invoice_service is not None:
        await _sweep_invoices(service, tenant_id, activated_at, moment, counts, budget, cache)
    await _sweep_deliveries(service, tenant_id, activated_at, moment, counts, budget, cache)
    await _sweep_cancelled_estimates(service, tenant_id, moment, counts, budget, cache)


async def _sweep_invoices(
    service: MarginService,
    tenant_id: str,
    activated_at: datetime,
    moment: datetime,
    counts: Counter,
    budget: _Budget,
    cache: ReaderCache,
) -> None:
    since = max(moment - SWEEP_INVOICE_LOOKBACK, activated_at)
    async for docs in _invoice_pages(service.invoice_service, tenant_id, updated_from=since):
        keys = [key for doc in docs for key, _ in source_keys(INVOICE, doc)]
        latest = await service.repository.latest_for_keys(tenant_id, INVOICE, keys)
        for doc in docs:
            for key, index in source_keys(INVOICE, doc):
                mode = invoice_repair_mode(doc, latest.get(key), activated_at)
                if mode is None:
                    continue
                if not budget.take():
                    return
                counts["sources"] += 1

                def make(doc: Mapping[str, Any] = doc, index: Optional[int] = index) -> Any:
                    return extract_inputs(INVOICE, doc, line_index=index, now=moment)

                if mode == WriteMode.VOID.value:
                    result = await service.void_key(tenant_id, INVOICE, key, make, cache=cache)
                else:
                    result = await service.process_item(make(), mode, cache=cache, background=True)
                _tally(counts, result)


async def _sweep_deliveries(
    service: MarginService,
    tenant_id: str,
    activated_at: datetime,
    moment: datetime,
    counts: Counter,
    budget: _Budget,
    cache: ReaderCache,
) -> None:
    start_date = (moment - SWEEP_ORDER_LOOKBACK).date().isoformat()
    async for docs in _order_pages(service.orders, tenant_id, status="delivered", start_date=start_date):
        keys = [order_source_key(str(d.get("order_id"))) for d in docs]
        latest = await service.repository.latest_for_keys(tenant_id, DELIVERY, keys)
        for doc in docs:
            if order_source_key(str(doc.get("order_id"))) in latest:
                continue
            if not delivered_after_activation(doc, activated_at):
                continue
            if not budget.take():
                return
            counts["sources"] += 1
            item = extract_inputs(DELIVERY, doc, now=moment)
            _tally(counts, await service.process_item(item, WriteMode.LIVE.value, cache=cache, background=True))


async def _sweep_cancelled_estimates(
    service: MarginService,
    tenant_id: str,
    moment: datetime,
    counts: Counter,
    budget: _Budget,
    cache: ReaderCache,
) -> None:
    """Void active estimates of cancelled / failed orders (voids never alert)."""

    start_date = (moment - SWEEP_ORDER_LOOKBACK).date().isoformat()
    for status in ("cancelled", "failed"):
        async for docs in _order_pages(service.orders, tenant_id, status=status, start_date=start_date):
            keys = [order_source_key(str(d.get("order_id"))) for d in docs]
            latest = await service.repository.latest_for_keys(tenant_id, ORDER_ESTIMATE, keys)
            for doc in docs:
                key = order_source_key(str(doc.get("order_id")))
                row = latest.get(key)
                if row is None or row.get("status") != RecordStatus.ACTIVE.value:
                    continue
                if not budget.take():
                    return
                counts["sources"] += 1
                result = await service.void_key(
                    tenant_id,
                    ORDER_ESTIMATE,
                    key,
                    lambda doc=doc: extract_inputs(ORDER_ESTIMATE, doc, now=moment),
                    cache=cache,
                )
                _tally(counts, result)


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------


def recompute_window(start_date: date, end_date: date, zone: Any) -> tuple:
    """Half-open UTC instants ``[start 00:00, (end + 1 day) 00:00)`` in ``zone``."""

    return (
        local_midnight_utc(start_date, zone),
        local_midnight_utc(end_date + timedelta(days=1), zone),
    )


def recompute_order_bounds(start_date: date, end_date: date, zone: Any) -> tuple:
    """``(start_date, end_date)`` strings for ``FuelOrderRepository.search`` (freeze M1).

    ``start_utc_date`` is the UTC date of the range's first instant. The
    repository compares ``created_at`` with bare dates (00:00 UTC), so
    ``end + 2 days`` covers the whole local end day at every IANA offset.
    """

    range_start, _ = recompute_window(start_date, end_date, zone)
    start_utc_date = range_start.date()
    return (
        (start_utc_date - timedelta(days=RECOMPUTE_ORDER_LEAD_DAYS)).isoformat(),
        (end_date + timedelta(days=RECOMPUTE_ORDER_END_PAD_DAYS)).isoformat(),
    )


def _needs_recompute(row: Optional[Mapping[str, Any]]) -> bool:
    """``only_missing``: no active record, or an active ``method=none`` one."""

    return row is None or row.get("status") != RecordStatus.ACTIVE.value or row.get("method") == "none"


class _Run:
    def __init__(self, service: MarginService, tenant_id: str, run_id: str, *, only_missing: bool) -> None:
        self.service = service
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.only_missing = only_missing
        self.cache = ReaderCache()
        self.ticks = 0
        self.counts: Dict[str, Any] = {
            "sources": 0,
            "out_of_range": 0,
            "written": 0,
            "skipped": 0,
            "invalid_inputs": 0,
            "no_inputs": 0,
            "errors": 0,
            "by_flag": {flag.value: 0 for flag in ALERTING_FLAGS},
        }

    async def tick(self) -> None:
        self.ticks += 1
        if self.ticks % RECOMPUTE_HEARTBEAT_EVERY == 0:
            await self.service.repository.heartbeat(self.tenant_id, self.run_id, counts=self.counts)

    def tally(self, result: ItemResult) -> None:
        if result.outcome == "written":
            self.counts["written"] += 1
            record = result.record or {}
            if record.get("status") == RecordStatus.ACTIVE.value:
                for flag in ALERTING_FLAGS:
                    if record.get(f"flag_{flag.value}"):
                        self.counts["by_flag"][flag.value] += 1
        elif result.outcome == "error":
            self.counts["errors"] += 1
        else:
            self.counts["skipped"] += 1

    async def process(self, stage: str, items: List[Any], window: tuple) -> None:
        start, end = window
        in_range: List[Inputs] = []
        for item in items:
            if isinstance(item, Skip):
                self.counts[item.reason] += 1
                await self.service.process_item(item, WriteMode.RECOMPUTE.value, background=True)
                continue
            if not (start <= item.as_of < end):
                self.counts["out_of_range"] += 1
                continue
            in_range.append(item)
        self.counts["sources"] += len(in_range)
        latest: Dict[str, Dict[str, Any]] = {}
        if self.only_missing and in_range:
            latest = await self.service.repository.latest_for_keys(
                self.tenant_id, stage, [i.source_key for i in in_range]
            )
        for item in in_range:
            if self.only_missing and not _needs_recompute(latest.get(item.source_key)):
                self.counts["skipped"] += 1
            else:
                self.tally(
                    await self.service.process_item(
                        item,
                        WriteMode.RECOMPUTE.value,
                        cache=self.cache,
                        background=True,
                        recompute_run_id=self.run_id,
                    )
                )
            await self.tick()


async def run_margin_recompute(
    service: MarginService,
    tenant_id: str,
    run_id: str,
    *,
    start_date: date,
    end_date: date,
    stages: Sequence[str],
    only_missing: bool,
    actor: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The recompute background task; finishes the run row and audits."""

    repo = service.repository
    run = _Run(service, tenant_id, run_id, only_missing=only_missing)
    status = "completed"
    finished: Optional[Dict[str, Any]] = None
    try:
        moment = now or service.now()
        settings = await repo.get_settings(tenant_id)
        zone = _zone(settings.get("timezone"))
        window = recompute_window(start_date, end_date, zone)
        if DELIVERY in stages:
            order_start, order_end = recompute_order_bounds(start_date, end_date, zone)
            async for docs in _order_pages(
                service.orders, tenant_id, status="delivered", start_date=order_start, end_date=order_end
            ):
                await run.process(DELIVERY, [extract_inputs(DELIVERY, d, now=moment) for d in docs], window)
        if INVOICE in stages and service.invoice_service is not None:
            created_from = window[0] - timedelta(days=RECOMPUTE_INVOICE_LEAD_DAYS)
            created_before = min(
                moment, local_midnight_utc(end_date + timedelta(days=RECOMPUTE_INVOICE_END_PAD_DAYS), zone)
            )
            async for docs in _invoice_pages(
                service.invoice_service, tenant_id, created_from=created_from, created_before=created_before
            ):
                items = [
                    extract_inputs(INVOICE, doc, line_index=index, now=moment)
                    for doc in docs
                    if str(doc.get("status") or "") != _VOID
                    for _, index in source_keys(INVOICE, doc)
                ]
                await run.process(INVOICE, items, window)
        if run.counts[SKIP_INVALID_INPUTS]:
            logger.warning(
                "margin recompute: %d source(s) skipped with invalid_inputs tenant=%s run_id=%s",
                run.counts[SKIP_INVALID_INPUTS],
                tenant_id,
                run_id,
            )
        finished = await repo.finish_run(tenant_id, run_id, status="completed", counts=run.counts)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        logger.error(
            "margin recompute failed tenant=%s run_id=%s error_type=%s", tenant_id, run_id, type(exc).__name__
        )
        try:
            await repo.finish_run(tenant_id, run_id, status="failed", counts=run.counts)
        except Exception as inner:  # noqa: BLE001
            logger.error(
                "margin recompute finish failed tenant=%s run_id=%s error_type=%s",
                tenant_id,
                run_id,
                type(inner).__name__,
            )
    service.audit(
        event_type="margin_recompute_finished",
        actor=actor,
        resource_type="margin_recompute_run",
        resource_id=run_id,
        action="finish",
        details={
            "tenant_id": tenant_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "stages": list(stages),
            "only_missing": only_missing,
            "status": status,
            "counts": run.counts,
        },
        tenant_id=tenant_id,
    )
    if finished is not None and finished.get("digest_state") == "pending":
        await service.publish_digest_hint(tenant_id, run_id, run.counts["by_flag"])
    return run.counts


# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------


def previous_iso_week(moment: datetime, zone: Any) -> tuple:
    """``(iso_week, period_start, period_end)`` of the previous ISO week in ``zone``."""

    today = moment.astimezone(zone).date()
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    iso = prev_monday.isocalendar()
    return (
        f"{iso[0]}-W{iso[1]:02d}",
        local_midnight_utc(prev_monday, zone),
        local_midnight_utc(this_monday, zone),
    )


def _weekly_enabled() -> bool:
    from config.settings import get_settings

    settings = get_settings()
    return bool(getattr(settings, "commerce_backbone_enabled", False)) and margin_feed_active()


async def run_margin_weekly_report_cycle(service: MarginService, *, now: Optional[datetime] = None) -> int:
    """Insert last week's report per tenant if absent; returns rows written."""

    if not _weekly_enabled():
        return 0
    repo = service.repository
    moment = now or service.now()
    try:
        tenants = await repo.discovery.tenants_with_records()
    except Exception as exc:  # noqa: BLE001
        logger.error("margin weekly report tenant scan failed: %s", type(exc).__name__)
        return 0
    written = 0
    for tenant_id in tenants:
        try:
            settings = await repo.get_settings(tenant_id)
            iso_week, start, end = previous_iso_week(moment, _zone(settings.get("timezone")))
            if await repo.report_exists(tenant_id, iso_week):
                continue
            totals = aggregate_rows(await service.counted_rows(tenant_id, as_of_from=start, as_of_to=end))
            report = {
                "iso_week": iso_week,
                "period_start": start,
                "period_end": end,
                "revenue_cents": totals["revenue_cents_with_cost"],
                "cost_cents": totals["cost_cents"],
                "margin_cents": totals["margin_cents"],
                "revenue_cents_missing_cost": totals["revenue_cents_missing_cost"],
                "records_total": totals["records"],
                "gallons_ugal_total": totals["gallons_ugal"],
                "flag_counts": totals["flag_counts"],
                "missing_cost_share_bp": totals["missing_cost_share_bp"],
                "generated_at": moment,
            }
            if await repo.insert_report_if_absent(tenant_id, report):
                written += 1
                logger.info("margin weekly report written tenant=%s iso_week=%s", tenant_id, iso_week)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "margin weekly report failed tenant=%s error_type=%s", tenant_id, type(exc).__name__
            )
    return written


__all__ = [
    "MARGIN_GAP_SWEEP_INTERVAL_SECONDS",
    "MARGIN_WEEKLY_REPORT_INTERVAL_SECONDS",
    "RECOMPUTE_HEARTBEAT_EVERY",
    "SWEEP_SOURCE_CAP",
    "delivered_after_activation",
    "discover_sweep_tenants",
    "invoice_event_time",
    "invoice_repair_mode",
    "previous_iso_week",
    "recompute_order_bounds",
    "recompute_window",
    "run_margin_gap_sweep_cycle",
    "run_margin_recompute",
    "run_margin_weekly_report_cycle",
]
