"""InvoiceService margin hook: placement, isolation, backpressure (AC-14-AC-16, AC-28).

The invoice operations run end to end over a document-store fake; the margin
work runs through the real MarginService on SQLite and is awaited with
``drain`` so the assertions see the background result.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock


from commerce.services import margin_service as margin_service_module
from commerce.services.invoice_service import InvoiceService
from commerce.services.margin_repository import RecordFilters
from commerce.services.price_protection_service import PriceResolution

from ._cost_basis_support import add_entry
from ._service_support import SweepStore, build_service, errors_logged
from .conftest import TENANT_A

UTC = timezone.utc
DELIVERED_AT = "2026-10-01T15:00:00+00:00"


class SplitEngine:
    """OI-14: a 500 gal delivery against a contract with 200 gal remaining."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def resolve_price(self, **kwargs: Any) -> PriceResolution:
        self.calls.append(kwargs)
        return PriceResolution(
            effective_price_cents=290,
            contract_id="pp-1",
            contract_type="fixed_price",
            market_price_cents=320,
            split_gallons_at_contract_price=200.0,
            split_gallons_at_market_price=300.0,
        )


def _line(gallons: float = 500.0) -> Dict[str, Any]:
    return {
        "line_id": "line-1",
        "product_code": "DIESEL_2",
        "quantity_gallons": gallons,
        "unit_price_micros": 3_000_000,
        "unit_price_cents": 300,
        "subtotal_cents": int(gallons * 300),
    }


def _delivery(gallons: float = 500.0) -> Dict[str, Any]:
    return {"pod_id": "pod-1", "actual_gallons": gallons, "delivered_at": DELIVERED_AT}


async def _generate(invoice_service: InvoiceService, order_id: str = "ORD-1", gallons: float = 500.0):
    return await invoice_service.generate_from_order(
        tenant_id=TENANT_A,
        order_id=order_id,
        customer_id="CUST-1",
        account_id="ACCT-1",
        line_items=[_line(gallons)],
        delivery_result=_delivery(gallons),
    )


async def _records(repo) -> List[Dict[str, Any]]:
    page = await repo.list_records(TENANT_A, RecordFilters(status="all"), limit=50)
    return sorted(page.items, key=lambda r: (r["line_index"] if r["line_index"] is not None else -1))


def _wire(repo, *, pricing: Optional[Any] = None, **service_kwargs):
    store = SweepStore()
    invoice_service = InvoiceService(
        store, sales_pricing_engine_factory=(lambda tenant: pricing) if pricing is not None else None
    )
    service = build_service(repo, store, invoice_service=invoice_service, **service_kwargs)
    invoice_service.set_margin_hook(service.hook)
    return store, invoice_service, service


async def test_split_invoice_writes_two_line_records_without_repricing(repo, flag_on):
    """AC-14 (re-run gate): the OI-14 split yields records at line_index 0 and 1
    from the persisted split lines; margin code calls no pricing engine."""

    engine = SplitEngine()

    def margin_pricing(tenant_id):
        raise AssertionError("margin code must not call a pricing engine")

    _, invoice_service, service = _wire(repo, pricing=engine, pricing_engine_factory=margin_pricing)
    invoice = await _generate(invoice_service)
    await service.drain()
    assert len(engine.calls) == 1  # only InvoiceService priced the line
    lines = invoice["line_items"]
    assert len(lines) == 2
    records = await _records(repo)
    assert [r["line_index"] for r in records] == [0, 1]
    for record, persisted in zip(records, lines):
        assert record["source_key"] == f"invoice:{invoice['invoice_id']}:line:{record['line_index']}"
        assert record["unit_price_micros"] == persisted["unit_price_micros"]
        assert record["revenue_cents"] == persisted["subtotal_cents"]
    assert [r["unit_price_micros"] for r in records] == [2_900_000, 3_200_000]
    assert sum(r["gallons_ugal"] for r in records) == 500_000_000


async def test_finalize_freezes_and_void_voids_through_invoice_service(repo, flag_on):
    """AC-15 through the real finalize_draft / void hook placement."""

    await add_entry(repo, TENANT_A, "override", unit_cost_micros=2_500_000, effective_at=datetime(2026, 1, 1, tzinfo=UTC))
    _, invoice_service, service = _wire(repo)
    invoice = await _generate(invoice_service)
    await service.drain()
    (draft,) = await _records(repo)
    assert draft["frozen_at"] is None and draft["status"] == "active"

    await invoice_service.finalize_draft(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"])
    await service.drain()
    (frozen,) = await _records(repo)
    assert frozen["record_id"] == draft["record_id"] and frozen["frozen_at"] is not None

    await invoice_service.void(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"], reason="dup", actor="u1")
    await service.drain()
    (voided,) = await _records(repo)
    assert voided["status"] == "void" and voided["cost_cents"] == frozen["cost_cents"]


async def test_phase_two_failure_still_returns_invoice(repo, flag_on, monkeypatch, caplog):
    """AC-16: phase 2 raising -> invoice returned, computation_error record, ERROR."""

    _, invoice_service, service = _wire(repo)

    async def boom(*args, **kwargs):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(service, "compute", boom)
    with caplog.at_level(logging.INFO):
        invoice = await _generate(invoice_service)
        await service.drain()
    assert invoice["status"] == "draft"
    (record,) = await _records(repo)
    assert record["method"] == "none" and record["no_cost_reason"] == "computation_error"
    assert record["flag_missing_cost"] is True and record["cost_cents"] is None
    assert any("computation_error" in r.getMessage() for r in errors_logged(caplog))


async def test_scheduling_failure_still_returns_invoice(repo, flag_on, monkeypatch, caplog):
    _, invoice_service, service = _wire(repo)

    def broken_schedule(*args, **kwargs):
        raise RuntimeError("loop gone")

    monkeypatch.setattr(service, "schedule", broken_schedule)
    with caplog.at_level(logging.INFO):
        invoice = await _generate(invoice_service)
        finalized = await invoice_service.finalize_draft(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"])
    assert finalized["status"] == "open"
    assert sum("margin hook failed" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR) == 2
    assert await _records(repo) == []


async def test_raising_hook_object_never_fails_invoice(repo, flag_on, caplog):
    """InvoiceService._notify_margin guards even a hook that raises itself."""

    store = SweepStore()
    invoice_service = InvoiceService(store)
    hook = MagicMock()
    hook.invoice_generated.side_effect = RuntimeError("bad hook")
    hook.invoice_finalized.side_effect = RuntimeError("bad hook")
    hook.invoice_voided.side_effect = RuntimeError("bad hook")
    invoice_service.set_margin_hook(hook)
    invoice = await _generate(invoice_service)
    await invoice_service.finalize_draft(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"])
    voided = await invoice_service.void(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"], reason="r", actor="u")
    assert voided["status"] == "void"
    assert hook.invoice_generated.call_count == 1
    assert hook.invoice_finalized.call_args.args[0]["status"] == "open"
    assert hook.invoice_voided.call_args.args[0]["status"] == "void"


async def test_idempotent_early_return_does_not_call_hook(repo, flag_on):
    store = SweepStore()
    idempotency = MagicMock()

    async def is_duplicate(key, tenant):
        return True

    idempotency.is_duplicate = is_duplicate
    invoice_service = InvoiceService(store, idempotency_service=idempotency)
    store.add("invoices_current", {"invoice_id": "INV-OLD", "tenant_id": TENANT_A, "order_id": "ORD-1", "status": "open"})
    hook = MagicMock()
    invoice_service.set_margin_hook(hook)
    existing = await _generate(invoice_service)
    assert existing["invoice_id"] == "INV-OLD"
    hook.invoice_generated.assert_not_called()


async def test_mutation_after_hook_returns_does_not_leak(repo, flag_on):
    """The hook deep-copies at call time."""

    _, invoice_service, service = _wire(repo)
    invoice = await _generate(invoice_service)
    invoice["line_items"][0]["subtotal_cents"] = 1  # caller mutates after the hook returned
    invoice["line_items"][0]["quantity_gallons"] = 1.0
    await service.drain()
    (record,) = await _records(repo)
    assert record["revenue_cents"] == 150_000 and record["gallons_ugal"] == 500_000_000


async def test_backpressure_schedules_nothing_and_warns(repo, flag_on, caplog):
    _, invoice_service, service = _wire(repo)
    loop = asyncio.get_running_loop()
    blockers = [loop.create_future() for _ in range(margin_service_module.MARGIN_HOOK_MAX_PENDING_TASKS)]
    service._tasks.update(blockers)
    try:
        with caplog.at_level(logging.INFO):
            invoice = await _generate(invoice_service)
        assert invoice["status"] == "draft"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "backpressure" in r.getMessage()]
        assert len(warnings) == 1
        assert TENANT_A in warnings[0].getMessage() and invoice["invoice_id"] in warnings[0].getMessage()
        assert service.pending_tasks == margin_service_module.MARGIN_HOOK_MAX_PENDING_TASKS
    finally:
        service._tasks.difference_update(blockers)
        for future in blockers:
            future.cancel()
    await service.drain()
    assert await _records(repo) == []


async def test_flag_off_hooks_write_nothing(repo, monkeypatch):
    """AC-28: with the flag off the hook returns before scheduling."""

    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "false")
    from config.settings import clear_settings_cache

    clear_settings_cache()
    _, invoice_service, service = _wire(repo)
    invoice = await _generate(invoice_service)
    await invoice_service.finalize_draft(tenant_id=TENANT_A, invoice_id=invoice["invoice_id"])
    assert service.pending_tasks == 0
    await service.drain()
    assert await _records(repo) == []
    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] is None


async def test_persistence_off_hooks_write_nothing(repo, flag_on, monkeypatch):
    monkeypatch.setattr("persistence.database.is_persistence_enabled", lambda: False)
    _, invoice_service, service = _wire(repo)
    await _generate(invoice_service)
    assert service.pending_tasks == 0


async def test_drain_cancels_tasks_past_timeout(repo, flag_on, caplog):
    _, _, service = _wire(repo)
    started = asyncio.Event()

    async def slow(stage, source, mode):
        started.set()
        await asyncio.sleep(30)

    service._run = slow  # type: ignore[assignment]
    assert service.schedule("invoice", {"tenant_id": TENANT_A, "invoice_id": "I"}, "live")
    await started.wait()
    with caplog.at_level(logging.WARNING):
        cancelled = await service.drain(timeout=0.05)
    assert cancelled == 1 and service.pending_tasks == 0
    assert any("cancelled 1" in r.getMessage() for r in caplog.records)
