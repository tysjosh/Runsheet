"""Resolver read budget and ReaderCache (NFR2, design "Read budget and ReaderCache").

A counting fake (every ``search_documents`` call plus every entry read)
pins: 1,000 BOLs + 2,000 rack rows within 28 queries uncached; a warm-cache
second source within 3; two products at one terminal sharing one BOL read;
one rack read per (terminal, product) for an unattributed sale; the live
path (no cache) reading fresh every time.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from commerce.services.margin_cost_basis import CACHE_MAX_KEYS, CostBasisSettings, ReaderCache

from ._cost_basis_support import (
    AS_OF,
    T1,
    T2,
    Harness,
    bol_doc,
    contract_doc,
    days,
    plan_doc,
    rack_doc,
)
from ._margin_fakes import CountingEntries, FakeDocStore


@pytest.fixture
def h(repo) -> Harness:
    return Harness(repo=repo, store=FakeDocStore(), entries=CountingEntries(repo))


def seed_1000_bols_2000_rack(h: Harness) -> None:
    start = AS_OF - days(30)
    span = timedelta(days=30)
    bols = []
    for i in range(1_000):
        page = i // 200
        bols.append(bol_doc(f"B{i:05d}", start + span * (i + 1) // 1_001, 1000.0, plan=f"PLAN-{page}"))
    h.store.add("terminal_bols", *bols)
    # Each page has its own plan and contract; the contract dates exclude every
    # lift, so rack at lift prices the lots and every read on the path is taken.
    h.store.add("mvp_load_plans", *[plan_doc(f"PLAN-{p}", contract_id=f"C-{p}") for p in range(5)])
    h.store.add(
        "supplier_contracts",
        *[contract_doc(f"C-{p}", 2.0, effective_from=date(2027, 1, 1)) for p in range(5)],
    )
    rack_start = start - days(4)
    step = timedelta(days=34) // 2_000
    h.store.add(
        "rack_prices",
        *[rack_doc(f"R{i:05d}", rack_start + step * i, 2.5) for i in range(2_000)],
    )


async def test_uncached_budget_1000_bols_2000_rack_rows_within_28(h):
    seed_1000_bols_2000_rack(h)
    basis = await h.resolve()
    assert basis.method == "wac"
    assert len(basis.lots) == 1_000
    assert {lot["priced_by"] for lot in basis.lots} == {"rack"}
    assert basis.diagnostics["bols_scanned"] == 1_000
    assert h.query_count() <= 28
    assert len(h.store.calls_to("terminal_bols")) == 5
    assert len(h.store.calls_to("mvp_load_plans")) == 5
    assert len(h.store.calls_to("supplier_contracts")) == 5
    assert len(h.store.calls_to("rack_prices")) == 4
    assert [name for name, _ in h.entries.calls].count("active_entries_for_bols") == 5


async def test_warm_cache_second_source_within_3(h):
    seed_1000_bols_2000_rack(h)
    cache = ReaderCache()
    first = await h.resolve(cache=cache)
    h.reset_counts()
    second = await h.resolve(cache=cache)
    assert h.query_count() <= 3
    assert second.to_snapshot() == first.to_snapshot()


async def test_live_path_without_cache_reads_fresh(h):
    seed_1000_bols_2000_rack(h)
    await h.resolve()
    first = h.query_count()
    h.reset_counts()
    await h.resolve()
    assert h.query_count() == first


async def test_two_products_at_one_terminal_share_one_bol_read(h):
    t = AS_OF - days(2)
    h.store.add(
        "terminal_bols",
        bol_doc("B-D", t, 1000.0, product="DIESEL_2"),
        bol_doc("B-G", t, 1000.0, product="GASOLINE_REG"),
    )
    h.store.add(
        "rack_prices",
        rack_doc("R-D", t - timedelta(hours=1), 2.5, product="DIESEL_2"),
        rack_doc("R-G", t - timedelta(hours=1), 2.9, product="GASOLINE_REG"),
    )
    cache = ReaderCache()
    diesel = await h.resolve(product="DIESEL_2", cache=cache)
    gasoline = await h.resolve(product="GASOLINE_REG", cache=cache)
    assert diesel.product_cost_micros == 2_500_000
    assert gasoline.product_cost_micros == 2_900_000
    assert len(h.store.calls_to("terminal_bols")) == 1
    rack_calls = h.store.calls_to("rack_prices")
    assert len(rack_calls) == 2  # rack is per (terminal, product), never per terminal


async def test_unattributed_sale_reads_rack_once_per_bol_terminal(h):
    t = AS_OF - days(2)
    h.store.add(
        "terminal_bols",
        bol_doc("B1", t, 1000.0, terminal=T1),
        bol_doc("B2", t + timedelta(hours=1), 1000.0, terminal=T1),
        bol_doc("B3", t, 1000.0, terminal=T2),
    )
    h.store.add(
        "rack_prices",
        rack_doc("R1", t - timedelta(hours=1), 2.5, terminal=T1),
        rack_doc("R2", t - timedelta(hours=1), 2.8, terminal=T2),
    )
    basis = await h.resolve(terminal=None)
    assert basis.method == "wac"
    assert sorted(lot["price_ref_id"] for lot in basis.lots) == ["R1", "R1", "R2"]
    rack_calls = h.store.calls_to("rack_prices")
    assert len(rack_calls) == 2
    terminals = sorted(
        clause["term"]["terminal_id"]
        for body in rack_calls
        for clause in body["query"]["bool"]["must"][0]["bool"]["filter"]
        if "term" in clause
    )
    assert terminals == [T1, T2]


async def test_rack_rows_cached_for_a_wider_window_serve_a_narrower_one(h):
    h.store.add("rack_prices", rack_doc("R1", AS_OF - days(1), 2.5), rack_doc("R-OLD", AS_OF - days(20), 2.0))
    cache = ReaderCache()
    wide = await h.resolve(cache=cache)
    h.reset_counts()
    narrow = await h.resolve(cache=cache, settings=CostBasisSettings(wac_window_days=7))
    assert h.store.calls_to("rack_prices") == []
    assert (wide.rack_price_id, narrow.rack_price_id) == ("R1", "R1")
    h.reset_counts()
    # A window starting earlier than the cached one re-reads.
    await h.resolve(cache=cache, as_of=AS_OF - days(1))
    assert len(h.store.calls_to("rack_prices")) == 1


def test_reader_cache_is_lru_bounded_at_64_keys():
    cache = ReaderCache()
    for i in range(CACHE_MAX_KEYS + 1):
        cache.rack.put((f"T{i}", "DIESEL_2"), object())
        cache.bols.put((f"T{i}", "w0", "w1"), object())
    assert len(cache.rack) == CACHE_MAX_KEYS
    assert len(cache.bols) == CACHE_MAX_KEYS
    assert ("T0", "DIESEL_2") not in cache.rack.keys()
    assert cache.rack.get(("T1", "DIESEL_2")) is not None
    cache.rack.put(("T-new", "DIESEL_2"), object())
    assert ("T1", "DIESEL_2") in cache.rack.keys()  # touched by get, so T2 was evicted
    assert ("T2", "DIESEL_2") not in cache.rack.keys()
