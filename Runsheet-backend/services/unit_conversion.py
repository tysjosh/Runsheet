"""
Unit conversion helpers — canonical gallons + miles, display units per tenant.

This module is the single source of truth for converting between the tenant's
display units (gallons/liters for volume, miles/kilometers for distance) and
the platform's canonical storage units. Capability 6 (Requirement 6.3) mandates
that API responses and UI screens present volumes and distances in the tenant's
configured ``measurement_units`` while the platform persists canonical values
internally.

Canonical units:
    * Volume   → gallons (``gal``)
    * Distance → miles   (``mi``)

Conversion factors (exact, per NIST Handbook 44):
    * ``GAL_TO_L = 3.785411784`` — US liquid gallon to liter
    * ``MI_TO_KM = 1.609344``    — international mile to kilometer

Design properties:

* **Idempotence** — converting a canonical value to its canonical unit is a
  no-op: ``to_canonical_volume(x, "gal") == x``.
* **Round-trip accuracy** — for any finite non-negative value ``x`` and a
  supported unit ``u``, ``from_canonical_*(to_canonical_*(x, u), u)`` returns
  to within 1e-9 relative tolerance (Requirement 6.3.4).
* **Case and whitespace tolerance** — unit strings are normalized before
  dispatch so ``"Gal"``, ``" L "`` and ``"LITERS"`` all resolve correctly.

Validates: Requirement 6.3.3.
"""
from __future__ import annotations

from typing import Dict, Final


# ---------------------------------------------------------------------------
# Conversion factors
# ---------------------------------------------------------------------------


#: US liquid gallons → liters (exact, NIST Handbook 44).
GAL_TO_L: Final[float] = 3.785411784

#: International miles → kilometers (exact, NIST Handbook 44).
MI_TO_KM: Final[float] = 1.609344


# ---------------------------------------------------------------------------
# Unit normalization
# ---------------------------------------------------------------------------


#: Accepted aliases for each supported volume unit. Canonical for volume is
#: ``gal``; ``l`` names the liter branch.
_VOLUME_ALIASES: Final[Dict[str, str]] = {
    "gal": "gal",
    "gallon": "gal",
    "gallons": "gal",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
}


#: Accepted aliases for each supported distance unit. Canonical for distance
#: is ``mi``; ``km`` names the kilometer branch and ``m`` the meter branch.
_DISTANCE_ALIASES: Final[Dict[str, str]] = {
    "mi": "mi",
    "mile": "mi",
    "miles": "mi",
    "km": "km",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
}


class UnknownUnitError(ValueError):
    """Raised when a caller passes a unit string the module does not recognize.

    Subclass of :class:`ValueError` so callers that only catch the standard
    exception still handle the error path.
    """

    def __init__(self, unit: str, kind: str) -> None:
        super().__init__(f"unknown {kind} unit: {unit!r}")
        self.unit = unit
        self.kind = kind


def _normalize_volume_unit(unit: str) -> str:
    """Return the canonical volume-unit key (``"gal"`` or ``"l"``)."""
    if not isinstance(unit, str):
        raise TypeError(
            f"unit must be a string, got {type(unit).__name__}"
        )
    key = unit.strip().lower()
    try:
        return _VOLUME_ALIASES[key]
    except KeyError as exc:
        raise UnknownUnitError(unit, "volume") from exc


def _normalize_distance_unit(unit: str) -> str:
    """Return the canonical distance-unit key (``"mi"``, ``"km"``, or ``"m"``)."""
    if not isinstance(unit, str):
        raise TypeError(
            f"unit must be a string, got {type(unit).__name__}"
        )
    key = unit.strip().lower()
    try:
        return _DISTANCE_ALIASES[key]
    except KeyError as exc:
        raise UnknownUnitError(unit, "distance") from exc


def _check_finite_number(value: float, name: str) -> float:
    """Reject NaN / inf / non-numeric inputs early with a clear error.

    Booleans are rejected explicitly because ``bool`` is a subclass of ``int``
    in Python and passing ``True`` here almost always indicates a bug.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"{name} must be an int or float, got {type(value).__name__}"
        )
    as_float = float(value)
    # NaN fails the self-equality check; +/-inf are explicitly rejected too.
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return as_float


# ---------------------------------------------------------------------------
# Volume helpers (canonical = gallons)
# ---------------------------------------------------------------------------


def to_canonical_volume(value: float, unit: str) -> float:
    """Convert ``value`` expressed in ``unit`` to canonical gallons.

    ``unit`` may be any of ``"gal"``, ``"l"``, ``"L"``, ``"liter"`` or
    ``"liters"`` (case- and whitespace-insensitive). Gallons pass through
    unchanged, making the function idempotent when the input is already
    canonical.

    Raises:
        UnknownUnitError: for unrecognized units.
        TypeError: if ``value`` is not numeric or ``unit`` is not a string.
        ValueError: if ``value`` is NaN or infinite.
    """
    as_float = _check_finite_number(value, "value")
    canonical = _normalize_volume_unit(unit)
    if canonical == "gal":
        return as_float
    # canonical == "l"
    return as_float / GAL_TO_L


def from_canonical_volume(value: float, unit: str) -> float:
    """Convert ``value`` from canonical gallons into ``unit``.

    The inverse of :func:`to_canonical_volume`. For the same ``unit``, the pair
    satisfies ``from_canonical_volume(to_canonical_volume(x, u), u) == x``
    within 1e-9 relative tolerance (Requirement 6.3.4).
    """
    as_float = _check_finite_number(value, "value")
    canonical = _normalize_volume_unit(unit)
    if canonical == "gal":
        return as_float
    # canonical == "l"
    return as_float * GAL_TO_L


# ---------------------------------------------------------------------------
# Distance helpers (canonical = miles)
# ---------------------------------------------------------------------------


def to_canonical_distance(value: float, unit: str) -> float:
    """Convert ``value`` expressed in ``unit`` to canonical miles.

    ``unit`` may be any of ``"mi"``, ``"km"`` or ``"m"`` (case- and
    whitespace-insensitive). Miles pass through unchanged, making the function
    idempotent when the input is already canonical.

    Raises:
        UnknownUnitError: for unrecognized units.
        TypeError: if ``value`` is not numeric or ``unit`` is not a string.
        ValueError: if ``value`` is NaN or infinite.
    """
    as_float = _check_finite_number(value, "value")
    canonical = _normalize_distance_unit(unit)
    if canonical == "mi":
        return as_float
    if canonical == "km":
        return as_float / MI_TO_KM
    # canonical == "m" — convert metres to kilometres first, then to miles.
    return (as_float / 1000.0) / MI_TO_KM


def from_canonical_distance(value: float, unit: str) -> float:
    """Convert ``value`` from canonical miles into ``unit``.

    The inverse of :func:`to_canonical_distance`. For the same ``unit``, the
    pair satisfies ``from_canonical_distance(to_canonical_distance(x, u), u)``
    ``== x`` within 1e-9 relative tolerance (Requirement 6.3.4).
    """
    as_float = _check_finite_number(value, "value")
    canonical = _normalize_distance_unit(unit)
    if canonical == "mi":
        return as_float
    if canonical == "km":
        return as_float * MI_TO_KM
    # canonical == "m"
    return as_float * MI_TO_KM * 1000.0


__all__ = [
    "GAL_TO_L",
    "MI_TO_KM",
    "UnknownUnitError",
    "to_canonical_volume",
    "from_canonical_volume",
    "to_canonical_distance",
    "from_canonical_distance",
]
