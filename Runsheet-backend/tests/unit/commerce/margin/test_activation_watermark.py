"""Activation watermark: Simplification 13 (a)-(d), on both read paths.

The sweep only creates a record for a source whose hook-equivalent event
time is at or after ``feed_activated_at``; pre-activation sources are
recompute-only, so enabling the flag never floods admins with alerts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from commerce.services.margin_jobs import run_margin_gap_sweep_cycle
from commerce.services.margin_repository import RecordFilters
from config.settings import clear_settings_cache

from ._service_support import (
    SweepStore,
    build_service,
    invoice_doc,
    order_doc,
    seed_invoice,
    seed_order,
)
from .conftest import TENANT_A

UTC = timezone.utc
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _hours(n: float) -> timedelta:
    return timedelta(hours=n)


async def _records(repo, **filters):
    filters.setdefault("status", "all")
    return (await repo.list_records(TENANT_A, RecordFilters(**filters), limit=200)).items


async def _pending(repo):
    return [r for r in await _records(repo) if r["alert_state"] == "pending"]


async def _sweep(service, store, now):
    return await run_margin_gap_sweep_cycle(service, es_service=store, now=now)


async def test_a_first_sweep_writes_nothing_for_pre_activation_sources(repo, read_path, flag_on):
    store = SweepStore()
    service = build_service(repo, store, clock=lambda: NOW)
    for i in range(10):
        at = NOW - timedelta(days=1 + i % 10)  # within the 14-day order lookback
        await seed_order(store, order_doc(f"ORD-{i}", created_at=at - _hours(2), delivered_at=at))
        inv_at = NOW - _hours(2 + i)  # within the 72 h invoice lookback
        await seed_invoice(store, invoice_doc(f"INV-{i}", created_at=inv_at, delivered_at=inv_at))
    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] is None

    counts = await _sweep(service, store, NOW)

    assert await _records(repo) == []
    assert await _pending(repo) == []
    assert counts.get("written", 0) == 0
    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] == NOW


async def test_b_post_activation_lost_task_repaired_with_one_alert(repo, read_path, flag_on):
    store = SweepStore()
    activated = NOW - timedelta(days=3)
    service = build_service(repo, store, clock=lambda: NOW)
    await repo.ensure_activated(TENANT_A, now=activated)
    await seed_order(store, order_doc("ORD-PRE", created_at=activated - _hours(5), delivered_at=activated - _hours(1)))
    await seed_order(store, order_doc("ORD-POST", created_at=activated + _hours(1), delivered_at=activated + _hours(2)))

    await _sweep(service, store, NOW)
    await _sweep(service, store, NOW)  # idempotent

    records = await _records(repo)
    assert [r["order_id"] for r in records] == ["ORD-POST"]
    assert records[0]["origin"] == "live"
    assert len(await _pending(repo)) == 1  # exactly one alert's worth of work


async def test_c_flag_off_on_keeps_watermark_and_repairs_off_window(repo, read_path, monkeypatch):
    store = SweepStore()
    t0 = NOW - timedelta(days=5)
    service = build_service(repo, store, clock=lambda: NOW)
    await repo.ensure_activated(TENANT_A, now=t0)  # first enable at t0
    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "false")
    clear_settings_cache()
    # Delivered during the off window: no hook fired.
    await seed_order(store, order_doc("ORD-OFF", created_at=t0 + timedelta(days=1), delivered_at=t0 + timedelta(days=1, hours=2)))
    assert await _sweep(service, store, t0 + timedelta(days=2)) == {}

    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "true")
    clear_settings_cache()
    await _sweep(service, store, NOW)

    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] == t0
    records = await _records(repo)
    assert [r["order_id"] for r in records] == ["ORD-OFF"]
    assert records[0]["alert_state"] == "pending"  # post-activation: alerts like the hook


async def test_d_payment_touch_vs_finalize_after_activation(repo, read_path, flag_on):
    store = SweepStore()
    activated = NOW - _hours(24)
    service = build_service(repo, store, clock=lambda: NOW)
    await repo.ensure_activated(TENANT_A, now=activated)
    before = activated - _hours(10)
    # Pre-activation open invoice; a payment moves updated_at after activation.
    await seed_invoice(
        store,
        invoice_doc("INV-PAID", created_at=before, status="partial", finalized_at=before, updated_at=NOW - _hours(1)),
    )
    # Pre-activation draft finalized after activation.
    await seed_invoice(
        store,
        invoice_doc("INV-FIN", created_at=before, status="open", finalized_at=NOW - _hours(2), updated_at=NOW - _hours(2)),
    )

    await _sweep(service, store, NOW)

    records = await _records(repo)
    assert [r["invoice_id"] for r in records] == ["INV-FIN"]
    assert records[0]["frozen_at"] is not None
