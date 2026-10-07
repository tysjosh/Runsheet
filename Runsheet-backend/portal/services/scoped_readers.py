"""Scoped readers: the only way portal handlers reach stored data (design §2.4, E8, E9).

Every method takes a :class:`~portal.api._authz.PortalScope` first and passes
both ``tenant_id`` and ``customer_id`` into the store query. A single-id
lookup reads by (tenant, id), compares the customer, and raises the *same*
``resource_not_found(...)`` for an out-of-scope id as for a missing one, so a
customer can't tell another customer's ids from unknown ones.

Readers are configured once from bootstrap (:func:`configure_portal_readers`)
and read back with :func:`get_portal_readers`; unconfigured, portal routes
answer 503 ``PORTAL_UNAVAILABLE``.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from errors.codes import ErrorCode
from errors.exceptions import AppException, portal_unavailable, resource_not_found

logger = logging.getLogger(__name__)

FORECAST_INDEX = "mvp_tank_forecasts"
FORECAST_SCAN_SIZE = 500
NEXT_DELIVERY_SCAN_SIZE = 200
DELIVERY_HISTORY_DAYS = 730


def _require_scope(scope: Any) -> Tuple[str, str]:
    """Both ids, non-empty, or a programming error (never a wider query)."""
    tenant_id = getattr(scope, "tenant_id", None)
    customer_id = getattr(scope, "customer_id", None)
    for name, value in (("tenant_id", tenant_id), ("customer_id", customer_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"portal reader called without a {name}")
    return tenant_id, customer_id


def order_not_found(order_id: str) -> AppException:
    return resource_not_found(
        f"Order '{order_id}' not found", details={"order_id": order_id}
    )


def tank_not_found(customer_tank_id: str) -> AppException:
    return resource_not_found(
        f"Tank '{customer_tank_id}' not found",
        details={"customer_tank_id": customer_tank_id},
    )


# ---------------------------------------------------------------------------
# Cursors: base64url(json([sort_value, id]))
# ---------------------------------------------------------------------------


def encode_cursor(last_key: Optional[Sequence[Any]]) -> Optional[str]:
    if not last_key:
        return None
    raw = json.dumps(list(last_key), separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[tuple]:
    """Validate and decode a cursor; anything malformed is 422."""
    if cursor is None or cursor == "":
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError):
        value = None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in value)
    ):
        raise AppException(
            ErrorCode.VALIDATION_ERROR,
            "cursor is not valid",
            status_code=422,
            details={"fields": ["cursor"]},
        )
    return (value[0], value[1])


@dataclass
class Page:
    items: List[Any]
    next_cursor: Optional[str]


def _page(result: Dict[str, Any], items: List[Any], limit: int) -> Page:
    """A next cursor only when the store returned a full page."""
    raw_count = result.get("raw_count", len(result.get("orders") or []))
    last_key = result.get("last_key")
    return Page(items, encode_cursor(last_key) if raw_count >= limit and last_key else None)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class PortalOrderReader:
    """Customer-scoped reads over :class:`fuel.order_repository.FuelOrderRepository`."""

    def __init__(self, order_repository: Any) -> None:
        self._repo = order_repository

    def _own(self, orders: Sequence[Any], customer_id: str) -> List[Any]:
        # Defense in depth: the query already filtered on customer_id.
        return [o for o in orders if getattr(o, "customer_id", None) == customer_id]

    async def list(self, scope: Any, *, limit: int, cursor: Optional[str] = None) -> Page:
        """Newest first, keyset on ``(created_at, order_id)``."""
        tenant_id, customer_id = _require_scope(scope)
        after = decode_cursor(cursor)
        result = await self._repo.search(
            tenant_id,
            customer_id=customer_id,
            sort="created_at:desc",
            keyset=True,
            after=after,
            size=limit,
            with_total=False,
        )
        return _page(result, self._own(result.get("orders") or [], customer_id), limit)

    async def get_or_none(self, scope: Any, order_id: str) -> Optional[Any]:
        """The order, or ``None`` when it is missing or another customer's."""
        tenant_id, customer_id = _require_scope(scope)
        order = await self._repo.get(tenant_id, order_id)
        if order is None or getattr(order, "customer_id", None) != customer_id:
            return None
        return order

    async def get(self, scope: Any, order_id: str) -> Any:
        order = await self.get_or_none(scope, order_id)
        if order is None:
            raise order_not_found(order_id)
        return order

    async def latest_for_tank(self, scope: Any, customer_tank_id: str) -> Optional[Any]:
        """The tank's most recent order (any status), for the ship-to rule (D4)."""
        tenant_id, customer_id = _require_scope(scope)
        result = await self._repo.search(
            tenant_id,
            customer_id=customer_id,
            customer_tank_id=customer_tank_id,
            size=1,
            sort="created_at:desc",
        )
        own = self._own(result.get("orders") or [], customer_id)
        return own[0] if own else None

    async def next_deliveries(
        self,
        scope: Any,
        *,
        statuses: Sequence[str],
        customer_tank_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``{customer_tank_id: earliest-window open order}`` (PD17).

        One query: ``size`` 200 for the list (accepted limitation, design §7),
        ``size`` 1 when a single tank is named.
        """
        tenant_id, customer_id = _require_scope(scope)
        result = await self._repo.search(
            tenant_id,
            customer_id=customer_id,
            customer_tank_id=customer_tank_id,
            statuses=list(statuses),
            size=1 if customer_tank_id else NEXT_DELIVERY_SCAN_SIZE,
            sort="delivery_window_start:asc",
        )
        out: Dict[str, Any] = {}
        for order in self._own(result.get("orders") or [], customer_id):
            tank_id = getattr(order, "customer_tank_id", None)
            if tank_id and tank_id not in out:
                out[tank_id] = order
        return out

    async def deliveries(
        self,
        scope: Any,
        customer_tank_id: str,
        *,
        limit: int,
        cursor: Optional[str] = None,
        now: datetime,
    ) -> Page:
        """Delivered orders for one tank created in the last 730 days (R7.4)."""
        tenant_id, customer_id = _require_scope(scope)
        after = decode_cursor(cursor)
        since = (now - timedelta(days=DELIVERY_HISTORY_DAYS)).astimezone(timezone.utc)
        result = await self._repo.search(
            tenant_id,
            customer_id=customer_id,
            customer_tank_id=customer_tank_id,
            status="delivered",
            start_date=since.isoformat(),
            sort="created_at:desc",
            keyset=True,
            after=after,
            size=limit,
            with_total=False,
        )
        own = [
            o for o in self._own(result.get("orders") or [], customer_id)
            if getattr(o, "customer_tank_id", None) == customer_tank_id
        ]
        return _page(result, own, limit)


# ---------------------------------------------------------------------------
# Tanks
# ---------------------------------------------------------------------------


class PortalTankReader:
    """Customer-scoped reads over :class:`fuel.customer_tank_models.CustomerTankRepository`."""

    def __init__(self, tank_repository: Any) -> None:
        self._repo = tank_repository

    async def list(self, scope: Any) -> List[Any]:
        """The customer's active tanks."""
        tenant_id, customer_id = _require_scope(scope)
        tanks = await self._repo.list_for_tenant(
            tenant_id, customer_id=customer_id, status="active"
        )
        return [
            t for t in tanks
            if getattr(t, "customer_id", None) == customer_id
            and getattr(t, "status", None) == "active"
        ]

    async def get(self, scope: Any, customer_tank_id: str) -> Any:
        """One active tank of the customer, else the not-found 404."""
        tenant_id, customer_id = _require_scope(scope)
        tank = await self._repo.get(tenant_id, customer_tank_id)
        if (
            tank is None
            or getattr(tank, "customer_id", None) != customer_id
            or getattr(tank, "status", None) != "active"
        ):
            raise tank_not_found(customer_tank_id)
        return tank


# ---------------------------------------------------------------------------
# Forecasts
# ---------------------------------------------------------------------------


class PortalForecastReader:
    """Latest ``mvp_tank_forecasts`` document per tank, for one customer."""

    def __init__(self, es_service: Any) -> None:
        self._es = es_service

    async def latest_by_tank(self, scope: Any) -> Dict[str, Dict[str, Any]]:
        """``{customer_tank_id: newest forecast}``.

        A store error is logged and reads as "no forecast", so the tank list
        still answers; the UI shows "No forecast yet".
        """
        tenant_id, customer_id = _require_scope(scope)
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"customer_id": customer_id}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": FORECAST_SCAN_SIZE,
        }
        try:
            resp = await self._es.search_documents(FORECAST_INDEX, query, FORECAST_SCAN_SIZE)
        except Exception as exc:  # noqa: BLE001 — forecast is optional on the page
            logger.warning(
                "portal forecasts: search failed for tenant=%s: %s",
                tenant_id,
                type(exc).__name__,
            )
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for hit in ((resp or {}).get("hits") or {}).get("hits") or []:
            source = hit.get("_source") or {}
            if source.get("tenant_id") != tenant_id or source.get("customer_id") != customer_id:
                continue
            tank_id = source.get("customer_tank_id")
            if tank_id and tank_id not in out:
                out[tank_id] = source
        return out


# ---------------------------------------------------------------------------
# Invoices (design §2.4, §5)
# ---------------------------------------------------------------------------

#: Every invoice status a customer may see; ``draft`` is excluded in the query.
PORTAL_INVOICE_STATUSES: Tuple[str, ...] = ("open", "partial", "paid", "overdue", "void")


def invoice_not_found(invoice_id: str) -> AppException:
    """The same constructor and message ``InvoiceService.get`` uses for a miss."""
    return resource_not_found(
        f"Invoice '{invoice_id}' not found", details={"invoice_id": invoice_id}
    )


class PortalInvoiceReader:
    """Customer-scoped reads over :class:`commerce.services.invoice_service.InvoiceService`.

    The portal cursor is opaque (``encode_cursor([created_at, invoice_id])``).
    Its invoice is re-checked against the scope before it is handed to the
    service, so a cursor naming another customer's invoice answers exactly
    like a malformed one (422) and can't be used to probe for ids.
    """

    def __init__(self, invoice_service: Any) -> None:
        self._service = invoice_service

    @staticmethod
    def _statuses(status: Optional[str]) -> List[str]:
        if status is None:
            return list(PORTAL_INVOICE_STATUSES)
        # An unknown or draft status matches nothing rather than widening.
        return [status] if status in PORTAL_INVOICE_STATUSES else []

    @staticmethod
    def _visible(doc: Any, customer_id: str) -> bool:
        return (
            isinstance(doc, dict)
            and doc.get("customer_id") == customer_id
            and doc.get("status") in PORTAL_INVOICE_STATUSES
        )

    async def get_or_none(self, scope: Any, invoice_id: str) -> Optional[Dict[str, Any]]:
        tenant_id, customer_id = _require_scope(scope)
        try:
            doc = await self._service.get(tenant_id=tenant_id, invoice_id=invoice_id)
        except AppException as exc:
            if exc.error_code == ErrorCode.RESOURCE_NOT_FOUND:
                return None
            raise
        return doc if self._visible(doc, customer_id) else None

    async def get(self, scope: Any, invoice_id: str) -> Dict[str, Any]:
        """One non-draft invoice of the customer, else the not-found 404 (E9)."""
        doc = await self.get_or_none(scope, invoice_id)
        if doc is None:
            raise invoice_not_found(invoice_id)
        return doc

    async def _service_cursor(self, scope: Any, cursor: Optional[str]) -> Optional[str]:
        after = decode_cursor(cursor)
        if after is None:
            return None
        invoice_id = str(after[1])
        if await self.get_or_none(scope, invoice_id) is None:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                "cursor is not valid",
                status_code=422,
                details={"fields": ["cursor"]},
            )
        return invoice_id

    async def list(
        self,
        scope: Any,
        *,
        limit: int,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        created_until: Optional[datetime] = None,
    ) -> Page:
        """Newest first (the service's ``created_at desc, invoice_id`` order)."""
        tenant_id, customer_id = _require_scope(scope)
        service_cursor = await self._service_cursor(scope, cursor)
        result = await self._service.list(
            tenant_id=tenant_id,
            customer_id=customer_id,
            statuses=self._statuses(status),
            created_from=created_from,
            created_before=created_before,
            created_until=created_until,
            cursor=service_cursor,
            limit=limit,
        )
        raw = list(result.get("items") or [])
        items = [doc for doc in raw if self._visible(doc, customer_id)]
        next_cursor = None
        if result.get("next_cursor") and raw:
            last = raw[-1]
            next_cursor = encode_cursor(
                [str(last.get("created_at") or ""), str(result["next_cursor"])]
            )
        return Page(items, next_cursor)

    async def count(
        self,
        scope: Any,
        *,
        status: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        created_until: Optional[datetime] = None,
    ) -> int:
        tenant_id, customer_id = _require_scope(scope)
        return int(
            await self._service.count(
                tenant_id=tenant_id,
                customer_id=customer_id,
                statuses=self._statuses(status),
                created_from=created_from,
                created_before=created_before,
                created_until=created_until,
            )
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortalReaders:
    orders: PortalOrderReader
    tanks: PortalTankReader
    forecasts: PortalForecastReader


_readers: Optional[PortalReaders] = None


def configure_portal_readers(readers: Optional[PortalReaders]) -> None:
    global _readers
    _readers = readers


def get_configured_readers() -> Optional[PortalReaders]:
    return _readers


def get_portal_readers() -> PortalReaders:
    """The configured readers, or 503 ``PORTAL_UNAVAILABLE``."""
    if _readers is None:
        raise portal_unavailable()
    return _readers


__all__ = [
    "PORTAL_INVOICE_STATUSES",
    "PortalForecastReader",
    "PortalInvoiceReader",
    "PortalOrderReader",
    "PortalReaders",
    "PortalTankReader",
    "configure_portal_readers",
    "decode_cursor",
    "encode_cursor",
    "get_configured_readers",
    "get_portal_readers",
    "invoice_not_found",
    "order_not_found",
    "tank_not_found",
]
