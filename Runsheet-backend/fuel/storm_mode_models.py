"""
Storm-Mode domain models for the fuel-ops hardening spec (Capability 9).

This module introduces three first-class Pydantic models that together power
Phase 10 (Storm and Disruption Mode):

* :class:`WeatherAlert` — a forecast/active/cleared severe-weather event
  ingested from NOAA/NWS (or manually created by a dispatcher). The model
  1:1 mirrors the strict ``weather_alerts`` Elasticsearch mapping defined
  in :mod:`fuel.services.fuel_ops_es_mappings` so a ``model_dump()``
  payload can be indexed without transformation.
* :class:`StormModeOverride` — a manual override record written by a
  dispatcher/admin to force Storm_Mode to an explicit state (activate,
  deactivate, snooze) irrespective of the evaluator's automatic decision.
  Mirrors the ``storm_mode_overrides`` mapping.
* :class:`KeepFullCustomer` — a feature-flag / configuration model that
  captures the Keep_Full tag on a Customer_Profile: whether the customer
  opts in to the keep-full program, the minimum water-level (in %) below
  which a refill is scheduled, and the priority_boost to apply when
  Storm_Mode is active.

The module also exports shared enumerations — :data:`WeatherAlertSeverity`,
:data:`WeatherAlertStatus`, :data:`WeatherAlertSource`, and
:data:`StormModeOverrideAction` — so callers and tests don't repeat the
string literals from the ES mapping.

Design notes:

* Every model uses ``ConfigDict(extra="forbid")`` so unknown fields surface
  as a validation error rather than silently round-tripping through ES.
* Coordinate and percentage ranges are enforced via Pydantic ``Field``
  constraints, matching the validation posture of the
  :class:`fuel.customer_tank_models.CustomerTank` model.
* Timestamps are optional on input so callers can let the repository layer
  (a follow-up task) stamp ``created_at`` / ``updated_at``. All timestamps
  are expected to be timezone-aware UTC ``datetime`` instances; serialized
  payloads are ISO-8601 strings.

Validates: Requirements 9.1.1, 9.1.2, 9.2.1, 9.2.2.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared enumerations
# ---------------------------------------------------------------------------

#: NOAA-style severity buckets. ``severe`` is the default threshold at which
#: the Storm_Mode_Evaluator (Task 10.3) flips state. ``extreme`` is reserved
#: for catastrophic events (Category 4+ hurricanes, ice storms).
WeatherAlertSeverity = Literal[
    "minor",
    "moderate",
    "severe",
    "extreme",
]


#: Lifecycle of a single :class:`WeatherAlert`. The alert is ``forecast``
#: until ``expected_start_at`` passes, ``active`` while live, and
#: ``cleared`` once ``expected_end_at`` has passed. ``cancelled`` captures
#: the case where NWS withdraws the advisory before it fired.
WeatherAlertStatus = Literal[
    "forecast",
    "active",
    "cleared",
    "cancelled",
]


#: Where the alert came from. ``noaa`` / ``nws`` are NOAA's two common feed
#: identifiers; ``manual`` is used by dispatcher-created records;
#: ``weather_provider`` is used by the pluggable weather provider adapter
#: from Requirement 1.2.
WeatherAlertSource = Literal[
    "noaa",
    "nws",
    "manual",
    "weather_provider",
]


#: What action the operator wants applied by the override. ``activate``
#: forces Storm_Mode on, ``deactivate`` forces it off, ``snooze`` suppresses
#: automatic activation for the override window. ``clear`` removes any
#: prior override without changing state.
StormModeOverrideAction = Literal[
    "activate",
    "deactivate",
    "snooze",
    "clear",
]


# ---------------------------------------------------------------------------
# WeatherAlert
# ---------------------------------------------------------------------------


class WeatherAlert(BaseModel):
    """A severe-weather alert tracked per tenant.

    The fields mirror the ``weather_alerts`` ES mapping in
    :mod:`fuel.services.fuel_ops_es_mappings`. The model enforces the
    additional invariants that the ES mapping cannot express:

    * ``expected_end_at``, when set, must not precede ``expected_start_at``.
    * ``affected_zip_codes`` entries are stripped and rejected if empty.

    This model is consumed by:

    * :mod:`Agents.autonomous.weather_alert_ingester` (Task 10.2) —
      materializes WeatherAlert records from NOAA.
    * :mod:`fuel.services.storm_mode_evaluator` (Task 10.3) — reads the
      active set to decide whether Storm_Mode should flip state.
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable, tenant-scoped identifier. Upstream NOAA/NWS alert_ids "
            "are reused verbatim when available so duplicate ingestion is "
            "idempotent (Requirement 9.4.5)."
        ),
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; the repository re-asserts this on every read.",
    )
    region_code: str = Field(
        ...,
        min_length=1,
        description=(
            "Coarse region identifier (e.g. US state code, NWS zone id). "
            "Used for quick filtering before the per-ZIP match."
        ),
    )
    alert_type: str = Field(
        ...,
        min_length=1,
        description=(
            "Upstream alert classification (``winter_storm_warning``, "
            "``hurricane_warning``, ``ice_storm_warning``, etc.). Stored as "
            "an open keyword so new NWS categories do not require a "
            "mapping migration."
        ),
    )
    severity: WeatherAlertSeverity = Field(
        ...,
        description=(
            "One of minor/moderate/severe/extreme. The Storm_Mode_Evaluator "
            "default activation threshold is ``severe``."
        ),
    )
    headline: Optional[str] = Field(
        None,
        description="Short NWS headline text (e.g. 'Winter Storm Warning in effect').",
    )
    description: Optional[str] = Field(
        None,
        description="Full NWS description; may be multi-paragraph.",
    )
    expected_start_at: datetime = Field(
        ...,
        description="Timezone-aware UTC datetime when the alert becomes active.",
    )
    expected_end_at: Optional[datetime] = Field(
        None,
        description=(
            "Timezone-aware UTC datetime when the alert is expected to "
            "clear. Nullable because some advisories (e.g. long-duration "
            "freeze watches) omit an end time."
        ),
    )
    affected_zip_codes: List[str] = Field(
        default_factory=list,
        description=(
            "US ZIP codes impacted by the alert. Used to match the alert "
            "to Customer_Profile ZIP codes for Storm_Mode prioritization."
        ),
    )
    source: WeatherAlertSource = Field(
        ...,
        description="Where the alert was ingested from (noaa/nws/manual/weather_provider).",
    )
    ingested_at: datetime = Field(
        ...,
        description="Timestamp at which the ingester persisted the record.",
    )
    activation_status: WeatherAlertStatus = Field(
        default="forecast",
        description=(
            "Current lifecycle position. The ingester sets ``forecast`` / "
            "``active`` / ``cleared`` / ``cancelled`` based on the alert's "
            "``expected_start_at`` and ``expected_end_at`` and the upstream "
            "cancellation flag."
        ),
    )
    updated_at: Optional[datetime] = Field(
        None,
        description="Last-modification timestamp written by the repository.",
    )
    created_at: Optional[datetime] = Field(
        None,
        description="Creation timestamp written by the repository.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("alert_id", "tenant_id", "region_code", "alert_type")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        """Collapse whitespace-only required strings into a validation error."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("affected_zip_codes")
    @classmethod
    def _strip_zip_codes(cls, value: List[str]) -> List[str]:
        """Strip each ZIP and reject whitespace-only entries."""
        cleaned: List[str] = []
        for zip_code in value:
            if not isinstance(zip_code, str):
                raise ValueError("affected_zip_codes entries must be strings")
            stripped = zip_code.strip()
            if not stripped:
                raise ValueError("affected_zip_codes entries must not be blank")
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def _check_time_window(self) -> "WeatherAlert":
        """Reject a window where ``expected_end_at`` precedes ``expected_start_at``."""
        if (
            self.expected_end_at is not None
            and self.expected_end_at < self.expected_start_at
        ):
            raise ValueError(
                f"expected_end_at ({self.expected_end_at.isoformat()}) must not "
                f"precede expected_start_at ({self.expected_start_at.isoformat()})"
            )
        return self


# ---------------------------------------------------------------------------
# StormModeOverride
# ---------------------------------------------------------------------------


class StormModeOverride(BaseModel):
    """A dispatcher/admin-issued Storm_Mode override.

    The override forces the Storm_Mode state to a specific value regardless
    of what the automatic evaluator (Task 10.3) would otherwise decide.
    Used for both false-positives ("the evaluator thinks we're in a storm
    but we're not") and false-negatives ("there's no NOAA alert yet but we
    can see it coming").

    Mirrors the ``storm_mode_overrides`` ES mapping.
    """

    model_config = ConfigDict(extra="forbid")

    override_id: str = Field(
        ...,
        min_length=1,
        description="Stable, tenant-scoped override identifier.",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; the repository re-asserts this on every read.",
    )
    action: StormModeOverrideAction = Field(
        ...,
        description=(
            "What the override does: ``activate`` forces Storm_Mode on, "
            "``deactivate`` forces it off, ``snooze`` suppresses automatic "
            "activation until ``expires_at``, ``clear`` removes any prior "
            "override without changing state."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Human-readable justification captured for audit. Required so "
            "every override is explainable at incident review."
        ),
    )
    actor_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the user (or service account) that issued the "
            "override. Used by the audit log referenced by Requirement "
            "9.4.5."
        ),
    )
    expires_at: Optional[datetime] = Field(
        None,
        description=(
            "Timezone-aware UTC datetime at which the override lapses. "
            "Nullable because ``clear`` overrides are instantaneous. "
            "``activate`` / ``deactivate`` / ``snooze`` overrides SHOULD "
            "set an expiry; a null expiry on those actions is treated as "
            "an indefinite override."
        ),
    )
    updated_at: Optional[datetime] = Field(
        None,
        description="Last-modification timestamp written by the repository.",
    )
    created_at: Optional[datetime] = Field(
        None,
        description="Creation timestamp written by the repository.",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("override_id", "tenant_id", "reason", "actor_id")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        """Collapse whitespace-only required strings into a validation error."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped


# ---------------------------------------------------------------------------
# KeepFullCustomer flag
# ---------------------------------------------------------------------------


class KeepFullCustomer(BaseModel):
    """The Keep_Full tag carried by a Customer_Profile.

    Requirement 9.2.1 calls for three Customer_Profile fields that together
    describe keep-full behavior:

    * ``keep_full_enabled`` — whether the customer is in the keep-full
      program. When ``False``, the other fields are ignored.
    * ``minimum_low_water_pct`` — the tank-level percentage (0–100) below
      which a keep-full refill is scheduled. Propane companies typically
      set this to 30%.
    * ``keep_full_priority_boost`` — the additive boost applied to
      ``priority_score`` during Storm_Mode when ``keep_full_enabled`` is
      ``True``. Constrained to [0.0, 1.0]; the spec default is 0.25.

    The model is defined separately from the Customer_Profile itself so it
    can be:

    * Re-used by the :class:`CustomerProfile` extension in this module,
      which composes it as a nested submodel.
    * Reused by the Delivery_Prioritization_Agent (Task 10.6) without
      importing the full Customer_Profile surface.

    Validates: Requirement 9.2.1.
    """

    model_config = ConfigDict(extra="forbid")

    keep_full_enabled: bool = Field(
        default=False,
        description=(
            "Whether the customer has opted in to the keep-full program. "
            "Defaults to ``False``; the platform does not enroll customers "
            "silently."
        ),
    )
    minimum_low_water_pct: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description=(
            "Tank-level percentage (0–100) below which a keep-full refill "
            "is scheduled. Default 30.0 matches the US propane industry "
            "convention."
        ),
    )
    keep_full_priority_boost: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description=(
            "Additive boost applied to the prioritization score when "
            "Storm_Mode is active and ``keep_full_enabled`` is true. "
            "Range [0.0, 1.0]. Default 0.25 per Requirement 9.2.1."
        ),
    )


# ---------------------------------------------------------------------------
# CustomerProfile extensions (Keep-Full + Storm-Mode tags)
# ---------------------------------------------------------------------------


#: Criticality buckets used by Storm_Mode prioritization (Requirement 9.1.1).
#: Declared here rather than in a dedicated customer-profile module because
#: the Storm_Mode spec is the first place the tier is consumed — future
#: Customer_Profile work (Task 5.3) will import this alias.
CriticalityTier = Literal[
    "keep_full_residential",
    "medical",
    "data_center",
    "industrial_critical",
    "commercial",
    "standard",
]


class CustomerProfileStormFields(BaseModel):
    """The storm-mode extensions to the Customer_Profile entity.

    Requirement 9.2.1 tags the Customer_Profile with three keep-full
    fields (exposed as the :class:`KeepFullCustomer` submodel below), and
    Requirement 9.1.1 adds three criticality fields that drive Storm_Mode
    prioritization. Both sets live on this mixin so the future full
    Customer_Profile model (Task 5.3) can pull them in without duplicating
    the definitions — and so Phase 10 agents can type-check against a
    narrow, Storm_Mode-only surface today.

    Fields:

    * ``keep_full`` — nested :class:`KeepFullCustomer` submodel covering
      ``keep_full_enabled`` / ``minimum_low_water_pct`` /
      ``keep_full_priority_boost`` (Requirement 9.2.1).
    * ``criticality_tier`` — the customer's storm-mode criticality bucket
      (Requirement 9.1.1). Defaults to ``standard``.
    * ``is_generator_fuel`` — whether the customer's primary use-case is
      backup-generator fueling. Drives the generator-specific Storm_Mode
      boost (Requirement 9.2.2).
    * ``requires_continuous_service`` — whether the customer's contract
      requires uninterrupted service (hospitals, data centers).

    Validates: Requirements 9.1.1, 9.2.1, 9.2.2.
    """

    model_config = ConfigDict(extra="forbid")

    keep_full: KeepFullCustomer = Field(
        default_factory=KeepFullCustomer,
        description=(
            "Keep-full configuration for the customer. See "
            ":class:`KeepFullCustomer` for field-level details."
        ),
    )
    criticality_tier: CriticalityTier = Field(
        default="standard",
        description=(
            "Storm_Mode criticality classification (Requirement 9.1.1). "
            "Defaults to ``standard`` so that Customer_Profiles created "
            "without explicit tagging receive no storm boost."
        ),
    )
    is_generator_fuel: bool = Field(
        default=False,
        description=(
            "True when the customer's primary fuel use is backup-generator "
            "fueling. Drives the generator-specific Storm_Mode boost "
            "(default 0.2, Requirement 9.2.2)."
        ),
    )
    requires_continuous_service: bool = Field(
        default=False,
        description=(
            "True when the customer's contract requires uninterrupted "
            "service (hospitals, data centers)."
        ),
    )


#: SLA tier values used by the business-impact component of the delivery
#: prioritization score (Requirement 3.3.1). Ordered platinum > gold > silver
#: > bronze; the numeric rank is applied by
#: :func:`fuel.services.prioritization_helpers.compute_business_impact`.
SLATier = Literal["platinum", "gold", "silver", "bronze"]


class CustomerProfile(BaseModel):
    """Customer_Profile entity used by Storm_Mode (Task 10.1) and
    Delivery_Prioritization (Task 5.3).

    The model carries:

    * Identity: ``customer_id``, ``tenant_id``, ``zip_code``.
    * Storm_Mode extensions: the fields from
      :class:`CustomerProfileStormFields` (keep-full + criticality +
      generator + continuous-service tags) — Requirements 9.1.1, 9.2.1,
      9.2.2.
    * Business-impact extensions (Task 5.3, Requirement 3.3.1): the
      revenue / contract-penalty / SLA-tier / missed-delivery-cost fields
      consumed by
      :func:`fuel.services.prioritization_helpers.compute_business_impact`.

    All four business-impact fields are ``Optional`` so that existing
    Customer_Profiles without the data continue to validate. When a field
    is ``None`` the prioritization helper treats it as zero and appends
    a ``missing_profile_field:{field}`` reason to the score output per
    Requirement 3.3.5.

    The model uses ``extra="ignore"`` (not ``forbid``) so that future
    additions to the same entity — and any legacy payload that still
    carries Phase-10-only fields — do not cause Phase-5 code paths to
    reject the document.
    """

    model_config = ConfigDict(extra="ignore")

    customer_id: str = Field(
        ...,
        min_length=1,
        description="Stable customer identifier within the tenant.",
    )
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Owning tenant; downstream agents re-assert this.",
    )
    zip_code: Optional[str] = Field(
        None,
        description=(
            "Primary ZIP code of the customer's service address. Used by "
            "the Storm_Mode prioritizer to match customers to "
            ":class:`WeatherAlert`\\ s via ``affected_zip_codes``."
        ),
    )

    # Storm_Mode extensions — flattened so callers can access
    # ``profile.keep_full.keep_full_enabled`` directly while still exposing
    # the criticality/generator flags at the top level.
    keep_full: KeepFullCustomer = Field(
        default_factory=KeepFullCustomer,
        description="Keep-full tag (Requirement 9.2.1).",
    )
    criticality_tier: CriticalityTier = Field(
        default="standard",
        description="Storm_Mode criticality classification (Requirement 9.1.1).",
    )
    is_generator_fuel: bool = Field(
        default=False,
        description="Generator-fuel customer flag (Requirement 9.2.2).",
    )
    requires_continuous_service: bool = Field(
        default=False,
        description="Continuous-service contract flag (Requirement 9.1.1).",
    )

    # Business-impact extensions (Requirement 3.3.1, Task 5.3).
    annual_revenue_usd: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Estimated annual revenue (USD) the customer generates for the "
            "tenant. Drives the 0.4-weight revenue component of the "
            "business-impact score (Requirement 3.3.2)."
        ),
    )
    contract_penalty_usd_per_day: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Contractual penalty (USD) incurred per day the tenant fails "
            "to deliver. Drives the 0.3-weight penalty component of the "
            "business-impact score (Requirement 3.3.2)."
        ),
    )
    sla_tier: Optional[SLATier] = Field(
        default=None,
        description=(
            "Service-level-agreement tier for this customer: one of "
            "platinum / gold / silver / bronze. Drives the 0.1-weight "
            "tier component of the business-impact score (Requirement "
            "3.3.1). ``None`` is treated as the lowest tier and surfaces "
            "a ``missing_profile_field:sla_tier`` reason."
        ),
    )
    missed_delivery_cost_usd: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Typical cost (USD) the customer incurs when a delivery is "
            "missed (e.g., spoilage, downtime). Drives the 0.2-weight "
            "missed-delivery component of the business-impact score "
            "(Requirement 3.3.2)."
        ),
    )

    @field_validator("customer_id", "tenant_id")
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        """Collapse whitespace-only required strings into a validation error."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("required string must not be blank")
        return stripped

    @field_validator("zip_code")
    @classmethod
    def _strip_optional_zip(cls, value: Optional[str]) -> Optional[str]:
        """Treat whitespace-only ZIPs as missing instead of storing them."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


__all__ = [
    # Enumerations
    "WeatherAlertSeverity",
    "WeatherAlertStatus",
    "WeatherAlertSource",
    "StormModeOverrideAction",
    "CriticalityTier",
    "SLATier",
    # Models
    "WeatherAlert",
    "StormModeOverride",
    "KeepFullCustomer",
    "CustomerProfileStormFields",
    "CustomerProfile",
]
