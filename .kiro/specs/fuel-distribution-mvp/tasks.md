# Implementation Plan: Fuel Distribution MVP

## Overview

This plan implements the five-agent fuel distribution pipeline in phases: shared infrastructure first (data contracts, ES mappings), then agents bottom-up (A1→A2→A3→A4→A5), then the pipeline coordinator, then API/WS integration, and finally bootstrap wiring.

## Tasks

- [x] 1. Foundation — Data Contracts and ES Mappings
  - [x] 1.1 Create support package structure and `__init__.py`
    - Create `Runsheet-backend/Agents/support/` directory
    - Create `Runsheet-backend/Agents/support/__init__.py` with placeholder exports
    - _Requirements: 11.1_

  - [x] 1.2 Implement shared data contracts in `Agents/support/mvp_data_contracts.py`
    - Implement FuelGrade enum (AGO, PMS, ATK, LPG)
    - Implement PriorityBucket enum (critical, high, medium, low)
    - Implement TankForecast model with all fields from Req 1.1
    - Implement DeliveryPriority and DeliveryPriorityList models from Req 2.1
    - Implement RoutePlan, RouteStop models from Req 4.1
    - Implement ReplanDiff and ReplanEvent models from Req 5.2
    - _Requirements: 1.1, 2.1, 4.1, 5.2_

  - [x] 1.3 Implement compartment models in `Agents/support/compartment_models.py`
    - Implement Compartment, DeliveryRequest (with min_drop_liters), CompartmentAssignment, LoadingPlan (with unserved_demand_liters, total_weight_kg), ConstraintViolation, FeasibilityResult
    - _Requirements: 3.1, 3.5, 3.7, 3.8, 3.10_

  - [x] 1.4 Implement MVP ES mappings in `Agents/support/mvp_es_mappings.py`
    - Define mappings for: mvp_tank_forecasts, mvp_delivery_priorities, mvp_load_plans, mvp_routes, mvp_replan_events, mvp_plan_outcomes, truck_compartments
    - Implement `setup_mvp_indices(es_service)` function
    - _Requirements: 7.1–7.9_

- [x] 2. Checkpoint — Verify foundation
  - Ensure all models import correctly and ES mappings are valid.

- [x] 3. Solvers — Compartment and Route
  - [x] 3.1 Implement compartment solver in `Agents/support/compartment_solver.py`
    - Implement `check_feasibility()` with grade, capacity, weight, and min-drop constraints plus uncertainty buffer
    - Implement `optimize_loading_plan()` with greedy largest-first algorithm, uncertainty buffer, weight tracking, and unserved demand reporting
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [ ]* 3.2 Write property tests for grade segregation invariant
    - **Property 1** — No two assignments in any plan assign different grades to the same compartment
    - **Validates: Requirement 3.2**

  - [ ]* 3.3 Write property tests for capacity constraint
    - **Property 2** — Sum of quantity_liters per compartment ≤ capacity_liters
    - **Validates: Requirement 3.4**

  - [ ]* 3.4 Write property tests for feasibility consistency
    - **Property 4** — feasible=True implies optimize returns non-None; feasible=False implies violations non-empty
    - **Validates: Requirement 3.3**

  - [x] 3.5 Implement route solver in `Agents/support/route_solver.py`
    - Implement `compute_distance()` (Haversine)
    - Implement `build_distance_matrix()`
    - Implement `nearest_neighbor_route()`
    - Implement `two_opt_improve()`
    - Implement `optimize_route()` combining both
    - _Requirements: 4.3, 4.5_

  - [ ]* 3.6 Write property tests for route improvement
    - **Property 6** — 2-opt distance ≤ nearest-neighbor distance
    - **Validates: Requirement 4.5**

- [x] 4. Checkpoint — Verify solvers
  - Ensure solver tests pass.

- [x] 5. Agent Implementations
  - [x] 5.1 Implement TankForecastingAgent in `Agents/overlay/tank_forecasting_agent.py`
    - Extend OverlayAgentBase with agent_id "tank_forecasting"
    - Subscribe to RiskSignals from fuel_management_agent
    - 300-second decision cycle
    - Implement evaluate(): query fuel_stations + fuel_events, compute consumption rates, estimate hours_to_runout with p50/p90, compute runout_risk_24h, handle anomaly flags, persist to mvp_tank_forecasts, publish TankForecast to SignalBus
    - _Requirements: 1.1–1.7_

  - [x] 5.2 Implement DeliveryPrioritizationAgent in `Agents/overlay/delivery_prioritization_agent.py`
    - Extend OverlayAgentBase with agent_id "delivery_prioritization"
    - Subscribe to TankForecast messages
    - 60-second decision cycle
    - Implement evaluate(): consume forecasts, compute weighted priority scores, assign buckets, persist to mvp_delivery_priorities, publish DeliveryPriorityList to SignalBus
    - _Requirements: 2.1–2.7_

  - [x] 5.3 Implement CompartmentLoadingAgent in `Agents/overlay/compartment_loading_agent.py`
    - Extend OverlayAgentBase with agent_id "compartment_loading"
    - Subscribe to DeliveryPriorityList messages
    - 60-second decision cycle, 30-minute per-truck cooldown
    - Implement evaluate(): build delivery requests from priorities, query fuel trucks + compartments, run feasibility + optimization, produce InterventionProposal with loading plan, persist to mvp_load_plans
    - _Requirements: 3.1–3.10_

  - [x] 5.4 Implement RoutePlanningAgent in `Agents/overlay/route_planning_agent.py`
    - Extend OverlayAgentBase with agent_id "route_planning"
    - Subscribe to InterventionProposals from compartment_loading agent
    - 60-second decision cycle
    - Implement evaluate(): extract loading plan, query station locations, run route optimization, compute objective value, produce InterventionProposal with route plan, persist to mvp_routes
    - _Requirements: 4.1–4.9_

  - [x] 5.5 Implement ExceptionReplanningAgent in `Agents/overlay/exception_replanning_agent.py`
    - Extend OverlayAgentBase with agent_id "exception_replanning"
    - Subscribe to RiskSignals from delay_response_agent, sla_guardian_agent, exception_commander
    - 30-second continuous decision cycle
    - Implement evaluate(): detect disruption type, load current plan snapshot, attempt replan (stop reorder, volume reallocation, truck swap), produce patched plan or escalate, persist to mvp_replan_events
    - _Requirements: 5.1–5.8_

- [x] 6. Checkpoint — Verify all agents
  - Ensure all agent tests pass.

- [x] 7. Pipeline and API Integration
  - [x] 7.1 Implement pipeline coordinator in `Agents/support/fuel_distribution_pipeline.py`
    - Implement FuelDistributionPipeline class with run(), get_status()
    - Assign run_id, trigger agents in sequence, track state, broadcast WS events
    - Implement circuit-breaker: halt on agent failure, retry next cycle
    - _Requirements: 6.1–6.6_

  - [x] 7.2 Implement REST endpoints in `Agents/support/mvp_endpoints.py`
    - POST /api/fuel/mvp/plan/generate
    - GET /api/fuel/mvp/plan/{plan_id}
    - POST /api/fuel/mvp/plan/{plan_id}/replan
    - GET /api/fuel/mvp/forecasts
    - GET /api/fuel/mvp/priorities
    - _Requirements: 8.1–8.6_

  - [x] 7.3 Implement WebSocket events for pipeline progress
    - Broadcast forecast_ready, priority_ready, loadplan_ready, route_ready, replan_applied, replan_failed via AgentActivityWSManager
    - _Requirements: 9.1–9.4_

- [x] 8. Checkpoint — Verify pipeline and API
  - Ensure pipeline orchestration and endpoint tests pass.

- [x] 9. Bootstrap Integration
  - [x] 9.1 Update `bootstrap/agents.py` to register MVP agents
    - Import all MVP agents and setup_mvp_indices
    - Call setup_mvp_indices(es_service)
    - Instantiate all 5 MVP agents with shared dependencies
    - Register with AgentScheduler using RestartPolicy.ON_FAILURE
    - Store on app.state.mvp_agents
    - _Requirements: 11.1–11.6_

  - [x] 9.2 Update support `__init__.py` exports
    - Export all MVP classes, solvers, and setup function
    - _Requirements: 11.1_

  - [x] 9.3 Wire MVP endpoints into application router
    - Register mvp_endpoints routes with the FastAPI app
    - _Requirements: 8.1–8.6_

- [x] 10. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional property-based tests
- The pipeline executes A1→A2→A3→A4 sequentially; A5 runs as a continuous monitor
- All agents start in shadow mode by default
- Feature flags: overlay.tank_forecasting, overlay.delivery_prioritization, overlay.truck_compartment_loading, overlay.route_planning, overlay.exception_replanning
- Scoring weights are per-tenant configurable via Redis
- The old truck-compartment-loading spec is superseded by this unified spec
