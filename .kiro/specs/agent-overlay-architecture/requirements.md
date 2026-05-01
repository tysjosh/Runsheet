# Requirements Document

## Introduction

This document specifies the requirements for a layered agent overlay architecture built on top of the existing three autonomous agents (DelayResponseAgent, FuelManagementAgent, SLAGuardianAgent). The overlay introduces five new agents organized in two layers: Layer 1 (decision overlays) contains DispatchOptimizer, ExceptionCommander, RevenueGuard, and CustomerPromise; Layer 2 (meta-control) contains LearningPolicyAgent. These overlay agents consume signals from the existing Layer 0 domain watchdogs and produce higher-order decisions — global reallocation, incident triage, margin protection, proactive customer communication, and continuous policy tuning.

The overlay agents do not replace the existing autonomous agents. They compose on top of them, reading their outputs via standardized data contracts (RiskSignal, InterventionProposal, OutcomeRecord, PolicyChangeProposal) and writing through the existing ExecutionPlanner and ConfirmationProtocol. Each overlay agent starts in shadow mode (read-only observation) and graduates to gated mutation through the existing approval and autonomy infrastructure.

## Glossary

- **Overlay_Agent**: A new agent that sits above the existing Layer 0 autonomous agents, consuming their signals and producing higher-order decisions. Overlay agents do not replace Layer 0 agents.
- **Layer_0**: The existing domain watchdog agents — DelayResponseAgent, FuelManagementAgent, SLAGuardianAgent — that perform continuous monitoring and local mitigation.
- **Layer_1**: Decision overlay agents — DispatchOptimizer, ExceptionCommander, RevenueGuard, CustomerPromise — that consume Layer 0 signals and produce cross-domain decisions.
- **Layer_2**: Meta-control agent — LearningPolicyAgent — that observes outcomes across all layers and proposes policy/threshold adjustments.
- **RiskSignal**: A standardized data contract emitted by Layer 0 agents containing source_agent, entity_id, severity, confidence, and ttl fields.
- **InterventionProposal**: A standardized data contract produced by Layer 1 agents containing a ranked list of actions, expected KPI delta, and risk classification.
- **OutcomeRecord**: A standardized data contract recording the before/after KPIs of an executed intervention, enabling closed-loop learning.
- **PolicyChangeProposal**: A standardized data contract produced by the LearningPolicyAgent containing parameter, old_value, new_value, evidence, and rollback_plan.
- **Shadow_Mode**: An operational mode where an overlay agent observes and logs what it would do without executing any mutations or emitting external signals.
- **Gated_Mutation**: An operational mode where an overlay agent's proposed actions are routed through the ConfirmationProtocol for risk-based approval before execution.
- **Signal_Bus**: An internal pub/sub mechanism that Layer 0 agents use to publish RiskSignals and Layer 1/2 agents subscribe to for consumption.
- **Dispatch_Optimizer**: Layer 1 overlay agent responsible for global reassignment and reroute portfolio optimization each decision cycle.
- **Exception_Commander**: Layer 1 overlay agent responsible for incident triage and ranked response plan generation.
- **Revenue_Guard**: Layer 1 overlay agent responsible for detecting and reducing margin leakage patterns through policy recommendations.
- **Customer_Promise**: Layer 1 overlay agent responsible for proactive ETA trust management and customer communication policy.
- **Learning_Policy_Agent**: Layer 2 meta-overlay agent responsible for continuously tuning thresholds, risk bands, and intervention policies based on outcome data.
- **Decision_Cycle**: A periodic evaluation interval during which an overlay agent collects signals, evaluates options, and produces proposals.
- **Confidence_Score**: A numeric value (0.0–1.0) attached to each RiskSignal or InterventionProposal indicating the agent's certainty in its assessment.

## Requirements

### Requirement 1: Standardized Data Contracts for Inter-Agent Communication

**User Story:** As a platform engineer, I want all agents to communicate through standardized, versioned data contracts, so that overlay agents can interoperate with Layer 0 agents without tight coupling and all inter-agent messages are auditable.

#### Acceptance Criteria

1. THE Platform SHALL define a `RiskSignal` data contract with fields: `signal_id` (UUID), `source_agent` (str), `entity_id` (str), `entity_type` (str), `severity` (enum: low, medium, high, critical), `confidence` (float 0.0–1.0), `ttl_seconds` (int), `timestamp` (ISO datetime), `context` (dict), and `tenant_id` (str)
2. THE Platform SHALL define an `InterventionProposal` data contract with fields: `proposal_id` (UUID), `source_agent` (str), `actions` (list of action dicts), `expected_kpi_delta` (dict mapping KPI name to numeric delta), `risk_class` (enum: low, medium, high), `confidence` (float 0.0–1.0), `priority` (int), `tenant_id` (str), and `timestamp` (ISO datetime)
3. THE Platform SHALL define an `OutcomeRecord` data contract with fields: `outcome_id` (UUID), `intervention_id` (UUID reference to InterventionProposal), `before_kpis` (dict), `after_kpis` (dict), `realized_delta` (dict), `execution_duration_ms` (float), `tenant_id` (str), and `timestamp` (ISO datetime)
4. THE Platform SHALL define a `PolicyChangeProposal` data contract with fields: `proposal_id` (UUID), `source_agent` (str), `parameter` (str), `old_value` (any), `new_value` (any), `evidence` (list of OutcomeRecord references), `rollback_plan` (dict), `confidence` (float 0.0–1.0), `tenant_id` (str), and `timestamp` (ISO datetime)
5. EACH data contract SHALL include a `schema_version` field (semver string) to support forward-compatible evolution
6. THE Platform SHALL validate all data contracts against their schema before publishing to the Signal_Bus, and IF validation fails, THEN THE Platform SHALL log the validation error and discard the invalid message
7. FOR ALL valid RiskSignal objects, serializing to JSON then deserializing SHALL produce an equivalent object (round-trip property)
8. FOR ALL valid InterventionProposal objects, serializing to JSON then deserializing SHALL produce an equivalent object (round-trip property)

### Requirement 2: Signal Bus for Inter-Layer Communication

**User Story:** As a platform architect, I want a lightweight internal pub/sub mechanism for agents to publish and subscribe to signals, so that overlay agents can consume Layer 0 outputs without polling or direct coupling.

#### Acceptance Criteria

1. THE Platform SHALL implement a `SignalBus` class that supports typed publish/subscribe for RiskSignal, InterventionProposal, OutcomeRecord, and PolicyChangeProposal message types
2. WHEN a Layer 0 agent detects a condition, THE agent SHALL publish a RiskSignal to the Signal_Bus with the appropriate severity and confidence
3. THE Signal_Bus SHALL support topic-based subscription where subscribers filter by `source_agent`, `entity_type`, `severity`, or `tenant_id`
4. THE Signal_Bus SHALL deliver messages to all matching subscribers within 500ms of publication under normal load (fewer than 100 signals per second)
5. WHEN a RiskSignal's `ttl_seconds` expires, THE Signal_Bus SHALL automatically discard the signal and not deliver it to new subscribers
6. THE Signal_Bus SHALL persist published signals to the `agent_signals` Elasticsearch index for audit and replay purposes
7. IF the Signal_Bus encounters a subscriber error during delivery, THEN THE Signal_Bus SHALL log the error, skip the failing subscriber, and continue delivering to remaining subscribers
8. THE Signal_Bus SHALL expose metrics: signals_published_total (counter by type), signals_delivered_total (counter by subscriber), signals_expired_total (counter), and active_subscriptions (gauge)

### Requirement 3: Overlay Agent Base Class

**User Story:** As a developer building overlay agents, I want a shared base class that handles signal subscription, decision cycle scheduling, shadow/active mode toggling, and proposal routing, so that each overlay agent only needs to implement its domain-specific decision logic.

#### Acceptance Criteria

1. THE Platform SHALL implement an `OverlayAgentBase` class in `Agents/overlay/base_overlay_agent.py` that extends `AutonomousAgentBase` with overlay-specific capabilities
2. THE OverlayAgentBase SHALL accept a list of Signal_Bus subscriptions at initialization, specifying which signal types and filters the agent consumes
3. THE OverlayAgentBase SHALL support two operational modes: `shadow` (observe and log proposals without execution) and `active` (route proposals through ConfirmationProtocol)
4. WHEN an overlay agent is in shadow mode, THE agent SHALL log all InterventionProposals it would have submitted to the `agent_shadow_proposals` Elasticsearch index with full context
5. WHEN an overlay agent transitions from shadow mode to active mode, THE OverlayAgentBase SHALL require explicit confirmation through the AutonomyConfigService
6. THE OverlayAgentBase SHALL implement a `decision_cycle` method that collects buffered signals, invokes the subclass's `evaluate` method, and routes resulting proposals
7. THE OverlayAgentBase SHALL track per-cycle metrics: signals_consumed (int), proposals_generated (int), cycle_duration_ms (float), and mode (shadow/active)
8. EACH overlay agent subclass SHALL implement an `async evaluate(signals: List[RiskSignal]) -> List[InterventionProposal]` method containing its domain-specific decision logic

### Requirement 4: Dispatch Optimizer Overlay Agent

**User Story:** As an operations manager, I want an intelligent dispatch optimizer that considers delay signals, fuel constraints, and scheduling state to recommend optimal reassignment and reroute portfolios, so that global fleet utilization improves beyond what individual watchdog agents can achieve locally.

#### Acceptance Criteria

1. THE Dispatch_Optimizer SHALL subscribe to RiskSignals from DelayResponseAgent and FuelManagementAgent via the Signal_Bus
2. WHEN the Dispatch_Optimizer receives delay or fuel risk signals, THE agent SHALL evaluate all affected routes and jobs within the same tenant to identify reassignment opportunities
3. THE Dispatch_Optimizer SHALL produce InterventionProposals containing ranked reassignment actions with expected impact on delivery time, fuel cost, and SLA compliance
4. EACH InterventionProposal from the Dispatch_Optimizer SHALL include at minimum: the affected job IDs, proposed new asset assignments, estimated time savings, and estimated fuel cost delta
5. THE Dispatch_Optimizer SHALL not propose reassignments that would create new SLA breaches for other jobs (constraint: no net-negative SLA impact across the portfolio)
6. WHILE in shadow mode, THE Dispatch_Optimizer SHALL log all proposals with their expected KPI deltas to enable comparison against actual human dispatcher decisions
7. THE Dispatch_Optimizer SHALL execute its decision cycle at a configurable interval (default: 60 seconds) and process all buffered signals accumulated since the previous cycle
8. THE Dispatch_Optimizer SHALL write proposals through the ExecutionPlanner and ConfirmationProtocol when in active mode

### Requirement 5: Exception Commander Overlay Agent

**User Story:** As an operations lead, I want an exception commander that triages incidents from all three watchdog agents and produces ranked response plans, so that operators receive a single prioritized action list instead of fragmented alerts from multiple agents.

#### Acceptance Criteria

1. THE Exception_Commander SHALL subscribe to RiskSignals from all three Layer 0 agents (DelayResponseAgent, FuelManagementAgent, SLAGuardianAgent) via the Signal_Bus
2. WHEN multiple RiskSignals arrive within a configurable correlation window (default: 30 seconds) for related entities, THE Exception_Commander SHALL correlate them into a single incident
3. THE Exception_Commander SHALL produce InterventionProposals containing a ranked list of response actions (playbook steps) with expected impact per action
4. EACH response plan from the Exception_Commander SHALL include: incident severity, affected entities, root cause hypothesis, ranked actions, and estimated resolution time
5. THE Exception_Commander SHALL broadcast incident summaries to the AgentActivityWSManager for real-time operator visibility
6. IF an incident involves entities across multiple tenants, THEN THE Exception_Commander SHALL scope its proposals to each tenant independently and not leak cross-tenant information
7. THE Exception_Commander SHALL maintain an incident state machine with states: `detected`, `triaging`, `plan_proposed`, `executing`, `resolved`, `escalated`
8. WHEN an incident remains in `plan_proposed` state for longer than a configurable timeout (default: 5 minutes) without operator action, THE Exception_Commander SHALL escalate by increasing the incident severity and re-broadcasting

### Requirement 6: Revenue Guard Overlay Agent

**User Story:** As a finance analyst, I want an agent that detects margin leakage patterns (excessive fuel costs, SLA penalty accumulation, suboptimal routing) and proposes corrective policies, so that operational decisions are informed by their financial impact.

#### Acceptance Criteria

1. THE Revenue_Guard SHALL subscribe to RiskSignals from FuelManagementAgent and OutcomeRecords from executed interventions via the Signal_Bus
2. THE Revenue_Guard SHALL query scheduling and ops data to compute per-job and per-route margin metrics (revenue minus fuel cost minus SLA penalties)
3. WHEN the Revenue_Guard detects a pattern of margin leakage (configurable threshold: margin below target for 3+ consecutive jobs on a route), THE agent SHALL generate a PolicyChangeProposal recommending corrective action
4. EACH PolicyChangeProposal from the Revenue_Guard SHALL include: the identified leakage pattern, affected routes/jobs, estimated weekly revenue impact, proposed policy change, and a rollback plan
5. THE Revenue_Guard SHALL operate in approval-gated mode where all PolicyChangeProposals require explicit human approval before taking effect
6. THE Revenue_Guard SHALL produce weekly summary reports persisted to the `agent_revenue_reports` Elasticsearch index containing: total margin analyzed, leakage patterns detected, proposals generated, and proposals approved/rejected
7. THE Revenue_Guard SHALL not execute any mutations directly — all recommendations are policy proposals routed through the ConfirmationProtocol with HIGH risk classification
8. WHILE in shadow mode, THE Revenue_Guard SHALL log detected patterns and what it would have proposed without generating actual PolicyChangeProposals

### Requirement 7: Customer Promise Overlay Agent

**User Story:** As a customer success manager, I want an agent that proactively manages customer ETA expectations by detecting delivery risks early and triggering appropriate communication, so that customers are informed before they experience a missed delivery window.

#### Acceptance Criteria

1. THE Customer_Promise SHALL subscribe to RiskSignals from SLAGuardianAgent and delay forecasts from DelayResponseAgent via the Signal_Bus
2. WHEN the Customer_Promise detects a high-confidence (≥0.7) risk of ETA breach for a delivery, THE agent SHALL generate a communication InterventionProposal within 60 seconds of signal receipt
3. EACH communication proposal from the Customer_Promise SHALL include: affected customer/job ID, current ETA, revised ETA estimate, recommended communication channel (SMS/email/push), and message template reference
4. THE Customer_Promise SHALL not send duplicate communications for the same delivery within a configurable cooldown period (default: 30 minutes)
5. WHILE in active mode with human-approved sends, THE Customer_Promise SHALL route all communication proposals through the ConfirmationProtocol with MEDIUM risk classification
6. THE Customer_Promise SHALL track communication outcomes (delivered, opened, customer responded) via the OutcomeRecord mechanism
7. THE Customer_Promise SHALL prioritize communications based on customer tier, delivery value, and breach severity
8. IF a previously flagged delivery recovers (ETA returns within SLA window), THEN THE Customer_Promise SHALL generate a recovery notification proposal to inform the customer of the improved ETA

### Requirement 8: Learning and Policy Agent (Meta-Overlay)

**User Story:** As a platform architect, I want a meta-agent that observes intervention outcomes across all overlay agents and proposes threshold/policy adjustments, so that the system continuously improves its decision quality without manual tuning.

#### Acceptance Criteria

1. THE Learning_Policy_Agent SHALL subscribe to all OutcomeRecords and PolicyChangeProposal approval/rejection events via the Signal_Bus
2. THE Learning_Policy_Agent SHALL query the ActivityLogService and FeedbackService to correlate intervention outcomes with operator feedback
3. WHEN the Learning_Policy_Agent identifies a parameter whose current value consistently produces suboptimal outcomes (configurable: 5+ negative outcomes within 7 days), THE agent SHALL generate a PolicyChangeProposal with the recommended adjustment
4. EACH PolicyChangeProposal from the Learning_Policy_Agent SHALL include: the parameter being tuned, statistical evidence (sample size, confidence interval), proposed new value, expected improvement, and an automatic rollback plan triggered if KPIs degrade within 48 hours
5. THE Learning_Policy_Agent SHALL write policy proposals through the AutonomyConfigService with mandatory human approval (all proposals classified as HIGH risk)
6. THE Learning_Policy_Agent SHALL maintain a policy experiment log in the `agent_policy_experiments` Elasticsearch index tracking: proposal, approval status, deployment date, observed impact, and rollback status
7. THE Learning_Policy_Agent SHALL implement bounded rollout — policy changes apply to a configurable percentage of traffic (default: 10%) before full deployment
8. IF a deployed policy change causes KPI degradation exceeding a configurable threshold (default: 5% worse than baseline) within the rollback window, THEN THE Learning_Policy_Agent SHALL automatically revert the change and log the rollback reason

### Requirement 9: Shadow Mode and Graduated Activation

**User Story:** As a risk-conscious operator, I want every overlay agent to start in shadow mode and graduate to active mode only after demonstrating value, so that new agents cannot cause operational harm before they are validated.

#### Acceptance Criteria

1. WHEN a new overlay agent is deployed, THE agent SHALL start in shadow mode by default regardless of the tenant's autonomy configuration
2. WHILE in shadow mode, THE overlay agent SHALL process all signals, run its decision logic, and log proposals to the `agent_shadow_proposals` index, but SHALL NOT submit proposals to the ConfirmationProtocol or trigger any external actions
3. THE Platform SHALL provide a shadow mode comparison dashboard query that compares shadow proposals against actual operator decisions for the same time period, enabling value assessment
4. WHEN an operator activates an overlay agent (transitions from shadow to active), THE AutonomyConfigService SHALL record the activation with the activating user, timestamp, and justification
5. THE activation transition SHALL be reversible — an operator SHALL be able to return an active overlay agent to shadow mode at any time without data loss
6. THE Platform SHALL support per-tenant activation — an overlay agent can be active for one tenant while remaining in shadow mode for another
7. EACH overlay agent SHALL expose its current mode (shadow/active) and activation history through the AgentScheduler health endpoint
8. THE shadow mode proposal log SHALL retain entries for a configurable duration (default: 30 days) to support retrospective analysis

### Requirement 10: Overlay Agent Registration and Lifecycle Management

**User Story:** As a platform engineer, I want overlay agents managed by the existing AgentScheduler with the same restart policies, health reporting, and graceful shutdown as Layer 0 agents, so that operational tooling works uniformly across all agent layers.

#### Acceptance Criteria

1. THE AgentScheduler SHALL support registering overlay agents alongside Layer 0 agents with the same restart policy options (always, on_failure, never)
2. THE AgentScheduler health endpoint SHALL distinguish between Layer 0, Layer 1, and Layer 2 agents in its response, including the layer and current mode (shadow/active) for each agent
3. WHEN an overlay agent is registered with the AgentScheduler, THE scheduler SHALL validate that all declared Signal_Bus subscriptions reference valid signal types before starting the agent
4. THE bootstrap sequence SHALL initialize overlay agents after Layer 0 agents are running and the Signal_Bus is operational, ensuring signals are flowing before overlays begin consuming
5. WHEN the application shuts down, THE AgentScheduler SHALL stop overlay agents before Layer 0 agents to prevent signal consumption from stopped producers
6. THE Platform SHALL support deploying overlay agents incrementally — adding a new overlay agent SHALL NOT require restarting existing agents or the application
7. EACH overlay agent SHALL report its decision cycle metrics (signals consumed, proposals generated, cycle duration) to the ActivityLogService using the existing monitoring cycle logging pattern
8. THE AgentScheduler SHALL enforce that no more than one instance of each overlay agent type runs per application instance to prevent duplicate signal processing

### Requirement 11: Outcome Tracking and Closed-Loop Learning

**User Story:** As a data scientist, I want every intervention to be tracked from proposal through execution to measured outcome, so that the platform can quantify agent effectiveness and feed results back into decision-making.

#### Acceptance Criteria

1. WHEN an InterventionProposal is approved and executed, THE Platform SHALL create an OutcomeRecord linking the proposal to its execution result
2. THE OutcomeRecord SHALL capture before-KPIs (measured at proposal time) and after-KPIs (measured after a configurable observation window, default: 1 hour post-execution)
3. THE Platform SHALL compute `realized_delta` as the difference between after-KPIs and before-KPIs for each tracked metric
4. THE Platform SHALL persist all OutcomeRecords to the `agent_outcomes` Elasticsearch index with full traceability back to the originating proposal and source signals
5. WHEN an OutcomeRecord is created, THE Platform SHALL publish it to the Signal_Bus so that the Learning_Policy_Agent and other interested agents can consume it
6. THE Platform SHALL expose an outcome statistics API endpoint returning: total interventions, success rate (positive realized_delta), average improvement per intervention type, and agent-level effectiveness scores
7. IF an intervention produces a negative realized_delta exceeding a configurable threshold (default: 10% worse than before-KPIs), THEN THE Platform SHALL flag the outcome as `adverse` and notify the Exception_Commander
8. THE outcome tracking pipeline SHALL handle the case where after-KPIs cannot be measured (entity deleted, tenant disabled) by marking the OutcomeRecord as `inconclusive` rather than failing

### Requirement 12: Implementation Sequencing and Feature Flags

**User Story:** As a release manager, I want each overlay agent gated behind a feature flag with a defined rollout sequence, so that agents can be enabled incrementally per tenant without code deployments.

#### Acceptance Criteria

1. THE Platform SHALL gate each overlay agent behind a dedicated feature flag: `overlay.dispatch_optimizer`, `overlay.exception_commander`, `overlay.customer_promise`, `overlay.revenue_guard`, `overlay.learning_policy`
2. WHEN a feature flag is disabled for a tenant, THE corresponding overlay agent SHALL not process signals or generate proposals for that tenant, even if the agent is running globally
3. THE recommended rollout sequence SHALL be: (1) Dispatch Optimizer in shadow mode, (2) Exception Commander in shadow mode, (3) Customer Promise with human-approved sends, (4) Revenue Guard with weekly policy proposals, (5) Learning Policy with bounded rollout
4. EACH feature flag SHALL support granular states: `disabled`, `shadow`, `active_gated` (proposals require approval), and `active_auto` (proposals auto-execute per autonomy level)
5. THE Platform SHALL log all feature flag state transitions for overlay agents to the ActivityLogService with the changing user, previous state, new state, and timestamp
6. WHEN a feature flag transitions from any active state to `disabled`, THE Platform SHALL gracefully drain in-flight proposals (allow pending approvals to complete) rather than discarding them
7. THE feature flag configuration SHALL be manageable through the existing Redis-backed FeatureFlagService without requiring application restarts
8. THE Platform SHALL validate that prerequisite agents are active before allowing dependent agents to activate (e.g., Dispatch Optimizer requires DelayResponseAgent and FuelManagementAgent to be running)

