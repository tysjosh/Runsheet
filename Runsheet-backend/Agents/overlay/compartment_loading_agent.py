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
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from uuid import uuid4

from Agents.overlay.base_overlay_agent import OverlayAgentBase
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
    LoadingPlan,
)
from Agents.support.compartment_solver import (
    check_feasibility,
    optimize_loading_plan,
)
from Agents.support.fuel_distribution_models import (
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
from fuel.services.contract_lift_service import ContractLiftService
from fuel.customer_tank_models import CustomerTankRepository
from fuel.services.fuel_ops_es_mappings import (
    CROSS_CONTAMINATION_EVENTS_INDEX,
    CUSTOMER_TANKS_INDEX,
)
from fuel.services.fuel_product_catalog import (
    UnknownFuelProductError,
    canonicalize,
    canonicalize_or_warn,
)
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

logger = logging.getLogger(__name__)

# Default minimum delivery quantity in liters (Req 3.5)
DEFAULT_MIN_DROP_LITERS = 500.0

# Default uncertainty buffer percentage (Req 3.6)
DEFAULT_UNCERTAINTY_BUFFER_PCT = 10.0


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
        is ``None`` (legacy path, test environments) the dyed-diesel
        check is skipped and all assignments pass through unchanged.
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
        2. Build delivery requests from priorities (Req 3.1).
        3. Query available fuel trucks and their compartments (Req 3.1).
        4. For each truck: run feasibility check (Req 3.3), then
           optimize loading plan (Req 3.4).
        5. Persist loading plans to mvp_load_plans (Req 3.9).
        6. Produce InterventionProposals with loading plan actions.

        Returns:
            List of InterventionProposals with loading plan actions.
        """
        # Step 1: Collect buffered priority lists
        priority_lists = list(self._priority_buffer)
        self._priority_buffer.clear()

        if not priority_lists:
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
        if not delivery_requests:
            return []

        # Step 3: Query available trucks with equipment check (Req 3.1, 3.2, 3.3)
        trucks = await self._query_trucks_with_equipment_check(tenant_id)
        if not trucks:
            logger.info(
                "CompartmentLoadingAgent: no trucks found for tenant %s",
                tenant_id,
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
        for truck_id, truck_data in trucks.items():
            compartments = truck_data["compartments"]
            max_weight_kg = truck_data.get("max_weight_kg")
            tare_weight_kg = truck_data.get("tare_weight_kg", 0.0)
            compartment_states: Dict[str, CompartmentState] = truck_data.get(
                "compartment_states", {}
            )

            # Check feasibility with weight constraints (Req 3.3, 3.7)
            feasibility = check_feasibility(
                compartments=compartments,
                requests=delivery_requests,
                max_weight_kg=max_weight_kg,
                tare_weight_kg=tare_weight_kg,
            )

            # Optimize loading plan (Req 3.4)
            loading_plan = optimize_loading_plan(
                compartments=compartments,
                requests=delivery_requests,
                truck_id=truck_id,
                tenant_id=tenant_id,
            )

            if loading_plan is None or not loading_plan.assignments:
                continue

            loading_plan.run_id = run_id

            # Task 6.5 / Req 7.2.2, 7.2.3, 7.2.6: before any assignment
            # is committed, verify each proposed compartment/product
            # pairing against the tenant compatibility matrix. Rejected
            # assignments are stripped from the plan, persisted as a
            # CrossContaminationViolation, and republished on the
            # SignalBus so downstream overlays can react.
            loading_plan = await self._enforce_cross_contamination(
                loading_plan=loading_plan,
                truck_id=truck_id,
                tenant_id=tenant_id,
                compartment_states=compartment_states,
                compatibility_rules=compatibility_rules,
                run_id=run_id,
            )

            if not loading_plan.assignments:
                # Every assignment was blocked — skip the plan to avoid
                # writing an empty Loading_Plan to mvp_load_plans.
                continue

            # Task 9.8 / Req 6.3, 6.4: before persisting the plan,
            # validate that any dyed-diesel assignments target
            # dyed-compatible compartments. Rejected assignments are
            # stripped from the plan and their volume is charged to
            # unserved_demand_liters.
            loading_plan = await self._enforce_dyed_diesel_compliance(
                loading_plan=loading_plan,
                tenant_id=tenant_id,
            )

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

        logger.info(
            "CompartmentLoadingAgent: produced %d loading plans for tenant %s "
            "(run_id=%s)",
            len(proposals),
            tenant_id,
            run_id,
        )

        return proposals

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
        """Build delivery requests by reading product_code and gallons_requested
        directly from each Fuel_Order in fuel_orders_current.

        Validates: Requirements 5.3.1, 5.3.2.

        For each priority entry in the DeliveryPriorityList, looks up the
        corresponding Fuel_Order to read:
          - ``product_code``: used directly as the fuel_grade for the
            DeliveryRequest (no FuelGrade.AGO/PMS/ATK/LPG fallback coercion).
          - ``gallons_requested``: converted to liters for the request.
          - ``fill_to_full``: when True, fetches the linked customer_tank
            and computes target_volume = max(0, capacity_gallons -
            current_level_gallons).

        Orders that have neither ``gallons_requested`` nor a resolvable
        tank level are failed with ``unresolved_fill_volume`` and excluded
        from the loading plan.

        Falls back to the legacy _build_delivery_requests path when no
        fuel orders can be resolved (e.g. during the deprecation window
        when orders are still in the legacy shipment shape).
        """
        # Query fuel orders for this tenant that are in loadable statuses
        fuel_orders = await self._query_fuel_orders(tenant_id)

        if not fuel_orders:
            # Fallback to legacy priority-list path during deprecation window
            logger.debug(
                "CompartmentLoadingAgent: no fuel_orders_current docs found "
                "for tenant %s; falling back to legacy priority-list path",
                tenant_id,
            )
            return self._build_delivery_requests(priority_list)

        # Build a lookup by station_id (customer_id) for matching priorities
        # to orders. The priority list's station_id corresponds to the order's
        # customer_id or station reference.
        priority_station_ids = {
            p.station_id
            for p in priority_list.priorities
            if p.priority_bucket in (
                PriorityBucket.CRITICAL,
                PriorityBucket.HIGH,
                PriorityBucket.MEDIUM,
            )
        }

        requests: List[DeliveryRequest] = []
        # US gallons to liters conversion factor (NIST)
        GALLONS_TO_LITERS = 3.785411784

        for order in fuel_orders:
            order_id = order.get("order_id", "")
            station_id = order.get("customer_id", "")
            product_code = order.get("product_code")
            gallons_requested = order.get("gallons_requested")
            fill_to_full = order.get("fill_to_full", False)
            customer_tank_id = order.get("customer_tank_id")

            # Skip orders whose station/customer is not in the priority set
            if priority_station_ids and station_id not in priority_station_ids:
                continue

            # Resolve product_code — read directly, no FuelGrade enum coercion
            if not product_code:
                logger.warning(
                    "CompartmentLoadingAgent: order %s has no product_code; "
                    "skipping",
                    order_id,
                )
                continue

            # Map canonical product_code to FuelGrade for the solver.
            # This reads product_code directly from the order — no legacy
            # FuelGrade.AGO/PMS/ATK/LPG fallback coercion on the intake path.
            fuel_grade = self._resolve_fuel_grade_from_product_code(product_code)
            if fuel_grade is None:
                logger.warning(
                    "CompartmentLoadingAgent: order %s has unrecognized "
                    "product_code %r; skipping",
                    order_id,
                    product_code,
                )
                continue

            # Resolve volume
            quantity_liters: Optional[float] = None

            if fill_to_full and customer_tank_id:
                # Fetch the linked customer_tank and compute target_volume
                target_volume = await self._resolve_fill_to_full_volume(
                    tenant_id=tenant_id,
                    customer_tank_id=customer_tank_id,
                    order_id=order_id,
                )
                if target_volume is not None:
                    quantity_liters = target_volume * GALLONS_TO_LITERS
                elif gallons_requested is not None and gallons_requested > 0:
                    # Tank resolution failed but gallons_requested is available
                    quantity_liters = gallons_requested * GALLONS_TO_LITERS
                else:
                    # Neither resolvable tank level nor gallons_requested
                    logger.error(
                        "CompartmentLoadingAgent: unresolved_fill_volume for "
                        "order %s (fill_to_full=true, customer_tank_id=%s) — "
                        "neither gallons_requested nor resolvable tank level "
                        "available",
                        order_id,
                        customer_tank_id,
                    )
                    await self._fail_order_loading(
                        order_id=order_id,
                        tenant_id=tenant_id,
                        reason="unresolved_fill_volume",
                        details={
                            "customer_tank_id": customer_tank_id,
                            "fill_to_full": True,
                        },
                    )
                    continue
            elif fill_to_full and not customer_tank_id:
                # fill_to_full but no linked tank — use gallons_requested
                # if available, otherwise fail
                if gallons_requested is not None and gallons_requested > 0:
                    quantity_liters = gallons_requested * GALLONS_TO_LITERS
                else:
                    logger.error(
                        "CompartmentLoadingAgent: unresolved_fill_volume for "
                        "order %s (fill_to_full=true, no customer_tank_id) — "
                        "gallons_requested not available",
                        order_id,
                    )
                    await self._fail_order_loading(
                        order_id=order_id,
                        tenant_id=tenant_id,
                        reason="unresolved_fill_volume",
                        details={
                            "customer_tank_id": None,
                            "fill_to_full": True,
                        },
                    )
                    continue
            elif gallons_requested is not None and gallons_requested > 0:
                quantity_liters = gallons_requested * GALLONS_TO_LITERS
            else:
                # No volume information available at all
                logger.error(
                    "CompartmentLoadingAgent: unresolved_fill_volume for "
                    "order %s — neither gallons_requested nor fill_to_full "
                    "with resolvable tank",
                    order_id,
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

            # Use product_code directly — no FuelGrade enum coercion
            requests.append(
                DeliveryRequest(
                    station_id=station_id,
                    order_id=order_id,
                    fuel_grade=fuel_grade,
                    quantity_liters=round(quantity_liters, 2),
                    min_drop_liters=DEFAULT_MIN_DROP_LITERS,
                )
            )

        if not requests:
            # If no orders could be resolved, fall back to legacy path
            logger.debug(
                "CompartmentLoadingAgent: no delivery requests built from "
                "fuel_orders_current for tenant %s; falling back to legacy "
                "priority-list path",
                tenant_id,
            )
            return self._build_delivery_requests(priority_list)

        return requests

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
                        {"terms": {"status": ["placed", "confirmed", "scheduled"]}},
                    ],
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
                in_filters={"status": ["placed", "confirmed", "scheduled"]},
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

    # ------------------------------------------------------------------
    # Fill-to-full volume resolution (Task 11.3 / Req 5.3.2)
    # ------------------------------------------------------------------

    async def _resolve_fill_to_full_volume(
        self,
        *,
        tenant_id: str,
        customer_tank_id: str,
        order_id: str,
    ) -> Optional[float]:
        """Fetch the linked customer_tank and compute target_volume.

        Returns:
            target_volume in gallons = max(0, capacity_gallons - current_level_gallons),
            or None if the tank cannot be resolved.
        """
        try:
            tank = await self._customer_tank_repo.get(
                tenant_id=tenant_id,
                customer_tank_id=customer_tank_id,
            )
        except Exception as exc:
            logger.warning(
                "CompartmentLoadingAgent: failed to fetch customer_tank %s "
                "for order %s (tenant=%s): %s",
                customer_tank_id,
                order_id,
                tenant_id,
                exc,
            )
            return None

        if tank is None:
            logger.warning(
                "CompartmentLoadingAgent: customer_tank %s not found for "
                "order %s (tenant=%s)",
                customer_tank_id,
                order_id,
                tenant_id,
            )
            return None

        capacity = tank.capacity_gallons
        current_level = tank.current_level_gallons
        target_volume = max(0.0, capacity - current_level)

        if target_volume <= 0:
            logger.info(
                "CompartmentLoadingAgent: customer_tank %s is already full "
                "(capacity=%.1f, level=%.1f) for order %s",
                customer_tank_id,
                capacity,
                current_level,
                order_id,
            )
            return None

        return target_volume

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
    # Build delivery requests from Fuel_Orders (Task 11.3)
    # ------------------------------------------------------------------

    async def build_delivery_requests_from_fuel_orders(
        self, tenant_id: str, orders: List[Dict[str, Any]]
    ) -> Tuple[List[DeliveryRequest], List[Dict[str, Any]]]:
        """Build delivery requests directly from Fuel_Order documents.

        Reads ``product_code`` and ``gallons_requested`` directly from
        each order. For ``fill_to_full = true`` orders, fetches the linked
        ``customer_tank`` and computes
        ``target_volume = max(0, capacity_gallons - current_level_gallons)``.

        Fails the loading with ``unresolved_fill_volume`` when neither
        ``gallons_requested`` nor a resolvable tank level is available.

        Args:
            tenant_id: Tenant scope.
            orders: List of Fuel_Order source documents.

        Returns:
            (requests, failures) where failures is a list of dicts with
            order_id and reason for orders that could not be loaded.
        """
        requests: List[DeliveryRequest] = []
        failures: List[Dict[str, Any]] = []

        # Batch-fetch customer tanks for fill_to_full orders
        tank_ids_needed = [
            o["customer_tank_id"]
            for o in orders
            if o.get("fill_to_full") and o.get("customer_tank_id")
        ]
        customer_tanks: Dict[str, Dict[str, Any]] = {}
        if tank_ids_needed:
            customer_tanks = await self._fetch_customer_tanks_for_loading(
                tenant_id, tank_ids_needed
            )

        for order in orders:
            order_id = order.get("order_id", "unknown")
            product_code = order.get("product_code")
            gallons_requested = order.get("gallons_requested")
            fill_to_full = order.get("fill_to_full", False)
            customer_tank_id = order.get("customer_tank_id")

            # Resolve fuel grade from product_code directly — no legacy
            # FuelGrade.AGO/PMS/ATK/LPG fallback coercion on the intake path
            if not product_code:
                failures.append({
                    "order_id": order_id,
                    "reason": "missing_product_code",
                })
                continue

            fuel_grade = self._resolve_fuel_grade_from_product_code(product_code)
            if fuel_grade is None:
                failures.append({
                    "order_id": order_id,
                    "reason": "unknown_product_code",
                    "product_code": product_code,
                })
                continue

            # Determine volume
            target_gallons: Optional[float] = None

            if fill_to_full:
                # Compute target_volume from linked customer_tank
                if customer_tank_id and customer_tank_id in customer_tanks:
                    tank = customer_tanks[customer_tank_id]
                    capacity = tank.get("capacity_gallons")
                    current_level = tank.get("current_level_gallons")
                    if capacity is not None and current_level is not None:
                        try:
                            target_gallons = max(
                                0.0,
                                float(capacity) - float(current_level),
                            )
                        except (TypeError, ValueError):
                            target_gallons = None

                # Fall back to gallons_requested if tank level unavailable
                if target_gallons is None and gallons_requested:
                    try:
                        target_gallons = float(gallons_requested)
                    except (TypeError, ValueError):
                        target_gallons = None

                if target_gallons is None:
                    failures.append({
                        "order_id": order_id,
                        "reason": "unresolved_fill_volume",
                        "detail": (
                            "fill_to_full=true but neither gallons_requested "
                            "nor a resolvable tank level is available"
                        ),
                    })
                    continue
            else:
                # Use gallons_requested directly
                if gallons_requested:
                    try:
                        target_gallons = float(gallons_requested)
                    except (TypeError, ValueError):
                        target_gallons = None

                if target_gallons is None or target_gallons <= 0:
                    failures.append({
                        "order_id": order_id,
                        "reason": "unresolved_fill_volume",
                        "detail": "no valid gallons_requested",
                    })
                    continue

            # Convert gallons to liters (1 gallon ≈ 3.785 liters)
            quantity_liters = round(target_gallons * 3.785411784, 2)

            requests.append(
                DeliveryRequest(
                    station_id=customer_tank_id or order_id,
                    order_id=order_id,
                    fuel_grade=fuel_grade,
                    quantity_liters=quantity_liters,
                    min_drop_liters=DEFAULT_MIN_DROP_LITERS,
                )
            )

        return requests, failures

    async def _fetch_customer_tanks_for_loading(
        self, tenant_id: str, tank_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch customer tank docs for fill_to_full volume computation."""
        if not tank_ids:
            return {}

        unique_ids = list(set(tank_ids))
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"tank_id": unique_ids}},
                    ]
                }
            },
            "size": len(unique_ids),
        }
        try:
            resp = await self._es.search_documents(
                CUSTOMER_TANKS_INDEX, query, len(unique_ids)
            )
            hits = (resp or {}).get("hits", {}).get("hits", [])
            result: Dict[str, Dict[str, Any]] = {}
            for hit in hits:
                source = hit.get("_source", {})
                tid = source.get("tank_id")
                if tid:
                    result[tid] = source
            return result
        except Exception as exc:
            logger.warning(
                "CompartmentLoadingAgent: customer_tanks fetch failed "
                "for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return {}

    def _resolve_fuel_grade_from_product_code(
        self, product_code: str
    ) -> Optional[FuelGrade]:
        """Map a product_code to a FuelGrade enum value using the mapping service.

        Supports both US market product codes (DIESEL_2, GASOLINE_REG, etc.)
        and FuelGrade enum values (AGO, PMS, ATK, LPG).
        """
        if not product_code:
            return None

        # Try direct FuelGrade enum parsing first
        try:
            return FuelGrade(product_code)
        except ValueError:
            pass

        # Use the mapping service for US product codes
        from fuel.services.fuel_product_mapping import fuel_product_mapper
        return fuel_product_mapper.us_to_fuel_grade(product_code)

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

                # Parse allowed_grades with product code mapping support
                allowed_grades_raw = source.get("allowed_grades", [])
                allowed_grades = []
                for g in allowed_grades_raw:
                    try:
                        # Try direct FuelGrade enum parsing first
                        allowed_grades.append(FuelGrade(g))
                    except ValueError:
                        # Try mapping from US product code
                        from fuel.services.fuel_product_mapping import fuel_product_mapper
                        mapped_grade = fuel_product_mapper.us_to_fuel_grade(g)
                        if mapped_grade:
                            allowed_grades.append(mapped_grade)
                        else:
                            logger.debug(
                                "CompartmentLoadingAgent: unrecognized fuel grade '%s' for truck %s compartment %s",
                                g, truck_id, source.get("compartment_id")
                            )

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
        """Build an InterventionProposal from a loading plan."""
        actions = [
            {
                "tool_name": "apply_loading_plan",
                "parameters": {
                    "plan_id": loading_plan.plan_id,
                    "truck_id": loading_plan.truck_id,
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

        # Convert liters to canonical gallons for the counter. Using the
        # exact NIST factor (1 gal = 3.785411784 L) keeps the counter
        # stable across runs even when source units mix (the agent-side
        # LoadingPlan is liters-valued, but the Redis counter and
        # Supplier_Contract.minimum_lift_gallons_per_month are both in
        # gallons).
        gallons = total_liters / 3.785411784

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
    ) -> LoadingPlan:
        """Reject assignments that load dyed diesel into clear-only compartments.

        Validates: Requirements 6.3, 6.4.

        For each assignment in ``loading_plan``, if the product is a
        dyed-diesel code, calls
        :meth:`DyedDieselEnforcer.validate_load_plan` to verify the
        compartment is dyed-compatible. If validation fails (error code
        ``dyed.compartment_incompatible`` or ``dyed.compartment_not_found``),
        the assignment is stripped from the plan and its volume is charged
        to ``unserved_demand_liters``.

        When no enforcer is configured (``_dyed_diesel_enforcer is None``),
        all assignments pass through unchanged (graceful degradation).

        Returns the (possibly filtered) LoadingPlan.
        """
        enforcer = getattr(self, "_dyed_diesel_enforcer", None)
        if enforcer is None:
            return loading_plan

        # Import here to avoid circular dependency at module level
        from compliance.services.dyed_diesel_enforcer import DyedDieselEnforcer

        if not isinstance(enforcer, DyedDieselEnforcer):
            return loading_plan

        original_assignments = loading_plan.assignments
        kept_assignments: List[CompartmentAssignment] = []
        rejected_volume = 0.0

        for assignment in original_assignments:
            product_code = assignment.fuel_grade

            # Only check dyed-diesel products
            if not enforcer.is_dyed_diesel(product_code):
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
                # Fail-open: if the enforcer raises, allow the assignment
                # so a transient ES issue does not block all dyed-diesel
                # loads. The enforcer itself logs the error.
                logger.warning(
                    "CompartmentLoadingAgent: dyed diesel validation failed "
                    "for compartment %s (plan=%s, tenant=%s): %s — "
                    "allowing assignment (fail-open)",
                    assignment.compartment_id,
                    loading_plan.plan_id,
                    tenant_id,
                    exc,
                )
                kept_assignments.append(assignment)
                continue

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
                rejected_volume += float(assignment.quantity_liters)

        if not rejected_volume:
            return loading_plan

        # Recompute plan metrics with the filtered assignments
        retained_volume = sum(a.quantity_liters for a in kept_assignments)
        total_capacity = sum(
            a.compartment_capacity_liters for a in original_assignments
        )
        new_utilization = (
            round((retained_volume / total_capacity) * 100, 2)
            if total_capacity > 0
            else 0.0
        )
        new_weight = round(
            sum(
                _fuel_density_kg_per_liter(a.fuel_grade) * a.quantity_liters
                for a in kept_assignments
            ),
            2,
        )
        new_unserved = round(
            float(loading_plan.unserved_demand_liters) + rejected_volume, 2
        )

        logger.warning(
            "CompartmentLoadingAgent: stripped %d dyed-diesel assignment(s) "
            "totalling %.0fL from plan %s (tenant=%s) due to "
            "compartment incompatibility (Req 6.3/6.4)",
            len(original_assignments) - len(kept_assignments),
            rejected_volume,
            loading_plan.plan_id,
            tenant_id,
        )

        return loading_plan.model_copy(
            update={
                "assignments": kept_assignments,
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
    ) -> LoadingPlan:
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

        The returned :class:`LoadingPlan` is either the original plan
        (when nothing was rejected) or a fresh :meth:`model_copy` with
        the filtered assignment list and recomputed totals so the
        downstream ``_persist_loading_plan`` / ``_build_proposal`` calls
        never see a rejected assignment.
        """

        original_assignments = loading_plan.assignments
        kept_assignments: List[CompartmentAssignment] = []
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
            rejected_count += 1
            rejected_volume += float(assignment.quantity_liters)

        if rejected_count == 0:
            return loading_plan

        # Build the filtered plan. Utilization and weight are recomputed
        # against the retained assignments so mvp_load_plans and the
        # intervention proposal reflect post-rejection reality. The
        # rejected volume is added to unserved_demand_liters so the
        # prioritization agent and dispatch KPIs see the blocked
        # delivery as unmet demand rather than silently disappearing.
        retained_volume = sum(a.quantity_liters for a in kept_assignments)
        total_capacity = sum(
            a.compartment_capacity_liters for a in original_assignments
        )
        # Recompute utilization from retained volume / original capacity
        # so the metric stays proportional to the truck's total tank
        # space (matching how optimize_loading_plan computes it).
        new_utilization = (
            round((retained_volume / total_capacity) * 100, 2)
            if total_capacity > 0
            else 0.0
        )
        # Fuel density table mirrors compartment_solver.FUEL_DENSITY but
        # keyed on canonical product codes as well so the weight total
        # is correct for mixed-catalog assignments.
        new_weight = round(
            sum(
                _fuel_density_kg_per_liter(a.fuel_grade) * a.quantity_liters
                for a in kept_assignments
            ),
            2,
        )
        new_unserved = round(
            float(loading_plan.unserved_demand_liters) + rejected_volume, 2
        )

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

        return loading_plan.model_copy(
            update={
                "assignments": kept_assignments,
                "total_utilization_pct": new_utilization,
                "total_weight_kg": new_weight,
                "unserved_demand_liters": new_unserved,
            }
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


_FUEL_DENSITY_CANONICAL: Dict[str, float] = {
    # Canonical US product densities (kg/L). Matches the gasoline/diesel
    # values used by compartment_solver.FUEL_DENSITY under the legacy NG
    # codes so plans evaluated before and after Task 6.5 report the same
    # total_weight_kg for the same assignments.
    "DIESEL_2": 0.85,
    "OFF_ROAD_DIESEL": 0.85,
    "HEATING_OIL": 0.85,
    "GASOLINE_REG": 0.74,
    "GASOLINE_PREM": 0.74,
    "ETHANOL_E85": 0.78,
    "KEROSENE": 0.80,
    "PROPANE": 0.51,
    "DEF": 1.09,
    # Legacy NG aliases carried forward so pre-canonicalization plans
    # still compute a sensible weight even if the assignment slipped
    # through without canonicalization.
    "AGO": 0.85,
    "PMS": 0.74,
    "ATK": 0.80,
    "LPG": 0.51,
}


def _fuel_density_kg_per_liter(fuel_grade: str) -> float:
    """Return the fuel density for a canonical or legacy code.

    Matches :data:`Agents.support.compartment_solver.FUEL_DENSITY` for
    the legacy NG codes and extends coverage to every catalog product in
    :data:`fuel.services.fuel_product_catalog.FUEL_PRODUCT_CATALOG`. An
    unknown code falls back to 0.85 kg/L (the diesel default used by
    ``compartment_solver``) so weight totals degrade gracefully.
    """

    if not isinstance(fuel_grade, str):
        return 0.85
    return _FUEL_DENSITY_CANONICAL.get(fuel_grade.strip().upper(), 0.85)
