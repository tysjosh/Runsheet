"""Admin-only margin API under ``/api/commerce/margin`` (margin-feed FR6, FR7).

Every route is a tenant ``admin`` surface (D1). The gate is a router-level
dependency, so a route added later inherits it:

1. :func:`require_margin_enabled`: ``commerce_backbone_enabled``,
   ``commerce_margin_feed_enabled`` and an active persistence layer, else 404
   ``COMMERCE_DISABLED`` (AC-28). The flag check comes first so a disabled
   feature stays invisible rather than advertising a 403.
2. :func:`require_margin_admin`: ``auth.authorization.require_role(tenant,
   "admin")``, an exact match, so ``dispatcher``, ``driver``,
   ``platform_admin`` alone and ``customer`` sessions get 403 (AC-29, AC-30).

``tenant_id`` only ever comes from the ``TenantContext`` (AC-25): bodies are
``extra="forbid"`` models and no route declares a ``tenant_id`` query
parameter.

The export goes through :mod:`services.csv_export` (``export_type="margin"``)
and is declared before ``GET /records/{record_id}`` so ``export`` is never
matched as a record id. Missing cost is an empty cell, never 0 (Simplification
12).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from auth.authorization import require_role
from commerce.models.margin import (
    AlertStatus,
    AlertType,
    CostEntrySupersede,
    CostEntryCreate,
    CostEntryVoid,
    CostMethod,
    MarginAlertAction,
    MarginFlag,
    MarginPreviewRequest,
    MarginRecomputeRequest,
    MarginSettingsUpdate,
    MarginStage,
    RecordStatus,
    margin_pct,
)
from commerce.services.margin_repository import RecordFilters
from commerce.services.margin_service import local_midnight_utc
from config.settings import get_settings
from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize
from middleware.rate_limiter import limiter
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from persistence.database import is_persistence_enabled
from services.csv_export import (
    EXPORT_RATE_LIMIT,
    ExportColumn,
    KeysetPage,
    KeysetSource,
    export_guard,
    export_rate_key,
    stream_csv_export,
)
from services.date_range import parse_date_range

logger = logging.getLogger(__name__)

#: ``GET /records`` and ``GET /cost-entries`` page bounds.
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
#: ``GET /reports`` bound (one year of ISO weeks).
REPORTS_MAX_LIMIT = 52
#: ``GET /summary`` default range when no dates are given.
SUMMARY_DEFAULT_DAYS = 30
_CURSOR_MAX_LENGTH = 512
_RECORD_STATUSES = frozenset({s.value for s in RecordStatus} | {"all"})
_ALERT_STATUSES = frozenset(s.value for s in AlertStatus)
_ALERT_TYPES = frozenset(t.value for t in AlertType)
_STAGES = frozenset(s.value for s in MarginStage)
_FLAGS = frozenset(f.value for f in MarginFlag)
_FLAG_COLUMNS: Tuple[Tuple[str, str], ...] = (
    (MarginFlag.MISSING_COST.value, "flag_missing_cost"),
    (MarginFlag.NEGATIVE_MARGIN.value, "flag_negative_margin"),
    (MarginFlag.BELOW_FLOOR.value, "flag_below_floor"),
    (MarginFlag.TERMINAL_UNATTRIBUTED.value, "flag_terminal_unattributed"),
)


# ---------------------------------------------------------------------------
# Wiring (bootstrap/core.py calls configure_margin_api after wire_margin_feed)
# ---------------------------------------------------------------------------

_margin_service: Any = None
_cost_entry_service: Any = None


def configure_margin_api(*, margin_service: Any, cost_entry_service: Any) -> None:
    """Inject the MarginService and MarginCostEntryService (bootstrap, tests)."""
    global _margin_service, _cost_entry_service
    _margin_service = margin_service
    _cost_entry_service = cost_entry_service


def _service() -> Any:
    if _margin_service is None:
        raise RuntimeError("Margin API not configured. Call configure_margin_api() during startup.")
    return _margin_service


def _entries() -> Any:
    if _cost_entry_service is None:
        raise RuntimeError("Margin API not configured. Call configure_margin_api() during startup.")
    return _cost_entry_service


# ---------------------------------------------------------------------------
# Guard (design "Admin API" / "Guard")
# ---------------------------------------------------------------------------


async def require_margin_enabled(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """404 ``COMMERCE_DISABLED`` unless the backbone, the margin flag and persistence are on."""
    settings = get_settings()
    if not (
        getattr(settings, "commerce_backbone_enabled", False)
        and getattr(settings, "commerce_margin_feed_enabled", False)
        and is_persistence_enabled()
    ):
        logger.debug("Margin request blocked: margin feed disabled for tenant_id=%s", tenant.tenant_id)
        raise AppException(
            ErrorCode.COMMERCE_DISABLED,
            "Margin feed is not enabled",
            status_code=404,
        )
    return tenant


async def require_margin_admin(
    tenant: TenantContext = Depends(require_margin_enabled),
) -> TenantContext:
    """403 unless the caller holds ``admin`` (exact match; ``platform_admin`` alone is refused)."""
    require_role(tenant, "admin")
    return tenant


router = APIRouter(
    prefix="/api/commerce/margin",
    tags=["commerce-margin"],
    dependencies=[Depends(require_margin_admin)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(request: Request, data: Any) -> Dict[str, Any]:
    return {"data": data, "request_id": getattr(request.state, "request_id", "unknown")}


def _invalid(loc: str, msg: str, type_: str) -> AppException:
    return AppException(
        ErrorCode.VALIDATION_ERROR,
        "Invalid margin request",
        status_code=422,
        details={"errors": [{"loc": [loc], "msg": msg, "type": type_}]},
    )


def _encode_cursor(key: Optional[Tuple[Any, ...]]) -> Optional[str]:
    """Opaque URL-safe base64 of the keyset tuple ``(timestamp, id)``."""
    if key is None:
        return None
    instant, ident = key
    raw = json.dumps([instant.astimezone(timezone.utc).isoformat(), ident]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> Optional[Tuple[datetime, str]]:
    if cursor is None or cursor == "":
        return None
    try:
        if len(cursor) > _CURSOR_MAX_LENGTH:
            raise ValueError("too long")
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, ident = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        instant = datetime.fromisoformat(stamp)
        if instant.tzinfo is None or not isinstance(ident, str) or not ident:
            raise ValueError("bad key")
        return instant, ident
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise _invalid("cursor", "the cursor is not valid; restart from the first page", "invalid_cursor") from None


def _product_filter(product_code: Optional[str]) -> Optional[str]:
    """Canonical code when known, else the raw value (records keep raw unknown codes)."""
    if product_code is None or not product_code.strip():
        return None
    raw = product_code.strip()
    try:
        return canonicalize(raw)
    except (UnknownFuelProductError, TypeError):
        return raw


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _bare_date(raw: Optional[str]) -> Optional[date]:
    """The day of a ``YYYY-MM-DD`` value (already validated), else ``None``."""
    value = (raw or "").strip()
    return date.fromisoformat(value) if len(value) == 10 else None


def _as_of_bounds(
    start_date: Optional[str], end_date: Optional[str], zone: ZoneInfo
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Half-open ``[from, to)`` on ``as_of``.

    A bare ``YYYY-MM-DD`` is a day in the settings timezone, the same axis as
    summary, recompute and the weekly report (Simplification 13), so the end
    is ``(end + 1 day) 00:00`` local. An ISO datetime keeps its exact instant,
    and a datetime end stays inclusive. Each bound is parsed on its own so
    the order check below runs on the local-day bounds, not on UTC days.
    """
    start = parse_date_range(start_date, None).gte
    end_inclusive = parse_date_range(None, end_date).lte
    start_day, end_day = _bare_date(start_date), _bare_date(end_date)
    as_of_from = local_midnight_utc(start_day, zone) if start_day is not None else start
    if end_day is not None:
        as_of_to: Optional[datetime] = local_midnight_utc(end_day + timedelta(days=1), zone)
    elif end_inclusive is not None:
        as_of_to = end_inclusive + timedelta(microseconds=1)
    else:
        as_of_to = None
    if as_of_from is not None and as_of_to is not None and as_of_from >= as_of_to:
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "start_date must not be after end_date",
            status_code=422,
            details={"field": "start_date", "reason": "after_end_date"},
        )
    return as_of_from, as_of_to


def _record_filters(
    *,
    zone: ZoneInfo,
    start_date: Optional[str],
    end_date: Optional[str],
    customer_id: Optional[str],
    product_code: Optional[str],
    terminal_id: Optional[str],
    stage: Optional[str],
    flag: Optional[str],
    status_value: str,
) -> RecordFilters:
    as_of_from, as_of_to = _as_of_bounds(start_date, end_date, zone)
    if stage is not None and stage not in _STAGES:
        raise _invalid("stage", "use order_estimate, delivery or invoice", "invalid_choice")
    if flag is not None and flag not in _FLAGS:
        raise _invalid("flag", "unknown margin flag", "invalid_choice")
    if status_value not in _RECORD_STATUSES:
        raise _invalid("status", "use active, superseded, void or all", "invalid_choice")
    return RecordFilters(
        as_of_from=as_of_from,
        as_of_to=as_of_to,
        customer_id=_clean(customer_id),
        product_code=_product_filter(product_code),
        terminal_id=_clean(terminal_id),
        stage=stage,
        flag=flag,
        status=status_value,
    )


def _flags(row: Mapping[str, Any]) -> List[str]:
    return [name for name, column in _FLAG_COLUMNS if row.get(column)]


def _record_view(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A stored record plus ``flags`` and ``margin_pct``; null cost stays null."""
    view = {k: v for k, v in row.items() if k != "tenant_id"}
    view["flags"] = _flags(row)
    view["margin_pct"] = margin_pct(row.get("margin_bp"))
    return view


def _without_tenant(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in row.items() if k != "tenant_id"}


# ---------------------------------------------------------------------------
# Cost entries (FR1)
# ---------------------------------------------------------------------------


@router.get("/cost-entries")
async def list_cost_entries(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    kind: Optional[str] = Query(default=None, max_length=16),
    product_code: Optional[str] = Query(default=None, max_length=64),
    terminal_id: Optional[str] = Query(default=None, max_length=128),
    status_filter: str = Query(default="active", alias="status", max_length=16),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
) -> Dict[str, Any]:
    """Cost entries, newest first (``created_at`` desc), keyset paged."""
    page = await _entries().list_entries(
        tenant.tenant_id,
        kind=kind,
        product_code=_clean(product_code),
        terminal_id=_clean(terminal_id),
        status=status_filter,
        after=_decode_cursor(cursor),
        limit=limit,
    )
    return _envelope(request, {
        "items": [_without_tenant(item) for item in page.items],
        "next_cursor": _encode_cursor(page.next_key),
    })


@router.post("/cost-entries", status_code=status.HTTP_201_CREATED)
async def create_cost_entry(
    request: Request,
    body: CostEntryCreate,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Create one active entry (purchase, override or adder). Audit-logged."""
    result = await _entries().create(tenant.tenant_id, tenant.user_id, body)
    return _envelope(request, {"entry": _without_tenant(result["entry"]), "warnings": result["warnings"]})


@router.post("/cost-entries/import")
async def import_cost_entries(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    file: UploadFile = File(..., description="UTF-8 CSV of cost entries (5 MB, 10,000 rows max)."),
    dry_run: bool = Form(default=True, description="Validate only (default); false writes the rows."),
) -> Dict[str, Any]:
    """CSV import. All or nothing: any invalid row is a 422 and nothing is written."""
    report = await _entries().import_csv(tenant.tenant_id, tenant.user_id, file, dry_run=dry_run)
    return _envelope(request, report)


@router.post("/cost-entries/{entry_id}/supersede", status_code=status.HTTP_201_CREATED)
async def supersede_cost_entry(
    request: Request,
    entry_id: str,
    body: CostEntrySupersede,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Replace an active entry with a new version (same kind) and a reason."""
    result = await _entries().supersede(tenant.tenant_id, tenant.user_id, entry_id, body)
    return _envelope(request, {
        "entry": _without_tenant(result["entry"]),
        "superseded": _without_tenant(result["superseded"]),
        "warnings": result["warnings"],
    })


@router.post("/cost-entries/{entry_id}/void")
async def void_cost_entry(
    request: Request,
    entry_id: str,
    body: CostEntryVoid,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Void an active entry with a reason."""
    result = await _entries().void(tenant.tenant_id, tenant.user_id, entry_id, body)
    return _envelope(request, {"entry": _without_tenant(result["entry"])})


# ---------------------------------------------------------------------------
# Cost basis (FR2 diagnostic)
# ---------------------------------------------------------------------------


@router.get("/cost-basis")
async def get_cost_basis(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    product_code: str = Query(..., min_length=1, max_length=64),
    terminal_id: Optional[str] = Query(default=None, max_length=128),
    as_of: Optional[datetime] = Query(default=None, description="ISO-8601; default now. A naive time is UTC."),
) -> Dict[str, Any]:
    """The method, lots, rack pick and exclusion counts the resolver would use now."""
    instant = as_of
    if instant is not None and instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    basis = await _service().cost_basis(
        tenant.tenant_id, product_code=product_code, terminal_id=terminal_id, as_of=instant
    )
    return _envelope(request, basis)


# ---------------------------------------------------------------------------
# Records (FR6.1) and the export (FR7)
# ---------------------------------------------------------------------------


@router.get("/records")
async def list_records(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    start_date: Optional[str] = Query(
        default=None, description="Sale date (as_of) from: YYYY-MM-DD (settings timezone) or ISO-8601."
    ),
    end_date: Optional[str] = Query(
        default=None, description="Sale date (as_of) to: YYYY-MM-DD (whole day, settings timezone) or ISO-8601."
    ),
    customer_id: Optional[str] = Query(default=None, max_length=128),
    product_code: Optional[str] = Query(default=None, max_length=64),
    terminal_id: Optional[str] = Query(default=None, max_length=128),
    stage: Optional[str] = Query(default=None, max_length=32),
    flag: Optional[str] = Query(default=None, max_length=32),
    status_filter: str = Query(default=RecordStatus.ACTIVE.value, alias="status", max_length=16),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
) -> Dict[str, Any]:
    """Margin records, newest sale first (``as_of`` desc, ``record_id`` desc).

    ``timezone`` is the settings timezone the date filters use; the UI shows
    each record's sale date in it.
    """
    zone = await _service().settings_zone(tenant.tenant_id)
    filters = _record_filters(
        zone=zone, start_date=start_date, end_date=end_date, customer_id=customer_id,
        product_code=product_code, terminal_id=terminal_id, stage=stage, flag=flag,
        status_value=status_filter,
    )
    page = await _service().repository.list_records(
        tenant.tenant_id, filters, after=_decode_cursor(cursor), limit=limit
    )
    return _envelope(request, {
        "items": [_record_view(item) for item in page.items],
        "next_cursor": _encode_cursor(page.next_key),
        "timezone": str(zone.key),
    })


_MICRO = Decimal("0.000001")
_CENT = Decimal("0.01")


def _usd(value: Optional[int], places: int) -> Optional[Decimal]:
    """Integer cents (2) or micros (6) as an exact USD ``Decimal``; ``None`` stays ``None``."""
    if value is None:
        return None
    return Decimal(int(value)).scaleb(-places).quantize(_CENT if places == 2 else _MICRO)


def _costed(row: Mapping[str, Any], value: Any) -> Any:
    """A cost-derived cell: empty for ``method=none``, never 0 (Simplification 12)."""
    if row.get("method") == CostMethod.NONE.value:
        return None
    return value


def _pct(row: Mapping[str, Any]) -> Optional[Decimal]:
    # A Decimal, not the "-12.50" string, so a negative margin is not
    # formula-escaped by escape_cell.
    text = margin_pct(row.get("margin_bp"))
    return _costed(row, Decimal(text) if text is not None else None)


def _col(name: str) -> ExportColumn:
    return ExportColumn(name, lambda r, n=name: r.get(n))


def _costed_col(name: str) -> ExportColumn:
    return ExportColumn(name, lambda r, n=name: _costed(r, r.get(n)))


#: Design "Export" column order. No contact fields.
MARGIN_EXPORT_COLUMNS: List[ExportColumn] = [
    *(_col(n) for n in (
        "record_id", "stage", "status", "origin", "version", "as_of", "order_id",
        "invoice_id", "line_index", "customer_id", "account_id", "product_code", "terminal_id",
    )),
    ExportColumn("gallons", lambda r: _usd(r.get("gallons_ugal"), 6)),
    _col("unit_price_micros"),
    ExportColumn("unit_price_usd", lambda r: _usd(r.get("unit_price_micros"), 6)),
    _col("revenue_cents"),
    ExportColumn("revenue_usd", lambda r: _usd(r.get("revenue_cents"), 2)),
    _col("method"),
    _col("no_cost_reason"),
    _costed_col("product_cost_micros"),
    _col("adders_micros"),
    _costed_col("landed_cost_micros"),
    ExportColumn("landed_cost_usd", lambda r: _costed(r, _usd(r.get("landed_cost_micros"), 6))),
    _costed_col("cost_cents"),
    ExportColumn("cost_usd", lambda r: _costed(r, _usd(r.get("cost_cents"), 2))),
    _costed_col("margin_cents"),
    ExportColumn("margin_usd", lambda r: _costed(r, _usd(r.get("margin_cents"), 2))),
    _costed_col("margin_per_gallon_micros"),
    ExportColumn("margin_per_gallon_usd", lambda r: _costed(r, _usd(r.get("margin_per_gallon_micros"), 6))),
    ExportColumn("margin_pct", _pct),
    ExportColumn("flags", _flags),
    _col("floor_micros_used"),
    ExportColumn("adders_configured", lambda r: bool((r.get("cost_snapshot") or {}).get("adders_configured"))),
    ExportColumn("terminal_unattributed", lambda r: bool(r.get("flag_terminal_unattributed"))),
    _col("computed_at"),
]


def _records_export_source(repository: Any, tenant_id: str, filters: RecordFilters) -> KeysetSource:
    """KeysetSource on (``as_of``, ``record_id``) with an exact count."""

    async def fetch(after, page_size, with_total):
        page = await repository.list_records(tenant_id, filters, after=after, limit=page_size)
        return KeysetPage(rows=page.items, raw_count=len(page.items), total=None, last_key=page.next_key)

    async def count() -> int:
        return await repository.count_records(tenant_id, filters)

    return KeysetSource(fetch, count=count)


# Declared BEFORE /records/{record_id}: otherwise "export" matches as a record
# id and the request bypasses export_guard and the limiter.
@router.get("/records/export", response_model=None)
@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)
async def export_records(
    request: Request,
    tenant: TenantContext = Depends(export_guard("admin", base=require_margin_enabled)),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    customer_id: Optional[str] = Query(default=None, max_length=128),
    product_code: Optional[str] = Query(default=None, max_length=64),
    terminal_id: Optional[str] = Query(default=None, max_length=128),
    stage: Optional[str] = Query(default=None, max_length=32),
    flag: Optional[str] = Query(default=None, max_length=32),
    status_filter: str = Query(default=RecordStatus.ACTIVE.value, alias="status", max_length=16),
):
    """CSV of the records matching the list filters (admin only, 50,000 rows max)."""
    filters = _record_filters(
        zone=await _service().settings_zone(tenant.tenant_id),
        start_date=start_date, end_date=end_date, customer_id=customer_id,
        product_code=product_code, terminal_id=terminal_id, stage=stage, flag=flag,
        status_value=status_filter,
    )
    source = _records_export_source(_service().repository, tenant.tenant_id, filters)
    return await stream_csv_export(
        request=request, tenant=tenant, export_type="margin",
        columns=MARGIN_EXPORT_COLUMNS, source=source,
        filters={
            "start_date": start_date, "end_date": end_date, "customer_id": filters.customer_id,
            "product_code": filters.product_code, "terminal_id": filters.terminal_id,
            "stage": stage, "flag": flag, "status": status_filter,
        },
    )


@router.get("/records/{record_id}")
async def get_record(
    request: Request,
    record_id: str,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """One record with its ``cost_snapshot`` and every version of its source key."""
    repository = _service().repository
    record = await repository.get_record(tenant.tenant_id, record_id)
    if record is None:
        raise AppException(ErrorCode.RESOURCE_NOT_FOUND, "Margin record not found", status_code=404)
    versions = await repository.record_versions(tenant.tenant_id, record["stage"], record["source_key"])
    view = _record_view(record)
    view["versions"] = [_record_view(v) for v in versions]
    return _envelope(request, view)


# ---------------------------------------------------------------------------
# Summary, preview, recompute (FR6.1, FR6.2, FR3.5)
# ---------------------------------------------------------------------------


@router.get("/summary")
async def get_summary(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    group_by: str = Query(default="day", max_length=16),
    start_date: Optional[date] = Query(default=None, description="YYYY-MM-DD (as_of date, settings timezone)."),
    end_date: Optional[date] = Query(default=None, description="YYYY-MM-DD, inclusive. Span <= 92 days."),
) -> Dict[str, Any]:
    """Stage-preferred totals; ``cost_cents``/``margin_cents`` cover costed records only."""
    end = end_date or (start_date + timedelta(days=SUMMARY_DEFAULT_DAYS - 1) if start_date else None)
    if end is None:  # "today" on the settings-timezone axis (Simplification 13)
        end = datetime.now(await _service().settings_zone(tenant.tenant_id)).date()
    start = start_date or end - timedelta(days=SUMMARY_DEFAULT_DAYS - 1)
    summary = await _service().summary(tenant.tenant_id, start_date=start, end_date=end, group_by=group_by)
    return _envelope(request, summary)


@router.post("/preview")
async def preview_margin(
    request: Request,
    body: MarginPreviewRequest,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Quote-stage margin for a hypothetical sale. Persists nothing; no alert."""
    return _envelope(request, await _service().preview(tenant.tenant_id, body))


@router.post("/recompute", status_code=status.HTTP_202_ACCEPTED)
async def start_recompute(
    request: Request,
    body: MarginRecomputeRequest,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Start a recompute run over an ``as_of`` date range (one at a time per tenant)."""
    return _envelope(request, await _service().start_recompute(tenant.tenant_id, tenant.user_id, body))


@router.get("/recompute/{run_id}")
async def get_recompute_run(
    request: Request,
    run_id: str,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    run = await _service().repository.get_run(tenant.tenant_id, run_id)
    if run is None:
        raise AppException(ErrorCode.RESOURCE_NOT_FOUND, "Recompute run not found", status_code=404)
    return _envelope(request, _without_tenant(run))


# ---------------------------------------------------------------------------
# Alerts and weekly reports (RevenueGuard's admin channel)
# ---------------------------------------------------------------------------


@router.get("/alerts")
async def list_alerts(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    status_filter: Optional[str] = Query(
        default=None, alias="status", max_length=128,
        description="Comma-separated: open, acknowledged, pending_review, approved, dismissed.",
    ),
    alert_type: Optional[str] = Query(default=None, max_length=32),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
) -> Dict[str, Any]:
    """Margin alerts, newest first, with the matching ``total``."""
    statuses: Optional[List[str]] = None
    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        if any(s not in _ALERT_STATUSES for s in statuses):
            raise _invalid("status", "unknown alert status", "invalid_choice")
    if alert_type is not None and alert_type not in _ALERT_TYPES:
        raise _invalid("alert_type", "unknown alert type", "invalid_choice")
    page = await _service().repository.list_alerts(
        tenant.tenant_id, statuses=statuses, alert_type=alert_type,
        after=_decode_cursor(cursor), limit=limit,
    )
    return _envelope(request, {
        "items": [_without_tenant(item) for item in page.items],
        "next_cursor": _encode_cursor(page.next_key),
        "total": page.total,
    })


async def _transition(
    request: Request, tenant: TenantContext, alert_id: str, action: str, body: Optional[MarginAlertAction]
) -> Dict[str, Any]:
    alert = await _service().transition_alert(
        tenant.tenant_id, tenant.user_id, alert_id, action, note=body.note if body else None
    )
    return _envelope(request, _without_tenant(alert))


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    request: Request,
    alert_id: str,
    body: Optional[MarginAlertAction] = None,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    return await _transition(request, tenant, alert_id, "acknowledge", body)


@router.post("/alerts/{alert_id}/approve")
async def approve_alert(
    request: Request,
    alert_id: str,
    body: Optional[MarginAlertAction] = None,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Record approval of a leakage proposal. Nothing executes (FR5.9)."""
    return await _transition(request, tenant, alert_id, "approve", body)


@router.post("/alerts/{alert_id}/dismiss")
async def dismiss_alert(
    request: Request,
    alert_id: str,
    body: Optional[MarginAlertAction] = None,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    return await _transition(request, tenant, alert_id, "dismiss", body)


@router.get("/reports")
async def list_reports(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
    limit: int = Query(default=12, ge=1, le=REPORTS_MAX_LIMIT),
) -> Dict[str, Any]:
    """Weekly margin reports, newest ISO week first."""
    reports = await _service().repository.list_reports(tenant.tenant_id, limit=limit)
    return _envelope(request, {"items": [_without_tenant(r) for r in reports]})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings")
async def get_margin_settings(
    request: Request,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    settings = await _service().repository.get_settings(tenant.tenant_id)
    return _envelope(request, _without_tenant(settings))


@router.put("/settings")
async def put_margin_settings(
    request: Request,
    body: MarginSettingsUpdate,
    tenant: TenantContext = Depends(require_margin_admin),
) -> Dict[str, Any]:
    """Replace the tenant's margin settings. Audit-logged with before/after."""
    result = await _entries().update_settings(tenant.tenant_id, tenant.user_id, body)
    return _envelope(request, {"settings": _without_tenant(result["settings"]), "warnings": result["warnings"]})
