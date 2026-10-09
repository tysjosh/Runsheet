"""CostBasisResolver: one test per AC-1..AC-12 plus the Simplification 8 tests.

Override -> weighted average over effective-dated lots -> non-stale rack
fallback -> explicit ``none``. A missing cost is never 0.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest

from commerce.services.margin_cost_basis import (
    BOL_SCAN_CAP,
    RACK_ROW_CAP,
    CostBasis,
    CostBasisSettings,
)

from ._cost_basis_support import (
    AS_OF,
    T1,
    T2,
    UTC,
    Harness,
    add_entry,
    add_purchase,
    bol_doc,
    contract_doc,
    days,
    plan_doc,
    rack_doc,
)
from ._margin_fakes import CountingEntries, FakeDocStore
from .conftest import TENANT_A


@pytest.fixture
def h(repo) -> Harness:
    return Harness(repo=repo, store=FakeDocStore(), entries=CountingEntries(repo))


# ---------------------------------------------------------------------------
# AC-1: override precedence and effective dating
# ---------------------------------------------------------------------------


async def test_active_override_beats_lots_and_rack(h):
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_100_000, effective_at=AS_OF - days(2), terminal=T1)
    await add_purchase(h.repo, gallons=1000, micros=2_500_000, at=AS_OF - days(1))
    h.store.add("rack_prices", rack_doc("R1", AS_OF - days(1), 2.9))
    basis = await h.resolve()
    assert basis.method == "override"
    assert basis.product_cost_micros == 2_100_000
    assert basis.landed_cost_micros == 2_100_000
    assert basis.override_entry_id is not None
    assert basis.lots == ()
    assert h.store.calls == []  # no BOL or rack read once an override applies


async def test_override_effective_to_is_exclusive(h):
    await add_entry(
        h.repo, TENANT_A, "override", unit_cost_micros=2_100_000,
        effective_at=AS_OF - days(5), effective_to=AS_OF, terminal=T1,
    )
    at_end = await h.resolve()
    assert at_end.method == "none"
    just_before = await h.resolve(as_of=AS_OF - timedelta(microseconds=1))
    assert just_before.method == "override"


async def test_override_not_yet_effective_is_ignored(h):
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_100_000, effective_at=AS_OF + timedelta(seconds=1), terminal=T1)
    assert (await h.resolve()).method == "none"


async def test_latest_effective_at_then_latest_created_at_wins(h):
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_000_000, effective_at=AS_OF - days(3), terminal=T1)
    same_at = AS_OF - days(1)
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_200_000, effective_at=same_at, terminal=T1, created_at=same_at)
    newest = await add_entry(
        h.repo, TENANT_A, "override", unit_cost_micros=2_300_000, effective_at=same_at, terminal=T1,
        created_at=same_at + timedelta(hours=1),
    )
    basis = await h.resolve()
    assert basis.product_cost_micros == 2_300_000
    assert basis.override_entry_id == newest["entry_id"]


async def test_terminal_override_beats_a_later_tenant_wide_override(h):
    terminal = await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_000_000, effective_at=AS_OF - days(5), terminal=T1)
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_900_000, effective_at=AS_OF - days(1))
    basis = await h.resolve()
    assert basis.override_entry_id == terminal["entry_id"]
    other_terminal = await h.resolve(terminal=T2)
    assert other_terminal.product_cost_micros == 2_900_000


# ---------------------------------------------------------------------------
# AC-2 / AC-3: weighted average and window bounds
# ---------------------------------------------------------------------------


async def test_wac_example_is_2575000_micros(h):
    await add_purchase(h.repo, gallons=1000, micros=2_500_000, at=AS_OF - days(10))
    await add_purchase(h.repo, gallons=3000, micros=2_600_000, at=AS_OF - days(5))
    basis = await h.resolve()
    assert basis.method == "wac"
    assert basis.product_cost_micros == 2_575_000
    assert basis.landed_cost_micros == 2_575_000
    assert [lot["priced_by"] for lot in basis.lots] == ["entry", "entry"]
    assert basis.lot_gallons_milli == 4_000_000


async def test_wac_rounds_once_half_up(h):
    # (1 x 1 + 2 x 2) / 3 micros = 1.666... -> 2; one half-up rounding.
    await add_entry(h.repo, TENANT_A, "purchase", unit_cost_micros=1, effective_at=AS_OF - days(2), terminal=T1, gallons_milli=1)
    await add_entry(h.repo, TENANT_A, "purchase", unit_cost_micros=2, effective_at=AS_OF - days(1), terminal=T1, gallons_milli=2)
    assert (await h.resolve()).product_cost_micros == 2


async def test_window_start_is_exclusive_and_as_of_inclusive(h):
    start = AS_OF - days(30)
    await add_purchase(h.repo, gallons=1000, micros=9_000_000, at=start)  # excluded
    await add_purchase(h.repo, gallons=1000, micros=2_000_000, at=start + timedelta(microseconds=1))
    await add_purchase(h.repo, gallons=1000, micros=3_000_000, at=AS_OF)  # included
    await add_purchase(h.repo, gallons=1000, micros=9_000_000, at=AS_OF + timedelta(seconds=1))  # after as_of
    basis = await h.resolve()
    assert basis.product_cost_micros == 2_500_000
    assert basis.window_start == start


async def test_bol_window_bounds_match_the_entry_window(h):
    start = AS_OF - days(30)
    h.store.add(
        "terminal_bols",
        bol_doc("B-START", start, 1000.0),
        bol_doc("B-IN", start + timedelta(seconds=1), 1000.0),
        bol_doc("B-END", AS_OF, 1000.0),
        bol_doc("B-AFTER", AS_OF + timedelta(milliseconds=500), 1000.0),
    )
    for bol_id in ("B-START", "B-IN", "B-END", "B-AFTER"):
        await add_entry(h.repo, TENANT_A, "purchase", unit_cost_micros=2_000_000, effective_at=start, terminal=T1, gallons_milli=1, bol_id=bol_id)
    basis = await h.resolve()
    assert [lot["id"] for lot in basis.lots] == ["B-IN", "B-END"]


async def test_wac_window_follows_settings(h):
    await add_purchase(h.repo, gallons=1000, micros=2_000_000, at=AS_OF - days(10))
    await add_purchase(h.repo, gallons=1000, micros=3_000_000, at=AS_OF - days(3))
    narrow = await h.resolve(settings=CostBasisSettings(wac_window_days=7))
    assert narrow.product_cost_micros == 3_000_000
    assert narrow.wac_window_days == 7


# ---------------------------------------------------------------------------
# AC-4: BOL-referenced purchase prices the BOL; no second lot
# ---------------------------------------------------------------------------


async def test_bol_referenced_purchase_uses_bol_net_gallons_and_adds_no_lot(h):
    h.store.add("terminal_bols", bol_doc("BOL-1", AS_OF - days(2), 7_500.25))
    entry = await add_entry(
        h.repo, TENANT_A, "purchase", unit_cost_micros=2_400_000, effective_at=AS_OF - days(2),
        terminal=T1, gallons_milli=99_000, bol_id="BOL-1",
    )
    basis = await h.resolve()
    assert basis.method == "wac"
    assert len(basis.lots) == 1
    lot = basis.lots[0]
    assert lot["lot_type"] == "bol" and lot["id"] == "BOL-1"
    assert lot["gallons_milli"] == 7_500_250  # BOL net, not the entry's 99 gal
    assert lot["priced_by"] == "entry" and lot["price_ref_id"] == entry["entry_id"]
    assert basis.product_cost_micros == 2_400_000


async def test_bol_gallons_are_quantized_half_up_from_the_document_value(h):
    h.store.add("terminal_bols", bol_doc("BOL-1", AS_OF - days(1), 1000.0005))
    await add_entry(h.repo, TENANT_A, "purchase", unit_cost_micros=2_000_000, effective_at=AS_OF, terminal=T1, gallons_milli=1, bol_id="BOL-1")
    assert (await h.resolve()).lots[0]["gallons_milli"] == 1_000_001


# ---------------------------------------------------------------------------
# AC-5: contract via plan.contract_id only; dates govern; zero -> rack
# ---------------------------------------------------------------------------


async def test_linked_contract_prices_the_lot(h):
    h.store.add("terminal_bols", bol_doc("BOL-1", AS_OF - days(2), 1000.0, plan="PLAN-1"))
    h.store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id="C-1"))
    h.store.add("supplier_contracts", contract_doc("C-1", 2.31))
    basis = await h.resolve()
    lot = basis.lots[0]
    assert (lot["priced_by"], lot["price_ref_id"], lot["unit_cost_micros"]) == ("contract", "C-1", 2_310_000)
    assert lot["contract_status_at_compute"] == "active"
    assert basis.contract_ids == ("C-1",)


async def test_contract_set_inactive_after_the_lift_still_prices(h):
    h.store.add("terminal_bols", bol_doc("BOL-1", AS_OF - days(2), 1000.0, plan="PLAN-1"))
    h.store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id="C-1"))
    h.store.add("supplier_contracts", contract_doc("C-1", 2.31, status="inactive"))
    lot = (await h.resolve()).lots[0]
    assert lot["priced_by"] == "contract"
    assert lot["contract_status_at_compute"] == "inactive"


async def test_lift_outside_contract_dates_falls_to_rack(h):
    lift = AS_OF - days(2)
    h.store.add("terminal_bols", bol_doc("BOL-1", lift, 1000.0, plan="PLAN-1"))
    h.store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id="C-1"))
    h.store.add(
        "supplier_contracts",
        contract_doc("C-1", 2.31, effective_from=date(2026, 1, 1), effective_to=lift.date() - timedelta(days=1)),
    )
    h.store.add("rack_prices", rack_doc("R1", lift - timedelta(hours=1), 2.50))
    lot = (await h.resolve()).lots[0]
    assert (lot["priced_by"], lot["price_ref_id"], lot["unit_cost_micros"]) == ("rack", "R1", 2_500_000)


async def test_lift_date_uses_the_settings_timezone(h):
    # 03:00Z on Sep 29 is Sep 28 in Chicago: a contract starting Sep 29 does not apply.
    lift = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
    h.store.add("terminal_bols", bol_doc("BOL-1", lift, 1000.0, plan="PLAN-1"))
    h.store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id="C-1"))
    h.store.add("supplier_contracts", contract_doc("C-1", 2.31, effective_from=date(2026, 9, 29)))
    h.store.add("rack_prices", rack_doc("R1", lift - timedelta(hours=1), 2.50))
    assert (await h.resolve()).lots[0]["priced_by"] == "rack"
    utc = await h.resolve(settings=CostBasisSettings(timezone="UTC"))
    assert utc.lots[0]["priced_by"] == "contract"


async def test_zero_price_contract_falls_to_rack_and_is_counted(h):
    lift = AS_OF - days(2)
    h.store.add("terminal_bols", bol_doc("BOL-1", lift, 1000.0, plan="PLAN-1"))
    h.store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id="C-1"))
    h.store.add("supplier_contracts", contract_doc("C-1", 0.0))
    h.store.add("rack_prices", rack_doc("R1", lift - timedelta(hours=1), 2.50))
    basis = await h.resolve()
    assert basis.lots[0]["priced_by"] == "rack"
    assert basis.lots[0]["unit_cost_micros"] == 2_500_000
    assert basis.diagnostics["zero_price_ignored"] == 1
    assert basis.product_cost_micros == 2_500_000


async def test_supplier_name_match_never_links_a_contract(h):
    lift = AS_OF - days(2)
    h.store.add("terminal_bols", bol_doc("BOL-1", lift, 1000.0, plan=None, supplier="Acme"))
    h.store.add("supplier_contracts", contract_doc("C-1", 2.0, supplier="Acme"))
    h.store.add("rack_prices", rack_doc("R1", lift - timedelta(hours=1), 2.50))
    basis = await h.resolve()
    assert basis.lots[0]["priced_by"] == "rack"
    assert h.store.calls_to("supplier_contracts") == []
    assert h.store.calls_to("mvp_load_plans") == []


async def test_plan_without_contract_id_prices_at_rack(h):
    lift = AS_OF - days(2)
    h.store.add("terminal_bols", bol_doc("BOL-1", lift, 1000.0, plan="PLAN-1"))
    h.store.add("mvp_load_plans", plan_doc("PLAN-1", contract_id=None))
    h.store.add("supplier_contracts", contract_doc("C-1", 2.0))
    h.store.add("rack_prices", rack_doc("R1", lift - timedelta(hours=1), 2.50))
    assert (await h.resolve()).lots[0]["priced_by"] == "rack"


# ---------------------------------------------------------------------------
# AC-6: exclusion reasons
# ---------------------------------------------------------------------------


async def test_each_exclusion_reason_is_counted_and_other_products_are_skipped(h):
    t = AS_OF - days(1)
    h.store.add(
        "terminal_bols",
        bol_doc("B-NEEDS", t, needs_confirmation=True),
        bol_doc("B-PENDING", t, status="pending_confirmation"),
        bol_doc("B-UNKNOWN", t, product="ROCKET_FUEL"),
        bol_doc("B-GAS", t, product="GASOLINE_REG"),
        bol_doc("B-ZERO", t, 0.0),
        bol_doc("B-NEG", t, -5.0),
        bol_doc("B-BAD", t, "abc"),
        bol_doc("B-UNPRICED", t, 1000.0),
        bol_doc("B-OK", t, 500.0),
    )
    await add_entry(h.repo, TENANT_A, "purchase", unit_cost_micros=2_000_000, effective_at=t, terminal=T1, gallons_milli=1, bol_id="B-OK")
    basis = await h.resolve()
    assert basis.diagnostics["excluded"] == {
        "needs_confirmation": 2,
        "product_unknown": 1,
        "non_positive_gallons": 3,
        "unpriced": 1,
    }
    assert [lot["id"] for lot in basis.lots] == ["B-OK"]
    assert basis.diagnostics["bols_scanned"] == 9


async def test_no_terminal_is_an_exclusion_for_unattributed_reads(h):
    t = AS_OF - days(1)
    h.store.add("terminal_bols", bol_doc("B-BLANK", t, terminal=" "))
    basis = await h.resolve(terminal=None)
    assert basis.diagnostics["excluded"] == {"no_terminal": 1}


# ---------------------------------------------------------------------------
# AC-7 / AC-8: rack fallback, staleness, explicit none
# ---------------------------------------------------------------------------


async def test_rack_fallback_uses_decimal_str_conversion(h):
    # 2.4567895 as a float is 2.45678949999...; Decimal(str()) keeps the half.
    h.store.add("rack_prices", rack_doc("R1", AS_OF - days(1), 2.4567895))
    basis = await h.resolve()
    assert basis.method == "rack_fallback"
    assert basis.product_cost_micros == 2_456_790
    assert basis.rack_price_id == "R1"
    assert basis.rack_selection == "unbranded_max"


async def test_stale_rack_in_window_is_rack_stale(h):
    h.store.add("rack_prices", rack_doc("R1", AS_OF - days(4) - timedelta(seconds=1), 2.5))
    basis = await h.resolve()
    assert (basis.method, basis.no_cost_reason) == ("none", "rack_stale")
    assert basis.landed_cost_micros is None
    assert len(h.store.calls_to("rack_prices")) == 1  # no probe needed


async def test_rack_exactly_at_staleness_limit_is_fresh(h):
    h.store.add("rack_prices", rack_doc("R1", AS_OF - days(4), 2.5))
    assert (await h.resolve()).method == "rack_fallback"


async def test_old_rack_outside_the_window_is_found_by_the_probe(h):
    h.store.add("rack_prices", rack_doc("R-OLD", AS_OF - days(60), 2.5))
    basis = await h.resolve()
    assert (basis.method, basis.no_cost_reason) == ("none", "rack_stale")
    probes = h.store.calls_to("rack_prices")
    assert len(probes) == 2
    assert {"range": {"price_per_gallon_usd": {"gt": 0}}} in probes[1]["query"]["bool"]["must"][0]["bool"]["filter"]


async def test_nothing_is_method_none_never_zero(h):
    basis = await h.resolve()
    assert basis.method == "none"
    assert basis.landed_cost_micros is None
    assert basis.product_cost_micros is None
    assert basis.no_cost_reason == "no_lots_no_rack"
    snapshot = basis.to_snapshot()
    assert snapshot["landed_cost_micros"] is None
    assert snapshot["method"] == "none"
    assert snapshot["adders_micros"] == 0


async def test_zero_only_rack_is_no_lots_no_rack(h):
    h.store.add("rack_prices", rack_doc("R0", AS_OF - days(1), 0.0), rack_doc("R00", AS_OF - days(90), 0.0))
    basis = await h.resolve()
    assert (basis.method, basis.no_cost_reason) == ("none", "no_lots_no_rack")
    assert basis.diagnostics["zero_price_ignored"] == 1


def test_cost_basis_refuses_a_zero_placeholder_for_missing_cost():
    with pytest.raises(ValueError):
        CostBasis(
            method="none", product_code="DIESEL_2", terminal_id=T1, as_of=AS_OF, window_start=None,
            wac_window_days=30, rack_staleness_days=4, product_cost_micros=None, adders_micros=0,
            landed_cost_micros=0, no_cost_reason="no_lots_no_rack",
        )


async def test_unknown_product_is_none(h):
    basis = await h.resolve(product="ROCKET_FUEL")
    assert (basis.method, basis.no_cost_reason, basis.landed_cost_micros) == ("none", "product_unknown", None)
    assert h.query_count() == 0


# ---------------------------------------------------------------------------
# AC-9 / AC-10: explicit zero and adders
# ---------------------------------------------------------------------------


async def test_explicit_zero_override_is_a_real_zero(h):
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=0, effective_at=AS_OF - days(1), terminal=T1)
    basis = await h.resolve()
    assert (basis.method, basis.product_cost_micros, basis.landed_cost_micros) == ("override", 0, 0)


async def test_adders_terminal_beats_tenant_per_type_and_sum(h):
    t = AS_OF - days(1)
    await add_entry(h.repo, TENANT_A, "adder", unit_cost_micros=90_000, effective_at=t, adder_type="freight")
    terminal_freight = await add_entry(h.repo, TENANT_A, "adder", unit_cost_micros=50_000, effective_at=t - days(3), terminal=T1, adder_type="freight")
    fee = await add_entry(h.repo, TENANT_A, "adder", unit_cost_micros=7_000, effective_at=t, adder_type="fee")
    await add_entry(h.repo, TENANT_A, "adder", unit_cost_micros=1_000_000, effective_at=t - days(5), effective_to=t - days(4), adder_type="other")
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_000_000, effective_at=t, terminal=T1)
    basis = await h.resolve()
    assert basis.adders_configured is True
    assert basis.adders_micros == 57_000
    assert basis.landed_cost_micros == 2_057_000
    assert [(a["adder_type"], a["entry_id"], a["scope"]) for a in basis.adders] == [
        ("fee", fee["entry_id"], "tenant"),
        ("freight", terminal_freight["entry_id"], "terminal"),
    ]


async def test_no_adders_means_not_configured(h):
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_000_000, effective_at=AS_OF - days(1), terminal=T1)
    basis = await h.resolve()
    assert (basis.adders_configured, basis.adders_micros, basis.landed_cost_micros) == (False, 0, 2_000_000)


async def test_adders_are_reported_on_a_none_result_but_landed_stays_null(h):
    await add_entry(h.repo, TENANT_A, "adder", unit_cost_micros=50_000, effective_at=AS_OF - days(1), adder_type="freight")
    basis = await h.resolve()
    assert (basis.method, basis.adders_micros, basis.landed_cost_micros) == ("none", 50_000, None)


# ---------------------------------------------------------------------------
# AC-11: unattributed
# ---------------------------------------------------------------------------


async def test_unattributed_uses_tenant_wide_entries_only_and_no_rack_fallback(h):
    t = AS_OF - days(1)
    await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_000_000, effective_at=t, terminal=T1)
    await add_entry(h.repo, TENANT_A, "adder", unit_cost_micros=50_000, effective_at=t, terminal=T1, adder_type="freight")
    h.store.add("rack_prices", rack_doc("R1", t, 2.5))
    basis = await h.resolve(terminal=None)
    assert (basis.method, basis.no_cost_reason) == ("none", "terminal_unattributed_no_cost")
    assert basis.terminal_unattributed is True
    assert basis.adders_configured is False
    assert h.store.calls_to("rack_prices") == []
    tenant_wide = await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_200_000, effective_at=t)
    again = await h.resolve(terminal=None)
    assert (again.method, again.override_entry_id) == ("override", tenant_wide["entry_id"])


async def test_unattributed_lots_come_from_any_terminal(h):
    t = AS_OF - days(1)
    await add_purchase(h.repo, gallons=1000, micros=2_000_000, at=t, terminal=T1)
    await add_purchase(h.repo, gallons=1000, micros=3_000_000, at=t, terminal=T2)
    h.store.add("terminal_bols", bol_doc("B-T2", t, 2000.0, terminal=T2))
    h.store.add("rack_prices", rack_doc("R-T2", t - timedelta(hours=2), 2.75, terminal=T2))
    basis = await h.resolve(terminal=None)
    assert basis.method == "wac"
    # (1000 x 2.0 + 1000 x 3.0 + 2000 x 2.75) / 4000 = 2.625
    assert basis.product_cost_micros == 2_625_000
    rack_lot = [lot for lot in basis.lots if lot["lot_type"] == "bol"][0]
    assert (rack_lot["priced_by"], rack_lot["price_ref_id"]) == ("rack", "R-T2")


# ---------------------------------------------------------------------------
# AC-12: superseded and voided entries are ignored
# ---------------------------------------------------------------------------


async def test_superseded_and_voided_entries_are_ignored(h):
    old = await add_entry(h.repo, TENANT_A, "override", unit_cost_micros=2_000_000, effective_at=AS_OF - days(1), terminal=T1)
    replacement = {
        "kind": "override", "product_code": "DIESEL_2", "terminal_id": T1, "effective_at": AS_OF - days(1),
        "unit_cost_micros": 2_100_000, "natural_key": "nk-new", "source": "manual", "created_by": "t",
    }
    await h.repo.transition_entry(TENANT_A, old["entry_id"], action="supersede", reason="fix", actor="t", replacement=replacement)
    assert (await h.resolve()).product_cost_micros == 2_100_000
    current = (await h.repo.list_entries(TENANT_A, kind="override")).items[0]
    await h.repo.transition_entry(TENANT_A, current["entry_id"], action="void", reason="gone", actor="t")
    lot = await add_purchase(h.repo, gallons=1000, micros=2_500_000, at=AS_OF - days(1))
    assert (await h.resolve()).method == "wac"
    await h.repo.transition_entry(TENANT_A, lot["entry_id"], action="void", reason="gone", actor="t")
    assert (await h.resolve()).method == "none"


# ---------------------------------------------------------------------------
# Simplification 8 (frozen) tests
# ---------------------------------------------------------------------------


async def test_2500_rack_rows_still_price_the_earliest_bol(h):
    start = AS_OF - days(30)
    rack_start = start - days(4)
    step = timedelta(minutes=19)  # 2,500 rows across the 34-day rack window
    h.store.add(
        "rack_prices",
        *[rack_doc(f"R{i:05d}", rack_start + step * i, 2.0 + (i % 7) * 0.01) for i in range(2_500)],
    )
    earliest = start + timedelta(minutes=30)
    h.store.add("terminal_bols", bol_doc("B-EARLY", earliest, 1000.0))
    basis = await h.resolve()
    assert basis.method == "wac"
    lot = basis.lots[0]
    assert lot["id"] == "B-EARLY" and lot["priced_by"] == "rack"
    expected = max(
        (i for i in range(2_500) if rack_start + step * i <= earliest), key=lambda i: rack_start + step * i
    )
    assert lot["price_ref_id"] == f"R{expected:05d}"
    assert len(h.store.calls_to("rack_prices")) == 5


async def test_10001_rack_rows_is_computation_error(h, caplog):
    t0 = AS_OF - days(30)
    h.store.add(
        "rack_prices",
        *[rack_doc(f"R{i:05d}", t0 + timedelta(minutes=4 * i), 2.5) for i in range(RACK_ROW_CAP + 1)],
    )
    with caplog.at_level(logging.ERROR):
        basis = await h.resolve()
    assert (basis.method, basis.no_cost_reason) == ("none", "computation_error")
    assert basis.diagnostics["rack_cap_exceeded"] is True
    assert basis.landed_cost_micros is None
    assert any("cap exceeded" in r.getMessage() and TENANT_A in r.getMessage() for r in caplog.records)


async def test_10000_rack_rows_is_within_the_cap(h):
    t0 = AS_OF - days(30)
    h.store.add(
        "rack_prices",
        *[rack_doc(f"R{i:05d}", t0 + timedelta(minutes=4 * i), 2.5) for i in range(RACK_ROW_CAP)],
    )
    basis = await h.resolve()
    assert basis.method == "rack_fallback"


async def test_bol_cap_counts_every_product(h, caplog):
    t = AS_OF - days(1)
    h.store.add("terminal_bols", *[bol_doc(f"X{i:05d}", t, product="DIESEL_2") for i in range(4_000)])
    h.store.add("terminal_bols", *[bol_doc(f"Y{i:05d}", t, product="GASOLINE_REG") for i in range(1_500)])
    with caplog.at_level(logging.ERROR):
        diesel = await h.resolve(product="DIESEL_2")
        gasoline = await h.resolve(product="GASOLINE_REG")
    for basis in (diesel, gasoline):
        assert (basis.method, basis.no_cost_reason) == ("none", "computation_error")
        assert basis.diagnostics["lot_cap_exceeded"] is True
        assert basis.diagnostics["bols_scanned"] > 0
        assert basis.lots == ()
    assert sum(1 for r in caplog.records if r.levelname == "ERROR" and "bol_scan cap exceeded" in r.getMessage()) == 2


async def test_bol_cap_boundary(h):
    t = AS_OF - days(1)
    h.store.add("terminal_bols", *[bol_doc(f"X{i:05d}", t, product="GASOLINE_REG") for i in range(BOL_SCAN_CAP)])
    at_cap = await h.resolve()
    assert at_cap.diagnostics["lot_cap_exceeded"] is False
    assert at_cap.diagnostics["bols_scanned"] == BOL_SCAN_CAP
    h.store.add("terminal_bols", bol_doc("X-OVER", t, product="GASOLINE_REG"))
    over = await h.resolve()
    assert over.diagnostics["lot_cap_exceeded"] is True


async def test_bol_cap_without_store_totals_pages_to_the_cap(h):
    class NoTotalStore(FakeDocStore):
        async def search_documents(self, index, body, size=100, request_timeout=10):
            response = await super().search_documents(index, body, size, request_timeout)
            response["hits"].pop("total")
            return response

    store = NoTotalStore()
    store.add("terminal_bols", *[bol_doc(f"X{i:05d}", AS_OF - days(1), product="GASOLINE_REG") for i in range(BOL_SCAN_CAP + 1)])
    harness = Harness(repo=h.repo, store=store, entries=CountingEntries(h.repo))
    basis = await harness.resolve()
    assert basis.diagnostics["lot_cap_exceeded"] is True
    assert basis.diagnostics["bols_scanned"] == BOL_SCAN_CAP + 1


async def test_snapshot_is_json_safe_and_complete(h):
    h.store.add("rack_prices", rack_doc("R1", AS_OF - days(1), 2.5))
    snapshot = (await h.resolve()).to_snapshot()
    import json

    json.dumps(snapshot)
    assert set(snapshot) >= {
        "method", "product_cost_micros", "adders_micros", "adders", "adders_configured", "landed_cost_micros",
        "override_entry_id", "lots", "rack_price_id", "rack_selection", "contract_ids", "wac_window_days",
        "rack_staleness_days", "window_start", "as_of", "terminal_unattributed", "no_cost_reason", "diagnostics",
    }
    assert set(snapshot["diagnostics"]) == {
        "excluded", "bols_scanned", "lot_cap_exceeded", "rack_cap_exceeded", "zero_price_ignored",
    }


async def test_reader_exceptions_propagate(h):
    h.store.raise_on.add("terminal_bols")
    with pytest.raises(RuntimeError):
        await h.resolve()


async def test_naive_as_of_is_refused(h):
    with pytest.raises(ValueError):
        await h.resolve(as_of=datetime(2026, 10, 1, 15, 0))
