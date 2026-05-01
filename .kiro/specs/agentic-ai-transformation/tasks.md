# Implementation Plan: Agentic AI Transformation

## Overview

This plan transforms the Runsheet logistics platform from a read-only AI assistant into a fully agentic system. Implementation proceeds bottom-up: core infrastructure (risk registry, business validator, confirmation protocol) first, then mutation tools, autonomous agents, multi-agent orchestration, memory/feedback services, REST endpoints, WebSocket integration, and finally frontend components. Each task builds on previous steps, and property-based tests validate correctness properties from the design document.

## Tasks

- [x] 1. Set up core infrastructure: Risk Registry and Business Validator
  - [x] 1.1 Create the Risk Registry module (`Runsheet-backend/Agents/risk_registry.py`)
    - Implement `RiskLevel` enum (low, medium, high)
    - Implement `DEFAULT_RISK_REGISTRY` dict mapping tool names to risk levels
    - Implement `RiskRegistry` class with `classify(tool_name)` and `set_override(tool_name, level)` methods
    - Support Redis-backed overrides with fallback to defaults
    - Unknown tool names default to HIGH risk
    - _Requirements: 1.4, 1.5_

  - [ ]* 1.2 Write property test for Risk Classification Completeness
    - **Property 2: Risk Classification Completeness**
    - Generate random tool names (registered and unregistered) and verify valid RiskLevel returned; unknown defaults to HIGH
    - **Validates: Requirements 1.4**

  - [x] 1.3 Create the Business Validator module (`Runsheet-backend/Agents/business_validator.py`)
    - Implement `ValidationResult` dataclass with `valid` and `reason` fields
    - Implement `VALID_JOB_TRANSITIONS` state machine dict
    - Implement `BusinessValidator` class with `validate(tool_name, params, tenant_id)` dispatcher
    - Implement validators: `_validate_update_job_status`, `_validate_assign_asset_to_job`, `_validate_cancel_job`, `_validate_request_fuel_refill`
    - Implement `_fetch_job` and `_fetch_asset` helper methods with tenant scoping
    - _Requirements: 1.9, 1.10_

  - [ ]* 1.4 Write property test for Business Rule Validation
    - **Property 3: Business Rule Validation Rejects Invalid Mutations**
    - Generate invalid mutation parameters (invalid status transitions, non-existent entities, out-of-range values) and verify rejection with reason
    - **Validates: Requirements 1.9**

- [x] 2. Implement Confirmation Protocol and Autonomy Config
  - [x] 2.1 Create the Autonomy Config Service (`Runsheet-backend/Agents/autonomy_config_service.py`)
    - Implement `AutonomyConfigService` class with Redis-backed `get_level(tenant_id)` and `set_level(tenant_id, level)` methods
    - Default autonomy level for new tenants is "suggest-only"
    - _Requirements: 10.1, 10.2, 10.6_

  - [x] 2.2 Create the Confirmation Protocol module (`Runsheet-backend/Agents/confirmation_protocol.py`)
    - Implement `MutationRequest` and `MutationResult` dataclasses
    - Implement `ConfirmationProtocol` class with `process_mutation(request)` method
    - Implement `_should_auto_execute(risk_level, autonomy_level)` routing matrix: suggest-only allows nothing, auto-low allows low, auto-medium allows low+medium, full-auto allows all
    - Wire risk registry, approval queue, autonomy config, activity log, and business validator
    - _Requirements: 1.4, 1.5, 1.6, 1.7, 1.8, 10.3_

  - [ ]* 2.3 Write property test for Confirmation Protocol Routing Matrix
    - **Property 1: Confirmation Protocol Routing Matrix**
    - Generate all 12 combinations of (risk_level × autonomy_level) and verify correct execute/queue decision
    - **Validates: Requirements 1.5, 1.7, 10.3**

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement Approval Queue Service
  - [x] 4.1 Create the Approval Queue Service (`Runsheet-backend/Agents/approval_queue_service.py`)
    - Implement `ApprovalQueueService` class with `create`, `approve`, `reject`, `expire_stale`, and `list_pending` methods
    - Store approval entries in `agent_approval_queue` ES index with fields: action_id, action_type, tool_name, parameters, risk_level, proposed_by, proposed_at, status, reviewed_by, reviewed_at, expiry_time, impact_summary, tenant_id
    - Broadcast approval events via WebSocket on state changes
    - Use ES optimistic concurrency for concurrent approve/reject
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_

  - [ ]* 4.2 Write property test for Approval Lifecycle State Machine
    - **Property 5: Approval Lifecycle State Machine**
    - Generate sequences of state transitions and verify only valid transitions succeed: pending→approved→executed, pending→rejected, pending→expired
    - **Validates: Requirements 2.4, 2.5, 2.6, 2.8**

  - [ ]* 4.3 Write property test for Approval Queue Sorting Invariant
    - **Property 6: Approval Queue Sorting Invariant**
    - Generate random approval entries with various proposed_at timestamps and verify list_pending returns them sorted by proposed_at descending
    - **Validates: Requirements 2.3**

  - [ ]* 4.4 Write property test for Approval Expiry Correctness
    - **Property 7: Approval Expiry Correctness**
    - Generate approvals with various expiry times (past and future) and verify expire_stale correctly marks only expired entries
    - **Validates: Requirements 2.6**

- [x] 5. Implement Activity Log Service
  - [x] 5.1 Create the Activity Log Service (`Runsheet-backend/Agents/activity_log_service.py`)
    - Implement `ActivityLogService` class with `log`, `log_mutation`, `log_monitoring_cycle`, `log_tool_invocation`, `query`, and `get_stats` methods
    - Store entries in `agent_activity_log` ES index with strict mapping: log_id, agent_id, action_type, tool_name, parameters, risk_level, outcome, duration_ms, tenant_id, user_id, session_id, timestamp, details
    - Broadcast activity events via WebSocket
    - _Requirements: 1.8, 8.1, 8.2, 8.3, 8.6, 8.7_

  - [ ]* 5.2 Write property test for Activity Log Completeness
    - **Property 4: Activity Log Completeness**
    - Generate sequences of agent actions and verify an activity log entry is created for each, containing agent_id, action_type, timestamp, outcome, and duration_ms
    - **Validates: Requirements 1.8, 3.7, 4.6, 5.6, 6.7, 8.2, 8.3, 10.5**

  - [ ]* 5.3 Write property test for Activity Log Filter Correctness
    - **Property 17: Activity Log Filter Correctness**
    - Generate log entries and filter combinations (agent_id, action_type, tenant_id, time_range, outcome), verify every returned entry matches all filters and no matching entry is excluded
    - **Validates: Requirements 8.4**

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement Mutation Tools
  - [x] 7.1 Create the Mutation Tools module (`Runsheet-backend/Agents/tools/mutation_tools.py`)
    - Implement `configure_mutation_tools(confirmation_protocol, es_service)` wiring function
    - Implement scheduling mutations: `assign_asset_to_job`, `update_job_status`, `cancel_job`, `create_job` — each as `@tool` decorated async functions routing through Confirmation Protocol
    - Implement ops mutations: `reassign_rider`, `escalate_shipment`
    - Implement fuel mutations: `request_fuel_refill`, `update_fuel_threshold`
    - Each tool creates a `MutationRequest`, calls `confirmation_protocol.process_mutation()`, and returns a formatted result string
    - _Requirements: 1.1, 1.2, 1.3, 1.8, 1.9, 1.10_

  - [x] 7.2 Update tools `__init__.py` to export mutation tools
    - Add mutation tool imports and include them in `ALL_TOOLS` list
    - _Requirements: 1.1, 1.2, 1.3_

  - [ ]* 7.3 Write unit tests for mutation tools
    - Test each mutation tool with valid and invalid parameters
    - Test that low-risk mutations execute immediately
    - Test that high-risk mutations return approval_id
    - Test validation failure reporting with corrective suggestions
    - _Requirements: 1.5, 1.6, 1.7, 1.9, 1.10_

- [x] 8. Implement Autonomous Agent Base and Fuel Calculations
  - [x] 8.1 Create the Autonomous Agent Base Class (`Runsheet-backend/Agents/autonomous/base_agent.py`)
    - Implement `AutonomousAgentBase` ABC with `start`, `stop`, `_run_loop`, `_is_on_cooldown`, `_set_cooldown`, `monitor_cycle` (abstract), and `status` property
    - Implement polling loop with configurable interval, cooldown tracking, and error handling
    - Log monitoring cycles to Activity Log Service
    - _Requirements: 3.1, 3.6, 3.7, 4.1, 4.4, 4.6, 5.1, 5.7_

  - [ ]* 8.2 Write property test for Autonomous Agent Cooldown
    - **Property 8: Autonomous Agent Cooldown Prevents Duplicates**
    - Generate action sequences with timestamps for the same entity and verify cooldown enforcement prevents duplicates within the configured period
    - **Validates: Requirements 3.6, 4.4, 5.7**

  - [x] 8.3 Create Fuel Calculations module (`Runsheet-backend/Agents/autonomous/fuel_calculations.py`)
    - Implement `FuelPriority` enum (critical, high, medium, normal)
    - Implement `calculate_refill_quantity(capacity, current_stock, target_pct=0.8)` returning `max(0, 0.8*C - S)`
    - Implement `calculate_refill_priority(days_until_empty)` with thresholds: <1=critical, <3=high, <5=medium, else normal
    - _Requirements: 4.3, 4.7_

  - [ ]* 8.4 Write property test for Fuel Refill Quantity Calculation
    - **Property 9: Fuel Refill Quantity Calculation**
    - Generate random (capacity, stock) pairs and verify result equals max(0, 0.8*C - S)
    - **Validates: Requirements 4.3**

  - [ ]* 8.5 Write property test for Fuel Refill Priority Classification
    - **Property 10: Fuel Refill Priority Classification**
    - Generate random days_until_empty values and verify correct priority: critical if <1, high if <3, medium if <5, normal otherwise
    - **Validates: Requirements 4.7**

- [x] 9. Implement Autonomous Agents
  - [x] 9.1 Create the Delay Response Agent (`Runsheet-backend/Agents/autonomous/delay_response_agent.py`)
    - Extend `AutonomousAgentBase` with agent_id "delay_response_agent"
    - Implement `monitor_cycle`: poll `jobs_current` for in_progress jobs past estimated_arrival
    - Find compatible available assets using `_find_available_asset`
    - Propose reassignment via Confirmation Protocol or escalate via WebSocket if no asset available
    - Respect tenant feature flags and cooldown periods
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [x] 9.2 Create the Fuel Management Agent (`Runsheet-backend/Agents/autonomous/fuel_management_agent.py`)
    - Extend `AutonomousAgentBase` with agent_id "fuel_management_agent"
    - Implement `monitor_cycle`: poll `fuel_stations` for critical stations or low days_until_empty
    - Calculate refill quantity using `calculate_refill_quantity` and priority using `calculate_refill_priority`
    - Create refill requests via `request_fuel_refill` mutation tool
    - Broadcast fuel_alert via WebSocket with station details and urgency
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x] 9.3 Create the SLA Guardian Agent (`Runsheet-backend/Agents/autonomous/sla_guardian_agent.py`)
    - Extend `AutonomousAgentBase` with agent_id "sla_guardian_agent"
    - Implement `monitor_cycle`: poll `shipments_current` for in_transit shipments approaching SLA breach (within 30 min of estimated_delivery)
    - Evaluate rider workload; propose reassignment if rider has >3 active shipments
    - Escalate breached shipments by updating priority to "critical" and broadcasting sla_breach event
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [x] 9.4 Create `__init__.py` for autonomous agents package (`Runsheet-backend/Agents/autonomous/__init__.py`)
    - Export all autonomous agent classes and fuel calculations
    - _Requirements: 3.1, 4.1, 5.1_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement Execution Planner
  - [x] 11.1 Create the Execution Planner module (`Runsheet-backend/Agents/execution_planner.py`)
    - Implement `StepStatus` enum, `PlanStep` and `ExecutionPlan` dataclasses
    - Implement `validate_plan_dag(steps)` using Kahn's algorithm for cycle detection and reference validation
    - Implement `topological_sort(steps)` for dependency-ordered execution
    - Implement `ExecutionPlanner` class with `create_plan`, `execute_plan`, and `rollback_plan` methods
    - Execute steps in dependency order, pass outputs between dependent steps
    - Support up to 2 recovery attempts per failed step (MAX_RECOVERY_ATTEMPTS)
    - Rollback completed steps in reverse order when requested
    - Log full plan, execution trace, and outcome to Activity Log
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [ ]* 11.2 Write property test for Execution Plan Dependency Acyclicity
    - **Property 11: Execution Plan Dependency Acyclicity**
    - Generate random dependency graphs and verify DAG validation: all depends_on references exist, no cycles
    - **Validates: Requirements 6.1**

  - [ ]* 11.3 Write property test for Execution Plan Step Ordering
    - **Property 12: Execution Plan Step Ordering**
    - Generate valid DAG plans and verify topological_sort produces a valid execution order where no step runs before its dependencies
    - **Validates: Requirements 6.3**

  - [ ]* 11.4 Write property test for Execution Plan Recovery Bound
    - **Property 13: Execution Plan Recovery Bound**
    - Generate failing steps and verify recovery_attempts never exceeds MAX_RECOVERY_ATTEMPTS (2)
    - **Validates: Requirements 6.5**

  - [ ]* 11.5 Write property test for Execution Plan Rollback Order
    - **Property 14: Execution Plan Rollback Order**
    - Generate completed plans and verify rollback executes in reverse completion order
    - **Validates: Requirements 6.8**

- [x] 12. Implement Multi-Agent Architecture
  - [x] 12.1 Create Specialist Agent classes (`Runsheet-backend/Agents/specialists/`)
    - Create `fleet_agent.py`: FleetAgent with tools limited to fleet search, summary, lookup, location, and fleet mutation tools
    - Create `scheduling_agent.py`: SchedulingAgent with scheduling search, details, available assets, summary, dispatch report, and scheduling mutation tools
    - Create `fuel_agent.py`: FuelAgent with fuel search, summary, consumption history, fuel report, and fuel mutation tools
    - Create `ops_intelligence_agent.py`: OpsIntelligenceAgent with ops search, riders, shipment events, ops metrics, and ops report/mutation tools
    - Create `reporting_agent.py`: ReportingAgent with all report generation tools across domains
    - Create `__init__.py` exporting all specialist classes
    - Each specialist has its own Strands Agent instance with domain-specific system prompt and tool set
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.9_

  - [ ]* 12.2 Write property test for Specialist Agent Tool Set Isolation
    - **Property 15: Specialist Agent Tool Set Isolation**
    - Verify each specialist's tool set matches exactly the tools defined for its domain; no specialist has tools outside its domain
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5**

  - [x] 12.3 Create the Agent Orchestrator (`Runsheet-backend/Agents/orchestrator.py`)
    - Implement `AgentOrchestrator` class with `ROUTING_TABLE` mapping domains to keywords
    - Implement `route(user_message, tenant_id, session_id)` method: classify intent, delegate to specialist(s), synthesize results
    - Implement `_classify_intent(message)` using keyword matching against routing table
    - Implement `_is_complex_request(message)` to detect multi-step requests
    - For complex requests, use ExecutionPlanner to create and execute plans
    - For no-match requests, fall back to reporting agent
    - _Requirements: 7.6, 7.7, 7.8_

  - [ ]* 12.4 Write property test for Orchestrator Intent Routing
    - **Property 16: Orchestrator Intent Routing**
    - Generate messages with domain keywords and verify routing to all matching specialists; no-match falls back to reporting
    - **Validates: Requirements 7.6, 7.7, 7.8**

- [x] 13. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Implement Memory and Feedback Services
  - [x] 14.1 Create the Memory Service (`Runsheet-backend/Agents/memory_service.py`)
    - Implement `MemoryService` class with `store_pattern`, `store_preference`, `query_relevant`, `decay_stale`, and `delete` methods
    - Store memories in `agent_memory` ES index with fields: memory_id, memory_type, agent_id, tenant_id, content, confidence_score, created_at, last_accessed, access_count, tags
    - Implement relevance decay: reduce confidence by 50% for memories not accessed in 90 days, purge below 0.1
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

  - [ ]* 14.2 Write property test for Memory Store Round-Trip
    - **Property 18: Memory Store Round-Trip**
    - Generate random memory entries (pattern and preference), store via Memory Service, retrieve by memory_id, verify content, memory_type, confidence_score, and tags match
    - **Validates: Requirements 11.2, 11.3**

  - [ ]* 14.3 Write property test for Memory Deletion Completeness
    - **Property 19: Memory Deletion Completeness**
    - Generate memories, delete via DELETE endpoint, verify querying by memory_id returns no results
    - **Validates: Requirements 11.6**

  - [ ]* 14.4 Write property test for Memory Relevance Decay
    - **Property 20: Memory Relevance Decay**
    - Generate memories with various ages and confidence scores, run decay function, verify confidence reduced by 50% for stale entries and entries below 0.1 are purged
    - **Validates: Requirements 11.7**

  - [x] 14.5 Create the Feedback Service (`Runsheet-backend/Agents/feedback_service.py`)
    - Implement `FeedbackService` class with `record_rejection`, `record_override`, `query_similar`, `get_stats`, and `compute_confidence` methods
    - Store feedback in `agent_feedback` ES index with fields: feedback_id, agent_id, action_type, original_proposal, user_action, feedback_type, tenant_id, user_id, timestamp, context
    - Implement confidence computation using exponential decay: `base * e^(-0.3 * rejection_count)`
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7_

  - [ ]* 14.6 Write property test for Rejection Feedback Signal Creation
    - **Property 21: Rejection Feedback Signal Creation**
    - Generate approval rejections and verify a feedback signal is created with feedback_type="rejection", matching original_proposal, and rejection reason
    - **Validates: Requirements 12.1**

  - [ ]* 14.7 Write property test for Confidence Score Monotonic Decrease
    - **Property 22: Confidence Score Monotonic Decrease with Rejections**
    - Generate increasing rejection counts and verify confidence score monotonically decreases; zero rejections yields higher confidence than any positive count
    - **Validates: Requirements 12.7**

- [x] 15. Implement Elasticsearch Index Mappings and ILM
  - [x] 15.1 Create ES index mappings for new indices
    - Create mapping for `agent_approval_queue` index with all fields from design
    - Create mapping for `agent_activity_log` index with strict mapping
    - Create mapping for `agent_memory` index
    - Create mapping for `agent_feedback` index
    - Apply ILM policy for `agent_activity_log`: hot 0-30d, warm 30-90d, cold 90-365d, delete after 365d
    - _Requirements: 2.1, 8.1, 8.6, 11.1, 12.3_

- [x] 16. Implement REST Endpoints and WebSocket
  - [x] 16.1 Create the Agent Activity WebSocket Manager (`Runsheet-backend/Agents/agent_ws_manager.py`)
    - Implement `AgentActivityWSManager` class with `connect`, `disconnect`, `broadcast_activity`, and `broadcast_approval_event` methods
    - Handle dead client cleanup on broadcast failures
    - _Requirements: 2.7, 8.7_

  - [x] 16.2 Create REST endpoints router (`Runsheet-backend/agent_endpoints.py`)
    - Implement approval queue endpoints: GET `/agent/approvals`, POST `/agent/approvals/{action_id}/approve`, POST `/agent/approvals/{action_id}/reject`
    - Implement activity log endpoints: GET `/agent/activity` (paginated with filters), GET `/agent/activity/stats`
    - Implement autonomy config endpoint: PATCH `/agent/config/autonomy` (admin-only JWT check)
    - Implement memory endpoints: GET `/agent/memory`, DELETE `/agent/memory/{memory_id}`
    - Implement feedback endpoints: GET `/agent/feedback`, GET `/agent/feedback/stats`
    - Implement agent health endpoints: GET `/agent/health`, POST `/agent/{agent_id}/pause`, POST `/agent/{agent_id}/resume`
    - _Requirements: 2.3, 2.4, 2.5, 8.4, 8.5, 10.4, 10.5, 11.5, 11.6, 12.5, 12.6, 9.6_

  - [x] 16.3 Add WebSocket route for `/ws/agent-activity`
    - Register WebSocket endpoint in FastAPI app
    - Wire to AgentActivityWSManager
    - _Requirements: 8.7_

  - [ ]* 16.4 Write unit tests for REST endpoints
    - Test approval queue CRUD operations and status transitions
    - Test activity log pagination and filtering
    - Test autonomy config update with admin vs non-admin JWT
    - Test memory list and delete operations
    - Test feedback list and stats endpoints
    - Test agent health, pause, and resume endpoints
    - _Requirements: 2.3, 2.4, 2.5, 8.4, 8.5, 10.4, 11.5, 11.6, 12.5, 12.6_

- [x] 17. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. Implement Pydantic Models and Data Validation
  - [x] 18.1 Create Pydantic models for agent data structures
    - Implement `ApprovalEntry`, `ActivityLogEntry`, `MemoryEntry`, `FeedbackSignal` models
    - Implement `AutonomyUpdateRequest`, `ApprovalRejectRequest` request models
    - Implement `RiskLevel`, `AutonomyLevel`, `ApprovalStatus` enums
    - _Requirements: 2.1, 8.1, 10.1, 11.1, 12.3_

- [x] 19. Wire Application Lifespan and Integration
  - [x] 19.1 Update `main.py` lifespan to initialize agentic services
    - Initialize RiskRegistry, BusinessValidator, ActivityLogService, AutonomyConfigService, ApprovalQueueService, ConfirmationProtocol
    - Initialize MemoryService and FeedbackService
    - Wire mutation tools via `configure_mutation_tools`
    - Initialize specialist agents (Fleet, Scheduling, Fuel, Ops, Reporting)
    - Initialize ExecutionPlanner and AgentOrchestrator
    - Start autonomous agents (DelayResponse, FuelManagement, SLAGuardian) as background tasks
    - Store agent references in `app.state` for health/pause/resume endpoints
    - Register agent_endpoints router
    - Gracefully stop autonomous agents on shutdown
    - _Requirements: 3.1, 4.1, 5.1, 7.1-7.9, 9.6_

  - [x] 19.2 Update `mainagent.py` to use the Orchestrator for routing
    - Replace direct agent invocation with orchestrator routing for chat requests
    - Maintain backward compatibility with existing chat_streaming interface
    - _Requirements: 7.6, 7.7_

- [x] 20. Implement Frontend Agent Interface Components
  - [x] 20.1 Create the Command Interface page (`/ops/command`)
    - Build full-width command interface with AI chat as primary view
    - Add collapsible dashboard sidebar
    - Support inline action confirmation for medium-risk actions in chat flow
    - _Requirements: 9.1, 9.4_

  - [x] 20.2 Create the Agent Activity Feed panel
    - Display real-time autonomous agent actions with timestamps and outcomes
    - Subscribe to `/ws/agent-activity` WebSocket channel
    - Show agent name, action summary, and outcome for each entry
    - _Requirements: 9.2, 9.7_

  - [x] 20.3 Create the Approval Queue panel
    - Display pending actions requiring human approval
    - Show approve/reject buttons with impact summaries
    - Subscribe to WebSocket for real-time approval queue updates
    - Wire to POST `/agent/approvals/{action_id}/approve` and `/reject` endpoints
    - _Requirements: 9.3_

  - [x] 20.4 Create the Agent Health panel
    - Display status of each autonomous agent (running, paused, error) with last activity timestamp
    - Add pause/resume controls wired to POST `/agent/{agent_id}/pause` and `/resume`
    - _Requirements: 9.5, 9.6_

  - [x] 20.5 Implement toast notifications for autonomous agent actions
    - Display toast notification when an autonomous agent takes an action
    - Show agent name, action summary, and outcome
    - _Requirements: 9.7_

- [x] 21. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate the 22 universal correctness properties from the design document using Hypothesis
- Unit tests validate specific examples and edge cases
- All 26 existing read-only tools remain unchanged; new mutation tools and autonomous agents are additive
- The implementation uses Python throughout: FastAPI backend, Strands SDK agents, Elasticsearch data layer, Redis state management
