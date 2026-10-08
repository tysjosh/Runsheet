"""MERGE-BLOCKING: the recompute end-day boundary (design "Design freeze
(2026-10-07)", Simplification 13 (f)-(g)).

Settings timezone ``America/Chicago``. Every test runs on BOTH read paths
through the real ``FuelOrderRepository.search`` filter:

* ``commerce_read_from_postgres=False``: the ES fake, which reads a bare-date
  ``range.created_at.lte`` as ``00:00:00`` UTC;
* ``commerce_read_from_postgres=True``: ``read_hybrid_search`` over the
  SQLite mirror, which compares ``created_at`` strings lexically.

Order A (created and delivered 10:00Z on ``end``) and order B (created
23:00 and delivered 23:30 Chicago on ``end``, both on the next UTC date) are
recomputed. Order C (delivered 00:30 Chicago on ``end + 1``) is enumerated,
not written, and counted in ``counts.out_of_range``. The repository call
received ``end_date == (end + 2 days).isoformat()``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


from commerce.services.margin_jobs import run_margin_recompute
from commerce.services.margin_repository import RecordFilters

from ._service_support import (
    SpyOrders,
    SweepStore,
    build_service,
    invoice_doc,
    line,
    order_doc,
    seed_invoice,
    seed_order,
)
from .conftest import TENANT_A

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")


def chicago(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=CHICAGO).astimezone(UTC)


async def _recompute(service, start: date, end: date, *, stages=("delivery",), now=None):
    run = await service.repository.start_run(
        TENANT_A,
        requested_by="admin-1",
        start_date=start,
        end_date=end,
        stages=list(stages),
        only_missing=True,
        reason="boundary test",
    )
    counts = await run_margin_recompute(
        service,
        TENANT_A,
        run["run_id"],
        start_date=start,
        end_date=end,
        stages=list(stages),
        only_missing=True,
        actor="admin-1",
        now=now,
    )
    return run["run_id"], counts


async def _written_orders(repo, stage: str = "delivery") -> set:
    page = await repo.list_records(TENANT_A, RecordFilters(stage=stage), limit=200)
    return {r["order_id"] for r in page.items}


async def _setup(repo, read_path_value):
    store = SweepStore()
    orders = SpyOrders(store)
    service = build_service(repo, store, orders=orders)
    await repo.put_settings(TENANT_A, {"timezone": "America/Chicago"}, actor="test")
    return store, orders, service


async def test_freeze_required_boundary_both_paths(repo, read_path, flag_on):
    """The freeze's required test, verbatim (A, B written; C out_of_range)."""

    store, orders, service = await _setup(repo, read_path)
    start, end = date(2026, 10, 14), date(2026, 10, 20)  # CDT, UTC-5
    a_at = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    b_created, b_delivered = chicago(2026, 10, 20, 23, 0), chicago(2026, 10, 20, 23, 30)
    c_created, c_delivered = chicago(2026, 10, 21, 0, 5), chicago(2026, 10, 21, 0, 30)
    assert b_created.date() == end + timedelta(days=1)  # next UTC date
    await seed_order(store, order_doc("ORD-A", created_at=a_at, delivered_at=a_at))
    await seed_order(store, order_doc("ORD-B", created_at=b_created, delivered_at=b_delivered))
    await seed_order(store, order_doc("ORD-C", created_at=c_created, delivered_at=c_delivered))

    _, counts = await _recompute(service, start, end, now=datetime(2026, 10, 25, tzinfo=UTC))

    assert {"ORD-A", "ORD-B"} <= await _written_orders(repo)
    assert "ORD-C" not in await _written_orders(repo)
    assert counts["out_of_range"] >= 1
    assert counts["out_of_range"] == 1  # C is the only seeded order outside the range
    assert counts["sources"] == 2 and counts["written"] == 2
    delivered_calls = [c for c in orders.search_calls if c.get("status") == "delivered"]
    assert delivered_calls, "recompute must enumerate through FuelOrderRepository.search"
    assert all(c["end_date"] == (end + timedelta(days=2)).isoformat() for c in delivered_calls)
    assert all(c["start_date"] == (date(2026, 10, 14) - timedelta(days=31)).isoformat() for c in delivered_calls)
    assert all(c["keyset"] is True for c in delivered_calls)
    # Every recomputed record is origin=recompute and never alerts.
    page = await repo.list_records(TENANT_A, RecordFilters(stage="delivery"), limit=10)
    assert {r["origin"] for r in page.items} == {"recompute"}
    assert {r["alert_state"] for r in page.items} == {"none"}
    if read_path:
        assert not store.calls_to("fuel_orders_current"), "Postgres path must not read ES"


async def test_end_plus_one_bound_would_miss_order_b(repo, read_path, flag_on):
    """Control: the pre-freeze ``end + 1`` bound drops B on both paths, so the
    boundary test above is meaningful."""

    store, orders, _ = await _setup(repo, read_path)
    end = date(2026, 10, 20)
    b_created, b_delivered = chicago(2026, 10, 20, 23, 0), chicago(2026, 10, 20, 23, 30)
    await seed_order(store, order_doc("ORD-B", created_at=b_created, delivered_at=b_delivered))
    narrow = await orders.search(
        TENANT_A, status="delivered", end_date=(end + timedelta(days=1)).isoformat(), keyset=True, size=50
    )
    wide = await orders.search(
        TENANT_A, status="delivered", end_date=(end + timedelta(days=2)).isoformat(), keyset=True, size=50
    )
    assert [o.order_id for o in narrow["orders"]] == []
    assert [o.order_id for o in wide["orders"]] == ["ORD-B"]


async def test_range_crossing_dst_change(repo, read_path, flag_on):
    """Simplification 13 (g): CDT -> CST on 2026-11-01 inside the range."""

    store, orders, service = await _setup(repo, read_path)
    start, end = date(2026, 10, 30), date(2026, 11, 2)
    # Start edge (CDT, UTC-5): 00:10 local on start is in; 23:50 local the day before is out,
    # although both share the UTC date of the range's first instant.
    in_start = chicago(2026, 10, 30, 0, 10)
    before_start = chicago(2026, 10, 29, 23, 50)
    assert in_start.date() == before_start.date()
    # End edge (CST, UTC-6).
    a_at = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    b_created, b_delivered = chicago(2026, 11, 2, 23, 0), chicago(2026, 11, 2, 23, 30)
    c_created, c_delivered = chicago(2026, 11, 3, 0, 5), chicago(2026, 11, 3, 0, 30)
    for order_id, created, delivered in [
        ("ORD-START-IN", in_start, in_start),
        ("ORD-START-OUT", before_start, before_start),
        ("ORD-A", a_at, a_at),
        ("ORD-B", b_created, b_delivered),
        ("ORD-C", c_created, c_delivered),
    ]:
        await seed_order(store, order_doc(order_id, created_at=created, delivered_at=delivered))

    _, counts = await _recompute(service, start, end, now=datetime(2026, 11, 10, tzinfo=UTC))

    assert await _written_orders(repo) == {"ORD-START-IN", "ORD-A", "ORD-B"}
    assert counts["out_of_range"] == 2  # START-OUT and C were enumerated and dropped
    call = next(c for c in orders.search_calls if c.get("status") == "delivered")
    assert call["end_date"] == "2026-11-04"
    # start_utc_date is the UTC date of 2026-10-30 00:00 CDT (= 05:00Z, same date).
    assert call["start_date"] == (date(2026, 10, 30) - timedelta(days=31)).isoformat()


async def test_freeze_13_f_membership_is_as_of(repo, read_path, flag_on):
    """Simplification 13 (f): range [day 0, day 6]."""

    store, _, service = await _setup(repo, read_path)
    day0 = date(2026, 9, 7)

    def noon(offset: int) -> datetime:
        return chicago(2026, 9, 7, 12) + timedelta(days=offset)

    await seed_order(store, order_doc("ORD-LEAD", created_at=noon(-3), delivered_at=noon(1)))
    await seed_order(store, order_doc("ORD-EARLY", created_at=noon(-2), delivered_at=noon(-1)))
    # An invoice created on day 9 for a delivery on day 5.
    await seed_invoice(
        store,
        invoice_doc(
            "INV-LATE",
            created_at=noon(9),
            delivered_at=noon(5),
            status="open",
            finalized_at=noon(9),
            order_id="ORD-NO-ORDER",
            lines=[line("l1")],
        ),
    )

    _, counts = await _recompute(
        service, day0, day0 + timedelta(days=6), stages=("delivery", "invoice"), now=noon(12)
    )

    assert await _written_orders(repo) == {"ORD-LEAD"}
    invoices = await repo.list_records(TENANT_A, RecordFilters(stage="invoice"), limit=10)
    assert [r["invoice_id"] for r in invoices.items] == ["INV-LATE"]
    assert invoices.items[0]["frozen_at"] is not None  # non-draft source: frozen
    assert counts["out_of_range"] == 1  # ORD-EARLY
