from decimal import Decimal

import pytest

from services.money import (
    legacy_unit_price_cents,
    line_subtotal_cents,
    unit_price_micros_from_record,
    unit_price_usd,
)


def test_fractional_cent_fuel_price_is_not_truncated():
    micros = unit_price_micros_from_record({"unit_price_usd": "2.9660"})

    assert micros == 2_966_000
    assert legacy_unit_price_cents(micros) == 297
    assert unit_price_usd(micros) == Decimal("2.966")
    assert line_subtotal_cents(7_000, micros) == 2_076_200


def test_fractional_legacy_cents_are_upgraded_without_precision_loss():
    assert unit_price_micros_from_record(
        {"unit_price_cents": 296.6}
    ) == 2_966_000


@pytest.mark.parametrize(
    "record",
    [
        {"unit_price_micros": -1},
        {"unit_price_usd": "NaN"},
        {"unit_price_cents": True},
    ],
)
def test_invalid_unit_prices_are_rejected(record):
    with pytest.raises(ValueError):
        unit_price_micros_from_record(record)
