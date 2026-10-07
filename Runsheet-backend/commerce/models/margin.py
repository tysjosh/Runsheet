"""Margin feed models, enums and exact money parsers.

This module is the single parsing layer for margin money (design "Money
arithmetic", NFR1, AC-19 to AC-23):

* Client money and gallon inputs are ``Union[StrictStr, StrictInt]``. A JSON
  float (``2.5``) never reaches a validator as a decimal, so it is rejected
  with ``type=invalid_decimal`` ("send as a string") instead of being silently
  rounded by float parsing.
* :func:`parse_usd_micros` and :func:`parse_gallons_milli` turn a decimal
  string or int into exact integer units. Extra precision is rejected, never
  rounded.
* Values read from documents (BOL ``net_gallons``, rack/contract ``*_usd``,
  order and line gallons) enter through ``Decimal(str(x))`` and are quantized
  once, half-up.
* Derived values round once each: WAC, ``margin_bp``. Everything else is
  integer arithmetic over cents and micros.

No ``float`` arithmetic happens on money here. The two ``Decimal`` divisions
(WAC and ``margin_bp``) are marked ``# margin: decimal-division`` and are the
only divisions the source-scan test allows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Dict, Iterable, List, Literal, Optional, Tuple, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)
from pydantic_core import PydanticCustomError

from services.money import MICROS_PER_CENT, MICROS_PER_DOLLAR, line_subtotal_cents


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CostEntryKind(str, Enum):
    PURCHASE = "purchase"
    OVERRIDE = "override"
    ADDER = "adder"


class AdderType(str, Enum):
    FREIGHT = "freight"
    FEE = "fee"
    OTHER = "other"


class CostEntryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    VOIDED = "voided"


class CostEntrySource(str, Enum):
    MANUAL = "manual"
    CSV_IMPORT = "csv_import"


class MarginStage(str, Enum):
    """Persisted record stages. ``quote`` (preview) is never persisted (D12)."""

    ORDER_ESTIMATE = "order_estimate"
    DELIVERY = "delivery"
    INVOICE = "invoice"


class CostMethod(str, Enum):
    OVERRIDE = "override"
    WAC = "wac"
    RACK_FALLBACK = "rack_fallback"
    NONE = "none"


class NoCostReason(str, Enum):
    PRODUCT_UNKNOWN = "product_unknown"
    NO_LOTS_NO_RACK = "no_lots_no_rack"
    RACK_STALE = "rack_stale"
    TERMINAL_UNATTRIBUTED_NO_COST = "terminal_unattributed_no_cost"
    COMPUTATION_ERROR = "computation_error"


class MarginFlag(str, Enum):
    MISSING_COST = "missing_cost"
    NEGATIVE_MARGIN = "negative_margin"
    BELOW_FLOOR = "below_floor"
    TERMINAL_UNATTRIBUTED = "terminal_unattributed"


#: Flags that put a live delivery/invoice record on the RevenueGuard queue.
ALERTING_FLAGS: Tuple[MarginFlag, ...] = (
    MarginFlag.NEGATIVE_MARGIN,
    MarginFlag.MISSING_COST,
    MarginFlag.BELOW_FLOOR,
)


class RecordStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    VOID = "void"


class RecordOrigin(str, Enum):
    LIVE = "live"
    RECOMPUTE = "recompute"


class WriteMode(str, Enum):
    """Mode argument of ``MarginRepository.write_record`` (write protocol)."""

    LIVE = "live"
    FINALIZE = "finalize"
    VOID = "void"
    RECOMPUTE = "recompute"


class AlertState(str, Enum):
    """``margin_records.alert_state``: the RevenueGuard work queue."""

    NONE = "none"
    PENDING = "pending"
    DONE = "done"
    EXPIRED = "expired"


class AlertType(str, Enum):
    NEGATIVE_MARGIN = "negative_margin"
    MISSING_COST = "missing_cost"
    LEAKAGE_PROPOSAL = "leakage_proposal"
    RECOMPUTE_DIGEST = "recompute_digest"


class AlertSeverity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


class AlertStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    DISMISSED = "dismissed"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DigestState(str, Enum):
    NONE = "none"
    PENDING = "pending"
    DONE = "done"


# ---------------------------------------------------------------------------
# Defaults and bounds
# ---------------------------------------------------------------------------

DEFAULT_WAC_WINDOW_DAYS = 30
DEFAULT_RACK_STALENESS_DAYS = 4
DEFAULT_FLOOR_MICROS = 100_000  # 10 cents per gallon (D14)
DEFAULT_TIMEZONE = "America/Chicago"

WAC_WINDOW_DAYS_MIN, WAC_WINDOW_DAYS_MAX = 1, 365
RACK_STALENESS_DAYS_MIN, RACK_STALENESS_DAYS_MAX = 1, 30
FLOOR_MICROS_MAX = 5_000_000
PRODUCT_FLOORS_MAX_KEYS = 50

MAX_UNIT_COST_MICROS = 100_000_000  # $100.000000 per gallon
MAX_GALLONS_MILLI = 1_000_000_000  # 1,000,000 gallons at 3 dp

RECOMPUTE_MAX_SPAN_DAYS = 92
REASON_MAX_LENGTH = 500

UGAL_PER_GALLON = 1_000_000
MILLI_PER_GALLON = 1_000
BP_PER_UNIT = 10_000


# ---------------------------------------------------------------------------
# Parsers (client input)
# ---------------------------------------------------------------------------


class MarginValueError(ValueError):
    """A money/gallons value failed parsing. ``type`` is the error-envelope type."""

    def __init__(self, field: str, type_: str, msg: str) -> None:
        super().__init__(f"{field}: {msg}")
        self.field = field
        self.type = type_
        self.msg = msg

    def as_error(self) -> Dict[str, Any]:
        """The ``details.errors`` item shape used by the validation envelope."""
        return {"loc": [self.field], "msg": self.msg, "type": self.type}


_USD_PATTERN = re.compile(r"^\d{1,3}(\.\d{1,12})?$", re.ASCII)
_GALLONS_PATTERN = re.compile(r"^\d{1,7}(\.\d{1,12})?$", re.ASCII)
_MICRO_QUANTUM = Decimal("0.000001")
_MILLI_QUANTUM = Decimal("0.001")
_ONE = Decimal("1")


def _parse_exact(
    value: Any,
    *,
    field: str,
    pattern: re.Pattern[str],
    quantum: Decimal,
    exponent: int,
) -> int:
    """Steps 1-3 and 5 of design "Money arithmetic"; the range check is the caller's."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MarginValueError(field, "invalid_decimal", "send as a string")
    if isinstance(value, str):
        text = value.strip()
        if not pattern.match(text):
            raise MarginValueError(field, "invalid_decimal", "not a plain decimal number")
        d = Decimal(text)
    else:
        if value < 0:
            raise MarginValueError(field, "out_of_range", "must not be negative")
        d = Decimal(value)
    try:
        exact = d == d.quantize(quantum)
    except InvalidOperation:
        raise MarginValueError(field, "out_of_range", "value too large") from None
    if not exact:
        raise MarginValueError(
            field, "too_many_decimals", f"at most {-quantum.as_tuple().exponent} decimal places"
        )
    return int(d.scaleb(exponent))


def parse_usd_micros(value: Any, *, field: str, max_micros: int) -> int:
    """Parse a USD-per-gallon decimal string (or int) into exact micros.

    ``"2.5000000"`` -> 2,500,000 (trailing zeros add no precision);
    ``"2.0000001"`` -> ``too_many_decimals``; ``"-1"``, ``"1e2"``, ``"NaN"``,
    ``"Infinity"`` -> ``invalid_decimal``; more than ``max_micros`` ->
    ``out_of_range``. Nothing is ever rounded (AC-21).
    """

    micros = _parse_exact(
        value, field=field, pattern=_USD_PATTERN, quantum=_MICRO_QUANTUM, exponent=6
    )
    if micros > max_micros:
        raise MarginValueError(field, "out_of_range", "value is above the maximum")
    return micros


def parse_gallons_milli(value: Any, *, field: str = "gallons") -> int:
    """Parse a gallons decimal string (or int) into exact milli-gallons.

    At most 3 dp, greater than 0 and at most 1,000,000 gallons.
    """

    milli = _parse_exact(
        value, field=field, pattern=_GALLONS_PATTERN, quantum=_MILLI_QUANTUM, exponent=3
    )
    if milli <= 0 or milli > MAX_GALLONS_MILLI:
        raise MarginValueError(
            field, "out_of_range", "must be greater than 0 and at most 1,000,000"
        )
    return milli


def _reject_non_decimal_input(value: Any) -> Any:
    """Pydantic before-validator: only ``str`` and ``int`` are accepted.

    A JSON number with a fraction arrives as ``float`` before any validator
    runs, so precision may already be lost; refuse it with a stable type.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PydanticCustomError("invalid_decimal", "send as a string")
    return value


#: Money / gallons field type for request models.
DecimalInput = Annotated[
    Union[StrictStr, StrictInt], BeforeValidator(_reject_non_decimal_input)
]


# ---------------------------------------------------------------------------
# Document values and derived money
# ---------------------------------------------------------------------------


def doc_decimal(value: Any, *, field: str) -> Decimal:
    """``Decimal(str(x))`` for a value read from a document; finite only."""

    if isinstance(value, bool) or value is None:
        raise MarginValueError(field, "invalid_decimal", "not a number")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise MarginValueError(field, "invalid_decimal", "not a number") from None
    if not d.is_finite():
        raise MarginValueError(field, "invalid_decimal", "not finite")
    return d


def quantize_half_up(value: Decimal) -> int:
    """Round a ``Decimal`` to an ``int``, half away from zero."""

    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP))


def usd_to_micros_half_up(value: Any, *, field: str = "price_per_gallon_usd") -> int:
    """Rack / contract ``*_usd`` document value -> micros, quantized once."""

    return quantize_half_up(doc_decimal(value, field=field).scaleb(6))


def gallons_to_ugal(value: Any, *, field: str = "gallons") -> int:
    """Order / invoice-line gallons -> micro-gallons (6 dp, half-up)."""

    return quantize_half_up(doc_decimal(value, field=field).scaleb(6))


def gallons_to_milli(value: Any, *, field: str = "net_gallons") -> int:
    """BOL net gallons -> milli-gallons (3 dp, half-up)."""

    return quantize_half_up(doc_decimal(value, field=field).scaleb(3))


def ugal_to_gallons(gallons_ugal: int) -> Decimal:
    """Exact ``Decimal`` gallons for stored micro-gallons."""

    return Decimal(gallons_ugal).scaleb(-6)


def weighted_average_micros(lots: Iterable[Tuple[int, int]]) -> Optional[int]:
    """WAC over ``(gallons_milli, unit_cost_micros)`` lots, rounded once.

    Returns ``None`` when the lots hold no gallons. AC-2:
    ``[(1_000_000, 2_500_000), (3_000_000, 2_600_000)]`` -> 2,575,000.
    """

    total_gallons_milli = 0
    total_cost = 0
    for gallons_milli, unit_cost_micros in lots:
        total_gallons_milli += int(gallons_milli)
        total_cost += int(gallons_milli) * int(unit_cost_micros)
    if total_gallons_milli <= 0:
        return None
    return quantize_half_up(
        Decimal(total_cost) / Decimal(total_gallons_milli)  # margin: decimal-division
    )


def margin_bp(margin_cents: Optional[int], revenue_cents: int) -> Optional[int]:
    """Margin in basis points, half-up; ``None`` on zero revenue or no cost."""

    if margin_cents is None or revenue_cents == 0:
        return None
    return quantize_half_up(
        Decimal(margin_cents) * BP_PER_UNIT / Decimal(revenue_cents)  # margin: decimal-division
    )


def margin_pct(bp: Optional[int]) -> Optional[str]:
    """Serialize ``margin_bp`` as a 2-dp percentage string (AC-23)."""

    if bp is None:
        return None
    return f"{Decimal(bp).scaleb(-2):.2f}"


@dataclass(frozen=True)
class MarginAmounts:
    """Cost-derived money fields of one record; all ``None`` when cost is missing."""

    cost_cents: Optional[int]
    margin_cents: Optional[int]
    margin_per_gallon_micros: Optional[int]
    margin_bp: Optional[int]


def compute_margin_amounts(
    *,
    gallons_ugal: int,
    unit_price_micros: int,
    revenue_cents: int,
    landed_cost_micros: Optional[int],
) -> MarginAmounts:
    """Money fields per design "Money arithmetic".

    ``cost_cents`` reuses :func:`services.money.line_subtotal_cents` (AC-20);
    margin values are integer subtraction. A missing landed cost gives
    ``None`` everywhere, never 0 (AC-8).
    """

    if landed_cost_micros is None:
        return MarginAmounts(None, None, None, None)
    cost_cents = line_subtotal_cents(ugal_to_gallons(gallons_ugal), landed_cost_micros)
    margin_cents = revenue_cents - cost_cents
    return MarginAmounts(
        cost_cents=cost_cents,
        margin_cents=margin_cents,
        margin_per_gallon_micros=unit_price_micros - landed_cost_micros,
        margin_bp=margin_bp(margin_cents, revenue_cents),
    )


# ---------------------------------------------------------------------------
# Request models (extra="forbid": a body tenant_id is a 422, AC-25)
# ---------------------------------------------------------------------------

_Id128 = Annotated[StrictStr, Field(min_length=1, max_length=128)]
_Text128 = Annotated[StrictStr, Field(max_length=128)]
_Reason = Annotated[StrictStr, Field(min_length=1, max_length=REASON_MAX_LENGTH)]


class _MarginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CostEntryCreate(_MarginRequest):
    """``POST /cost-entries`` body. Semantic checks live in the entry service."""

    kind: CostEntryKind
    product_code: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    terminal_id: Optional[_Id128] = None
    supplier_name: Optional[_Text128] = None
    effective_at: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    effective_to: Optional[Annotated[StrictStr, Field(min_length=1, max_length=64)]] = None
    unit_cost_usd: DecimalInput
    gallons: Optional[DecimalInput] = None
    adder_type: Optional[AdderType] = None
    bol_id: Optional[_Id128] = None
    reference: Optional[_Text128] = None
    notes: Optional[Annotated[StrictStr, Field(max_length=500)]] = None


class CostEntrySupersede(CostEntryCreate):
    """``POST /cost-entries/{id}/supersede``: the full new entry plus a reason."""

    reason: _Reason


class CostEntryVoid(_MarginRequest):
    reason: _Reason


class MarginSettingsUpdate(_MarginRequest):
    """``PUT /settings``. USD fields are decimal strings (or ints)."""

    wac_window_days: Annotated[
        StrictInt, Field(ge=WAC_WINDOW_DAYS_MIN, le=WAC_WINDOW_DAYS_MAX)
    ] = DEFAULT_WAC_WINDOW_DAYS
    rack_staleness_days: Annotated[
        StrictInt, Field(ge=RACK_STALENESS_DAYS_MIN, le=RACK_STALENESS_DAYS_MAX)
    ] = DEFAULT_RACK_STALENESS_DAYS
    floor_usd_per_gallon: DecimalInput = "0.100000"
    product_floors: Annotated[
        Dict[Annotated[StrictStr, Field(min_length=1, max_length=64)], DecimalInput],
        Field(max_length=PRODUCT_FLOORS_MAX_KEYS),
    ] = Field(default_factory=dict)
    timezone: Annotated[StrictStr, Field(min_length=1, max_length=64)] = DEFAULT_TIMEZONE


class MarginPreviewRequest(_MarginRequest):
    """``POST /preview``: exactly one of ``unit_price_usd`` and ``customer_id``."""

    product_code: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    gallons: DecimalInput
    terminal_id: Optional[_Id128] = None
    as_of: Optional[AwareDatetime] = None
    unit_price_usd: Optional[DecimalInput] = None
    customer_id: Optional[_Id128] = None
    account_id: Optional[_Id128] = None

    @model_validator(mode="after")
    def _one_price_source(self) -> "MarginPreviewRequest":
        if (self.unit_price_usd is None) == (self.customer_id is None):
            raise ValueError("set exactly one of unit_price_usd and customer_id")
        if self.account_id is not None and self.customer_id is None:
            raise ValueError("account_id requires customer_id")
        return self


class MarginRecomputeRequest(_MarginRequest):
    """``POST /recompute``. Dates are ``as_of`` dates in the settings timezone."""

    start_date: date
    end_date: date
    stages: Annotated[
        List[Literal["invoice", "delivery"]], Field(min_length=1, max_length=2)
    ] = Field(default_factory=lambda: ["invoice", "delivery"])
    only_missing: StrictBool = True
    reason: _Reason

    @model_validator(mode="after")
    def _range(self) -> "MarginRecomputeRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (self.end_date - self.start_date).days + 1 > RECOMPUTE_MAX_SPAN_DAYS:
            raise ValueError(f"the range may span at most {RECOMPUTE_MAX_SPAN_DAYS} days")
        return self


class MarginAlertAction(_MarginRequest):
    """Body of acknowledge / approve / dismiss."""

    note: Optional[Annotated[StrictStr, Field(max_length=500)]] = None


__all__ = [
    "ALERTING_FLAGS",
    "AdderType",
    "AlertSeverity",
    "AlertState",
    "AlertStatus",
    "AlertType",
    "BP_PER_UNIT",
    "CostEntryCreate",
    "CostEntryKind",
    "CostEntrySource",
    "CostEntryStatus",
    "CostEntrySupersede",
    "CostEntryVoid",
    "CostMethod",
    "DEFAULT_FLOOR_MICROS",
    "DEFAULT_RACK_STALENESS_DAYS",
    "DEFAULT_TIMEZONE",
    "DEFAULT_WAC_WINDOW_DAYS",
    "DecimalInput",
    "DigestState",
    "FLOOR_MICROS_MAX",
    "MAX_GALLONS_MILLI",
    "MAX_UNIT_COST_MICROS",
    "MICROS_PER_CENT",
    "MICROS_PER_DOLLAR",
    "MILLI_PER_GALLON",
    "MarginAlertAction",
    "MarginAmounts",
    "MarginFlag",
    "MarginPreviewRequest",
    "MarginRecomputeRequest",
    "MarginSettingsUpdate",
    "MarginStage",
    "MarginValueError",
    "NoCostReason",
    "PRODUCT_FLOORS_MAX_KEYS",
    "RECOMPUTE_MAX_SPAN_DAYS",
    "RecordOrigin",
    "RecordStatus",
    "RunStatus",
    "UGAL_PER_GALLON",
    "WriteMode",
    "compute_margin_amounts",
    "doc_decimal",
    "gallons_to_milli",
    "gallons_to_ugal",
    "margin_bp",
    "margin_pct",
    "parse_gallons_milli",
    "parse_usd_micros",
    "quantize_half_up",
    "ugal_to_gallons",
    "usd_to_micros_half_up",
    "weighted_average_micros",
]
