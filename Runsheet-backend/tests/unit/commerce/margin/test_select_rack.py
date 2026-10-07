"""``select_rack``: the one rack selection rule (Simplification 8, pure function).

Candidates ``effective_at <= t`` with a positive price; brand match, else
unbranded, else branded; latest instant, highest price, lowest id; fresh
within ``rack_staleness_days``. Store order never changes the pick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from commerce.services.margin_cost_basis import RackRow, select_rack

T = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def row(rid: str, *, at: datetime = T - timedelta(hours=1), micros: int = 2_500_000, branded: bool = False, brand: str | None = None) -> RackRow:
    return RackRow(
        rack_price_id=rid,
        terminal_id="TERM-1",
        product_code="DIESEL_2",
        effective_at=at,
        price_micros=micros,
        branded_flag=branded,
        supplier_brand=brand,
    )


def test_same_instant_unbranded_takes_the_higher_price_in_any_order():
    a, b = row("R-A", micros=2_400_000), row("R-B", micros=2_450_000)
    for rows in ([a, b], [b, a]):
        pick = select_rack(rows, T)
        assert pick.row.rack_price_id == "R-B"
        assert pick.selection == "unbranded_max"
        assert pick.fresh is True


def test_latest_effective_at_beats_a_higher_older_price():
    older = row("R-OLD", at=T - timedelta(hours=5), micros=3_000_000)
    newer = row("R-NEW", at=T - timedelta(hours=1), micros=2_000_000)
    assert select_rack([older, newer], T).row.rack_price_id == "R-NEW"


def test_ties_break_on_the_lowest_id():
    rows = [row("R-2"), row("R-1"), row("R-3")]
    assert select_rack(rows, T).row.rack_price_id == "R-1"


def test_brand_match_beats_a_higher_unbranded_row():
    branded = row("R-BR", micros=2_300_000, branded=True, brand="Acme Fuels")
    unbranded = row("R-UN", micros=2_600_000)
    pick = select_rack([unbranded, branded], T, supplier="  acme fuels ")
    assert (pick.row.rack_price_id, pick.selection) == ("R-BR", "brand_match")


def test_no_brand_match_falls_back_to_unbranded_then_branded():
    branded = row("R-BR", micros=2_900_000, branded=True, brand="Other")
    unbranded = row("R-UN", micros=2_600_000)
    assert select_rack([branded, unbranded], T, supplier="Acme").selection == "unbranded_max"
    only_branded = select_rack([branded], T, supplier="Acme")
    assert (only_branded.row.rack_price_id, only_branded.selection) == ("R-BR", "branded_max")
    assert select_rack([branded], T).selection == "branded_max"


def test_without_a_supplier_branded_rows_are_ignored_when_unbranded_exist():
    branded = row("R-BR", micros=2_900_000, branded=True, brand="Acme")
    unbranded = row("R-UN", micros=2_600_000)
    assert select_rack([branded, unbranded], T).row.rack_price_id == "R-UN"


def test_future_and_non_positive_rows_are_not_candidates():
    future = row("R-FUT", at=T + timedelta(seconds=1), micros=9_000_000)
    zero = row("R-ZERO", micros=0)
    assert select_rack([future, zero], T) is None
    ok = row("R-OK", at=T - timedelta(days=1))
    assert select_rack([future, zero, ok], T).row.rack_price_id == "R-OK"


def test_freshness_boundary():
    exactly = row("R-4D", at=T - timedelta(days=4))
    assert select_rack([exactly], T, staleness_days=4).fresh is True
    past = row("R-4D+", at=T - timedelta(days=4, microseconds=1))
    assert select_rack([past], T, staleness_days=4).fresh is False
    assert select_rack([past], T, staleness_days=5).fresh is True


def test_a_row_at_t_is_a_candidate():
    assert select_rack([row("R-AT", at=T)], T).row.rack_price_id == "R-AT"


_rows = st.lists(
    st.builds(
        row,
        st.text(alphabet="ABC123", min_size=1, max_size=3),
        at=st.integers(min_value=-96, max_value=2).map(lambda h: T + timedelta(hours=h)),
        micros=st.sampled_from([0, 2_400_000, 2_450_000, 2_500_000]),
        branded=st.booleans(),
        brand=st.sampled_from([None, "Acme", "acme", "Other"]),
    ),
    min_size=0,
    max_size=12,
    unique_by=lambda r: r.rack_price_id,  # rack_price_id is the store's document id
)


@settings(max_examples=300, deadline=None)
@given(rows=_rows, supplier=st.sampled_from([None, "ACME", "Other", "Nobody"]), data=st.data())
def test_a_permutation_never_changes_the_pick(rows, supplier, data):
    shuffled = data.draw(st.permutations(rows))
    first = select_rack(rows, T, supplier)
    second = select_rack(shuffled, T, supplier)
    if first is None:
        assert second is None
    else:
        assert second is not None
        assert (first.row, first.selection, first.fresh) == (second.row, second.selection, second.fresh)
