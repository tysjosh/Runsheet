"""
Compartment Loading Agent — overlay agent for feasible multi-compartment loading plans.

Subscribes to DeliveryPriorityList messages from the SignalBus, queries
fuel trucks and compartments from the truck_compartments ES index, runs
feasibility checks and optimization using the compartment_solver, produces
InterventionProposals with loading plan actions, and persists plans to
mvp_load_plans.

Task 6.5 wires cross-contamination enforcement into the agent: every
proposed compartment assignment passes through
:func:`fuel.services.compatibility_matrix.check_compatibility` before it
is committed to the Loading_Plan. Rejections persist a
:class:`CrossContaminationViolation` to ``cross_contamination_events``
and publish a ``cross_contamination_violation`` RiskSignal on the
SignalBus so downstream overlays (dispatch, exception replanning) can
react without parsing the loading plan. Assignments rejected by the
engine are stripped from the Loading_Plan before persistence so
``mvp_load_plans`` never carries a contaminating assignment; the unmet
volume is charged to ``unserved_demand_liters`` so downstream metrics
reflect the blocked delivery.

Task 6.6 layers the compartment-state write on top: after a successful
Loading_Plan commit, the agent calls
:meth:`fuel.compartment_state_models.CompartmentStateRepository.mark_loaded`
once per assignment to atomically update ``last_loaded_product``,
``last_loaded_at``, and ``state`` on the ``truck_compartments`` doc.
The repository uses ``if_seq_no`` / ``if_primary_term`` OCC so
concurrent plan commits never overwrite each other. The state write is
gated on overlay mode — shadow-mode evaluation still produces the plan
for retrospective analysis but leaves the live compartment state
untouched, matching the spec guarantee that the mutation fires only on
a successful commit.

Default configuration:
    - decision_cycle: 60 seconds
    - cooldown: 30 minutes per truck

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10,
              7.1.2, 7.2.2, 7.2.3, 7.2.6
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from Agents.overlay.base_overlay_agent import (
    DEGRADATION_KIND_NO_INPUT,
    DEGRADATION_KIND_PRODUCED_NOTHING,
    OverlayAgentBase,
    build_degradation_reason,
)
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.signal_bus import SignalBus
from Agents.support.compartment_models import (
    Compartment,
    CompartmentAssignment,
    DeliveryRequest,
    FeasibilityResult,
    FleetAllocation,
    LoadingPlan,
    TruckSpec,
)
from Agents.support.compartment_solver import (
    allocate_across_trucks,
    check_feasibility,
    fuel_density_kg_per_liter,
    legacy_grade_for_product,
    output_fuel_grade,
    planned_liters,
    replace_stripped,
    request_key,
    segregation_key,
)
from Agents.support.fuel_distribution_models import (
    DeliveryPriority,
    DeliveryPriorityList,
    FuelGrade,
    PriorityBucket,
)
from Agents.support.mvp_es_mappings import (
    MVP_LOAD_PLANS_INDEX,
    TRUCK_COMPARTMENTS_INDEX,
)
from inventory.es_mappings import INVENTORY_INDEX
from fuel.compartment_state_models import (
    CROSS_CONTAMINATION_VIOLATION_ENTITY_TYPE,
    CompartmentNotFoundError,
    CompartmentState,
    CompartmentStateConflictError,
    CompartmentStateRepository,
    CrossContaminationViolation,
    CrossTenantCompartmentAccessError,
)
from fuel.services.compatibility_matrix import (
    DECISION_ALLOWED,
    REASON_CLEANING_REQUIRED,
    REASON_CROSS_CONTAMINATION_BLOCKED,
    RuleType,
    check_compatibility,
    load_tenant_compatibility_rules,
)
from compliance.services.dyed_diesel_enforcer import DyedDieselCheckUnavailable
from fuel.services.contract_lift_service import ContractLiftService
from fuel.customer_tank_models import CustomerTank, CustomerTankRepository
from fuel.services.fuel_ops_es_mappings import (
    CROSS_CONTAMINATION_EVENTS_INDEX,
)
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    canonicalize_or_warn,
    is_known_product,
)
from fuel.order_models import LOADABLE_ORDER_STATUSES
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX
from services.unit_conversion import GAL_TO_L

logger = logging.getLogger(__name__)

#: US gallons → litres. Bound to :data:`services.unit_conversion.GAL_TO_L` so
#: this module holds no independent definition of the factor. The agent needs
#: the conversion because orders are gallons-denominated while the compartment
#: solver and its kg/L densities are litres-denominated.
GALLONS_TO_LITERS: Final[float] = GAL_TO_L

#: K11 / R8.1: an order whose ``assigned_run_id`` is neither absent, null nor
#: ``""`` is committed to an applied loading plan and is not loaded again.
#: R8.6 rule: loadable = loadable statuses minus committed; routable = loadable
#: statuses (RoutePlanningAgent keeps no committed exclusion, R8.5); committed
#: draw = committed loadable plus dispatched/in_transit. The status set is the
#: shared :data:`fuel.order_models.LOADABLE_ORDER_STATUSES` (N2); the committed
#: clause stays separate.
COMMITTED_ORDER_FIELD: Final[str] = "assigned_run_id"
_LOADABLE_ORDER_STATUSES: Final[tuple] = LOADABLE_ORDER_STATUSES
_IN_FLIGHT_ORDER_STATUSES: Final[tuple] = ("dispatched", "in_transit")


def _unlinked_clause(field: str) -> Dict[str, Any]:
    """K11 document-store clause: ``field`` absent, null or ``""``."""
    return {"bool": {
        "should": [
            {"bool": {"must_not": [{"exists": {"field": field}}]}},
            {"term": {field: ""}},
        ],
        "minimum_should_match": 1,
    }}

# Default minimum delivery quantity in liters (Req 3.5)
DEFAULT_MIN_DROP_LITERS = 500.0

# Default uncertainty buffer percentage (Req 3.6)
DEFAULT_UNCERTAINTY_BUFFER_PCT = 10.0


def _assignment_key(a: CompartmentAssignment) -> str:
    """Run identity of an assignment; matches :func:`request_key` of its request."""
    return a.order_id or "{}:{}".format(
        a.station_id,
        segregation_key(product_code=a.product_code, fuel_grade=a.fuel_grade),
    )


def _unplaced_entry(
    *,
    order_id: Optional[str],
    station_id: str,
    product_code: Optional[str],
    liters: float,
    reason: str,
    partial: bool = False,
) -> Dict[str, Any]:
    """One ``last_unplaced_orders`` entry (OI-39). Ids and litres only."""
    return {
        "order_id": order_id,
        "station_id": station_id,
        "product_code": product_code,
        "liters": round(float(liters), 2),
        "reason": reason,
        "partial": partial,
    }


@dataclass(frozen=True)
class _OrderCandidate:
    """A loadable order before its tank's shared ullage is applied."""

    score: Optional[float]
    order_id: str
    #: Set only when the tank's ullage is known; ``None`` means uncapped.
    tank_id: Optional[str]
    ullage_liters: Optional[float]
    requested_liters: float
    station_id: str
    fuel_grade: FuelGrade
    product_code: str


class CompartmentLoadingAgent(OverlayAgentBase):
    """Produces feasible multi-compartment loading plans for fuel trucks.

    Consumes DeliveryPriorityList messages, queries available trucks and
    their compartments, runs feasibility checks and greedy optimization,
    and produces InterventionProposals containing loading plan actions.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service for querying indices.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager for broadcasting events.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval in seconds (default 60).
        cooldown_minutes: Per-truck cooldown in minutes (default 30).
    """

    def __init__(
        self,
        signal_bus: SignalBus,
        es_service,
        activity_log_service,
        ws_manager,
        confirmation_protocol,
        autonomy_config_service,
        feature_flag_service,
        poll_interval: int = 60,
        cooldown_minutes: int = 30,
        compartment_state_repo: Optional[CompartmentStateRepository] = None,
        tenant_config: Optional[Any] = None,
        contract_lift_service: Optional[ContractLiftService] = None,
        customer_tank_repo: Optional[CustomerTankRepository] = None,
    ):
        super().__init__(
            agent_id="compartment_loading",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": DeliveryPriorityList,
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=cooldown_minutes,
        )
        # Buffer priority lists between cycles
        self._priority_buffer: List[DeliveryPriorityList] = []
        # Repository for atomic truck_compartments state updates (Req 7.1.2).
        # Lazily constructed from the shared ES service so existing call
        # sites (bootstrap, tests) do not need to thread a new dependency.
        self._compartment_state_repo = (
            compartment_state_repo
            if compartment_state_repo is not None
            else CompartmentStateRepository(es_service)
        )
        # Optional Redis-like handle used to read the tenant
        # ``compatibility_matrix_config:{tenant_id}`` override (Task 6.4).
        # When unset the agent falls back to DEFAULT_COMPATIBILITY_RULES so
        # a Redis outage never blocks the cross-contamination guard.
        self._tenant_config: Optional[Any] = tenant_config
        # Monthly rolling-lift counter (Task 7.6 / Req 8.3.4). Optional:
        # when no service is injected we default to a no-op wrapper so
        # legacy tests and bootstrap paths keep working unchanged. The
        # default :class:`ContractLiftService` with ``redis_client=None``
        # treats every write as a no-op and every read as zero, so
        # constructing one unconditionally is safe.
        self._contract_lift_service: ContractLiftService = (
            contract_lift_service
            if contract_lift_service is not None
            else ContractLiftService(redis_client=None)
        )
        # Tenant-scoped customer tank repository for resolving
        # fill_to_full orders (Task 11.3 / Req 5.3.2). When not
        # injected, lazily constructed from the shared ES service.
        self._customer_tank_repo: CustomerTankRepository = (
            customer_tank_repo
            if customer_tank_repo is not None
            else CustomerTankRepository(es_service)
        )
        # Per-evaluate state, reset at the start of every evaluate (R3.10, K11).
        self._order_snapshots: Dict[str, Dict[str, Any]] = {}
        self._all_orders_committed = False
        self._committed_order_count = 0
        #: Orders this evaluate() could not load, one entry per order (and
        #: reason): ``{order_id, station_id, product_code, liters, reason,
        #: partial}``. Read by FuelDistributionPipeline (OI-39).
        self.last_unplaced_orders: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Post-construction wiring helpers
    # ------------------------------------------------------------------

    def set_tenant_config(self, tenant_config: Optional[Any]) -> None:
        """Inject or replace the tenant-config backend post-construction.

        Bootstrap plumbs the Redis handle into the agent here rather than
        threading it through the constructor kwargs so tests can continue
        to use the existing ``overlay_common_args`` call shape.
        """

        self._tenant_config = tenant_config

    def set_dyed_diesel_enforcer(self, enforcer: Optional[Any]) -> None:
        """Inject the :class:`DyedDieselEnforcer` post-construction.

        Validates: Requirements 6.3, 6.4.

        Bootstrap injects the DyedDieselEnforcer into the agent so that
        every proposed compartment assignment involving dyed diesel is
        validated against the compartment's dyed-compatible flag before
        the plan is committed to ``mvp_load_plans``. When the enforcer
        is ``None`` a plan carrying dyed diesel is blocked (fail closed,
        OI-02); plans with no dyed diesel are unaffected.
        """
        self._dyed_diesel_enforcer = enforcer

    def set_contract_lift_service(
        self, contract_lift_service: Optional[ContractLiftService]
    ) -> None:
        """Inject or replace the monthly rolling-lift counter service.

        Validates: Requirement 8.3.4.

        Bootstrap injects a :class:`ContractLiftService` backed by the
        shared Redis client after the agent is constructed so every
        Loading_Plan commit with a ``contract_id`` bumps
        ``contract_lift:{tenant_id}:{contract_id}:{YYYY-MM}``. When
        ``contract_lift_service`` is ``None`` the agent falls back to
        the no-op default wired in ``__init__`` so legacy plans that
        don't carry a ``contract_id`` keep working unchanged.
        """

        self._contract_lift_service = (
            contract_lift_service
            if contract_lift_service is not None
            else ContractLiftService(redis_client=None)
        )

    # ------------------------------------------------------------------
    # Mode helpers (Task 6.6 / Req 7.1.2)
    # ------------------------------------------------------------------

    # ``_is_active_commit_mode`` now lives on :class:`OverlayAgentBase` so the
    # compartment-state write (Req 7.1.2) and the replan apply share one
    # definition of "this is a real commit". The behaviour is unchanged: the
    # spec reserves the ``last_loaded_*`` / ``state`` write on
    # ``truck_compartments`` for a successful assignment commit, never for
    # shadow-mode evaluation, and resolution fails closed.

    # ------------------------------------------------------------------
    # Signal handling override — buffer DeliveryPriorityList messages
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Buffer incoming signals. DeliveryPriorityLists are stored separately."""
        if isinstance(signal, DeliveryPriorityList):
            self._priority_buffer.append(signal)
        else:
            await super()._on_signal(signal)

    def _pending_work_tenants(self) -> List[str]:
        """Tenants with a buffered priority list awaiting a loading plan.

        Required because :meth:`_on_signal` files every
        :class:`DeliveryPriorityList` in ``_priority_buffer`` and nothing in
        ``_signal_buffer``. Without this, ``monitor_cycle`` saw an empty
        ``_signal_buffer`` and returned before ``evaluate()``, so on the
        SignalBus path this agent buffered priority lists forever and never
        produced a plan — silently, with no error and no log line.

        Validates: Requirement 3.1
        """
        tenants: List[str] = []
        for priority_list in self._priority_buffer:
            tenant_id = getattr(priority_list, "tenant_id", None)
            if tenant_id and tenant_id not in tenants:
                tenants.append(tenant_id)
        return tenants

    # ------------------------------------------------------------------
    # Core evaluation (Req 3.1–3.10)
    # ------------------------------------------------------------------

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Consume priority lists, build loading plans, produce proposals.

        Steps:
        1. Collect buffered DeliveryPriorityList messages.
        2. Build delivery requests from fuel orders, in priority order and
           capped at tank ullage (Req 3.1).
        3. Query available fuel trucks and their compartments once (Req 3.1).
        4. Allocate the requests across the fleet, each to at most one truck
           (Req 3.4); report requests no truck could take.
        5. Per plan: feasibility of that truck's share (Req 3.3),
           cross-contamination and dyed-diesel enforcement, persist to
           mvp_load_plans (Req 3.9).
        6. Produce one InterventionProposal per persisted plan.

        Returns:
            List of InterventionProposals with loading plan actions.
        """
        self._order_snapshots = {}
        self._all_orders_committed = False
        self._committed_order_count = 0
        self.last_unplaced_orders = []

        # Step 1: Collect buffered priority lists
        priority_lists = list(self._priority_buffer)
        self._priority_buffer.clear()

        if not priority_lists:
            # No priority list was buffered, so there is nothing to load and the
            # routing stage downstream will receive no plan. Silence here is
            # what let a plan run with zero load plans report COMPLETE.
            self.report_degradation(
                build_degradation_reason(
                    reason_code="no_priority_lists_buffered",
                    kind=DEGRADATION_KIND_NO_INPUT,
                    detail=(
                        "the prioritization stage published no DeliveryPriority"
                        "List, so no load plan could be built"
                    ),
                )
            )
            return []

        # Use the most recent priority list
        priority_list = priority_lists[-1]
        tenant_id = priority_list.tenant_id

        # The [-1] above discards everything else the buffer held. That is the
        # long-standing contract (a newer list supersedes an older one), but
        # when the discarded lists belong to *other* tenants it means their
        # work is dropped without a trace. Say so.
        dropped_tenants = sorted(
            {
                getattr(other, "tenant_id", None)
                for other in priority_lists[:-1]
            }
            - {tenant_id, None}
        )
        if dropped_tenants:
            logger.warning(
                "CompartmentLoadingAgent: discarding buffered priority lists "
                "for tenant(s) %s — this cycle acts only on the most recent "
                "list (tenant %s). %d list(s) buffered in total.",
                ", ".join(dropped_tenants),
                tenant_id,
                len(priority_lists),
            )
        # Use pipeline run_id if available, otherwise fall back to priority list's run_id
        run_id = getattr(self, '_current_run_id', None) or priority_list.run_id

        # Step 2: Build delivery requests from fuel orders (Req 5.3.1, 5.3.2).
        # Read product_code and gallons_requested directly from each
        # Fuel_Order in fuel_orders_current rather than relying on the
        # DeliveryPriorityList's FuelGrade enum. For fill_to_full orders,
        # resolve the linked customer_tank to compute target_volume.
        delivery_requests = await self._build_delivery_requests_from_orders(
            tenant_id, priority_list
        )
        if not delivery_requests and self._all_orders_committed:
            # K11: the steady state after every plan is applied. no_input keeps
            # it non-error in the pipeline log, and the distinct code tells a
            # dispatcher "nothing left to load" from a failure.
            logger.info(
                "CompartmentLoadingAgent: every loadable order for tenant %s "
                "is committed to an applied loading plan (%d order(s))",
                tenant_id,
                self._committed_order_count,
            )
            self.report_degradation(
                build_degradation_reason(
                    reason_code="all_loadable_orders_committed",
                    kind=DEGRADATION_KIND_NO_INPUT,
                    detail=(
                        "every loadable order is already committed to an "
                        "applied loading plan"
                    ),
                    committed_orders=self._committed_order_count,
                )
            )
            return []
        if not delivery_requests:
            # A priority list arrived but none of its entries resolved to a
            # loadable request. Unlike the empty-buffer case above there *was*
            # input, so this is a produced-nothing outcome: the orders exist and
            # the stage turned none of them into demand.
            logger.warning(
                "CompartmentLoadingAgent: priority list for tenant %s yielded "
                "no delivery requests from %d prioritized order(s)",
                tenant_id,
                len(getattr(priority_list, "priorities", []) or []),
            )
            self.report_degradation(
                build_degradation_reason(
                    reason_code="no_delivery_requests",
                    kind=DEGRADATION_KIND_PRODUCED_NOTHING,
                    detail=(
                        "no prioritized order resolved to a loadable delivery "
                        "request (missing product_code, gallons, or tank link)"
                    ),
                    priorities=len(getattr(priority_list, "priorities", []) or []),
                    delivery_requests=0,
                )
            )
            return []

        # Step 3: Query available trucks with equipment check (Req 3.1, 3.2, 3.3)
        trucks = await self._query_trucks_with_equipment_check(tenant_id)
        if not trucks:
            # Demand exists and there is no truck to put it on. Reported as
            # no_input rather than produced_nothing: the stage was not handed
            # the fleet it needs, which is a provisioning gap, not a planning
            # defect. Either way the run has no load plan and must not claim
            # COMPLETE.
            logger.warning(
                "CompartmentLoadingAgent: no trucks found for tenant %s — "
                "%d delivery request(s) cannot be loaded",
                tenant_id,
                len(delivery_requests),
            )
            self.report_degradation(
                build_degradation_reason(
                    reason_code="no_trucks_available",
                    kind=DEGRADATION_KIND_NO_INPUT,
                    detail=(
                        "no truck passed the equipment check, so the "
                        "outstanding delivery requests could not be loaded"
                    ),
                    delivery_requests=len(delivery_requests),
                    trucks=0,
                )
            )
            return []

        # Step 4: For each truck, check feasibility and optimize
        proposals: List[InterventionProposal] = []
        # Task 6.5: load the tenant compatibility rule table once per
        # cycle so each assignment is evaluated against the effective
        # matrix (defaults merged with Redis overrides). A Redis outage
        # degrades gracefully to the seed table.
        compatibility_rules = await load_tenant_compatibility_rules(
            tenant_id, self._tenant_config
        )
        # Task 6.6 / Req 7.1.2: the compartment-state write must only
        # fire on a real commit path. Shadow-mode evaluation runs the
        # full optimization so the plan can be logged for retrospective
        # analysis, but the ``last_loaded_*`` / ``state`` fields on
        # ``truck_compartments`` stay untouched until the overlay is
        # flipped to an active mode. Resolve the current mode once per
        # cycle so every truck processed in the same evaluation shares
        # the same commit gate.
        commit_compartment_state = await self._is_active_commit_mode(tenant_id)

        # Allocate the whole run across the fleet at once (F10). The old loop
        # ran the solver per truck with the full request list, so every truck
        # was planned to carry every order and each copy was queued as its own
        # approval. ``delivery_requests`` is already in priority order.
        truck_specs = [
            TruckSpec(
                truck_id=truck_id,
                compartments=truck_data["compartments"],
                max_weight_kg=truck_data.get("max_weight_kg"),
                tare_weight_kg=truck_data.get("tare_weight_kg") or 0.0,
            )
            for truck_id, truck_data in trucks.items()
        ]
        allocation = allocate_across_trucks(
            truck_specs,
            delivery_requests,
            uncertainty_buffer_pct=DEFAULT_UNCERTAINTY_BUFFER_PCT,
        )
        await self._report_unassigned_orders(allocation, tenant_id, run_id)
        for unserved in allocation.unassigned:
            self.last_unplaced_orders.append(_unplaced_entry(
                order_id=unserved.order_id,
                station_id=unserved.station_id,
                product_code=unserved.product_code,
                liters=unserved.planned_liters,
                reason=unserved.reason,
            ))

        # Index of each request by its run identity, first occurrence wins
        # (priority order).
        requests_by_key: Dict[str, Tuple[int, DeliveryRequest]] = {}
        for index, req in enumerate(delivery_requests):
            requests_by_key.setdefault(request_key(req), (index, req))

        # Phase A — cross-contamination (Task 6.5 / Req 7.2.2, 7.2.3, 7.2.6).
        # Before any assignment is committed, verify each proposed
        # compartment/product pairing against the tenant compatibility
        # matrix. Rejected assignments are stripped from the plan, persisted
        # as a CrossContaminationViolation, and republished on the SignalBus
        # so downstream overlays can react.
        plans: Dict[str, LoadingPlan] = {}
        stripped_liters: Dict[str, float] = {}
        stripped_sample: Dict[str, CompartmentAssignment] = {}
        source_truck: Dict[str, str] = {}
        for truck_id, loading_plan in allocation.plans.items():
            loading_plan.run_id = run_id
            loading_plan, stripped = await self._enforce_cross_contamination(
                loading_plan=loading_plan,
                truck_id=truck_id,
                tenant_id=tenant_id,
                compartment_states=trucks[truck_id].get("compartment_states", {}),
                compatibility_rules=compatibility_rules,
                run_id=run_id,
            )
            plans[truck_id] = loading_plan
            for a in stripped:
                key = _assignment_key(a)
                stripped_liters[key] = stripped_liters.get(key, 0.0) + float(
                    a.quantity_liters
                )
                stripped_sample.setdefault(key, a)
                source_truck.setdefault(key, truck_id)

        # Phase B — re-place stripped litres in the same run (OI-39).
        new_reason_entries: List[Dict[str, Any]] = []
        if stripped_liters:
            new_reason_entries.extend(
                await self._replace_stripped_assignments(
                    plans=plans,
                    trucks=trucks,
                    truck_specs=truck_specs,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    requests_by_key=requests_by_key,
                    stripped_liters=stripped_liters,
                    stripped_sample=stripped_sample,
                    source_truck=source_truck,
                    compatibility_rules=compatibility_rules,
                )
            )

        # Phase C — per truck: feasibility, dyed diesel, persist, propose.
        buffer_mult = 1.0 + DEFAULT_UNCERTAINTY_BUFFER_PCT / 100.0
        blocked_trucks: List[str] = []
        blocked_keys: set = set()
        for truck_id, loading_plan in plans.items():
            if not loading_plan.assignments:
                # Every assignment was blocked — skip the plan to avoid
                # writing an empty Loading_Plan to mvp_load_plans.
                continue
            truck_data = trucks[truck_id]
            compartments = truck_data["compartments"]

            # Feasibility of this truck's final share (Req 3.3, 3.7), at the
            # litres the allocator planned, so the buffer is not applied twice.
            # It only sets proposal confidence.
            plan_keys = {_assignment_key(a) for a in loading_plan.assignments}
            subset = [
                req.model_copy(update={
                    "quantity_liters": planned_liters(
                        req, buffer_mult, buffer_orders=False
                    ),
                })
                for req in delivery_requests
                if request_key(req) in plan_keys
            ]
            feasibility = check_feasibility(
                compartments=compartments,
                requests=subset,
                max_weight_kg=truck_data.get("max_weight_kg"),
                tare_weight_kg=truck_data.get("tare_weight_kg") or 0.0,
                uncertainty_buffer_pct=0.0,
            )

            loading_plan.run_id = run_id

            # Task 9.8 / Req 6.3, 6.4: before persisting the plan,
            # validate that any dyed-diesel assignments target
            # dyed-compatible compartments. Rejected assignments are
            # stripped from the plan and their volume is charged to
            # unserved_demand_liters. OI-02: if the check cannot run, the
            # whole plan is withheld (fail closed).
            try:
                loading_plan, dyed_stripped = (
                    await self._enforce_dyed_diesel_compliance(
                        loading_plan=loading_plan,
                        tenant_id=tenant_id,
                    )
                )
            except DyedDieselCheckUnavailable as exc:
                logger.error(
                    "CompartmentLoadingAgent: loading plan %s for truck %s "
                    "blocked; dyed-diesel check unavailable (%s, tenant=%s, "
                    "run_id=%s)",
                    loading_plan.plan_id,
                    truck_id,
                    exc.reason,
                    tenant_id,
                    run_id,
                )
                blocked_trucks.append(truck_id)
                for a in loading_plan.assignments:
                    key = _assignment_key(a)
                    if key in blocked_keys:
                        continue
                    blocked_keys.add(key)
                    new_reason_entries.append(_unplaced_entry(
                        order_id=a.order_id,
                        station_id=a.station_id,
                        product_code=a.product_code,
                        liters=sum(
                            x.quantity_liters
                            for x in loading_plan.assignments
                            if _assignment_key(x) == key
                        ),
                        reason="dyed_diesel_check_unavailable",
                    ))
                continue

            for a in dyed_stripped:
                new_reason_entries.append(_unplaced_entry(
                    order_id=a.order_id,
                    station_id=a.station_id,
                    product_code=a.product_code,
                    liters=a.quantity_liters,
                    reason="dyed_diesel_compartment_incompatible",
                ))

            if not loading_plan.assignments:
                # Every assignment was blocked by dyed-diesel rules —
                # skip the plan.
                continue

            # Step 5: Persist loading plan to ES (Req 3.9)
            await self._persist_loading_plan(
                loading_plan, commit_compartment_state=commit_compartment_state
            )

            # Step 6: Build InterventionProposal
            proposal = self._build_proposal(
                loading_plan=loading_plan,
                feasibility=feasibility,
                tenant_id=tenant_id,
            )
            proposals.append(proposal)

        # Per-order report (OI-39): every order this run could not load, with
        # the reason. no_truck_capacity was signalled by
        # _report_unassigned_orders above; the reasons added here are signalled
        # now through the same RiskSignal.
        self.last_unplaced_orders.extend(new_reason_entries)
        for entry in new_reason_entries:
            if not entry["order_id"]:
                continue
            await self._fail_order_loading(
                order_id=entry["order_id"],
                tenant_id=tenant_id,
                reason=entry["reason"],
                details={
                    "liters": entry["liters"],
                    "product_code": entry["product_code"],
                    "run_id": run_id,
                    "partial": entry["partial"],
                },
            )

        if blocked_trucks:
            self.report_degradation(
                build_degradation_reason(
                    reason_code="dyed_diesel_check_unavailable",
                    kind=DEGRADATION_KIND_PRODUCED_NOTHING,
                    detail=(
                        "Dyed-diesel compliance check unavailable: "
                        f"{len(blocked_trucks)} loading plan(s) blocked "
                        f"(trucks {', '.join(blocked_trucks)}). Retry when "
                        "the compliance service is back."
                    ),
                    blocked_trucks=list(blocked_trucks),
                    blocked_orders=sorted(
                        e["order_id"]
                        for e in new_reason_entries
                        if e["reason"] == "dyed_diesel_check_unavailable"
                        and e["order_id"]
                    ),
                )
            )

        logger.info(
            "CompartmentLoadingAgent: produced %d loading plans for tenant %s "
            "(run_id=%s)",
            len(proposals),
            tenant_id,
            run_id,
        )

        # Trucks and demand were both present and the loop produced no plan —
        # no truck had room for any request, or every allocated assignment was
        # stripped by the cross-contamination or dyed-diesel rules. This is the loading equivalent of the route
        # stage skipping every truck it was handed, and it is the one case here
        # that is unambiguously produced_nothing.
        if not proposals:
            logger.error(
                "CompartmentLoadingAgent: built 0 loading plans for tenant %s "
                "from %d truck(s) and %d delivery request(s) (run_id=%s)",
                tenant_id,
                len(trucks),
                len(delivery_requests),
                run_id,
            )
            self.report_degradation(
                build_degradation_reason(
                    reason_code="no_feasible_loading_plan",
                    kind=DEGRADATION_KIND_PRODUCED_NOTHING,
                    detail=(
                        "every truck was rejected as infeasible or had all of "
                        "its assignments blocked by compatibility rules"
                    ),
                    trucks=len(trucks),
                    delivery_requests=len(delivery_requests),
                    loading_plans=0,
                )
            )

        return proposals

    async def _report_unassigned_orders(
        self, allocation: FleetAllocation, tenant_id: str, run_id: str
    ) -> None:
        """Report every request no truck had room for in this run.

        Order-backed requests get the same ``_fail_order_loading`` RiskSignal
        an unresolvable order gets, with ``reason="no_truck_capacity"``, so a
        dispatcher sees which orders this run left behind. Legacy station
        demand has no order to fail and is only counted.
        """
        if not allocation.unassigned:
            return
        logger.warning(
            "CompartmentLoadingAgent: %d request(s) for tenant %s fit on no "
            "truck this run (run_id=%s): %s",
            len(allocation.unassigned),
            tenant_id,
            run_id,
            ", ".join(u.order_key for u in allocation.unassigned),
        )
        for unserved in allocation.unassigned:
            if not unserved.order_id:
                continue
            await self._fail_order_loading(
                order_id=unserved.order_id,
                tenant_id=tenant_id,
                reason="no_truck_capacity",
                details={
                    "planned_liters": unserved.planned_liters,
                    "product_code": unserved.product_code,
                    "run_id": run_id,
                },
            )

    async def _replace_stripped_assignments(
        self,
        *,
        plans: Dict[str, LoadingPlan],
        trucks: Dict[str, Dict[str, Any]],
        truck_specs: List[TruckSpec],
        tenant_id: str,
        run_id: str,
        requests_by_key: Mapping[str, Tuple[int, DeliveryRequest]],
        stripped_liters: Mapping[str, float],
        stripped_sample: Mapping[str, CompartmentAssignment],
        source_truck: Mapping[str, str],
        compatibility_rules: Mapping[Tuple[str, str], "RuleType"],
    ) -> List[Dict[str, Any]]:
        """Offer cross-contamination strips to other compartments (OI-39).

        Mutates ``plans`` in place: re-placed assignments are merged into
        their destination truck's plan (a new plan when that truck had none),
        and the re-placed litres are taken back off the source plan's
        ``unserved_demand_liters``. Re-placed assignments already passed the
        compatibility check, so they are not re-checked (no duplicate
        violation records); they still go through the dyed-diesel check.

        Returns the per-order ``no_compatible_compartment`` entries for
        litres that fit nowhere.
        """
        entries: List[Dict[str, Any]] = []
        stripped: List[Tuple[int, DeliveryRequest, float]] = []
        for key, liters in stripped_liters.items():
            found = requests_by_key.get(key)
            if found is None:
                # No request to re-place from (should not happen: every
                # assignment comes from a request). Report it, don't drop it.
                sample = stripped_sample[key]
                entries.append(_unplaced_entry(
                    order_id=sample.order_id,
                    station_id=sample.station_id,
                    product_code=sample.product_code,
                    liters=liters,
                    reason="no_compatible_compartment",
                ))
                continue
            index, req = found
            stripped.append((index, req, liters))
        stripped.sort(key=lambda item: item[0])

        probe_requests: Dict[str, DeliveryRequest] = {}
        for _, req, _ in stripped:
            probe_requests.setdefault(
                segregation_key(
                    product_code=req.product_code, fuel_grade=req.fuel_grade.value
                ),
                req,
            )

        def is_allowed(truck_id: str, comp: Compartment, key: str) -> bool:
            req = probe_requests.get(key)
            if req is None:
                return False
            probe = CompartmentAssignment(
                compartment_id=comp.compartment_id,
                station_id=req.station_id,
                order_id=req.order_id,
                fuel_grade=output_fuel_grade(req),
                product_code=req.product_code,
                quantity_liters=1.0,
                compartment_capacity_liters=comp.capacity_liters,
            )
            decision = self._evaluate_assignment_compatibility(
                assignment=probe,
                compartment_states=trucks[truck_id].get("compartment_states", {}),
                compatibility_rules=compatibility_rules,
            )
            return decision is not None and decision["decision"] == DECISION_ALLOWED

        replacement = replace_stripped(
            truck_specs,
            {truck_id: list(plan.assignments) for truck_id, plan in plans.items()},
            [(req, liters) for _, req, liters in stripped],
            is_allowed,
        )

        # Take re-placed litres back off each source plan's unserved total.
        credit: Dict[str, float] = {}
        replaced_total = 0.0
        for new_assignments in replacement.placed.values():
            for a in new_assignments:
                src = source_truck.get(_assignment_key(a))
                if src is not None:
                    credit[src] = credit.get(src, 0.0) + float(a.quantity_liters)
                replaced_total += float(a.quantity_liters)

        for truck_id in list(plans) + [
            t for t in replacement.placed if t not in plans
        ]:
            new_assignments = replacement.placed.get(truck_id, [])
            if not new_assignments and truck_id not in credit:
                continue
            base = plans.get(truck_id)
            if base is None:
                base = LoadingPlan(
                    truck_id=truck_id,
                    assignments=[],
                    total_utilization_pct=0.0,
                    unserved_demand_liters=0.0,
                    total_weight_kg=0.0,
                    tenant_id=trucks[truck_id]["compartments"][0].tenant_id,
                    run_id=run_id,
                )
            if new_assignments:
                plans[truck_id] = self._rebuild_plan(
                    base,
                    list(base.assignments) + list(new_assignments),
                    unserved_delta=-credit.get(truck_id, 0.0),
                    capacity_liters=sum(
                        c.capacity_liters for c in trucks[truck_id]["compartments"]
                    ),
                )
            else:
                plans[truck_id] = base.model_copy(update={
                    "unserved_demand_liters": max(
                        0.0,
                        round(
                            float(base.unserved_demand_liters)
                            - credit.get(truck_id, 0.0),
                            2,
                        ),
                    ),
                })

        for unserved in replacement.unplaced:
            entries.append(_unplaced_entry(
                order_id=unserved.order_id,
                station_id=unserved.station_id,
                product_code=unserved.product_code,
                liters=unserved.planned_liters,
                reason=unserved.reason,
            ))
        for key, shortfall in replacement.partial.items():
            _, req = requests_by_key[key]
            entries.append(_unplaced_entry(
                order_id=req.order_id,
                station_id=req.station_id,
                product_code=req.product_code,
                liters=shortfall,
                reason="no_compatible_compartment",
                partial=True,
            ))

        logger.info(
            "CompartmentLoadingAgent: re-placed %.0fL of %.0fL stripped by "
            "cross-contamination rules; %d order(s) not re-placeable "
            "(tenant=%s, run_id=%s)",
            replaced_total,
            sum(stripped_liters.values()),
            len(entries),
            tenant_id,
            run_id,
        )
        return entries

    # ------------------------------------------------------------------
    # Build delivery requests from priorities (Req 3.1) — legacy path
    # ------------------------------------------------------------------

    def _build_delivery_requests(
        self, priority_list: DeliveryPriorityList
    ) -> List[DeliveryRequest]:
        """Convert priority list into delivery requests (legacy fallback).

        Includes priorities with CRITICAL, HIGH, or MEDIUM buckets.
        Assigns a default quantity based on priority score.

        NOTE: This method is retained as a fallback for the legacy
        DeliveryPriorityList path. The primary intake path now reads
        product_code and gallons_requested directly from fuel_orders_current
        via _build_delivery_requests_from_orders (Task 11.3 / Req 5.3.1).
        """
        requests: List[DeliveryRequest] = []
        for priority in priority_list.priorities:
            if priority.priority_bucket not in (
                PriorityBucket.CRITICAL,
                PriorityBucket.HIGH,
                PriorityBucket.MEDIUM,
            ):
                continue

            # Estimate delivery quantity based on priority score
            # Higher priority → larger delivery
            base_quantity = 5000.0  # Base delivery in liters
            quantity = base_quantity * (0.5 + priority.priority_score * 0.5)

            requests.append(
                DeliveryRequest(
                    station_id=priority.station_id,
                    fuel_grade=priority.fuel_grade,
                    quantity_liters=round(quantity, 2),
                    min_drop_liters=DEFAULT_MIN_DROP_LITERS,
                )
            )

        return requests

    # ------------------------------------------------------------------
    # Build delivery requests from Fuel_Orders (Task 11.3 / Req 5.3.1, 5.3.2)
    # ------------------------------------------------------------------

    async def _build_delivery_requests_from_orders(
        self,
        tenant_id: str,
        priority_list: DeliveryPriorityList,
    ) -> List[DeliveryRequest]:
        """Build delivery requests from the tenant's Fuel_Orders, in priority order.

        Validates: Requirements 5.3.1, 5.3.2.

        * Priorities are matched to orders by ``order_id``, then
          ``customer_tank_id``, then ``customer_id``. The prioritization agent
          keys each entry ``customer_tank_id or order_id``, so matching on
          ``customer_id`` alone (as before) matched nothing for US orders.
          When the list has CRITICAL/HIGH/MEDIUM entries, only orders that
          match one of them are loaded.
        * Requests come back by matched ``priority_score`` descending,
          unmatched last, then ``order_id``: the fleet allocator gives earlier
          requests first pick of the trucks.
        * ``product_code`` is canonicalized and is what every plan, approval
          and persisted document carries. The legacy ``FuelGrade`` is derived
          from it only for legacy compartment eligibility.
        * An order on a known customer tank is capped at the tank's ullage
          (``hard_cap_liters``); ``fill_to_full`` asks for exactly the ullage.
          Orders on the same tank share that ullage in priority order, so
          their caps together never exceed it; an order left with 0 L is
          skipped. A full tank is skipped. An unknown tank is loaded as
          requested.
        * Orders with no resolvable volume fail with ``unresolved_fill_volume``.

        Falls back to the legacy station path only when fuel_orders_current
        returns no orders at all. When orders exist but none can be loaded
        the result is empty: the fallback used to invent 5 000 L station
        demands on top of the real orders.
        """
        # Query fuel orders for this tenant that are in loadable statuses
        fuel_orders = await self._query_fuel_orders(tenant_id)

        if not fuel_orders:
            # K11: no uncommitted order. When committed ones exist, the tenant
            # has real demand already on applied plans: never invent legacy
            # station demand on top of it.
            committed = await self._count_committed_loadable_orders(tenant_id)
            if committed is None:
                # Probe failed: fail closed (no invented demand), no flag.
                return []
            if committed > 0:
                self._all_orders_committed = True
                self._committed_order_count = committed
                logger.info(
                    "CompartmentLoadingAgent: no uncommitted fuel order for "
                    "tenant %s; %d loadable order(s) already committed",
                    tenant_id,
                    committed,
                )
                return []
            # Fallback to legacy priority-list path during deprecation window
            logger.debug(
                "CompartmentLoadingAgent: no fuel_orders_current docs found "
                "for tenant %s; falling back to legacy priority-list path",
                tenant_id,
            )
            return self._build_delivery_requests(priority_list)

        eligible = [
            p
            for p in priority_list.priorities
            if p.priority_bucket in (
                PriorityBucket.CRITICAL,
                PriorityBucket.HIGH,
                PriorityBucket.MEDIUM,
            )
        ]
        by_order: Dict[str, DeliveryPriority] = {}
        by_station: Dict[str, DeliveryPriority] = {}
        for p in eligible:
            if p.order_id:
                by_order.setdefault(p.order_id, p)
            by_station.setdefault(p.station_id, p)

        tank_cache: Dict[str, Optional[CustomerTank]] = {}
        scored: List[_OrderCandidate] = []

        for order in fuel_orders:
            order_id = order.get("order_id", "")
            station_id = order.get("customer_id", "")
            product_code = order.get("product_code")
            gallons_requested = order.get("gallons_requested")
            fill_to_full = order.get("fill_to_full", False)
            customer_tank_id = order.get("customer_tank_id")
            if order_id:
                # R3.10: what the plan was built from, checked at approve time.
                self._order_snapshots[order_id] = {
                    "product_code": product_code,
                    "customer_tank_id": customer_tank_id,
                    "gallons_requested": gallons_requested,
                    "fill_to_full": fill_to_full,
                }

            priority = self._match_priority(order, by_order, by_station)
            if eligible and priority is None:
                # Not in the CRITICAL/HIGH/MEDIUM set for this run.
                continue

            if not product_code:
                logger.warning(
                    "CompartmentLoadingAgent: order %s has no product_code; "
                    "skipping",
                    order_id,
                )
                continue
            try:
                canonical_code = canonicalize(product_code)
            except (UnknownFuelProductError, TypeError):
                logger.warning(
                    "CompartmentLoadingAgent: order %s has unrecognized "
                    "product_code %r; skipping",
                    order_id,
                    product_code,
                )
                continue
            # Family only: eligibility for legacy AGO/PMS/ATK/LPG compartments.
            # DEF belongs to no family; AGO just fills the required field, and
            # segregation/eligibility key on the product code.
            fuel_grade = FuelGrade(legacy_grade_for_product(canonical_code) or "AGO")

            ullage_gal: Optional[float] = None
            if customer_tank_id:
                tank = await self._customer_tank_cached(
                    tenant_id, customer_tank_id, order_id, tank_cache
                )
                if tank is not None:
                    ullage_gal = max(
                        0.0,
                        float(tank.capacity_gallons)
                        - float(tank.current_level_gallons),
                    )
                    if ullage_gal <= 0:
                        logger.info(
                            "CompartmentLoadingAgent: customer_tank %s is "
                            "full (capacity=%.1f, level=%.1f); skipping order %s",
                            customer_tank_id,
                            tank.capacity_gallons,
                            tank.current_level_gallons,
                            order_id,
                        )
                        continue

            if fill_to_full and ullage_gal is not None:
                requested_liters = ullage_gal * GALLONS_TO_LITERS
            elif gallons_requested is not None and gallons_requested > 0:
                requested_liters = gallons_requested * GALLONS_TO_LITERS
            else:
                logger.error(
                    "CompartmentLoadingAgent: unresolved_fill_volume for "
                    "order %s (fill_to_full=%s, customer_tank_id=%s) — "
                    "neither gallons_requested nor a resolvable tank level",
                    order_id,
                    fill_to_full,
                    customer_tank_id,
                )
                await self._fail_order_loading(
                    order_id=order_id,
                    tenant_id=tenant_id,
                    reason="unresolved_fill_volume",
                    details={
                        "customer_tank_id": customer_tank_id,
                        "fill_to_full": fill_to_full,
                    },
                )
                continue

            scored.append(_OrderCandidate(
                score=priority.priority_score if priority is not None else None,
                order_id=order_id,
                # Only a tank with a known ullage has a budget to share.
                tank_id=customer_tank_id if ullage_gal is not None else None,
                ullage_liters=(
                    ullage_gal * GALLONS_TO_LITERS if ullage_gal is not None else None
                ),
                requested_liters=requested_liters,
                station_id=station_id,
                fuel_grade=fuel_grade,
                product_code=canonical_code,
            ))

        # Priority order, then the tank budget: two orders on one tank (a
        # duplicate import, a re-order before delivery) share its ullage, and
        # the higher-priority order draws on it first (review R3).
        scored.sort(key=lambda c: (c.score is None, -(c.score or 0.0), c.order_id))
        remaining_liters: Dict[str, float] = {}
        # K11 committed tank draw: volume already on applied plans or in flight
        # comes off each known tank's budget first, so it can under-load but
        # never overfill.
        ullage_by_tank: Dict[str, float] = {}
        for c in scored:
            if c.tank_id is not None and c.ullage_liters is not None:
                ullage_by_tank.setdefault(c.tank_id, c.ullage_liters)
        if ullage_by_tank:
            draw = await self._committed_tank_draw(
                tenant_id, sorted(ullage_by_tank)
            )
            for tank_id, drawn in draw.items():
                if tank_id not in ullage_by_tank:
                    continue
                remaining_liters[tank_id] = (
                    0.0
                    if drawn is None
                    else max(0.0, round(ullage_by_tank[tank_id] - drawn, 2))
                )
        requests: List[DeliveryRequest] = []
        for candidate in scored:
            cap_liters = candidate.requested_liters
            if candidate.tank_id is not None:
                left = remaining_liters.setdefault(
                    candidate.tank_id, round(candidate.ullage_liters, 2)
                )
                cap_liters = min(cap_liters, left)
            cap_liters = round(cap_liters, 2)
            if cap_liters <= 0:
                logger.info(
                    "CompartmentLoadingAgent: order %s resolves to 0 L "
                    "(customer_tank %s ullage already allocated); skipping",
                    candidate.order_id,
                    candidate.tank_id,
                )
                continue
            if candidate.tank_id is not None:
                remaining_liters[candidate.tank_id] = round(
                    remaining_liters[candidate.tank_id] - cap_liters, 2
                )
            requests.append(DeliveryRequest(
                station_id=candidate.station_id,
                order_id=candidate.order_id,
                fuel_grade=candidate.fuel_grade,
                # The order's own product code, canonicalized. Carried
                # alongside the coarse grade so the solver can weigh and
                # label the exact product — DEF is 1.09 kg/L but maps to AGO.
                product_code=candidate.product_code,
                quantity_liters=cap_liters,
                hard_cap_liters=cap_liters,
                min_drop_liters=DEFAULT_MIN_DROP_LITERS,
            ))

        if not requests:
            logger.info(
                "CompartmentLoadingAgent: %d fuel order(s) for tenant %s "
                "yielded no loadable delivery request",
                len(fuel_orders),
                tenant_id,
            )
        return requests

    @staticmethod
    def _match_priority(
        order: Mapping[str, Any],
        by_order: Mapping[str, DeliveryPriority],
        by_station: Mapping[str, DeliveryPriority],
    ) -> Optional[DeliveryPriority]:
        """The priority entry for ``order``: by order id, tank, then customer."""
        order_id = order.get("order_id")
        if order_id:
            if order_id in by_order:
                return by_order[order_id]
            if order_id in by_station:
                return by_station[order_id]
        for key in (order.get("customer_tank_id"), order.get("customer_id")):
            if key and key in by_station:
                return by_station[key]
        return None

    # ------------------------------------------------------------------
    # Query fuel orders from fuel_orders_current (Task 11.3)
    # ------------------------------------------------------------------

    async def _query_fuel_orders(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Query fuel_orders_current for orders in loadable statuses.

        Returns orders with status IN {placed, confirmed, scheduled} that
        are ready for compartment loading. Reads product_code and
        gallons_requested directly from each order document.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"status": list(_LOADABLE_ORDER_STATUSES)}},
                    ],
                    # K11 / R8.1: committed orders (linked run id) are not
                    # loadable; a hand-scheduled order with no run id is (R8.3).
                    "filter": [_unlinked_clause(COMMITTED_ORDER_FIELD)],
                },
            },
            "size": 500,
        }

        try:
            # Read-cutover: serve from Postgres when enabled (tenant-scoped).
            from commerce.services.commerce_persistence_bridge import (
                _NOT_CUT_OVER,
                read_hybrid_search,
            )

            pg = await read_hybrid_search(
                "fuel_order", tenant_id,
                in_filters={"status": list(_LOADABLE_ORDER_STATUSES)},
                unlinked_fields=[COMMITTED_ORDER_FIELD],
                page=1, size=500,
            )
            if pg is not _NOT_CUT_OVER:
                return pg.get("items", [])

            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, query, 500
            )
            orders: List[Dict[str, Any]] = []
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit.get("_source")
                if source:
                    orders.append(source)
            return orders
        except Exception as e:
            logger.error(
                "CompartmentLoadingAgent: failed to query fuel_orders_current "
                "for tenant %s: %s",
                tenant_id,
                e,
            )
            return []

    async def _read_orders(
        self,
        tenant_id: str,
        statuses: Sequence[str],
        *,
        linked: bool,
        tank_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Tenant-scoped order read by status, optionally linked to a run.

        ``linked=True`` keeps only orders with a non-empty ``assigned_run_id``
        (K11 committed). Raises on a store error; the callers decide how to
        fail closed.
        """
        in_filters: Dict[str, list] = {"status": list(statuses)}
        must: List[Dict[str, Any]] = [
            {"term": {"tenant_id": tenant_id}},
            {"terms": {"status": list(statuses)}},
        ]
        if tank_ids is not None:
            in_filters["customer_tank_id"] = list(tank_ids)
            must.append({"terms": {"customer_tank_id": list(tank_ids)}})
        bool_query: Dict[str, Any] = {"must": must}
        if linked:
            bool_query["filter"] = [{"exists": {"field": COMMITTED_ORDER_FIELD}}]
            bool_query["must_not"] = [{"term": {COMMITTED_ORDER_FIELD: ""}}]

        from commerce.services.commerce_persistence_bridge import (
            _NOT_CUT_OVER,
            read_hybrid_search,
        )

        pg = await read_hybrid_search(
            "fuel_order", tenant_id,
            in_filters=in_filters,
            exists_fields=[COMMITTED_ORDER_FIELD] if linked else None,
            page=1, size=500,
        )
        if pg is not _NOT_CUT_OVER:
            orders = list(pg.get("items", []) or [])
        else:
            resp = await self._es.search_documents(
                FUEL_ORDERS_CURRENT_INDEX, {"query": {"bool": bool_query}, "size": 500}, 500
            )
            orders = [
                hit["_source"]
                for hit in resp.get("hits", {}).get("hits", [])
                if hit.get("_source")
            ]
        # Re-check in Python: exists_fields keeps "" on the hybrid path, and
        # the status set must hold whatever the backend returned.
        wanted = set(statuses)
        return [
            o for o in orders
            if o.get("status") in wanted
            and (not linked or o.get(COMMITTED_ORDER_FIELD))
        ]

    async def _count_committed_loadable_orders(
        self, tenant_id: str
    ) -> Optional[int]:
        """Loadable-status orders already linked to a run (K11), or ``None``
        when the probe fails (logged WARNING)."""
        try:
            committed = await self._read_orders(
                tenant_id, _LOADABLE_ORDER_STATUSES, linked=True
            )
        except Exception as e:
            logger.warning(
                "CompartmentLoadingAgent: committed-order probe failed for "
                "tenant %s (%s); building no delivery request",
                tenant_id,
                type(e).__name__,
            )
            return None
        return len(committed)

    async def _committed_tank_draw(
        self, tenant_id: str, tank_ids: Sequence[str]
    ) -> Dict[str, Optional[float]]:
        """Litres already committed to each tank (K11); ``None`` = full draw.

        Counts dispatched/in_transit orders whatever their run id, plus
        loadable-status orders linked to a run. A ``fill_to_full`` order
        draws the whole ullage; otherwise ``gallons_requested`` in litres. On
        a read error every tank is treated as fully drawn (fail closed for
        overfill).
        """
        if not tank_ids:
            return {}
        try:
            in_flight = await self._read_orders(
                tenant_id, _IN_FLIGHT_ORDER_STATUSES, linked=False,
                tank_ids=tank_ids,
            )
            committed = await self._read_orders(
                tenant_id, _LOADABLE_ORDER_STATUSES, linked=True,
                tank_ids=tank_ids,
            )
        except Exception as e:
            logger.warning(
                "CompartmentLoadingAgent: committed tank-draw read failed for "
                "tenant %s (%s); treating %d known tank(s) as full",
                tenant_id,
                type(e).__name__,
                len(tank_ids),
            )
            return {tank_id: None for tank_id in tank_ids}

        wanted = set(tank_ids)
        draw: Dict[str, Optional[float]] = {}
        seen: set = set()
        for order in [*in_flight, *committed]:
            tank_id = order.get("customer_tank_id")
            order_id = order.get("order_id")
            if tank_id not in wanted or order.get("tenant_id") not in (None, tenant_id):
                continue
            if order_id and order_id in seen:
                continue
            if order_id:
                seen.add(order_id)
            if tank_id in draw and draw[tank_id] is None:
                continue
            if order.get("fill_to_full"):
                draw[tank_id] = None
                continue
            try:
                liters = float(order.get("gallons_requested") or 0.0) * GALLONS_TO_LITERS
            except (TypeError, ValueError):
                # Unknown volume on a committed order: assume the full draw.
                draw[tank_id] = None
                continue
            draw[tank_id] = round((draw.get(tank_id) or 0.0) + max(0.0, liters), 2)
        return draw

    # ------------------------------------------------------------------
    # Customer tank resolution for ullage caps (Task 11.3 / Req 5.3.2)
    # ------------------------------------------------------------------

    async def _customer_tank_cached(
        self,
        tenant_id: str,
        customer_tank_id: str,
        order_id: str,
        cache: Dict[str, Optional[CustomerTank]],
    ) -> Optional[CustomerTank]:
        """The linked customer tank, read once per evaluate, or ``None``.

        ``None`` means the ullage is unknown: the order is then loaded as
        requested, without a tank cap, and a WARNING says so.
        """
        if customer_tank_id in cache:
            return cache[customer_tank_id]
        tank: Optional[CustomerTank] = None
        try:
            tank = await self._customer_tank_repo.get(
                tenant_id=tenant_id,
                customer_tank_id=customer_tank_id,
            )
            if tank is not None:
                # Validate the two levels up front so a malformed doc is
                # "unknown" rather than a crash in the caller.
                float(tank.capacity_gallons)
                float(tank.current_level_gallons)
        except Exception as exc:
            logger.warning(
                "CompartmentLoadingAgent: failed to read customer_tank %s "
                "for order %s (tenant=%s): %s",
                customer_tank_id,
                order_id,
                tenant_id,
                exc,
            )
            tank = None
        if tank is None:
            logger.warning(
                "CompartmentLoadingAgent: ullage unknown for customer_tank %s "
                "(order %s, tenant=%s); loading as requested without a tank cap",
                customer_tank_id,
                order_id,
                tenant_id,
            )
        cache[customer_tank_id] = tank
        return tank

    # ------------------------------------------------------------------
    # Fail loading with unresolved_fill_volume (Task 11.3)
    # ------------------------------------------------------------------

    async def _fail_order_loading(
        self,
        *,
        order_id: str,
        tenant_id: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a loading failure for an order that cannot be resolved.

        Publishes a RiskSignal so downstream overlays (dispatch, exception
        replanning) can react to the unresolvable order.
        """
        context: Dict[str, Any] = {
            "order_id": order_id,
            "reason": reason,
        }
        if details:
            context.update(details)

        try:
            signal = RiskSignal(
                source_agent=self.agent_id,
                entity_id=order_id,
                entity_type="fuel_order",
                severity=Severity.HIGH,
                confidence=1.0,
                ttl_seconds=3600,
                tenant_id=tenant_id,
                context=context,
            )
            await self._signal_bus.publish(signal)
        except Exception as exc:
            logger.error(
                "CompartmentLoadingAgent: failed to publish %s RiskSignal "
                "for order %s (tenant=%s): %s",
                reason,
                order_id,
                tenant_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Query trucks and compartments (Req 3.1)
    # ------------------------------------------------------------------

    async def _query_trucks(
        self, tenant_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Query truck_compartments ES index for available trucks.

        Returns a dict keyed by truck_id with dicts containing:
        - 'compartments': List[Compartment]
        - 'max_weight_kg': Optional[float]
        - 'tare_weight_kg': float
        - 'depot_location': Optional[str]
        - 'compartment_states': Dict[str, CompartmentState] — keyed by
          the in-memory ``compartment.compartment_id`` (not the composite
          ES doc id) so the cross-contamination guard (Task 6.5) can
          look the prior-load state up in O(1) without a second ES
          round-trip.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                    ],
                },
            },
            "size": 200,
        }

        trucks: Dict[str, Dict[str, Any]] = {}
        try:
            resp = await self._es.search_documents(
                TRUCK_COMPARTMENTS_INDEX, query, 200
            )
            for hit in resp.get("hits", {}).get("hits", []):
                source = hit["_source"]
                truck_id = source.get("truck_id", "")
                if not truck_id:
                    continue

                # Hydrate eligibility. Two fields can carry it:
                #
                #   allowed_product_codes — exact canonical US codes, authoritative
                #   allowed_grades        — legacy four-value NG family
                #
                # A US product code found in ``allowed_grades`` is kept as an
                # exact code rather than collapsed to its family grade. This
                # replaces ``fuel_product_mapper.us_to_fuel_grade``, which
                # mapped HEATING_OIL -> AGO and thereby widened a heating-oil
                # compartment to accept taxed road diesel — two different tax
                # classes (off_road vs road_diesel).
                allowed_grades_raw = source.get("allowed_grades", []) or []
                explicit_codes: List[str] = [
                    str(c) for c in (source.get("allowed_product_codes") or [])
                ]
                allowed_grades: List[FuelGrade] = []

                for g in allowed_grades_raw:
                    try:
                        allowed_grades.append(FuelGrade(g))
                        continue
                    except ValueError:
                        pass
                    if is_known_product(g):
                        canonical = canonicalize(g)
                        if canonical not in explicit_codes:
                            explicit_codes.append(canonical)
                    else:
                        logger.debug(
                            "CompartmentLoadingAgent: unrecognized fuel grade '%s' "
                            "for truck %s compartment %s",
                            g, truck_id, source.get("compartment_id"),
                        )

                # ``allowed_grades`` is required (min_length=1). When the stored
                # data was entirely US codes, derive each family so the model
                # validates; ``allowed_product_codes`` stays authoritative for
                # eligibility, so the derived grades cannot widen anything.
                if not allowed_grades and explicit_codes:
                    for code in explicit_codes:
                        family = legacy_grade_for_product(code)
                        if family and FuelGrade(family) not in allowed_grades:
                            allowed_grades.append(FuelGrade(family))
                    if not allowed_grades:
                        # Every code is an isolate (e.g. DEF only). Any grade
                        # satisfies the field; eligibility comes from the codes.
                        allowed_grades = [FuelGrade.AGO]

                if not allowed_grades:
                    logger.debug(
                        "CompartmentLoadingAgent: truck %s compartment %s has no valid allowed_grades, skipping",
                        truck_id, source.get("compartment_id")
                    )
                    continue

                compartment = Compartment(
                    compartment_id=source.get("compartment_id", ""),
                    truck_id=truck_id,
                    capacity_liters=source.get("capacity_liters", 0.0),
                    allowed_grades=allowed_grades,
                    allowed_product_codes=explicit_codes or None,
                    position_index=source.get("position_index", 0),
                    tenant_id=tenant_id,
                )

                if truck_id not in trucks:
                    trucks[truck_id] = {
                        "compartments": [],
                        "max_weight_kg": source.get("max_weight_kg"),
                        "tare_weight_kg": source.get("tare_weight_kg", 0.0),
                        "depot_location": source.get("depot_city"),  # Use depot_city for equipment check
                        "compartment_states": {},
                    }
                trucks[truck_id]["compartments"].append(compartment)

                # Capture the compartment state triple for the
                # cross-contamination guard. Legacy documents predating
                # Task 6.1 have no state fields; build a permissive
                # default (``clean``) so the guard treats them as empty
                # rather than incorrectly blocking every load.
                state = self._build_state_from_source(source, tenant_id, truck_id)
                if state is not None:
                    trucks[truck_id]["compartment_states"][compartment.compartment_id] = state
        except Exception as e:
            logger.error(
                "CompartmentLoadingAgent: failed to query truck_compartments: %s",
                e,
            )

        return trucks

    # ------------------------------------------------------------------
    # Compartment state parsing
    # ------------------------------------------------------------------

    def _build_state_from_source(
        self,
        source: Dict[str, Any],
        tenant_id: str,
        truck_id: str,
    ) -> Optional[CompartmentState]:
        """Extract a :class:`CompartmentState` from a truck_compartments hit.

        Legacy pre-Task-6.1 documents that lack any of the four state
        fields are coerced into a ``state=clean`` default so the
        compatibility guard treats them as empty rather than raising.
        An outright validation failure is logged and the state is
        dropped — the guard falls back to the "empty compartment" branch
        and allows the load in that case, matching the behavior of the
        engine's ``_is_empty_previous`` short-circuit.
        """

        compartment_id = source.get("compartment_id")
        if not compartment_id:
            return None
        payload = {
            "compartment_id": compartment_id,
            "truck_id": truck_id,
            "tenant_id": tenant_id,
            "state": source.get("state") or "clean",
            "last_loaded_product": source.get("last_loaded_product"),
            "last_loaded_at": source.get("last_loaded_at"),
            "last_cleaned_at": source.get("last_cleaned_at"),
        }
        try:
            return CompartmentState(**payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "CompartmentLoadingAgent: dropping unparsable compartment "
                "state for %s (tenant=%s, truck=%s): %s",
                compartment_id,
                tenant_id,
                truck_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Fuel equipment availability check (Req 3.1, 3.2, 3.3, 3.4, 3.5)
    # ------------------------------------------------------------------

    async def _check_fuel_equipment(
        self, truck_id: str, depot_location: str, tenant_id: str
    ) -> Tuple[bool, List[str]]:
        """Check fuel equipment availability at a truck's depot.

        Queries the inventory index for items with category ``fuel_equipment``
        at the given depot location. If any item has status ``out_of_stock``,
        the truck is considered unavailable for loading.

        Args:
            truck_id: The truck being evaluated.
            depot_location: The depot where the truck is based.
            tenant_id: Tenant scope.

        Returns:
            Tuple of (available: bool, missing_item_ids: List[str]).
            ``available`` is True if all fuel_equipment items are in stock.
            ``missing_item_ids`` contains item_ids of out_of_stock items.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"category": "fuel_equipment"}},
                        {"term": {"tenant_id": tenant_id}},
                        {"match": {"location": depot_location}},
                    ],
                },
            },
            "size": 100,
        }

        try:
            resp = await self._es.search_documents(
                INVENTORY_INDEX, query, 100
            )
            hits = resp.get("hits", {}).get("hits", [])

            missing_item_ids: List[str] = []
            for hit in hits:
                source = hit["_source"]
                if source.get("status") == "out_of_stock":
                    item_id = source.get("item_id", "")
                    if item_id:
                        missing_item_ids.append(item_id)

            available = len(missing_item_ids) == 0
            return available, missing_item_ids

        except Exception as e:
            # Fail-open: on inventory query failure, include the truck (Req 3.5)
            logger.warning(
                "CompartmentLoadingAgent: inventory query failed for truck %s "
                "at depot %s, failing open: %s",
                truck_id,
                depot_location,
                e,
            )
            return True, []

    async def _query_trucks_with_equipment_check(
        self, tenant_id: str
    ) -> Dict[str, Dict[str, Any]]:
        """Query trucks and filter by fuel equipment availability.

        Wraps ``_query_trucks`` and removes trucks whose depot lacks
        required fuel_equipment items (status out_of_stock). If all trucks
        are excluded, publishes a critical RiskSignal indicating equipment
        shortage.

        Fail-open: trucks without a known depot_location are included
        without an equipment check.

        Args:
            tenant_id: Tenant scope.

        Returns:
            Filtered dict of trucks with equipment available at their depot.
        """
        trucks = await self._query_trucks(tenant_id)
        logger.info(
            "CompartmentLoadingAgent: _query_trucks returned %d trucks for tenant %s",
            len(trucks),
            tenant_id,
        )
        if not trucks:
            return trucks

        eligible_trucks: Dict[str, Dict[str, Any]] = {}
        excluded_trucks: List[Dict[str, Any]] = []

        for truck_id, truck_data in trucks.items():
            depot_location = truck_data.get("depot_location")

            # Fail-open: if no depot_location known, include the truck
            if not depot_location:
                eligible_trucks[truck_id] = truck_data
                continue

            available, missing_item_ids = await self._check_fuel_equipment(
                truck_id, depot_location, tenant_id
            )

            if available:
                eligible_trucks[truck_id] = truck_data
            else:
                # Req 3.4: Log exclusion with truck_id, depot, and missing item_ids
                logger.warning(
                    "CompartmentLoadingAgent: excluding truck %s — "
                    "depot %s missing fuel_equipment items: %s",
                    truck_id,
                    depot_location,
                    missing_item_ids,
                )
                excluded_trucks.append({
                    "truck_id": truck_id,
                    "depot_location": depot_location,
                    "missing_item_ids": missing_item_ids,
                })

        # Req 3.5: If all trucks excluded, publish critical RiskSignal
        if not eligible_trucks and excluded_trucks:
            await self._publish_equipment_shortage_signal(
                tenant_id, excluded_trucks
            )

        return eligible_trucks

    async def _publish_equipment_shortage_signal(
        self,
        tenant_id: str,
        excluded_trucks: List[Dict[str, Any]],
    ) -> None:
        """Publish a critical RiskSignal when all trucks lack fuel equipment.

        Args:
            tenant_id: Tenant scope.
            excluded_trucks: List of dicts with truck_id, depot_location,
                and missing_item_ids for each excluded truck.
        """
        try:
            signal = RiskSignal(
                source_agent=self.agent_id,
                entity_id="fuel_equipment_shortage",
                entity_type="equipment_shortage",
                severity=Severity.CRITICAL,
                confidence=1.0,
                ttl_seconds=3600,
                tenant_id=tenant_id,
                context={
                    "reason": "All candidate trucks excluded due to fuel equipment shortage",
                    "excluded_truck_count": len(excluded_trucks),
                    "excluded_trucks": excluded_trucks,
                },
            )
            await self._signal_bus.publish(signal)
            logger.critical(
                "CompartmentLoadingAgent: ALL trucks excluded for tenant %s "
                "due to fuel equipment shortage — critical RiskSignal published",
                tenant_id,
            )
        except Exception as e:
            logger.error(
                "CompartmentLoadingAgent: failed to publish equipment "
                "shortage RiskSignal: %s",
                e,
            )

    # ------------------------------------------------------------------
    # Build InterventionProposal
    # ------------------------------------------------------------------

    def _build_proposal(
        self,
        loading_plan: LoadingPlan,
        feasibility: FeasibilityResult,
        tenant_id: str,
    ) -> InterventionProposal:
        """Build an InterventionProposal from a loading plan.

        ``run_id`` and ``order_ids`` let the approval queue spot two
        ``apply_loading_plan`` approvals that would dispatch the same order
        (F10): approving one supersedes the other.
        """
        order_ids: List[str] = []
        for a in loading_plan.assignments:
            if a.order_id and a.order_id not in order_ids:
                order_ids.append(a.order_id)
        actions = [
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": loading_plan.plan_id,
                    "run_id": loading_plan.run_id,
                    "order_ids": order_ids,
                    "truck_id": loading_plan.truck_id,
                    # R3.10: the order fields this plan was built from; the
                    # executor refuses a plan whose orders changed since.
                    "order_snapshots": {
                        oid: self._order_snapshots[oid]
                        for oid in order_ids
                        if oid in self._order_snapshots
                    },
                    "assignments": [
                        a.model_dump(mode="json")
                        for a in loading_plan.assignments
                    ],
                    "total_utilization_pct": loading_plan.total_utilization_pct,
                    "unserved_demand_liters": loading_plan.unserved_demand_liters,
                    "total_weight_kg": loading_plan.total_weight_kg,
                    # Forward the optional external-lift terminal id so the
                    # Route_Planning_Agent (Task 7.10) can detect the
                    # external-lift condition and invoke the
                    # Sourcing_Recommender. ``None`` when the plan will
                    # lift from the tenant's depot.
                    "terminal_id": loading_plan.terminal_id,
                    "contract_id": loading_plan.contract_id,
                },
                "description": (
                    f"Loading plan for truck {loading_plan.truck_id}: "
                    f"{loading_plan.total_utilization_pct:.1f}% utilization, "
                    f"{loading_plan.unserved_demand_liters:.0f}L unserved"
                ),
            }
        ]

        risk_class = RiskClass.LOW
        if loading_plan.unserved_demand_liters > 0:
            risk_class = RiskClass.MEDIUM

        return InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta={
                "truck_utilization_pct": loading_plan.total_utilization_pct,
                "unserved_demand_liters": -loading_plan.unserved_demand_liters,
            },
            risk_class=risk_class,
            confidence=0.85 if feasibility.feasible else 0.5,
            priority=1,
            tenant_id=tenant_id,
        )

    # ------------------------------------------------------------------
    # Persistence (Req 3.9)
    # ------------------------------------------------------------------

    async def _persist_loading_plan(
        self,
        loading_plan: LoadingPlan,
        *,
        commit_compartment_state: bool = True,
    ) -> None:
        """Persist a LoadingPlan to the mvp_load_plans ES index.

        Canonicalizes the ``fuel_grade`` on every assignment before write
        so loading plans produced from NG-aliased forecasts land in ES as
        the canonical US codes (Req 6.1.4). Unknown values are preserved
        with a warning rather than dropped to avoid silently corrupting a
        plan that has already passed feasibility.

        On a successful plan commit, each assigned compartment's
        ``last_loaded_product``, ``last_loaded_at``, and ``state`` fields
        are updated atomically in the ``truck_compartments`` index via
        :class:`CompartmentStateRepository` (Req 7.1.2, Task 6.6). Per-
        compartment state failures are logged but never raised so a
        transient ES hiccup on one compartment cannot invalidate a plan
        that already landed in ``mvp_load_plans``.

        ``commit_compartment_state`` gates the post-write state update
        so shadow-mode evaluation — which still records the plan for
        retrospective analysis via ``evaluate`` → ``_log_shadow_proposal``
        — never mutates the live compartment state. Active and
        active-gated/auto modes pass ``True`` so the spec's
        "only on successful commit" guarantee (Req 7.1.2) holds.
        """
        try:
            doc = loading_plan.model_dump(mode="json")
            assignments = doc.get("assignments") or []
            for assignment in assignments:
                grade = assignment.get("fuel_grade")
                if grade is not None:
                    assignment["fuel_grade"] = canonicalize_or_warn(
                        grade,
                        context="mvp_load_plans.assignments.fuel_grade",
                        logger_=logger,
                    )
            await self._es.index_document(
                MVP_LOAD_PLANS_INDEX,
                loading_plan.plan_id,
                doc,
            )
        except Exception as e:
            logger.error(
                "CompartmentLoadingAgent: failed to persist loading plan %s: %s",
                loading_plan.plan_id,
                e,
            )
            # Do not attempt compartment-state updates for a plan that did
            # not land in mvp_load_plans — otherwise the compartment would
            # report as loaded against a plan that was never committed.
            return

        # Req 7.1.2 / Task 6.6: write last_loaded_product, last_loaded_at,
        # state=loaded on every compartment assigned by this plan. The
        # canonical fuel_grade from the persisted doc is the source of
        # truth so the compartment state matches what mvp_load_plans
        # stores. Shadow-mode cycles skip this entirely — the spec
        # reserves the state mutation for successful commits, and the
        # mvp_load_plans write in shadow mode is recorded for analysis
        # only (the ``InterventionProposal`` is shipped to
        # ``agent_shadow_proposals`` rather than the ConfirmationProtocol).
        if commit_compartment_state:
            await self._record_compartment_loads(loading_plan, doc)
        else:
            logger.debug(
                "CompartmentLoadingAgent: shadow mode — skipping last_loaded "
                "state update for plan %s (tenant=%s)",
                loading_plan.plan_id,
                loading_plan.tenant_id,
            )

        # Task 7.6 / Req 8.3.4: bump the monthly rolling-lift counter
        # whenever the plan was sourced against a specific
        # Supplier_Contract. When no ``contract_id`` is attached (legacy
        # plans, depot-only loads) this is a no-op.
        await self._record_contract_lift(loading_plan)

    async def _record_compartment_loads(
        self,
        loading_plan: LoadingPlan,
        persisted_doc: Dict[str, Any],
    ) -> None:
        """Atomically stamp every loaded compartment with its last-loaded fields.

        Validates: Requirement 7.1.2.

        For each assignment in ``loading_plan``, build the truck-qualified
        document id (``{truck_id}_{compartment_id}``, matching the write
        key used by ``mvp_endpoints.configure_compartments``) and call
        :meth:`CompartmentStateRepository.mark_loaded`. The repository
        handles the ``_seq_no`` / ``_primary_term`` OCC loop so concurrent
        plan commits cannot silently overwrite each other.

        Failures for individual compartments are logged and swallowed:

        * :class:`CompartmentNotFoundError` — misconfigured plan that
          references a compartment no longer in ``truck_compartments``.
        * :class:`CrossTenantCompartmentAccessError` — defensive guard,
          should never fire because the plan is tenant-scoped.
        * :class:`CompartmentStateConflictError` — persistent OCC
          contention; surfaced as a warning so operators can investigate.
        * :class:`UnknownFuelProductError` — already canonicalized above,
          so only fires if the catalog rejected the value; logged as an
          error and skipped.

        The loading plan itself has already been persisted to
        ``mvp_load_plans`` before this method is called, so swallowing
        per-compartment errors never corrupts the primary write.
        """

        tenant_id = loading_plan.tenant_id
        truck_id = loading_plan.truck_id
        if not tenant_id or not truck_id:
            # Defensive: the model validator guarantees non-empty values,
            # but if a caller constructs a plan via __new__ bypassing the
            # validator we want a clean skip rather than a noisy traceback.
            logger.warning(
                "CompartmentLoadingAgent: skipping compartment-state updates "
                "for plan %s — missing tenant_id or truck_id",
                loading_plan.plan_id,
            )
            return

        # Defensive access: some callers (notably unit-test helpers that
        # instantiate the agent via ``__new__``) skip ``__init__`` and
        # therefore never wire the repository. Treat that as a no-op
        # rather than a traceback — the primary mvp_load_plans write has
        # already succeeded.
        repo = getattr(self, "_compartment_state_repo", None)
        if repo is None:
            logger.debug(
                "CompartmentLoadingAgent: no compartment_state_repo configured; "
                "skipping last_loaded state update for plan %s",
                loading_plan.plan_id,
            )
            return

        loaded_at = datetime.now(timezone.utc)
        # Drive the writes off the persisted doc so the canonical
        # fuel_grade (post canonicalize_or_warn) is the value that lands
        # on the compartment. This keeps mvp_load_plans and
        # truck_compartments in agreement on product_code.
        persisted_assignments = persisted_doc.get("assignments") or []
        if len(persisted_assignments) != len(loading_plan.assignments):
            # The doc is generated from the same plan in the same method,
            # so a length mismatch would indicate a serializer change. Fall
            # back to the in-memory assignments to stay safe.
            persisted_assignments = [
                {
                    "compartment_id": a.compartment_id,
                    "fuel_grade": a.fuel_grade,
                }
                for a in loading_plan.assignments
            ]

        for persisted in persisted_assignments:
            compartment_id = persisted.get("compartment_id")
            product_code = persisted.get("fuel_grade")
            if not compartment_id or not product_code:
                logger.warning(
                    "CompartmentLoadingAgent: skipping state update for "
                    "plan %s — assignment missing compartment_id/fuel_grade: %r",
                    loading_plan.plan_id,
                    persisted,
                )
                continue

            compartment_doc_id = f"{truck_id}_{compartment_id}"
            try:
                await repo.mark_loaded(
                    tenant_id=tenant_id,
                    compartment_doc_id=compartment_doc_id,
                    product_code=product_code,
                    loaded_at=loaded_at,
                )
            except CompartmentNotFoundError:
                logger.warning(
                    "CompartmentLoadingAgent: compartment %s missing from "
                    "truck_compartments during state update for plan %s "
                    "(tenant=%s); skipping",
                    compartment_doc_id,
                    loading_plan.plan_id,
                    tenant_id,
                )
            except CrossTenantCompartmentAccessError:
                logger.error(
                    "CompartmentLoadingAgent: refused cross-tenant compartment "
                    "state update for %s on plan %s (tenant=%s)",
                    compartment_doc_id,
                    loading_plan.plan_id,
                    tenant_id,
                )
            except CompartmentStateConflictError:
                logger.warning(
                    "CompartmentLoadingAgent: persistent OCC conflict on "
                    "compartment %s for plan %s (tenant=%s); last_loaded "
                    "fields may be stale",
                    compartment_doc_id,
                    loading_plan.plan_id,
                    tenant_id,
                )
            except UnknownFuelProductError as exc:
                logger.error(
                    "CompartmentLoadingAgent: catalog rejected canonicalized "
                    "product %r on compartment %s for plan %s (tenant=%s): %s",
                    product_code,
                    compartment_doc_id,
                    loading_plan.plan_id,
                    tenant_id,
                    exc,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "CompartmentLoadingAgent: unexpected failure recording "
                    "last_loaded state for compartment %s (plan=%s, "
                    "tenant=%s): %s",
                    compartment_doc_id,
                    loading_plan.plan_id,
                    tenant_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Contract lift counter (Task 7.6 / Req 8.3.4)
    # ------------------------------------------------------------------

    async def _record_contract_lift(self, loading_plan: LoadingPlan) -> None:
        """Bump the monthly rolling-lift counter for the plan's contract.

        Validates: Requirement 8.3.4.

        When the Loading_Plan carries a ``contract_id`` — set by the
        Route_Planning_Agent when the plan is sourced against a
        Supplier_Contract (Task 7.10) — this method bumps the Redis
        counter ``contract_lift:{tenant_id}:{contract_id}:{YYYY-MM}``
        by the plan's total loaded volume (sum of
        ``assignment.quantity_liters`` converted to gallons via the
        canonical NIST factor).

        When no ``contract_id`` is attached (the common case today for
        depot-only loads) this method is a no-op.

        Failures are logged and swallowed so a transient Redis outage
        cannot invalidate a plan that already landed in
        ``mvp_load_plans``. ``mvp_load_plans`` is the authoritative
        source of truth; the counter is a derived aggregate.
        """

        contract_id = getattr(loading_plan, "contract_id", None)
        if not contract_id:
            return

        service = self._contract_lift_service
        if service is None:
            return

        total_liters = 0.0
        for assignment in loading_plan.assignments:
            qty = getattr(assignment, "quantity_liters", None)
            if qty is None:
                continue
            try:
                total_liters += max(0.0, float(qty))
            except (TypeError, ValueError):
                continue

        if total_liters <= 0.0:
            return

        # Convert liters to canonical gallons for the counter. Using the one
        # shared NIST factor keeps the counter stable across runs even when
        # source units mix (the agent-side LoadingPlan is liters-valued, but
        # the Redis counter and
        # Supplier_Contract.minimum_lift_gallons_per_month are both in
        # gallons).
        gallons = total_liters / GALLONS_TO_LITERS

        try:
            new_total = await service.record_lift(
                tenant_id=loading_plan.tenant_id,
                contract_id=contract_id,
                gallons=gallons,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "CompartmentLoadingAgent: contract-lift bump failed "
                "plan=%s tenant=%s contract=%s gallons=%.3f err=%s",
                loading_plan.plan_id,
                loading_plan.tenant_id,
                contract_id,
                gallons,
                exc,
            )
            return

        logger.info(
            "CompartmentLoadingAgent: contract-lift bump "
            "plan=%s tenant=%s contract=%s gallons=%.3f month_total=%.3f",
            loading_plan.plan_id,
            loading_plan.tenant_id,
            contract_id,
            gallons,
            new_total,
        )

    # ------------------------------------------------------------------
    # Dyed diesel compliance enforcement (Task 9.8 / Req 6.3, 6.4)
    # ------------------------------------------------------------------

    async def _enforce_dyed_diesel_compliance(
        self,
        *,
        loading_plan: LoadingPlan,
        tenant_id: str,
    ) -> Tuple[LoadingPlan, List[CompartmentAssignment]]:
        """Reject assignments that load dyed diesel into clear-only compartments.

        Validates: Requirements 6.3, 6.4.

        For each dyed-diesel assignment in ``loading_plan``, calls
        :meth:`DyedDieselEnforcer.validate_load_plan` to verify the
        compartment is dyed-compatible. If validation fails (error code
        ``dyed.compartment_incompatible`` or ``dyed.compartment_not_found``),
        the assignment is stripped from the plan and its volume is charged
        to ``unserved_demand_liters``.

        Fail closed (OI-02): when the plan carries dyed diesel and the
        enforcer is not wired, or :meth:`validate_load_plan` raises, the
        check could not run, so the plan is not produced as compliant. This
        raises :class:`DyedDieselCheckUnavailable` and logs at ERROR; the
        caller withholds the whole plan. A plan with no dyed assignment is
        returned unchanged whether or not an enforcer is wired.

        Returns the (possibly filtered) LoadingPlan and the stripped
        assignments.
        """
        # Import here to avoid circular dependency at module level
        from compliance.services.dyed_diesel_enforcer import (
            DyedDieselCheckUnavailable,
            DyedDieselEnforcer,
        )

        original_assignments = loading_plan.assignments
        if not any(
            DyedDieselEnforcer.is_dyed_diesel(a.fuel_grade)
            for a in original_assignments
        ):
            return loading_plan, []

        enforcer = getattr(self, "_dyed_diesel_enforcer", None)
        if enforcer is None or not isinstance(enforcer, DyedDieselEnforcer):
            logger.error(
                "CompartmentLoadingAgent: no DyedDieselEnforcer wired; "
                "blocking plan %s (truck=%s, tenant=%s) because it carries "
                "dyed diesel (fail closed, OI-02)",
                loading_plan.plan_id,
                loading_plan.truck_id,
                tenant_id,
            )
            raise DyedDieselCheckUnavailable(
                reason="enforcer_not_wired",
                tenant_id=tenant_id,
                plan_id=loading_plan.plan_id,
                truck_id=loading_plan.truck_id,
            )

        kept_assignments: List[CompartmentAssignment] = []
        stripped: List[CompartmentAssignment] = []

        for assignment in original_assignments:
            product_code = assignment.fuel_grade

            # Only check dyed-diesel products
            if not DyedDieselEnforcer.is_dyed_diesel(product_code):
                kept_assignments.append(assignment)
                continue

            # Validate the compartment is dyed-compatible
            try:
                result = await enforcer.validate_load_plan(
                    tenant_id=tenant_id,
                    compartment_id=assignment.compartment_id,
                    product_code=product_code,
                )
            except Exception as exc:
                # Fail closed (OI-02): an unverified dyed-diesel load is a
                # regulatory exposure, so the plan is blocked, not allowed.
                logger.error(
                    "CompartmentLoadingAgent: dyed diesel validation failed "
                    "for compartment %s (plan=%s, truck=%s, tenant=%s): %s; "
                    "blocking the plan (fail closed, OI-02)",
                    assignment.compartment_id,
                    loading_plan.plan_id,
                    loading_plan.truck_id,
                    tenant_id,
                    type(exc).__name__,
                    exc_info=True,
                )
                raise DyedDieselCheckUnavailable(
                    reason="enforcer_error",
                    tenant_id=tenant_id,
                    plan_id=loading_plan.plan_id,
                    truck_id=loading_plan.truck_id,
                    compartment_id=assignment.compartment_id,
                    cause=type(exc).__name__,
                ) from exc

            if result.valid:
                kept_assignments.append(assignment)
            else:
                # Rejected — log and strip from the plan
                logger.warning(
                    "CompartmentLoadingAgent: dyed diesel assignment rejected "
                    "for compartment %s (plan=%s, tenant=%s): %s [%s]",
                    assignment.compartment_id,
                    loading_plan.plan_id,
                    tenant_id,
                    result.error_code,
                    result.message,
                )
                stripped.append(assignment)

        if not stripped:
            return loading_plan, []

        rejected_volume = sum(float(a.quantity_liters) for a in stripped)
        logger.warning(
            "CompartmentLoadingAgent: stripped %d dyed-diesel assignment(s) "
            "totalling %.0fL from plan %s (tenant=%s) due to "
            "compartment incompatibility (Req 6.3/6.4)",
            len(stripped),
            rejected_volume,
            loading_plan.plan_id,
            tenant_id,
        )

        return (
            self._rebuild_plan(
                loading_plan,
                kept_assignments,
                unserved_delta=rejected_volume,
                capacity_liters=sum(
                    a.compartment_capacity_liters for a in original_assignments
                ),
            ),
            stripped,
        )

    @staticmethod
    def _rebuild_plan(
        plan: LoadingPlan,
        assignments: List[CompartmentAssignment],
        *,
        unserved_delta: float,
        capacity_liters: float,
    ) -> LoadingPlan:
        """Copy ``plan`` with ``assignments`` and recomputed totals.

        Utilization is retained volume over ``capacity_liters``. The
        enforcement passes pass the summed ``compartment_capacity_liters`` of
        the plan's assignments before filtering, as they always computed it;
        the OI-39 merge passes the truck's total compartment capacity, as the
        allocator does. Weight is re-derived per product. ``unserved_demand_liters`` is the
        plan's current value plus ``unserved_delta`` (negative when stripped
        volume was re-placed), clamped at zero.
        """
        retained_volume = sum(a.quantity_liters for a in assignments)
        total_capacity = capacity_liters
        new_utilization = (
            min(100.0, round((retained_volume / total_capacity) * 100, 2))
            if total_capacity > 0
            else 0.0
        )
        new_weight = round(
            sum(
                fuel_density_kg_per_liter(
                    product_code=a.product_code,
                    fuel_grade=a.fuel_grade,
                ) * a.quantity_liters
                for a in assignments
            ),
            2,
        )
        new_unserved = max(
            0.0, round(float(plan.unserved_demand_liters) + unserved_delta, 2)
        )
        return plan.model_copy(
            update={
                "assignments": assignments,
                "total_utilization_pct": new_utilization,
                "total_weight_kg": new_weight,
                "unserved_demand_liters": new_unserved,
            }
        )

    # ------------------------------------------------------------------
    # Cross-contamination enforcement (Task 6.5 / Req 7.2.2, 7.2.3, 7.2.6)
    # ------------------------------------------------------------------

    async def _enforce_cross_contamination(
        self,
        *,
        loading_plan: LoadingPlan,
        truck_id: str,
        tenant_id: str,
        compartment_states: Mapping[str, CompartmentState],
        compatibility_rules: Mapping[Tuple[str, str], "RuleType"],
        run_id: str,
    ) -> Tuple[LoadingPlan, List[CompartmentAssignment]]:
        """Reject any assignment the compatibility matrix blocks or gates.

        Validates: Requirements 7.2.2, 7.2.3, 7.2.6.

        For each assignment in ``loading_plan``:

            1. Canonicalize the proposed ``fuel_grade`` against the fuel
               product catalog so NG aliases (``AGO``, ``PMS``, ``ATK``,
               ``LPG``) resolve to their US equivalents before the matrix
               lookup.
            2. Look up the compartment's current
               :class:`CompartmentState`. Missing state (legacy doc, ES
               hiccup) is treated as an empty compartment — the engine's
               ``_is_empty_previous`` short-circuit then allows the load.
            3. Call :func:`check_compatibility`. If the decision is
               ``allowed``, keep the assignment. Otherwise:

                 * Persist a :class:`CrossContaminationViolation` to the
                   ``cross_contamination_events`` index (best-effort; the
                   write is wrapped so an ES failure never aborts the
                   plan).
                 * Publish a ``cross_contamination_violation`` RiskSignal
                   on the SignalBus with the full rejection context.
                 * Drop the assignment from the plan and charge its
                   volume to ``unserved_demand_liters``.

        Returns ``(plan, stripped)``: the plan is either the original
        (when nothing was rejected) or a fresh :meth:`model_copy` with the
        filtered assignment list and recomputed totals so the downstream
        ``_persist_loading_plan`` / ``_build_proposal`` calls never see a
        rejected assignment; ``stripped`` lists the rejected assignments.

        ``evaluate()`` offers the stripped volume to other compartments and
        trucks in the same run (OI-39, :func:`replace_stripped`) and reports
        whatever cannot be re-placed per order.
        """

        original_assignments = loading_plan.assignments
        kept_assignments: List[CompartmentAssignment] = []
        stripped: List[CompartmentAssignment] = []
        rejected_count = 0
        rejected_volume = 0.0

        for assignment in original_assignments:
            decision_info = self._evaluate_assignment_compatibility(
                assignment=assignment,
                compartment_states=compartment_states,
                compatibility_rules=compatibility_rules,
            )
            if decision_info is None:
                # Product could not be canonicalized — conservative
                # default is to block the assignment so an unknown
                # product never ends up in a loaded compartment. We
                # still emit a violation record so operators see the
                # drop.
                await self._record_cross_contamination_rejection(
                    assignment=assignment,
                    truck_id=truck_id,
                    tenant_id=tenant_id,
                    plan_id=loading_plan.plan_id,
                    run_id=run_id,
                    previous_product=self._previous_product_for(
                        assignment.compartment_id, compartment_states
                    ),
                    attempted_product=assignment.fuel_grade,
                    governing_rule="blocked",
                    decision="blocked",
                    reason=REASON_CROSS_CONTAMINATION_BLOCKED,
                    extra_context={"unknown_product_code": True},
                )
                stripped.append(assignment)
                rejected_count += 1
                rejected_volume += float(assignment.quantity_liters)
                continue

            decision = decision_info["decision"]
            if decision == DECISION_ALLOWED:
                kept_assignments.append(assignment)
                continue

            # Non-allowed — persist, publish, and drop the assignment.
            await self._record_cross_contamination_rejection(
                assignment=assignment,
                truck_id=truck_id,
                tenant_id=tenant_id,
                plan_id=loading_plan.plan_id,
                run_id=run_id,
                previous_product=decision_info["previous_product"],
                attempted_product=decision_info["attempted_product"],
                governing_rule=decision_info["governing_rule"],
                decision=decision,
                reason=decision_info["reason"] or REASON_CROSS_CONTAMINATION_BLOCKED,
            )
            stripped.append(assignment)
            rejected_count += 1
            rejected_volume += float(assignment.quantity_liters)

        if rejected_count == 0:
            return loading_plan, []

        logger.warning(
            "CompartmentLoadingAgent: stripped %d assignment(s) totalling "
            "%.0fL from plan %s (truck=%s, tenant=%s) due to "
            "cross-contamination rules",
            rejected_count,
            rejected_volume,
            loading_plan.plan_id,
            truck_id,
            tenant_id,
        )

        # The rejected volume is added to unserved_demand_liters so the
        # prioritization agent and dispatch KPIs see the blocked delivery as
        # unmet demand rather than silently disappearing; evaluate() takes
        # back whatever it re-places.
        return (
            self._rebuild_plan(
                loading_plan,
                kept_assignments,
                unserved_delta=rejected_volume,
                capacity_liters=sum(
                    a.compartment_capacity_liters for a in original_assignments
                ),
            ),
            stripped,
        )

    def _evaluate_assignment_compatibility(
        self,
        *,
        assignment: CompartmentAssignment,
        compartment_states: Mapping[str, CompartmentState],
        compatibility_rules: Mapping[Tuple[str, str], "RuleType"],
    ) -> Optional[Dict[str, Any]]:
        """Return the engine decision for an assignment, or None on unknown product.

        The decision dict carries ``decision`` / ``reason`` /
        ``governing_rule`` straight from
        :func:`check_compatibility`, plus the canonical ``previous_product``
        and ``attempted_product`` values so the rejection-record writer
        does not have to re-canonicalize.
        """

        try:
            attempted_product = canonicalize(assignment.fuel_grade)
        except (UnknownFuelProductError, TypeError) as exc:
            logger.error(
                "CompartmentLoadingAgent: unknown product_code %r on "
                "assignment for compartment %s; blocking: %s",
                assignment.fuel_grade,
                assignment.compartment_id,
                exc,
            )
            return None

        state = compartment_states.get(assignment.compartment_id)
        previous_product_raw = (
            state.last_loaded_product if state is not None else None
        )

        try:
            decision = check_compatibility(
                previous_product_raw,
                attempted_product,
                state,
                rules=compatibility_rules,
            )
        except UnknownFuelProductError as exc:
            # Only fires when previous_product is an unknown legacy value
            # — treat as a hard block so the bad value never slips past
            # the guard.
            logger.error(
                "CompartmentLoadingAgent: compartment %s last_loaded_product "
                "%r is not in the fuel catalog; blocking next load: %s",
                assignment.compartment_id,
                previous_product_raw,
                exc,
            )
            return {
                "decision": "blocked",
                "reason": REASON_CROSS_CONTAMINATION_BLOCKED,
                "governing_rule": "blocked",
                "previous_product": previous_product_raw,
                "attempted_product": attempted_product,
            }

        # ``previous_product`` on the violation record is canonical, so
        # pass through ``canonicalize_or_warn`` (tolerant of None) to
        # preserve legacy/unknown values without crashing the audit
        # write when they have already been accepted upstream.
        canonical_prev = (
            canonicalize_or_warn(
                previous_product_raw,
                context="cross_contamination_events.previous_product",
                logger_=logger,
            )
            if previous_product_raw
            else None
        )

        return {
            "decision": decision["decision"],
            "reason": decision["reason"],
            "governing_rule": decision["governing_rule"],
            "previous_product": canonical_prev,
            "attempted_product": attempted_product,
        }

    @staticmethod
    def _previous_product_for(
        compartment_id: str,
        compartment_states: Mapping[str, CompartmentState],
    ) -> Optional[str]:
        """Return the raw ``last_loaded_product`` for a compartment (or None)."""

        state = compartment_states.get(compartment_id)
        return state.last_loaded_product if state is not None else None

    async def _record_cross_contamination_rejection(
        self,
        *,
        assignment: CompartmentAssignment,
        truck_id: str,
        tenant_id: str,
        plan_id: str,
        run_id: str,
        previous_product: Optional[str],
        attempted_product: str,
        governing_rule: str,
        decision: str,
        reason: str,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a CrossContaminationViolation and publish a RiskSignal.

        The two side effects are each wrapped in a try/except so a
        transient failure (ES outage, signal bus blip) on one does not
        abort the other — the important safety invariant is that the
        Compartment_Loading_Agent keeps evaluating the remaining
        assignments and never commits a rejected one to
        ``mvp_load_plans``.
        """

        compartment_id = assignment.compartment_id
        compartment_doc_id = f"{truck_id}_{compartment_id}"
        event_id = f"ccv_{uuid4()}"
        now_iso = datetime.now(timezone.utc)

        # ---- Build + persist the violation record ----
        try:
            violation = CrossContaminationViolation(
                event_id=event_id,
                tenant_id=tenant_id,
                compartment_id=compartment_doc_id,
                truck_id=truck_id,
                previous_product=previous_product,
                attempted_product=attempted_product,
                governing_rule=governing_rule,  # type: ignore[arg-type]
                decision=decision,  # type: ignore[arg-type]
                reason=reason,  # type: ignore[arg-type]
                actor_id=self.agent_id,
                plan_id=plan_id,
                timestamp=now_iso,
                created_at=now_iso,
                updated_at=now_iso,
            )
        except Exception as exc:
            # The Pydantic validator rejected the payload (e.g. the
            # governing_rule was outside the literal set). Log a
            # structured error and continue — the SignalBus publish
            # below still fires so downstream overlays react.
            logger.error(
                "CompartmentLoadingAgent: failed to build "
                "CrossContaminationViolation for compartment %s (plan=%s, "
                "tenant=%s): %s",
                compartment_doc_id,
                plan_id,
                tenant_id,
                exc,
            )
            violation = None

        if violation is not None:
            try:
                await self._es.index_document(
                    CROSS_CONTAMINATION_EVENTS_INDEX,
                    violation.event_id,
                    violation.model_dump(mode="json", exclude_none=False),
                )
            except Exception as exc:
                # An ES write failure here must never abort the plan —
                # we log and keep going so the RiskSignal still fires.
                logger.error(
                    "CompartmentLoadingAgent: failed to persist "
                    "CrossContaminationViolation %s for compartment %s "
                    "(plan=%s, tenant=%s): %s",
                    event_id,
                    compartment_doc_id,
                    plan_id,
                    tenant_id,
                    exc,
                )

        # ---- Publish the RiskSignal ----
        context: Dict[str, Any] = {
            "event_id": event_id,
            "compartment_id": compartment_doc_id,
            "truck_id": truck_id,
            "previous_product": previous_product,
            "attempted_product": attempted_product,
            "decision": decision,
            "reason": reason,
            "governing_rule": governing_rule,
            "plan_id": plan_id,
            "run_id": run_id,
            "fuel_grade": assignment.fuel_grade,
            "station_id": assignment.station_id,
            "quantity_liters": assignment.quantity_liters,
        }
        if extra_context:
            context.update(extra_context)

        try:
            severity = (
                Severity.HIGH if decision == "blocked" else Severity.MEDIUM
            )
            signal = RiskSignal(
                source_agent=self.agent_id,
                entity_id=compartment_doc_id,
                entity_type=CROSS_CONTAMINATION_VIOLATION_ENTITY_TYPE,
                severity=severity,
                confidence=1.0,
                ttl_seconds=3600,
                tenant_id=tenant_id,
                context=context,
            )
            await self._signal_bus.publish(signal)
        except Exception as exc:
            logger.error(
                "CompartmentLoadingAgent: failed to publish "
                "cross_contamination_violation RiskSignal for compartment "
                "%s (plan=%s, tenant=%s): %s",
                compartment_doc_id,
                plan_id,
                tenant_id,
                exc,
            )

        logger.warning(
            "CompartmentLoadingAgent: rejected assignment for compartment %s "
            "(plan=%s, tenant=%s) — decision=%s reason=%s "
            "previous_product=%r attempted_product=%r",
            compartment_doc_id,
            plan_id,
            tenant_id,
            decision,
            reason,
            previous_product,
            attempted_product,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fuel_density_kg_per_liter(fuel_grade: str) -> float:
    """Density for a canonical product code or a legacy grade code.

    Thin wrapper over :func:`Agents.support.compartment_solver.fuel_density_kg_per_liter`.
    This module previously owned a second, near-identical density table. Only
    this one had the correct DEF value, and it was reached only when
    recomputing a plan's weight *after* assignments were stripped — the
    feasibility gate used the other table, so the more heavily filtered a plan
    was, the more accurate its weight became. There is now one table, in the
    solver, and the gate uses it.
    """
    return fuel_density_kg_per_liter(
        product_code=fuel_grade, fuel_grade=fuel_grade
    )
