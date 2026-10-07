"""MarginService: stage inputs, two-phase compute, voids, signals, preview, summary.

AC-13, AC-15, AC-17, freeze-9 (two-phase compute), settings_unavailable,
void needs no cost (N8), ids-only signals (FR5.2 / FR8.2), preview (AC-42),
summary stage preference and the costed/uncosted revenue split (freeze 12).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from commerce.models.margin import MarginPreviewRequest
from commerce.services import margin_cost_basis
from commerce.services.margin_repository import RecordFilters
from commerce.services.margin_service import (
    SKIP_INVALID_INPUTS,
    SKIP_NO_INPUTS,
    Inputs,
    Skip,
    WARNING_CONTRACT_SPLIT,
    extract_inputs,
)
from errors.exceptions import AppException

from ._cost_basis_support import add_entry
from ._service_support import (
    CapturingBus,
    SweepStore,
    assert_no_money,
    build_service,
    errors_logged,
    invoice_doc,
    line,
    order_doc,
)
from .conftest import TENANT_A, TENANT_B

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
DELIVERED = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
CREATED = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
LOGGER = "commerce.services.margin_service"


def _service(repo, **kwargs):
    store = SweepStore()
    kwargs.setdefault("clock", lambda: NOW)
    return build_service(repo, store, **kwargs), store


async def _override(repo, micros=2_500_000, tenant=TENANT_A):
    await add_entry(repo, tenant, "override", unit_cost_micros=micros, effective_at=datetime(2026, 1, 1, tzinfo=UTC))


async def _records(repo, tenant=TENANT_A, **filters):
    filters.setdefault("status", "all")
    return (await repo.list_records(tenant, RecordFilters(**filters), limit=200)).items


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------


def test_delivery_requires_delivered_at():
    doc = order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED)
    doc["delivery_result"]["delivered_at"] = None
    skip = extract_inputs("delivery", doc, now=NOW)
    assert isinstance(skip, Skip)
    assert (skip.reason, skip.error_type) == (SKIP_INVALID_INPUTS, "delivered_at_missing")
    del doc["delivery_result"]
    assert extract_inputs("delivery", doc, now=NOW).reason == SKIP_NO_INPUTS


def test_delivery_inputs_use_actual_gallons_and_delivered_at():
    doc = order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED, gallons=1234.5)
    inputs = extract_inputs("delivery", doc, now=NOW)
    assert isinstance(inputs, Inputs)
    assert inputs.source_key == "order:ORD-1"
    assert inputs.gallons_ugal == 1_234_500_000
    assert inputs.as_of == DELIVERED
    assert inputs.revenue_cents == 370_350  # 1234.5 x 3.000000


def test_estimate_as_of_is_window_start_clamped_to_now():
    doc = order_doc("ORD-1", created_at=CREATED, delivery_window_start=NOW + timedelta(days=2))
    assert extract_inputs("order_estimate", doc, now=NOW).as_of == NOW
    doc = order_doc("ORD-2", created_at=CREATED, delivery_window_start=CREATED + timedelta(hours=1))
    assert extract_inputs("order_estimate", doc, now=NOW).as_of == CREATED + timedelta(hours=1)
    doc = order_doc("ORD-3", created_at=CREATED)
    assert extract_inputs("order_estimate", doc, now=NOW).as_of == CREATED
    doc["unit_price_micros"] = None
    assert extract_inputs("order_estimate", doc, now=NOW).reason == SKIP_NO_INPUTS


def test_invoice_line_reads_persisted_values_only():
    lines = [line("l0", gallons=200.0, micros=2_900_000), line("l1", gallons=300.0, micros=3_200_000)]
    doc = invoice_doc("INV-1", created_at=CREATED, delivered_at=DELIVERED, lines=lines)
    first = extract_inputs("invoice", doc, line_index=0)
    second = extract_inputs("invoice", doc, line_index=1)
    assert (first.source_key, first.unit_price_micros, first.revenue_cents) == ("invoice:INV-1:line:0", 2_900_000, 58_000)
    assert (second.source_key, second.unit_price_micros, second.revenue_cents) == ("invoice:INV-1:line:1", 3_200_000, 96_000)
    assert first.as_of == DELIVERED and first.source_final is False


def test_invoice_line_legacy_cents_and_invalid_values():
    doc = invoice_doc("INV-1", created_at=CREATED, lines=[line("l0")])
    legacy = dict(doc["line_items"][0])
    legacy.pop("unit_price_micros")
    doc["line_items"] = [legacy]
    assert extract_inputs("invoice", doc, line_index=0).unit_price_micros == 3_000_000
    assert extract_inputs("invoice", doc, line_index=0).as_of == CREATED  # no delivered_at

    cases = {
        "gallons_invalid": {"quantity_gallons": "abc"},
        "gallons_not_positive": {"quantity_gallons": 0},
        "subtotal_missing": {"subtotal_cents": None},
        "price_missing": {"unit_price_micros": None, "unit_price_cents": None},
        "price_invalid": {"unit_price_micros": -5},
    }
    for error_type, change in cases.items():
        bad = dict(line("l0"), **change)
        doc["line_items"] = [bad]
        skip = extract_inputs("invoice", doc, line_index=0)
        assert isinstance(skip, Skip) and skip.reason == SKIP_INVALID_INPUTS, error_type
        assert skip.error_type == error_type


# ---------------------------------------------------------------------------
# Phase 2 and the write
# ---------------------------------------------------------------------------


async def test_live_delivery_record_money(repo, flag_on):
    service, _ = _service(repo)
    await _override(repo)
    await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    (record,) = await _records(repo)
    assert record["method"] == "override"
    assert (record["revenue_cents"], record["cost_cents"], record["margin_cents"]) == (300_000, 250_000, 50_000)
    assert record["margin_per_gallon_micros"] == 500_000
    assert record["flag_terminal_unattributed"] is True
    assert not (record["flag_missing_cost"] or record["flag_negative_margin"] or record["flag_below_floor"])
    assert record["alert_state"] == "none"
    assert record["origin"] == "live" and record["cost_snapshot"]["floor_micros_used"] == 100_000


async def test_invoice_margin_uses_persisted_resolved_price(repo, flag_on):
    """AC-13/AC-14: margin is on the persisted (resolved) line price; margin code
    never calls a pricing engine."""

    def no_pricing(tenant_id):
        raise AssertionError("margin code must not price an invoice line")

    service, _ = _service(repo, pricing_engine_factory=no_pricing)
    await _override(repo)
    resolved = line("l0", gallons=1000.0, micros=3_100_000)  # the engine changed 3.00 -> 3.10
    doc = invoice_doc("INV-1", created_at=CREATED, delivered_at=DELIVERED, lines=[resolved])
    await service._run("invoice", doc, "live")
    (record,) = await _records(repo)
    assert record["unit_price_micros"] == 3_100_000
    assert record["revenue_cents"] == resolved["subtotal_cents"] == 310_000
    assert record["margin_cents"] == 60_000


async def test_replay_keeps_one_active_record(repo, flag_on):
    """AC-17: the same event twice -> one active record, one version."""

    service, _ = _service(repo)
    await _override(repo)
    doc = order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED)
    await service._run("delivery", doc, "live")
    await service._run("delivery", doc, "live")
    records = await _records(repo)
    assert len(records) == 1 and records[0]["version"] == 1 and records[0]["status"] == "active"


async def test_invalid_inputs_write_no_record_and_one_error(repo, flag_on, caplog):
    """Freeze 9: quantity_gallons='abc' -> no record, one ERROR, one skipped row."""

    service, _ = _service(repo)
    doc = invoice_doc("INV-1", created_at=CREATED, lines=[dict(line("l0"), quantity_gallons="abc")])
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await service._run("invoice", doc, "live")
    assert await _records(repo) == []
    errors = errors_logged(caplog)
    assert len(errors) == 1 and "gallons_invalid" in errors[0].getMessage()
    assert "abc" not in errors[0].getMessage()
    skipped = await repo.skipped_sources(TENANT_A)
    assert skipped == {"count": 1, "sample": ["invoice:INV-1:line:0"]}


async def test_settings_unavailable_gives_computation_error(repo, flag_on, monkeypatch):
    service, _ = _service(repo)

    async def broken(tenant_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "get_settings", broken)
    await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    (record,) = await _records(repo)
    assert record["method"] == "none" and record["no_cost_reason"] == "computation_error"
    assert record["floor_micros_used"] == 100_000
    assert record["cost_snapshot"]["settings_unavailable"] is True
    assert record["cost_cents"] is None and record["margin_cents"] is None  # never 0
    assert record["flag_missing_cost"] is True


async def test_resolver_failure_writes_computation_error(repo, flag_on, caplog):
    service, store = _service(repo)
    store.raise_on.add("terminal_bols")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    (record,) = await _records(repo)
    assert record["method"] == "none" and record["no_cost_reason"] == "computation_error"
    assert record["cost_snapshot"]["error_type"] == "RuntimeError"
    assert record["alert_state"] == "pending"  # live delivery with missing cost
    (error,) = errors_logged(caplog)
    assert "computation_error" in error.getMessage() and "RuntimeError" in error.getMessage()


async def test_void_with_latest_row_never_calls_resolver(repo, flag_on, monkeypatch, caplog):
    """N8: voiding keeps the record's cost fields and needs no resolver read."""

    service, _ = _service(repo)
    await _override(repo)
    order = order_doc("ORD-1", created_at=CREATED)
    await service._run("order_estimate", order, "live")
    (before,) = await _records(repo)

    async def explode(*args, **kwargs):
        raise AssertionError("resolver must not be called on void")

    monkeypatch.setattr(margin_cost_basis.CostBasisResolver, "resolve", explode)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await service._run("order_estimate", dict(order, status="cancelled"), "void")
    (after,) = await _records(repo)
    assert after["record_id"] == before["record_id"] and after["status"] == "void"
    assert after["cost_cents"] == before["cost_cents"] == 250_000
    assert errors_logged(caplog) == []


async def test_void_without_record_writes_tombstones(repo, flag_on):
    service, _ = _service(repo)
    lines = [line("l0"), line("l1")]
    doc = invoice_doc("INV-1", created_at=CREATED, status="void", voided_at=NOW, lines=lines)
    await service._run("invoice", doc, "void")
    records = await _records(repo)
    assert sorted(r["line_index"] for r in records) == [0, 1]
    assert {r["status"] for r in records} == {"void"} and {r["alert_state"] for r in records} == {"none"}
    # A later live replay cannot create an active row.
    await service._run("invoice", dict(doc, status="draft"), "live")
    assert {r["status"] for r in await _records(repo)} == {"void"}


async def test_finalize_freezes_and_void_voids(repo, flag_on):
    """AC-15 at the service level (the hook test covers InvoiceService)."""

    service, _ = _service(repo)
    await _override(repo)
    draft = invoice_doc("INV-1", created_at=CREATED, delivered_at=DELIVERED)
    await service._run("invoice", draft, "live")
    await service._run("invoice", dict(draft, status="open", finalized_at=NOW.isoformat()), "finalize")
    (frozen,) = await _records(repo)
    assert frozen["frozen_at"] is not None and frozen["version"] == 1
    await service._run("invoice", dict(draft, status="void"), "void")
    (voided,) = await _records(repo)
    assert voided["status"] == "void"


# ---------------------------------------------------------------------------
# Activation memo
# ---------------------------------------------------------------------------


async def test_live_path_activates_once_and_survives_failure(repo, flag_on, monkeypatch, caplog):
    service, _ = _service(repo)
    calls = []
    real = repo.ensure_activated

    async def failing(tenant_id, **kwargs):
        calls.append(tenant_id)
        raise RuntimeError("db blip")

    monkeypatch.setattr(repo, "ensure_activated", failing)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    assert len(await _records(repo)) == 1  # the write still proceeds
    assert any("ensure_activated" in r.getMessage() for r in errors_logged(caplog))

    async def counting(tenant_id, **kwargs):
        calls.append(tenant_id)
        return await real(tenant_id, **kwargs)

    monkeypatch.setattr(repo, "ensure_activated", counting)
    for order_id in ("ORD-2", "ORD-3"):
        await service._run("delivery", order_doc(order_id, created_at=CREATED, delivered_at=DELIVERED), "live")
    assert calls == [TENANT_A, TENANT_A]  # failed once, then memoized after one success
    assert (await repo.get_settings(TENANT_A))["feed_activated_at"] is not None


# ---------------------------------------------------------------------------
# Signals (ids only)
# ---------------------------------------------------------------------------


async def test_flagged_live_record_publishes_one_ids_only_signal(repo, flag_on):
    bus = CapturingBus()
    service, _ = _service(repo, signal_bus=bus)
    await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    (record,) = await _records(repo)
    (signal,) = bus.published
    assert signal.source_agent == "margin_feed" and signal.tenant_id == TENANT_A
    assert signal.entity_type == "delivery" and signal.entity_id == record["record_id"]
    assert signal.severity.value == "medium" and signal.ttl_seconds == 86_400
    assert set(signal.context) == {"flags", "stage", "record_id", "customer_id", "product_code"}
    assert "missing_cost" in signal.context["flags"]
    assert_no_money(signal.context)


async def test_unflagged_or_estimate_records_publish_nothing(repo, flag_on):
    bus = CapturingBus()
    service, _ = _service(repo, signal_bus=bus)
    await service._run("order_estimate", order_doc("ORD-E", created_at=CREATED), "live")  # missing cost, estimate
    await _override(repo)
    await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    assert bus.published == []


async def test_negative_margin_signal_is_high(repo, flag_on):
    bus = CapturingBus()
    service, _ = _service(repo, signal_bus=bus)
    await _override(repo, micros=3_500_000)
    await service._run("delivery", order_doc("ORD-1", created_at=CREATED, delivered_at=DELIVERED), "live")
    (signal,) = bus.published
    assert signal.severity.value == "high" and signal.context["flags"] == ["negative_margin", "terminal_unattributed"]


async def test_no_bus_logs_info_once_and_publish_failure_warns(repo, flag_on, caplog):
    service, _ = _service(repo)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for order_id in ("ORD-1", "ORD-2"):
            await service._run("delivery", order_doc(order_id, created_at=CREATED, delivered_at=DELIVERED), "live")
    assert sum("no signal bus" in r.getMessage() for r in caplog.records) == 1

    class Broken:
        async def publish(self, message):
            raise RuntimeError("bus down")

    service.set_signal_bus(Broken())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await service._run("delivery", order_doc("ORD-3", created_at=CREATED, delivered_at=DELIVERED), "live")
    assert any(r.levelno == logging.WARNING and "publish failed" in r.getMessage() for r in caplog.records)
    assert len(await _records(repo)) == 3


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class _Resolution:
    def __init__(self, cents, split=None):
        self.effective_price_cents = cents
        self.effective_price_micros = None
        self.split_gallons_at_market_price = split


class _Engine:
    def __init__(self, resolution):
        self.resolution = resolution
        self.calls = []

    async def resolve_price(self, **kwargs):
        self.calls.append(kwargs)
        return self.resolution


async def test_preview_with_unit_price_persists_nothing(repo):
    bus = CapturingBus()
    service, _ = _service(repo, signal_bus=bus)
    await _override(repo)
    result = await service.preview(
        TENANT_A, MarginPreviewRequest(product_code="DIESEL_2", gallons="1000", unit_price_usd="3.000000")
    )
    assert (result["revenue_cents"], result["cost_cents"], result["margin_cents"]) == (300_000, 250_000, 50_000)
    assert result["margin_pct"] == "16.67" and result["warnings"] == []
    assert await _records(repo) == [] and bus.published == []


async def test_preview_with_customer_warns_on_contract_split(repo):
    engine = _Engine(_Resolution(290, split=300.0))
    service, _ = _service(repo, pricing_engine_factory=lambda tenant: engine)
    result = await service.preview(
        TENANT_A, MarginPreviewRequest(product_code="DIESEL_2", gallons="500", customer_id="CUST-1")
    )
    assert result["unit_price_micros"] == 2_900_000 and result["price_source"] == "pricing_engine"
    assert result["warnings"] == [WARNING_CONTRACT_SPLIT]
    assert result["method"] == "none" and result["cost_cents"] is None  # missing cost is never 0
    assert engine.calls[0]["gallons"] == 500.0 and engine.calls[0]["route_miles"] == 0.0


async def test_preview_rejects_bad_values_and_unknown_terminal(repo):
    class Terminals:
        async def get(self, tenant_id, terminal_id):
            return None

    service, _ = _service(repo, terminals=Terminals())
    with pytest.raises(AppException) as excinfo:
        await service.preview(
            TENANT_A,
            MarginPreviewRequest(
                product_code="DIESEL_2", gallons="1000", unit_price_usd="100.000001", terminal_id="T-OTHER"
            ),
        )
    assert excinfo.value.status_code == 422
    types = {e["type"] for e in excinfo.value.details["errors"]}
    assert types == {"out_of_range", "unknown_terminal"}


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


async def test_summary_stage_preference_and_revenue_split(repo, make_candidate, make_missing_cost):
    service, _ = _service(repo)
    as_of = datetime(2026, 10, 1, 17, 0, tzinfo=UTC)
    # Order 1: estimate + delivery + invoice -> only the invoice counts.
    await repo.write_record(TENANT_A, make_candidate(stage="order_estimate", source_key="order:O1", order_id="O1", as_of=as_of), "live")
    await repo.write_record(TENANT_A, make_candidate(stage="delivery", source_key="order:O1", order_id="O1", as_of=as_of), "live")
    await repo.write_record(
        TENANT_A,
        make_candidate(stage="invoice", source_key="invoice:I1:line:0", order_id="O1", invoice_id="I1", line_index=0, as_of=as_of),
        "live",
    )
    # Order 2: delivery only, missing cost.
    await repo.write_record(
        TENANT_A, make_missing_cost(stage="delivery", source_key="order:O2", order_id="O2", revenue_cents=120_000, as_of=as_of), "live"
    )
    # Order 3: estimate only.
    await repo.write_record(TENANT_A, make_candidate(stage="order_estimate", source_key="order:O3", order_id="O3", as_of=as_of), "live")
    # Another tenant's record never counts.
    await repo.write_record(TENANT_B, make_candidate(as_of=as_of), "live")
    await repo.record_skip(TENANT_A, stage="invoice", source_key="invoice:BAD:line:0", error_type="gallons_invalid")

    result = await service.summary(TENANT_A, start_date=date(2026, 10, 1), end_date=date(2026, 10, 1), group_by="day")
    totals = result["totals"]
    assert totals["records"] == 3  # invoice O1, delivery O2, estimate O3
    assert totals["revenue_cents"] == 300_000 + 120_000 + 300_000
    assert totals["revenue_cents_with_cost"] + totals["revenue_cents_missing_cost"] == totals["revenue_cents"]
    assert totals["revenue_cents_missing_cost"] == 120_000
    assert totals["cost_cents"] == 500_000 and totals["margin_cents"] == 100_000
    assert totals["missing_cost_share_bp"] == 3_333
    assert [g["key"] for g in result["groups"]] == ["2026-10-01"]  # 17:00Z is 12:00 Chicago
    for group in result["groups"]:
        assert group["revenue_cents_with_cost"] + group["revenue_cents_missing_cost"] == group["revenue_cents"]
    assert result["skipped_sources"] == {"count": 1, "sample": ["invoice:BAD:line:0"]}


async def test_summary_range_cap_and_bad_group(repo):
    service, _ = _service(repo)
    ok = await service.summary(TENANT_A, start_date=date(2026, 1, 1), end_date=date(2026, 4, 2), group_by="product")
    assert ok["totals"]["records"] == 0 and ok["totals"]["missing_cost_share_bp"] is None
    with pytest.raises(AppException) as excinfo:
        await service.summary(TENANT_A, start_date=date(2026, 1, 1), end_date=date(2026, 4, 3))
    assert excinfo.value.status_code == 422
    with pytest.raises(AppException):
        await service.summary(TENANT_A, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), group_by="week")
