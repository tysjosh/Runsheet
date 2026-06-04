"""
Terminal / rack sourcing domain models and tenant-scoped repositories.

Capability 8 of the fuel-ops hardening spec introduces four first-class
entities so the platform can recommend the best loading terminal per
truck-run:

* :class:`Terminal` — physical fuel-loading rack (Kinder Morgan, Buckeye,
  Magellan, custom). Persisted to the ``terminals`` ES index.
  (Requirement 8.1.1)
* :class:`SupplierContract` — tenant's purchasing commitment with a
  supplier at one or more terminals, including volume minimums and
  branded/unbranded constraints. Persisted to ``supplier_contracts``.
  (Requirement 8.3.1)
* :class:`TerminalWaitReport` — a single wait-time observation at a
  terminal (driver-submitted, ELD-geofence-derived, or connector-imported).
  Persisted to ``terminal_wait_reports``. (Requirement 8.4.1)
* :class:`SourcingRecommendation` — the ranked terminal list produced by
  the Sourcing_Recommender for a given product + volume + origin +
  as_of request. Persisted to ``sourcing_recommendations`` for audit.
  (Requirement 8.5.4)

Each Pydantic model mirrors its ES mapping (see
:mod:`fuel.services.fuel_ops_es_mappings`) 1:1 so a ``model_dump()``
payload can be indexed directly without transformation. Every catalog-
backed ``product_code`` is canonicalized on write through
:func:`fuel.services.fuel_product_catalog.canonicalize` so legacy
Nigerian aliases (AGO, PMS, ATK, LPG) are normalized to their US
equivalents (DIESEL_2, GASOLINE_REG, KEROSENE, PROPANE) before
persistence (Requirement 6.1.4).

Each entity ships with a matching Repository class that exposes async
``create`` / ``get`` / ``list_for_tenant`` / ``update`` / ``delete``
against the corresponding ES index. Tenant isolation is enforced at two
points for defense-in-depth:

    1. Every ES query includes a ``term`` clause on ``tenant_id``.
    2. Every returned ``_source`` is re-validated against the caller's
       ``tenant_id`` before it crosses the repository boundary — a
       mis-labelled document never leaks across tenants.

Cross-tenant reads degrade silently to ``None`` (so REST layers return
HTTP 404 without leaking existence). Cross-tenant writes / deletes
raise :class:`CrossTenantAccessError` (a ``PermissionError`` subclass),
which middleware maps to HTTP 403.

Validates: Requirements 8.1.1, 8.3.1, 8.4.1, 8.5.4.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple, Type, TypeVar
from uuid import uuid4

try:  # pragma: no cover - zoneinfo ships with Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):
        """Fallback stub when zoneinfo is unavailable."""


from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fuel.services.fuel_ops_es_mappings import (
    SOURCING_RECOMMENDATIONS_INDEX,
    SUPPLIER_CONTRACTS_INDEX,
    TERMINALS_INDEX,
    TERMINAL_WAIT_REPORTS_INDEX,
)
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
)

logger = logging.getLogger(__name__)


# Maps a base-repo ``entity_type`` to the persistence-layer aggregate type for
# the Postgres dual-write mirror. Only entities being migrated are listed; all
# others (terminal_wait_reports, sourcing_recommendations, …) are skipped.
_BASE_REPO_MIRROR_AGGREGATES = {
    "terminal": "terminal",
    "supplier_contract": "supplier_contract",
}


# ---------------------------------------------------------------------------
# Shared enums / constants
# ---------------------------------------------------------------------------


#: Source-of-truth status for Terminal and SupplierContract. Kept consistent
#: across entities so filtering APIs have one surface to reason about.
ActiveStatus = Literal["active", "inactive"]


#: ISO day-of-week short codes used in Terminal.operating_hours. Lowercase
#: three-letter codes make the data compact in ES and unambiguous to parse.
DayOfWeek = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


#: Where a :class:`TerminalWaitReport` came from — required by Requirement
#: 8.4.1 so downstream analytics can weight or exclude specific sources.
WaitReportSource = Literal["driver_report", "eld_geofence", "connector_import"]


# Match "HH:MM" 24-hour clock values used in Terminal.operating_hours.
_TIME_HH_MM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


#: Short codes for day-of-week lookup indexed by ``datetime.weekday()``.
#: Must match :data:`DayOfWeek` so :meth:`Terminal.is_open_at` can look
#: up the right window for an ``as_of`` datetime. Kept at module scope
#: so both the model method and any helper (e.g. the
#: ``proposed-load`` endpoint's next-open-window walk) share the same
#: source of truth.
_DAY_OF_WEEK_CODES: Tuple[str, ...] = (
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
)


# ---------------------------------------------------------------------------
# Terminal model
# ---------------------------------------------------------------------------


class OperatingHours(BaseModel):
    """A single day's open/close window for a terminal.

    Matches the nested ``operating_hours`` shape in the ``terminals`` ES
    mapping. Open/close are ``HH:MM`` strings in the terminal's local
    timezone (held on the parent :class:`Terminal`). Closed days are
    represented by omitting the entry rather than a sentinel value, so
    consumers never have to special-case ``"00:00"-"00:00"``.
    """

    model_config = ConfigDict(extra="forbid")

    day_of_week: DayOfWeek
    open: str = Field(..., description="Local-time open, HH:MM 24-hour.")
    close: str = Field(..., description="Local-time close, HH:MM 24-hour.")

    @field_validator("open", "close")
    @classmethod
    def _check_time_format(cls, value: str) -> str:
        if not _TIME_HH_MM.match(value):
            raise ValueError(
                f"operating_hours time must be HH:MM 24-hour format, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _check_open_before_close(self) -> "OperatingHours":
        # Allow 24-hour terminals to use "00:00"/"23:59"; only reject ranges
        # where close ≤ open, which would encode an empty / negative window.
        if self.close <= self.open:
            raise ValueError(
                f"operating_hours close ({self.close}) must be after open ({self.open}); "
                "use a single-day window and rely on the tenant-local timezone."
            )
        return self


class Terminal(BaseModel):
    """A physical fuel-loading rack operated by a supplier.

    Field shapes mirror the ``terminals`` ES mapping (Task 1.1) so a
    ``model_dump()`` payload can be indexed directly. The geo_point
    ``location`` field declared in the mapping is intentionally not
    exposed on the model — the repository derives it on write from
    ``location_lat`` / ``location_lon`` so callers do not have to
    maintain two representations of the same coordinate.
    """

    model_config = ConfigDict(extra="forbid")

    terminal_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    operator: str = Field(
        ...,
        min_length=1,
        description="Rack operator brand, e.g. Buckeye, Kinder Morgan, Magellan.",
    )
    location_lat: float = Field(..., ge=-90.0, le=90.0)
    location_lon: float = Field(..., ge=-180.0, le=180.0)
    address: str = Field(..., min_length=1)
    timezone: str = Field(
        ...,
        min_length=1,
        description=(
            "IANA timezone for operating_hours (e.g. America/New_York). "
            "Stored as-is; conversion to the dispatcher's timezone happens "
            "at the API / UI layer."
        ),
    )
    operating_hours: List[OperatingHours] = Field(
        default_factory=list,
        description=(
            "One entry per open day. Closed days are simply omitted. "
            "Overlapping entries for the same day are rejected by the "
            "model validator so Terminal.is_open_at has unambiguous input."
        ),
    )
    supported_products: List[str] = Field(
        default_factory=list,
        description=(
            "Canonical product_codes this terminal can load. Always "
            "canonicalized on write so legacy aliases are normalized."
        ),
    )
    branded: bool = Field(default=False)
    supplier_brand: Optional[str] = Field(
        default=None,
        description=(
            "Brand name when ``branded`` is True (e.g. Shell, Exxon). "
            "Enforced by the model validator: branded terminals must "
            "carry a supplier_brand, unbranded terminals must not."
        ),
    )
    status: ActiveStatus = "active"
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator(
        "terminal_id", "tenant_id", "name", "operator", "address", "timezone"
    )
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("supported_products", mode="before")
    @classmethod
    def _canonicalize_supported_products(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            return value  # let Pydantic raise a clear type error
        out: List[str] = []
        seen: set[str] = set()
        for entry in value:
            if not isinstance(entry, str):
                return value  # let Pydantic raise a clear type error
            canonical = canonicalize(entry)
            if canonical not in seen:
                seen.add(canonical)
                out.append(canonical)
        return out

    @field_validator("supplier_brand")
    @classmethod
    def _strip_supplier_brand(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _check_branded_supplier_brand(self) -> "Terminal":
        if self.branded and not self.supplier_brand:
            raise ValueError(
                "branded=True requires supplier_brand to be set"
            )
        if not self.branded and self.supplier_brand is not None:
            raise ValueError(
                "supplier_brand must be omitted when branded=False"
            )
        return self

    @model_validator(mode="after")
    def _check_unique_operating_days(self) -> "Terminal":
        seen: set[str] = set()
        for hours in self.operating_hours:
            if hours.day_of_week in seen:
                raise ValueError(
                    f"operating_hours contains duplicate entry for "
                    f"{hours.day_of_week!r}; collapse into a single window."
                )
            seen.add(hours.day_of_week)
        return self

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_open_at(self, as_of: datetime) -> bool:
        """Return ``True`` when this terminal is open at ``as_of``.

        :attr:`operating_hours` entries are declared in the terminal's
        local timezone (:attr:`timezone`). This method converts the
        supplied ``as_of`` into that zone, looks up the matching
        day-of-week window, and checks the ``HH:MM`` range using an
        inclusive-lower / exclusive-upper comparison so ``close="22:00"``
        does not incorrectly report open at exactly 22:00.

        Closed days are encoded by *omission* of an entry for that day
        (see :meth:`_check_unique_operating_days`), so a missing day
        always returns ``False``.

        Defensive on two edges that would otherwise break sourcing or
        the proposed-load validator on misconfigured data:

        * **Empty ``operating_hours``** — treated as 24/7 open. This is
          the "operator did not constrain availability yet" posture we
          use for newly-created terminals; the Req 8.1.4 proposed-load
          validator separately surfaces a distinct reason when a
          terminal has not yet been populated with a schedule.
        * **Unknown timezone string** — degrades to ``True`` with a
          warning log. Refusing to load because an operator typo'd an
          IANA name would be a worse failure than the tiny chance of
          recommending a closed terminal; the sourcing path logs the
          warning so ops can chase the typo.
        * **Naive ``as_of``** — assumed to be UTC so callers that drop
          in a ``datetime.utcnow()`` do not get a surprising False
          from timezone math.

        Validates: Requirement 8.1.4.
        """

        if not self.operating_hours:
            return True

        local = _to_local_datetime(as_of, self.timezone)
        if local is None:
            # Unknown / unresolvable timezone. Degrade to "open" with a
            # warning rather than refusing a load because an operator
            # typo'd the IANA name — the warning logged inside
            # ``_to_local_datetime`` makes the misconfiguration
            # observable to ops.
            return True
        day_code = _DAY_OF_WEEK_CODES[local.weekday()]
        hhmm = local.strftime("%H:%M")
        for window in self.operating_hours:
            if window.day_of_week != day_code:
                continue
            if window.open <= hhmm < window.close:
                return True
        return False


# ---------------------------------------------------------------------------
# SupplierContract model
# ---------------------------------------------------------------------------


class SupplierContract(BaseModel):
    """A tenant's purchasing commitment at one or more terminals.

    When ``contract_price_per_gallon_usd`` is set it overrides the live
    rack price in Sourcing_Recommendations (Requirement 8.3.3). The
    ``minimum_lift_gallons_per_month`` drives the monthly rolling-lift
    counter surfaced in the admin UI.
    """

    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    supplier_name: str = Field(..., min_length=1)
    product_code: str = Field(
        ...,
        min_length=1,
        description=(
            "Canonical catalog product_code. Aliases (AGO, PMS, ATK, LPG) "
            "are canonicalized to DIESEL_2 / GASOLINE_REG / KEROSENE / "
            "PROPANE at construction time."
        ),
    )
    preferred_terminal_ids: List[str] = Field(default_factory=list)
    contract_price_per_gallon_usd: Optional[float] = Field(default=None, ge=0)
    branded_required: bool = Field(default=False)
    minimum_lift_gallons_per_month: Optional[float] = Field(default=None, ge=0)
    rebate_terms: Optional[str] = None
    effective_from: date
    effective_to: Optional[date] = None
    status: ActiveStatus = "active"
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("product_code", mode="before")
    @classmethod
    def _canonicalize_product_code(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        return canonicalize(value)

    @field_validator("contract_id", "tenant_id", "supplier_name")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("preferred_terminal_ids", mode="before")
    @classmethod
    def _dedupe_terminal_ids(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            return value
        seen: set[str] = set()
        out: List[str] = []
        for entry in value:
            if not isinstance(entry, str):
                return value
            stripped = entry.strip()
            if not stripped:
                continue
            if stripped not in seen:
                seen.add(stripped)
                out.append(stripped)
        return out

    @model_validator(mode="after")
    def _check_effective_window(self) -> "SupplierContract":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(
                "effective_to must be on or after effective_from"
            )
        return self


# ---------------------------------------------------------------------------
# TerminalWaitReport model
# ---------------------------------------------------------------------------


class TerminalWaitReport(BaseModel):
    """A single wait-time observation at a terminal.

    Wait reports feed both the rolling 2-hour average surfaced on
    ``GET /api/fuel/terminals/{id}/wait-summary`` and the ``avg_wait_minutes``
    scored by the Sourcing_Recommender (Requirement 8.4.4, 8.4.5).

    ``reporter_id`` is required for ``driver_report`` submissions (so we
    can attribute the report) and optional for ``eld_geofence`` and
    ``connector_import`` sources where the derivation is automatic.
    """

    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    terminal_id: str = Field(..., min_length=1)
    wait_minutes: float = Field(..., ge=0)
    source: WaitReportSource
    reporter_id: Optional[str] = Field(
        default=None,
        description=(
            "User id for driver_report submissions. Required when source "
            "is driver_report; optional otherwise."
        ),
    )
    truck_id: Optional[str] = Field(
        default=None,
        description="Truck id for ELD-derived reports.",
    )
    observed_at: datetime = Field(
        ...,
        description=(
            "When the wait was observed (driver submitted time or geofence "
            "exit time). Distinct from retrieved_at so late-arriving "
            "telemetry can still be attributed to the correct 2-hour "
            "rolling-average window."
        ),
    )
    retrieved_at: datetime = Field(
        ...,
        description=(
            "When the platform recorded the observation. Populated by the "
            "repository if missing, so callers only need to supply "
            "observed_at."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=1000,
        description=(
            "Optional free-form dispatcher / driver note explaining the "
            "observation (why the wait was long, bottleneck cause, etc). "
            "Capped at 1000 chars; strip-on-write via the validator below."
        ),
    )
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("report_id", "tenant_id", "terminal_id")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("reporter_id", "truck_id", "notes")
    @classmethod
    def _strip_optional_strings(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _check_reporter_for_driver_submissions(self) -> "TerminalWaitReport":
        if self.source == "driver_report" and not self.reporter_id:
            raise ValueError(
                "reporter_id is required when source is 'driver_report'"
            )
        if self.retrieved_at < self.observed_at:
            raise ValueError(
                "retrieved_at must be on or after observed_at"
            )
        return self


# ---------------------------------------------------------------------------
# SourcingRecommendation model
# ---------------------------------------------------------------------------


class TerminalCandidate(BaseModel):
    """One ranked terminal within a :class:`SourcingRecommendation`.

    Matches the nested ``candidates`` shape of the
    ``sourcing_recommendations`` ES mapping. The ``score`` is a
    normalized 0.0–1.0 ranking score; the ``reasons`` list carries the
    human-readable explanation surfaced in the dispatcher UI.
    """

    model_config = ConfigDict(extra="forbid")

    terminal_id: str = Field(..., min_length=1)
    price_per_gallon_usd: float = Field(..., ge=0)
    branded_flag: bool = Field(default=False)
    contract_id: Optional[str] = Field(default=None)
    avg_wait_minutes: float = Field(..., ge=0)
    distance_km_from_start: float = Field(..., ge=0)
    score: float = Field(..., ge=0.0, le=1.0)
    reasons: List[str] = Field(default_factory=list)
    wait_warning: bool = Field(
        default=False,
        description=(
            "True when avg_wait_minutes exceeds the tenant-configured "
            "terminal_wait_warning_minutes threshold (Requirement 8.4.5)."
        ),
    )

    @field_validator("terminal_id")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("terminal_id must not be blank")
        return stripped

    @field_validator("contract_id")
    @classmethod
    def _strip_contract_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class SourcingRecommendation(BaseModel):
    """A persisted audit record of a Sourcing_Recommender invocation.

    Written by ``GET /api/fuel/sourcing/recommendations`` and consulted
    by the Route_Planning_Agent when a Loading_Plan requires a non-depot
    lift. ``candidates`` is ordered by ``score`` descending when the
    recommender finishes ranking.

    **wait_warning annotation (Task 7.11 / Requirement 8.4.5).** The
    Sourcing_Recommender stamps ``TerminalCandidate.wait_warning=True``
    whenever a candidate's rolling 2-hour ``avg_wait_minutes`` exceeds
    the tenant's ``terminal_wait_warning_minutes`` threshold (Redis key
    ``terminal_wait_warning_minutes:{tenant_id}``, default 60). The
    same threshold drives the ``wait_warning_exceeded`` flag returned
    by ``GET /api/fuel/terminals/{terminal_id}/wait-summary`` so both
    surfaces stay in lock-step.

    At the recommendation level we also surface
    :attr:`wait_warning_terminal_ids` — a computed list of every
    candidate ``terminal_id`` whose ``wait_warning`` flag is true. The
    dispatcher UI can render a "terminal wait warning" banner by
    checking ``len(wait_warning_terminal_ids) > 0`` without having to
    traverse every candidate, and the audit trail in
    ``sourcing_recommendations`` carries the same summary so post-hoc
    queries on wait-warning exposure are a single ES filter instead of
    a nested candidate scan.
    """

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    truck_id: Optional[str] = Field(default=None)
    run_id: Optional[str] = Field(default=None)
    product_code: str = Field(..., min_length=1)
    volume_gallons: float = Field(..., gt=0)
    origin_lat: float = Field(..., ge=-90.0, le=90.0)
    origin_lon: float = Field(..., ge=-180.0, le=180.0)
    candidates: List[TerminalCandidate] = Field(default_factory=list)
    rack_price_fallback: bool = Field(
        default=False,
        description=(
            "True when the recommender fell back to the most recent "
            "cached rack price because the live provider timed out "
            "(Requirement 8.2.5)."
        ),
    )
    # Top-level wait-warning summary (Task 7.11 / Req 8.4.5). Derived
    # from ``candidates`` by :meth:`_derive_wait_warning_terminal_ids`
    # at every construction so the list stays in sync with the nested
    # candidate flags — including after ``model_copy(update=...)`` on
    # ``candidates`` or ``rack_price_fallback`` (:func:`integrations.
    # rack_price_sync.annotate_rack_price_fallback` triggers the latter).
    # Callers may not override the list from the outside; the
    # ``model_validator`` below always recomputes it from the candidates.
    wait_warning_terminal_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Terminal ids of every candidate whose rolling 2-hour "
            "avg_wait_minutes exceeds the tenant-configured "
            "``terminal_wait_warning_minutes`` threshold (Redis key "
            "``terminal_wait_warning_minutes:{tenant_id}``, default 60). "
            "Computed from ``candidates`` so the dispatcher UI can "
            "render a wait-warning banner without traversing each "
            "candidate (Requirement 8.4.5)."
        ),
    )
    generated_at: datetime
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("product_code", mode="before")
    @classmethod
    def _canonicalize_product_code(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        return canonicalize(value)

    @field_validator("recommendation_id", "request_id", "tenant_id")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        if value is None:
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("truck_id", "run_id")
    @classmethod
    def _strip_optional_ids(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _derive_wait_warning_terminal_ids(self) -> "SourcingRecommendation":
        """Recompute ``wait_warning_terminal_ids`` from candidates.

        Runs on every construction (including ``model_copy``) so the
        top-level summary can never drift out of sync with the nested
        ``TerminalCandidate.wait_warning`` flags. Preserves the
        candidate ordering so the first entry corresponds to the
        highest-ranked terminal that tripped the threshold, which is
        the one the dispatcher UI surfaces first.

        Duplicates are collapsed defensively (the recommender ranks
        each terminal once, but a caller hand-assembling a
        recommendation for tests should not be able to leave a stale
        duplicate entry behind).
        """

        seen: set[str] = set()
        ordered: List[str] = []
        for candidate in self.candidates:
            if not candidate.wait_warning:
                continue
            tid = candidate.terminal_id
            if tid in seen:
                continue
            seen.add(tid)
            ordered.append(tid)
        # Assign via ``object.__setattr__`` because Pydantic v2 freezes
        # fields after the ``mode="after"`` validator enters, but we
        # still need to coerce any externally-supplied value so the
        # derived list is the single source of truth.
        object.__setattr__(self, "wait_warning_terminal_ids", ordered)
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CrossTenantAccessError(PermissionError):
    """Raised when a write/delete targets a record owned by another tenant.

    Subclass of :class:`PermissionError` so middleware that maps
    exceptions to HTTP 403 works automatically. Reads degrade silently to
    ``None`` instead of raising because a 404 is the appropriate response
    for a missing-or-not-owned document (any other code would leak
    existence to a would-be attacker).
    """

    def __init__(
        self,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        owning_tenant_id: Optional[str] = None,
    ) -> None:
        message = (
            f"{entity_type} {entity_id!r} does not belong to tenant "
            f"{tenant_id!r}"
        )
        super().__init__(message)
        self.tenant_id = tenant_id
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.owning_tenant_id = owning_tenant_id


# ---------------------------------------------------------------------------
# Base repository
# ---------------------------------------------------------------------------


ModelT = TypeVar("ModelT", bound=BaseModel)


class _BaseTenantScopedRepository:
    """Shared tenant-scoped CRUD plumbing for the Capability 8 entities.

    Subclasses bind the repository to a specific :class:`BaseModel` by
    providing:

        * ``model_cls``       — the Pydantic class to validate source
                                 documents against.
        * ``index_name``      — the ES index this repository reads/writes.
        * ``id_field``        — the primary-key field on the model (also
                                 used as ES ``_id``).
        * ``entity_type``     — human-readable entity name for error
                                 messages.
        * ``immutable_fields``— field names that ``update`` refuses to
                                 overwrite (in addition to ``id_field``,
                                 ``tenant_id``, ``created_at``).
        * ``canonicalize_fields``— catalog-backed fields to canonicalize
                                 on every write.
        * ``id_prefix``       — prefix used when the repository mints a
                                 primary key on ``create``.

    Defining the CRUD plumbing once keeps the four repositories uniform
    so callers can rely on identical tenant-isolation semantics across
    terminals, contracts, wait reports, and sourcing recommendations.
    """

    #: Default per-query cap for ``list_for_tenant``. Callers that
    #: legitimately need more pass ``size=`` explicitly; this prevents
    #: accidental full-cluster scans.
    DEFAULT_LIST_SIZE: int = 500

    model_cls: Type[BaseModel]
    index_name: str
    id_field: str
    entity_type: str
    immutable_fields: frozenset[str] = frozenset()
    canonicalize_fields: tuple[str, ...] = ()
    id_prefix: str = ""

    def __init__(self, es_service: Any, *, index_name: Optional[str] = None) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        # Distinguish an explicit empty string (user error) from ``None``
        # (fall back to the class default). Treating ``""`` as a silent
        # fallback would mask a misconfigured caller.
        if index_name is not None and not index_name:
            raise ValueError("index_name must not be empty")
        effective_index = index_name or self.index_name
        if not effective_index:
            raise ValueError("index_name must not be empty")
        self._es = es_service
        self._index = effective_index

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def _create(
        self,
        tenant_id: str,
        entity: BaseModel | Dict[str, Any],
        *,
        additional_stamps: Optional[Dict[str, Any]] = None,
    ) -> BaseModel:
        self._require_tenant(tenant_id)

        payload = self._coerce_to_dict(entity)
        payload.setdefault("tenant_id", tenant_id)
        if payload["tenant_id"] != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                entity_type=self.entity_type,
                entity_id=str(payload.get(self.id_field, "<new>")),
                owning_tenant_id=payload["tenant_id"],
            )

        payload.setdefault(self.id_field, f"{self.id_prefix}{uuid4()}")

        now = _utcnow_iso()
        if not payload.get("created_at"):
            payload["created_at"] = now
        payload["updated_at"] = now
        if additional_stamps:
            for key, value in additional_stamps.items():
                payload.setdefault(key, value)

        self._canonicalize_in_place(payload)

        model = self.model_cls(**payload)

        doc = model.model_dump(mode="json", exclude_none=False)
        doc_id = getattr(model, self.id_field)
        await self._es.index_document(self._index, doc_id, doc)
        # Dual-write master/config entities to the Postgres source-of-truth.
        _agg = _BASE_REPO_MIRROR_AGGREGATES.get(self.entity_type)
        if _agg is not None:
            if _agg == "supplier_contract":
                from commerce.services.commerce_persistence_bridge import (
                    mirror_compliance_config_upsert,
                )
                await mirror_compliance_config_upsert(_agg, doc)
            else:
                from commerce.services.commerce_persistence_bridge import (
                    mirror_current_state_upsert,
                )
                await mirror_current_state_upsert(_agg, doc)
        return model

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def _get(self, tenant_id: str, entity_id: str) -> Optional[BaseModel]:
        self._require_tenant(tenant_id)
        if not entity_id or not entity_id.strip():
            raise ValueError(f"{self.id_field} must be a non-empty string")

        # Read-cutover: serve from Postgres when this entity is migrated and
        # COMMERCE_READ_FROM_POSTGRES is on. Returns the verbatim document,
        # re-hydrated through the model so the public contract is unchanged.
        _agg = _BASE_REPO_MIRROR_AGGREGATES.get(self.entity_type)
        if _agg is not None:
            from commerce.services.commerce_persistence_bridge import (
                _NOT_CUT_OVER,
                read_hybrid_get,
            )
            pg = await read_hybrid_get(_agg, tenant_id, entity_id)
            if pg is not _NOT_CUT_OVER:
                return self._safe_model_load(pg) if pg is not None else None

        source = await self._fetch_source(entity_id)
        if source is None:
            return None
        if source.get("tenant_id") != tenant_id:
            logger.info(
                "%s._get: suppressing cross-tenant hit for %s=%s (owner=%s, requester=%s)",
                type(self).__name__,
                self.id_field,
                entity_id,
                source.get("tenant_id"),
                tenant_id,
            )
            return None
        return self._safe_model_load(source)

    async def _list_for_tenant(
        self,
        tenant_id: str,
        *,
        extra_must: Optional[List[Dict[str, Any]]] = None,
        size: Optional[int] = None,
        sort: Optional[List[Dict[str, Any]]] = None,
    ) -> List[BaseModel]:
        self._require_tenant(tenant_id)
        effective_size = size if size is not None else self.DEFAULT_LIST_SIZE
        if effective_size <= 0:
            raise ValueError("size must be a positive integer")

        # Read-cutover: serve from Postgres for migrated entity types. Only the
        # simple ``{"term": {field: value}}`` filter shape is translated, AND
        # only when every filtered field maps to a typed ORM column the PG
        # ``list`` can honor. Document-only fields (e.g. the ``supported_products``
        # / ``preferred_terminal_ids`` JSON arrays, or scalar doc fields like
        # ``operator``) are NOT typed columns — the PG ``list`` would silently
        # drop them and return the whole tenant set, so any such filter (plus
        # range/terms/geo/custom sort) falls back to ES to preserve list
        # semantics exactly.
        _agg = _BASE_REPO_MIRROR_AGGREGATES.get(self.entity_type)
        if _agg is not None and not sort:
            from persistence.read_repositories import HybridReadRepository

            model = HybridReadRepository(_agg).model
            translatable = True
            pg_filters: Dict[str, Any] = {}
            for clause in (extra_must or []):
                term = clause.get("term") if isinstance(clause, dict) else None
                if term and len(term) == 1:
                    (field, value), = term.items()
                    # Only honor a filter the PG ``list`` can actually apply —
                    # i.e. backed by a typed column on the ORM model.
                    if getattr(model, field, None) is None:
                        translatable = False
                        break
                    pg_filters[field] = value
                else:
                    translatable = False
                    break
            if translatable:
                from commerce.services.commerce_persistence_bridge import (
                    _NOT_CUT_OVER,
                    read_hybrid_list,
                )
                pg = await read_hybrid_list(
                    _agg, tenant_id, filters=pg_filters, limit=effective_size
                )
                if pg is not _NOT_CUT_OVER:
                    out: List[BaseModel] = []
                    for source in pg["items"]:
                        model = self._safe_model_load(source)
                        if model is not None:
                            out.append(model)
                    return out

        must: List[Dict[str, Any]] = [{"term": {"tenant_id": tenant_id}}]
        if extra_must:
            must.extend(extra_must)

        query: Dict[str, Any] = {
            "query": {"bool": {"must": must}},
            "size": effective_size,
        }
        if sort:
            query["sort"] = sort

        resp = await self._es.search_documents(self._index, query, effective_size)
        sources = _extract_sources(resp)

        out: List[BaseModel] = []
        for source in sources:
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "%s._list_for_tenant: dropping %s doc with mismatched tenant_id %s "
                    "(expected %s)",
                    type(self).__name__,
                    self._index,
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            model = self._safe_model_load(source)
            if model is not None:
                out.append(model)
        return out

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def _update(
        self,
        tenant_id: str,
        entity_id: str,
        patch: Dict[str, Any],
    ) -> Optional[BaseModel]:
        self._require_tenant(tenant_id)
        if not entity_id or not entity_id.strip():
            raise ValueError(f"{self.id_field} must be a non-empty string")
        if not isinstance(patch, dict):
            raise TypeError(f"patch must be a dict, got {type(patch).__name__}")

        source = await self._fetch_source(entity_id)
        if source is None:
            return None
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                entity_type=self.entity_type,
                entity_id=entity_id,
                owning_tenant_id=owner,
            )

        blocked = frozenset({self.id_field, "tenant_id", "created_at"}) | self.immutable_fields
        clean_patch = {k: v for k, v in patch.items() if k not in blocked}
        if not clean_patch:
            return self._safe_model_load(source)

        self._canonicalize_in_place(clean_patch)

        merged = {**source, **clean_patch}
        merged["updated_at"] = _utcnow_iso()
        validated = self.model_cls(**merged)

        partial = validated.model_dump(
            mode="json",
            include=set(clean_patch.keys()) | {"updated_at"},
        )
        await self._es.update_document(self._index, entity_id, partial)
        # Dual-write master/config entity updates to Postgres (full doc).
        _agg = _BASE_REPO_MIRROR_AGGREGATES.get(self.entity_type)
        if _agg is not None:
            full = validated.model_dump(mode="json", exclude_none=False)
            if _agg == "supplier_contract":
                from commerce.services.commerce_persistence_bridge import (
                    mirror_compliance_config_upsert,
                )
                await mirror_compliance_config_upsert(_agg, full)
            else:
                from commerce.services.commerce_persistence_bridge import (
                    mirror_current_state_upsert,
                )
                await mirror_current_state_upsert(_agg, full)
        return validated

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def _delete(self, tenant_id: str, entity_id: str) -> bool:
        self._require_tenant(tenant_id)
        if not entity_id or not entity_id.strip():
            raise ValueError(f"{self.id_field} must be a non-empty string")

        source = await self._fetch_source(entity_id)
        if source is None:
            return False
        owner = source.get("tenant_id")
        if owner != tenant_id:
            raise CrossTenantAccessError(
                tenant_id=tenant_id,
                entity_type=self.entity_type,
                entity_id=entity_id,
                owning_tenant_id=owner,
            )

        return bool(await self._es.delete_document(self._index, entity_id))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _require_tenant(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    def _coerce_to_dict(
        self, entity: BaseModel | Dict[str, Any]
    ) -> Dict[str, Any]:
        if isinstance(entity, self.model_cls):
            return entity.model_dump(mode="python")
        if isinstance(entity, BaseModel):
            # Different Pydantic class than expected — coerce through dict so
            # the repository-level validator runs and catches schema drift.
            return entity.model_dump(mode="python")
        if isinstance(entity, dict):
            return dict(entity)
        raise TypeError(
            f"entity must be a {self.model_cls.__name__} or dict, "
            f"got {type(entity).__name__}"
        )

    def _canonicalize_in_place(self, payload: Dict[str, Any]) -> None:
        for field in self.canonicalize_fields:
            if field in payload and payload[field] is not None:
                value = payload[field]
                if isinstance(value, str):
                    payload[field] = canonicalize(value)
                elif isinstance(value, (list, tuple)):
                    payload[field] = [
                        canonicalize(entry) if isinstance(entry, str) else entry
                        for entry in value
                    ]

    async def _fetch_source(self, entity_id: str) -> Optional[Dict[str, Any]]:
        query = {
            "query": {"term": {self.id_field: entity_id}},
            "size": 1,
        }
        try:
            resp = await self._es.search_documents(self._index, query, 1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "%s._fetch_source: search failed for %s=%s: %s",
                type(self).__name__,
                self.id_field,
                entity_id,
                exc,
            )
            return None
        sources = _extract_sources(resp)
        return sources[0] if sources else None

    def _safe_model_load(self, source: Dict[str, Any]) -> Optional[BaseModel]:
        try:
            return self.model_cls(**source)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "%s: dropping %s doc that failed model validation (id=%s): %s",
                type(self).__name__,
                self._index,
                source.get(self.id_field),
                exc,
            )
            return None


# ---------------------------------------------------------------------------
# Concrete repositories
# ---------------------------------------------------------------------------


class TerminalRepository(_BaseTenantScopedRepository):
    """Tenant-scoped CRUD for the ``terminals`` ES index."""

    model_cls = Terminal
    index_name = TERMINALS_INDEX
    id_field = "terminal_id"
    entity_type = "terminal"
    canonicalize_fields = ("supported_products",)
    id_prefix = "term_"

    async def create(
        self, tenant_id: str, terminal: Terminal | Dict[str, Any]
    ) -> Terminal:
        result = await self._create(tenant_id, terminal)
        return result  # type: ignore[return-value]

    async def get(self, tenant_id: str, terminal_id: str) -> Optional[Terminal]:
        result = await self._get(tenant_id, terminal_id)
        return result  # type: ignore[return-value]

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: Optional[ActiveStatus] = None,
        operator: Optional[str] = None,
        supported_product: Optional[str] = None,
        branded: Optional[bool] = None,
        size: int = _BaseTenantScopedRepository.DEFAULT_LIST_SIZE,
    ) -> List[Terminal]:
        extra_must: List[Dict[str, Any]] = []
        if status is not None:
            extra_must.append({"term": {"status": status}})
        if operator:
            extra_must.append({"term": {"operator": operator}})
        if supported_product:
            extra_must.append(
                {"term": {"supported_products": canonicalize(supported_product)}}
            )
        if branded is not None:
            extra_must.append({"term": {"branded": branded}})
        result = await self._list_for_tenant(
            tenant_id, extra_must=extra_must, size=size
        )
        return result  # type: ignore[return-value]

    async def update(
        self, tenant_id: str, terminal_id: str, patch: Dict[str, Any]
    ) -> Optional[Terminal]:
        result = await self._update(tenant_id, terminal_id, patch)
        return result  # type: ignore[return-value]

    async def delete(self, tenant_id: str, terminal_id: str) -> bool:
        return await self._delete(tenant_id, terminal_id)


class SupplierContractRepository(_BaseTenantScopedRepository):
    """Tenant-scoped CRUD for the ``supplier_contracts`` ES index."""

    model_cls = SupplierContract
    index_name = SUPPLIER_CONTRACTS_INDEX
    id_field = "contract_id"
    entity_type = "supplier_contract"
    canonicalize_fields = ("product_code",)
    id_prefix = "sc_"

    async def create(
        self, tenant_id: str, contract: SupplierContract | Dict[str, Any]
    ) -> SupplierContract:
        result = await self._create(tenant_id, contract)
        return result  # type: ignore[return-value]

    async def get(
        self, tenant_id: str, contract_id: str
    ) -> Optional[SupplierContract]:
        result = await self._get(tenant_id, contract_id)
        return result  # type: ignore[return-value]

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: Optional[ActiveStatus] = None,
        supplier_name: Optional[str] = None,
        product_code: Optional[str] = None,
        preferred_terminal_id: Optional[str] = None,
        size: int = _BaseTenantScopedRepository.DEFAULT_LIST_SIZE,
    ) -> List[SupplierContract]:
        extra_must: List[Dict[str, Any]] = []
        if status is not None:
            extra_must.append({"term": {"status": status}})
        if supplier_name:
            extra_must.append({"term": {"supplier_name": supplier_name}})
        if product_code:
            extra_must.append(
                {"term": {"product_code": canonicalize(product_code)}}
            )
        if preferred_terminal_id:
            extra_must.append(
                {"term": {"preferred_terminal_ids": preferred_terminal_id}}
            )
        result = await self._list_for_tenant(
            tenant_id, extra_must=extra_must, size=size
        )
        return result  # type: ignore[return-value]

    async def update(
        self, tenant_id: str, contract_id: str, patch: Dict[str, Any]
    ) -> Optional[SupplierContract]:
        result = await self._update(tenant_id, contract_id, patch)
        return result  # type: ignore[return-value]

    async def delete(self, tenant_id: str, contract_id: str) -> bool:
        return await self._delete(tenant_id, contract_id)


class TerminalWaitReportRepository(_BaseTenantScopedRepository):
    """Tenant-scoped CRUD for the ``terminal_wait_reports`` ES index."""

    model_cls = TerminalWaitReport
    index_name = TERMINAL_WAIT_REPORTS_INDEX
    id_field = "report_id"
    entity_type = "terminal_wait_report"
    # Immutable — observed_at is the source-of-truth timestamp; once written
    # it must not be silently rewritten by a later update.
    immutable_fields = frozenset({"observed_at", "source"})
    id_prefix = "twr_"

    async def create(
        self,
        tenant_id: str,
        report: TerminalWaitReport | Dict[str, Any],
    ) -> TerminalWaitReport:
        # Default retrieved_at to "now" when callers only supply observed_at;
        # this mirrors the common case of submitting a wait report via the
        # driver endpoint where the server controls retrieved_at.
        payload = self._coerce_to_dict(report)
        payload.setdefault("retrieved_at", _utcnow_iso())
        result = await self._create(tenant_id, payload)
        return result  # type: ignore[return-value]

    async def get(
        self, tenant_id: str, report_id: str
    ) -> Optional[TerminalWaitReport]:
        result = await self._get(tenant_id, report_id)
        return result  # type: ignore[return-value]

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        terminal_id: Optional[str] = None,
        source: Optional[WaitReportSource] = None,
        observed_since: Optional[datetime] = None,
        size: int = _BaseTenantScopedRepository.DEFAULT_LIST_SIZE,
    ) -> List[TerminalWaitReport]:
        extra_must: List[Dict[str, Any]] = []
        if terminal_id:
            extra_must.append({"term": {"terminal_id": terminal_id}})
        if source is not None:
            extra_must.append({"term": {"source": source}})
        if observed_since is not None:
            extra_must.append(
                {"range": {"observed_at": {"gte": observed_since.isoformat()}}}
            )
        result = await self._list_for_tenant(
            tenant_id,
            extra_must=extra_must,
            size=size,
            sort=[{"observed_at": {"order": "desc"}}],
        )
        return result  # type: ignore[return-value]

    async def update(
        self, tenant_id: str, report_id: str, patch: Dict[str, Any]
    ) -> Optional[TerminalWaitReport]:
        result = await self._update(tenant_id, report_id, patch)
        return result  # type: ignore[return-value]

    async def delete(self, tenant_id: str, report_id: str) -> bool:
        return await self._delete(tenant_id, report_id)


class SourcingRecommendationRepository(_BaseTenantScopedRepository):
    """Tenant-scoped CRUD for the ``sourcing_recommendations`` ES index."""

    model_cls = SourcingRecommendation
    index_name = SOURCING_RECOMMENDATIONS_INDEX
    id_field = "recommendation_id"
    entity_type = "sourcing_recommendation"
    canonicalize_fields = ("product_code",)
    # Immutable — a recommendation is an audit artifact. We allow updating
    # metadata like rack_price_fallback annotations but never the original
    # inputs that drove the ranking. ``wait_warning_terminal_ids`` is
    # derived from ``candidates`` by the model validator, so freezing it
    # here prevents a patch from leaving the summary desynced from the
    # candidate flags (Task 7.11 / Req 8.4.5).
    immutable_fields = frozenset(
        {
            "request_id",
            "product_code",
            "volume_gallons",
            "origin_lat",
            "origin_lon",
            "generated_at",
            "wait_warning_terminal_ids",
        }
    )
    id_prefix = "srec_"

    async def create(
        self,
        tenant_id: str,
        recommendation: SourcingRecommendation | Dict[str, Any],
    ) -> SourcingRecommendation:
        payload = self._coerce_to_dict(recommendation)
        payload.setdefault("generated_at", _utcnow_iso())
        payload.setdefault("request_id", f"req_{uuid4()}")
        result = await self._create(tenant_id, payload)
        return result  # type: ignore[return-value]

    async def get(
        self, tenant_id: str, recommendation_id: str
    ) -> Optional[SourcingRecommendation]:
        result = await self._get(tenant_id, recommendation_id)
        return result  # type: ignore[return-value]

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        truck_id: Optional[str] = None,
        run_id: Optional[str] = None,
        product_code: Optional[str] = None,
        request_id: Optional[str] = None,
        size: int = _BaseTenantScopedRepository.DEFAULT_LIST_SIZE,
    ) -> List[SourcingRecommendation]:
        extra_must: List[Dict[str, Any]] = []
        if truck_id:
            extra_must.append({"term": {"truck_id": truck_id}})
        if run_id:
            extra_must.append({"term": {"run_id": run_id}})
        if product_code:
            extra_must.append(
                {"term": {"product_code": canonicalize(product_code)}}
            )
        if request_id:
            extra_must.append({"term": {"request_id": request_id}})
        result = await self._list_for_tenant(
            tenant_id,
            extra_must=extra_must,
            size=size,
            sort=[{"generated_at": {"order": "desc"}}],
        )
        return result  # type: ignore[return-value]

    async def update(
        self,
        tenant_id: str,
        recommendation_id: str,
        patch: Dict[str, Any],
    ) -> Optional[SourcingRecommendation]:
        result = await self._update(tenant_id, recommendation_id, patch)
        return result  # type: ignore[return-value]

    async def delete(self, tenant_id: str, recommendation_id: str) -> bool:
        return await self._delete(tenant_id, recommendation_id)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Return a timezone-aware UTC timestamp as an ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat()


def _ensure_utc(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC so downstream math is consistent.

    Mirrors the helper in :mod:`fuel.services.sourcing_recommender` so
    :meth:`Terminal.is_open_at` and every caller that reasons about the
    terminal's local time share one notion of "assume UTC on naive
    inputs".
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_local_datetime(value: datetime, tz_name: str) -> Optional[datetime]:
    """Convert ``value`` into the IANA zone ``tz_name``.

    Returns ``None`` when ``ZoneInfo`` is unavailable or ``tz_name`` is
    not a known zone so the caller (notably :meth:`Terminal.is_open_at`)
    can degrade its behavior — e.g. treat "unknown zone" as "assume
    open" rather than incorrectly evaluating the UTC wall-clock against
    operating_hours declared in a different locale. A warning is logged
    so misconfigured terminals are observable.

    Used by :meth:`Terminal.is_open_at` and the ``proposed-load``
    validator to map ``operating_hours`` (expressed in local time) onto
    a UTC-aware ``as_of``.
    """

    utc_value = _ensure_utc(value)
    if ZoneInfo is None:  # pragma: no cover - Python <3.9 fallback
        logger.warning(
            "Terminal.is_open_at: ZoneInfo unavailable; cannot resolve %r",
            tz_name,
        )
        return None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning(
            "Terminal.is_open_at: unknown timezone %r", tz_name
        )
        return None
    return utc_value.astimezone(tz)


def _extract_sources(resp: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES-shaped response.

    Accepts both the canonical ``{"hits": {"hits": [{"_source": ...}]}}``
    shape and ``None`` so the helper is robust across the variety of mock
    shapes used by tests.
    """

    if not resp:
        return []
    # Handle both dict and ObjectApiResponse (which has .get() but isn't a dict)
    hits_outer = resp.get("hits") if hasattr(resp, 'get') else None
    if not hits_outer:
        return []
    hits = hits_outer.get("hits") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        if hasattr(hit, 'get') and hit.get("_source"):
            out.append(hit["_source"])
    return out


__all__ = [
    # Enums
    "ActiveStatus",
    "DayOfWeek",
    "WaitReportSource",
    # Models
    "OperatingHours",
    "Terminal",
    "SupplierContract",
    "TerminalWaitReport",
    "TerminalCandidate",
    "SourcingRecommendation",
    # Repositories
    "TerminalRepository",
    "SupplierContractRepository",
    "TerminalWaitReportRepository",
    "SourcingRecommendationRepository",
    # Errors
    "CrossTenantAccessError",
]
