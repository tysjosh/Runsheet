"""Margin data never reaches a non-admin surface (FR8, AC-31).

1. Import graph (``ast``): only the allowed modules import
   ``margin_repository`` or a margin ORM class.
2. Field scan: forbidden keys are DERIVED from the ``MarginRecordORM`` and
   ``MarginCostEntryORM`` columns plus the export's derived names, minus the
   shared ids that legitimately appear elsewhere. Every existing surface is
   scanned on a tenant that HAS margin records: order and invoice GET/list
   responses, the stored order/invoice/BOL documents, the invoice export
   header (and the other exports'), the ``_broadcast_invoice_ws`` payload,
   the driver-app order payload, ``_build_qbo_push_payload`` and the
   notification dict handed to a channel ``dispatch``; the job and BOL
   response models are scanned by field name.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, get_args, get_origin
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from commerce.api import invoice_endpoints
from commerce.services.commerce_external_sync import CommerceExternalSync
from commerce.services.invoice_service import InvoiceService
from commerce.services.margin_repository import RecordFilters
from errors.handlers import register_exception_handlers
from fuel.api import order_endpoints
from fuel.order_repository import FuelOrderRepository
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from persistence.database import Base
from persistence.models import MarginCostEntryORM, MarginRecordORM

from ._cost_basis_support import add_entry
from ._service_support import ORDERS_INDEX, SweepStore, build_service, order_doc, seed_order
from .conftest import TENANT_A

UTC = timezone.utc
BACKEND = Path(__file__).resolve().parents[4]
DRIVER = "DRV-1"

# ---------------------------------------------------------------------------
# Forbidden keys (derived, not hand-listed)
# ---------------------------------------------------------------------------

#: Export-derived names (design "Non-exposure"). The generic ``flags`` column
#: is left out on purpose: existing docs may use it.
EXPORT_DERIVED = {"cost_usd", "margin_usd", "landed_cost_usd", "margin_per_gallon_usd", "margin_pct"}

#: Shared ids and fields that legitimately appear on existing surfaces (design list).
SHARED = {
    "tenant_id", "order_id", "invoice_id", "customer_id", "account_id", "product_code",
    "terminal_id", "unit_price_micros", "revenue_cents", "status", "version", "line_index",
    "line_id", "created_at", "created_by", "notes", "reference", "supplier_name", "bol_id",
    "effective_at", "effective_to", "source", "stage",
}
#: Column names that also name an unrelated field on an existing surface.
#: ``margin_records.origin`` is live/recompute; ``Job.origin`` (and the jobs
#: export column) is the pickup location. Neither carries a cost or margin.
GENERIC = {"origin"}

MINIMUM = {
    "unit_cost_micros", "cost_cents", "margin_cents", "landed_cost_micros", "floor_micros_used",
    "flag_negative_margin", "flag_missing_cost", "flag_below_floor", "cost_snapshot",
}


def forbidden_keys() -> Set[str]:
    columns = {c.name for c in MarginRecordORM.__table__.columns}
    columns |= {c.name for c in MarginCostEntryORM.__table__.columns}
    return (columns | EXPORT_DERIVED) - SHARED - GENERIC


FORBIDDEN = forbidden_keys()


def test_forbidden_set_keeps_the_money_and_flag_columns():
    """A future column rename cannot silently empty the scan."""
    assert MINIMUM <= FORBIDDEN
    assert EXPORT_DERIVED <= FORBIDDEN
    assert not FORBIDDEN & SHARED


def test_the_scan_finds_a_nested_margin_key():
    with pytest.raises(AssertionError, match="cost_cents"):
        assert_clean("probe", {"data": [{"line_items": [{"cost_cents": 1}]}]})
    assert_clean("probe", {"data": {"order_id": "O", "unit_price_micros": 1, "origin": "Depot"}})


def keys_in(value: Any) -> Set[str]:
    found: Set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.add(str(key))
            found |= keys_in(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found |= keys_in(item)
    return found


def assert_clean(surface: str, value: Any) -> None:
    leaked = keys_in(value) & FORBIDDEN
    assert not leaked, f"{surface} exposes margin keys {sorted(leaked)}"


def model_field_names(model: type, seen: Optional[Set[type]] = None) -> Set[str]:
    """Field names of a pydantic model and every model nested in its annotations."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names: Set[str] = set()
    for name, field in model.model_fields.items():
        names.add(field.alias or name)
        stack = [field.annotation]
        while stack:
            ann = stack.pop()
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                names |= model_field_names(ann, seen)
            elif get_origin(ann) is not None:
                stack.extend(get_args(ann))
    return names


# ---------------------------------------------------------------------------
# 1. Import graph
# ---------------------------------------------------------------------------

MARGIN_ORM_NAMES = {
    mapper.class_.__name__
    for mapper in Base.registry.mappers
    if str(getattr(mapper.class_, "__tablename__", "")).startswith("margin_")
}
_SKIP_DIRS = {"venv", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".hypothesis"}


def _allowed(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        rel.startswith("tests/")
        or rel.startswith("alembic/")
        or rel.startswith("bootstrap/")
        or rel == "persistence/models.py"
        or rel == "commerce/api/margin_endpoints.py"
        or rel == "commerce/hooks/margin_order_subscriber.py"
        or rel == "Agents/overlay/revenue_guard.py"
        or (rel.startswith("commerce/services/") and len(parts) == 3 and name.startswith("margin_"))
    )


def _module_of(rel: str) -> List[str]:
    return rel[:-3].split("/")


def _resolve(rel: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = _module_of(rel)[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *(node.module.split(".") if node.module else [])])


def margin_imports(rel: str, tree: ast.AST) -> List[str]:
    hits: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("margin_repository"):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(rel, node)
            for alias in node.names:
                if module.endswith("margin_repository") or alias.name == "margin_repository":
                    hits.append(f"{module}.{alias.name}")
                elif module == "persistence.models" and (alias.name in MARGIN_ORM_NAMES or alias.name == "*"):
                    hits.append(f"{module}.{alias.name}")
    return hits


def _python_files() -> Iterable[Path]:
    for path in BACKEND.rglob("*.py"):
        if _SKIP_DIRS & set(path.relative_to(BACKEND).parts):
            continue
        yield path


def test_margin_orm_names_cover_the_seven_tables():
    assert {"MarginRecordORM", "MarginCostEntryORM", "MarginAlertORM", "MarginSettingsORM",
            "MarginRecomputeRunORM", "MarginWeeklyReportORM", "MarginSkippedSourceORM"} == MARGIN_ORM_NAMES


def test_only_allowed_modules_import_the_margin_repository_or_orm():
    violations: Dict[str, List[str]] = {}
    importers: Set[str] = set()
    for path in _python_files():
        rel = path.relative_to(BACKEND).as_posix()
        hits = margin_imports(rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel))
        if hits:
            importers.add(rel)
            if not _allowed(rel):
                violations[rel] = hits
    assert violations == {}
    # The scan is live: the known importers are found.
    assert {"commerce/services/margin_service.py", "commerce/api/margin_endpoints.py"} <= importers


def test_import_graph_detects_a_violation():
    """The checker itself flags an import from a non-allowed module."""
    tree = ast.parse(
        "from commerce.services.margin_repository import MarginRepository\n"
        "from persistence.models import MarginRecordORM\n"
        "from ..services import margin_repository\n"
    )
    hits = margin_imports("fuel/api/order_endpoints.py", tree)
    assert len(hits) == 3
    assert not _allowed("fuel/api/order_endpoints.py")
    assert _allowed("commerce/services/margin_jobs.py")
    assert not _allowed("commerce/services/invoice_service.py")


# ---------------------------------------------------------------------------
# 2. Field scan on a tenant that has margin records
# ---------------------------------------------------------------------------


class CapturingInvoiceWs:
    def __init__(self) -> None:
        self.payloads: List[Dict[str, Any]] = []

    async def broadcast_invoice_update(self, invoice_doc: Dict[str, Any]) -> int:
        self.payloads.append(invoice_doc)
        return 1


class CapturingDispatcher:
    channel_name = "sms"

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def dispatch(self, notification: dict) -> str:
        self.sent.append(notification)
        return "sent"


def _line(gallons: float = 1000.0) -> Dict[str, Any]:
    return {
        "line_id": "line-1",
        "product_code": "DIESEL_2",
        "quantity_gallons": gallons,
        "unit_price_micros": 3_000_000,
        "unit_price_cents": 300,
        "subtotal_cents": int(gallons * 300),
    }


@pytest.fixture
async def world(repo, flag_on):
    """Orders, an invoice and margin records (invoice + delivery) for TENANT_A."""
    await add_entry(
        repo, TENANT_A, "override", unit_cost_micros=3_200_000,  # negative margin
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    store = SweepStore()
    ws = CapturingInvoiceWs()
    invoice_service = InvoiceService(store, invoice_ws_manager=ws)
    service = build_service(repo, store, invoice_service=invoice_service)
    invoice_service.set_margin_hook(service.hook)

    delivered = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    order = order_doc("ORD-1", created_at=datetime(2026, 9, 30, tzinfo=UTC), delivered_at=delivered)
    order["assigned_driver_id"] = DRIVER
    await seed_order(store, order)
    service.hook.order_event(dict(order), "order.delivered")

    invoice = await invoice_service.generate_from_order(
        tenant_id=TENANT_A,
        order_id="ORD-1",
        customer_id="CUST-1",
        account_id="ACCT-1",
        line_items=[_line()],
        delivery_result={"pod_id": "pod-1", "actual_gallons": 1000.0, "delivered_at": delivered.isoformat()},
    )
    await service.drain()
    await invoice_service.finalize_draft(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"])
    await service.drain()

    records = (await repo.list_records(TENANT_A, RecordFilters(status="all"), limit=50)).items
    assert {r["stage"] for r in records} >= {"delivery", "invoice"}
    assert any(r["flag_negative_margin"] for r in records)  # margin data really exists
    store.add("terminal_bols", {
        "bol_id": "BOL-1", "tenant_id": TENANT_A, "terminal_id": "TERM-1", "product_code": "DIESEL_2",
        "net_gallons": 1000.0,
    })
    return {
        "store": store, "ws": ws, "invoice_service": invoice_service, "service": service,
        "invoice_id": invoice["invoice_id"], "order": order,
    }


def _client(*routers: Any) -> httpx.AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=TENANT_A, user_id="u-disp", has_pii_access=True, roles=["dispatcher"]
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_stored_documents_carry_no_margin_keys(world):
    for index, docs in world["store"].indices.items():
        for doc in docs:
            assert_clean(f"stored {index} doc", doc)


async def test_invoice_get_list_ws_qbo_and_export_header(world, monkeypatch):
    monkeypatch.setattr(invoice_endpoints, "_invoice_service", world["invoice_service"])
    invoice_id = world["invoice_id"]
    async with _client(invoice_endpoints.router) as c:
        got = await c.get(f"/api/commerce/invoices/{invoice_id}")
        listed = await c.get("/api/commerce/invoices")
    assert got.status_code == 200, got.text
    assert listed.status_code == 200, listed.text
    assert got.json()["data"]["invoice_id"] == invoice_id
    assert_clean("GET /api/commerce/invoices/{id}", got.json())
    assert_clean("GET /api/commerce/invoices", listed.json())

    assert world["ws"].payloads, "finalize_draft broadcast nothing"
    for payload in world["ws"].payloads:
        assert_clean("_broadcast_invoice_ws payload", payload)

    invoice = await world["invoice_service"].get(tenant_id=TENANT_A, invoice_id=invoice_id)
    sync = CommerceExternalSync(qbo_connector=None, stripe_connector=None,
                                invoice_service=None, payment_service=None)
    assert_clean("_build_qbo_push_payload", sync._build_qbo_push_payload(invoice))

    assert_clean("invoice export header", {c.header: None for c in invoice_endpoints._INVOICE_EXPORT_COLUMNS})


async def test_other_export_headers_carry_no_margin_columns():
    from compliance.api import ifta_endpoints
    from fuel.api import fuel_ops_endpoints
    from scheduling.api import endpoints as scheduling_endpoints

    for name, columns in {
        "orders": order_endpoints._ORDER_EXPORT_COLUMNS,
        "jobs": scheduling_endpoints._JOB_EXPORT_COLUMNS,
        "reconciliation": fuel_ops_endpoints._RECONCILIATION_EXPORT_COLUMNS,
        "ifta": ifta_endpoints._IFTA_EXPORT_COLUMNS,
    }.items():
        assert_clean(f"{name} export header", {c.header: None for c in columns})


async def test_order_get_and_list(world, monkeypatch):
    repository = FuelOrderRepository(world["store"])
    monkeypatch.setattr(order_endpoints, "_order_repository", repository)
    async with _client(order_endpoints.router) as c:
        got = await c.get("/api/orders/ORD-1")
        listed = await c.get("/api/orders")
    assert got.status_code == 200, got.text
    assert listed.status_code == 200, listed.text
    assert got.json()["order_id"] == "ORD-1"
    assert_clean("GET /api/orders/{id}", got.json())
    assert_clean("GET /api/orders", listed.json())


async def test_driver_app_order_payload(world):
    from driver.services.work_service import DriverWorkService

    store = world["store"]
    work = DriverWorkService(
        es_service=store, order_repository=FuelOrderRepository(store), job_service=None
    )
    payload = await work.get_work(TENANT_A, DRIVER, "ORD-1", has_pii_access=True)
    assert payload["data"]["order_id"] == "ORD-1"
    assert_clean("driver-app GET /work/{order_id}", payload)


async def test_notification_dispatch_payload(world):
    from notifications.services.notification_service import NotificationService

    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    service = NotificationService(es_service=es)
    service._rule_engine.evaluate_rule = AsyncMock(return_value={
        "rule_id": "r1", "event_type": "past_due_invoice", "enabled": True, "default_channels": ["sms"],
    })
    service._preference_resolver.resolve_channels = AsyncMock(return_value=[])
    service._template_renderer.list_templates = AsyncMock(return_value=[{"template_id": "t1"}])

    async def render(template_id, event_data, tenant_id):
        return {"subject": "Invoice", "body": " ".join(f"{k}={v}" for k, v in sorted(event_data.items()))}

    service._template_renderer.render = AsyncMock(side_effect=render)
    dispatcher = CapturingDispatcher()
    service.register_dispatcher("sms", dispatcher)

    invoice = await world["invoice_service"].get(tenant_id=TENANT_A, invoice_id=world["invoice_id"])
    event = {k: v for k, v in invoice.items() if not isinstance(v, (dict, list))}
    await service.notify_event("past_due_invoice", event, TENANT_A)
    assert dispatcher.sent, "the capturing dispatcher received nothing"
    for notification in dispatcher.sent:
        assert_clean("notification dispatch", notification)
        assert not any(k in str(notification.get("message_body", "")) for k in ("cost_cents", "margin_cents"))


def test_job_bol_and_order_response_models_have_no_margin_fields():
    from compliance.models.terminal_bol import TerminalBOL
    from scheduling.models import Job

    for name, model in {
        "Job": Job,
        "TerminalBOL": TerminalBOL,
        "OrderResponse": order_endpoints.OrderResponse,
        "OrderListResponse": order_endpoints.OrderListResponse,
    }.items():
        leaked = model_field_names(model) & FORBIDDEN
        assert not leaked, f"{name} has margin fields {sorted(leaked)}"


async def test_stored_order_doc_unchanged_by_margin_processing(world):
    """The hook reads a deepcopy; the order doc in the store gains no key."""
    stored = next(d for d in world["store"].indices[ORDERS_INDEX] if d["order_id"] == "ORD-1")
    assert set(stored) == set(world["order"])
