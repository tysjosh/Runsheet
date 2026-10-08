"""Gap sweep: state reconciliation on both read paths (Simplification 10).

Every test runs twice: ``commerce_read_from_postgres=False`` (ES fake) and
``True`` (SQLite mirror), so the ``updated_from`` invoice filter and the order
search are proven on both read paths. Activation-watermark cases are in
``test_activation_watermark.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone


from commerce.services import margin_jobs
from commerce.services.margin_jobs import run_margin_gap_sweep_cycle
from commerce.services.margin_repository import RecordFilters

from ._service_support import (
    INVOICES_INDEX,
    ORDERS_INDEX,
    SweepStore,
    build_service,
    invoice_doc,
    line,
    order_doc,
    seed_invoice,
    seed_order,
)
from .conftest import TENANT_A, TENANT_B

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _hours(n: float) -> timedelta:
    return timedelta(hours=n)


def _setup(repo, now):
    store = SweepStore()
    return store, build_service(repo, store, clock=lambda: now)


async def _sweep(service, store, now, **kwargs):
    return await run_margin_gap_sweep_cycle(service, es_service=store, now=now, **kwargs)


async def _records(repo, tenant=TENANT_A, **filters):
    filters.setdefault("status", "all")
    return (await repo.list_records(tenant, RecordFilters(**filters), limit=200)).items


def _assert_read_path(store, read_path):
    """On the Postgres path no source read went to the document store."""

    if read_path:
        for index in (INVOICES_INDEX, ORDERS_INDEX):
            assert [b for b in store.calls_to(index) if "aggs" not in b] == [], index


async def test_lost_finalize_is_frozen(repo, read_path, flag_on):
    now = T0 + _hours(3)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=T0)
    draft = invoice_doc("INV-1", created_at=T0 + _hours(1), delivered_at=T0 + _hours(1))
    await service._run("invoice", draft, "live")  # the live draft record
    (before,) = await _records(repo)
    await seed_invoice(store, dict(draft, status="open", finalized_at=(T0 + _hours(2)).isoformat(), updated_at=(T0 + _hours(2)).isoformat()))

    counts = await _sweep(service, store, now)

    (after,) = await _records(repo)
    assert after["record_id"] == before["record_id"] and after["frozen_at"] is not None
    assert counts["written"] == 1
    _assert_read_path(store, read_path)


async def test_lost_void_is_voided(repo, read_path, flag_on):
    now = T0 + _hours(3)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=T0)
    draft = invoice_doc("INV-1", created_at=T0 + _hours(1))
    await service._run("invoice", draft, "live")
    await seed_invoice(store, dict(draft, status="void", voided_at=(T0 + _hours(2)).isoformat(), updated_at=(T0 + _hours(2)).isoformat()))

    await _sweep(service, store, now)

    (record,) = await _records(repo)
    assert record["status"] == "void"
    _assert_read_path(store, read_path)


async def test_invoice_created_5_days_ago_finalized_1_hour_ago_is_frozen(repo, read_path, flag_on):
    now = T0
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=now - timedelta(days=10))
    created = now - timedelta(days=5)
    draft = invoice_doc("INV-1", created_at=created)
    await service._run("invoice", draft, "live")
    await seed_invoice(store, dict(draft, status="open", finalized_at=(now - _hours(1)).isoformat(), updated_at=(now - _hours(1)).isoformat()))

    await _sweep(service, store, now)

    (record,) = await _records(repo)
    assert record["frozen_at"] is not None
    _assert_read_path(store, read_path)


async def test_updated_from_lookback_selects_invoices_on_both_paths(repo, read_path, flag_on):
    """Only invoices with updated_at >= max(now - 72h, activated_at) are read."""

    now = T0
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=now - timedelta(days=10))
    created = now - timedelta(days=5)
    await seed_invoice(store, invoice_doc("INV-STALE", created_at=created, updated_at=now - timedelta(hours=73)))
    await seed_invoice(store, invoice_doc("INV-FRESH", created_at=created, updated_at=now - _hours(1)))

    await _sweep(service, store, now)

    assert [r["invoice_id"] for r in await _records(repo)] == ["INV-FRESH"]
    _assert_read_path(store, read_path)


async def test_missing_line_key_of_void_invoice_becomes_void_tombstone(repo, read_path, flag_on):
    now = T0 + _hours(5)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=T0)
    await seed_invoice(
        store,
        invoice_doc("INV-1", created_at=T0 + _hours(1), status="void", voided_at=T0 + _hours(2), lines=[line("a"), line("b")]),
    )

    await _sweep(service, store, now)

    records = await _records(repo)
    assert sorted(r["line_index"] for r in records) == [0, 1]
    assert {r["status"] for r in records} == {"void"} and {r["alert_state"] for r in records} == {"none"}


async def test_cancelled_order_estimate_is_voided(repo, read_path, flag_on):
    now = T0 + timedelta(days=2)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=T0)
    dispatched = order_doc("ORD-1", created_at=T0 + _hours(1))
    await service._run("order_estimate", dispatched, "live")
    await seed_order(store, dict(dispatched, status="cancelled", updated_at=(T0 + _hours(5)).isoformat()))
    await seed_order(store, dict(order_doc("ORD-2", created_at=T0 + _hours(1)), status="failed"))  # no estimate

    await _sweep(service, store, now)

    records = await _records(repo, stage="order_estimate")
    assert [(r["order_id"], r["status"]) for r in records] == [("ORD-1", "void")]
    _assert_read_path(store, read_path)


async def test_ensure_activated_failure_skips_tenant_and_next_runs(repo, read_path, flag_on, monkeypatch, caplog):
    now = T0 + _hours(6)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_B, now=T0)
    await seed_order(store, order_doc("ORD-A", tenant=TENANT_A, created_at=T0 + _hours(1), delivered_at=T0 + _hours(2)))
    await seed_order(store, order_doc("ORD-B", tenant=TENANT_B, created_at=T0 + _hours(1), delivered_at=T0 + _hours(2)))
    real = repo.ensure_activated

    async def flaky(tenant_id, **kwargs):
        if tenant_id == TENANT_A:
            raise RuntimeError("db blip")
        return await real(tenant_id, **kwargs)

    monkeypatch.setattr(repo, "ensure_activated", flaky)
    with caplog.at_level(logging.ERROR):
        counts = await _sweep(service, store, now)

    assert counts["tenants_failed"] == 1 and counts["tenants"] == 1
    assert await _records(repo, TENANT_A) == []
    assert [r["order_id"] for r in await _records(repo, TENANT_B)] == ["ORD-B"]
    assert any("ensure_activated failed" in r.getMessage() for r in caplog.records)


async def test_invalid_inputs_swept_twice_warns_twice_and_writes_nothing(repo, read_path, flag_on, caplog):
    """Freeze 9: two sweeps over an invalid line -> two WARNING counts, no record."""

    now = T0 + _hours(4)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=T0)
    await seed_invoice(store, invoice_doc("INV-1", created_at=T0 + _hours(1), lines=[line("l0", gallons=0.0)]))

    with caplog.at_level(logging.WARNING, logger="commerce.services.margin_jobs"):
        first = await _sweep(service, store, now)
        second = await _sweep(service, store, now)

    assert first["invalid_inputs"] == 1 and second["invalid_inputs"] == 1
    warnings = [r for r in caplog.records if "invalid_inputs" in r.getMessage()]
    assert len(warnings) == 2 and all(r.levelno == logging.WARNING for r in warnings)
    assert await _records(repo) == []
    assert (await repo.skipped_sources(TENANT_A))["count"] == 1


async def test_source_cap_stops_the_tenant_pass(repo, read_path, flag_on, monkeypatch, caplog):
    now = T0 + _hours(6)
    store, service = _setup(repo, now)
    await repo.ensure_activated(TENANT_A, now=T0)
    for i in range(3):
        await seed_order(store, order_doc(f"ORD-{i}", created_at=T0 + _hours(1), delivered_at=T0 + _hours(2)))
    monkeypatch.setattr(margin_jobs, "SWEEP_SOURCE_CAP", 2)
    with caplog.at_level(logging.WARNING, logger="commerce.services.margin_jobs"):
        counts = await _sweep(service, store, now)
    assert counts["written"] == 2 and len(await _records(repo)) == 2
    assert any("source cap" in r.getMessage() for r in caplog.records)


async def test_flag_off_returns_before_any_query(repo, monkeypatch):
    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "false")
    from config.settings import clear_settings_cache

    clear_settings_cache()
    store, service = _setup(repo, T0)
    store.add(INVOICES_INDEX, invoice_doc("INV-1", created_at=T0))
    assert await _sweep(service, store, T0) == {}
    assert store.calls == []
    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] is None


async def test_tenant_discovery_uses_both_indices():
    store = SweepStore()
    store.add(INVOICES_INDEX, {"tenant_id": TENANT_A, "invoice_id": "I"})
    store.add(ORDERS_INDEX, {"tenant_id": TENANT_B, "order_id": "O"})
    assert await margin_jobs.discover_sweep_tenants(store) == [TENANT_A, TENANT_B]
    store.raise_on.add(INVOICES_INDEX)
    assert await margin_jobs.discover_sweep_tenants(store) == [TENANT_B]
