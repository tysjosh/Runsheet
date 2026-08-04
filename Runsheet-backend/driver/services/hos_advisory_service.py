"""
``HOSAdvisoryService`` — the read-only Hours-of-Service advisory.

Runsheet is **not** an ELD. The carrier's certified device is the authoritative
record of Hours of Service (R17.1), and this module produces no record of duty
status: it reads ``truck_telemetry`` and returns what the telematics vendor last
reported, labelled advisory on every figure. Nothing here writes to
``truck_telemetry``, nothing here calls Geotab, and nothing here emits a value to
any ELD. The RODS event model, driver certification of logs, edit and annotation
history, carrier edit proposals, unassigned-driving-time assignment, malfunction
and diagnostic event codes, ERODS roadside output, and FMCSA self-certification
and registration are all outside this feature by construction (R17.2) — there is
no field, no index, and no code path for any of them.

**The resolution chain has exactly two links.** The driver's truck comes from
``drivers_current.assigned_truck_id`` (R17.3); the reading is the
``truck_telemetry`` document with the greatest ``recorded_at`` for that
``(tenant_id, truck_id)`` pair (R17.4). ``truck_telemetry.driver_id`` is
**excluded** from the resolution (R17.5) — that field carries the telematics
vendor's own driver identifier, not a Runsheet ``driver_id``, so matching on it
would attribute one carrier's reading to whoever happens to share the vendor's
id space. The exclusion is structural, not conventional: the query filters on
``tenant_id`` and ``truck_id`` only and asks Elasticsearch to omit the field from
``_source`` (:data:`EXCLUDED_READING_FIELDS`), so a reading that arrives carrying
it cannot be read by accident.

**Classification is total and conservative** (Property 34). Every combination of
"no assigned truck / no document / a document of some age" lands on exactly one
of :data:`FRESHNESS_STATES`:

======================================  ==========  ==========================
Condition                               State       Reason code
======================================  ==========  ==========================
no ``assigned_truck_id``                ``unknown`` ``HOS_TRUCK_UNASSIGNED``
no ``truck_telemetry`` document         ``unknown`` ``HOS_NO_READING``
``recorded_at`` absent or unparseable   ``unknown`` ``HOS_NO_READING``
age > the tenant's freshness window     ``stale``   ``HOS_READING_STALE``
otherwise                               ``fresh``   ``None``
======================================  ==========  ==========================

An unmapped device is the second row: ``instance.config["device_map"]`` is what
turns a Geotab device into a ``truck_id``, and an unmapped device persists with
``truck_id=None`` (``integrations/geotab.py:1600-1612``), so no document exists
for the driver's truck and the answer is ``unknown`` rather than wrong (R17.7).

A ``stale`` or ``unknown`` state reports the compliance state as ``unknown``
rather than as within limits (R17.10) and reports every remaining-hours figure as
``unavailable`` (R17.8). A read failure is treated the same way as an absent
document — an advisory that cannot be resolved says so; it never guesses.

**The three remaining-hours figures are ``unavailable`` today, and that is a
fact about the connector, not a stub.** ``_normalize_duty_status``
(``integrations/geotab.py:470-489``) extracts only the vendor duty-status string;
``truck_telemetry`` is ``dynamic: strict`` with ``hos_status`` typed ``keyword``
(``fuel/services/fuel_ops_es_mappings.py:340-357``), so the index cannot even
hold remaining drive minutes, a remaining on-duty window, or cycle hours
(R17.13). :func:`hos_status_from_reading` is the seam for a connector that does
supply them: it populates the **existing**
:class:`compliance.services.hos_checker.HOSStatus` field set rather than
declaring a second Hours-of-Service model (R17.14), and its ``driver_id`` is the
Runsheet driver — never ``truck_telemetry.driver_id``.

The freshness window defaults to 300 seconds, bound to
``integrations.geotab.DEFAULT_FRESHNESS_SECONDS`` by import rather than copied,
and a tenant may override it through the ``hos_freshness_seconds`` key on its
``gps_eld`` ``IntegrationInstance.config`` (R17.9).

Requirement 17.27 holds here by omission and is worth stating: the Requirement 13
duty statuses ``active`` / ``inactive`` / ``on_break`` / ``off_duty`` are
Runsheet *availability* values. This module never reads
``drivers_current.status``, so no drive-time accrual, on-duty-window accrual, or
Hours-of-Service duty-status record is derived from any of them.

**The gate is the second surface, and it is armed.** :meth:`gate_verdict` turns
an advisory into the verdict the driver transition gate stack consumes
(``driver/services/order_transition_service.py`` ``_gate_hos``), and
``bootstrap/driver.py`` now passes ``configured_hos_advisory_service()`` where it
used to pass ``None``. The posture is **fail-open at every step**, and the order
of the checks is what makes it so:

===================================================  ==============  ==========================
Condition                                            Gate outcome    Reason code
===================================================  ==============  ==========================
overlay ``driver.hos_gating`` not enforcing           no gate at all  ``HOS_GATING_DISABLED``
no enabled ``gps_eld`` instance                       skipped         ``HOS_GPS_ELD_DISABLED``
reading ``stale`` or ``unknown``                      skipped         the advisory's own code
fresh, but the connector supplies no figures          skipped         ``HOS_FIGURES_UNAVAILABLE``
fresh, at or past a limit, unexpired override         passed          ``HOS_OVERRIDE_APPLIED``
fresh, at or past a limit, no override                **blocked**     ``HOS_AT_LIMIT``
fresh, within limits                                  passed          ``None``
===================================================  ==============  ==========================

Exactly one row blocks. The first row is R17.19 — a tenant that has not enabled
gating gets *no gate*, not a passing one, and no audit record; every other skip
carries the audit outcome ``hos_gate_skipped`` and its reason code (R17.18).
Both switches must be on for the gate to exist at all (R17.20), and both default
to false: the overlay key is ``disabled`` until a tenant sets it and
``IntegrationInstance.enabled`` is ``False`` by construction
(``integrations/connector_base.py:169-176``).

**The Geotab connector as built cannot arm this gate, and that is the point.**
It supplies no remaining-drive-time figure, so a fresh reading lands on the
``HOS_FIGURES_UNAVAILABLE`` row and the transition proceeds.
:meth:`assert_gating_can_be_enabled` is the other half of that guarantee: a
tenant asking to switch gating on while its connector supplies no
remaining-drive-time figure is refused with 409 ``HOS_FIGURES_UNAVAILABLE``
(R17.21), so the gate can never be armed against data that does not exist.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Service interfaces and
Properties 34 and 35.

**The override is the third surface.** :meth:`HOSAdvisoryService.record_override`
is the write side of the read the gate makes: a ``dispatcher`` or ``admin``
clearance persisted to ``hos_gate_overrides`` with a server-minted
``override_id``, an ``actor_id`` taken from the verified session, a non-blank
reason, and an expiry in the future (R17.23). The role gate itself lives on the
router, and a caller holding only ``driver`` is refused — a driver may not clear
its own gate (R17.24).

Validates: Requirements 17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8, 17.9,
17.10, 17.11, 17.12, 17.13, 17.14, 17.17, 17.18, 17.19, 17.20, 17.21, 17.23,
17.24, 17.25, 17.26
- 17.1: every figure carries ``advisory=True``; the advisory names the carrier's
  ELD as the authoritative record
- 17.2: no RODS, certification, edit-history, or ERODS field or path exists
- 17.3: the truck comes from ``drivers_current.assigned_truck_id``
- 17.4: the reading is the greatest ``recorded_at`` for the tenant and truck
- 17.5: ``truck_telemetry.driver_id`` is excluded from the resolution
- 17.6: no assigned truck → ``unknown`` / ``HOS_TRUCK_UNASSIGNED``
- 17.7: no document → ``unknown`` / ``HOS_NO_READING``
- 17.8: an over-age reading → ``stale``, the age in seconds, every figure
  ``unavailable``
- 17.9: the window defaults to 300s and accepts a per-tenant override
- 17.10: ``stale`` or ``unknown`` reports compliance state ``unknown``
- 17.11: a resolved reading returns its duty status, ``recorded_at``, age,
  freshness state, and provider name
- 17.12: a figure the reading carries is returned with its unit of measure
- 17.13: the three figures are ``unavailable`` for a duty-status-only connector
- 17.14: a figure-supplying connector populates the existing ``HOSStatus``
- 17.17: a fresh at-limit reading blocks with the reason code and ``recorded_at``
- 17.18: a ``stale`` or ``unknown`` reading, or a disabled ``gps_eld`` instance,
  permits the transition and records ``hos_gate_skipped`` with its reason code
- 17.19: a tenant with gating disabled gets no gate at all
- 17.20: enablement is the overlay toggle **and** ``IntegrationInstance.enabled``,
  both defaulting to false
- 17.21: enabling gating without a remaining-drive-time figure is refused with
  409 ``HOS_FIGURES_UNAVAILABLE``
- 17.23: an override carries a server-minted id, a session-derived actor, a
  non-blank reason, and a future expiry, persisted to a ``dynamic: strict`` index
- 17.24: the ``dispatcher`` / ``admin`` role set the router enforces exactly
- 17.25: an unexpired override permits the transition and its identifier travels
  on the verdict
- 17.26: the verdict carries the acting ``driver_id``, the reading
  ``recorded_at``, the freshness state, the gate outcome, and the override id
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from compliance.services.hos_checker import HOSStatus
from driver.services.driver_es_mappings import HOS_GATE_OVERRIDES_INDEX
from errors.exceptions import (
    elasticsearch_unavailable,
    hos_figures_unavailable,
    invalid_request,
)
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from fuel.services.fuel_ops_es_mappings import TRUCK_TELEMETRY_INDEX
from integrations.geotab import DEFAULT_FRESHNESS_SECONDS as _GEOTAB_FRESHNESS_SECONDS
from ops.middleware.tenant_guard import inject_tenant_filter
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: The freshness window in seconds, bound to ``DEFAULT_FRESHNESS_SECONDS`` in
#: ``integrations/geotab.py`` by import so the advisory and the connector's own
#: ``trucks.current_location`` gate cannot drift apart (R17.9).
DEFAULT_FRESHNESS_SECONDS: int = int(_GEOTAB_FRESHNESS_SECONDS)

#: ``IntegrationInstance.config`` key carrying a tenant's override of the
#: freshness window, in seconds. Non-positive and non-numeric values are ignored
#: in favour of the default rather than treated as "never stale" (R17.9).
FRESHNESS_CONFIG_KEY: str = "hos_freshness_seconds"

#: The ``IntegrationInstance.category`` the telematics connector registers under.
GPS_ELD_CATEGORY: str = "gps_eld"

#: Fallback provider name, used only when no ``gps_eld`` instance can be read.
#: The instance's own ``provider_name`` wins whenever one resolves, so no
#: provider identifier is hard-wired into the resolution itself.
DEFAULT_PROVIDER_NAME: str = "geotab"

#: The three freshness states. Exhaustive: :meth:`HOSAdvisoryService.resolve`
#: returns exactly one of them for every input (Property 34).
FRESHNESS_STATES: Tuple[str, ...] = ("fresh", "stale", "unknown")

#: The compliance states. ``unknown`` is the answer for every ``stale`` or
#: ``unknown`` reading and for every reading whose figures are ``unavailable``
#: (R17.10) — it is never reported as within limits on the strength of a
#: duty-status string alone.
COMPLIANCE_STATES: Tuple[str, ...] = ("within_limits", "at_limit", "unknown")

#: Reason code for a driver with no ``assigned_truck_id`` (R17.6).
HOS_TRUCK_UNASSIGNED: str = "HOS_TRUCK_UNASSIGNED"

#: Reason code for a truck with no usable ``truck_telemetry`` document (R17.7).
HOS_NO_READING: str = "HOS_NO_READING"

#: Reason code for a reading older than the tenant's freshness window (R17.8).
HOS_READING_STALE: str = "HOS_READING_STALE"

#: The two reason codes an ``unknown`` state may carry (Property 34).
UNKNOWN_REASON_CODES: Tuple[str, ...] = (HOS_TRUCK_UNASSIGNED, HOS_NO_READING)

#: Overlay feature-flag key carrying a tenant's Hours-of-Service gating toggle.
#: One of the two switches of R17.20, and the one that decides whether the gate
#: exists at all: ``get_overlay_state`` returns ``disabled`` for an unset key and
#: fails closed to ``disabled`` when Redis is unreachable, so the default is off
#: in every tenant.
HOS_GATING_FLAG_KEY: str = "driver.hos_gating"

#: Overlay states that mean "enforce". ``shadow`` observes without blocking and
#: ``disabled`` is the default, so both leave the gate unarmed — the same
#: convention the pre-trip gate uses.
ENFORCING_OVERLAY_STATES: Tuple[str, ...] = ("active_gated", "active_auto")

#: Reason code for a tenant that has not enabled gating (R17.19). Distinct from
#: every other code here because it is the one case that records **no** audit
#: outcome: there is no gate to skip.
HOS_GATING_DISABLED: str = "HOS_GATING_DISABLED"

#: Reason code for a tenant with no *enabled* ``gps_eld`` instance (R17.18,
#: R17.20). ``IntegrationInstance.enabled`` defaults to ``False``.
HOS_GPS_ELD_DISABLED: str = "HOS_GPS_ELD_DISABLED"

#: Reason code for a fresh reading whose connector supplies no remaining-hours
#: figure — the Geotab connector as built (R17.13). Shares its spelling with the
#: ``HOS_FIGURES_UNAVAILABLE`` error code of R17.21 deliberately: the skip and
#: the refusal to arm the gate are the same fact seen from two sides.
HOS_FIGURES_UNAVAILABLE: str = "HOS_FIGURES_UNAVAILABLE"

#: Reason code carried by the one blocking verdict: a fresh reading at or past a
#: limit with no unexpired override (R17.17).
HOS_AT_LIMIT: str = "HOS_AT_LIMIT"

#: Reason code for an at-limit reading cleared by an unexpired override (R17.25).
HOS_OVERRIDE_APPLIED: str = "HOS_OVERRIDE_APPLIED"

#: The gate outcomes. Mirrors ``GateOutcome.outcome`` in
#: ``driver/services/order_transition_service.py`` so the gate stack can honour
#: the verdict verbatim instead of inferring an outcome from a boolean.
GATE_OUTCOMES: Tuple[str, ...] = ("passed", "blocked", "skipped")

#: Audit outcome R17.18 names for a permitted transition the gate could not
#: evaluate.
AUDIT_GATE_SKIPPED: str = "hos_gate_skipped"

#: Audit outcome for the blocking verdict.
AUDIT_GATE_BLOCKED: str = "hos_gate_blocked"

#: Audit outcome for a reading evaluated and found within limits.
AUDIT_GATE_PASSED: str = "hos_gate_passed"

#: Audit outcome for an at-limit reading cleared by an unexpired override
#: (R17.25) — a distinct value from ``hos_gate_passed`` so a cleared gate is
#: never indistinguishable from one that never fired.
AUDIT_GATE_OVERRIDDEN: str = "hos_gate_overridden"

#: The two roles that may clear a driver's Hours-of-Service gate (R17.23).
#: Matched **exactly** by :func:`auth.authorization.require_role`, so a tenant
#: role lexicon such as ``dispatcher_lead`` does not satisfy the gate. A caller
#: holding only ``driver`` is refused (R17.24) — a driver may not clear its own.
OVERRIDE_ROLES: Tuple[str, ...] = ("dispatcher", "admin")

#: Prefix of a server-minted override identifier: ``hgo_<uuid4hex>``. Minted
#: here and never accepted from a request body, so a caller can neither spoof
#: ownership of an override nor overwrite an existing one (R17.23).
OVERRIDE_ID_PREFIX: str = "hgo_"

#: Ceiling on the stored override reason, in characters. The reason is an audit
#: note, not a document; the bound keeps an unbounded body out of the index.
MAX_OVERRIDE_REASON_LENGTH: int = 1000

#: ``IntegrationInstance.config`` key by which a connector declares that it
#: supplies the three remaining-hours figures. The seam R17.21 needs: a tenant
#: whose connector genuinely reports remaining drive time can arm the gate
#: without waiting for a reading to arrive first. Absent, the answer is taken
#: from the readings themselves.
FIGURES_CONFIG_KEY: str = "hos_supplies_remaining_hours"

#: How many recent readings :meth:`HOSAdvisoryService.supplies_remaining_drive_time`
#: samples before concluding that a connector supplies no figures.
FIGURE_PROBE_SIZE: int = 25

#: Fields on a ``truck_telemetry`` document this service must not resolve
#: through. ``driver_id`` there is the telematics vendor's driver identifier
#: (R17.5), so it is excluded from ``_source`` at the query and never read.
EXCLUDED_READING_FIELDS: Tuple[str, ...] = ("driver_id",)

#: Unit of measure for the three remaining-hours figures (R17.12).
HOURS_UNIT: str = "hours"

#: The statement that accompanies every advisory (R17.1, R16.20).
ELD_AUTHORITATIVE_STATEMENT: str = (
    "The carrier's ELD is the authoritative record of Hours of Service. "
    "Every figure shown here is advisory."
)

#: Keys a reading may carry for each ``HOSStatus`` figure. Absent from the
#: ``truck_telemetry`` mapping today, which is exactly why R17.13 reports the
#: figures as unavailable; declared here so a connector that starts supplying
#: them needs no second model (R17.14).
_DRIVE_HOURS_KEYS: Tuple[str, ...] = (
    "available_drive_hours",
    "availableDriveHours",
)
_WINDOW_HOURS_KEYS: Tuple[str, ...] = (
    "available_window_hours",
    "availableWindowHours",
)
_CYCLE_HOURS_KEYS: Tuple[str, ...] = (
    "cumulative_cycle_hours",
    "cumulativeCycleHours",
)
_CYCLE_TYPE_KEYS: Tuple[str, ...] = ("cycle_type", "cycleType")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware datetime, or ``None``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sources(response: Any) -> List[Dict[str, Any]]:
    """Extract ``_source`` payloads from an ES search response."""
    if not response:
        return []
    outer = response.get("hits") if hasattr(response, "get") else None
    if not outer:
        return []
    hits = outer.get("hits") if hasattr(outer, "get") else []
    out: List[Dict[str, Any]] = []
    for hit in hits or []:
        source = hit.get("_source") if hasattr(hit, "get") else None
        if source:
            out.append(dict(source))
    return out


def _as_document(record: Any) -> Optional[Dict[str, Any]]:
    """Normalize a repository result (model or raw dict) into a plain dict."""
    if record is None:
        return None
    if isinstance(record, dict):
        return dict(record)
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    return None


def _first_float(document: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    """Return the first key in ``keys`` that holds a real number, as a float."""
    for key in keys:
        if key not in document:
            continue
        value = document[key]
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _text(value: Any) -> Optional[str]:
    """Return ``value`` as a non-blank string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def hos_status_from_reading(
    reading: Dict[str, Any],
    *,
    driver_id: str,
    recorded_at: Optional[datetime],
    provider_name: Optional[str],
) -> Optional[HOSStatus]:
    """Return the reading's :class:`HOSStatus`, or ``None`` when it has none.

    The seam R17.14 asks for. All three figures must be present — a partial set
    is no set, because an advisory built from two of three would report an
    on-duty window the connector never supplied. The Geotab connector as built
    supplies none of them, so this returns ``None`` for every
    ``truck_telemetry`` document written today (R17.13) and the caller reports
    the three figures as ``unavailable``.

    ``driver_id`` is the **Runsheet** driver, never ``truck_telemetry.driver_id``
    (R17.5) — which is why it is a required keyword rather than something read
    off the reading.

    Validates: Requirements 17.5, 17.13, 17.14
    """
    drive_hours = _first_float(reading, _DRIVE_HOURS_KEYS)
    window_hours = _first_float(reading, _WINDOW_HOURS_KEYS)
    cycle_hours = _first_float(reading, _CYCLE_HOURS_KEYS)
    if drive_hours is None or window_hours is None or cycle_hours is None:
        return None

    cycle_type = "7_day"
    for key in _CYCLE_TYPE_KEYS:
        raw = _text(reading.get(key))
        if raw:
            cycle_type = "8_day" if raw in ("8_day", "8day", "8-day") else "7_day"
            break

    return HOSStatus(
        driver_id=driver_id,
        available_drive_hours=drive_hours,
        available_window_hours=window_hours,
        cumulative_cycle_hours=cycle_hours,
        cycle_type=cycle_type,
        last_updated=recorded_at or utcnow(),
        source=provider_name or DEFAULT_PROVIDER_NAME,
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HOSFigure(BaseModel):
    """One Hours-of-Service figure, or the explicit absence of one.

    ``availability`` is the discriminator R17.13 needs: ``unavailable`` says the
    tenant's connector does not supply this figure, which is a different claim
    from "the figure is zero". A figure that *is* available carries its unit of
    measure (R17.12), and ``advisory`` is ``True`` on both, because R17.1 labels
    every Hours-of-Service figure this feature surfaces as advisory.

    Validates: Requirements 17.1, 17.12, 17.13
    """

    model_config = ConfigDict(extra="forbid")

    availability: Literal["available", "unavailable"]
    value: Optional[float] = None
    unit: Optional[str] = None
    advisory: Literal[True] = True

    @classmethod
    def unavailable(cls) -> "HOSFigure":
        """The figure a duty-status-only connector supplies (R17.13)."""
        return cls(availability="unavailable")

    @classmethod
    def hours(cls, value: float) -> "HOSFigure":
        """A figure in hours, with its unit attached (R17.12)."""
        return cls(availability="available", value=float(value), unit=HOURS_UNIT)


class HOSAdvisory(BaseModel):
    """The advisory one driver's ``GET /api/driver/hos`` returns.

    Read-only and self-describing: the freshness state, the reason code behind
    it, the reading that produced it, and three figures that each say whether
    they are available at all. ``compliance_state`` is ``unknown`` whenever the
    freshness state is ``stale`` or ``unknown`` (R17.10), and it is never
    ``within_limits`` on the strength of a duty-status string alone — that
    inference belongs to a connector that supplies remaining hours.

    Validates: Requirements 17.1, 17.6, 17.7, 17.8, 17.10, 17.11, 17.12, 17.13,
    17.14
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    driver_id: str
    freshness_state: Literal["fresh", "stale", "unknown"]
    compliance_state: Literal["within_limits", "at_limit", "unknown"]
    reason_code: Optional[str] = None
    truck_id: Optional[str] = None
    duty_status: Optional[str] = None
    recorded_at: Optional[str] = None
    reading_age_seconds: Optional[int] = None
    freshness_window_seconds: int = DEFAULT_FRESHNESS_SECONDS
    provider_name: Optional[str] = None
    remaining_drive_time: HOSFigure = Field(default_factory=HOSFigure.unavailable)
    remaining_on_duty_window: HOSFigure = Field(
        default_factory=HOSFigure.unavailable
    )
    cycle_hours: HOSFigure = Field(default_factory=HOSFigure.unavailable)
    hos_status: Optional[HOSStatus] = None
    advisory: Literal[True] = True
    authoritative_record: Literal["carrier_eld"] = "carrier_eld"
    authoritative_record_statement: str = ELD_AUTHORITATIVE_STATEMENT


class HOSGateVerdict(BaseModel):
    """What the Hours-of-Service gate decided about one driver's transition.

    ``outcome`` is the field the gate stack honours, not ``blocked``: a verdict
    that permits a transition is either ``passed`` (the reading was evaluated) or
    ``skipped`` (it could not be), and collapsing the two would lose exactly the
    distinction R17.18 asks to be audited. ``blocked`` is kept as the derived
    boolean so a caller reading only that field still gets the safe answer.

    The record R17.26 asks for is this model: the acting ``driver_id``, the
    reading ``recorded_at``, the ``freshness_state``, the gate outcome, and
    ``override_id`` when one applied. :meth:`audit_record` is the flattened form
    the gate stack puts on the resulting order event.

    Validates: Requirements 17.17, 17.18, 17.19, 17.20, 17.25, 17.26
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    driver_id: str
    outcome: Literal["passed", "blocked", "skipped"]
    blocked: bool = False
    gating_enabled: bool = False
    reason_code: Optional[str] = None
    freshness_state: Optional[Literal["fresh", "stale", "unknown"]] = None
    recorded_at: Optional[str] = None
    override_id: Optional[str] = None
    audit_outcome: Optional[str] = None

    def audit_record(self) -> Dict[str, Any]:
        """The flattened audit record, with empty values dropped.

        Empty — ``{}`` — for the one verdict that carries no ``audit_outcome``:
        a tenant with gating disabled had no gate, and R17.19 asks for no gate
        rather than for an audit trail of non-events. Every other verdict yields
        a record.

        Carries no reading contents beyond ``recorded_at`` and the freshness
        state, and never another driver's identity.
        """
        if not self.audit_outcome:
            return {}
        record = {
            "driver_id": self.driver_id,
            "outcome": self.audit_outcome or self.outcome,
            "gate_outcome": self.outcome,
            "reason_code": self.reason_code,
            "freshness_state": self.freshness_state,
            "recorded_at": self.recorded_at,
            "override_id": self.override_id,
        }
        return {k: v for k, v in record.items() if v not in (None, "")}


class HOSGateOverride(BaseModel):
    """One dispatcher or admin clearance of a driver's Hours-of-Service gate.

    The field set **is** the ``hos_gate_overrides`` mapping, which is
    ``dynamic: strict`` — a field this model carries and the mapping does not
    fails the write outright, so the two are kept identical on purpose and
    ``extra="forbid"`` catches a drift here before Elasticsearch catches it
    there. ``updated_at`` is the one mapped field absent from the model:
    ``ElasticsearchService.index_document`` stamps it on the way in.

    Three fields are **not** the caller's to supply. ``override_id`` is minted
    server-side (:data:`OVERRIDE_ID_PREFIX`), ``tenant_id`` is stamped from the
    verified scope, and ``actor_id`` is the verified session's user — never a
    body value, so audit attribution cannot be spoofed (R17.23). What the caller
    does supply is the subject ``driver_id``, a non-blank ``reason``, and an
    ``expires_at`` in the future: an override that never lapses is not an
    override, it is a disabled gate.

    This is the write side of the read
    :meth:`HOSAdvisoryService._active_override_id` performs — same index, same
    ``(tenant_id, driver_id)`` pair, same ``expires_at`` comparison.

    Validates: Requirements 17.23, 17.25
    """

    model_config = ConfigDict(extra="forbid")

    override_id: str
    tenant_id: str
    driver_id: str
    actor_id: str
    reason: str
    expires_at: datetime
    created_at: datetime


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class HOSAdvisoryService:
    """Resolves one driver's Hours-of-Service advisory from ``truck_telemetry``.

    Args:
        es_service: The shared ``ElasticsearchService``. Required — it is what
            reads ``truck_telemetry``, and it is the fallback reader for
            ``drivers_current``.
        driver_repository: ``DriverRepository``. Preferred reader of
            ``drivers_current`` because it validates tenant ownership on the way
            in; absent it the record is read with a tenant-filtered search.
        integration_instance_repository: ``IntegrationInstanceRepository``. Read
            for three values — the tenant's freshness-window override and the
            provider name for :meth:`resolve` (R17.9, R17.11), and the
            ``enabled`` field for :meth:`gate_verdict`, which is one of the two
            gating switches of R17.20. :meth:`resolve` never consults
            ``enabled``: an advisory is served whether or not the gate is armed.
        feature_flag_service: ``FeatureFlagService``. Read by
            :meth:`gate_verdict` only, for the overlay key
            ``driver.hos_gating``. Absent or unreachable → the toggle reads as
            disabled, which is the fail-open answer R17.19 requires.
        clock: Zero-arg callable returning the current UTC time. Injected by
            tests so a reading's age and an override's expiry are deterministic.
    """

    def __init__(
        self,
        *,
        es_service,
        driver_repository=None,
        integration_instance_repository=None,
        feature_flag_service=None,
        clock: Any = None,
    ) -> None:
        self._es_service = es_service
        self._driver_repository = driver_repository
        self._integration_instance_repository = integration_instance_repository
        self._feature_flag_service = feature_flag_service
        self._clock = clock or utcnow

    # ------------------------------------------------------------------
    # Resolve (R17.1, R17.3-R17.14)
    # ------------------------------------------------------------------

    async def resolve(self, tenant_id: str, driver_id: str) -> HOSAdvisory:
        """Return the driver's Hours-of-Service advisory.

        Total by construction: every path returns an :class:`HOSAdvisory`
        carrying exactly one of :data:`FRESHNESS_STATES`. Nothing raises for a
        missing truck, a missing reading, or a failed read — an advisory that
        cannot be resolved reports ``unknown`` and says why, because a driver
        opening the screen must not be handed an error where the honest answer
        is "the telematics feed has nothing for your truck".

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The **Runsheet** driver identifier, taken from the
                verified session by the caller.

        Returns:
            The advisory. See :class:`HOSAdvisory` for the field set.

        Validates: Requirements 17.1, 17.3, 17.4, 17.5, 17.6, 17.7, 17.8, 17.9,
        17.10, 17.11, 17.12, 17.13, 17.14
        """
        tenant_id = (tenant_id or "").strip()
        driver_id = (driver_id or "").strip()

        window_seconds, provider_name = await self._resolve_tenant_telematics(
            tenant_id
        )

        truck_id = await self._resolve_truck_id(tenant_id, driver_id)
        if not truck_id:
            # No truck, so there is no device, so there is nothing to read
            # (R17.6). Not an error: an unassigned driver is a normal state.
            return self._unknown(
                tenant_id=tenant_id,
                driver_id=driver_id,
                reason_code=HOS_TRUCK_UNASSIGNED,
                truck_id=None,
                window_seconds=window_seconds,
                provider_name=provider_name,
            )

        reading = await self._resolve_latest_reading(tenant_id, truck_id)
        recorded_at = _parse_timestamp((reading or {}).get("recorded_at"))
        if reading is None or recorded_at is None:
            # Either no document for this truck, or one whose ``recorded_at``
            # cannot be read — an unmapped device produces the first and both
            # answer the same way (R17.7).
            return self._unknown(
                tenant_id=tenant_id,
                driver_id=driver_id,
                reason_code=HOS_NO_READING,
                truck_id=truck_id,
                window_seconds=window_seconds,
                provider_name=provider_name,
            )

        # A reading stamped in the future is clock skew, not a negative age.
        age_seconds = max(
            0, int(round((self._now() - recorded_at).total_seconds()))
        )
        duty_status = _text(reading.get("hos_status"))
        recorded_at_iso = recorded_at.isoformat()

        if age_seconds > window_seconds:
            # Stale: the age and the duty status are still reported, because a
            # driver is better served by "this is 40 minutes old" than by
            # silence — but every figure is ``unavailable`` and the compliance
            # state is ``unknown`` (R17.8, R17.10).
            return HOSAdvisory(
                tenant_id=tenant_id,
                driver_id=driver_id,
                freshness_state="stale",
                compliance_state="unknown",
                reason_code=HOS_READING_STALE,
                truck_id=truck_id,
                duty_status=duty_status,
                recorded_at=recorded_at_iso,
                reading_age_seconds=age_seconds,
                freshness_window_seconds=window_seconds,
                provider_name=provider_name,
            )

        # Fresh. The three figures come from the reading when it carries them
        # and are ``unavailable`` otherwise, which is every reading the Geotab
        # connector writes today (R17.13).
        hos_status = hos_status_from_reading(
            reading,
            driver_id=driver_id,
            recorded_at=recorded_at,
            provider_name=provider_name,
        )
        if hos_status is None:
            return HOSAdvisory(
                tenant_id=tenant_id,
                driver_id=driver_id,
                freshness_state="fresh",
                # No remaining-hours figure exists, so "within limits" is not
                # something this service knows (R17.10, R17.13).
                compliance_state="unknown",
                reason_code=None,
                truck_id=truck_id,
                duty_status=duty_status,
                recorded_at=recorded_at_iso,
                reading_age_seconds=age_seconds,
                freshness_window_seconds=window_seconds,
                provider_name=provider_name,
            )

        return HOSAdvisory(
            tenant_id=tenant_id,
            driver_id=driver_id,
            freshness_state="fresh",
            compliance_state=(
                "at_limit"
                if hos_status.available_drive_hours <= 0
                else "within_limits"
            ),
            reason_code=None,
            truck_id=truck_id,
            duty_status=duty_status,
            recorded_at=recorded_at_iso,
            reading_age_seconds=age_seconds,
            freshness_window_seconds=window_seconds,
            provider_name=provider_name,
            remaining_drive_time=HOSFigure.hours(
                hos_status.available_drive_hours
            ),
            remaining_on_duty_window=HOSFigure.hours(
                hos_status.available_window_hours
            ),
            cycle_hours=HOSFigure.hours(hos_status.cumulative_cycle_hours),
            hos_status=hos_status,
        )

    # ------------------------------------------------------------------
    # Gate verdict (R17.17-R17.21, R17.25, R17.26)
    # ------------------------------------------------------------------

    async def gate_verdict(self, tenant_id: str, driver_id: str) -> HOSGateVerdict:
        """Return the Hours-of-Service gate verdict for one driver.

        Consumed by ``DriverTransitionGateStack._gate_hos``, which raises 409
        ``HOS_LIMIT_REACHED`` for the single ``blocked`` outcome and records
        every other verdict on the evaluation.

        The checks run cheapest-and-most-decisive first, which is also the order
        the requirements read in:

        1. **The overlay toggle** (R17.19, R17.20). Not enforcing → no gate at
           all: ``skipped`` with :data:`HOS_GATING_DISABLED`, **no** audit
           outcome, and no read of ``truck_telemetry`` at all.
        2. **The ``gps_eld`` instance** (R17.18, R17.20). No enabled instance →
           ``skipped`` with :data:`HOS_GPS_ELD_DISABLED` and the audit outcome
           ``hos_gate_skipped``.
        3. **The reading.** ``stale`` or ``unknown`` → ``skipped`` carrying the
           advisory's own reason code (R17.18). Fresh but figure-less → skipped
           with :data:`HOS_FIGURES_UNAVAILABLE`, which is every reading the
           Geotab connector writes (R17.13).
        4. **The limit, then the override.** At or past a limit with an unexpired
           override → ``passed`` carrying the ``override_id`` (R17.25); without
           one → ``blocked`` (R17.17).

        Nothing here raises. A read failure at any step degrades to a skip,
        because a telemetry outage must not stop a driver from starting a run.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The acting **Runsheet** driver, from the verified session.

        Returns:
            The verdict. See :class:`HOSGateVerdict`.

        Validates: Requirements 17.17, 17.18, 17.19, 17.20, 17.25, 17.26
        """
        tenant_id = (tenant_id or "").strip()
        driver_id = (driver_id or "").strip()

        if not await self._gating_toggled_on(tenant_id):
            # R17.19 — no gate at all. Deliberately no audit outcome: there was
            # no gate to skip, and an audit trail of non-events is noise.
            return HOSGateVerdict(
                tenant_id=tenant_id,
                driver_id=driver_id,
                outcome="skipped",
                gating_enabled=False,
                reason_code=HOS_GATING_DISABLED,
            )

        if not await self._gps_eld_enabled(tenant_id):
            return self._skip(
                tenant_id=tenant_id,
                driver_id=driver_id,
                reason_code=HOS_GPS_ELD_DISABLED,
            )

        advisory = await self.resolve(tenant_id, driver_id)

        if advisory.freshness_state != "fresh":
            # R17.18 — the fail-open core. A reading nobody can vouch for never
            # stops a driver.
            return self._skip(
                tenant_id=tenant_id,
                driver_id=driver_id,
                reason_code=advisory.reason_code,
                freshness_state=advisory.freshness_state,
                recorded_at=advisory.recorded_at,
            )

        if advisory.remaining_drive_time.availability != "available":
            # Fresh, but the connector supplies no remaining-drive-time figure,
            # so there is no limit to be at or past (R17.13). The gate cannot
            # normally be armed in this state at all — R17.21 refuses the
            # request — but a connector that stops supplying figures after the
            # fact must degrade to a skip rather than to a block.
            return self._skip(
                tenant_id=tenant_id,
                driver_id=driver_id,
                reason_code=HOS_FIGURES_UNAVAILABLE,
                freshness_state=advisory.freshness_state,
                recorded_at=advisory.recorded_at,
            )

        if advisory.compliance_state != "at_limit":
            return HOSGateVerdict(
                tenant_id=tenant_id,
                driver_id=driver_id,
                outcome="passed",
                gating_enabled=True,
                freshness_state=advisory.freshness_state,
                recorded_at=advisory.recorded_at,
                audit_outcome=AUDIT_GATE_PASSED,
            )

        override_id = await self._active_override_id(tenant_id, driver_id)
        if override_id:
            # R17.25 — an unexpired override permits the transition and its
            # identifier travels on the verdict so it lands on the order event.
            return HOSGateVerdict(
                tenant_id=tenant_id,
                driver_id=driver_id,
                outcome="passed",
                gating_enabled=True,
                reason_code=HOS_OVERRIDE_APPLIED,
                freshness_state=advisory.freshness_state,
                recorded_at=advisory.recorded_at,
                override_id=override_id,
                audit_outcome=AUDIT_GATE_OVERRIDDEN,
            )

        # The one blocking verdict (R17.17).
        return HOSGateVerdict(
            tenant_id=tenant_id,
            driver_id=driver_id,
            outcome="blocked",
            blocked=True,
            gating_enabled=True,
            reason_code=HOS_AT_LIMIT,
            freshness_state=advisory.freshness_state,
            recorded_at=advisory.recorded_at,
            audit_outcome=AUDIT_GATE_BLOCKED,
        )

    @staticmethod
    def _skip(
        *,
        tenant_id: str,
        driver_id: str,
        reason_code: Optional[str],
        freshness_state: Optional[str] = None,
        recorded_at: Optional[str] = None,
    ) -> HOSGateVerdict:
        """Build the permitted-but-unevaluated verdict R17.18 describes."""
        return HOSGateVerdict(
            tenant_id=tenant_id,
            driver_id=driver_id,
            outcome="skipped",
            gating_enabled=True,
            reason_code=reason_code,
            freshness_state=freshness_state,
            recorded_at=recorded_at,
            audit_outcome=AUDIT_GATE_SKIPPED,
        )

    # ------------------------------------------------------------------
    # Arming the gate (R17.21)
    # ------------------------------------------------------------------

    async def assert_gating_can_be_enabled(self, tenant_id: str) -> None:
        """Refuse to arm the gate against data that does not exist (R17.21).

        Called by whatever surface flips ``driver.hos_gating`` on. A tenant whose
        telematics connector supplies no remaining-drive-time figure is refused
        with 409 ``HOS_FIGURES_UNAVAILABLE`` — which is every tenant on the
        Geotab connector as built, because ``truck_telemetry`` is
        ``dynamic: strict`` and declares no such field.

        Raises:
            AppException: 409 ``HOS_FIGURES_UNAVAILABLE``.

        Validates: Requirements 17.21
        """
        if await self.supplies_remaining_drive_time(tenant_id):
            return
        raise hos_figures_unavailable(
            message=(
                "Hours-of-service gating cannot be enabled: this tenant's "
                "telematics connector supplies no remaining-drive-time figure"
            ),
            details={
                "reason": "no_remaining_drive_time_figure",
                "flag_key": HOS_GATING_FLAG_KEY,
            },
        )

    async def supplies_remaining_drive_time(self, tenant_id: str) -> bool:
        """Whether the tenant's connector supplies a remaining-drive-time figure.

        Two sources, in order. A ``gps_eld`` instance may **declare** the
        capability through the :data:`FIGURES_CONFIG_KEY` config flag, which lets
        a genuinely capable connector arm the gate before its first reading
        lands. Absent that declaration the answer comes from the readings
        themselves: the most recent :data:`FIGURE_PROBE_SIZE` documents for the
        tenant are sampled and the answer is yes only if one of them carries a
        drive-hours key.

        Both paths answer ``False`` for the Geotab connector, and a read failure
        answers ``False`` too — an unverifiable capability is not a capability.
        """
        tenant_id = (tenant_id or "").strip()
        if not tenant_id:
            return False

        for document in await self._gps_eld_documents(tenant_id):
            declared = (document.get("config") or {}).get(FIGURES_CONFIG_KEY)
            if declared is True or str(declared).strip().lower() in ("true", "1", "yes"):
                return True

        es = self._es_service
        if es is None:
            return False

        query = inject_tenant_filter({"query": {"bool": {"filter": []}}}, tenant_id)
        query["size"] = FIGURE_PROBE_SIZE
        query["sort"] = [{"recorded_at": {"order": "desc"}}]
        query["_source"] = {"excludes": list(EXCLUDED_READING_FIELDS)}
        try:
            response = await es.search_documents(
                TRUCK_TELEMETRY_INDEX, query, FIGURE_PROBE_SIZE
            )
        except Exception as exc:
            logger.warning(
                "HOSAdvisoryService: truck_telemetry figure probe failed for "
                "tenant=%s (%s) — reporting no remaining-drive-time figure",
                tenant_id,
                exc,
            )
            return False

        for source in _sources(response):
            # Per-document tenant re-validation, as everywhere else here.
            if source.get("tenant_id") != tenant_id:
                continue
            if _first_float(source, _DRIVE_HOURS_KEYS) is not None:
                return True
        return False

    # ------------------------------------------------------------------
    # The override write (R17.23)
    # ------------------------------------------------------------------

    async def record_override(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        actor_id: str,
        reason: str,
        expires_at: datetime,
    ) -> HOSGateOverride:
        """Persist a dispatcher or admin clearance of one driver's gate.

        The write side of :meth:`_active_override_id`. The role gate is the
        router's — ``dispatcher`` or ``admin``, matched exactly (R17.23, R17.24);
        what this method owns is the document: a server-minted ``override_id``, a
        ``tenant_id`` and an ``actor_id`` the caller cannot influence, a reason
        that is not blank, and an expiry that is genuinely in the future.

        ``actor_id`` is a **required keyword** rather than something read off a
        payload, so there is no call shape in which a body value could reach the
        stored attribution (R17.23). An ``expires_at`` at or before now is
        refused rather than stored: it would clear nothing, and a caller that
        believes it cleared a gate is worse than a caller told it did not.

        Unlike every read here, this method **raises**. A read that cannot be
        resolved has an honest conservative answer; a write that did not land has
        none, and reporting success for an override the gate will never see would
        put a driver on the road on the strength of a clearance that does not
        exist.

        Args:
            tenant_id: The verified tenant scope, stamped onto the document.
            driver_id: The subject driver — the caller's to name, since a
                dispatcher clears someone else's gate by definition.
            actor_id: The verified session's user identifier.
            reason: The audit note. Non-blank after stripping, and truncated to
                :data:`MAX_OVERRIDE_REASON_LENGTH`.
            expires_at: When the clearance lapses. Must be in the future.

        Returns:
            The persisted :class:`HOSGateOverride`.

        Raises:
            AppException: 400 ``INVALID_REQUEST`` for a blank reason, an expiry
                at or before now, or a missing scope; 503
                ``ELASTICSEARCH_UNAVAILABLE`` when the override cannot be
                persisted.

        Validates: Requirements 17.23, 17.24, 17.25
        """
        tenant_id = (tenant_id or "").strip()
        driver_id = (driver_id or "").strip()
        actor_id = (actor_id or "").strip()

        if not tenant_id or not actor_id:
            # Neither is client-supplied, so this is a wiring fault rather than
            # a bad request — but it must never become a document with a blank
            # scope or a blank actor.
            raise invalid_request(
                message="An hours-of-service override requires a verified tenant and actor",
                details={"reason": "unverified_override_context"},
            )

        if not driver_id:
            raise invalid_request(
                message="An hours-of-service override must name a driver",
                details={"reason": "driver_id_required"},
            )

        cleaned_reason = (reason or "").strip()
        if not cleaned_reason:
            raise invalid_request(
                message="An hours-of-service override requires a non-blank reason",
                details={"reason": "reason_required"},
            )
        cleaned_reason = cleaned_reason[:MAX_OVERRIDE_REASON_LENGTH]

        expiry = expires_at
        if expiry is None:
            raise invalid_request(
                message="An hours-of-service override requires an expiry timestamp",
                details={"reason": "expires_at_required"},
            )
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        now = self._now()
        if expiry <= now:
            raise invalid_request(
                message="An hours-of-service override must expire in the future",
                details={"reason": "expires_at_not_in_future"},
            )

        override = HOSGateOverride(
            override_id=f"{OVERRIDE_ID_PREFIX}{uuid4().hex}",
            tenant_id=tenant_id,
            driver_id=driver_id,
            actor_id=actor_id,
            reason=cleaned_reason,
            expires_at=expiry,
            created_at=now,
        )

        es = self._es_service
        if es is None:
            logger.error(
                "HOSAdvisoryService has no es_service; the override for "
                "tenant=%s driver=%s cannot be persisted",
                tenant_id,
                driver_id,
            )
            raise elasticsearch_unavailable(
                message="The hours-of-service override could not be recorded",
                details={"reason": "override_store_unavailable"},
            )

        try:
            await es.index_document(
                HOS_GATE_OVERRIDES_INDEX,
                override.override_id,
                override.model_dump(mode="json"),
            )
        except Exception as exc:
            logger.exception(
                "HOSAdvisoryService: hos_gate_overrides write failed for "
                "tenant=%s driver=%s override=%s: %s",
                tenant_id,
                driver_id,
                override.override_id,
                exc,
            )
            raise elasticsearch_unavailable(
                message="The hours-of-service override could not be recorded",
                details={"reason": "override_write_failed"},
            )

        logger.info(
            "HOSAdvisoryService: hos gate override tenant=%s driver=%s "
            "override=%s actor=%s expires=%s",
            tenant_id,
            driver_id,
            override.override_id,
            override.actor_id,
            override.expires_at.isoformat(),
        )
        return override

    # ------------------------------------------------------------------
    # Internals — the two gating switches (R17.20)
    # ------------------------------------------------------------------

    async def _gating_toggled_on(self, tenant_id: str) -> bool:
        """Read the ``driver.hos_gating`` overlay toggle, defaulting to off.

        An absent service, an unreachable Redis, or any read failure means
        disabled — the documented fail-closed-to-off posture of
        ``get_overlay_state``, which here is also the fail-*open* posture of the
        gate.
        """
        get_state = getattr(self._feature_flag_service, "get_overlay_state", None)
        if not callable(get_state) or not tenant_id:
            return False
        try:
            state = await get_state(HOS_GATING_FLAG_KEY, tenant_id)
        except Exception as exc:
            logger.warning(
                "HOSAdvisoryService: %s unreadable for tenant=%s (%s) — "
                "treating gating as disabled",
                HOS_GATING_FLAG_KEY,
                tenant_id,
                exc,
            )
            return False
        return state in ENFORCING_OVERLAY_STATES

    async def _gps_eld_enabled(self, tenant_id: str) -> bool:
        """Whether the tenant has an **enabled** ``gps_eld`` instance (R17.20).

        ``IntegrationInstance.enabled`` defaults to ``False``
        (``integrations/connector_base.py:169-176``), and a tenant with no
        instance at all, or an unreadable repository, answers ``False`` too.
        """
        for document in await self._gps_eld_documents(tenant_id):
            if document.get("enabled") is True:
                return True
        return False

    async def _active_override_id(
        self, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        """The identifier of an unexpired gate override, or ``None`` (R17.25).

        Reads ``hos_gate_overrides`` for the ``(tenant_id, driver_id)`` pair with
        ``expires_at`` in the future, newest first. The write side is the
        dispatcher override endpoint; this is the read the gate makes.

        A read failure returns ``None``, which is the conservative answer *for
        the override* — the gate still only blocks a fresh at-limit reading.
        """
        es = self._es_service
        if es is None or not tenant_id or not driver_id:
            return None

        now = self._now()
        query = inject_tenant_filter(
            {
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"driver_id": driver_id}},
                            {"range": {"expires_at": {"gt": now.isoformat()}}},
                        ]
                    }
                }
            },
            tenant_id,
        )
        query["size"] = 1
        query["sort"] = [{"expires_at": {"order": "desc"}}]

        try:
            response = await es.search_documents(HOS_GATE_OVERRIDES_INDEX, query, 1)
        except Exception as exc:
            logger.warning(
                "HOSAdvisoryService: hos_gate_overrides read failed for "
                "tenant=%s driver=%s: %s",
                tenant_id,
                driver_id,
                exc,
            )
            return None

        for source in _sources(response):
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "HOSAdvisoryService: dropping hos_gate_overrides document "
                    "labelled tenant=%s while resolving tenant=%s",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            if source.get("driver_id") != driver_id:
                continue
            # The range filter is Elasticsearch's; re-check it here so a
            # mis-typed ``expires_at`` cannot silently clear a gate forever.
            expires_at = _parse_timestamp(source.get("expires_at"))
            if expires_at is None or expires_at <= now:
                continue
            return _text(source.get("override_id"))
        return None

    # ------------------------------------------------------------------
    # Internals — the unknown advisory
    # ------------------------------------------------------------------

    def _unknown(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        reason_code: str,
        truck_id: Optional[str],
        window_seconds: int,
        provider_name: Optional[str],
    ) -> HOSAdvisory:
        """Build the ``unknown`` advisory for one of the two reason codes.

        Every remaining-hours figure defaults to ``unavailable`` and the
        compliance state is ``unknown``, never within limits (R17.8, R17.10).
        """
        return HOSAdvisory(
            tenant_id=tenant_id,
            driver_id=driver_id,
            freshness_state="unknown",
            compliance_state="unknown",
            reason_code=reason_code,
            truck_id=truck_id,
            freshness_window_seconds=window_seconds,
            provider_name=provider_name,
        )

    # ------------------------------------------------------------------
    # Internals — reads
    # ------------------------------------------------------------------

    async def _resolve_truck_id(
        self, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        """Return ``drivers_current.assigned_truck_id``, or ``None`` (R17.3).

        A driver with no ``drivers_current`` record and a driver whose record
        carries no ``assigned_truck_id`` are the same answer here: there is no
        truck, so R17.6 applies.
        """
        record = await self._read_driver_document(tenant_id, driver_id)
        if record is None:
            return None
        return _text(record.get("assigned_truck_id"))

    async def _read_driver_document(
        self, tenant_id: str, driver_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the ``drivers_current`` document for the driver, or ``None``."""
        if self._driver_repository is not None:
            try:
                return _as_document(
                    await self._driver_repository.get(tenant_id, driver_id)
                )
            except Exception as exc:
                logger.warning(
                    "HOSAdvisoryService: drivers_current read failed for "
                    "tenant=%s driver=%s: %s",
                    tenant_id,
                    driver_id,
                    exc,
                )
                return None

        es = self._es_service
        if es is None:
            logger.error(
                "HOSAdvisoryService has neither a driver_repository nor an "
                "es_service; the advisory for tenant=%s driver=%s resolves to "
                "unknown",
                tenant_id,
                driver_id,
            )
            return None

        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"driver_id": driver_id}}]}}},
            tenant_id,
        )
        query["size"] = 1
        try:
            response = await es.search_documents(DRIVERS_CURRENT_INDEX, query, 1)
        except Exception as exc:
            logger.warning(
                "HOSAdvisoryService: drivers_current read failed for tenant=%s "
                "driver=%s: %s",
                tenant_id,
                driver_id,
                exc,
            )
            return None

        # Per-document tenant re-validation: a filter regression drops the
        # document rather than resolving another tenant's truck.
        return next(
            (
                source
                for source in _sources(response)
                if source.get("tenant_id") == tenant_id
                and source.get("driver_id") == driver_id
            ),
            None,
        )

    async def _resolve_latest_reading(
        self, tenant_id: str, truck_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the greatest-``recorded_at`` reading for the truck (R17.4).

        The filter is ``(tenant_id, truck_id)`` and nothing else. There is no
        ``driver_id`` clause and the field is excluded from ``_source``, so the
        vendor's driver identifier cannot participate in the resolution even by
        accident (R17.5).
        """
        es = self._es_service
        if es is None:
            logger.error(
                "HOSAdvisoryService has no es_service; the truck_telemetry read "
                "for tenant=%s truck=%s resolves to no reading",
                tenant_id,
                truck_id,
            )
            return None

        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"truck_id": truck_id}}]}}},
            tenant_id,
        )
        query["size"] = 1
        query["sort"] = [{"recorded_at": {"order": "desc"}}]
        query["_source"] = {"excludes": list(EXCLUDED_READING_FIELDS)}

        try:
            response = await es.search_documents(TRUCK_TELEMETRY_INDEX, query, 1)
        except Exception as exc:
            # A read failure is an unresolved advisory, not a resolved one with
            # optimistic contents.
            logger.warning(
                "HOSAdvisoryService: truck_telemetry read failed for tenant=%s "
                "truck=%s: %s",
                tenant_id,
                truck_id,
                exc,
            )
            return None

        for source in _sources(response):
            if source.get("tenant_id") != tenant_id:
                logger.warning(
                    "HOSAdvisoryService: dropping truck_telemetry document "
                    "labelled tenant=%s while resolving tenant=%s",
                    source.get("tenant_id"),
                    tenant_id,
                )
                continue
            if source.get("truck_id") != truck_id:
                continue
            # Defense in depth behind the ``_source`` exclusion: if the field
            # arrives anyway, it is dropped before anything can read it (R17.5).
            for field in EXCLUDED_READING_FIELDS:
                source.pop(field, None)
            return source
        return None

    async def _resolve_tenant_telematics(
        self, tenant_id: str
    ) -> Tuple[int, Optional[str]]:
        """Return the tenant's ``(freshness_window_seconds, provider_name)``.

        The window defaults to :data:`DEFAULT_FRESHNESS_SECONDS` and is
        overridden by the ``hos_freshness_seconds`` key on the tenant's
        ``gps_eld`` ``IntegrationInstance.config`` (R17.9). The provider name is
        that instance's own ``provider_name``, so no provider identifier is
        hard-wired into the resolution; :data:`DEFAULT_PROVIDER_NAME` is the
        fallback for a tenant whose instance cannot be read.

        ``enabled`` is not consulted **here**: it is the gating switch of R17.20,
        read by :meth:`gate_verdict`, and the advisory is served whether or not
        the gate is armed.
        """
        documents = await self._gps_eld_documents(tenant_id)

        window_seconds = DEFAULT_FRESHNESS_SECONDS
        provider_name: Optional[str] = None
        for document in documents:
            provider_name = provider_name or _text(document.get("provider_name"))
            override = self._validated_window(
                (document.get("config") or {}).get(FRESHNESS_CONFIG_KEY),
                tenant_id=tenant_id,
            )
            if override is not None:
                window_seconds = override
                break

        return window_seconds, provider_name or DEFAULT_PROVIDER_NAME

    async def _gps_eld_documents(self, tenant_id: str) -> List[Dict[str, Any]]:
        """The tenant's ``gps_eld`` ``IntegrationInstance`` documents.

        The single read behind three values: the freshness-window override and
        the provider name for :meth:`resolve`, and ``enabled`` for
        :meth:`gate_verdict`. An unreadable repository yields an empty list,
        which every caller treats as "no override, no provider, not enabled".
        """
        repository = self._integration_instance_repository
        if repository is None or not tenant_id:
            return []

        try:
            instances = await repository.list_for_tenant(
                tenant_id, category=GPS_ELD_CATEGORY
            )
        except Exception as exc:
            logger.warning(
                "HOSAdvisoryService: gps_eld instance read failed for "
                "tenant=%s (%s) — falling back to the %ds default window with "
                "gating treated as disabled",
                tenant_id,
                exc,
                DEFAULT_FRESHNESS_SECONDS,
            )
            return []

        documents: List[Dict[str, Any]] = []
        for instance in instances or []:
            document = _as_document(instance) or {}
            # Per-document tenant re-validation: a repository whose filter
            # regressed must not hand this tenant another tenant's connector.
            declared = document.get("tenant_id")
            if declared is not None and declared != tenant_id:
                logger.warning(
                    "HOSAdvisoryService: dropping gps_eld instance labelled "
                    "tenant=%s while resolving tenant=%s",
                    declared,
                    tenant_id,
                )
                continue
            documents.append(document)
        return documents

    @staticmethod
    def _validated_window(value: Any, *, tenant_id: str) -> Optional[int]:
        """Return a positive integer freshness override, or ``None``.

        A zero, a negative, or a non-numeric value is ignored rather than
        honoured: "0 seconds" would make every reading stale and a
        non-numeric value would make the window undefined, and in both cases
        the documented 300-second default is the safer answer.
        """
        if value is None or isinstance(value, bool):
            return None
        try:
            seconds = int(float(value))
        except (TypeError, ValueError):
            logger.warning(
                "HOSAdvisoryService: ignoring non-numeric %s=%r for tenant=%s",
                FRESHNESS_CONFIG_KEY,
                value,
                tenant_id,
            )
            return None
        if seconds <= 0:
            logger.warning(
                "HOSAdvisoryService: ignoring non-positive %s=%r for tenant=%s",
                FRESHNESS_CONFIG_KEY,
                value,
                tenant_id,
            )
            return None
        return seconds

    def _now(self) -> datetime:
        """Return the current UTC time as an aware datetime."""
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment


__all__ = [
    "AUDIT_GATE_BLOCKED",
    "AUDIT_GATE_OVERRIDDEN",
    "AUDIT_GATE_PASSED",
    "AUDIT_GATE_SKIPPED",
    "COMPLIANCE_STATES",
    "DEFAULT_FRESHNESS_SECONDS",
    "DEFAULT_PROVIDER_NAME",
    "ELD_AUTHORITATIVE_STATEMENT",
    "ENFORCING_OVERLAY_STATES",
    "EXCLUDED_READING_FIELDS",
    "FIGURES_CONFIG_KEY",
    "FIGURE_PROBE_SIZE",
    "FRESHNESS_CONFIG_KEY",
    "FRESHNESS_STATES",
    "GATE_OUTCOMES",
    "GPS_ELD_CATEGORY",
    "HOS_AT_LIMIT",
    "HOS_FIGURES_UNAVAILABLE",
    "HOS_GATING_DISABLED",
    "HOS_GATING_FLAG_KEY",
    "HOS_GPS_ELD_DISABLED",
    "HOS_NO_READING",
    "HOS_OVERRIDE_APPLIED",
    "HOS_READING_STALE",
    "HOS_TRUCK_UNASSIGNED",
    "HOURS_UNIT",
    "MAX_OVERRIDE_REASON_LENGTH",
    "OVERRIDE_ID_PREFIX",
    "OVERRIDE_ROLES",
    "UNKNOWN_REASON_CODES",
    "HOSAdvisory",
    "HOSAdvisoryService",
    "HOSFigure",
    "HOSGateOverride",
    "HOSGateVerdict",
    "hos_status_from_reading",
]
