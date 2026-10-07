"""Dispatch Board models: draft document, commands, checks and API bodies.

Dispatch-board design K2 (data model), K4.1 (command union), K5 (snapshot) and
"External input validation". Every model is ``extra="forbid"``.

Request bodies are never FastAPI body parameters: FastAPI's default
``RequestValidationError`` handler echoes ``input`` (for a missing field, the
whole body), and board bodies carry free-text reasons. Handlers take
``body: Any = Body(...)`` and call :func:`parse_body`, which answers 422 with
field paths and Pydantic error types only, never values.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Type, TypeVar, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
)

from errors.codes import ErrorCode
from errors.exceptions import AppException

# ---------------------------------------------------------------------------
# Limits (K15) and shared field types
# ---------------------------------------------------------------------------

#: Lanes per draft; more are refused with ``limit_exceeded`` (K15).
MAX_LANES = 60
#: Stops per load; more are refused with ``limit_exceeded`` (K15).
MAX_STOPS_PER_LOAD = 30
#: Orders named by one command (K4.1).
MAX_COMMAND_ORDERS = 25
#: Candidates per validate call (K3.5).
MAX_CANDIDATES = 60
#: Compartment shares per allocation override.
MAX_SHARES = 12
#: Command ids remembered on the draft (K4.5).
APPLIED_COMMANDS_LIMIT = 500
#: Publish request ids remembered on the draft (K7.5).
PUBLISHES_LIMIT = 20

ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
WARNING_ID_PATTERN = r"^[0-9a-f]{16}$"
PRODUCT_FILTER_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
_ID_RE = re.compile(ID_PATTERN)
_PRODUCT_RE = re.compile(PRODUCT_FILTER_PATTERN)
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

EntityId = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
ClientId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
WarningId = Annotated[str, StringConstraints(pattern=WARNING_ID_PATTERN)]


def _check_reason(value: str) -> str:
    """Trimmed 3-500 characters, no control characters except newline."""
    trimmed = value.strip()
    if not 3 <= len(trimmed) <= 500:
        raise ValueError("reason must be 3-500 characters")
    if _CONTROL_RE.search(trimmed):
        raise ValueError("reason contains control characters")
    return trimmed


Reason = Annotated[str, AfterValidator(_check_reason)]

ShiftId = Literal["day", "night", "all"]
InputModality = Literal["drag", "menu", "place", "keyboard", "suggestion", "system"]
CallType = Literal["keep_full", "auto_fill", "will_call", "one_off"]
WindowFilter = Literal["overdue", "today", "later"]
Outcome = Literal["pass", "warn", "block", "info"]
BoardMode = Literal["disabled", "shadow", "active_gated", "active_auto"]
CheckId = Literal[
    "order_state",
    "delivery_window",
    "driver_qualification",
    "driver_pairing",
    "hos",
    "asset_certification",
    "compartment_fit",
    "compartment_compatibility",
    "dyed_diesel",
    "schedule",
    "terminal_supply",
    "post_publish",
    "suggestion",
]

#: Q2 shift presets (B6: tenant-configurable later).
SHIFTS: List[Dict[str, str]] = [
    {"id": "day", "start": "06:00", "end": "18:00"},
    {"id": "night", "start": "18:00", "end": "06:00"},
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Draft document (K2.1)
# ---------------------------------------------------------------------------


class LatLon(_Strict):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class DeliveryWindow(_Strict):
    start: Optional[datetime] = None
    end: Optional[datetime] = None


class OrderSnapshot(_Strict):
    """The order as it was when the stop was inserted (K2.1)."""

    product_code: Optional[str] = None
    customer_id: Optional[str] = None
    customer_tank_id: Optional[str] = None
    gallons_requested: Optional[float] = None
    fill_to_full: bool = False
    window: DeliveryWindow = Field(default_factory=DeliveryWindow)
    call_type: Optional[str] = None
    status: Optional[str] = None


class Stop(_Strict):
    order_id: str
    snapshot: OrderSnapshot
    location: Optional[LatLon] = None
    eta: Optional[datetime] = None


class CompartmentShare(_Strict):
    compartment_id: EntityId
    liters: float = Field(gt=0)


class Allocation(_Strict):
    order_id: Optional[str] = None
    compartment_id: str
    product_code: Optional[str] = None
    liters: float
    capacity_liters: float


class Load(_Strict):
    load_id: str
    shift_id: ShiftId = "day"
    terminal_id: Optional[str] = None
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    stops: List[Stop] = Field(default_factory=list)
    allocation_overrides: Dict[str, List[CompartmentShare]] = Field(default_factory=dict)
    allocations: List[Allocation] = Field(default_factory=list)
    source: Literal["dispatcher", "suggestion"] = "dispatcher"
    suggestion_id: Optional[str] = None


class Scope(_Strict):
    truck_id: str
    load_id: Optional[str] = None
    order_id: Optional[str] = None


class FixLink(_Strict):
    """What fixes a check. ``move_back`` names the lane and load to move to (freeze rule 11 (e))."""

    kind: Literal["driver", "asset", "order", "hos_override", "compartment", "terminal", "move_back"]
    id: str
    truck_id: Optional[str] = None
    load_id: Optional[str] = None


class Check(_Strict):
    check: CheckId
    outcome: Outcome
    reason_code: str
    message: str
    source: str
    scope: Scope
    warning_id: Optional[str] = None
    fix_link: Optional[FixLink] = None


class WarningAck(_Strict):
    reason: str
    actor_user_id: str
    at: datetime
    truck_id: str


class AppliedCommand(_Strict):
    draft_version: int
    payload_hash: str
    at: datetime


class Dismissal(_Strict):
    actor_user_id: str
    at: datetime


class PublishedPlan(_Strict):
    plan_id: str
    route_id: str
    run_id: str
    revision: int = Field(ge=1)


class LaneContent(_Strict):
    """The dispatcher-owned part of a lane: what ``content_hash`` covers."""

    driver_id: Optional[str] = None
    loads: List[Load] = Field(default_factory=list)
    shelf: List[str] = Field(default_factory=list)


class PublishResult(_Strict):
    state: Optional[str] = None
    stage: Optional[str] = None
    reason: Optional[str] = None
    writes_made: Optional[Union[bool, Literal["unknown"]]] = None
    retryable: Optional[bool] = None
    failures: List[Dict[str, Any]] = Field(default_factory=list)
    rolled_back: Optional[bool] = None
    recovery: Optional[str] = None
    dropped_orders: List[str] = Field(default_factory=list)
    notifications_failed: List[str] = Field(default_factory=list)
    #: Phase 2 (K8.4): the order a relink refused on and the status it had.
    order_id: Optional[str] = None
    observed_status: Optional[str] = None
    #: Amend kept a stop a driver completed concurrently (K8.4 phase 4).
    kept_completed_stops: List[str] = Field(default_factory=list)
    #: Drivers with no manifest while a forward recovery is pending (banner, Q16).
    drivers_without_routes: List[str] = Field(default_factory=list)
    publish_id: Optional[str] = None
    at: Optional[datetime] = None


class Link(_Strict):
    run_id: Optional[str] = None
    truck_id: Optional[str] = None
    driver_id: Optional[str] = None


class AttemptLoad(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    load_class: Literal["unchanged", "new", "new_revision", "amend", "removed"] = Field(alias="class")
    plan_id: str
    route_id: str
    run_id: str
    revision: int = Field(ge=1)


class AttemptRelink(_Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    order_id: str
    from_link: Link = Field(alias="from")
    to: Link


class RedispatchAttempt(_Strict):
    """The fixed record of one redispatch group attempt (K2.1, K8.4)."""

    attempt_id: str
    group_truck_ids: List[str]
    phase: Literal["claimed", "retire", "stage_relink", "apply", "amend", "notify", "finalize"] = "claimed"
    loads: Dict[str, AttemptLoad] = Field(default_factory=dict)
    relinks: List[AttemptRelink] = Field(default_factory=list)
    notify_baseline: Dict[str, List[str]] = Field(default_factory=dict)
    recovery: Literal["none", "rollback", "forward"] = "none"


class LanePublish(_Strict):
    state: Literal["draft", "publishing", "published", "failed"] = "draft"
    attempt_id: Optional[str] = None
    lease_until: Optional[datetime] = None
    published_version: Optional[int] = None
    published_content: Optional[LaneContent] = None
    published_hash: Optional[str] = None
    plans: Dict[str, PublishedPlan] = Field(default_factory=dict)
    last_result: Optional[PublishResult] = None
    attempt: Optional[RedispatchAttempt] = None


class Lane(_Strict):
    truck_id: str
    version: int = Field(ge=0)
    driver_id: Optional[str] = None
    loads: List[Load] = Field(default_factory=list)
    shelf: List[str] = Field(default_factory=list)
    checks: List[Check] = Field(default_factory=list)
    checks_computed_at: Optional[datetime] = None
    checks_stale: bool = False
    publish: LanePublish = Field(default_factory=LanePublish)

    def content(self) -> LaneContent:
        return LaneContent(driver_id=self.driver_id, loads=self.loads, shelf=self.shelf)


class BoardDraft(_Strict):
    tenant_id: str
    service_date: date
    timezone: str
    draft_version: int = Field(default=0, ge=0)
    lanes: Dict[str, Lane] = Field(default_factory=dict)
    order_index: Dict[str, str] = Field(default_factory=dict)
    order_ids: List[str] = Field(default_factory=list)
    acknowledged: Dict[str, WarningAck] = Field(default_factory=dict)
    applied_commands: Dict[str, AppliedCommand] = Field(default_factory=dict)
    dismissed_suggestions: Dict[str, Dismissal] = Field(default_factory=dict)
    publishes: Dict[str, str] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def draft_doc_id(tenant_id: str, service_date: date) -> str:
    """``"{tenant_id}:{service_date}"`` (K2.1)."""
    return f"{tenant_id}:{service_date.isoformat()}"


def is_in_recovery(lane: Lane, now: datetime) -> bool:
    """A lane whose attempt is set and is failed, or publishing with an expired lease (K2.1)."""
    publish = lane.publish
    if publish.attempt is None:
        return False
    if publish.state == "failed":
        return True
    return publish.state == "publishing" and publish.lease_until is not None and publish.lease_until < now


# ---------------------------------------------------------------------------
# Commands (K4.1)
# ---------------------------------------------------------------------------


def _unique(values: List[str]) -> List[str]:
    if len(set(values)) != len(values):
        raise ValueError("ids must be unique")
    return values


UniqueOrderIds = Annotated[
    List[EntityId],
    Field(min_length=1, max_length=MAX_COMMAND_ORDERS),
    AfterValidator(_unique),
]


class Target(_Strict):
    """Where an order goes. ``load_id`` None: best-fit load (K4.3); ``"new"``: a new load."""

    load_id: Optional[Union[Literal["new"], EntityId]] = None
    index: Optional[int] = Field(default=None, ge=0)


class _CommandBase(_Strict):
    client_command_id: ClientId
    expected_lane_versions: Dict[EntityId, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    input_modality: InputModality = "menu"

    @field_validator("expected_lane_versions")
    @classmethod
    def _bounded(cls, value: Dict[str, int]) -> Dict[str, int]:
        if len(value) > MAX_LANES:
            raise ValueError("too many lanes")
        return value


class AddLaneCommand(_CommandBase):
    type: Literal["add_lane"]
    truck_id: EntityId


class RemoveLaneCommand(_CommandBase):
    type: Literal["remove_lane"]
    truck_id: EntityId


class PairDriverCommand(_CommandBase):
    type: Literal["pair_driver"]
    truck_id: EntityId
    driver_id: Optional[EntityId] = None


class AssignOrdersCommand(_CommandBase):
    type: Literal["assign_orders"]
    order_ids: UniqueOrderIds
    truck_id: EntityId
    target: Target = Field(default_factory=Target)


class MoveStopsCommand(_CommandBase):
    type: Literal["move_stops"]
    order_ids: UniqueOrderIds
    truck_id: EntityId
    target: Target = Field(default_factory=Target)


class UnassignOrdersCommand(_CommandBase):
    type: Literal["unassign_orders"]
    order_ids: UniqueOrderIds


class MoveLoadCommand(_CommandBase):
    type: Literal["move_load"]
    load_id: EntityId
    truck_id: EntityId
    index: int = Field(ge=0)


class SetTerminalCommand(_CommandBase):
    type: Literal["set_terminal"]
    load_id: EntityId
    terminal_id: Optional[EntityId] = None


class SetAllocationCommand(_CommandBase):
    type: Literal["set_allocation"]
    load_id: EntityId
    order_id: EntityId
    shares: Optional[List[CompartmentShare]] = Field(default=None, max_length=MAX_SHARES)


class SetLoadShiftCommand(_CommandBase):
    type: Literal["set_load_shift"]
    load_id: EntityId
    shift_id: ShiftId


class AcknowledgeWarningCommand(_CommandBase):
    type: Literal["acknowledge_warning"]
    truck_id: EntityId
    warning_id: WarningId
    reason: Reason


class AcceptSuggestionCommand(_CommandBase):
    type: Literal["accept_suggestion"]
    suggestion_id: EntityId
    load_ids: Optional[Annotated[List[EntityId], Field(min_length=1, max_length=MAX_LANES)]] = None


class DiscardLaneChangesCommand(_CommandBase):
    type: Literal["discard_lane_changes"]
    truck_id: EntityId


class RevertCommand(_CommandBase):
    type: Literal["revert"]
    target_command_id: ClientId


class ReapplyCommand(_CommandBase):
    type: Literal["reapply"]
    target_command_id: ClientId


AnyCommand = Annotated[
    Union[
        AddLaneCommand,
        RemoveLaneCommand,
        PairDriverCommand,
        AssignOrdersCommand,
        MoveStopsCommand,
        UnassignOrdersCommand,
        MoveLoadCommand,
        SetTerminalCommand,
        SetAllocationCommand,
        SetLoadShiftCommand,
        AcknowledgeWarningCommand,
        AcceptSuggestionCommand,
        DiscardLaneChangesCommand,
        RevertCommand,
        ReapplyCommand,
    ],
    Field(discriminator="type"),
]

COMMAND_TYPES = (
    "add_lane", "remove_lane", "pair_driver", "assign_orders", "move_stops",
    "unassign_orders", "move_load", "set_terminal", "set_allocation",
    "set_load_shift", "acknowledge_warning", "accept_suggestion",
    "discard_lane_changes", "revert", "reapply",
)


class CommandBody(RootModel[AnyCommand]):
    """The body of ``POST /{service_date}/commands``."""


# ---------------------------------------------------------------------------
# Validate, publish and suggestion bodies (K3.5, K7.1, K9)
# ---------------------------------------------------------------------------


class DragItem(_Strict):
    kind: Literal["order", "stop", "load", "driver", "truck"]
    ids: Annotated[List[EntityId], Field(min_length=1, max_length=MAX_COMMAND_ORDERS), AfterValidator(_unique)]


class ValidateBody(_Strict):
    item: DragItem
    candidates: Annotated[List[EntityId], Field(min_length=1, max_length=MAX_CANDIDATES), AfterValidator(_unique)]
    position: Optional[Target] = None


class PublishLaneRef(_Strict):
    truck_id: EntityId
    expected_version: int = Field(ge=0)


class PublishBody(_Strict):
    """A real publish. ``dry_run`` must be absent or exactly ``false``."""

    client_request_id: ClientId
    lanes: Annotated[List[PublishLaneRef], Field(min_length=1, max_length=MAX_LANES)]
    warning_reasons: Dict[WarningId, Reason] = Field(default_factory=dict)
    dry_run: Literal[False] = False


class PublishPreviewBody(_Strict):
    """The review-dialog dry run (K7.1). ``client_request_id`` is accepted and ignored."""

    dry_run: Literal[True]
    lanes: Annotated[List[PublishLaneRef], Field(min_length=1, max_length=MAX_LANES)]
    warning_reasons: Dict[WarningId, Reason] = Field(default_factory=dict)
    client_request_id: Optional[ClientId] = None


def publish_body_model(body: Any) -> Type[BaseModel]:
    """Pick the publish body model before parsing (K7.1, review pass 3 finding 6).

    Only a JSON object whose ``dry_run`` is the boolean ``True`` is a dry run.
    Everything else, including ``"true"`` and non-object bodies, is a real
    publish and therefore needs ``client_request_id``.
    """
    is_dry_run = isinstance(body, dict) and body.get("dry_run") is True
    return PublishPreviewBody if is_dry_run else PublishBody


class RejectSuggestionBody(_Strict):
    reason: Optional[Reason] = None


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


class SnapshotQuery(_Strict):
    lanes: Optional[Annotated[List[EntityId], Field(min_length=1, max_length=MAX_LANES)]] = None
    call_type: Optional[CallType] = None
    product: Optional[Annotated[str, StringConstraints(pattern=PRODUCT_FILTER_PATTERN)]] = None
    window: Optional[WindowFilter] = None


class HistoryQuery(_Strict):
    truck_id: Optional[EntityId] = None
    cursor: Optional[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:+-]{1,256}$")]] = None
    size: int = Field(default=20, ge=1, le=50)


# ---------------------------------------------------------------------------
# Responses (K4.2 step 12, K5, K3.1)
# ---------------------------------------------------------------------------


class DriverSummary(_Strict):
    driver_id: str
    name: Optional[str] = None
    status: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    cdl_class: Optional[str] = None
    hazmat_endorsement: Optional[bool] = None
    eligible: Optional[bool] = None
    ineligible_reasons: List[str] = Field(default_factory=list)
    hos: Optional[Dict[str, Any]] = None
    paired_truck_id: Optional[str] = None


class SuggestedDriver(_Strict):
    driver_id: str
    name: str
    source: Literal["assigned_truck_id"] = "assigned_truck_id"


class CompartmentView(_Strict):
    compartment_id: str
    position_index: int = 0
    capacity_l: float
    accepts_products: List[str] = Field(default_factory=list)
    state: Optional[str] = None
    last_loaded_product: Optional[str] = None


class LaneView(_Strict):
    truck_id: str
    version: int
    driver_id: Optional[str] = None
    driver: Optional[DriverSummary] = None
    suggested_driver: Optional[SuggestedDriver] = None
    compartments: List[CompartmentView] = Field(default_factory=list)
    loads: List[Load] = Field(default_factory=list)
    shelf: List[str] = Field(default_factory=list)
    checks: List[Check] = Field(default_factory=list)
    checks_computed_at: Optional[datetime] = None
    checks_stale: bool = False
    outcome: Outcome = "pass"
    publish: LanePublish = Field(default_factory=LanePublish)
    state: Literal["draft", "published", "modified", "publishing", "failed", "recovering"] = "draft"
    modified: bool = False
    ever_published: bool = False


class CommandResponse(_Strict):
    draft_version: int
    lanes: List[LaneView]
    checks: Dict[str, List[Check]] = Field(default_factory=dict)
    already_applied: bool = False
    audit_degraded: bool = False


class CandidatePreview(_Strict):
    fill_by_compartment: Dict[str, float] = Field(default_factory=dict)
    insertion_index: Optional[int] = None
    load_id: Optional[str] = None
    eta_delta_minutes: Optional[float] = None


class CandidateResult(_Strict):
    outcome: Outcome
    worst_checks: List[Check] = Field(default_factory=list)
    reason: Optional[str] = None
    preview: CandidatePreview = Field(default_factory=CandidatePreview)


class TrayOrder(_Strict):
    order_id: str
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    product_code: Optional[str] = None
    gallons_requested: Optional[float] = None
    fill_to_full: bool = False
    delivery_window_start: Optional[datetime] = None
    delivery_window_end: Optional[datetime] = None
    call_type: Optional[str] = None
    status: Optional[str] = None
    priority_score: Optional[float] = None
    priority_bucket: Optional[str] = None
    draggable: bool = True
    block_reason: Optional[str] = None
    missing_window: bool = False
    dyed: bool = False


class TrayTruck(_Strict):
    truck_id: str
    compartment_count: int
    capacity_l: float


class Trays(_Strict):
    orders: List[TrayOrder] = Field(default_factory=list)
    orders_truncated: bool = False
    drivers: List[DriverSummary] = Field(default_factory=list)
    trucks: List[TrayTruck] = Field(default_factory=list)


class Snapshot(_Strict):
    service_date: date
    timezone: str
    mode: BoardMode
    draft_version: int
    read_only: bool
    read_only_reason: Optional[str] = None
    degraded_sources: List[str] = Field(default_factory=list)
    shifts: List[Dict[str, str]] = Field(default_factory=lambda: [dict(s) for s in SHIFTS])
    lanes: List[LaneView] = Field(default_factory=list)
    trays: Trays = Field(default_factory=Trays)
    suggestions: List[Dict[str, Any]] = Field(default_factory=list)
    acknowledged: Dict[str, WarningAck] = Field(default_factory=dict)


class HistoryItem(_Strict):
    command_id: str
    type: str
    result: str
    actor_user_id: Optional[str] = None
    actor_name: Optional[str] = None
    input_modality: Optional[str] = None
    created_at: Optional[datetime] = None
    lanes: List[Dict[str, Any]] = Field(default_factory=list)
    checks_summary: Dict[str, Any] = Field(default_factory=dict)
    overrides: List[Dict[str, Any]] = Field(default_factory=list)


class HistoryPage(_Strict):
    items: List[HistoryItem]
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# parse_body (External input validation)
# ---------------------------------------------------------------------------

_IDEMPOTENCY_FIELDS = frozenset({"client_command_id", "client_request_id"})

T = TypeVar("T", bound=BaseModel)


def parse_body(model: Type[T], body: Any) -> T:
    """Validate ``body`` against ``model``; 422 with field paths only on failure.

    A missing ``client_command_id`` / ``client_request_id`` is 422
    ``MISSING_IDEMPOTENCY_KEY``; anything else is 422 ``VALIDATION_ERROR``.
    The details carry field paths and Pydantic error types, never ``input``,
    ``ctx`` or ``msg`` (messages can quote values).

    A key's location may carry the command tag first (``assign_orders.client_command_id``)
    because the command body is a discriminated union, so the check is on the
    last path element at depth one or two.
    """
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        fields = [".".join(str(part) for part in e["loc"]) for e in errors]
        missing_key = any(
            e["type"] == "missing"
            and e["loc"]
            and len(e["loc"]) <= 2
            and str(e["loc"][-1]) in _IDEMPOTENCY_FIELDS
            for e in errors
        )
        raise AppException(
            ErrorCode.MISSING_IDEMPOTENCY_KEY if missing_key else ErrorCode.VALIDATION_ERROR,
            "The request is not valid.",
            status_code=422,
            details={"fields": fields, "reason": errors[0]["type"] if errors else "invalid"},
        ) from None


def invalid(reason: str, *, fields: Optional[List[str]] = None, status_code: int = 422) -> AppException:
    """A 422 ``VALIDATION_ERROR`` with a stable ``reason`` and no submitted values."""
    details: Dict[str, Any] = {"reason": reason}
    if fields:
        details["fields"] = fields
    return AppException(ErrorCode.VALIDATION_ERROR, "The request is not valid.", status_code=status_code, details=details)


def parse_lanes_param(raw: Optional[str]) -> Optional[List[str]]:
    """``?lanes=T1,T2``: at most 60 ids with the id charset, else 422."""
    if raw is None:
        return None
    parts = [p for p in raw.split(",") if p != ""]
    if not parts or len(parts) > MAX_LANES or any(not _ID_RE.match(p) for p in parts):
        raise invalid("invalid_lanes", fields=["lanes"])
    return list(dict.fromkeys(parts))


def parse_filter_params(call_type: Optional[str], product: Optional[str], window: Optional[str]) -> SnapshotQuery:
    """The three tray filter params (K5); any bad value is 422 ``invalid_filter``."""
    if call_type is not None and call_type not in ("keep_full", "auto_fill", "will_call", "one_off"):
        raise invalid("invalid_filter", fields=["call_type"])
    if product is not None and not _PRODUCT_RE.match(product):
        raise invalid("invalid_filter", fields=["product"])
    if window is not None and window not in ("overdue", "today", "later"):
        raise invalid("invalid_filter", fields=["window"])
    return SnapshotQuery(call_type=call_type, product=product, window=window)
