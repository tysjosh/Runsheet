"""bootstrap/core.py margin wiring (tasks.md item 20)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from bootstrap.container import ServiceContainer
from bootstrap.core import wire_margin_feed
from commerce.services.margin_jobs import run_margin_gap_sweep_cycle, run_margin_weekly_report_cycle

from ._service_support import CapturingBus, SweepStore
from .test_margin_order_subscriber import FakeOrderService


def test_wire_margin_feed_sets_hook_subscribers_and_container():
    container = ServiceContainer()
    invoice_service = MagicMock()
    container.commerce_invoice_service = invoice_service
    container.order_service = FakeOrderService()

    service = wire_margin_feed(container, SweepStore())

    invoice_service.set_margin_hook.assert_called_once_with(service.hook)
    assert container.margin_service is service
    assert container.margin_repository is service.repository
    assert container.has("margin_cost_entry_service")
    assert service.invoice_service is invoice_service
    assert sorted(container.order_service.subscribers) == [
        "order.cancelled", "order.delivered", "order.dispatched", "order.failed"
    ]
    assert len(container.margin_order_subscribers) == 4
    # The signal bus is created by bootstrap/agents after core: resolved lazily.
    assert service._signal_bus() is None
    bus = CapturingBus()
    container.signal_bus = bus
    assert service._signal_bus() is bus


def test_without_order_service_core_leaves_subscription_to_fuel():
    container = ServiceContainer()
    wire_margin_feed(container, SweepStore())
    assert container.has("margin_service")
    assert not container.has("margin_order_subscribers")  # bootstrap/fuel late-binds it


async def test_flag_off_jobs_touch_no_store(margin_engine, monkeypatch):
    """With the flag off both jobs return before any query."""

    monkeypatch.setenv("COMMERCE_MARGIN_FEED_ENABLED", "false")
    monkeypatch.setenv("COMMERCE_BACKBONE_ENABLED", "true")
    from config.settings import clear_settings_cache

    clear_settings_cache()
    store = SweepStore()
    container = ServiceContainer()
    service = wire_margin_feed(container, store)

    class Exploding:
        async def tenants_with_records(self):
            raise AssertionError("no query")

    monkeypatch.setattr(service.repository, "discovery", Exploding())
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert await run_margin_gap_sweep_cycle(service, es_service=store, now=now) == {}
    assert await run_margin_weekly_report_cycle(service, now=now) == 0
    assert store.calls == []
