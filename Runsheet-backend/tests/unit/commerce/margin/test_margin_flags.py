"""FR4 flags: boundaries, per-product floor, missing cost (AC-38, AC-39, AC-41)."""

from __future__ import annotations

import pytest

from commerce.services.margin_service import compute_flags, floor_for

FLOOR = 100_000


@pytest.mark.parametrize(
    "per_gallon, below, negative",
    [
        (FLOOR - 1, True, False),  # floor - 1 micro: below floor
        (FLOOR, False, False),  # exactly the floor: not below
        (0, True, False),  # zero margin: below floor, not negative
        (-1, False, True),  # -1 micro: negative, not below floor
    ],
)
def test_flag_boundaries(make_candidate, per_gallon, below, negative):
    flags = compute_flags(make_candidate(margin_per_gallon_micros=per_gallon), FLOOR)
    assert flags["flag_below_floor"] is below
    assert flags["flag_negative_margin"] is negative
    assert flags["flag_missing_cost"] is False


def test_missing_cost_iff_method_none(make_missing_cost, make_candidate):
    missing = compute_flags(make_missing_cost(), FLOOR)
    assert missing == {
        "flag_missing_cost": True,
        "flag_negative_margin": False,
        "flag_below_floor": False,
        "flag_terminal_unattributed": False,
    }
    assert compute_flags(make_candidate(method="override"), FLOOR)["flag_missing_cost"] is False


def test_terminal_unattributed(make_candidate):
    assert compute_flags(make_candidate(terminal_id=None), FLOOR)["flag_terminal_unattributed"] is True
    assert compute_flags(make_candidate(terminal_id="TERM-1"), FLOOR)["flag_terminal_unattributed"] is False


def test_per_product_floor_beats_tenant_floor(make_candidate):
    settings = {"floor_micros": 100_000, "product_floors": {"DIESEL_2": 250_000}}
    assert floor_for(settings, "DIESEL_2") == 250_000
    assert floor_for(settings, "GASOLINE_REG") == 100_000
    candidate = make_candidate(margin_per_gallon_micros=200_000)
    assert compute_flags(candidate, floor_for(settings, "DIESEL_2"))["flag_below_floor"] is True
    assert compute_flags(candidate, floor_for(settings, "GASOLINE_REG"))["flag_below_floor"] is False


def test_floor_defaults_when_settings_lack_it():
    assert floor_for({}, "DIESEL_2") == 100_000
