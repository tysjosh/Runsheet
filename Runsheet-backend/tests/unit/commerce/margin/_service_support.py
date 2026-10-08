"""Shared builders for the MarginService, hook, sweep and recompute tests.

Sources are seeded on BOTH read paths: the ES fake (:class:`SweepStore`) and
the SQLite mirror (``fuel_orders_current`` hybrid rows and ``InvoiceORM``
rows). ``commerce_read_from_postgres`` then decides which one the real
``FuelOrderRepository.search`` / ``InvoiceService.list`` read, so each
dual-path test exercises the production query shape on both.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from commerce.services.invoice_service import InvoiceService
from commerce.services.margin_service import MarginService
from fuel.order_repository import FuelOrderRepository
from persistence.database import session_scope

from ._margin_fakes import CapturingTelemetry, FakeDocStore
from .conftest import TENANT_A

UTC = timezone.utc
ORDERS_INDEX = "fuel_orders_current"
INVOICES_INDEX = "invoices_current"
MONEY_KEYS = ("cents", "micros", "usd", "price", "cost", "revenue", "margin_bp")


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class SweepStore(FakeDocStore):
    """FakeDocStore plus the document calls InvoiceService and the sweep make."""

    async def search_documents(self, index: str, body: Dict[str, Any], size: int = 100, request_timeout: int = 10):
        aggs = body.get("aggs") or {}
        if "tenants" in aggs:
            self.calls.append((index, body))
            if index in self.raise_on:
                raise RuntimeError(f"store unavailable for {index}")
            tenants = sorted({d.get("tenant_id") for d in self.indices.get(index, []) if d.get("tenant_id")})
            return {
                "hits": {"total": {"value": 0}, "hits": []},
                "aggregations": {"tenants": {"buckets": [{"key": t, "doc_count": 1} for t in tenants]}},
            }
        result = await super().search_documents(index, body, size, request_timeout)
        if "max_seq" in aggs:
            from ._margin_fakes import _matches

            seqs = [
                int(d["sequence_number"])
                for d in self.indices.get(index, [])
                if _matches(d, body.get("query")) and d.get("sequence_number") is not None
            ]
            result["aggregations"] = {"max_seq": {"value": max(seqs) if seqs else None}}
        return result

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any], **kwargs: Any):
        docs = self.indices.setdefault(index, [])
        docs[:] = [d for d in docs if d.get("_doc_id") != doc_id]
        docs.append({**dict(document), "_doc_id": doc_id})
        return {"result": "created"}

    async def update_document(self, index: str, doc_id: str, partial: Dict[str, Any], **kwargs: Any):
        for doc in self.indices.get(index, []):
            if doc.get("_doc_id") == doc_id or doc.get("invoice_id") == doc_id:
                doc.update(partial)
        return {"result": "updated"}

    async def get_document(self, index: str, doc_id: str):
        for doc in self.indices.get(index, []):
            if doc.get("invoice_id") == doc_id or doc.get("order_id") == doc_id:
                return dict(doc)
        return None


def order_doc(
    order_id: str,
    *,
    created_at: datetime,
    delivered_at: Optional[datetime] = None,
    status: Optional[str] = None,
    tenant: str = TENANT_A,
    gallons: float = 1000.0,
    unit_price_micros: int = 3_000_000,
    product_code: str = "DIESEL_2",
    customer_id: str = "CUST-1",
    updated_at: Optional[datetime] = None,
    delivery_window_start: Optional[datetime] = None,
) -> Dict[str, Any]:
    """A FuelOrder-valid document (delivered when ``delivered_at`` is given)."""

    doc: Dict[str, Any] = {
        "order_id": order_id,
        "tenant_id": tenant,
        "customer_id": customer_id,
        "customer_name": "Acme",
        "ship_to_address": "1 St",
        "ship_to_lat": 40.0,
        "ship_to_lon": -75.0,
        "call_type": "will_call",
        "product_code": product_code,
        "gallons_requested": gallons,
        "unit_price_micros": unit_price_micros,
        "intake_channel": "dispatcher",
        "intake_channel_id": "ch_1",
        "status": status or ("delivered" if delivered_at else "dispatched"),
        "source_schema_version": "1.0",
        "trace_id": "t",
        "created_at": iso(created_at),
        "updated_at": iso(updated_at or delivered_at or created_at),
        "last_event_timestamp": iso(updated_at or delivered_at or created_at),
    }
    if delivery_window_start is not None:
        doc["delivery_window_start"] = iso(delivery_window_start)
    if delivered_at is not None:
        doc["delivery_result"] = {
            "pod_id": f"pod-{order_id}",
            "actual_gallons": gallons,
            "actual_gallons_source": "meter",
            "delivered_at": iso(delivered_at),
            "recipient_name": "R",
            "geotag": {"lat": 40.0, "lon": -75.0},
        }
    return doc


def invoice_doc(
    invoice_id: str,
    *,
    created_at: datetime,
    status: str = "draft",
    tenant: str = TENANT_A,
    order_id: Optional[str] = None,
    delivered_at: Optional[datetime] = None,
    finalized_at: Optional[datetime] = None,
    voided_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
    lines: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if lines is None:
        lines = [line(f"l-{invoice_id}")]
    subtotal = sum(int(li["subtotal_cents"]) for li in lines)
    return {
        "invoice_id": invoice_id,
        "tenant_id": tenant,
        "customer_id": "CUST-1",
        "account_id": "ACCT-1",
        "order_id": order_id or f"ORD-{invoice_id}",
        "status": status,
        "total_cents": subtotal,
        "subtotal_cents": subtotal,
        "tax_cents": 0,
        "amount_paid_cents": 0,
        "remaining_cents": subtotal,
        "line_items": lines,
        "delivered_at": iso(delivered_at) if delivered_at else None,
        "finalized_at": iso(finalized_at) if finalized_at else None,
        "voided_at": iso(voided_at) if voided_at else None,
        "created_at": iso(created_at),
        "updated_at": iso(updated_at or voided_at or finalized_at or created_at),
    }


def line(line_id: str, *, gallons: float = 1000.0, micros: int = 3_000_000, product: str = "DIESEL_2") -> Dict[str, Any]:
    from services.money import legacy_unit_price_cents, line_subtotal_cents

    return {
        "line_id": line_id,
        "product_code": product,
        "quantity_gallons": gallons,
        "unit_price_micros": micros,
        "unit_price_cents": legacy_unit_price_cents(micros),
        "subtotal_cents": line_subtotal_cents(gallons, micros),
    }


async def seed_order(store: SweepStore, doc: Dict[str, Any]) -> None:
    """Both read paths: the ES fake and the hybrid ``fuel_order`` mirror row."""

    from persistence.repositories import CurrentStateRepository

    store.indices.setdefault(ORDERS_INDEX, [])
    store.indices[ORDERS_INDEX] = [d for d in store.indices[ORDERS_INDEX] if d["order_id"] != doc["order_id"]]
    store.add(ORDERS_INDEX, doc)
    async with session_scope() as session:
        await CurrentStateRepository("fuel_order").upsert(session, doc=dict(doc))


async def seed_invoice(store: SweepStore, doc: Dict[str, Any]) -> None:
    """Both read paths: the ES projection and the Postgres ``InvoiceORM`` row."""

    from persistence.models import InvoiceORM
    from persistence.repositories import InvoiceRepository
    from sqlalchemy import select

    store.indices.setdefault(INVOICES_INDEX, [])
    store.indices[INVOICES_INDEX] = [d for d in store.indices[INVOICES_INDEX] if d["invoice_id"] != doc["invoice_id"]]
    store.add(INVOICES_INDEX, doc)

    def ts(key: str) -> Optional[datetime]:
        value = doc.get(key)
        return datetime.fromisoformat(value) if value else None

    async with session_scope() as session:
        row = (
            await session.execute(select(InvoiceORM).where(InvoiceORM.invoice_id == doc["invoice_id"]))
        ).scalar_one_or_none()
        if row is None:
            row = await InvoiceRepository().create(
                session,
                invoice_id=doc["invoice_id"],
                tenant_id=doc["tenant_id"],
                customer_id=doc["customer_id"],
                account_id=doc["account_id"],
                order_id=doc["order_id"],
                line_items=[dict(li) for li in doc["line_items"]],
                status=doc["status"],
            )
        row.status = doc["status"]
        row.delivered_at = ts("delivered_at")
        row.finalized_at = ts("finalized_at")
        row.voided_at = ts("voided_at")
        row.created_at = ts("created_at")
        row.updated_at = ts("updated_at")
        await session.flush()


class SpyOrders(FuelOrderRepository):
    """The real repository, recording every ``search`` call's keyword arguments."""

    def __init__(self, es_service: Any) -> None:
        super().__init__(es_service)
        self.search_calls: List[Dict[str, Any]] = []

    async def search(self, tenant_id: str, **kwargs: Any):
        self.search_calls.append({"tenant_id": tenant_id, **kwargs})
        return await super().search(tenant_id, **kwargs)


class CapturingBus:
    def __init__(self) -> None:
        self.published: List[Any] = []

    async def publish(self, message: Any) -> int:
        self.published.append(message)
        return 1


def build_service(repo: Any, store: SweepStore, **kwargs: Any) -> MarginService:
    kwargs.setdefault("orders", SpyOrders(store))
    kwargs.setdefault("invoice_service", InvoiceService(store))
    kwargs.setdefault("telemetry", CapturingTelemetry())
    return MarginService(repo, es_service=store, **kwargs)


def errors_logged(caplog, logger_name: str = "commerce.services.margin_service") -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger_name and r.levelno >= logging.ERROR]


def assert_no_money(value: Any) -> None:
    """No key in a signal/proposal payload names money (FR8.2)."""

    if isinstance(value, dict):
        for key, item in value.items():
            assert not any(token in str(key).lower() for token in MONEY_KEYS), key
            assert_no_money(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_no_money(item)
