# ADR 002: Safety Confirmation Model

## Status

Accepted

## Context

The Runsheet platform's AI agents can execute mutation operations — actions that modify data in Elasticsearch, such as updating shipment statuses, reassigning jobs, adjusting fuel records, or cancelling schedules. These mutations carry varying levels of operational risk: cancelling a high-priority delivery has different consequences than updating a vehicle's location.

The platform needed a safety model to govern when AI-initiated mutations execute automatically versus when they require human approval. Several approaches were considered:

1. **No safety layer**: All agent-proposed mutations execute immediately. Simple but dangerous — a misclassified intent could trigger irreversible changes.

2. **Confirm-all**: Every mutation requires explicit human approval before execution. Safe but creates friction that defeats the purpose of autonomous agents.

3. **Binary auto/manual toggle**: A single global setting that either allows all mutations or blocks all mutations. Too coarse-grained for a multi-tenant platform with different risk tolerances.

4. **Risk-classified confirmation with approval queues**: Mutations are classified by risk level, and the execution decision is made by cross-referencing the risk level against the tenant's configured autonomy level. High-risk actions can be queued for human approval while low-risk actions execute immediately.

## Decision

We chose risk-classified confirmation with approval queues, implemented as a `ConfirmationProtocol` that wires together five components:

- **RiskRegistry**: Classifies each mutation tool into a risk level (low, medium, high) based on the tool name and its potential impact.
- **BusinessValidator**: Validates business rules before any mutation executes (e.g., checking that a job exists before cancelling it).
- **AutonomyConfigService**: Stores per-tenant autonomy level configuration, allowing each tenant to control their risk tolerance.
- **ApprovalQueueService**: Manages a queue of pending mutations that require human approval, with WebSocket notifications for real-time approval workflows.
- **ActivityLogService**: Records every mutation decision (executed, queued, or rejected) for audit and compliance.

The routing matrix maps `(risk_level × autonomy_level)` to an execute/queue decision:

| Autonomy Level | Low Risk | Medium Risk | High Risk |
|---------------|----------|-------------|-----------|
| suggest-only  | Queue    | Queue       | Queue     |
| auto-low      | Execute  | Queue       | Queue     |
| auto-medium   | Execute  | Execute     | Queue     |
| full-auto     | Execute  | Execute     | Execute   |

The processing flow for every mutation is:

1. Classify the risk level of the tool via the RiskRegistry.
2. Validate business rules via the BusinessValidator. Reject if validation fails.
3. Look up the tenant's autonomy level via AutonomyConfigService.
4. Consult the routing matrix to decide: execute immediately or queue for approval.
5. Log the decision and outcome via ActivityLogService.

## Consequences

### Positive

- **Granular control**: Each tenant can independently configure their autonomy level, allowing conservative tenants to require approval for all actions while experienced tenants can enable full automation.
- **Risk-proportional safety**: Low-risk actions (like reading data or updating non-critical fields) execute without friction, while high-risk actions (like cancelling deliveries or bulk updates) require appropriate oversight.
- **Audit trail**: Every mutation decision is logged with the risk classification, autonomy level, and outcome, providing a complete audit trail for compliance.
- **Extensible**: New risk levels, autonomy tiers, or validation rules can be added without changing the core protocol. The routing matrix is a simple data structure.
- **Real-time approval workflow**: The approval queue integrates with WebSocket notifications, allowing operators to approve or reject pending mutations in real time through the UI.

### Negative

- **Complexity**: Five interacting components (RiskRegistry, BusinessValidator, AutonomyConfigService, ApprovalQueueService, ActivityLogService) add architectural complexity compared to simpler approaches.
- **Latency for queued actions**: Mutations that require approval are not executed until a human acts on them. In time-sensitive logistics operations, this delay could be problematic.
- **Risk classification accuracy**: The RiskRegistry classifies risk based on tool names, which is a static mapping. The actual risk of a mutation may depend on its parameters (e.g., cancelling one job vs. cancelling all jobs for a tenant), which is not captured by tool-level classification alone.
- **Configuration burden**: Each tenant must have their autonomy level configured. New tenants default to a safe level, but the configuration adds an onboarding step.
