"""Exact money helpers for per-unit fuel pricing.

Invoice totals remain integer cents, but fuel unit prices commonly carry four
decimal places in dollars (for example, ``$2.9660`` per gallon).  Storing a
unit price as integer cents loses that precision on large deliveries.

Canonical precise unit prices are therefore stored as integer micro-dollars:

``$1.00 == 1_000_000 micros``.

The legacy ``unit_price_cents`` field remains available for compatibility and
display, while all quantity multiplication should use ``unit_price_micros``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional


MICROS_PER_DOLLAR = 1_000_000
MICROS_PER_CENT = 10_000

_ONE = Decimal("1")


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def unit_price_micros_from_record(
    record: Mapping[str, Any],
) -> Optional[int]:
    """Resolve a precise unit price from a canonical or legacy record.

    Resolution order is ``unit_price_micros``, ``unit_price_usd``, then the
    legacy ``unit_price_cents``.  Fractional legacy cents are intentionally
    accepted so older integrations sending ``296.6`` are upgraded without
    truncation.
    """

    if record.get("unit_price_micros") is not None:
        value = _decimal(
            record["unit_price_micros"], field_name="unit_price_micros"
        )
        micros = int(value.quantize(_ONE, rounding=ROUND_HALF_UP))
    elif record.get("unit_price_usd") is not None:
        value = _decimal(record["unit_price_usd"], field_name="unit_price_usd")
        micros = int(
            (value * MICROS_PER_DOLLAR).quantize(
                _ONE, rounding=ROUND_HALF_UP
            )
        )
    elif record.get("unit_price_cents") is not None:
        value = _decimal(
            record["unit_price_cents"], field_name="unit_price_cents"
        )
        micros = int(
            (value * MICROS_PER_CENT).quantize(
                _ONE, rounding=ROUND_HALF_UP
            )
        )
    else:
        return None

    if micros < 0:
        raise ValueError("unit price must be non-negative")
    return micros


def legacy_unit_price_cents(unit_price_micros: int) -> int:
    """Return a rounded whole-cent compatibility value."""

    if unit_price_micros < 0:
        raise ValueError("unit_price_micros must be non-negative")
    return int(
        (Decimal(unit_price_micros) / MICROS_PER_CENT).quantize(
            _ONE, rounding=ROUND_HALF_UP
        )
    )


def line_subtotal_cents(
    quantity_gallons: Any,
    unit_price_micros: int,
) -> int:
    """Multiply gallons by a micro-dollar unit price and round once to cents."""

    quantity = _decimal(quantity_gallons, field_name="quantity_gallons")
    if quantity < 0:
        raise ValueError("quantity_gallons must be non-negative")
    if unit_price_micros < 0:
        raise ValueError("unit_price_micros must be non-negative")

    subtotal = quantity * Decimal(unit_price_micros) / MICROS_PER_CENT
    return int(subtotal.quantize(_ONE, rounding=ROUND_HALF_UP))


def unit_price_usd(unit_price_micros: int) -> Decimal:
    """Return the exact decimal-dollar representation of a micro price."""

    if unit_price_micros < 0:
        raise ValueError("unit_price_micros must be non-negative")
    return Decimal(unit_price_micros) / MICROS_PER_DOLLAR
