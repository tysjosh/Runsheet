"""Typed ``start_date`` / ``end_date`` query parameters (data-export G1/G2/G3).

One parser shared by the reconciliation and invoice lists, the reconciliation
and invoice exports, and the admin HOS/DVIR lists, so every endpoint that gains
a date range validates it the same way and answers 422 on bad input.

Rules (design §1 "Date range parser"):

- Each bound is optional. Both omitted gives an all-``None`` range and callers
  add no clause, which keeps today's behavior.
- Values are whitespace-stripped and at most 40 characters.
- ``YYYY-MM-DD`` or an ISO-8601 datetime (a trailing ``Z`` is allowed).
  Naive datetimes are UTC; aware datetimes are converted to UTC.
- A date-only ``start_date`` is midnight UTC, inclusive (``gte``).
- A date-only ``end_date`` becomes ``lt`` = the next midnight UTC, so the whole
  named day is included whatever the stored timestamp's precision.
- A datetime ``end_date`` is an inclusive ``lte``.
- ``start > end`` (comparing the effective bounds) is a 422.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from errors.codes import ErrorCode
from errors.exceptions import AppException

_MAX_LEN = 40


@dataclass(frozen=True)
class DateRange:
    """A UTC range. At most one of ``lt`` / ``lte`` is set."""

    gte: Optional[datetime] = None
    lt: Optional[datetime] = None
    lte: Optional[datetime] = None

    @property
    def is_empty(self) -> bool:
        return self.gte is None and self.lt is None and self.lte is None


def _invalid(field: str, value: str) -> AppException:
    return AppException(
        error_code=ErrorCode.VALIDATION_ERROR,
        message=f"{field} must be YYYY-MM-DD or an ISO-8601 datetime",
        status_code=422,
        details={"field": field, "value": value},
    )


def _parse_one(field: str, raw: Optional[str]) -> tuple[Optional[datetime], bool]:
    """Return ``(utc_datetime, is_date_only)``, or ``(None, False)`` when absent."""
    if raw is None:
        return None, False
    value = raw.strip()
    if not value:
        return None, False
    if len(value) > _MAX_LEN:
        raise _invalid(field, value[:_MAX_LEN])
    if len(value) == 10:
        try:
            d = date.fromisoformat(value)
        except ValueError:
            raise _invalid(field, value) from None
        return datetime.combine(d, time.min, tzinfo=timezone.utc), True
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        raise _invalid(field, value) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc), False


def parse_date_range(start_date: Optional[str], end_date: Optional[str]) -> DateRange:
    """Parse and validate an optional ``start_date`` / ``end_date`` pair.

    Raises:
        AppException: 422 ``VALIDATION_ERROR`` with ``details={"field", "value"}``
            on an unparseable or over-long value, or
            ``details={"field": "start_date", "reason": "after_end_date"}`` when
            the start is after the end.
    """
    start, _ = _parse_one("start_date", start_date)
    end, end_date_only = _parse_one("end_date", end_date)
    lt = end + timedelta(days=1) if end is not None and end_date_only else None
    lte = end if end is not None and not end_date_only else None
    if start is not None:
        if (lt is not None and start >= lt) or (lte is not None and start > lte):
            raise AppException(
                error_code=ErrorCode.VALIDATION_ERROR,
                message="start_date must not be after end_date",
                status_code=422,
                details={"field": "start_date", "reason": "after_end_date"},
            )
    return DateRange(gte=start, lt=lt, lte=lte)


def to_doc_bound(dt: datetime, *, z_suffix: bool, op: str = "gte") -> str:
    """Format a bound for a document-store ``range`` clause.

    The document store compares timestamps as text, so the bound must sort
    against the stored form exactly as the instant does.

    ``z_suffix=False``: stored values are ``datetime.isoformat()``
    (``...SS+00:00`` or ``...SS.ffffff+00:00``). ``+`` sorts before ``.``, so
    that form is already in chronological text order and the bound is plain
    ``isoformat()``.

    ``z_suffix=True``: stored values are pydantic ``model_dump(mode="json")``,
    which writes ``...SSZ`` when the microsecond is 0 and ``...SS.ffffffZ``
    otherwise. ``Z`` sorts AFTER ``.``, so a stored ``SSZ`` behaves like
    ``SS.999…``. A ``gte`` / ``gt`` / ``lt`` bound therefore always carries a
    6-digit fraction (``SS.000000Z``), which compares correctly against both
    stored forms. An inclusive ``lte`` uses the pydantic form so a stored
    value equal to the bound is included; the only imprecision left is that a
    stored ``SS.ffffffZ`` within the bound's own second is also included.
    """
    utc = dt.astimezone(timezone.utc)
    if not z_suffix:
        return utc.isoformat()
    if op == "lte":
        return utc.isoformat().replace("+00:00", "Z")
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def doc_range_clause(field: str, date_range: DateRange, *, z_suffix: bool) -> Optional[dict]:
    """Build a ``{"range": {field: {...}}}`` clause, or ``None`` for an empty range."""
    if date_range.is_empty:
        return None
    bounds: dict[str, str] = {}
    if date_range.gte is not None:
        bounds["gte"] = to_doc_bound(date_range.gte, z_suffix=z_suffix, op="gte")
    if date_range.lt is not None:
        bounds["lt"] = to_doc_bound(date_range.lt, z_suffix=z_suffix, op="lt")
    if date_range.lte is not None:
        bounds["lte"] = to_doc_bound(date_range.lte, z_suffix=z_suffix, op="lte")
    return {"range": {field: bounds}}
