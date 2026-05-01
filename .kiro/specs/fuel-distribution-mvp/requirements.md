# Requirements Document — Fuel Distribution MVP

## Introduction

This document specifies the requirements for a five-agent fuel distribution pipeline that automates end-to-end fuel delivery planning. The pipeline composes five new MVP agents on top of the existing autonomous agents and overlay architecture: (1) Tank Forecasting Agent predicts per-station/per-grade runout risk, (2) Delivery Prioritization Agent ranks stations for service, (3) Truck Compartment Loading Agent produces feasible multi-compartment loading plans, (4) Route Planning Agent generates optimized delivery routes, and (5) Exception Replanning Agent patches plans when disruptions occur.

The agents do not replace existing infrastructure. They wrap and extend: FuelManagementAgent, DelayResponseAgent, SLAGuardianAgent, ExceptionCommander, and DispatchOptimizer remain unchanged. The MVP agents consume their signals via the SignalBus and produce plans through the existing ConfirmationProtocol.

All agents live under `Agents/overlay/` with supporting infrastructure (data contracts, solvers, ES mappings, pipeline coordinator) in `Agents/support/`, following the overlay architecture patterns (OverlayAgentBase, SignalBus, shadow-first activation, per-tenant feature flags).

## Glossary

- **Tank_Forecast**: A probabilistic prediction of runout risk for a (station_id, fuel_grade) pair over a 24–72 hour horizon
- **Delivery_Priority**: A scored and bucketed ranking of stations that should be served first, based on forecast urgency, SLA tier, and business impact
- **Loading_Plan**: A feasible assignment of fuel grades and quantities to truck compartments for a single trip
- **Route_Plan**: An ordered sequence of delivery stops with ETAs, drop quantities, and distance/cost metrics
- **Replan_Event**: A plan modification triggered by a live disruption (delay, outage, breakdown, demand spike)
- **Pipeline_Run**: A single end-to-end execution of the five-agent pipeline, identified by run_id
- **Fuel_Grade**: One of AGO, PMS, ATK, LPG
- **Compartment**: A physical subdivision of a fuel truck with capacity and grade constraints
- **Grade_Segregation**: The absolute constraint that no compartment carries more than one fuel grade simultaneously
- **Runout_Risk**: The probability that a station's stock for a given grade reaches zero within a time horizon
- **Priority_Bucket**: One of critical, high, medium, low — derived from the priority score

## Requirements

### Requirement 1: Tank Forecasting Agent — Runout Risk Prediction

**User Story:** As an operations planner, I want per-station per-grade runout risk predictions for the next 24–72 hours, so that I can proactively schedule deliveries before stockouts occur.

#### Acceptance Criteria

1. THE Tank_Forecasting_Agent SHALL produce a Tank_Forecast for each (station_id, fuel_grade) pair containing: station_id, fuel_grade, hours_to_runout_p50 (median estimate), hours_to_runout_p90 (pessimistic estimate), runout_risk_24h (probability 0.0–1.0), confidence (0.0–1.0), and feature_version
2. THE Tank_Forecasting_Agent SHALL consume station inventory levels from the `fuel_stations` ES index, historical consumption rates from `fuel_events`, and inbound scheduled deliveries from the scheduling domain
3. THE Tank_Forecasting_Agent SHALL incorporate anomaly flags (sensor drift, station outage, demand spikes) from RiskSignals published by FuelManagementAgent when computing forecasts
4. THE Tank_Forecasting_Agent SHALL persist all forecasts to the `mvp_tank_forecasts` ES index with tenant_id, run_id, and timestamp
5. THE Tank_Forecasting_Agent SHALL publish forecasts to the SignalBus so downstream agents (Delivery Prioritization) can consume them
6. THE Tank_Forecasting_Agent SHALL execute its forecast cycle at a configurable interval (default: 300 seconds) and reuse existing `fuel_calculations.py` logic for baseline consumption rate estimation
7. WHEN a station has zero historical consumption data, THE Tank_Forecasting_Agent SHALL assign a default runout_risk_24h of 0.5 with confidence 0.1 and flag the forecast as "insufficient_data"

### Requirement 2: Delivery Prioritization Agent — Station Ranking

**User Story:** As a dispatch coordinator, I want stations ranked by delivery urgency so that the most critical stations are served first.

#### Acceptance Criteria

1. THE Delivery_Prioritization_Agent SHALL consume Tank_Forecasts from the SignalBus and produce a ranked priority list containing: station_id, fuel_grade, priority_score (0.0–1.0), priority_bucket (critical/high/medium/low), and reasons (list of human-readable strings)
2. THE Delivery_Prioritization_Agent SHALL compute priority_score as a weighted combination of: runout_risk_24h (weight configurable, default 0.4), SLA/criticality tier (weight 0.25), travel-time estimate from nearest available truck (weight 0.2), and stockout business impact (weight 0.15)
3. THE Delivery_Prioritization_Agent SHALL assign priority_bucket based on score thresholds: critical ≥ 0.8, high ≥ 0.6, medium ≥ 0.3, low < 0.3
4. THE Delivery_Prioritization_Agent SHALL persist priority lists to the `mvp_delivery_priorities` ES index with tenant_id, run_id, and timestamp
5. THE Delivery_Prioritization_Agent SHALL publish the priority list to the SignalBus for consumption by the Loading and Route agents
6. THE Delivery_Prioritization_Agent SHALL support per-tenant configurable scoring weights via a policy configuration stored in Redis
7. WHEN a station has no SLA tier configured, THE agent SHALL default to the lowest tier with a reason "no_sla_tier_configured"

### Requirement 3: Truck Compartment Loading Agent — Feasible Loading Plans

**User Story:** As a logistics planner, I want feasible multi-compartment loading plans that respect grade segregation and capacity constraints, so that tankers are loaded safely and efficiently.

#### Acceptance Criteria

1. THE Loading_Agent SHALL model each truck compartment with: compartment_id, truck_id, capacity_liters (> 0), allowed_grades (non-empty list of Fuel_Grade), position_index, and tenant_id, stored in the `truck_compartments` ES index
2. THE Loading_Agent SHALL enforce absolute grade segregation: no compartment may carry more than one fuel grade simultaneously, and assigned grades must be in the compartment's allowed_grades list
3. THE Loading_Agent SHALL determine feasibility for a set of delivery requests against a truck's compartments, reporting infeasible plans with specific constraint violations (capacity shortfall per grade, no compatible compartments, total overage)
4. THE Loading_Agent SHALL produce optimized loading plans using a greedy largest-compartment-first algorithm that maximizes compartment utilization, supports splitting deliveries across multiple compartments, and completes within 500ms for ≤6 compartments and ≤10 deliveries
5. THE Loading_Agent SHALL validate minimum delivery quantities — reject assignments below a configurable minimum drop (default: 500 liters)
6. THE Loading_Agent SHALL apply an uncertainty buffer (configurable, default: 10%) to planned station demand to account for consumption variance between planning and delivery
7. THE Loading_Agent SHALL validate max vehicle weight constraints — reject plans where total loaded weight exceeds the truck's legal weight limit (fuel density × total liters + tare weight)
8. THE Loading_Agent SHALL report unserved_demand_liters in the loading plan output when not all delivery requests can be fulfilled by a single truck
9. THE Loading_Agent SHALL persist loading plans to the `mvp_load_plans` ES index with plan_id, run_id, tenant_id, and timestamp
10. THE Loading_Agent SHALL define LoadingPlan and CompartmentAssignment as Pydantic v2 models with JSON round-trip support

### Requirement 4: Route Planning Agent — Optimized Delivery Routes

**User Story:** As a fleet manager, I want optimized delivery routes that minimize cost and travel time while respecting SLA windows, so that fuel is delivered efficiently.

#### Acceptance Criteria

1. THE Route_Planning_Agent SHALL consume the prioritized station list and loading plan to produce a Route_Plan containing: route_id, truck_id, ordered stops (each with station_id, ETA, drop quantities per grade), total distance_km, eta_confidence, and objective_value
2. THE Route_Planning_Agent SHALL respect truck start/end depot locations and driver shift windows when sequencing stops
3. THE Route_Planning_Agent SHALL use a travel-time/distance matrix (configurable source: static table or external API) to estimate ETAs between stops
4. THE Route_Planning_Agent SHALL respect SLA delivery windows — stations with time-bound SLAs must be visited within their window, or the plan must flag the SLA as at-risk
5. THE Route_Planning_Agent SHALL optimize routes using a nearest-neighbor heuristic with 2-opt improvement, completing within 2 seconds for ≤15 stops
6. THE Route_Planning_Agent SHALL compute an objective_value as a weighted sum of: route cost (fuel + distance), runout risk reduction, truck utilization, and late delivery penalty — using configurable per-tenant weights
7. THE Route_Planning_Agent SHALL persist route plans to the `mvp_routes` ES index with route_id, run_id, tenant_id, and timestamp
8. THE Route_Planning_Agent SHALL integrate with the existing DispatchOptimizer for scoring and refinement of candidate routes
9. THE Route_Planning_Agent SHALL link route plans to the scheduling domain by creating or updating jobs with cargo manifest references

### Requirement 5: Exception Replanning Agent — Live Plan Patching

**User Story:** As an operations manager, I want plans automatically updated when disruptions occur, so that deliveries continue with minimal manual intervention.

#### Acceptance Criteria

1. THE Exception_Replanning_Agent SHALL subscribe to disruption signals: delay RiskSignals from DelayResponseAgent, SLA breach signals from SLAGuardianAgent, and incident signals from ExceptionCommander
2. THE Exception_Replanning_Agent SHALL consume the current plan snapshot (loading plan + route plan) and produce a patched plan with a diff describing: stop reordering, volume reallocation, truck swap, and partial delivery adjustments
3. WHEN a truck breakdown occurs, THE agent SHALL attempt to reassign the remaining stops to another available truck with compatible compartments, producing a new loading plan and route for the replacement truck
4. WHEN a station outage occurs, THE agent SHALL remove the station from the route, reallocate its planned volume to other stations or mark as deferred, and reoptimize the remaining stops
5. WHEN a demand spike is detected, THE agent SHALL increase the planned delivery quantity (up to compartment capacity) and adjust downstream stop quantities accordingly
6. WHEN no feasible replan exists, THE agent SHALL escalate by publishing a HIGH-severity RiskSignal and flagging the plan as "escalation_required" for human intervention
7. THE Exception_Replanning_Agent SHALL persist replan events to the `mvp_replan_events` ES index with the original plan_id, patched plan_id, diff, trigger_signal_id, and timestamp
8. THE Exception_Replanning_Agent SHALL route all plan mutations through the ConfirmationProtocol with MEDIUM risk classification (truck swaps classified as HIGH)

### Requirement 6: Pipeline Orchestration

**User Story:** As a platform architect, I want the five agents coordinated in a sequential pipeline with clear handoffs, so that the end-to-end planning process is reliable and observable.

#### Acceptance Criteria

1. THE Pipeline SHALL execute agents in order: Forecast (A1) → Prioritize (A2) → Load Plan (A3) → Route Plan (A4), with Exception Replanning (A5) running as a continuous monitor
2. THE Pipeline SHALL assign a unique run_id to each end-to-end execution, propagated through all agent outputs for traceability
3. THE Pipeline SHALL support triggering via: (a) periodic schedule (configurable, default every 30 minutes), (b) on-demand via REST API, (c) reactive when FuelManagementAgent emits critical signals
4. THE Pipeline SHALL track pipeline state (pending, forecasting, prioritizing, loading, routing, complete, failed) and broadcast state transitions via WebSocket
5. THE Pipeline SHALL implement circuit-breaker behavior: if any agent fails, the pipeline halts, logs the failure with run_id and agent_id, and retries on the next scheduled cycle
6. THE Pipeline SHALL hook into the existing AgentOrchestrator and ExecutionPlanner for plan execution, and ApprovalQueueService for high-risk changes

### Requirement 7: Data Model and ES Indices

**User Story:** As a data engineer, I want all pipeline data stored in dedicated ES indices with strict mappings and common identifiers, so that data is queryable, auditable, and isolated per tenant.

#### Acceptance Criteria

1. THE Platform SHALL create the following ES indices with strict dynamic mappings: mvp_tank_forecasts, mvp_delivery_priorities, mvp_load_plans, mvp_routes, mvp_replan_events, mvp_plan_outcomes
2. ALL documents across MVP indices SHALL include common fields: tenant_id (keyword), run_id (keyword), plan_id (keyword where applicable), timestamp (date)
3. THE mvp_tank_forecasts index SHALL store: station_id, fuel_grade, hours_to_runout_p50, hours_to_runout_p90, runout_risk_24h, confidence, feature_version, anomaly_flags
4. THE mvp_delivery_priorities index SHALL store: station_id, fuel_grade, priority_score, priority_bucket, reasons, scoring_weights
5. THE mvp_load_plans index SHALL store: plan_id, truck_id, compartments (nested), feasibility, utilization, unserved_demand_l, weight_kg, status
6. THE mvp_routes index SHALL store: route_id, truck_id, stops (nested with station_id, eta, drop quantities), distance_km, eta_confidence, objective_value, status
7. THE mvp_replan_events index SHALL store: event_id, original_plan_id, patched_plan_id, trigger_signal_id, diff, replan_type, status
8. THE mvp_plan_outcomes index SHALL store: outcome_id, plan_id, run_id, before_kpis, after_kpis, realized_delta, status (measured/adverse/inconclusive)
9. ALL indices SHALL be created during application bootstrap via a `setup_mvp_indices(es_service)` function following the existing `setup_overlay_indices` pattern

### Requirement 8: REST API Endpoints

**User Story:** As a frontend developer, I want REST endpoints to trigger planning, retrieve plans, and initiate replanning, so that the UI can interact with the pipeline.

#### Acceptance Criteria

1. THE Platform SHALL expose `POST /api/fuel/mvp/plan/generate` to trigger a full pipeline run, returning the run_id and initial status
2. THE Platform SHALL expose `GET /api/fuel/mvp/plan/{plan_id}` to retrieve a complete plan (loading + route) by plan_id
3. THE Platform SHALL expose `POST /api/fuel/mvp/plan/{plan_id}/replan` to trigger exception replanning for an existing plan, accepting a disruption description
4. THE Platform SHALL expose `GET /api/fuel/mvp/forecasts` to retrieve the latest tank forecasts for a tenant, with optional station_id and fuel_grade filters
5. THE Platform SHALL expose `GET /api/fuel/mvp/priorities` to retrieve the latest delivery priority rankings for a tenant
6. ALL endpoints SHALL require tenant_id via the existing tenant guard middleware and return paginated responses following the existing PaginatedResponse pattern

### Requirement 9: WebSocket Events

**User Story:** As a real-time dashboard user, I want live updates as the pipeline progresses, so that I can monitor planning status without polling.

#### Acceptance Criteria

1. THE Platform SHALL expose a `/ws/fuel-planning` WebSocket endpoint for real-time pipeline events
2. THE Platform SHALL broadcast the following events: forecast_ready, priority_ready, loadplan_ready, route_ready, replan_applied, replan_failed
3. EACH event SHALL include: run_id, tenant_id, timestamp, and a summary payload appropriate to the event type
4. THE WebSocket SHALL use the existing AgentActivityWSManager pattern for connection management and tenant-scoped broadcasting

### Requirement 10: MVP Scoring Objectives

**User Story:** As a business analyst, I want the pipeline's optimization objectives explicitly defined and configurable per tenant, so that planning reflects each tenant's operational priorities.

#### Acceptance Criteria

1. THE Pipeline SHALL use a weighted multi-objective function for agents A2–A4 with the following default weights: minimize runout risk (0.30), minimize late deliveries (0.25), minimize route/fuel cost (0.20), maximize truck utilization (0.15), penalize plan churn (0.10)
2. THE scoring weights SHALL be configurable per tenant via Redis-backed policy configuration
3. THE Pipeline SHALL log the objective weights used for each run_id to enable retrospective analysis
4. THE Pipeline SHALL compute and persist per-run KPI metrics to the `mvp_plan_outcomes` index: stockout_risk_reduction, stations_served_before_critical, tanker_utilization_pct, route_cost_per_liter, replan_count, manual_interventions

### Requirement 11: Overlay Architecture Integration

**User Story:** As a platform architect, I want all MVP agents to follow the existing overlay patterns, so that they integrate seamlessly with the agent infrastructure.

#### Acceptance Criteria

1. ALL MVP agents SHALL extend OverlayAgentBase and implement the evaluate() method
2. ALL MVP agents SHALL register with the AgentScheduler using RestartPolicy.ON_FAILURE
3. ALL MVP agents SHALL start in shadow mode by default for all tenants
4. ALL MVP agents SHALL respect per-tenant mode configuration via feature flags: overlay.tank_forecasting, overlay.delivery_prioritization, overlay.truck_compartment_loading, overlay.route_planning, overlay.exception_replanning
5. ALL MVP agents SHALL publish their outputs to the SignalBus for downstream consumption and outcome tracking
6. THE Exception_Replanning_Agent SHALL run as a continuous monitor (not pipeline-triggered) with a 30-second decision cycle
