# Technical Design Document — Truck Compartment Loading Agent

## 1. Overview

This design describes a Layer 1 overlay agent that produces multi-compartment tanker loading plans for fuel delivery operations. The agent models truck compartments with grade constraints, enforces absolute fuel grade segregation, runs feasibility checks, and produces optimized compartment-to-delivery assignments that maximize truck utilization.

The agent integrates with the existing overlay architecture: it extends `OverlayAgentBase`, subscribes to `RiskSignal` messages from `FuelManagementAgent` via the `SignalBus`, and produces `InterventionProposal` messages routed through the `ConfirmationProtocol`. It starts in shadow mode by default.

### Design Principles

1. **Composition over modification** — The Loading Agent reads fuel station signals and truck/compartment data without modifying existing agent interfaces.
2. **Shadow-first safety** — Starts in shadow mode; loading plans are logged but not executed until explicitly activated per tenant.
3. **Greedy-optimal solver** — Uses a greedy largest-compartment-first algorithm for loading plan optimization. This is sufficient for the typical problem size (≤6 compartments, ≤10 deliveries) and meets the 500ms performance target.
4. **Strict grade segregation** — Grade mixing is treated as a hard constraint, never relaxed.

### Key Design Decisions

| Decision | Rationale |
|---|---|
| Greedy solver over ILP/constraint programming | Problem size is small (≤6 compartments × ≤10 deliveries). Greedy largest-first produces near-optimal results and is deterministic within the 500ms budget. |
| Dedicated `truck_compartments` ES index | Compartment data is structural (rarely changes), separate from the dynamic `trucks` index. Avoids schema pollution of the existing asset model. |
| Pydantic v2 models for LoadingPlan/CompartmentAssignment | Consistent with existing data contracts pattern. Provides validation, serialization, and round-trip guarantees. |
| FuelGrade enum shared across models | Single source of truth for supported fuel grades (AGO, PMS, ATK, LPG). |

## 2. Architecture

### 2.1 Signal Flow

```
FuelManagementAgent (L0)
    │ RiskSignal (source_agent="fuel_management_agent")
    ▼
SignalBus
    │
    ▼
TruckCompartmentLoadingAgent (L1)
    │ 1. Query fuel_stations for delivery requirements
    │ 2. Query trucks + truck_compartments for available capacity
    │ 3. Run feasibility check per truck
    │ 4. Produce optimized LoadingPlan for best-fit truck
    │ 5. Emit InterventionProposal
    ▼
ConfirmationProtocol → ExecutionPlanner (active mode)
    or
agent_shadow_proposals ES index (shadow mode)
```

### 2.2 Package Structure

```
Runsheet-backend/Agents/overlay/
├── ... (existing overlay modules)
├── compartment_models.py        # FuelGrade, Compartment, DeliveryRequest,
│                                 # CompartmentAssignment, LoadingPlan,
│                                 # FeasibilityResult
├── compartment_solver.py        # check_feasibility(), optimize_loading_plan()
├── compartment_es_mappings.py   # TRUCK_COMPARTMENTS_MAPPING, setup function
└── truck_compartment_loading_agent.py  # TruckCompartmentLoadingAgent
```

## 3. Components and Interfaces

### 3.1 Data Models (`Agents/overlay/compartment_models.py`)

```python
"""
Data models for the Truck Compartment Loading Agent.

Defines FuelGrade enum, Compartment, DeliveryRequest,
CompartmentAssignment, LoadingPlan, and FeasibilityResult.

Validates: Requirements 1, 2, 9
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class FuelGrade(str, Enum):
    AGO = "AGO"
    PMS = "PMS"
    ATK = "ATK"
    LPG = "LPG"


class Compartment(BaseModel):
    """A physical compartment on a fuel truck.

    Validates: Requirement 1.1, 1.2, 1.3
    """
    compartment_id: str
    truck_id: str
    capacity_liters: float = Field(gt=0)
    allowed_grades: List[FuelGrade] = Field(min_length=1)
    position_index: int = Field(ge=0)
    tenant_id: str

    @field_validator("allowed_grades")
    @classmethod
    def validate_allowed_grades(cls, v: List[FuelGrade]) -> List[FuelGrade]:
        if not v:
            raise ValueError("allowed_grades must not be empty")
        return v


class DeliveryRequest(BaseModel):
    """A demand for fuel at a specific station.

    Validates: Requirement 3.1
    """
    station_id: str
    fuel_grade: FuelGrade
    quantity_liters: float = Field(gt=0)


class CompartmentAssignment(BaseModel):
    """A single compartment-to-delivery assignment within a LoadingPlan.

    Validates: Requirement 9.2
    """
    compartment_id: str
    station_id: str
    fuel_grade: str
    quantity_liters: float = Field(gt=0)
    compartment_capacity_liters: float = Field(gt=0)


class LoadingPlan(BaseModel):
    """A complete loading plan for a single truck trip.

    Validates: Requirements 4.1, 9.1, 9.3, 9.4
    """
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    truck_id: str
    assignments: List[CompartmentAssignment]
    total_utilization_pct: float = Field(ge=0.0, le=100.0)
    tenant_id: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: str = "proposed"  # proposed | approved | executed | rejected

    @model_validator(mode="after")
    def validate_utilization(self) -> "LoadingPlan":
        """Validate total_utilization_pct matches assignments."""
        if not self.assignments:
            return self
        total_loaded = sum(a.quantity_liters for a in self.assignments)
        total_capacity = sum(
            a.compartment_capacity_liters for a in self.assignments
        )
        if total_capacity > 0:
            expected = round((total_loaded / total_capacity) * 100, 2)
            if abs(self.total_utilization_pct - expected) > 0.1:
                raise ValueError(
                    f"total_utilization_pct {self.total_utilization_pct} "
                    f"does not match computed {expected}"
                )
        return self


class ConstraintViolation(BaseModel):
    """Describes a single constraint violation in a feasibility check."""
    violation_type: str  # "capacity_shortfall" | "no_compatible_compartments" | "total_overage"
    fuel_grade: Optional[str] = None
    shortfall_liters: Optional[float] = None
    message: str


class FeasibilityResult(BaseModel):
    """Result of a multi-compartment feasibility check.

    Validates: Requirements 3.2, 3.3, 3.4
    """
    feasible: bool
    max_utilization_pct: float = 0.0
    violations: List[ConstraintViolation] = Field(default_factory=list)
```

### 3.2 Compartment Solver (`Agents/overlay/compartment_solver.py`)

The solver contains two pure functions: `check_feasibility()` and `optimize_loading_plan()`. No side effects, no ES queries — just constraint logic operating on model objects.

```python
"""
Compartment solver — feasibility checking and loading plan optimization.

Pure functions operating on Compartment and DeliveryRequest models.
No side effects, no ES queries.

Validates: Requirements 2, 3, 4, 5
"""
from typing import Dict, List, Optional, Tuple

from Agents.overlay.compartment_models import (
    Compartment,
    CompartmentAssignment,
    ConstraintViolation,
    DeliveryRequest,
    FeasibilityResult,
    FuelGrade,
    LoadingPlan,
)


def check_feasibility(
    compartments: List[Compartment],
    requests: List[DeliveryRequest],
) -> FeasibilityResult:
    """Check whether a truck can fulfill all delivery requests.

    Validates:
    - Req 2.1-2.4: Grade segregation (one grade per compartment)
    - Req 3.1-3.4: Feasibility with capacity and grade constraints
    - Req 5.1-5.3: Constraint violation reporting

    Args:
        compartments: The truck's compartments with capacity and grade info.
        requests: The delivery requests to fulfill.

    Returns:
        FeasibilityResult with feasible flag, utilization, and violations.
    """
    violations: List[ConstraintViolation] = []

    # Check total capacity
    total_capacity = sum(c.capacity_liters for c in compartments)
    total_requested = sum(r.quantity_liters for r in requests)
    if total_requested > total_capacity:
        violations.append(ConstraintViolation(
            violation_type="total_overage",
            shortfall_liters=total_requested - total_capacity,
            message=(
                f"Total requested {total_requested}L exceeds "
                f"truck capacity {total_capacity}L"
            ),
        ))

    # Check per-grade capacity
    grade_demands: Dict[FuelGrade, float] = {}
    for req in requests:
        grade_demands[req.fuel_grade] = (
            grade_demands.get(req.fuel_grade, 0) + req.quantity_liters
        )

    for grade, demand in grade_demands.items():
        compatible = [
            c for c in compartments if grade in c.allowed_grades
        ]
        if not compatible:
            violations.append(ConstraintViolation(
                violation_type="no_compatible_compartments",
                fuel_grade=grade.value,
                message=(
                    f"No compartments support grade {grade.value}"
                ),
            ))
            continue

        compatible_capacity = sum(c.capacity_liters for c in compatible)
        if demand > compatible_capacity:
            violations.append(ConstraintViolation(
                violation_type="capacity_shortfall",
                fuel_grade=grade.value,
                shortfall_liters=demand - compatible_capacity,
                message=(
                    f"Grade {grade.value} needs {demand}L but only "
                    f"{compatible_capacity}L of compatible capacity available"
                ),
            ))

    if violations:
        return FeasibilityResult(
            feasible=False,
            max_utilization_pct=0.0,
            violations=violations,
        )

    # Feasible — compute max utilization
    utilization = round((total_requested / total_capacity) * 100, 2) if total_capacity > 0 else 0.0
    return FeasibilityResult(
        feasible=True,
        max_utilization_pct=utilization,
    )


def optimize_loading_plan(
    compartments: List[Compartment],
    requests: List[DeliveryRequest],
    truck_id: str,
    tenant_id: str,
) -> Optional[LoadingPlan]:
    """Produce an optimized loading plan using greedy largest-first.

    Algorithm:
    1. Group requests by fuel grade.
    2. For each grade, sort compatible compartments by capacity descending.
    3. Assign demand to compartments greedily (largest first).
    4. If a compartment can't hold the full remaining demand, fill it
       and continue to the next compatible compartment.

    Validates:
    - Req 4.1-4.5: Optimization with largest-first, splitting, utilization
    - Req 2.1-2.4: Grade segregation enforced during assignment

    Args:
        compartments: The truck's compartments.
        requests: The delivery requests (must be feasible).
        truck_id: The truck identifier.
        tenant_id: The tenant identifier.

    Returns:
        LoadingPlan if successful, None if infeasible.
    """
    # Group requests by grade
    grade_demands: Dict[FuelGrade, List[DeliveryRequest]] = {}
    for req in requests:
        grade_demands.setdefault(req.fuel_grade, []).append(req)

    # Track compartment usage: compartment_id -> (grade, remaining_capacity)
    compartment_state: Dict[str, Tuple[Optional[FuelGrade], float]] = {
        c.compartment_id: (None, c.capacity_liters) for c in compartments
    }
    compartment_map = {c.compartment_id: c for c in compartments}

    assignments: List[CompartmentAssignment] = []

    for grade, reqs in grade_demands.items():
        # Get compatible compartments sorted by capacity descending (Req 4.2)
        compatible = sorted(
            [c for c in compartments if grade in c.allowed_grades],
            key=lambda c: c.capacity_liters,
            reverse=True,
        )

        for req in reqs:
            remaining = req.quantity_liters

            for comp in compatible:
                cid = comp.compartment_id
                assigned_grade, cap_remaining = compartment_state[cid]

                # Skip if compartment already assigned to a different grade
                if assigned_grade is not None and assigned_grade != grade:
                    continue

                if cap_remaining <= 0:
                    continue

                # Assign as much as possible to this compartment
                assign_qty = min(remaining, cap_remaining)
                if assign_qty <= 0:
                    continue

                assignments.append(CompartmentAssignment(
                    compartment_id=cid,
                    station_id=req.station_id,
                    fuel_grade=grade.value,
                    quantity_liters=assign_qty,
                    compartment_capacity_liters=comp.capacity_liters,
                ))

                compartment_state[cid] = (grade, cap_remaining - assign_qty)
                remaining -= assign_qty

                if remaining <= 0:
                    break

            if remaining > 0:
                # Infeasible — shouldn't happen if check_feasibility passed
                return None

    # Compute utilization
    total_loaded = sum(a.quantity_liters for a in assignments)
    total_capacity = sum(c.capacity_liters for c in compartments)
    utilization = round((total_loaded / total_capacity) * 100, 2) if total_capacity > 0 else 0.0

    return LoadingPlan(
        truck_id=truck_id,
        assignments=assignments,
        total_utilization_pct=utilization,
        tenant_id=tenant_id,
    )
```

### 3.3 ES Index Mappings (`Agents/overlay/compartment_es_mappings.py`)

```python
"""
Elasticsearch index mappings for the truck_compartments index.

Validates: Requirement 8
"""
import logging

logger = logging.getLogger(__name__)

TRUCK_COMPARTMENTS_INDEX = "truck_compartments"

TRUCK_COMPARTMENTS_MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "compartment_id": {"type": "keyword"},
            "truck_id":       {"type": "keyword"},
            "capacity_liters": {"type": "float"},
            "allowed_grades": {"type": "keyword"},
            "position_index": {"type": "integer"},
            "tenant_id":      {"type": "keyword"},
            "created_at":     {"type": "date"},
            "updated_at":     {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}


def setup_compartment_index(es_service) -> None:
    """Create the truck_compartments index if it doesn't exist.

    Follows the same pattern as setup_overlay_indices.
    """
    from services.elasticsearch_service import ElasticsearchService

    es_client = es_service.client
    is_serverless = es_service.is_serverless

    try:
        if not es_client.indices.exists(index=TRUCK_COMPARTMENTS_INDEX):
            mapping = TRUCK_COMPARTMENTS_MAPPING
            if is_serverless:
                mapping = ElasticsearchService.strip_serverless_incompatible_settings(mapping)
            es_client.indices.create(
                index=TRUCK_COMPARTMENTS_INDEX, body=mapping
            )
            logger.info(f"Created index: {TRUCK_COMPARTMENTS_INDEX}")
        else:
            logger.info(f"Index already exists: {TRUCK_COMPARTMENTS_INDEX}")
    except Exception as e:
        logger.error(
            f"Failed to create index {TRUCK_COMPARTMENTS_INDEX}: {e}"
        )
```

### 3.4 TruckCompartmentLoadingAgent (`Agents/overlay/truck_compartment_loading_agent.py`)

```python
"""
Truck Compartment Loading Agent — Layer 1 overlay agent.

Subscribes to fuel RiskSignals, queries truck compartments,
runs feasibility checks, and produces optimized loading plans
as InterventionProposals.

Decision cycle: 60 seconds (configurable).
Cooldown: 30 minutes per truck.

Validates: Requirements 6, 7
"""
import logging
from typing import Any, Dict, List

from Agents.overlay.base_overlay_agent import OverlayAgentBase
from Agents.overlay.compartment_models import (
    Compartment,
    DeliveryRequest,
    FuelGrade,
)
from Agents.overlay.compartment_solver import (
    check_feasibility,
    optimize_loading_plan,
)
from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
)
from Agents.overlay.signal_bus import SignalBus

logger = logging.getLogger(__name__)

FUEL_STATIONS_INDEX = "fuel_stations"
TRUCKS_INDEX = "trucks"
TRUCK_COMPARTMENTS_INDEX = "truck_compartments"


class TruckCompartmentLoadingAgent(OverlayAgentBase):
    """Multi-compartment tanker loading plan optimizer.

    Consumes fuel RiskSignals, queries available fuel trucks and
    their compartments, runs feasibility checks, and produces
    optimized loading plans as InterventionProposals.

    Args:
        signal_bus: SignalBus for pub/sub.
        es_service: Elasticsearch service.
        activity_log_service: For logging agent activity.
        ws_manager: WebSocket manager.
        confirmation_protocol: For routing proposals.
        autonomy_config_service: For mode management.
        feature_flag_service: For per-tenant feature flags.
        poll_interval: Decision cycle interval (default 60).
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
    ):
        super().__init__(
            agent_id="truck_compartment_loading",
            signal_bus=signal_bus,
            subscriptions=[
                {
                    "message_type": RiskSignal,
                    "filters": {
                        "source_agent": "fuel_management_agent",
                    },
                },
            ],
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            autonomy_config_service=autonomy_config_service,
            feature_flag_service=feature_flag_service,
            es_service=es_service,
            poll_interval=poll_interval,
            cooldown_minutes=30,
        )

    async def evaluate(
        self, signals: List[RiskSignal]
    ) -> List[InterventionProposal]:
        """Evaluate fuel signals and produce loading plans.

        Steps:
        1. Extract station IDs from signals.
        2. Query fuel_stations for delivery requirements.
        3. Query available fuel trucks and their compartments.
        4. For each truck, run feasibility check.
        5. For the best-fit truck, produce optimized loading plan.
        6. Return InterventionProposal with the plan.
        """
        if not signals:
            return []

        tenant_id = signals[0].tenant_id
        station_ids = list({s.entity_id for s in signals})

        # Build delivery requests from station data
        delivery_requests = await self._build_delivery_requests(
            station_ids, tenant_id
        )
        if not delivery_requests:
            return []

        # Query available fuel trucks
        trucks = await self._query_fuel_trucks(tenant_id)
        if not trucks:
            return []

        # Find best truck with feasible loading plan
        best_plan = None
        best_utilization = -1.0

        for truck in trucks:
            truck_id = truck.get("asset_id") or truck.get("id")
            if not truck_id:
                continue

            if self._is_on_cooldown(truck_id):
                continue

            compartments = await self._query_compartments(
                truck_id, tenant_id
            )
            if not compartments:
                continue

            # Feasibility check
            result = check_feasibility(compartments, delivery_requests)
            if not result.feasible:
                continue

            # Optimize
            plan = optimize_loading_plan(
                compartments, delivery_requests, truck_id, tenant_id
            )
            if plan and plan.total_utilization_pct > best_utilization:
                best_plan = plan
                best_utilization = plan.total_utilization_pct

        if not best_plan:
            return []

        # Build InterventionProposal
        actions = []
        for assignment in best_plan.assignments:
            actions.append({
                "tool_name": "execute_loading_plan",
                "parameters": {
                    "plan_id": best_plan.plan_id,
                    "truck_id": best_plan.truck_id,
                    "compartment_id": assignment.compartment_id,
                    "station_id": assignment.station_id,
                    "fuel_grade": assignment.fuel_grade,
                    "quantity_liters": assignment.quantity_liters,
                },
            })

        # Count unique stations served
        stations_served = len({a.station_id for a in best_plan.assignments})
        total_loaded = sum(a.quantity_liters for a in best_plan.assignments)
        total_capacity = sum(
            a.compartment_capacity_liters for a in best_plan.assignments
        )
        waste = total_capacity - total_loaded

        proposal = InterventionProposal(
            source_agent=self.agent_id,
            actions=actions,
            expected_kpi_delta={
                "compartment_utilization_pct": best_plan.total_utilization_pct,
                "deliveries_consolidated": float(stations_served),
                "estimated_fuel_waste_liters": waste,
            },
            risk_class=RiskClass.MEDIUM,
            confidence=min(s.confidence for s in signals),
            priority=stations_served,
            tenant_id=tenant_id,
        )

        self._set_cooldown(best_plan.truck_id)
        return [proposal]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def _build_delivery_requests(
        self, station_ids: List[str], tenant_id: str
    ) -> List[DeliveryRequest]:
        """Query fuel stations and build delivery requests."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"terms": {"station_id": station_ids}},
                        {"terms": {"status": ["critical", "low"]}},
                    ]
                }
            },
            "size": len(station_ids),
        }
        resp = await self._es.search_documents(
            FUEL_STATIONS_INDEX, query, len(station_ids)
        )
        stations = [h["_source"] for h in resp["hits"]["hits"]]

        requests = []
        for station in stations:
            capacity = station.get("capacity_liters", 0)
            current = station.get("current_stock_liters", 0)
            # Refill to 80% capacity
            needed = (capacity * 0.8) - current
            if needed <= 0:
                continue

            fuel_type = station.get("fuel_type", "AGO")
            try:
                grade = FuelGrade(fuel_type)
            except ValueError:
                continue

            requests.append(DeliveryRequest(
                station_id=station.get("station_id", ""),
                fuel_grade=grade,
                quantity_liters=needed,
            ))

        return requests

    async def _query_fuel_trucks(
        self, tenant_id: str
    ) -> List[Dict[str, Any]]:
        """Query available fuel trucks for the tenant."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"asset_subtype": "fuel_truck"}},
                        {"terms": {"status": ["active", "on_time"]}},
                    ]
                }
            },
            "size": 20,
        }
        resp = await self._es.search_documents(TRUCKS_INDEX, query, 20)
        return [h["_source"] for h in resp["hits"]["hits"]]

    async def _query_compartments(
        self, truck_id: str, tenant_id: str
    ) -> List[Compartment]:
        """Query compartments for a truck, ordered by position_index."""
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"truck_id": truck_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "sort": [{"position_index": {"order": "asc"}}],
            "size": 20,
        }
        resp = await self._es.search_documents(
            TRUCK_COMPARTMENTS_INDEX, query, 20
        )
        hits = [h["_source"] for h in resp["hits"]["hits"]]

        compartments = []
        for hit in hits:
            try:
                grades = [
                    FuelGrade(g) for g in hit.get("allowed_grades", [])
                ]
                compartments.append(Compartment(
                    compartment_id=hit["compartment_id"],
                    truck_id=hit["truck_id"],
                    capacity_liters=hit["capacity_liters"],
                    allowed_grades=grades,
                    position_index=hit.get("position_index", 0),
                    tenant_id=hit["tenant_id"],
                ))
            except (ValueError, KeyError) as e:
                logger.warning(
                    "Skipping invalid compartment %s: %s",
                    hit.get("compartment_id"), e,
                )

        return compartments
```

## 4. Correctness Properties

### Property 1: Loading Plan JSON Round-Trip

*For any* valid LoadingPlan instance, serializing to JSON via `model_dump(mode="json")` then deserializing via `model_validate` SHALL produce an equal object.

**Validates: Requirement 9.3**

### Property 2: Grade Segregation Invariant

*For any* LoadingPlan produced by `optimize_loading_plan`, no two assignments in the plan SHALL assign different fuel grades to the same compartment_id.

**Validates: Requirements 2.1, 2.4**

### Property 3: Capacity Constraint

*For any* LoadingPlan produced by `optimize_loading_plan`, the sum of `quantity_liters` assigned to any single compartment SHALL NOT exceed that compartment's `capacity_liters`.

**Validates: Requirement 4.4**

### Property 4: Demand Fulfillment

*For any* feasible set of DeliveryRequests, the LoadingPlan produced by `optimize_loading_plan` SHALL assign quantities that sum to exactly the requested `quantity_liters` for each delivery.

**Validates: Requirement 4.4**

### Property 5: Feasibility Consistency

*For any* set of compartments and delivery requests, if `check_feasibility` returns `feasible=True`, then `optimize_loading_plan` SHALL return a non-None LoadingPlan. If `check_feasibility` returns `feasible=False`, the violations list SHALL be non-empty.

**Validates: Requirements 3.2, 3.3, 3.4**

### Property 6: Grade Compatibility

*For any* CompartmentAssignment in a LoadingPlan, the assigned `fuel_grade` SHALL be present in the corresponding Compartment's `allowed_grades` list.

**Validates: Requirements 2.2, 2.3**

### Property 7: Utilization Accuracy

*For any* LoadingPlan, `total_utilization_pct` SHALL equal `(sum of all assignment quantity_liters / sum of all truck compartment capacity_liters) × 100`, rounded to 2 decimal places.

**Validates: Requirement 9.4**

## 5. Error Handling

| Error Scenario | Handling Strategy |
|---|---|
| No fuel trucks available | `evaluate()` returns empty list. No proposal generated. |
| No compartments for a truck | Skip truck, try next available truck. |
| Invalid compartment data in ES | Log warning, skip invalid compartment, continue with valid ones. |
| ES query failure | Caught in agent. Logged. Empty result returned. Retried next cycle. |
| Infeasible delivery set for all trucks | `evaluate()` returns empty list. Logged for shadow analysis. |
| Station not found in fuel_stations | Skip station. Delivery request not created for missing stations. |

## 6. Bootstrap Integration

The `TruckCompartmentLoadingAgent` is registered in `bootstrap/agents.py` alongside the other overlay agents:

```python
from Agents.overlay.truck_compartment_loading_agent import TruckCompartmentLoadingAgent
from Agents.overlay.compartment_es_mappings import setup_compartment_index

# After setup_overlay_indices(es_service):
setup_compartment_index(es_service)

truck_compartment_loading = TruckCompartmentLoadingAgent(**overlay_common_args)
scheduler.register(truck_compartment_loading, RestartPolicy.ON_FAILURE)

app.state.overlay_agents["truck_compartment_loading"] = truck_compartment_loading
```

Feature flag key: `overlay.truck_compartment_loading`
