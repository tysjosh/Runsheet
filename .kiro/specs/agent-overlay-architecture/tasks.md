# Implementation Plan: Agent Overlay Architecture

## Overview

This plan implements a layered agent overlay architecture composing five new agents on top of the existing three Layer 0 autonomous agents. The implementation follows a bottom-up sequence: foundation infrastructure first (data contracts, signal bus, ES mappings), then the overlay framework (base class, feature flags, bootstrap wiring), then Layer 1 decision agents, then Layer 2 meta-control, and finally integration validation. Each phase builds on the previous, with checkpoints to verify correctness before proceeding.

## Tasks

- [ ] 1. Foundation — Data Contracts, Signal Bus, and ES Mappings
  - [x] 1.1 Create overlay package structure and `__init__.py`
    - Create `Runsheet-backend/Agents/overlay/` directory
    - Create `Runsheet-backend/Agents/overlay/__init__.py` with public exports for all overlay modules
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

  - [x] 1.2 Implement data contracts in `Agents/overlay/data_contracts.py`
    - Implement `RiskSignal` Pydantic v2 model with fields: signal_id (UUID), source_agent, entity_id, entity_type, severity (enum: low/medium/high/critical), confidence (float 0.0–1.0), ttl_seconds (int > 0), timestamp, context (dict), tenant_id, schema_version
    - Implement `InterventionProposal` Pydantic v2 model with fields: proposal_id, source_agent, actions (list), expected_kpi_delta (dict), risk_class (enum: low/medium/high), confidence, priority, tenant_id, timestamp, schema_version
    - Implement `OutcomeRecord` Pydantic v2 model with fields: outcome_id, intervention_id, before_kpis, after_kpis, realized_delta, execution_duration_ms, tenant_id, timestamp, status, schema_version
    - Implement `PolicyChangeProposal` Pydantic v2 model with fields: proposal_id, source_agent, parameter, old_value, new_value, evidence, rollback_plan, confidence, tenant_id, timestamp, schema_version
    - Implement `Severity` and `RiskClass` string enums
    - Add `field_validator` for confidence rounding on RiskSignal
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [ ]* 1.3 Write property tests for data contract JSON round-trip
    - **Property 1: Data Contract JSON Round-Trip** — For any valid data contract instance, serializing to JSON via `model_dump(mode="json")` then deserializing via `model_validate` produces an equal object
    - **Validates: Requirements 1.7, 1.8**

  - [ ]* 1.4 Write property tests for data contract schema validation
    - **Property 2: Data Contract Schema Validation** — For any data contract instance with valid field values, all required fields are present, correctly typed, and within constraints (confidence in [0.0, 1.0], ttl_seconds > 0, schema_version non-empty)
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

  - [x] 1.5 Implement Signal Bus in `Agents/overlay/signal_bus.py`
    - Implement `Subscription` dataclass with subscriber_id, message_type, callback, and filters
    - Implement `SignalBus` class with asyncio-based pub/sub: `subscribe()`, `unsubscribe()`, `publish()` methods
    - Implement `_matches_filters()` for topic-based filtering by source_agent, entity_type, severity, tenant_id
    - Implement TTL expiration for RiskSignals — discard signals whose age exceeds ttl_seconds
    - Implement ES persistence via `_persist()` — index all published signals to `agent_signals` index
    - Implement error isolation — log subscriber errors, skip failing subscriber, continue delivery (Req 2.7)
    - Implement metrics tracking: signals_published_total, signals_delivered_total, signals_expired_total, active_subscriptions
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 2.7, 2.8_

  - [ ]* 1.6 Write property tests for SignalBus type-correct routing
    - **Property 3: SignalBus Type-Correct Routing** — For any set of subscriptions and published messages, the SignalBus delivers each message only to subscribers whose registered message type matches and whose filters match
    - **Validates: Requirements 2.1, 2.3**

  - [ ]* 1.7 Write property tests for SignalBus TTL expiration
    - **Property 4: SignalBus TTL Expiration** — For any RiskSignal whose age exceeds ttl_seconds, the SignalBus does not deliver it and increments the expired counter
    - **Validates: Requirement 2.5**

  - [ ]* 1.8 Write property tests for SignalBus metrics consistency
    - **Property 5: SignalBus Metrics Consistency** — For any sequence of publish/subscribe operations, signals_published_total equals publish call count per type, and signals_delivered_total per subscriber equals successful delivery count
    - **Validates: Requirement 2.8**

  - [x] 1.9 Implement overlay ES index mappings in `Agents/overlay/overlay_es_mappings.py`
    - Define `AGENT_SIGNALS_MAPPING` with fields for all signal types (RiskSignal, InterventionProposal, OutcomeRecord, PolicyChangeProposal)
    - Define `AGENT_SHADOW_PROPOSALS_MAPPING` with shadow_agent and shadow_timestamp fields
    - Define `AGENT_OUTCOMES_MAPPING` with outcome tracking fields
    - Define `AGENT_REVENUE_REPORTS_MAPPING` with weekly report fields
    - Define `AGENT_POLICY_EXPERIMENTS_MAPPING` with experiment tracking fields
    - Implement `setup_overlay_indices()` function following the same pattern as `setup_agent_indices` in `agent_es_mappings.py`, with serverless compatibility
    - _Requirements: 2.6, 3.4, 6.6, 8.6, 11.4_

- [x] 2. Checkpoint — Verify foundation components
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 3. Overlay Framework — Base Class, Feature Flags, and Bootstrap Wiring
  - [x] 3.1 Implement OverlayAgentBase in `Agents/overlay/base_overlay_agent.py`
    - Extend `AutonomousAgentBase` with signal buffering via `_on_signal()` callback and `_buffer_lock`
    - Override `start()` to register Signal Bus subscriptions before starting the polling loop
    - Override `stop()` to unsubscribe from Signal Bus before stopping
    - Override `monitor_cycle()` to implement the decision cycle: collect buffered signals, group by tenant, check mode per tenant, invoke `evaluate()`, route proposals
    - Implement `_get_mode()` to check per-tenant feature flag state via `get_overlay_state()` — returns disabled/shadow/active_gated/active_auto
    - Implement `_log_shadow_proposal()` to persist proposals to `agent_shadow_proposals` ES index
    - Implement `_route_proposal()` to submit InterventionProposals through ConfirmationProtocol and publish to SignalBus
    - Implement `_group_by_tenant()` helper
    - Track per-cycle metrics: signals_consumed, proposals_generated, cycle_duration_ms, mode
    - Define abstract `evaluate()` method for subclass implementation
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [ ]* 3.2 Write property tests for shadow mode isolation
    - **Property 6: Shadow Mode Isolation** — For any overlay agent in shadow mode processing any signals, proposals are logged to shadow index and NOT submitted to ConfirmationProtocol
    - **Validates: Requirements 3.3, 3.4, 9.2**

  - [ ]* 3.3 Write property tests for decision cycle signal consumption
    - **Property 7: Decision Cycle Signal Consumption** — For any overlay agent with non-empty buffer, one decision cycle consumes all buffered signals, invokes evaluate grouped by tenant, and updates cycle metrics
    - **Validates: Requirements 3.6, 3.7**

  - [x] 3.4 Extend FeatureFlagService with overlay state methods
    - Add `OVERLAY_PREFIX = "overlay_ff:"` and `VALID_OVERLAY_STATES` frozenset to `ops/services/feature_flags.py`
    - Implement `get_overlay_state(flag_key, tenant_id)` — reads Redis key `overlay_ff:{flag_key}:{tenant_id}`, returns state string or "disabled" default
    - Implement `set_overlay_state(flag_key, tenant_id, state, user_id)` — validates state, sets Redis key, logs transition
    - Support granular states: disabled, shadow, active_gated, active_auto
    - _Requirements: 12.1, 12.4, 12.5, 12.7_

  - [ ]* 3.5 Write property tests for overlay feature flag state round-trip
    - **Property 23: Overlay Feature Flag State Round-Trip** — For any valid overlay state, setting via `set_overlay_state` then reading via `get_overlay_state` returns the same value
    - **Validates: Requirement 12.4**

  - [ ]* 3.6 Write property tests for per-tenant mode isolation
    - **Property 21: Per-Tenant Mode Isolation** — For any overlay agent and two tenants with different modes, the agent processes each tenant's signals according to that tenant's mode with no cross-tenant leakage
    - **Validates: Requirements 9.5, 9.6**

- [x] 4. Checkpoint — Verify overlay framework
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Layer 1 Agents — DispatchOptimizer, ExceptionCommander, RevenueGuard, CustomerPromise
  - [x] 5.1 Implement DispatchOptimizer in `Agents/overlay/dispatch_optimizer.py`
    - Extend OverlayAgentBase with agent_id "dispatch_optimizer"
    - Subscribe to RiskSignals from delay_response_agent and fuel_management_agent
    - Implement `evaluate()`: extract affected entities, query affected jobs and available assets from ES, score reassignment candidates, filter out net-negative SLA impact candidates, build ranked InterventionProposal
    - Implement `_query_affected_jobs()` and `_query_available_assets()` ES query helpers
    - Implement `_score_reassignments()` with composite scoring (time_saved, fuel_delta, sla_impact)
    - Implement `_is_compatible()` for job-type to asset-type compatibility
    - Configure 60-second decision cycle, 5-minute cooldown
    - Wire to ExecutionPlanner for multi-step reassignments in active mode
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [ ]* 5.2 Write property tests for DispatchOptimizer SLA constraint
    - **Property 8: DispatchOptimizer No Net-Negative SLA Constraint** — Every candidate in the final proposal has non-negative SLA impact; no reassignment creating new SLA breaches appears
    - **Validates: Requirement 4.5**

  - [ ]* 5.3 Write property tests for DispatchOptimizer proposal completeness
    - **Property 9: DispatchOptimizer Proposal Completeness** — Each proposal contains ranked actions with affected job IDs, proposed asset assignments, estimated time savings, and fuel cost delta
    - **Validates: Requirements 4.3, 4.4**

  - [x] 5.4 Implement ExceptionCommander in `Agents/overlay/exception_commander.py`
    - Extend OverlayAgentBase with agent_id "exception_commander"
    - Subscribe to all Layer 0 RiskSignals (no source_agent filter)
    - Implement `Incident` class with state machine: detected → triaging → plan_proposed → executing → resolved | escalated
    - Implement `IncidentState` enum
    - Implement `evaluate()`: correlate signals into incidents by entity overlap within 30-second window, generate ranked response plans for new incidents, check escalation timeouts, broadcast via WebSocket
    - Implement `_find_or_create_incident()` correlation logic
    - Implement `_generate_response_plan()` with playbook steps based on signal sources
    - Implement `_check_escalations()` — escalate incidents stuck in plan_proposed beyond 5-minute timeout, increase severity
    - Implement `_broadcast_incident()` via AgentActivityWSManager
    - Implement `_cleanup_old_incidents()` for resolved/escalated incidents older than 1 hour
    - Configure 30-second decision cycle, 2-minute cooldown
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [ ]* 5.5 Write property tests for ExceptionCommander signal correlation
    - **Property 10: ExceptionCommander Signal Correlation** — Signals within the correlation window sharing entity overlap are correlated into a single incident
    - **Validates: Requirement 5.2**

  - [ ]* 5.6 Write property tests for ExceptionCommander tenant isolation
    - **Property 11: ExceptionCommander Tenant Isolation** — Signals from different tenants produce separate incidents; no cross-tenant signal or entity leakage
    - **Validates: Requirement 5.6**

  - [ ]* 5.7 Write property tests for incident state machine validity
    - **Property 12: Incident State Machine Validity** — State transitions follow valid paths: detected → triaging → plan_proposed → (executing → resolved) | escalated
    - **Validates: Requirement 5.7**

  - [ ]* 5.8 Write property tests for escalation timeout
    - **Property 13: Escalation Timeout** — Incidents in plan_proposed state exceeding the timeout are transitioned to escalated with severity increased by one level
    - **Validates: Requirement 5.8**

  - [x] 5.9 Implement RevenueGuard in `Agents/overlay/revenue_guard.py`
    - Extend OverlayAgentBase with agent_id "revenue_guard"
    - Subscribe to RiskSignals from fuel_management_agent and OutcomeRecords
    - Implement `evaluate()`: compute per-route margin metrics from ES, detect leakage patterns (3+ consecutive below-target margins), generate PolicyChangeProposals with rollback plans
    - Implement `_compute_route_margins()` ES query for completed jobs in last 7 days
    - Implement `_detect_leakage()` — returns True if last N margins are all below target
    - Implement `_maybe_generate_weekly_report()` — persist weekly summary to `agent_revenue_reports` index
    - Track per-route margin history with 20-entry rolling window
    - All proposals classified as HIGH risk, routed through ConfirmationProtocol
    - Configure 120-second decision cycle, 60-minute cooldown
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [ ]* 5.10 Write property tests for margin leakage detection
    - **Property 14: Margin Leakage Detection** — Leakage detected if and only if last N consecutive margins are all below target percentage
    - **Validates: Requirements 6.2, 6.3**

  - [x] 5.11 Write property tests for RevenueGuard output constraint
    - **Property 15: RevenueGuard Output Constraint** — All output proposals are PolicyChangeProposals with HIGH risk classification; no direct mutation actions
    - **Validates: Requirements 6.5, 6.7**

  - [x] 5.12 Implement CustomerPromise in `Agents/overlay/customer_promise.py`
    - Extend OverlayAgentBase with agent_id "customer_promise"
    - Subscribe to RiskSignals from sla_guardian_agent and delay_response_agent
    - Implement `evaluate()`: filter signals with confidence ≥ 0.7, check per-delivery cooldown, generate communication InterventionProposals with channel selection, detect recovery conditions for previously flagged deliveries
    - Implement `_compute_priority()` — customer_tier_weight × delivery_value × severity_weight
    - Implement `_select_channel()` — SMS for critical/high, email for medium, push for low
    - Implement `_cleanup_flagged()` for deliveries older than 24 hours
    - Track flagged deliveries for recovery detection (Req 7.8)
    - Configure 45-second decision cycle, 30-minute per-delivery cooldown
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8_

  - [ ]* 5.13 Write property tests for CustomerPromise confidence threshold
    - **Property 16: CustomerPromise Confidence Threshold** — Communication proposal generated if and only if confidence ≥ 0.7 and delivery not in cooldown
    - **Validates: Requirements 7.2, 7.4**

  - [ ]* 5.14 Write property tests for CustomerPromise priority computation
    - **Property 17: CustomerPromise Priority Computation** — Priority equals customer_tier_weight × delivery_value × severity_weight (low=1, medium=2, high=3, critical=4)
    - **Validates: Requirement 7.7**

  - [ ]* 5.15 Write property tests for CustomerPromise recovery detection
    - **Property 18: CustomerPromise Recovery Detection** — Previously flagged delivery receiving low-severity signal generates recovery notification
    - **Validates: Requirement 7.8**

- [x] 6. Checkpoint — Verify Layer 1 agents
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Layer 2 Agent and Outcome Tracking
  - [x] 7.1 Implement LearningPolicyAgent in `Agents/overlay/learning_policy_agent.py`
    - Extend OverlayAgentBase with agent_id "learning_policy_agent"
    - Subscribe to OutcomeRecords and PolicyChangeProposals
    - Implement `PolicyExperiment` class for tracking deployed experiments
    - Implement `evaluate()`: categorize incoming signals, track outcome history per source agent, identify parameters with 5+ negative outcomes in 7-day window, generate PolicyChangeProposals with statistical evidence and rollback plans, monitor active experiments for rollback triggers
    - Implement `_log_experiment()` — persist to `agent_policy_experiments` ES index
    - Implement `_check_experiment_rollbacks()` — auto-rollback if KPI degrades >5% within 48-hour window, graduate experiments past the window
    - Implement `_update_experiment_status()` ES update helper
    - All proposals classified as HIGH risk with mandatory human approval
    - Bounded rollout: 10% traffic initially
    - Configure 300-second decision cycle, 60-minute cooldown
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

  - [ ]* 7.2 Write property tests for LearningPolicyAgent negative outcome detection
    - **Property 19: LearningPolicyAgent Negative Outcome Detection** — PolicyChangeProposal generated if and only if 5+ negative outcomes in 7-day window; proposals include evidence and rollback plan
    - **Validates: Requirements 8.3, 8.4, 8.5, 8.7**

  - [ ]* 7.3 Write property tests for auto-rollback on KPI degradation
    - **Property 20: Auto-Rollback on KPI Degradation** — Deployed experiment with >5% KPI degradation within 48-hour window is automatically reverted and marked rolled_back
    - **Validates: Requirement 8.8**

  - [x] 7.4 Implement OutcomeTracker in `Agents/overlay/outcome_tracker.py`
    - Implement `PendingOutcome` class for tracking pending after-KPI measurements
    - Implement `OutcomeTracker` class with `record_proposal_execution()` — captures before-KPIs and schedules measurement after observation window (default 1 hour)
    - Implement `check_pending_outcomes()` — measures after-KPIs for proposals past observation window, computes realized_delta, flags adverse outcomes (>10% worse), handles inconclusive cases (entity deleted/tenant disabled)
    - Implement `_measure_kpis()` — ES query for current job KPIs
    - Implement `_persist_outcome()` — index OutcomeRecord to `agent_outcomes` ES index
    - Publish OutcomeRecords to SignalBus for LearningPolicyAgent consumption
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.7, 11.8_

  - [ ]* 7.5 Write property tests for outcome delta computation
    - **Property 22: Outcome Delta Computation** — realized_delta equals after_kpis minus before_kpis for each metric; outcome flagged adverse if degradation exceeds 10% of before value
    - **Validates: Requirements 11.1, 11.3, 11.7**

- [x] 8. Checkpoint — Verify Layer 2 and outcome tracking
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Integration — Bootstrap Wiring and Lifecycle Management
  - [x] 9.1 Update `bootstrap/agents.py` to initialize overlay infrastructure
    - After Layer 0 agents start, create SignalBus wired to es_service
    - Create OutcomeTracker wired to SignalBus and es_service
    - Inject signal_bus reference into Layer 0 agents for signal publishing
    - Call `setup_overlay_indices(es_service)` to create overlay ES indices
    - Instantiate all five overlay agents with shared dependencies (signal_bus, es_service, activity_log_service, ws_manager, confirmation_protocol, autonomy_config_service, feature_flag_service)
    - Register overlay agents with AgentScheduler using ON_FAILURE restart policy — Layer 1 first, then Layer 2
    - Start overlay agents via scheduler after Layer 0 signals are flowing
    - Store overlay agent references on `app.state.overlay_agents`
    - Store signal_bus and outcome_tracker on container
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.6, 10.7, 10.8_

  - [x] 9.2 Update `bootstrap/agents.py` shutdown for ordered teardown
    - Update shutdown function to stop agents in reverse layer order: L2 → L1 → L0
    - Stop learning_policy_agent first, then Layer 1 agents (dispatch_optimizer, exception_commander, revenue_guard, customer_promise), then Layer 0 agents
    - Maintain fallback to `scheduler.stop_all()` if ordered shutdown fails
    - _Requirements: 10.5_

  - [x] 9.3 Update overlay `__init__.py` exports
    - Export all overlay classes from `Agents/overlay/__init__.py`: data contracts, SignalBus, OverlayAgentBase, all five agents, OutcomeTracker, setup_overlay_indices
    - _Requirements: 10.6_

  - [ ]* 9.4 Write integration tests for bootstrap overlay initialization
    - Test that SignalBus is created and wired to ES service
    - Test that all five overlay agents are registered with AgentScheduler
    - Test that overlay ES indices are created
    - Test ordered shutdown: L2 stops before L1, L1 stops before L0
    - _Requirements: 10.1, 10.4, 10.5_

- [x] 10. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each phase
- Property tests validate the 23 correctness properties defined in the design document
- The implementation uses Python with Pydantic v2, asyncio, and Elasticsearch — consistent with the existing codebase
- All overlay agents start in shadow mode by default (Req 9.1) — no mutations until explicitly activated per tenant
- Feature flags use the existing Redis-backed FeatureFlagService extended with granular overlay states
