"""Money precision: Decimal parsing, no float drift (AC-19 to AC-23).

Covers design "Money arithmetic": exact round trips, a rejection matrix that
never rounds, JSON floats refused as ``invalid_decimal``, AC-20's cost
example, ``margin = revenue - cost`` exactly, and the ``margin_bp`` /
``margin_pct`` rounding rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st
from pydantic import ValidationError

from commerce.models.margin import (
    FLOOR_MICROS_MAX,
    MAX_UNIT_COST_MICROS,
    CostEntryCreate,
    MarginPreviewRequest,
    MarginRecomputeRequest,
    MarginSettingsUpdate,
    MarginValueError,
    compute_margin_amounts,
    gallons_to_milli,
    gallons_to_ugal,
    margin_bp,
    margin_pct,
    parse_gallons_milli,
    parse_usd_micros,
    quantize_half_up,
    usd_to_micros_half_up,
    weighted_average_micros,
)
from errors.handlers import register_exception_handlers
from services.money import line_subtotal_cents


def _usd(value):
    return parse_usd_micros(value, field="unit_cost_usd", max_micros=MAX_UNIT_COST_MICROS)


def _error_type(fn, value) -> str:
    with pytest.raises(MarginValueError) as exc:
        fn(value)
    return exc.value.type


# ---------------------------------------------------------------------------
# parse_usd_micros
# ---------------------------------------------------------------------------


def _micros_text(micros: int, extra_zeros: int) -> str:
    whole, frac = divmod(micros, 1_000_000)
    return f"{whole}.{frac:06d}" + "0" * extra_zeros


@settings(max_examples=300, deadline=None)
@given(
    micros=st.integers(min_value=0, max_value=MAX_UNIT_COST_MICROS),
    extra_zeros=st.integers(min_value=0, max_value=6),
)
def test_usd_strings_round_trip_exactly(micros, extra_zeros):
    assert _usd(_micros_text(micros, extra_zeros)) == micros


@settings(max_examples=200, deadline=None)
@given(
    micros=st.integers(min_value=0, max_value=MAX_UNIT_COST_MICROS),
    dp=st.integers(min_value=0, max_value=6),
)
def test_usd_strings_with_fewer_decimals_round_trip(micros, dp):
    quantum = 10 ** (6 - dp)
    micros = micros - micros % quantum
    text = f"{Decimal(micros).scaleb(-6):.{dp}f}"
    assert _usd(text) == micros


@given(st.integers(min_value=0, max_value=100))
def test_usd_ints_round_trip(value):
    assert _usd(value) == value * 1_000_000


@settings(max_examples=200, deadline=None)
@given(
    micros=st.integers(min_value=0, max_value=MAX_UNIT_COST_MICROS - 1),
    tail=st.integers(min_value=1, max_value=999_999),
)
def test_extra_precision_is_rejected_never_rounded(micros, tail):
    text = _micros_text(micros, 0) + f"{tail:06d}".rstrip("0")
    assert _error_type(_usd, text) == "too_many_decimals"


def test_trailing_zeros_pass():
    assert _usd("2.5000000") == 2_500_000
    assert _usd(" 2.5 ") == 2_500_000
    assert _usd("0") == 0
    assert _usd("100") == MAX_UNIT_COST_MICROS
    assert _usd("100.000000000000") == MAX_UNIT_COST_MICROS


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2.0000001", "too_many_decimals"),
        ("0.0000005", "too_many_decimals"),
        ("-1", "invalid_decimal"),
        ("+1", "invalid_decimal"),
        ("1e2", "invalid_decimal"),
        ("1E-2", "invalid_decimal"),
        ("NaN", "invalid_decimal"),
        ("Infinity", "invalid_decimal"),
        ("-Infinity", "invalid_decimal"),
        ("", "invalid_decimal"),
        ("1,000", "invalid_decimal"),
        (".5", "invalid_decimal"),
        ("5.", "invalid_decimal"),
        ("1000", "invalid_decimal"),  # more than 3 integer digits
        ("\u0662.5", "invalid_decimal"),  # non-ASCII digit
        ("100.000001", "out_of_range"),
        (2.5, "invalid_decimal"),
        (2.0, "invalid_decimal"),
        (True, "invalid_decimal"),
        (None, "invalid_decimal"),
        (Decimal("2.5"), "invalid_decimal"),
        (-1, "out_of_range"),
        (101, "out_of_range"),
    ],
)
def test_usd_rejection_matrix(value, expected):
    assert _error_type(_usd, value) == expected


def test_max_micros_is_the_callers_bound():
    floor = parse_usd_micros("5", field="floor_usd_per_gallon", max_micros=FLOOR_MICROS_MAX)
    assert floor == FLOOR_MICROS_MAX
    with pytest.raises(MarginValueError) as exc:
        parse_usd_micros("5.000001", field="floor_usd_per_gallon", max_micros=FLOOR_MICROS_MAX)
    assert exc.value.type == "out_of_range"
    assert exc.value.as_error() == {
        "loc": ["floor_usd_per_gallon"],
        "msg": exc.value.msg,
        "type": "out_of_range",
    }


# ---------------------------------------------------------------------------
# parse_gallons_milli
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(
    milli=st.integers(min_value=1, max_value=1_000_000_000),
    extra_zeros=st.integers(min_value=0, max_value=9),
)
def test_gallons_round_trip_exactly(milli, extra_zeros):
    whole, frac = divmod(milli, 1_000)
    assert parse_gallons_milli(f"{whole}.{frac:03d}" + "0" * extra_zeros) == milli


@pytest.mark.parametrize(
    "value, expected",
    [
        ("4321.456", 4_321_456),
        ("4321.4560000", 4_321_456),
        ("0.001", 1),
        ("1000000", 1_000_000_000),
        (250, 250_000),
    ],
)
def test_gallons_accepts(value, expected):
    assert parse_gallons_milli(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("0.0005", "too_many_decimals"),
        ("1.0001", "too_many_decimals"),
        ("0", "out_of_range"),
        ("0.000", "out_of_range"),
        (0, "out_of_range"),
        ("1000000.001", "out_of_range"),
        ("-1", "invalid_decimal"),
        ("1e3", "invalid_decimal"),
        ("NaN", "invalid_decimal"),
        ("Infinity", "invalid_decimal"),
        ("12345678", "invalid_decimal"),  # more than 7 integer digits
        (2.5, "invalid_decimal"),
        (True, "invalid_decimal"),
    ],
)
def test_gallons_rejection_matrix(value, expected):
    assert _error_type(parse_gallons_milli, value) == expected


# ---------------------------------------------------------------------------
# Request models: JSON floats are refused as invalid_decimal
# ---------------------------------------------------------------------------

_ENTRY = {
    "kind": "purchase",
    "product_code": "DIESEL_2",
    "terminal_id": "T-1",
    "effective_at": "2026-10-01",
    "unit_cost_usd": "2.5",
    "gallons": "1000",
}


def _types(exc: ValidationError) -> set[str]:
    return {e["type"] for e in exc.errors()}


@pytest.mark.parametrize("field", ["unit_cost_usd", "gallons"])
@pytest.mark.parametrize("bad", [2.5, 1e2, 2.0000000000000001, True, [1], {"v": 1}])
def test_cost_entry_money_fields_refuse_non_string_numbers(field, bad):
    with pytest.raises(ValidationError) as exc:
        CostEntryCreate(**{**_ENTRY, field: bad})
    assert _types(exc.value) == {"invalid_decimal"}
    assert "send as a string" in str(exc.value)


def test_cost_entry_accepts_strings_and_ints_unparsed():
    entry = CostEntryCreate(**{**_ENTRY, "gallons": 1000})
    assert entry.unit_cost_usd == "2.5" and entry.gallons == 1000


def test_cost_entry_body_tenant_id_is_rejected():
    with pytest.raises(ValidationError) as exc:
        CostEntryCreate(**{**_ENTRY, "tenant_id": "other"})
    assert _types(exc.value) == {"extra_forbidden"}


def test_settings_and_preview_money_fields_refuse_floats():
    with pytest.raises(ValidationError) as exc:
        MarginSettingsUpdate(floor_usd_per_gallon=0.1)
    assert _types(exc.value) == {"invalid_decimal"}
    with pytest.raises(ValidationError) as exc:
        MarginSettingsUpdate(product_floors={"DIESEL_2": 0.2})
    assert _types(exc.value) == {"invalid_decimal"}
    with pytest.raises(ValidationError) as exc:
        MarginPreviewRequest(product_code="DIESEL_2", gallons=10.5, unit_price_usd="3")
    assert _types(exc.value) == {"invalid_decimal"}
    with pytest.raises(ValidationError) as exc:
        MarginPreviewRequest(product_code="DIESEL_2", gallons="10", unit_price_usd=3.25)
    assert _types(exc.value) == {"invalid_decimal"}


def test_preview_needs_exactly_one_price_source():
    MarginPreviewRequest(product_code="DIESEL_2", gallons="10", unit_price_usd="3")
    MarginPreviewRequest(product_code="DIESEL_2", gallons="10", customer_id="C-1")
    for kwargs in ({}, {"unit_price_usd": "3", "customer_id": "C-1"}):
        with pytest.raises(ValidationError):
            MarginPreviewRequest(product_code="DIESEL_2", gallons="10", **kwargs)


def test_settings_product_floor_key_cap():
    floors = {f"P{i}": "0.1" for i in range(51)}
    with pytest.raises(ValidationError):
        MarginSettingsUpdate(product_floors=floors)


def test_recompute_range_cap():
    MarginRecomputeRequest(start_date="2026-01-01", end_date="2026-04-02", reason="r")  # 92 days
    with pytest.raises(ValidationError):
        MarginRecomputeRequest(start_date="2026-01-01", end_date="2026-04-03", reason="r")
    with pytest.raises(ValidationError):
        MarginRecomputeRequest(start_date="2026-01-02", end_date="2026-01-01", reason="r")


def test_json_float_through_the_app_is_a_422_invalid_decimal():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/entries")
    async def _create(body: CostEntryCreate):  # pragma: no cover - never reached
        return {"ok": True}

    client = TestClient(app)
    resp = client.post(
        "/entries",
        content=(
            '{"kind": "purchase", "product_code": "DIESEL_2", "terminal_id": "T-1", '
            '"effective_at": "2026-10-01", "unit_cost_usd": 2.0000000000000001, '
            '"gallons": "1000"}'
        ),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "VALIDATION_ERROR"
    assert body["details"]["errors"] == [
        {"loc": ["body", "unit_cost_usd"], "msg": "send as a string", "type": "invalid_decimal"}
    ]


# ---------------------------------------------------------------------------
# Derived money
# ---------------------------------------------------------------------------


def test_ac20_cost_cents_example():
    gallons = Decimal(parse_gallons_milli("4321.456")).scaleb(-3)
    assert line_subtotal_cents(gallons, 2_987_654) == 1_291_102
    amounts = compute_margin_amounts(
        gallons_ugal=4_321_456_000,
        unit_price_micros=3_000_000,
        revenue_cents=line_subtotal_cents(gallons, 3_000_000),
        landed_cost_micros=2_987_654,
    )
    assert amounts.cost_cents == 1_291_102


def test_ac2_wac_example():
    lots = [(parse_gallons_milli("1000"), _usd("2.5")), (parse_gallons_milli("3000"), _usd("2.6"))]
    assert weighted_average_micros(lots) == 2_575_000
    assert weighted_average_micros([]) is None


@settings(max_examples=300, deadline=None)
@given(
    gallons_milli=st.integers(min_value=1, max_value=100_000_000),  # 0.001 to 100,000 gal
    price=st.integers(min_value=0, max_value=MAX_UNIT_COST_MICROS),
    cost=st.integers(min_value=0, max_value=MAX_UNIT_COST_MICROS),
)
def test_margin_is_revenue_minus_cost_exactly(gallons_milli, price, cost):
    gallons_ugal = gallons_milli * 1_000
    gallons = Decimal(gallons_milli).scaleb(-3)
    revenue = line_subtotal_cents(gallons, price)
    amounts = compute_margin_amounts(
        gallons_ugal=gallons_ugal,
        unit_price_micros=price,
        revenue_cents=revenue,
        landed_cost_micros=cost,
    )
    assert amounts.cost_cents == line_subtotal_cents(gallons, cost)
    assert amounts.margin_cents == revenue - amounts.cost_cents
    assert amounts.margin_per_gallon_micros == price - cost
    for value in (amounts.cost_cents, amounts.margin_cents, amounts.margin_per_gallon_micros):
        assert type(value) is int


def test_missing_cost_is_none_never_zero():
    amounts = compute_margin_amounts(
        gallons_ugal=1_000_000_000, unit_price_micros=3_000_000, revenue_cents=300_000,
        landed_cost_micros=None,
    )
    assert amounts.cost_cents is None
    assert amounts.margin_cents is None
    assert amounts.margin_per_gallon_micros is None
    assert amounts.margin_bp is None


def test_explicit_zero_cost_is_zero():
    amounts = compute_margin_amounts(
        gallons_ugal=1_000_000, unit_price_micros=3_000_000, revenue_cents=300,
        landed_cost_micros=0,
    )
    assert amounts.cost_cents == 0 and amounts.margin_cents == 300
    assert margin_pct(amounts.margin_bp) == "100.00"


@pytest.mark.parametrize(
    "margin_cents, revenue_cents, bp, pct",
    [
        (1, 800, 13, "0.13"),  # +12.5 bp -> +13
        (-1, 800, -13, "-0.13"),  # -12.5 bp -> -13 (half away from zero)
        (3, 1_600, 19, "0.19"),  # 18.75 -> 19
        (-3, 1_600, -19, "-0.19"),
        (1_250, 10_000, 1_250, "12.50"),
        (0, 10_000, 0, "0.00"),
        (-30_000, 10_000, -30_000, "-300.00"),
    ],
)
def test_margin_bp_rounding_and_pct(margin_cents, revenue_cents, bp, pct):
    assert margin_bp(margin_cents, revenue_cents) == bp
    assert margin_pct(bp) == pct


def test_margin_pct_null_cases():
    assert margin_bp(100, 0) is None
    assert margin_bp(None, 1_000) is None
    assert margin_pct(None) is None
    amounts = compute_margin_amounts(
        gallons_ugal=1_000_000, unit_price_micros=0, revenue_cents=0, landed_cost_micros=5,
    )
    assert amounts.cost_cents == 0 and amounts.margin_bp is None


def test_margin_bp_extreme_bound_exceeds_32_bits():
    # Price 1 micro, 1,000,000 gal, cost 100,000,000 micros (design "Testing").
    gallons = Decimal(1_000_000)
    revenue = line_subtotal_cents(gallons, 1)
    amounts = compute_margin_amounts(
        gallons_ugal=1_000_000 * 1_000_000,
        unit_price_micros=1,
        revenue_cents=revenue,
        landed_cost_micros=MAX_UNIT_COST_MICROS,
    )
    assert revenue == 100
    assert amounts.cost_cents == 10_000_000_000
    assert amounts.margin_bp < -(2**31)


def test_document_floats_enter_through_str():
    # 2.4 as a float is 2.39999999999999991118...; Decimal(str()) keeps 2.4.
    assert usd_to_micros_half_up(2.4) == 2_400_000
    assert usd_to_micros_half_up(2.4567895) == 2_456_790  # quantized once, half-up
    assert gallons_to_ugal(1234.5678905) == 1_234_567_891
    assert gallons_to_milli(4321.4565) == 4_321_457
    assert quantize_half_up(Decimal("-0.5")) == -1
    with pytest.raises(MarginValueError):
        usd_to_micros_half_up(float("nan"))
    with pytest.raises(MarginValueError):
        gallons_to_ugal("abc")
