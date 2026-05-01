# ADR 003: Domain Decomposition

## Status

Accepted

## Context

The Runsheet logistics platform serves multiple operational domains: fleet/asset tracking (ops), fuel management, job scheduling, and AI agent orchestration. As the platform grew, all initialization logic, service wiring, and endpoint definitions accumulated in a single `main.py` file that reached nearly 2,000 lines.

The platform needed a clear organizational structure. Two approaches were considered:

1. **Flat module structure**: All services, models, and endpoints in a single directory tree organized by technical layer (e.g., `services/`, `models/`, `endpoints/`). Cross-domain dependencies are managed through imports.

2. **Domain-based decomposition**: Each operational domain (ops, fuel, scheduling, agents) gets its own module tree with its own services, models, API endpoints, Elasticsearch mappings, and WebSocket manager. Shared infrastructure (config, middleware, health, telemetry) lives in separate cross-cutting packages.

## Decision

We chose domain-based decomposition. The backend is organized into the following top-level packages:

```
Runsheet-backend/
├── ops/                    # Ops domain (shipments, riders, SLA)
│   ├── api/endpoints.py    # Ops HTTP endpoints
│   ├── services/           # OpsElasticsearchService, FeatureFlagService, etc.
│   ├── websocket/          # OpsWebSocketManager
│   ├── ingestion/          # Idempotency, poison queue, webhooks
│   └── models/             # Ops-specific Pydantic models
├── fuel/                   # Fuel domain
│   ├── api/endpoints.py    # Fuel HTTP endpoints
│   └── services/           # FuelService
├── scheduling/             # Scheduling domain
│   ├── api/endpoints.py    # Scheduling HTTP endpoints
│   ├── services/           # JobService, CargoService, DelayDetectionService
│   └── websocket/          # SchedulingWebSocketManager
├── Agents/                 # AI agent domain
│   ├── specialists/        # Domain-specific specialist agents
│   ├── autonomous/         # Background autonomous agents
│   ├── tools/              # Agent tool definitions
│   ├── orchestrator.py     # Request routing orchestrator
│   └── ...                 # Supporting services (memory, feedback, etc.)
├── bootstrap/              # Application initialization
│   ├── container.py        # ServiceContainer (dependency injection)
│   ├── core.py             # Core infrastructure (ES, Redis, Settings)
│   ├── middleware.py        # Middleware registration
│   ├── ops.py              # Ops domain initialization
│   ├── fuel.py             # Fuel domain initialization
│   ├── scheduling.py       # Scheduling domain initialization
│   ├── agents.py           # Agent domain initialization
│   └── agent_scheduler.py  # Autonomous agent lifecycle
├── middleware/             # Cross-cutting middleware
├── schemas/                # Shared Pydantic schemas
├── websocket/              # Shared WebSocket base class
├── services/               # Shared infrastructure services (ES client)
├── config/                 # Application configuration
├── health/                 # Health check endpoints
├── telemetry/              # Observability and metrics
└── main.py                 # App creation and lifespan (≤200 lines)
```

Key design choices:

- **Each domain owns its full stack**: A domain package contains its API endpoints, services, models, WebSocket manager, and Elasticsearch mappings. This means a developer working on fuel features only needs to look at the `fuel/` package.
- **Bootstrap modules mirror domains**: Each domain has a corresponding bootstrap module (`bootstrap/ops.py`, `bootstrap/fuel.py`, etc.) that handles its initialization and service registration.
- **Shared infrastructure is separate**: Cross-cutting concerns (middleware, schemas, config, health, telemetry) live in their own packages and are used by all domains.
- **ServiceContainer bridges domains**: When one domain needs a service from another (e.g., agents need the ops feature flag service), the dependency is resolved through the `ServiceContainer` rather than direct imports between domain packages.

## Consequences

### Positive

- **Developer focus**: A developer working on scheduling features can focus on the `scheduling/` package without understanding ops or fuel internals. The cognitive load per task is reduced.
- **Independent testability**: Each domain's services can be unit-tested in isolation by mocking the `ServiceContainer`. Bootstrap modules are independently testable.
- **Clear ownership boundaries**: In a team setting, domains can be assigned to different developers or teams with minimal merge conflicts.
- **Startup isolation**: The fail-open bootstrap design means a failure in one domain's initialization does not prevent other domains from starting. This is only possible because domains are cleanly separated.
- **Incremental migration**: The domain decomposition was achieved incrementally — each domain was extracted from `main.py` one at a time, with compatibility adapters preserving existing singleton patterns during the transition.

### Negative

- **Cross-domain operations**: Some operations span multiple domains (e.g., an agent that needs to check fuel levels before scheduling a job). These require careful dependency management through the container.
- **Duplication risk**: Similar patterns (e.g., Elasticsearch CRUD operations) may be implemented slightly differently in each domain. The shared `schemas/common.py` and `BaseWSManager` mitigate this for response shapes and WebSocket management, but service-level patterns may diverge.
- **Navigation overhead**: New developers must understand the package structure before they can find where a feature is implemented. The endpoint registry (`docs/endpoint-registry.md`) helps by mapping routes to their source locations.
- **Bootstrap ordering**: Domains have initialization dependencies (e.g., agents depend on ops feature flags). The bootstrap sequence must respect this ordering, which is defined in `bootstrap/__init__.py` as `core → middleware → ops → fuel → scheduling → agents`.
