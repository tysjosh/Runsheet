# Requirements Document

## Introduction

This document specifies the requirements for a combined platform refactoring and production hardening effort for the Runsheet logistics platform. The platform consists of a Python FastAPI backend (`Runsheet-backend/`) and a Next.js frontend (`runsheet/`). The backend has grown organically: `main.py` is nearly 2,000 lines containing all service initialization, WebSocket route handlers, lifespan management, and inline endpoint definitions. Four separate WebSocket managers (fleet, ops, scheduling, agent activity) share no common base and have inconsistent lifecycle/backpressure behavior. Three autonomous background agents run as bare `asyncio.create_task` calls with no restart policy. Services are wired together via module-level singletons and ad-hoc `configure_*()` calls rather than an explicit dependency container.

On the repository hygiene side, committed artifacts include `.coverage`, `.hypothesis/constants/` (99 files), and the `.gitignore` has overly broad patterns (e.g., `.env.*`) that exclude environment templates alongside real secrets. The `.env.development` file contains a live Elasticsearch API key. There is no CI gate enforcing minimum test coverage or route smoke tests, and no architecture decision records documenting the orchestration model, safety model, or domain decomposition choices.

This spec addresses 15 prioritized work items spanning service decomposition, dependency injection, schema unification, WebSocket hardening, autonomous agent lifecycle, repository hygiene, configuration safety, CI enforcement, endpoint documentation, and architectural governance.

## Phasing Plan

Implementation proceeds in five phases. Each phase is independently shippable and reduces risk for subsequent phases.

| Phase | Requirements | Focus | Estimated Effort |
|-------|-------------|-------|-----------------|
| **Phase 1: Hygiene & Security Baseline** | 11, 12, 13 | Remove artifacts, fix .gitignore, isolate secrets, rotate leaked keys | 1-2 days |
| **Phase 2: Bootstrap & Container** | 1, 2 | Decompose main.py, introduce ServiceContainer with compatibility adapters | 3-5 days |
| **Phase 3: Schemas & Auth Contracts** | 4, 5, 3 | Unified response schemas, auth policy matrix, endpoint registry | 3-4 days |
| **Phase 4: WS + Scheduler Hardening** | 6, 7, 8 | WS base class, backpressure, agent scheduler with restart policies, SLOs | 4-5 days |
| **Phase 5: Testing, CI & Governance** | 9, 10, 14, 15 | Integration test matrix, CI gates, ADRs, route smoke tests | 3-4 days |

## Non-Goals

The following are explicitly out of scope for this refactor:

- **No new features**: This spec does not add new user-facing functionality, endpoints, or agent capabilities.
- **No database migration**: Elasticsearch index mappings and data models are not changed.
- **No frontend refactoring**: The Next.js frontend is only affected by .gitignore changes and schema alignment; no component restructuring.
- **No Python version upgrade**: The runtime stays on the current Python version.
- **No infrastructure changes**: Deployment topology (Cloud Run, Elasticsearch, Redis) is unchanged.
- **No API versioning scheme**: Req 4 introduces unified schemas with a deprecation window but does not introduce URL-based API versioning (e.g., `/v1/`, `/v2/`).

## Rollback Criteria

For high-risk changes, the following rollback triggers apply:

| Change | Rollback Trigger | Rollback Action |
|--------|-----------------|-----------------|
| ServiceContainer (Req 2) | >5% of existing tests fail after migration | Revert to singleton pattern; compatibility adapters remain |
| WS Manager base class (Req 6) | Any WS endpoint fails connection smoke test | Revert to per-manager implementations; keep metrics additions |
| Agent Scheduler (Req 7) | Any agent fails to start or restart within 30s | Revert to bare `asyncio.create_task`; keep health endpoint |
| Unified schemas (Req 4) | Frontend integration tests fail on response parsing | Revert affected endpoints; keep schema definitions for new endpoints only |

## Glossary

- **Bootstrap_Module**: A Python module within a `bootstrap/` package that encapsulates the initialization logic for a specific domain (ops, fuel, scheduling, agents) currently inlined in `main.py`.
- **Dependency_Container**: An explicit registry object that holds all service instances and their wiring, replacing the current pattern of module-level singletons and `configure_*()` functions.
- **Endpoint_Registry**: An auto-generated documentation artifact listing every HTTP route and WebSocket endpoint with its path, method, auth requirements, rate limits, and request/response schemas.
- **Unified_Schema**: A set of shared Pydantic base models for common request/response patterns (pagination, error responses, list envelopes, tenant-scoped queries) used consistently across all domain routers.
- **Auth_Middleware_Contract**: A documented interface specifying how authentication (JWT verification), tenant scoping, and feature flag gating are applied to endpoints, with explicit opt-in/opt-out declarations per router.
- **WS_Manager**: Any of the four WebSocket connection managers (fleet `ConnectionManager`, `OpsWebSocketManager`, `SchedulingWebSocketManager`, `AgentActivityWSManager`) that manage client connections and broadcast events.
- **Backpressure_Policy**: A mechanism that limits the rate of outbound WebSocket messages per client or drops messages when a client's send buffer exceeds a threshold, preventing slow consumers from degrading server performance.
- **Connection_Lifecycle_Metric**: A Prometheus/OpenTelemetry metric tracking WebSocket connection events: connects, disconnects, message counts, send failures, and active connection gauge per manager.
- **Agent_Scheduler**: A framework component that manages the lifecycle of autonomous background agents (start, stop, restart, health check) with configurable restart policies, replacing bare `asyncio.create_task` calls.
- **SLO**: Service Level Objective — a target for operational behavior such as restart latency, maximum downtime, or recovery time for background agents and periodic jobs.
- **ADR**: Architecture Decision Record — a lightweight document capturing the context, decision, and consequences of a significant architectural choice.
- **CI_Gate**: An automated check in the continuous integration pipeline that must pass before code is merged, such as minimum test coverage or route smoke tests.
- **Compatibility_Adapter**: A thin wrapper that preserves the existing `get_*()` singleton API while delegating to the ServiceContainer internally, enabling staged migration without breaking existing code.
- **Smoke_Test_Fixture**: A per-route test fixture providing the minimal valid request payload (headers, query params, JSON body) needed to invoke an endpoint without triggering a 422 validation error.
- **Deprecation_Window**: A 60-day period during which old response shapes are still returned alongside the new unified schema, giving consumers time to migrate.

## Requirements

### Requirement 1: Service Decomposition — Break main.py into Bootstrap Modules

**User Story:** As a backend developer, I want the application startup logic organized into domain-specific bootstrap modules, so that I can understand, modify, and test each domain's initialization independently without navigating a 2,000-line file.

#### Acceptance Criteria

1. THE Backend_Service SHALL organize startup logic into a `Runsheet-backend/bootstrap/` package with separate modules: `ops.py`, `fuel.py`, `scheduling.py`, `agents.py`, `middleware.py`, and `core.py`
2. EACH Bootstrap_Module SHALL expose an `async def initialize(app, container)` function that accepts the FastAPI app and the Dependency_Container and returns nothing
3. THE `main.py` file SHALL contain only the FastAPI app creation, lifespan context manager (delegating to bootstrap modules), middleware registration, and router inclusion — no service instantiation or `configure_*()` calls
4. WHEN the application starts, THE lifespan context manager SHALL call each Bootstrap_Module's `initialize` function in a defined dependency order: core → middleware → ops → fuel → scheduling → agents
5. WHEN a Bootstrap_Module's `initialize` function raises an exception, THE lifespan context manager SHALL log the error with the module name and continue starting remaining modules without crashing the application
6. THE refactored `main.py` SHALL not exceed 200 lines of code excluding imports and comments
7. EACH Bootstrap_Module SHALL be independently unit-testable by mocking the Dependency_Container
8. THE application startup time SHALL not increase by more than 10% compared to the current monolithic `main.py` (measured as time from process start to first request served)

#### Definition of Done

- Files: `bootstrap/__init__.py`, `bootstrap/core.py`, `bootstrap/middleware.py`, `bootstrap/ops.py`, `bootstrap/fuel.py`, `bootstrap/scheduling.py`, `bootstrap/agents.py`, refactored `main.py`
- Tests: Unit test per bootstrap module verifying `initialize()` with mocked container; integration test verifying full startup sequence
- Docs: Updated README startup section
- CI: All existing 1449+ tests pass; `main.py` line count verified ≤200

### Requirement 2: Explicit Dependency Container for Services

**User Story:** As a backend developer, I want all service instances registered in a single dependency container, so that I can trace service wiring, swap implementations for testing, and eliminate hidden coupling through module-level singletons.

#### Acceptance Criteria

1. THE Backend_Service SHALL implement a `ServiceContainer` class in `bootstrap/container.py` that stores all service instances as typed attributes
2. THE ServiceContainer SHALL register services during bootstrap and provide typed access via attribute lookup (e.g., `container.fuel_service`, `container.ops_es_service`)
3. THE ServiceContainer SHALL support a `get(service_name)` method that raises a descriptive `KeyError` if the requested service has not been registered
4. WHEN used in tests, THE ServiceContainer SHALL accept mock or stub implementations for any registered service without modifying production code
5. THE ServiceContainer SHALL be stored on `app.state.container` so that FastAPI dependency injection and endpoint handlers can access it
6. THE migration SHALL proceed in two stages: (a) introduce Compatibility_Adapters that preserve existing `get_*()` singleton APIs while delegating to the container internally, then (b) migrate callers to use the container directly and remove adapters
7. DURING the adapter stage, ALL existing module-level singleton patterns (`get_connection_manager()`, `get_ops_ws_manager()`, `get_scheduling_ws_manager()`, `get_agent_ws_manager()`) SHALL continue to work unchanged
8. THE hard cut (removing adapters) SHALL only proceed after all existing tests pass with the container as the sole source of truth

#### Definition of Done

- Files: `bootstrap/container.py`, compatibility adapter wrappers in each singleton module
- Tests: Unit tests for ServiceContainer (register, get, missing key, mock injection); integration test verifying adapters delegate correctly
- CI: All existing tests pass in both adapter and post-adapter states

### Requirement 3: Endpoint Registry Documentation from Code

**User Story:** As a developer onboarding to the project, I want an auto-generated endpoint registry that lists every HTTP and WebSocket route with its auth requirements and schemas, so that I can understand the API surface without reading every router file.

#### Acceptance Criteria

1. THE Backend_Service SHALL include a script `scripts/generate_endpoint_registry.py` that introspects the FastAPI app and produces a Markdown document listing all registered routes
2. EACH entry in the Endpoint_Registry SHALL include: HTTP method, path, router prefix, authentication requirement (JWT/API key/none), rate limit, and request/response schema names
3. THE Endpoint_Registry SHALL include all WebSocket endpoints with their path, subscription types, and authentication requirements
4. WHEN a new router is added to the application, THE generation script SHALL automatically include its routes without manual updates to the script
5. THE generated Endpoint_Registry document SHALL be written to `docs/endpoint-registry.md` in the repository root

#### Definition of Done

- Files: `scripts/generate_endpoint_registry.py`, `docs/endpoint-registry.md`
- Tests: Unit test verifying script output includes all known routes; test that generated doc is not stale (CI check)
- CI: Registry generation runs in CI and fails if output differs from committed version

### Requirement 4: Unified Request/Response Schemas Across Domains

**User Story:** As a frontend developer, I want consistent response shapes across all API domains (ops, fuel, scheduling, agents), so that I can write generic data-fetching utilities instead of per-domain parsers.

#### Acceptance Criteria

1. THE Backend_Service SHALL define shared base schemas in `Runsheet-backend/schemas/common.py`: `PaginatedResponse[T]`, `ErrorResponse`, `ListEnvelope[T]`, and `TenantScopedRequest`
2. THE `PaginatedResponse` schema SHALL include fields: `items` (list), `total` (int), `page` (int), `page_size` (int), and `has_next` (bool)
3. THE `ErrorResponse` schema SHALL include fields: `error_code` (str), `message` (str), `details` (optional dict), and `request_id` (str)
4. ALL paginated list endpoints across ops, fuel, scheduling, and agent routers SHALL return responses conforming to the `PaginatedResponse` schema
5. ALL error responses across all routers SHALL conform to the `ErrorResponse` schema, replacing any ad-hoc error dictionaries
6. WHEN a domain endpoint currently returns a non-conforming response shape, THE refactoring SHALL maintain backward compatibility for a 60-day Deprecation_Window by returning both the old field names and the new unified field names in the same response body (dual-field approach, not URL versioning)
7. AFTER the 60-day Deprecation_Window, THE old field names SHALL be removed and only the unified schema fields SHALL be returned
8. THE deprecation timeline SHALL be documented in `docs/schema-migration.md` with the start date, affected endpoints, old vs new field mappings, and removal date

#### Definition of Done

- Files: `schemas/common.py`, `docs/schema-migration.md`, updated response models in each domain router
- Tests: Unit tests for each shared schema; parametrized test verifying all list endpoints return `PaginatedResponse`-conforming JSON
- CI: Schema conformance test runs in CI

### Requirement 5: Centralized Auth/Tenant Policy Middleware Contracts

**User Story:** As a security engineer, I want a single, documented contract for how authentication and tenant scoping are applied to every endpoint, so that I can audit coverage and ensure no endpoint is accidentally unprotected.

#### Acceptance Criteria

1. THE Backend_Service SHALL define an `AuthPolicy` enum with values: `JWT_REQUIRED`, `API_KEY_REQUIRED`, `WEBHOOK_HMAC`, and `PUBLIC`
2. EACH FastAPI router SHALL declare its default `AuthPolicy` and any per-route overrides using a decorator or dependency
3. THE Backend_Service SHALL implement a middleware or dependency that enforces the declared `AuthPolicy` for every request, rejecting unauthenticated requests with a 401 status code and a conforming `ErrorResponse`
4. THE Backend_Service SHALL implement tenant scoping as a composable dependency that extracts `tenant_id` from the JWT claims and injects it into the request context
5. WHEN a router is registered without an explicit `AuthPolicy` declaration, THE middleware SHALL default to `JWT_REQUIRED` and log a warning at startup identifying the unprotected router
6. THE auth middleware contract SHALL be documented in `docs/auth-contract.md` including an explicit policy matrix table:

| Router | Default Policy | Exceptions |
|--------|---------------|------------|
| `/api/scheduling/*` | JWT_REQUIRED | none |
| `/api/ops/*` | JWT_REQUIRED | none |
| `/api/ops/admin/*` | JWT_REQUIRED (admin role) | none |
| `/api/fuel/*` | JWT_REQUIRED | none |
| `/api/agent/*` | JWT_REQUIRED | `GET /agent/health` → PUBLIC |
| `/api/chat` | JWT_REQUIRED | none |
| `/api/chat/clear` | JWT_REQUIRED | none |
| `/api/data/*` | JWT_REQUIRED | none |
| `/ws/*` | JWT_REQUIRED (via query param or first message) | `/ws/agent-activity` → PUBLIC for read-only |
| `/health` | PUBLIC | — |
| `/docs`, `/openapi.json` | PUBLIC | — |

7. THE policy matrix SHALL be validated at startup by a check that compares declared policies against registered routes and logs any mismatches

#### Definition of Done

- Files: `middleware/auth_policy.py`, `docs/auth-contract.md`, updated router declarations
- Tests: Unit test per policy type; integration test verifying unauthenticated requests to JWT_REQUIRED routes return 401; test verifying PUBLIC routes accept unauthenticated requests
- CI: Startup policy validation check runs in CI

### Requirement 6: WebSocket Manager Hardening with Lifecycle Metrics and Backpressure

**User Story:** As an SRE, I want all four WebSocket managers to emit connection lifecycle metrics and enforce backpressure on slow consumers, so that I can monitor WebSocket health and prevent a single slow client from degrading broadcast performance.

#### Acceptance Criteria

1. EACH WS_Manager SHALL emit Prometheus-compatible metrics for: active connections (gauge), total connections (counter), total disconnections (counter), messages sent (counter), and send failures (counter), labeled by manager name and tenant_id
2. EACH WS_Manager SHALL implement a Backpressure_Policy that drops messages for a client when its pending send queue exceeds a configurable threshold (default: 100 messages)
3. WHEN a client's messages are dropped due to backpressure, THE WS_Manager SHALL log a warning with the client identifier and drop count, and increment a `messages_dropped` counter metric
4. EACH WS_Manager SHALL track and expose the time since last successful send per client, enabling stale client detection across all managers (not just `OpsWebSocketManager`)
5. THE fleet `ConnectionManager` SHALL be upgraded to support tenant-scoped connections and subscription filtering, matching the capabilities of `OpsWebSocketManager`
6. ALL four WS_Managers SHALL share a common base class `BaseWSManager` that defines the standard lifecycle methods: `connect`, `disconnect`, `broadcast`, `shutdown`, and `get_connection_count`, plus metric emission hooks
7. WHEN a WebSocket client disconnects unexpectedly (network error, timeout), THE WS_Manager SHALL clean up the connection within 5 seconds and decrement the active connections metric
8. THE WS broadcast latency under 50 concurrent clients SHALL not exceed 100ms (p99), verified by a benchmark test in CI
9. THE WebSocket handshake contract SHALL be standardized: all WS endpoints SHALL send a JSON `{"type": "connection", "status": "connected", ...}` message upon successful connection. Smoke tests SHALL verify this message is received within 2 seconds of connection.

#### Definition of Done

- Files: `websocket/base_ws_manager.py`, updated fleet/ops/scheduling/agent WS managers extending base class
- Tests: Unit tests for base class; per-manager tests verifying metrics emission, backpressure drop behavior, and stale client cleanup; benchmark test for broadcast latency
- CI: WS smoke tests and benchmark run in CI

### Requirement 7: Autonomous Agent Scheduler/Runner Framework

**User Story:** As a platform engineer, I want a centralized agent scheduler that manages the lifecycle of all autonomous background agents with restart policies and health reporting, so that agent failures are automatically recovered and visible in monitoring.

#### Acceptance Criteria

1. THE Backend_Service SHALL implement an `AgentScheduler` class in `bootstrap/agent_scheduler.py` that manages the lifecycle of all autonomous agents (Delay_Response_Agent, Fuel_Management_Agent, SLA_Guardian_Agent)
2. THE AgentScheduler SHALL support configurable restart policies per agent: `always` (restart on any exit), `on_failure` (restart only on unhandled exception), and `never` (do not restart)
3. WHEN an autonomous agent's background task exits due to an unhandled exception, THE AgentScheduler SHALL restart the agent within 5 seconds if the restart policy permits, up to a configurable maximum of 3 consecutive restarts within a 5-minute window
4. IF an agent exceeds the maximum restart count within the restart window, THEN THE AgentScheduler SHALL mark the agent as `failed` and emit a critical alert via the telemetry service
5. THE AgentScheduler SHALL expose a `get_health()` method returning the status of each managed agent: `running`, `stopped`, `restarting`, or `failed`, along with uptime, restart count, and last error message
6. THE AgentScheduler SHALL be the sole owner of agent background tasks, replacing the current pattern of bare `asyncio.create_task` calls in the lifespan function
7. WHEN the application shuts down, THE AgentScheduler SHALL stop all agents gracefully with a configurable timeout (default: 10 seconds) before force-cancelling

#### Definition of Done

- Files: `bootstrap/agent_scheduler.py`, updated `bootstrap/agents.py` to use scheduler
- Tests: Unit tests for restart policies (always/on_failure/never), max restart window, health reporting, graceful shutdown; integration test verifying agent recovery after simulated crash
- CI: All agent-related tests pass; scheduler health endpoint returns valid JSON

### Requirement 8: Operational SLOs for Agents and Periodic Jobs

**User Story:** As an operations lead, I want defined SLOs for autonomous agent restart behavior and periodic job execution, so that I can set alerting thresholds and measure platform reliability.

#### Acceptance Criteria

1. THE Backend_Service SHALL define SLOs for each autonomous agent: maximum time from crash to restart (target: 5 seconds), maximum consecutive failures before escalation (target: 3), and minimum uptime percentage per 24-hour window (target: 99%)
2. THE Backend_Service SHALL define SLOs for the periodic delay detection job: maximum interval drift from configured schedule (target: ±10%), and maximum execution duration per cycle (target: 5 seconds)
3. THE AgentScheduler SHALL record restart events with timestamps in the Agent_Activity_Log, enabling SLO compliance reporting
4. WHEN an agent's uptime falls below the defined SLO threshold within a rolling 24-hour window, THE telemetry service SHALL emit a warning alert
5. THE SLO definitions SHALL be documented in `docs/operational-slos.md` with target values, measurement methods, and alerting thresholds
6. THE CI pipeline SHALL include a benchmark test that verifies agent startup time is under 5 seconds and a single monitoring cycle completes within the SLO budget

#### Definition of Done

- Files: `docs/operational-slos.md`, SLO constants in `bootstrap/agent_scheduler.py`, telemetry alert integration
- Tests: Unit test verifying SLO threshold alerts fire correctly; benchmark test for startup and cycle time
- CI: SLO benchmark runs in CI

### Requirement 9: Integration Test Matrix for Feature Flags and Tenant Disable Flows

**User Story:** As a QA engineer, I want a comprehensive integration test suite covering feature flag toggling and tenant disable flows across all endpoints and WebSocket channels, so that tenant isolation is verified end-to-end before every release.

#### Acceptance Criteria

1. THE test suite SHALL include integration tests that verify: enabling a tenant flag allows access to all ops, fuel, scheduling, and agent endpoints for that tenant
2. THE test suite SHALL include integration tests that verify: disabling a tenant flag returns 403 for all gated HTTP endpoints and closes existing WebSocket connections with code 4403
3. THE test suite SHALL include integration tests that verify: autonomous agents skip processing for disabled tenants and log the skip reason
4. THE test suite SHALL include integration tests that verify: re-enabling a previously disabled tenant restores full access without requiring application restart
5. THE test suite SHALL cover the interaction matrix: each of the four WebSocket managers (fleet, ops, scheduling, agent activity) × tenant enabled/disabled states
6. THE test suite SHALL include smoke tests that verify every registered HTTP route returns a non-500 response when called with valid authentication and minimal valid input
7. ALL integration tests SHALL be runnable in CI without external service dependencies by using mocked Elasticsearch and Redis backends

#### Definition of Done

- Files: `tests/integration/test_feature_flag_matrix.py`, `tests/integration/test_tenant_disable_flows.py`
- Tests: ≥40 integration tests covering the full matrix; all pass with mocked backends
- CI: Integration tests run as a separate CI job

### Requirement 10: CI Gates for Coverage and Route Smoke Tests

**User Story:** As a tech lead, I want CI to enforce minimum test coverage and route smoke tests on every pull request, so that regressions in test coverage or broken endpoints are caught before merge.

#### Acceptance Criteria

1. THE CI pipeline SHALL run the full test suite with coverage measurement on every pull request
2. THE CI pipeline SHALL fail the build if overall line coverage drops below a configurable threshold (initial target: 70%)
3. THE CI pipeline SHALL fail the build if any changed source file (files in `Runsheet-backend/` matching `*.py`, excluding `tests/`, `scripts/`, and `__pycache__/`) introduced in the PR has zero test coverage. Coverage is measured on changed lines, not entire files, to avoid noisy failures on large pre-existing files.
4. THE CI pipeline SHALL run route smoke tests that verify every registered HTTP endpoint returns a non-500 status code with valid authentication
5. THE CI pipeline SHALL run route smoke tests that verify every WebSocket endpoint accepts a connection and responds within 2 seconds (connection confirmation message if the endpoint sends one, or simply a successful connection upgrade)
6. IF a CI gate fails, THEN THE pipeline SHALL report the specific failure reason (coverage delta, failing route, missing tests) in the pull request summary
7. THE CI configuration SHALL be defined in `.github/workflows/ci.yml` committed to the repository

#### Definition of Done

- Files: `.github/workflows/ci.yml`, `scripts/check_coverage.py` (coverage gate logic), `tests/smoke/test_route_smoke.py`
- Tests: CI config validated by running locally with `act` or equivalent; smoke tests pass against mocked app
- CI: Pipeline runs end-to-end on a test PR

### Requirement 11: Repository Hygiene — Remove Committed Runtime Artifacts

**User Story:** As a developer, I want the repository free of committed runtime and build artifacts, so that clone times are fast, diffs are clean, and the repository only contains source code and configuration.

#### Acceptance Criteria

1. THE repository SHALL not contain committed `.coverage` files, `.hypothesis/` directories, or `coverage_html/` directories
2. THE `.gitignore` SHALL include explicit rules for: `.coverage`, `.hypothesis/`, `coverage_html/`, `*.pyc`, `__pycache__/`, `.pytest_cache/`, `htmlcov/`, and `Runsheet-backend/.hypothesis/`
3. THE `.gitignore` SHALL include explicit rules for frontend build artifacts: `runsheet/.next/`, `runsheet/coverage/`, `runsheet/test-results/`, `runsheet/playwright-report/`
4. WHEN the hygiene cleanup is applied, THE committed artifacts SHALL be removed from the git index using `git rm --cached` so that git history is not rewritten
5. THE `.gitignore` SHALL not use overly broad patterns that accidentally exclude files needed for development (e.g., `.env.example` must remain tracked while `.env.local` and `.env.development` are excluded)

#### Definition of Done

- Files: Updated `.gitignore`, `git rm --cached` commands executed
- Tests: `git status` shows no tracked artifacts matching ignore rules
- CI: A CI check verifies no ignored patterns are tracked (`git ls-files -i --exclude-standard` returns empty)

### Requirement 12: Configuration Isolation — Secrets Never Committed

**User Story:** As a security engineer, I want to verify that no real secrets (API keys, JWT secrets, webhook secrets) are committed in any environment configuration file, so that credential exposure through version control is prevented.

#### Acceptance Criteria

1. THE repository SHALL track only `.env.example` files containing placeholder values (e.g., `your-api-key-here`) and no real credentials
2. THE `.gitignore` SHALL exclude `.env.local`, `.env.development`, `.env.staging`, `.env.production`, and `runsheet/.env.local` from version control
3. IF the `.env.development` file currently contains real API keys or secrets, THEN THE cleanup SHALL follow this remediation process: (a) identify all exposed credentials, (b) remove the file from git tracking, (c) rotate all exposed credentials within 24 hours, (d) document the rotation in a `docs/incident-log.md` entry with date, affected credentials, rotation status, and responsible party
4. THE repository SHALL include a `scripts/check-secrets.sh` script (or pre-commit hook) that scans staged files for patterns matching API keys, JWT secrets, and connection strings, and blocks the commit if matches are found
5. THE `.env.example` files SHALL document every required environment variable with a description, expected format, and whether it is required or optional
6. WHEN a developer clones the repository, THE README SHALL instruct them to copy `.env.example` to `.env.development` and fill in their own credentials

#### Definition of Done

- Files: Updated `.gitignore`, `scripts/check-secrets.sh`, updated `.env.example` files with full documentation, `docs/incident-log.md` (if rotation needed), updated README
- Tests: Pre-commit hook blocks a test commit containing a fake API key pattern
- CI: Secret scan runs on every PR; `.env.example` completeness check runs in CI

### Requirement 13: Git Hygiene Pass and .gitignore Correction

**User Story:** As a developer, I want a correct and comprehensive `.gitignore` that prevents accidental commits of generated files while preserving all necessary source and configuration files.

#### Acceptance Criteria

1. THE `.gitignore` SHALL be organized into clearly labeled sections: Python, Node.js/Next.js, IDE/Editor, OS, Testing/Coverage, Environment, and Build Artifacts
2. THE `.gitignore` SHALL not contain rules that exclude tracked configuration templates (`.env.example`, `.coveragerc`, `pytest.ini`)
3. THE `.gitignore` SHALL include rules for Python-specific artifacts: `*.pyc`, `__pycache__/`, `*.egg-info/`, `dist/`, `build/`, `.eggs/`, `*.so`
4. THE `.gitignore` SHALL include rules for Node.js artifacts: `node_modules/`, `.next/`, `out/`, `coverage/`, `.swc/`
5. THE `.gitignore` SHALL include rules for test artifacts: `.coverage`, `htmlcov/`, `coverage_html/`, `.hypothesis/`, `.pytest_cache/`, `test-results/`, `playwright-report/`
6. THE `.gitignore` SHALL include rules for virtual environments: `venv/`, `.venv/`, `env/`
7. WHEN the corrected `.gitignore` is applied, THE repository SHALL verify that no previously tracked source files are accidentally untracked by comparing `git ls-files` output before and after (only generated artifacts should be removed)

#### Definition of Done

- Files: Updated `.gitignore`
- Tests: Before/after `git ls-files` diff shows only artifact removals, no source file losses
- CI: `.gitignore` lint check verifies no tracked files match ignore patterns

### Requirement 14: Architecture Decision Records for Key Choices

**User Story:** As a new team member, I want documented architecture decision records explaining why the platform uses its current orchestration model, safety model, and domain decomposition, so that I can understand the rationale behind design choices without reverse-engineering the code.

#### Acceptance Criteria

1. THE repository SHALL contain a `docs/adr/` directory with Architecture Decision Records following the standard ADR format: Title, Status, Context, Decision, Consequences
2. THE repository SHALL include an ADR for the agent orchestration model: why keyword-based routing to specialist agents was chosen over a single monolithic agent
3. THE repository SHALL include an ADR for the safety/confirmation model: why risk-classified confirmation with approval queues was chosen over simpler approaches
4. THE repository SHALL include an ADR for the domain decomposition: why ops, fuel, scheduling, and agents are separate module trees with their own services, models, and endpoints
5. THE repository SHALL include an ADR for the WebSocket architecture: why four separate managers exist instead of a unified pub/sub system
6. THE repository SHALL include an ADR for the data layer choice: why Elasticsearch is used as the primary data store instead of a relational database
7. EACH ADR SHALL be numbered sequentially (e.g., `001-agent-orchestration-model.md`) and linked from a `docs/adr/README.md` index file

#### Definition of Done

- Files: `docs/adr/README.md`, `docs/adr/001-agent-orchestration-model.md`, `docs/adr/002-safety-confirmation-model.md`, `docs/adr/003-domain-decomposition.md`, `docs/adr/004-websocket-architecture.md`, `docs/adr/005-elasticsearch-data-layer.md`
- Tests: None (documentation only)
- CI: ADR index file existence check

### Requirement 15: Formalize Endpoint Registry with Route Smoke Tests

**User Story:** As a platform engineer, I want every registered route verified by an automated smoke test, so that broken imports, missing dependencies, or misconfigured middleware are caught immediately in CI.

#### Acceptance Criteria

1. THE test suite SHALL include a parametrized smoke test that iterates over every HTTP route registered in the FastAPI app and sends a minimal valid request using a Smoke_Test_Fixture registry
2. THE Smoke_Test_Fixture registry SHALL be a dict mapping route paths to their minimal valid request payloads (headers, query params, JSON body). For GET routes, the fixture provides query params only. For POST/PATCH/PUT routes, the fixture provides a minimal valid JSON body derived from the route's Pydantic request model using `schema_json()` with default/example values.
3. EACH smoke test SHALL verify that the endpoint returns a status code other than 500 (Internal Server Error), accepting 200, 201, 400, 401, 403, or 404 as valid responses
4. THE smoke test SHALL cover all WebSocket endpoints by verifying that a connection can be established and, if the endpoint sends a connection confirmation message, that it is received within 2 seconds. Endpoints that do not send a confirmation message SHALL pass if the connection upgrade succeeds.
5. WHEN a new router is added to the application, THE smoke test SHALL automatically include its routes without manual test updates, by introspecting `app.routes`. Routes without a fixture entry SHALL use a default empty-body request and are expected to return 400 or 422 (not 500).
6. IF a smoke test fails for a specific route, THEN THE test output SHALL include the route path, HTTP method, response status code, and response body to aid debugging
7. THE smoke tests SHALL run with mocked external dependencies (Elasticsearch, Redis, Google Cloud) so they execute in under 30 seconds total

#### Definition of Done

- Files: `tests/smoke/test_route_smoke.py`, `tests/smoke/fixtures.py` (fixture registry)
- Tests: Smoke tests cover all registered routes; all pass with mocked backends
- CI: Smoke tests run as part of the main CI pipeline

## Traceability Matrix

| Req | Primary Files to Change | Test Files | Phase |
|-----|------------------------|------------|-------|
| 1 | `main.py`, `bootstrap/*.py` | `tests/unit/test_bootstrap_*.py` | 2 |
| 2 | `bootstrap/container.py`, singleton modules | `tests/unit/test_container.py` | 2 |
| 3 | `scripts/generate_endpoint_registry.py` | `tests/unit/test_endpoint_registry.py` | 3 |
| 4 | `schemas/common.py`, all domain routers | `tests/unit/test_unified_schemas.py` | 3 |
| 5 | `middleware/auth_policy.py`, all routers | `tests/unit/test_auth_policy.py` | 3 |
| 6 | `websocket/base_ws_manager.py`, 4 WS managers | `tests/unit/test_ws_*.py` | 4 |
| 7 | `bootstrap/agent_scheduler.py` | `tests/unit/test_agent_scheduler.py` | 4 |
| 8 | `docs/operational-slos.md`, scheduler | `tests/benchmark/test_slo_compliance.py` | 4 |
| 9 | `tests/integration/test_feature_flag_matrix.py` | (self) | 5 |
| 10 | `.github/workflows/ci.yml`, `scripts/` | `tests/smoke/test_route_smoke.py` | 5 |
| 11 | `.gitignore` | git verification commands | 1 |
| 12 | `.gitignore`, `.env.*`, `scripts/check-secrets.sh` | pre-commit hook test | 1 |
| 13 | `.gitignore` | git verification commands | 1 |
| 14 | `docs/adr/*.md` | none (docs only) | 5 |
| 15 | `tests/smoke/test_route_smoke.py`, `tests/smoke/fixtures.py` | (self) | 5 |
