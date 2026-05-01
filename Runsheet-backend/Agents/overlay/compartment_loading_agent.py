"""
Compartment Loading Agent — overlay agent for feasible multi-compartment loading plans.

Subscribes to DeliveryPriorityList messages from the SignalBus, queries
fuel trucks and compartments from the truck_compartments ES index, runs
feasibility checks and optimization using the compartment_solver, produces
InterventionProposals with loading plan actions, and persists plans to
mvp_load_plans.

Default configuration:
    - decision_cycle: 60 seconds
    - cooldown: 30 minutes per truck

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
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

logger = logging.getLogger(__name__)

# Elasticsearch indices consumed by this agent
FUEL_TRUCKS_INDEX = "fuel_trucks"

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

    # ------------------------------------------------------------------
    # Signal handling override — buffer DeliveryPriorityList messages
    # ------------------------------------------------------------------

    async def _on_signal(self, signal) -> None:
        """Buffer incoming signals. DeliveryPriorityLists are stored separately."""
        if isinstance(signal, DeliveryPriorityList):
            self._priority_buffer.append(signal)
        else:
            await super()._on_signal(signal)

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
        run_id = priority_list.run_id

        # Step 2: Build delivery requests from priorities (Req 3.1)
        delivery_requests = self._build_delivery_requests(priority_list)
        if not delivery_requests:
            return []

        # Step 3: Query available trucks and compartments (Req 3.1)
        trucks = await self._query_trucks(tenant_id)
        if not trucks:
            logger.info(
                "CompartmentLoadingAgent: no trucks found for tenant %s",
                tenant_id,
            )
            return []

        # Step 4: For each truck, check feasibility and optimize
        proposals: List[InterventionProposal] = []
        for truck_id, truck_data in trucks.items():
            compartments = truck_data["compartments"]
            max_weight_kg = truck_data.get("max_weight_kg")
            tare_weight_kg = truck_data.get("tare_weight_kg", 0.0)

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

            # Step 5: Persist loading plan to ES (Req 3.9)
            await self._persist_loading_plan(loading_plan)

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
    # Build delivery requests from priorities (Req 3.1)
    # ------------------------------------------------------------------

    def _build_delivery_requests(
        self, priority_list: DeliveryPriorityList
    ) -> List[DeliveryRequest]:
        """Convert priority list into delivery requests.

        Only includes priorities with CRITICAL or HIGH buckets.
        Assigns a default quantity based on priority score.
        """
        requests: List[DeliveryRequest] = []
        for priority in priority_list.priorities:
            if priority.priority_bucket not in (
                PriorityBucket.CRITICAL,
                PriorityBucket.HIGH,
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

                # Parse allowed_grades
                allowed_grades_raw = source.get("allowed_grades", [])
                allowed_grades = []
                for g in allowed_grades_raw:
                    try:
                        allowed_grades.append(FuelGrade(g))
                    except ValueError:
                        continue

                if not allowed_grades:
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
                    }
                trucks[truck_id]["compartments"].append(compartment)
        except Exception as e:
            logger.error(
                "CompartmentLoadingAgent: failed to query truck_compartments: %s",
                e,
            )

        return trucks

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

    async def _persist_loading_plan(self, loading_plan: LoadingPlan) -> None:
        """Persist a LoadingPlan to the mvp_load_plans ES index."""
        try:
            doc = loading_plan.model_dump(mode="json")
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
