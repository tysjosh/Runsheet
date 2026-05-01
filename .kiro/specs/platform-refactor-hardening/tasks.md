# Implementation Plan: Platform Refactor & Hardening

## Overview

This implementation plan covers the combined platform refactoring and production hardening effort for the Runsheet logistics platform. Work is organized into five independently shippable phases: Hygiene & Security Baseline → Bootstrap & Container → Schemas & Auth Contracts → WS + Scheduler Hardening → Testing, CI & Governance. Each phase reduces risk for subsequent phases and can be validated in isolation.

## Tasks

### Phase 1: Hygiene & Security Baseline (Reqs 11, 12, 13)

- [x] 1. Remove committed runtime artifacts from the repository (Req 11)
  - [x] 1.1 Remove `.coverage` file from git tracking using `git rm --cached .coverage`
    - Verify the file is no longer in the git index
    - _Requirements: 11.1_

  - [x] 1.2 Remove `.hypothesis/` directories from git tracking using `git rm --cached -r Runsheet-backend/.hypothesis/`
    - Remove all 99+ constant files and unicode data from the index
    - _Requirements: 11.1_

  - [x] 1.3 Remove `coverage_html/` directory from git tracking using `git rm --cached -r Runsheet-backend/coverage_html/`
    - Remove all 171 generated HTML coverage report files from the index
    - _Requirements: 11.1_

  - [x] 1.4 Remove any other `__pycache__/` and `.pytest_cache/` directories from git tracking
    - Scan for all tracked `__pycache__/` and `.pytest_cache/` dirs and remove with `git rm --cached -r`
    - _Requirements: 11.1_

  - [x] 1.5 Verify artifact removal with `git status` showing only index changes, no source file losses
    - Compare `git ls-files` before and after to confirm only generated artifacts were removed
    - _Requirements: 11.4, 11.5_

- [x] 2. Correct and reorganize `.gitignore` (Req 13)
  - [x] 2.1 Restructure `.gitignore` into clearly labeled sections
    - Organize into sections: Python, Node.js/Next.js, IDE/Editor, OS, Testing/Coverage, Environment, Build Artifacts
    - _Requirements: 13.1_

  - [x] 2.2 Add Python-specific artifact rules
    - Add rules for: `*.pyc`, `__pycache__/`, `*.egg-info/`, `dist/`, `build/`, `.eggs/`, `*.so`
    - _Requirements: 13.3_

  - [x] 2.3 Add Node.js/Next.js artifact rules
    - Add rules for: `node_modules/`, `.next/`, `out/`, `coverage/`, `.swc/`
    - Ensure `runsheet/.next/`, `runsheet/coverage/`, `runsheet/test-results/`, `runsheet/playwright-report/` are covered
    - _Requirements: 13.4, 11.3_

  - [x] 2.4 Add testing and coverage artifact rules
    - Add rules for: `.coverage`, `htmlcov/`, `coverage_html/`, `.hypothesis/`, `.pytest_cache/`, `test-results/`, `playwright-report/`
    - Include `Runsheet-backend/.hypothesis/` explicitly
    - _Requirements: 13.5, 11.2_

  - [x] 2.5 Add virtual environment rules
    - Add rules for: `venv/`, `.venv/`, `env/`
    - _Requirements: 13.6_

  - [x] 2.6 Ensure `.gitignore` does not exclude tracked configuration templates
    - Verify `.env.example`, `.coveragerc`, `pytest.ini` are NOT matched by ignore rules
    - Add negation rules if needed (e.g., `!.env.example`)
    - _Requirements: 13.2_

  - [x] 2.7 Verify corrected `.gitignore` with `git ls-files -i --exclude-standard` returning empty
    - Run verification to confirm no tracked files match the new ignore patterns
    - _Requirements: 13.7_

- [x] 3. Isolate secrets and fix environment configuration (Req 12)
  - [x] 3.1 Audit `.env.development` for real API keys and secrets
    - Identify all exposed credentials (Elasticsearch API key, JWT secrets, webhook secrets)
    - Document findings for the incident log
    - _Requirements: 12.3_

  - [x] 3.2 Update `.gitignore` to exclude real environment files
    - Add rules for: `.env.local`, `.env.development`, `.env.staging`, `.env.production`, `runsheet/.env.local`
    - Ensure `.env.example` files remain tracked
    - _Requirements: 12.2_

  - [x] 3.3 Remove real environment files from git tracking
    - Run `git rm --cached` on `.env.development`, `.env.staging`, `.env.production`, `runsheet/.env.local`
    - Keep `.env.example` files tracked
    - _Requirements: 12.2, 12.3_

  - [x] 3.4 Update `.env.example` files with full documentation
    - Add placeholder values (e.g., `your-api-key-here`) for all secrets
    - Document every required environment variable with description, expected format, and required/optional status
    - _Requirements: 12.1, 12.5_

  - [x] 3.5 Create `scripts/check-secrets.sh` secret scanner script
    - Implement pattern matching for: API keys (base64 40+ chars), AWS keys (AKIA...), Elasticsearch API keys, JWT secrets, webhook secrets, Redis URLs with passwords, generic password assignments
    - Support both pre-commit hook mode (staged files) and manual scan mode
    - Skip `.env.example` files and binary files
    - Exit with code 1 if secrets detected, code 0 if clean
    - _Requirements: 12.4_

  - [x] 3.6 Create `docs/incident-log.md` documenting credential exposure and rotation
    - Document date of discovery, affected credentials, rotation status, and responsible party
    - Note that all exposed credentials must be rotated within 24 hours
    - _Requirements: 12.3_

  - [x] 3.7 Update README with instructions to copy `.env.example` to `.env.development`
    - Add setup instructions for new developers to create their own credential files
    - _Requirements: 12.6_

  - [ ]* 3.8 Write property test for secret scanner patterns
    - **Property: Secret Pattern Detection Completeness**
    - Generate strings matching API key, JWT secret, and connection string patterns; verify scanner detects them
    - Generate safe placeholder strings; verify scanner does not flag them
    - **Validates: Requirements 12.4**

- [x] 4. Checkpoint — Verify Phase 1: Hygiene & Security Baseline
  - Verify `git ls-files -i --exclude-standard` returns empty
  - Verify `bash scripts/check-secrets.sh` passes on the repository
  - Verify `.env.example` files contain only placeholders
  - Verify no source files were accidentally untracked
  - Run existing test suite to confirm no regressions


### Phase 2: Bootstrap & Container (Reqs 1, 2)

- [x] 5. Implement the ServiceContainer for dependency injection (Req 2)
  - [x] 5.1 Create `Runsheet-backend/bootstrap/container.py` with the `ServiceContainer` class
    - Implement typed attribute storage backed by an internal `_registry` dict
    - Implement `__setattr__` to store non-private attributes in the registry
    - Implement `__getattr__` to retrieve from the registry with descriptive `AttributeError` on missing
    - Implement `get(service_name)` method that raises descriptive `KeyError` if not registered
    - Implement `has(service_name)` method for existence checks
    - Implement `registered_services` property returning sorted list of registered service names
    - Add type annotations for all known services (core, WS managers, ops, fuel, scheduling, agents)
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 5.2 Write unit tests for `ServiceContainer`
    - Test: register a service and retrieve via attribute access
    - Test: register a service and retrieve via `get()` method
    - Test: `get()` raises `KeyError` with descriptive message for unregistered service
    - Test: `has()` returns True for registered, False for unregistered
    - Test: `registered_services` returns sorted list
    - Test: mock/stub injection — register a mock and verify it is returned
    - _Requirements: 2.3, 2.4_

  - [x] 5.3 Implement compatibility adapters for existing singleton patterns
    - Add `bind_container()` and adapter logic to `websocket/connection_manager.py` (`get_connection_manager()`)
    - Add `bind_container()` and adapter logic to ops WS manager (`get_ops_ws_manager()`)
    - Add `bind_container()` and adapter logic to scheduling WS manager (`get_scheduling_ws_manager()`)
    - Add `bind_container()` and adapter logic to `Agents/agent_ws_manager.py` (`get_agent_ws_manager()`)
    - Each adapter: if container is bound, delegate to it; otherwise fall back to legacy singleton
    - _Requirements: 2.6, 2.7_

  - [x] 5.4 Write unit tests for compatibility adapters
    - Test: without container bound, `get_*()` returns legacy singleton instance
    - Test: with container bound, `get_*()` returns `container.<service>` instance
    - Test: adapter returns the same object as direct container access (`is` identity check)
    - _Requirements: 2.7, Correctness Property P4_

  - [ ]* 5.5 Write property test for ServiceContainer registration and retrieval
    - **Property: Service Registration Round-Trip**
    - Generate arbitrary service names and mock objects; register them; verify `get()` returns the same object
    - Generate unregistered names; verify `KeyError` is raised with the name in the message
    - **Validates: Requirements 2.1, 2.3, Correctness Property P2**

- [x] 6. Decompose main.py into bootstrap modules (Req 1)
  - [x] 6.1 Create `Runsheet-backend/bootstrap/__init__.py` with `initialize_all()` and `shutdown_all()`
    - Define `_BOOT_ORDER = ["core", "middleware", "ops", "fuel", "scheduling", "agents"]`
    - `initialize_all()`: iterate modules in order, call `initialize(app, container)`, log success/failure
    - `shutdown_all()`: iterate modules in reverse order, call `shutdown(app, container)` if it exists
    - On module failure: log error with module name and `exc_info=True`, continue to next module (fail-open)
    - _Requirements: 1.4, 1.5_

  - [x] 6.2 Create `Runsheet-backend/bootstrap/core.py` — core infrastructure initialization
    - Extract Elasticsearch client, Redis client, Settings, Telemetry, DataSeeder, HealthCheckService initialization from `main.py`
    - Implement `async def initialize(app, container)` that creates and registers these services
    - Implement `async def shutdown(app, container)` for cleanup (close ES/Redis connections)
    - _Requirements: 1.1, 1.2_

  - [x] 6.3 Create `Runsheet-backend/bootstrap/middleware.py` — middleware registration
    - Extract CORS, RequestID, RateLimit, SecurityHeaders middleware setup from `main.py`
    - Implement `async def initialize(app, container)` that registers all middleware on the app
    - _Requirements: 1.1, 1.2_

  - [x] 6.4 Create `Runsheet-backend/bootstrap/ops.py` — ops domain initialization
    - Extract OpsElasticsearchService, OpsAdapter, IdempotencyService, PoisonQueueService, FeatureFlagService, WebhookReceiver, DriftDetector, OpsWebSocketManager initialization
    - Register all services in the container
    - Bind compatibility adapter for OpsWebSocketManager
    - _Requirements: 1.1, 1.2_

  - [x] 6.5 Create `Runsheet-backend/bootstrap/fuel.py` — fuel domain initialization
    - Extract FuelService and fuel index initialization from `main.py`
    - Register services in the container
    - _Requirements: 1.1, 1.2_

  - [x] 6.6 Create `Runsheet-backend/bootstrap/scheduling.py` — scheduling domain initialization
    - Extract JobService, CargoService, DelayDetectionService, SchedulingWebSocketManager, periodic delay task initialization
    - Register services in the container
    - Bind compatibility adapter for SchedulingWebSocketManager
    - _Requirements: 1.1, 1.2_

  - [x] 6.7 Create `Runsheet-backend/bootstrap/agents.py` — agentic AI initialization
    - Extract RiskRegistry, BusinessValidator, ActivityLogService, AutonomyConfigService, ApprovalQueueService, ConfirmationProtocol, MemoryService, FeedbackService, specialist agents, Orchestrator, and autonomous agent initialization
    - Register all services in the container
    - Bind compatibility adapter for AgentActivityWSManager
    - Wire autonomous agents through AgentScheduler (placeholder until Phase 4 implements full scheduler)
    - _Requirements: 1.1, 1.2_

  - [x] 6.8 Refactor `main.py` to delegate to bootstrap modules
    - Reduce `main.py` to: FastAPI app creation, lifespan context manager (creates container, calls `initialize_all`/`shutdown_all`), router inclusion
    - Store container on `app.state.container`
    - Remove all inline service instantiation and `configure_*()` calls
    - Target: ≤200 lines of code excluding imports and comments
    - _Requirements: 1.3, 1.6, 2.5, Correctness Property P13_

  - [x] 6.9 Write unit tests for each bootstrap module
    - Test each module's `initialize()` with a mocked `ServiceContainer` and mocked dependencies
    - Verify services are registered in the container after initialization
    - Test `shutdown()` where applicable
    - _Requirements: 1.7_

  - [x] 6.10 Write integration test for full bootstrap sequence
    - Test that `initialize_all()` completes without error with mocked external services
    - Verify all expected services are registered in the container
    - Test fail-open behavior: patch one module's `initialize` to raise, verify others still complete
    - _Requirements: 1.4, 1.5, Correctness Property P3_

  - [x] 6.11 Write test verifying startup route equivalence
    - Compare `sorted([r.path for r in app.routes])` from refactored app against known route list
    - Ensure no routes are lost or duplicated during decomposition
    - _Requirements: Correctness Property P1_

  - [x] 6.12 Verify `main.py` line count does not exceed 200 lines (excluding imports and comments)
    - Add a CI-compatible check script or test assertion
    - _Requirements: 1.6, Correctness Property P13_

  - [ ]* 6.13 Write property test for bootstrap fail-open behavior
    - **Property: Fail-Open Bootstrap Resilience**
    - For each bootstrap module, patch its `initialize` to raise a random exception; verify the app still starts and serves a health check request
    - **Validates: Requirements 1.5, Correctness Property P3**

- [x] 7. Checkpoint — Verify Phase 2: Bootstrap & Container
  - Run full existing test suite (1,449+ tests) — zero regressions
  - Verify `main.py` ≤200 lines
  - Verify all services accessible via both `container.get()` and legacy `get_*()` singletons
  - Verify app starts successfully and serves requests
  - Verify startup time has not increased by more than 10%


### Phase 3: Schemas & Auth Contracts (Reqs 4, 5, 3)

- [x] 8. Define unified request/response schemas (Req 4)
  - [x] 8.1 Create `Runsheet-backend/schemas/common.py` with shared base schemas
    - Implement `PaginatedResponse[T]` generic model with fields: `items` (list), `total` (int), `page` (int), `page_size` (int), `has_next` (bool)
    - Implement `ErrorResponse` model with fields: `error_code` (str), `message` (str), `details` (optional dict), `request_id` (str)
    - Implement `ListEnvelope[T]` generic model with fields: `items` (list), `count` (int)
    - Implement `TenantScopedRequest` base model with field: `tenant_id` (str, min_length=1)
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 8.2 Write unit tests for each shared schema
    - Test `PaginatedResponse` serialization/deserialization with various item types
    - Test `ErrorResponse` validation (required fields, optional details)
    - Test `ListEnvelope` with empty and populated lists
    - Test `TenantScopedRequest` validation (min_length constraint)
    - _Requirements: 4.1, 4.2, 4.3_

  - [x] 8.3 Audit all paginated list endpoints across ops, fuel, scheduling, and agent routers
    - Identify every endpoint that returns a list with pagination
    - Document current response shapes and field names for each
    - Map old field names to new unified field names
    - _Requirements: 4.4, 4.6_

  - [x] 8.4 Migrate paginated endpoints to use `PaginatedResponse` with dual-field deprecation
    - Update each identified endpoint to return `PaginatedResponse`-conforming JSON
    - During the 60-day deprecation window, include both old field names and new unified field names in the response body
    - _Requirements: 4.4, 4.6_

  - [x] 8.5 Migrate error responses to use `ErrorResponse` schema
    - Replace ad-hoc error dictionaries (`{"detail": ...}`, `{"error": ...}`) with `ErrorResponse`
    - Update exception handlers to return `ErrorResponse`-conforming JSON
    - _Requirements: 4.5_

  - [x] 8.6 Create `docs/schema-migration.md` documenting the deprecation timeline
    - Document start date, affected endpoints, old vs new field mappings, and removal date (60 days from start)
    - _Requirements: 4.8_

  - [x] 8.7 Write parametrized test verifying all list endpoints return `PaginatedResponse`-conforming JSON
    - Call each paginated endpoint with mocked backends
    - Validate response against `PaginatedResponse.model_validate()`
    - _Requirements: 4.4, Correctness Property P10_

  - [ ]* 8.8 Write property test for unified schema conformance
    - **Property: PaginatedResponse Schema Invariants**
    - Generate arbitrary items lists, page numbers, and page sizes; verify `has_next` is consistent with `total`, `page`, and `page_size`
    - **Validates: Requirements 4.2, Correctness Property P10**

- [x] 9. Implement centralized auth/tenant policy middleware (Req 5)
  - [x] 9.1 Create `Runsheet-backend/middleware/auth_policy.py` with `AuthPolicy` enum and policy matrix
    - Define `AuthPolicy` enum: `JWT_REQUIRED`, `API_KEY_REQUIRED`, `WEBHOOK_HMAC`, `PUBLIC`
    - Define `POLICY_MATRIX` dict mapping route prefixes to their default `AuthPolicy`
    - Define `POLICY_EXCEPTIONS` dict for per-route overrides (e.g., `GET /api/agent/health` → PUBLIC)
    - _Requirements: 5.1, 5.6_

  - [x] 9.2 Implement `validate_policy_matrix(app)` startup check
    - Compare declared policies against registered routes at startup
    - Log warnings for any route without an explicit policy declaration
    - Default unmatched routes to `JWT_REQUIRED`
    - _Requirements: 5.5, 5.7_

  - [x] 9.3 Implement auth enforcement middleware/dependency
    - Create a FastAPI dependency that enforces the declared `AuthPolicy` for each request
    - Reject unauthenticated requests to `JWT_REQUIRED` routes with 401 and `ErrorResponse`
    - Allow unauthenticated requests to `PUBLIC` routes
    - _Requirements: 5.3_

  - [x] 9.4 Implement tenant scoping dependency
    - Create `require_tenant` FastAPI dependency that extracts `tenant_id` from JWT claims
    - Return a `TenantContext` model with `tenant_id`, `user_id`, and `roles`
    - Raise 401 if no valid JWT is present
    - _Requirements: 5.4_

  - [x] 9.5 Update all routers to declare their default `AuthPolicy`
    - Add `AuthPolicy` declaration to each router (ops, fuel, scheduling, agent, data, chat)
    - Add per-route overrides where needed (e.g., health endpoints → PUBLIC)
    - _Requirements: 5.2_

  - [x] 9.6 Create `docs/auth-contract.md` with the policy matrix table
    - Document the full policy matrix as specified in Req 5.6
    - Include all routers, their default policies, and exceptions
    - _Requirements: 5.6_

  - [x] 9.7 Write unit tests for auth policy enforcement
    - Test: unauthenticated request to `JWT_REQUIRED` route returns 401 with `ErrorResponse`
    - Test: authenticated request to `JWT_REQUIRED` route succeeds
    - Test: unauthenticated request to `PUBLIC` route succeeds
    - Test: `validate_policy_matrix` logs warnings for unmatched routes
    - Test: tenant scoping dependency extracts correct `tenant_id` from JWT
    - _Requirements: 5.3, 5.5, Correctness Property P11_

  - [ ]* 9.8 Write property test for auth policy coverage
    - **Property: Auth Policy Coverage Completeness**
    - Introspect all registered routes; verify each has an explicit or default `AuthPolicy` declaration
    - **Validates: Requirements 5.5, 5.7, Correctness Property P11**

- [x] 10. Create endpoint registry generator (Req 3)
  - [x] 10.1 Create `scripts/generate_endpoint_registry.py`
    - Introspect the FastAPI app to list all registered HTTP routes and WebSocket endpoints
    - For each HTTP route: extract method, path, router prefix, auth requirement, rate limit, request/response schema names
    - For each WebSocket route: extract path, subscription types, auth requirements
    - Output a Markdown document to `docs/endpoint-registry.md`
    - Auto-discover new routers without manual script updates
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 10.2 Generate initial `docs/endpoint-registry.md`
    - Run the script against the current app to produce the baseline registry
    - Commit the generated document
    - _Requirements: 3.5_

  - [x] 10.3 Write unit test verifying script output includes all known routes
    - Compare generated registry against `app.routes` to ensure completeness
    - _Requirements: 3.4, Correctness Property P15_

  - [x] 10.4 Write CI freshness check for endpoint registry
    - Add a test or CI step that regenerates the registry and asserts `git diff --exit-code docs/endpoint-registry.md`
    - _Requirements: 3.4, Correctness Property P15_

- [x] 11. Checkpoint — Verify Phase 3: Schemas & Auth Contracts
  - Verify all paginated endpoints return `PaginatedResponse`-conforming JSON
  - Verify all error responses conform to `ErrorResponse` schema
  - Verify `validate_policy_matrix` reports zero warnings at startup
  - Verify unauthenticated requests to protected routes return 401
  - Verify endpoint registry is complete and up to date
  - Run full test suite — zero regressions


### Phase 4: WS + Scheduler Hardening (Reqs 6, 7, 8)

- [x] 12. Implement BaseWSManager with lifecycle metrics and backpressure (Req 6)
  - [x] 12.1 Create `Runsheet-backend/websocket/base_ws_manager.py` with `BaseWSManager` class
    - Implement connection registry with metadata: `connected_at`, `last_send`, `tenant_id`, `pending_count`
    - Implement Prometheus-compatible metric counters: `connections_total`, `disconnections_total`, `messages_sent_total`, `send_failures_total`, `messages_dropped_total`, plus `active_connections` gauge
    - Label all metrics by `manager_name` and `tenant_id`
    - Implement standard lifecycle methods: `connect`, `disconnect`, `broadcast`, `shutdown`, `get_connection_count`
    - _Requirements: 6.1, 6.6_

  - [x] 12.2 Implement backpressure policy in `BaseWSManager`
    - During `broadcast`, check each client's `pending_count` against `max_pending_messages` (default: 100)
    - Drop messages for clients exceeding the threshold
    - Log a warning with client identifier and drop count on each drop
    - Increment `messages_dropped_total` counter metric
    - _Requirements: 6.2, 6.3_

  - [x] 12.3 Implement stale client detection in `BaseWSManager`
    - Track `last_send` timestamp per client (updated on every successful send)
    - Implement `get_stale_clients(stale_seconds)` returning clients that haven't received a message within the threshold
    - _Requirements: 6.4_

  - [x] 12.4 Implement standard WebSocket handshake confirmation
    - On `connect()`, send a JSON message: `{"type": "connection", "status": "connected", "manager": "<name>", "timestamp": "<iso>"}`
    - _Requirements: 6.9_

  - [x] 12.5 Implement dead client cleanup within 5 seconds
    - During `broadcast`, detect send failures and remove dead clients from the registry
    - Decrement `active_connections` metric on cleanup
    - _Requirements: 6.7_

  - [x] 12.6 Migrate `ConnectionManager` (fleet) to extend `BaseWSManager`
    - Preserve domain-specific methods: `broadcast_location_update`, `broadcast_batch_update`, `send_heartbeat`
    - Add tenant-scoped connections and subscription filtering (matching `OpsWebSocketManager` capabilities)
    - _Requirements: 6.5, 6.6_

  - [x] 12.7 Migrate `OpsWebSocketManager` to extend `BaseWSManager`
    - Preserve domain-specific methods: `broadcast_shipment_update`, `broadcast_rider_update`, `broadcast_sla_breach`, `disconnect_tenant`, subscription filtering
    - Remove duplicated lifecycle logic now handled by base class
    - _Requirements: 6.6_

  - [x] 12.8 Migrate `SchedulingWebSocketManager` to extend `BaseWSManager`
    - Preserve domain-specific methods: `broadcast_job_created`, `broadcast_status_changed`, `broadcast_delay_alert`, `broadcast_cargo_update`, subscription filtering, heartbeat loop
    - _Requirements: 6.6_

  - [x] 12.9 Migrate `AgentActivityWSManager` to extend `BaseWSManager`
    - Preserve domain-specific methods: `broadcast_activity`, `broadcast_approval_event`, `broadcast_event`
    - _Requirements: 6.6_

  - [x] 12.10 Write unit tests for `BaseWSManager`
    - Test: `connect` registers client and sends handshake confirmation
    - Test: `disconnect` removes client and increments disconnection counter
    - Test: `broadcast` sends to all connected clients and increments `messages_sent_total`
    - Test: backpressure drops messages when `pending_count >= max_pending_messages` and increments `messages_dropped_total`
    - Test: dead client cleanup removes failed clients during broadcast
    - Test: `get_stale_clients` returns clients exceeding stale threshold
    - Test: `shutdown` closes all connections and clears client pool
    - Test: `get_metrics` returns correct snapshot of all counters
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.7, 6.9_

  - [x] 12.11 Write per-manager tests verifying metrics emission and backpressure
    - Test each migrated manager (fleet, ops, scheduling, agent activity) emits correct metrics
    - Test backpressure behavior per manager
    - _Requirements: 6.1, 6.2_

  - [x] 12.12 Write benchmark test for broadcast latency under 50 concurrent clients
    - Verify p99 broadcast latency does not exceed 100ms with 50 concurrent mock clients
    - _Requirements: 6.8_

  - [ ]* 12.13 Write property test for backpressure enforcement
    - **Property: Backpressure Drop Guarantee**
    - Generate random numbers of clients with random pending counts; broadcast a message; verify clients above threshold have messages dropped and `messages_dropped_total` equals the count of over-threshold clients
    - **Validates: Requirements 6.2, 6.3, Correctness Property P6**

- [x] 13. Implement AgentScheduler with restart policies (Req 7)
  - [x] 13.1 Create `Runsheet-backend/bootstrap/agent_scheduler.py` with `AgentScheduler` class
    - Implement `RestartPolicy` enum: `ALWAYS`, `ON_FAILURE`, `NEVER`
    - Implement `AgentState` dataclass: agent, policy, status, task, started_at, restart_count, restart_timestamps, last_error, total_uptime_seconds
    - Implement `register(agent, policy)` to register an agent with a restart policy
    - _Requirements: 7.1, 7.2_

  - [x] 13.2 Implement agent start and monitoring logic
    - `start_all()`: start all registered agents
    - `_start_agent(state)`: call `agent.start()`, set status to `running`, create monitoring task
    - `_monitor_agent(state)`: watch agent task, detect exits and exceptions, trigger restart logic
    - _Requirements: 7.1, 7.6_

  - [x] 13.3 Implement restart policy enforcement
    - On agent crash: check `_can_restart(state)` — verify policy allows restart and restart count within window
    - Restart within 5 seconds if policy permits (ALWAYS or ON_FAILURE with exception)
    - Maximum 3 consecutive restarts within a 5-minute window
    - If max restarts exceeded: mark agent as `failed`, emit critical alert via telemetry service
    - _Requirements: 7.2, 7.3, 7.4_

  - [x] 13.4 Implement health reporting
    - `get_health()`: return dict keyed by `agent_id` with `status`, `uptime_seconds`, `restart_count`, `last_error`, `policy`
    - Status values: `running`, `stopped`, `restarting`, `failed`
    - _Requirements: 7.5_

  - [x] 13.5 Implement graceful shutdown
    - `stop_all()`: stop all agents with configurable timeout (default: 10 seconds)
    - If agent does not stop within timeout, force-cancel its task
    - _Requirements: 7.7_

  - [x] 13.6 Update `bootstrap/agents.py` to use `AgentScheduler`
    - Replace bare `asyncio.create_task` calls with `scheduler.register()` and `scheduler.start_all()`
    - Register Delay_Response_Agent, Fuel_Management_Agent, SLA_Guardian_Agent with appropriate restart policies
    - Wire scheduler shutdown into the agents bootstrap module's `shutdown()` function
    - _Requirements: 7.6_

  - [x] 13.7 Write unit tests for AgentScheduler
    - Test: `ALWAYS` policy restarts agent on any exit (normal or exception)
    - Test: `ON_FAILURE` policy restarts agent only on unhandled exception, not on normal exit
    - Test: `NEVER` policy does not restart agent
    - Test: max restart window — agent exceeding 3 restarts in 5 minutes is marked `failed`
    - Test: `get_health()` returns correct status, uptime, restart count, and last error
    - Test: graceful shutdown completes within timeout
    - Test: force-cancel on shutdown timeout exceeded
    - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.7, Correctness Properties P7, P8_

  - [x] 13.8 Write integration test for agent recovery after simulated crash
    - Register a mock agent that raises on first `monitor_cycle`, then succeeds
    - Verify scheduler restarts the agent and status transitions: `running` → `restarting` → `running`
    - _Requirements: 7.3_

  - [ ]* 13.9 Write property test for restart policy bounded behavior
    - **Property: Agent Restart Bounded by SLO Window**
    - Generate random crash sequences with timestamps; verify agent is never restarted more than `SLO_MAX_CONSECUTIVE_FAILURES` times within `SLO_RESTART_WINDOW_SECONDS`
    - **Validates: Requirements 7.3, 7.4, Correctness Property P7**

- [x] 14. Define and enforce operational SLOs (Req 8)
  - [x] 14.1 Define SLO constants in `bootstrap/agent_scheduler.py`
    - `SLO_MAX_RESTART_SECONDS = 5` (max time from crash to restart)
    - `SLO_MAX_CONSECUTIVE_FAILURES = 3` (max consecutive failures before escalation)
    - `SLO_MIN_UPTIME_PCT = 99.0` (minimum uptime per 24-hour window)
    - `SLO_RESTART_WINDOW_SECONDS = 300` (5-minute window for max restarts)
    - `SLO_MAX_CYCLE_DURATION_SECONDS = 5` (max execution duration per monitoring cycle)
    - `SLO_SCHEDULE_DRIFT_PCT = 10` (max interval drift from configured schedule)
    - _Requirements: 8.1, 8.2_

  - [x] 14.2 Implement SLO compliance tracking in AgentScheduler
    - Record restart events with timestamps in the Agent_Activity_Log
    - Track uptime per agent for rolling 24-hour window calculation
    - _Requirements: 8.3_

  - [x] 14.3 Implement SLO threshold alerting
    - When agent uptime falls below `SLO_MIN_UPTIME_PCT` in a rolling 24-hour window, emit a warning alert via telemetry service
    - _Requirements: 8.4_

  - [x] 14.4 Create `docs/operational-slos.md` documenting all SLO definitions
    - Document target values, measurement methods, and alerting thresholds for each SLO
    - Include agent restart SLOs and periodic job SLOs
    - _Requirements: 8.5_

  - [x] 14.5 Write benchmark test for agent startup time and cycle duration
    - Verify agent startup time is under 5 seconds
    - Verify a single monitoring cycle completes within the SLO budget (5 seconds)
    - _Requirements: 8.6_

  - [x] 14.6 Write unit test verifying SLO threshold alerts fire correctly
    - Simulate agent uptime dropping below 99% in a 24-hour window
    - Verify telemetry service receives a warning alert
    - _Requirements: 8.4_

- [x] 15. Checkpoint — Verify Phase 4: WS + Scheduler Hardening
  - Verify all four WS managers extend `BaseWSManager` and emit metrics
  - Verify backpressure drops messages for slow clients
  - Verify WS handshake confirmation is sent on all endpoints
  - Verify AgentScheduler restart policies work correctly (always/on_failure/never)
  - Verify agent health endpoint returns valid JSON
  - Verify broadcast latency benchmark passes (p99 < 100ms under 50 clients)
  - Run full test suite — zero regressions


### Phase 5: Testing, CI & Governance (Reqs 9, 10, 14, 15)

- [x] 16. Create integration test matrix for feature flags and tenant flows (Req 9)
  - [x] 16.1 Create `tests/integration/test_feature_flag_matrix.py`
    - Test: enabling a tenant flag allows access to all ops, fuel, scheduling, and agent endpoints for that tenant
    - Test: disabling a tenant flag returns 403 for all gated HTTP endpoints
    - Test: disabling a tenant flag closes existing WebSocket connections with code 4403
    - Test: re-enabling a previously disabled tenant restores full access without application restart
    - _Requirements: 9.1, 9.2, 9.4_

  - [x] 16.2 Create `tests/integration/test_tenant_disable_flows.py`
    - Test: autonomous agents skip processing for disabled tenants and log the skip reason
    - Test interaction matrix: each of the four WS managers (fleet, ops, scheduling, agent activity) × tenant enabled/disabled states
    - _Requirements: 9.3, 9.5_

  - [x] 16.3 Create route smoke tests covering all registered HTTP endpoints
    - Parametrized test iterating over every registered HTTP route
    - Each test sends a minimal valid request with valid authentication
    - Verify response status code is not 500 (accept 200, 201, 400, 401, 403, 404)
    - _Requirements: 9.6_

  - [x] 16.4 Ensure all integration tests run with mocked Elasticsearch and Redis backends
    - Configure test fixtures to use mocked ES and Redis
    - Verify tests execute without external service dependencies
    - Target: ≥40 integration tests covering the full matrix
    - _Requirements: 9.7_

- [x] 17. Create route smoke test infrastructure (Req 15)
  - [x] 17.1 Create `tests/smoke/fixtures.py` with the smoke test fixture registry
    - Define `ROUTE_FIXTURES` dict mapping route paths to minimal valid request payloads (method, headers, json, params)
    - Define `WS_FIXTURES` dict mapping WebSocket paths to connection parameters
    - Include fixtures for all known routes: health, chat, demo, ops, fuel, scheduling, agent, data, upload, locations
    - For routes without explicit fixtures, use default empty-body request (expect 400/422, not 500)
    - _Requirements: 15.2, 15.5_

  - [x] 17.2 Create `tests/smoke/test_route_smoke.py` with parametrized HTTP smoke tests
    - Auto-discover all registered HTTP routes from `app.routes`
    - For each route: look up fixture or use default, send request, assert status < 500
    - On failure: include route path, HTTP method, response status code, and response body in test output
    - _Requirements: 15.1, 15.3, 15.5, 15.6_

  - [x] 17.3 Add WebSocket smoke tests to `tests/smoke/test_route_smoke.py`
    - For each WS endpoint: establish connection, verify connection upgrade succeeds
    - If endpoint sends a confirmation message, verify it is received within 2 seconds
    - Endpoints without confirmation messages pass if connection upgrade succeeds
    - _Requirements: 15.4, Correctness Property P5_

  - [x] 17.4 Ensure smoke tests run with mocked external dependencies in under 30 seconds
    - Mock Elasticsearch, Redis, and Google Cloud dependencies
    - Verify total smoke test execution time < 30 seconds
    - _Requirements: 15.7_

- [x] 18. Set up CI pipeline with coverage and smoke test gates (Req 10)
  - [x] 18.1 Create `.github/workflows/ci.yml` with the full CI pipeline
    - Job 1: Secret scan — run `scripts/check-secrets.sh` on changed files
    - Job 2: Backend tests + coverage — run full test suite with `pytest --cov`, fail if coverage < 70%
    - Job 3: Route smoke tests — run `tests/smoke/` with 30-second timeout
    - Job 4: Integration tests — run `tests/integration/` with 120-second timeout
    - Job 5: Endpoint registry freshness — regenerate and `git diff --exit-code`
    - Job 6: Git hygiene — verify `git ls-files -i --exclude-standard` returns empty, verify ADR index exists
    - _Requirements: 10.1, 10.2, 10.4, 10.5, 10.7_

  - [x] 18.2 Create `scripts/check_coverage.py` for changed-file coverage gate
    - Accept a `--threshold` parameter (default: 0 — no zero-coverage files)
    - Identify changed Python source files in the PR (exclude `tests/`, `scripts/`, `__pycache__/`)
    - Fail if any changed source file has zero test coverage on changed lines
    - _Requirements: 10.3_

  - [x] 18.3 Configure CI failure reporting
    - On coverage gate failure: report specific coverage delta and failing files in PR summary
    - On smoke test failure: report failing route, method, and status code
    - On missing test failure: report which changed files lack coverage
    - _Requirements: 10.6_

  - [x] 18.4 Validate CI pipeline runs end-to-end
    - Test locally with `act` or equivalent CI runner
    - Verify all jobs pass against the current codebase
    - _Requirements: 10.7_

- [x] 19. Create Architecture Decision Records (Req 14)
  - [x] 19.1 Create `docs/adr/README.md` index file
    - List all ADRs with number, title, status, and link
    - _Requirements: 14.7_

  - [x] 19.2 Create `docs/adr/001-agent-orchestration-model.md`
    - Document why keyword-based routing to specialist agents was chosen over a single monolithic agent
    - Follow standard ADR format: Title, Status, Context, Decision, Consequences
    - _Requirements: 14.2_

  - [x] 19.3 Create `docs/adr/002-safety-confirmation-model.md`
    - Document why risk-classified confirmation with approval queues was chosen over simpler approaches
    - _Requirements: 14.3_

  - [x] 19.4 Create `docs/adr/003-domain-decomposition.md`
    - Document why ops, fuel, scheduling, and agents are separate module trees with their own services, models, and endpoints
    - _Requirements: 14.4_

  - [x] 19.5 Create `docs/adr/004-websocket-architecture.md`
    - Document why four separate WS managers exist instead of a unified pub/sub system
    - _Requirements: 14.5_

  - [x] 19.6 Create `docs/adr/005-elasticsearch-data-layer.md`
    - Document why Elasticsearch is used as the primary data store instead of a relational database
    - _Requirements: 14.6_

- [x] 20. Checkpoint — Verify Phase 5: Testing, CI & Governance
  - Verify ≥40 integration tests pass covering feature flag and tenant disable matrix
  - Verify smoke tests cover all registered HTTP and WebSocket routes
  - Verify CI pipeline runs all jobs successfully
  - Verify coverage gate enforces 70% minimum
  - Verify all 5 ADRs exist and follow standard format
  - Verify endpoint registry is committed and up to date
  - Run full test suite — zero regressions

### Final Validation

- [x] 21. End-to-end validation across all phases
  - [x] 21.1 Run full existing test suite (1,449+ tests) — verify zero regressions
    - _Requirements: Correctness Property P12_

  - [x] 21.2 Verify no secrets in repository
    - Run `git ls-files | xargs bash scripts/check-secrets.sh` — assert exit code 0
    - _Requirements: Correctness Property P9_

  - [x] 21.3 Verify git hygiene
    - Run `git ls-files -i --exclude-standard` — assert empty output
    - _Requirements: Correctness Property P14_

  - [x] 21.4 Verify startup route equivalence
    - Compare registered routes before and after refactoring — assert identical sets
    - _Requirements: Correctness Property P1_

  - [x] 21.5 Verify service availability through container
    - For every known service, assert `container.get(name)` returns correct instance type
    - _Requirements: Correctness Property P2_

  - [x] 21.6 Verify `main.py` line count ≤200
    - Count non-blank, non-comment, non-import lines
    - _Requirements: Correctness Property P13_

  - [x] 21.7 Verify all correctness properties P1–P15 pass
    - Run the full property verification suite
    - Document any deviations or known limitations