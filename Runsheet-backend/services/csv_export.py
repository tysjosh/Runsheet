"""Shared CSV export helper (data-export v1, design §1).

Everything that makes a tenant export safe lives here, so the five export
handlers stay thin: cell escaping (CSV/formula injection), UTF-8 BOM, header
row, keyset paging driver, the 50,000-row cap checked before the first byte,
the per-user rate-limit key, the role guard dependency, the filename and the
single structured audit line.

No handler writes CSV or sets ``Content-Disposition`` itself.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

from auth.authorization import require_role
from config.settings import get_settings
from errors.codes import ErrorCode
from errors.exceptions import AppException
from middleware.rate_limiter import get_client_ip
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)

#: Hard cap on rows per export. Over it the request gets 413 before any byte.
MAX_EXPORT_ROWS: int = 50_000
#: Rows fetched per page. Equals the HybridReadRepository clamp.
EXPORT_PAGE_SIZE: int = 200
#: Per user, per export route.
EXPORT_RATE_LIMIT: str = f"{get_settings().export_rate_limit}/minute"

ExportType = Literal[
    "ifta", "orders", "jobs", "reconciliation", "invoices",
    # OI-20 / OI-57 (owner decision 2026-10-07), admin only.
    "driver_hours", "driver_qualifications",
    # margin-feed FR7, tenant admin only.
    "margin",
]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_FREE_TEXT_FILTERS = frozenset({"q"})


class ExportCapExceededDuringStream(RuntimeError):
    """More rows arrived while streaming than the pre-stream count allowed."""


@dataclass(frozen=True)
class ExportColumn:
    header: str
    value: Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class KeysetPage:
    #: Rows after validation/mapping (what gets written).
    rows: list
    #: Hits/rows the store returned BEFORE any drop. Paging stops on this.
    raw_count: int
    #: Exact scope total when requested with ``with_total=True``.
    total: Optional[int]
    #: Key of the LAST RAW hit/row, never the last valid row.
    last_key: Optional[tuple]


KeysetFetch = Callable[[Optional[tuple], int, bool], Awaitable[KeysetPage]]


class ExportSource(Protocol):
    async def count(self) -> int: ...

    def pages(self) -> AsyncIterator[Sequence[Mapping[str, Any]]]: ...


class KeysetSource:
    """ExportSource over a ``fetch(after, size, with_total)`` callable."""

    def __init__(
        self,
        fetch: KeysetFetch,
        *,
        page_size: int = EXPORT_PAGE_SIZE,
        count: Optional[Callable[[], Awaitable[int]]] = None,
    ) -> None:
        self._fetch = fetch
        self._page_size = page_size
        self._count = count

    async def count(self) -> int:
        if self._count is not None:
            return int(await self._count())
        page = await self._fetch(None, 1, True)
        return int(page.total or 0)

    async def pages(self) -> AsyncIterator[Sequence[Mapping[str, Any]]]:
        after: Optional[tuple] = None
        while True:
            page = await self._fetch(after, self._page_size, False)
            if page.rows:
                yield page.rows
            if page.raw_count < self._page_size or page.last_key is None:
                break
            after = page.last_key


class StaticSource:
    """ExportSource over an in-memory row list (IFTA)."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    async def count(self) -> int:
        return len(self._rows)

    async def pages(self) -> AsyncIterator[Sequence[Mapping[str, Any]]]:
        for start in range(0, len(self._rows), EXPORT_PAGE_SIZE):
            yield self._rows[start:start + EXPORT_PAGE_SIZE]


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def _format(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "; ".join(_format(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def escape_cell(value: Any) -> str:
    """Stringify one cell and neutralise spreadsheet formulas.

    Strings (and lists/dicts, which are formatted to strings) starting with one
    of ``= + - @ \\t \\r`` get a leading ``'``. Numbers are never prefixed, so a
    negative quantity stays numeric in Excel (DD-3). Quoting of commas, quotes
    and newlines is left to ``csv.writer``.
    """
    if value is None or isinstance(value, (bool, int, float, Decimal, datetime, date)):
        return _format(value)
    text = _format(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def export_filename(export_type: str, tenant_id: str, now: datetime) -> str:
    """``<type>_<tenant>_<YYYYMMDD>.csv`` with the tenant sanitised for a header."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", tenant_id or "")[:64] or "tenant"
    return f"{export_type}_{safe}_{now.astimezone(timezone.utc):%Y%m%d}.csv"


# ---------------------------------------------------------------------------
# Guard and rate-limit key
# ---------------------------------------------------------------------------


def export_guard(*allowed: str, base: Callable = get_tenant_context) -> Callable:
    """Build a dependency: resolve ``base``, require a role, stamp the user.

    Same shape as :func:`auth.router_guards.roles_dependency`. ``base`` lets
    invoices pass ``require_invoicing_enabled`` so the flag 404 still comes
    before any 403. The stamp feeds :func:`export_rate_key`.
    """
    if not allowed:
        raise ValueError("export_guard() requires at least one role name")

    async def _dependency(
        request: Request,
        tenant: TenantContext = Depends(base),
    ) -> TenantContext:
        require_role(tenant, *allowed)
        request.state.export_tenant_id = tenant.tenant_id
        request.state.export_user_id = tenant.user_id
        return tenant

    _dependency.__name__ = f"export_guard_{'_or_'.join(allowed)}"
    return _dependency


def export_rate_key(request: Request) -> str:
    """``export:{tenant}:{user}`` once the guard has run, else the client IP."""
    user_id = getattr(request.state, "export_user_id", None)
    tenant_id = getattr(request.state, "export_tenant_id", None) or getattr(
        request.state, "tenant_id", None
    )
    if user_id and tenant_id:
        return f"export:{tenant_id}:{user_id}"
    return get_client_ip(request)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _loggable_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    """Filters for the audit line. Free text is logged as presence and length."""
    out: dict[str, Any] = {}
    for key, value in filters.items():
        if value is None:
            continue
        if key in _FREE_TEXT_FILTERS:
            out[key] = {"present": True, "length": len(str(value))}
        elif isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def _encode_rows(rows: Sequence[Sequence[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


async def stream_csv_export(
    *,
    request: Request,
    tenant: TenantContext,
    export_type: ExportType,
    columns: Sequence[ExportColumn],
    source: ExportSource,
    filters: Mapping[str, Any],
    max_rows: Optional[int] = None,
) -> StreamingResponse:
    """Count, enforce the cap, then stream BOM + header + one chunk per page."""
    cap = MAX_EXPORT_ROWS if max_rows is None else max_rows
    started = time.monotonic()
    request_id = getattr(request.state, "request_id", "unknown")
    log_filters = _loggable_filters(filters)

    def audit(outcome: str, row_count: int) -> None:
        level = logging.WARNING if outcome in ("failed", "aborted_cap_race") else logging.INFO
        logger.log(
            level,
            "data_export",
            extra={"extra_data": {
                "event": "data_export",
                "export_type": export_type,
                "tenant_id": tenant.tenant_id,
                "user_id": tenant.user_id,
                "filters": log_filters,
                "row_count": row_count,
                "outcome": outcome,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "request_id": request_id,
            }},
        )

    try:
        total = await source.count()
    except AppException:
        raise
    except Exception:
        logger.exception(
            "data_export count failed",
            extra={"extra_data": {
                "export_type": export_type,
                "tenant_id": tenant.tenant_id,
                "user_id": tenant.user_id,
            }},
        )
        # Every export attempt leaves one audit line, including a count that
        # fails before streaming starts (OI-24).
        audit("failed", 0)
        raise

    if total > cap:
        audit("rejected_too_large", total)
        raise AppException(
            error_code=ErrorCode.EXPORT_TOO_LARGE,
            message=(
                f"This export has {total:,} rows, over the {cap:,}-row limit. "
                "Narrow the filters and try again."
            ),
            status_code=413,
            details={"row_count": total, "max_rows": cap},
        )

    header = [c.header for c in columns]

    async def gen() -> AsyncIterator[bytes]:
        written = 0
        outcome = "failed"
        try:
            yield "\ufeff".encode("utf-8") + _encode_rows([header])
            async for page in source.pages():
                if written + len(page) > cap:
                    outcome = "aborted_cap_race"
                    raise ExportCapExceededDuringStream(
                        f"export exceeded {cap} rows while streaming"
                    )
                chunk = _encode_rows(
                    [[escape_cell(c.value(row)) for c in columns] for row in page]
                )
                written += len(page)
                yield chunk
            outcome = "completed"
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "client_disconnected"
            raise
        except ExportCapExceededDuringStream:
            raise
        except Exception as exc:
            outcome = "failed"
            logger.error(
                "data_export stream failed: %s",
                type(exc).__name__,
                extra={"extra_data": {
                    "export_type": export_type,
                    "tenant_id": tenant.tenant_id,
                    "user_id": tenant.user_id,
                    "request_id": request_id,
                }},
            )
            raise
        finally:
            audit(outcome, written)

    name = export_filename(export_type, tenant.tenant_id, datetime.now(timezone.utc))
    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
