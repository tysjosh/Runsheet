# Requirements Document

## Introduction

The Truck Compartment Loading Agent is a Layer 1 overlay agent that handles multi-compartment tanker loading plans for fuel delivery operations. It models truck compartments with grade constraints, enforces absolute fuel grade segregation, determines multi-delivery feasibility, and produces optimized loading plans that maximize truck utilization. The agent consumes RiskSignals from the FuelManagementAgent (critical stations needing fuel) and produces InterventionProposals containing compartment-to-delivery assignments through the existing overlay architecture (OverlayAgentBase, SignalBus, ConfirmationProtocol).

## Glossary

- **Loading_Agent**: The Truck Compartment Loading Agent — a Layer 1 overlay agent extending OverlayAgentBase
- **Compartment**: A physical subdivision of a fuel truck's tank, identified by compartment_id, with a fixed capacity_liters and a list of allowed_grades
- **Loading_Plan**: A complete assignment of compartments to deliveries for a single trip, specifying which compartment carries which fuel grade and quantity for which station
- **Grade_Segregation**: The absolute constraint that no compartment may carry more than one fuel grade simultaneously
- **Fuel_Grade**: One of the supported fuel types: AGO, PMS, ATK, LPG
- **Delivery_Request**: A demand for fuel at a specific station — includes station_id, fuel_grade, and quantity_liters
- **Compartment_Utilization**: The ratio of total loaded liters to total compartment capacity across all compartments in a loading plan
- **Feasibility_Check**: The process of determining whether a set of delivery requests can be fulfilled by a single truck in one trip given compartment constraints
- **Compartment_Registry**: The Elasticsearch index (truck_compartments) storing compartment definitions for each fuel truck
- **RiskSignal**: A data contract emitted by Layer 0 agents (e.g., FuelManagementAgent) indicating a detected condition requiring attention
- **InterventionProposal**: A data contract produced by Layer 1 agents containing ranked actions for the ConfirmationProtocol

## Requirements

### Requirement 1: Compartment Data Model

**User Story:** As a fleet manager, I want truck compartments modeled with capacity and grade constraints, so that loading plans respect physical truck configurations.

#### Acceptance Criteria

1. THE Compartment_Registry SHALL store each compartment with fields: compartment_id (keyword), truck_id (keyword), capacity_liters (float, > 0), allowed_grades (list of Fuel_Grade values), position_index (integer), and tenant_id (keyword)
2. WHEN a compartment is created, THE Compartment_Registry SHALL reject entries where capacity_liters is zero or negative
3. WHEN a compartment is created, THE Compartment_Registry SHALL reject entries where allowed_grades is an empty list
4. THE Compartment_Registry SHALL enforce that each compartment_id is unique within a given truck_id
5. WHEN a truck's compartments are queried, THE Compartment_Registry SHALL return all compartments ordered by position_index ascending

### Requirement 2: Grade Segregation Enforcement

**User Story:** As an operations manager, I want absolute fuel grade segregation enforced in every loading plan, so that incompatible fuels are never mixed in a compartment.

#### Acceptance Criteria

1. THE Loading_Agent SHALL assign at most one Fuel_Grade to each Compartment in any Loading_Plan
2. WHEN a Loading_Plan assigns a Fuel_Grade to a Compartment, THE Loading_Agent SHALL verify that the assigned grade is present in the Compartment's allowed_grades list
3. IF a Loading_Plan would assign a Fuel_Grade not in a Compartment's allowed_grades, THEN THE Loading_Agent SHALL reject that assignment and exclude the compartment from consideration for that grade
4. THE Loading_Agent SHALL treat AGO, PMS, ATK, and LPG as mutually incompatible — no two grades may occupy the same Compartment simultaneously

### Requirement 3: Multi-Compartment Feasibility Check

**User Story:** As a dispatch coordinator, I want to know whether a truck can fulfill multiple delivery requests in one trip, so that I can plan efficient multi-drop routes.

#### Acceptance Criteria

1. WHEN a set of Delivery_Requests is provided along with a truck_id, THE Loading_Agent SHALL determine whether the truck can carry all requested quantities respecting grade segregation and compartment capacity constraints
2. WHEN the total requested quantity for a Fuel_Grade exceeds the sum of capacity_liters across all compartments with that grade in their allowed_grades, THE Loading_Agent SHALL report the plan as infeasible and identify the grade and shortfall
3. WHEN a feasibility check succeeds, THE Loading_Agent SHALL return a boolean true result with the maximum achievable Compartment_Utilization
4. WHEN a feasibility check fails, THE Loading_Agent SHALL return a boolean false result with a list of constraint violations describing each failure reason

### Requirement 4: Loading Plan Optimization

**User Story:** As a logistics planner, I want optimal compartment-to-delivery assignments that maximize truck utilization, so that fewer trips are needed and fuel delivery costs are minimized.

#### Acceptance Criteria

1. WHEN a feasible set of Delivery_Requests is provided, THE Loading_Agent SHALL produce a Loading_Plan that assigns each delivery to one or more compartments while maximizing Compartment_Utilization
2. THE Loading_Agent SHALL prefer assignments that fill larger compartments first to minimize wasted capacity
3. WHEN multiple valid Loading_Plans exist, THE Loading_Agent SHALL select the plan with the highest Compartment_Utilization
4. THE Loading_Agent SHALL ensure that the sum of assigned quantities across all compartments for a given Delivery_Request equals the requested quantity_liters for that delivery
5. WHEN a single compartment cannot hold the full quantity for a Delivery_Request, THE Loading_Agent SHALL split the delivery across multiple compatible compartments
6. THE Loading_Agent SHALL produce Loading_Plans within 500ms for trucks with up to 6 compartments and up to 10 delivery requests

### Requirement 5: Constraint Validation

**User Story:** As a safety officer, I want infeasible loading plans rejected with clear explanations, so that unsafe or impossible configurations are never proposed.

#### Acceptance Criteria

1. WHEN a Delivery_Request specifies a quantity_liters exceeding the total compatible compartment capacity for that Fuel_Grade, THE Loading_Agent SHALL reject the request and report the capacity shortfall in liters
2. WHEN a Delivery_Request specifies a Fuel_Grade that has zero compatible compartments on the truck, THE Loading_Agent SHALL reject the request and report that no compartments support the requested grade
3. IF the sum of all Delivery_Request quantities exceeds the truck's total compartment capacity, THEN THE Loading_Agent SHALL reject the plan and report the total overage in liters
4. THE Loading_Agent SHALL validate that every station_id in a Delivery_Request references an existing station in the fuel_stations index before producing a Loading_Plan

### Requirement 6: Signal Consumption and Proposal Production

**User Story:** As a platform operator, I want the Loading Agent to automatically respond to critical fuel station signals by producing loading plans, so that fuel deliveries are coordinated without manual intervention.

#### Acceptance Criteria

1. THE Loading_Agent SHALL subscribe to RiskSignal messages on the SignalBus filtered by source_agent equal to "fuel_management_agent"
2. WHEN one or more RiskSignals are received for fuel stations, THE Loading_Agent SHALL query available fuel trucks and their compartments for the same tenant
3. WHEN available trucks are identified, THE Loading_Agent SHALL run feasibility checks and produce optimized Loading_Plans for the best-fit truck
4. THE Loading_Agent SHALL produce an InterventionProposal containing the Loading_Plan as structured actions with tool_name "execute_loading_plan"
5. THE Loading_Agent SHALL include expected_kpi_delta with keys: compartment_utilization_pct, deliveries_consolidated (count of stations served in one trip), and estimated_fuel_waste_liters (unused compartment capacity)
6. WHILE in shadow mode, THE Loading_Agent SHALL log proposals to the agent_shadow_proposals index without submitting to the ConfirmationProtocol

### Requirement 7: Overlay Architecture Integration

**User Story:** As a platform architect, I want the Loading Agent to follow the same overlay patterns as DispatchOptimizer, so that it integrates seamlessly with the existing agent infrastructure.

#### Acceptance Criteria

1. THE Loading_Agent SHALL extend OverlayAgentBase and implement the evaluate() method
2. THE Loading_Agent SHALL register with the AgentScheduler using RestartPolicy.ON_FAILURE
3. THE Loading_Agent SHALL start in shadow mode by default for all tenants
4. THE Loading_Agent SHALL respect per-tenant mode configuration via the feature flag service with flag key "overlay.truck_compartment_loading"
5. THE Loading_Agent SHALL use a configurable decision cycle interval defaulting to 60 seconds
6. THE Loading_Agent SHALL apply a per-entity cooldown of 30 minutes to prevent duplicate loading plans for the same truck within the cooldown window
7. THE Loading_Agent SHALL publish produced InterventionProposals to the SignalBus for downstream consumers (OutcomeTracker, LearningPolicyAgent)

### Requirement 8: Compartment Registry Elasticsearch Index

**User Story:** As a data engineer, I want compartment data stored in a dedicated ES index with strict mappings, so that compartment queries are fast and data integrity is maintained.

#### Acceptance Criteria

1. THE Compartment_Registry SHALL use a dedicated Elasticsearch index named "truck_compartments" with strict dynamic mapping
2. THE Compartment_Registry SHALL define mappings for: compartment_id (keyword), truck_id (keyword), capacity_liters (float), allowed_grades (keyword array), position_index (integer), tenant_id (keyword), created_at (date), updated_at (date)
3. WHEN the application starts, THE Loading_Agent SHALL ensure the truck_compartments index exists by calling the index setup function during bootstrap
4. THE Compartment_Registry SHALL support querying all compartments for a given truck_id filtered by tenant_id

### Requirement 9: Loading Plan Serialization

**User Story:** As a developer, I want loading plans represented as Pydantic models with JSON round-trip support, so that plans can be persisted, transmitted, and validated consistently.

#### Acceptance Criteria

1. THE Loading_Agent SHALL define a LoadingPlan Pydantic model containing: plan_id (str), truck_id (str), assignments (list of CompartmentAssignment), total_utilization_pct (float), tenant_id (str), created_at (datetime), and status (str)
2. THE Loading_Agent SHALL define a CompartmentAssignment Pydantic model containing: compartment_id (str), station_id (str), fuel_grade (str), quantity_liters (float), and compartment_capacity_liters (float)
3. FOR ALL valid LoadingPlan objects, serializing to JSON then deserializing back SHALL produce an equivalent LoadingPlan object (round-trip property)
4. THE Loading_Agent SHALL validate that total_utilization_pct equals the sum of all assignment quantity_liters divided by the sum of all truck compartment capacity_liters
