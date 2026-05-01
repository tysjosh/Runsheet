# Technical Design Document — Fuel Distribution MVP

## 1. Overview

This design describes a five-agent fuel distribution pipeline that automates end-to-end fuel delivery planning. The agents execute in a sequential pipeline (Forecast → Prioritize → Load → Route) with a continuous exception replanning monitor. All agents extend `OverlayAgentBase`, communicate via the `SignalBus`, and follow the existing overlay architecture patterns.

### Package Structure

```
Runsheet-backend/Agents/
├── overlay/
│   ├── ...                                # Existing overlay agents
│   ├── tank_forecasting_agent.py          # Agent 1
│   ├── delivery_prioritization_agent.py   # Agent 2
│   ├── compartment_loading_agent.py       # Agent 3
│   ├── route_planning_agent.py            # Agent 4
│   └── exception_replanning_agent.py      # Agent 5
├── support/
│   ├── __init__.py
│   ├── mvp_data_contracts.py              # TankForecast, DeliveryPriority, shared models
│   ├── mvp_es_mappings.py                 # All 6 MVP ES index mappings + setup
│   ├── compartment_models.py              # Compartment, LoadingPlan, solver models
│   ├── compartment_solver.py              # check_feasibility(), optimize_loading_plan()
│   ├── route_solver.py                    # nearest_neighbor_2opt(), travel time helpers
│   ├── fuel_distribution_pipeline.py      # Pipeline coordinator
│   └── mvp_endpoints.py                   # REST API endpoints
```

### Key Design Decisions

| Decision | Rationale |
|---|---|
| Sequential pipeline with SignalBus handoffs | Each agent publishes output to SignalBus; next agent subscribes. Decoupled, observable, replayable. |
| Greedy solver for loading (Agent 3) | Problem size ≤6 compartments × ≤10 deliveries. Greedy largest-first is fast and deterministic. |
| Nearest-neighbor + 2-opt for routing (Agent 4) | Sufficient for ≤15 stops within 2s. No external solver dependency. |
| Agents in `overlay/`, support in `support/` | Agents extend OverlayAgentBase so they belong with other overlay agents. Solvers, data contracts, ES mappings, and pipeline coordinator are support infrastructure in a dedicated `support/` directory. |
| Redis-backed scoring weights | Per-tenant configurable without code deployment. Consistent with existing FeatureFlagService pattern. |

## 2. Architecture

### 2.1 Pipeline Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    Pipeline Coordinator                          │
│                    (run_id, tenant_id)                           │
│                                                                  │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│  │ A1: Tank  │──▶│ A2: Prio │──▶│ A3: Load │──▶│ A4: Route│    │
│  │ Forecast  │   │ Ranking  │   │ Planning │   │ Planning │    │
│  └──────────┘   └──────────┘   └──────────┘   └──────────┘    │
│       │              │              │              │             │
│       ▼              ▼              ▼              ▼             │
│  mvp_tank_     mvp_delivery_  mvp_load_     mvp_routes         │
│  forecasts     priorities     plans                             │
└─────────────────────────────────────────────────────────────────┘
                                                    │
                    ┌──────────────────────────────┐│
                    │ A5: Exception Replanning      ││
                    │ (continuous 30s monitor)       │◀── disruption signals
                    └──────────────────────────────┘
                              │
                         mvp_replan_events
```

### 2.2 Signal Flow

- **A1** subscribes to: RiskSignals from `fuel_management_agent`
- **A1** publishes: `TankForecast` messages to SignalBus
- **A2** subscribes to: `TankForecast` messages
- **A2** publishes: `DeliveryPriorityList` messages to SignalBus
- **A3** subscribes to: `DeliveryPriorityList` messages
- **A3** publishes: `InterventionProposal` with loading plan actions
- **A4** subscribes to: `InterventionProposal` from `compartment_loading_agent`
- **A4** publishes: `InterventionProposal` with route plan actions
- **A5** subscribes to: RiskSignals from `delay_response_agent`, `sla_guardian_agent`, `exception_commander`

## 3. Components

### 3.1 MVP Data Contracts (`Agents/support/mvp_data_contracts.py`)

```python
"""
Shared data contracts for the Fuel Distribution MVP pipeline.

Defines TankForecast, DeliveryPriority, DeliveryPriorityList,
RoutePlan, RouteStop, and ReplanEvent models.

Validates: Requirements 1.1, 2.1, 4.1, 5.2
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class FuelGrade(str, Enum):
    AGO = "AGO"
    PMS = "PMS"
    ATK = "ATK"
    LPG = "LPG"


class PriorityBucket(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TankForecast(BaseModel):
    """Probabilistic runout forecast for a (station, grade) pair.
    Validates: Requirement 1.1
    """
    forecast_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    station_id: str
    fuel_grade: FuelGrade
    hours_to_runout_p50: float = Field(ge=0.0)
    hours_to_runout_p90: float = Field(ge=0.0)
    runout_risk_24h: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    feature_version: str = "v1.0"
    anomaly_flags: List[str] = Field(default_factory=list)
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class DeliveryPriority(BaseModel):
    """Scored priority for a single station/grade.
    Validates: Requirement 2.1
    """
    station_id: str
    fuel_grade: FuelGrade
    priority_score: float = Field(ge=0.0, le=1.0)
    priority_bucket: PriorityBucket
    reasons: List[str] = Field(default_factory=list)


class DeliveryPriorityList(BaseModel):
    """Ranked list of delivery priorities for a pipeline run.
    Validates: Requirement 2.1
    """
    priority_list_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    priorities: List[DeliveryPriority]
    scoring_weights: Dict[str, float] = Field(default_factory=dict)
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class RouteStop(BaseModel):
    """A single stop in a delivery route."""
    station_id: str
    eta: str  # ISO 8601
    drop: Dict[str, float]  # grade -> liters
    sequence: int = Field(ge=0)


class RoutePlan(BaseModel):
    """An optimized delivery route.
    Validates: Requirement 4.1
    """
    route_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    truck_id: str
    plan_id: str  # References the loading plan
    stops: List[RouteStop]
    distance_km: float = Field(ge=0.0)
    eta_confidence: float = Field(ge=0.0, le=1.0)
    objective_value: float = 0.0
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: str = "proposed"


class ReplanDiff(BaseModel):
    """Describes changes made during replanning."""
    stops_reordered: List[str] = Field(default_factory=list)
    volumes_reallocated: Dict[str, float] = Field(default_factory=dict)
    truck_swapped: Optional[str] = None
    stations_deferred: List[str] = Field(default_factory=list)
    stations_added: List[str] = Field(default_factory=list)


class ReplanEvent(BaseModel):
    """A plan modification triggered by a disruption.
    Validates: Requirement 5.2
    """
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    original_plan_id: str
    patched_plan_id: Optional[str] = None
    trigger_signal_id: str
    replan_type: str  # truck_swap | station_outage | demand_spike | delay
    diff: ReplanDiff = Field(default_factory=ReplanDiff)
    status: str = "applied"  # applied | failed | escalated
    tenant_id: str
    run_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
```

### 3.2 Compartment Models (`Agents/support/compartment_models.py`)

Reuses the same models from the truck-compartment-loading spec with additions for min delivery quantity, uncertainty buffer, and weight constraints:

```python
"""
Compartment data models for the Loading Agent.

Extends base compartment models with min_drop_liters, uncertainty_buffer_pct,
and max_weight_kg constraints.

Validates: Requirements 3.1–3.10
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from Agents.support.mvp_data_contracts import FuelGrade


class Compartment(BaseModel):
    compartment_id: str
    truck_id: str
    capacity_liters: float = Field(gt=0)
    allowed_grades: List[FuelGrade] = Field(min_length=1)
    position_index: int = Field(ge=0)
    tenant_id: str


class DeliveryRequest(BaseModel):
    station_id: str
    fuel_grade: FuelGrade
    quantity_liters: float = Field(gt=0)
    min_drop_liters: float = Field(default=500.0, ge=0)


class CompartmentAssignment(BaseModel):
    compartment_id: str
    station_id: str
    fuel_grade: str
    quantity_liters: float = Field(gt=0)
    compartment_capacity_liters: float = Field(gt=0)


class LoadingPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    truck_id: str
    assignments: List[CompartmentAssignment]
    total_utilization_pct: float = Field(ge=0.0, le=100.0)
    unserved_demand_liters: float = Field(default=0.0, ge=0.0)
    total_weight_kg: float = Field(default=0.0, ge=0.0)
    tenant_id: str
    run_id: str = ""
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: str = "proposed"


class ConstraintViolation(BaseModel):
    violation_type: str
    fuel_grade: Optional[str] = None
    shortfall_liters: Optional[float] = None
    message: str


class FeasibilityResult(BaseModel):
    feasible: bool
    max_utilization_pct: float = 0.0
    violations: List[ConstraintViolation] = Field(default_factory=list)
```

### 3.3 Compartment Solver (`Agents/support/compartment_solver.py`)

Same greedy algorithm as the truck-compartment-loading spec, extended with:
- **Min drop validation**: Reject assignments below `min_drop_liters`
- **Uncertainty buffer**: Inflate demand by `uncertainty_buffer_pct` (default 10%)
- **Weight constraint**: Validate total weight against `max_weight_kg` using fuel density lookup

```python
"""
Compartment solver — feasibility and optimization.

Pure functions. No side effects.

Validates: Requirements 2, 3, 4, 5 of the Loading Agent
"""
from typing import Dict, List, Optional, Tuple

from Agents.support.compartment_models import (
    Compartment, CompartmentAssignment, ConstraintViolation,
    DeliveryRequest, FeasibilityResult, FuelGrade, LoadingPlan,
)

# Fuel density in kg/liter (approximate)
FUEL_DENSITY: Dict[str, float] = {
    "AGO": 0.85,
    "PMS": 0.74,
    "ATK": 0.80,
    "LPG": 0.51,
}

DEFAULT_UNCERTAINTY_BUFFER_PCT = 10.0


def check_feasibility(
    compartments: List[Compartment],
    requests: List[DeliveryRequest],
    max_weight_kg: Optional[float] = None,
    tare_weight_kg: float = 0.0,
    uncertainty_buffer_pct: float = DEFAULT_UNCERTAINTY_BUFFER_PCT,
) -> FeasibilityResult:
    """Check feasibility with grade, capacity, min-drop, and weight constraints."""
    violations = []
    buffer_mult = 1.0 + (uncertainty_buffer_pct / 100.0)

    # Apply uncertainty buffer to demands
    buffered_requests = []
    for req in requests:
        buffered_qty = req.quantity_liters * buffer_mult
        buffered_requests.append(req.model_copy(
            update={"quantity_liters": buffered_qty}
        ))

    total_capacity = sum(c.capacity_liters for c in compartments)
    total_requested = sum(r.quantity_liters for r in buffered_requests)

    # Total capacity check
    if total_requested > total_capacity:
        violations.append(ConstraintViolation(
            violation_type="total_overage",
            shortfall_liters=total_requested - total_capacity,
            message=f"Total requested {total_requested:.0f}L exceeds capacity {total_capacity:.0f}L",
        ))

    # Per-grade capacity check
    grade_demands: Dict[FuelGrade, float] = {}
    for req in buffered_requests:
        grade_demands[req.fuel_grade] = grade_demands.get(req.fuel_grade, 0) + req.quantity_liters

    for grade, demand in grade_demands.items():
        compatible = [c for c in compartments if grade in c.allowed_grades]
        if not compatible:
            violations.append(ConstraintViolation(
                violation_type="no_compatible_compartments",
                fuel_grade=grade.value,
                message=f"No compartments support grade {grade.value}",
            ))
            continue
        cap = sum(c.capacity_liters for c in compatible)
        if demand > cap:
            violations.append(ConstraintViolation(
                violation_type="capacity_shortfall",
                fuel_grade=grade.value,
                shortfall_liters=demand - cap,
                message=f"Grade {grade.value} needs {demand:.0f}L, only {cap:.0f}L available",
            ))

    # Weight check
    if max_weight_kg is not None and not violations:
        total_weight = tare_weight_kg
        for req in buffered_requests:
            density = FUEL_DENSITY.get(req.fuel_grade.value, 0.85)
            total_weight += req.quantity_liters * density
        if total_weight > max_weight_kg:
            violations.append(ConstraintViolation(
                violation_type="weight_exceeded",
                shortfall_liters=0,
                message=f"Total weight {total_weight:.0f}kg exceeds limit {max_weight_kg:.0f}kg",
            ))

    # Min drop check
    for req in requests:
        if req.quantity_liters < req.min_drop_liters:
            violations.append(ConstraintViolation(
                violation_type="below_min_drop",
                fuel_grade=req.fuel_grade.value,
                shortfall_liters=req.min_drop_liters - req.quantity_liters,
                message=f"Station {req.station_id} requests {req.quantity_liters:.0f}L, below min {req.min_drop_liters:.0f}L",
            ))

    if violations:
        return FeasibilityResult(feasible=False, violations=violations)

    utilization = round((total_requested / total_capacity) * 100, 2) if total_capacity > 0 else 0.0
    return FeasibilityResult(feasible=True, max_utilization_pct=utilization)


def optimize_loading_plan(
    compartments: List[Compartment],
    requests: List[DeliveryRequest],
    truck_id: str,
    tenant_id: str,
    uncertainty_buffer_pct: float = DEFAULT_UNCERTAINTY_BUFFER_PCT,
) -> Optional[LoadingPlan]:
    """Greedy largest-first loading plan with uncertainty buffer."""
    buffer_mult = 1.0 + (uncertainty_buffer_pct / 100.0)

    grade_demands: Dict[FuelGrade, List[DeliveryRequest]] = {}
    for req in requests:
        grade_demands.setdefault(req.fuel_grade, []).append(req)

    compartment_state = {c.compartment_id: (None, c.capacity_liters) for c in compartments}
    compartment_map = {c.compartment_id: c for c in compartments}
    assignments = []

    for grade, reqs in grade_demands.items():
        compatible = sorted(
            [c for c in compartments if grade in c.allowed_grades],
            key=lambda c: c.capacity_liters, reverse=True,
        )
        for req in reqs:
            remaining = req.quantity_liters * buffer_mult
            for comp in compatible:
                cid = comp.compartment_id
                assigned_grade, cap_remaining = compartment_state[cid]
                if assigned_grade is not None and assigned_grade != grade:
                    continue
                if cap_remaining <= 0:
                    continue
                assign_qty = min(remaining, cap_remaining)
                if assign_qty <= 0:
                    continue
                assignments.append(CompartmentAssignment(
                    compartment_id=cid,
                    station_id=req.station_id,
                    fuel_grade=grade.value,
                    quantity_liters=round(assign_qty, 2),
                    compartment_capacity_liters=comp.capacity_liters,
                ))
                compartment_state[cid] = (grade, cap_remaining - assign_qty)
                remaining -= assign_qty
                if remaining <= 0:
                    break
            # remaining > 0 means partial fulfillment — tracked as unserved

    total_loaded = sum(a.quantity_liters for a in assignments)
    total_capacity = sum(c.capacity_liters for c in compartments)
    total_requested = sum(r.quantity_liters * buffer_mult for r in requests)
    unserved = max(0, total_requested - total_loaded)
    utilization = round((total_loaded / total_capacity) * 100, 2) if total_capacity > 0 else 0.0

    # Compute weight
    total_weight = 0.0
    for a in assignments:
        density = FUEL_DENSITY.get(a.fuel_grade, 0.85)
        total_weight += a.quantity_liters * density

    return LoadingPlan(
        truck_id=truck_id,
        assignments=assignments,
        total_utilization_pct=utilization,
        unserved_demand_liters=round(unserved, 2),
        total_weight_kg=round(total_weight, 2),
        tenant_id=tenant_id,
    )
```

### 3.4 Route Solver (`Agents/support/route_solver.py`)

```python
"""
Route solver — nearest-neighbor + 2-opt improvement.

Pure functions. No side effects.

Validates: Requirement 4.5
"""
from typing import Dict, List, Optional, Tuple
import math


def compute_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km between two coordinates."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_distance_matrix(
    locations: List[Dict[str, float]],
) -> List[List[float]]:
    """Build NxN distance matrix from list of {lat, lon} dicts."""
    n = len(locations)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = compute_distance(
                locations[i]["lat"], locations[i]["lon"],
                locations[j]["lat"], locations[j]["lon"],
            )
            matrix[i][j] = d
            matrix[j][i] = d
    return matrix


def nearest_neighbor_route(
    distance_matrix: List[List[float]],
    start_index: int = 0,
) -> Tuple[List[int], float]:
    """Nearest-neighbor heuristic for TSP. Returns (order, total_distance)."""
    n = len(distance_matrix)
    visited = [False] * n
    order = [start_index]
    visited[start_index] = True
    total = 0.0

    current = start_index
    for _ in range(n - 1):
        best_next = -1
        best_dist = float("inf")
        for j in range(n):
            if not visited[j] and distance_matrix[current][j] < best_dist:
                best_dist = distance_matrix[current][j]
                best_next = j
        if best_next == -1:
            break
        visited[best_next] = True
        order.append(best_next)
        total += best_dist
        current = best_next

    return order, total


def two_opt_improve(
    order: List[int],
    distance_matrix: List[List[float]],
    max_iterations: int = 100,
) -> Tuple[List[int], float]:
    """2-opt local search improvement on a route."""
    def route_distance(route):
        return sum(distance_matrix[route[i]][route[i+1]] for i in range(len(route)-1))

    best = list(order)
    best_dist = route_distance(best)
    improved = True
    iterations = 0

    while improved and iterations < max_iterations:
        improved = False
        iterations += 1
        for i in range(1, len(best) - 1):
            for j in range(i + 1, len(best)):
                new_route = best[:i] + best[i:j+1][::-1] + best[j+1:]
                new_dist = route_distance(new_route)
                if new_dist < best_dist - 0.01:
                    best = new_route
                    best_dist = new_dist
                    improved = True

    return best, best_dist


def optimize_route(
    locations: List[Dict[str, float]],
    start_index: int = 0,
) -> Tuple[List[int], float]:
    """Full route optimization: nearest-neighbor + 2-opt."""
    matrix = build_distance_matrix(locations)
    order, _ = nearest_neighbor_route(matrix, start_index)
    order, total_dist = two_opt_improve(order, matrix)
    return order, total_dist
```

### 3.5 MVP ES Mappings (`Agents/support/mvp_es_mappings.py`)

Defines all 6 MVP indices plus `truck_compartments` and the `setup_mvp_indices()` function. Follows the same pattern as `setup_overlay_indices`.

Index names: `mvp_tank_forecasts`, `mvp_delivery_priorities`, `mvp_load_plans`, `mvp_routes`, `mvp_replan_events`, `mvp_plan_outcomes`, `truck_compartments`.

### 3.6 Agent Implementations

Each agent follows the same pattern:
1. Extends `OverlayAgentBase`
2. Subscribes to upstream signals
3. Implements `evaluate()` with domain logic
4. Publishes output to SignalBus
5. Persists to its ES index

### 3.7 Pipeline Coordinator (`Agents/support/fuel_distribution_pipeline.py`)

The coordinator is NOT an overlay agent — it's a service that triggers pipeline runs:

```python
class FuelDistributionPipeline:
    """Orchestrates the A1→A2→A3→A4 pipeline sequence.

    Assigns run_id, triggers each agent in order, tracks state,
    broadcasts progress via WebSocket.
    """
    async def run(self, tenant_id: str) -> str:
        """Execute a full pipeline run. Returns run_id."""
        ...

    async def get_status(self, run_id: str) -> dict:
        """Get pipeline run status."""
        ...
```

### 3.8 REST Endpoints (`Agents/support/mvp_endpoints.py`)

- `POST /api/fuel/mvp/plan/generate` → triggers `pipeline.run(tenant_id)`
- `GET /api/fuel/mvp/plan/{plan_id}` → queries `mvp_load_plans` + `mvp_routes`
- `POST /api/fuel/mvp/plan/{plan_id}/replan` → triggers exception replanning
- `GET /api/fuel/mvp/forecasts` → queries `mvp_tank_forecasts`
- `GET /api/fuel/mvp/priorities` → queries `mvp_delivery_priorities`

### 3.9 WebSocket Events

Uses existing `AgentActivityWSManager.broadcast_event()`:
- `forecast_ready` — after A1 completes
- `priority_ready` — after A2 completes
- `loadplan_ready` — after A3 completes
- `route_ready` — after A4 completes
- `replan_applied` / `replan_failed` — after A5 acts

## 4. Correctness Properties

### Property 1: Grade Segregation Invariant
For any LoadingPlan, no two assignments assign different fuel grades to the same compartment.

### Property 2: Capacity Constraint
For any LoadingPlan, the sum of quantity_liters per compartment does not exceed capacity_liters.

### Property 3: Demand Fulfillment
For feasible requests, assigned quantities (minus buffer) sum to at least the original requested amount per delivery.

### Property 4: Feasibility Consistency
If check_feasibility returns feasible=True, optimize_loading_plan returns non-None.

### Property 5: Priority Score Bounds
For any DeliveryPriority, priority_score is in [0.0, 1.0] and priority_bucket matches the score threshold.

### Property 6: Route Improvement
The 2-opt improved route distance is ≤ the nearest-neighbor route distance.

### Property 7: Forecast Confidence Bounds
For any TankForecast, confidence is in [0.0, 1.0] and runout_risk_24h is in [0.0, 1.0].

### Property 8: Pipeline Run Traceability
For any pipeline run, all outputs (forecasts, priorities, plans, routes) share the same run_id.

## 5. Error Handling

| Scenario | Strategy |
|---|---|
| No fuel stations in critical state | A1 produces empty forecasts. Pipeline completes with no plans. |
| No available fuel trucks | A3 returns empty. Pipeline logs and completes. |
| Infeasible loading for all trucks | A3 returns empty. A4 skipped. Logged for analysis. |
| Route solver timeout | Return nearest-neighbor result without 2-opt improvement. |
| A5 cannot find feasible replan | Escalate with HIGH-severity RiskSignal. Flag plan as escalation_required. |
| ES query failure in any agent | Caught, logged, empty result. Retried next cycle. |
| Pipeline agent failure | Circuit breaker halts pipeline. Retried next scheduled cycle. |
