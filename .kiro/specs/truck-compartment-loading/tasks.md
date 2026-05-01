# Implementation Plan: Truck Compartment Loading Agent

## Overview

This plan implements a Layer 1 overlay agent for multi-compartment tanker loading plan optimization. The implementation follows a bottom-up sequence: data models first, then the solver (pure logic), then ES mappings, then the agent itself, and finally bootstrap integration.

## Tasks

- [ ] 1. Data Models — Compartment, DeliveryRequest, LoadingPlan
  - [ ] 1.1 Create `Agents/overlay/compartment_models.py`
    - Implement FuelGrade enum (AGO, PMS, ATK, LPG)
    - Implement Compartment Pydantic model with compartment_id, truck_id, capacity_liters (gt=0), allowed_grades (min_length=1), position_index, tenant_id
    - Implement DeliveryRequest Pydantic model with station_id, fuel_grade, quantity_liters (gt=0)
    - Implement CompartmentAssignment Pydantic model with compartment_id, station_id, fuel_grade, quantity_liters, compartment_capacity_liters
    - Implement LoadingPlan Pydantic model with plan_id (UUID default), truck_id, assignments, total_utilization_pct, tenant_id, created_at, status; add model_validator for utilization accuracy
    - Implement ConstraintViolation and FeasibilityResult models
    - _Requirements: 1.1, 1.2, 1.3, 2.4, 9.1, 9.2_

  - [ ]* 1.2 Write property tests for LoadingPlan JSON round-trip
    - **Property 1: Loading Plan JSON Round-Trip** — For any valid LoadingPlan, serializing to JSON then deserializing produces an equal object
    - **Validates: Requirement 9.3**

- [ ] 2. Compartment Solver — Feasibility and Optimization
  - [ ] 2.1 Create `Agents/overlay/compartment_solver.py`
    - Implement `check_feasibility(compartments, requests)` — validates total capacity, per-grade capacity, returns FeasibilityResult with violations
    - Implement `optimize_loading_plan(compartments, requests, truck_id, tenant_id)` — greedy largest-compartment-first algorithm, handles delivery splitting across compartments, returns LoadingPlan or None
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3_

  - [ ]* 2.2 Write property tests for grade segregation invariant
    - **Property 2: Grade Segregation Invariant** — No two assignments in any plan assign different fuel grades to the same compartment
    - **Validates: Requirements 2.1, 2.4**

  - [ ]* 2.3 Write property tests for capacity constraint
    - **Property 3: Capacity Constraint** — Sum of quantity_liters assigned to any compartment does not exceed its capacity_liters
    - **Validates: Requirement 4.4**

  - [ ]* 2.4 Write property tests for demand fulfillment
    - **Property 4: Demand Fulfillment** — For feasible requests, the plan assigns quantities summing to exactly the requested amount per delivery
    - **Validates: Requirement 4.4**

  - [ ]* 2.5 Write property tests for feasibility consistency
    - **Property 5: Feasibility Consistency** — If check_feasibility returns feasible=True, optimize_loading_plan returns non-None; if feasible=False, violations list is non-empty
    - **Validates: Requirements 3.2, 3.3, 3.4**

- [ ] 3. Checkpoint — Verify models and solver
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. ES Mappings and Agent Implementation
  - [ ] 4.1 Create `Agents/overlay/compartment_es_mappings.py`
    - Define TRUCK_COMPARTMENTS_MAPPING with strict mappings for compartment_id, truck_id, capacity_liters, allowed_grades, position_index, tenant_id, created_at, updated_at
    - Implement `setup_compartment_index(es_service)` following the same pattern as setup_overlay_indices
    - _Requirements: 8.1, 8.2, 8.3_

  - [ ] 4.2 Create `Agents/overlay/truck_compartment_loading_agent.py`
    - Extend OverlayAgentBase with agent_id "truck_compartment_loading"
    - Subscribe to RiskSignals from fuel_management_agent
    - 60-second decision cycle, 30-minute per-truck cooldown
    - Implement evaluate(): extract station IDs from signals, build delivery requests from fuel_stations data, query available fuel trucks and compartments, run feasibility + optimization per truck, select best-fit truck, produce InterventionProposal with loading plan actions
    - Implement _build_delivery_requests() — query fuel_stations, compute refill to 80% capacity
    - Implement _query_fuel_trucks() — query trucks index for fuel_truck subtype
    - Implement _query_compartments() — query truck_compartments index, parse into Compartment models
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 7.4, 7.5, 7.6, 7.7_

- [ ] 5. Checkpoint — Verify agent implementation
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Bootstrap Integration
  - [ ] 6.1 Update `bootstrap/agents.py` to register TruckCompartmentLoadingAgent
    - Import TruckCompartmentLoadingAgent and setup_compartment_index
    - Call setup_compartment_index(es_service) after setup_overlay_indices
    - Instantiate TruckCompartmentLoadingAgent with overlay_common_args
    - Register with scheduler using RestartPolicy.ON_FAILURE
    - Add to app.state.overlay_agents dict
    - _Requirements: 7.2, 7.3, 8.3_

  - [ ] 6.2 Update overlay `__init__.py` exports
    - Export TruckCompartmentLoadingAgent, compartment models, solver functions, and setup_compartment_index
    - _Requirements: 7.1_

- [ ] 7. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional property-based tests
- The solver uses a greedy largest-first algorithm — sufficient for ≤6 compartments × ≤10 deliveries within 500ms
- All overlay agents start in shadow mode by default
- Feature flag key: `overlay.truck_compartment_loading`
