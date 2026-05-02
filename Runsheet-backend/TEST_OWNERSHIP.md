# Test Ownership Map

This document defines which test directory owns which concerns, which test
categories are CI gates, and the boundaries between test types.

## Directory Structure

```
tests/
├── conftest.py          # Shared fixtures, Hypothesis profiles (default/ci/debug/fast)
├── unit/                # Fast, isolated tests — no external services
├── integration/         # Tests requiring service mocks or real backends (ES, Redis)
├── smoke/               # Route registration and health-endpoint sanity checks
├── property/            # Hypothesis property-based tests (bug condition + preservation)
├── contract/            # Webhook payload contract verification
└── fixtures/            # Shared test data (e.g., Dinee webhook payloads)
```

## Ownership by Directory

### `tests/unit/`

| Concern | Examples |
|---|---|
| Domain logic | `test_fuel_calculations.py`, `test_scheduling_status_transitions.py` |
| Service classes | `test_activity_log_service.py`, `test_memory_service.py`, `test_fuel_service.py` |
| Middleware & auth | `test_tenant_guard.py`, `test_auth_policy.py`, `test_request_id_middleware.py` |
| Error handling | `test_error_handlers.py`, `test_schema_validation.py` |
| Agent logic | `test_base_agent.py`, `test_orchestrator.py`, `test_execution_planner.py` |
| Bootstrap lifecycle | `test_bootstrap_core.py`, `test_bootstrap_init.py`, `test_bootstrap_middleware.py` |
| API endpoint handlers | `test_ops_api_endpoints.py`, `test_fuel_endpoints.py`, `test_scheduling_api_endpoints.py` |
| WebSocket managers | `test_base_ws_manager.py`, `test_agent_ws_manager.py`, `test_ws_manager_metrics.py` |
| Overlay agents | `test_dispatch_optimizer.py`, `test_route_planning_agent.py`, `test_compartment_loading_agent.py` |

**Scope**: Each unit test file covers a single module or class. Tests use mocks
for all external dependencies (Elasticsearch, Redis, JWT). No network calls.

### `tests/integration/`

| Concern | Examples |
|---|---|
| API endpoint flows | `test_api_endpoints.py`, `test_ops_api_integration.py` |
| Feature flag matrix | `test_feature_flag_matrix.py`, `test_ops_feature_flag_integration.py` |
| Tenant disable flows | `test_tenant_disable_flows.py` |
| WebSocket lifecycle | `test_ops_websocket_integration.py` |
| Data import pipelines | `test_import_endpoints.py` |
| Fuel domain flows | `test_fuel_integration.py` |
| Ops ingestion | `test_ops_ingestion_integration.py` |

**Scope**: Tests exercise multiple modules together. Use mock Elasticsearch by
default (`TEST_USE_MOCK_ES=true`); can run against a real instance when
`TEST_USE_MOCK_ES=false` with credentials provided. Redis service required in CI.

### `tests/smoke/`

| Concern | Examples |
|---|---|
| Route registration | `test_route_smoke.py` — verifies every HTTP and WebSocket route is registered with a callable handler |
| Health endpoints | Asserts `/health`, `/health/ready`, `/health/live`, `/api/health` return 200 |

**Scope**: Lightweight checks that the app boots and all routes are wired.
No business logic assertions. 30-second timeout in CI.

### `tests/property/`

| Concern | Examples |
|---|---|
| Bug condition exploration | `test_bug_condition_exploration.py` — tenant isolation, auth, error envelopes, exception logging |
| Preservation properties | `test_preservation_properties.py` — existing tenant scoping, error handler, health, rate limiting, PII masking, CORS, bootstrap |
| Tenant isolation | `test_tenant_isolation_property.py`, `test_scheduling_tenant_isolation_property.py` |
| Scheduling invariants | `test_scheduling_status_transitions_property.py`, `test_scheduling_job_id_uniqueness_property.py`, `test_scheduling_asset_conflict_property.py` |
| Webhook HMAC | `test_hmac_property.py` |
| Idempotency | `test_idempotency_property.py` |
| Revenue guard | `test_revenue_guard_output_constraint_property.py` |

**Scope**: Uses Hypothesis to generate many inputs and verify invariants hold
across the input space. Profiles configured in `conftest.py`:

| Profile | `max_examples` | Use case |
|---|---|---|
| `default` | 100 | Local development |
| `ci` | 200 | CI pipeline (derandomized) |
| `debug` | 10 | Quick debugging |
| `fast` | 20 | Quick smoke |

Set via `HYPOTHESIS_PROFILE` environment variable.

### `tests/contract/`

| Concern | Examples |
|---|---|
| Webhook payload contracts | `test_webhook_contract.py` — Dinee webhook HMAC, idempotency, all 6 event types, ES document shape |

**Scope**: Verifies that external webhook payloads produce the expected
internal documents. Uses real fixture payloads from `tests/fixtures/dinee_webhooks/`.

### `tests/fixtures/`

Shared test data consumed by other test directories. Not a test suite itself.

## CI Gate Configuration

The CI pipeline (`.github/workflows/ci.yml`) defines which test categories
block a merge and which are informational.

### Required Gates (must pass to merge)

| CI Job | Test Scope | Timeout | Notes |
|---|---|---|---|
| `backend-tests` | **All tests** (`pytest` at repo root) | — | Runs unit + integration + smoke + property + contract. Coverage must meet 70% threshold. |
| `smoke-tests` | `tests/smoke/` | 30 s | Route registration sanity. Runs after `backend-tests`. |
| `integration-tests` | `tests/integration/` | 120 s | Feature flags, tenant flows, WebSocket lifecycle. Runs after `backend-tests`. |
| `endpoint-registry` | Registry freshness check | — | Regenerates `docs/endpoint-registry.md` and diffs. Runs after `backend-tests`. |
| `git-hygiene` | No test files tracked, ADR index, `main.py` line limit | — | Blocks if `.coverage`, `.hypothesis/`, `__pycache__/`, or `*.pyc` are in the git index. |
| `secret-scan` | Secret detection on changed files | — | Runs `scripts/check-secrets.sh`. |

All six jobs are required gates — a failure in any job blocks the PR.

### Optional / Local-Only

| Category | How to run | Notes |
|---|---|---|
| Property tests with `debug` profile | `HYPOTHESIS_PROFILE=debug pytest tests/property/` | Fewer examples for fast iteration |
| Real Elasticsearch integration | `TEST_USE_MOCK_ES=false pytest tests/integration/` | Requires live ES credentials |
| HTML coverage report | `pytest --cov=. --cov-report=html:coverage_html` | Also generated in CI as the canonical artifact |

## Boundaries Between Test Types

```
┌─────────────────────────────────────────────────────────────────┐
│                        What to test where                       │
├──────────────┬──────────────────────────────────────────────────┤
│  unit/       │ Single function or class in isolation.           │
│              │ Mock ALL external deps. No network, no disk I/O. │
│              │ Fast: < 1 s per test.                            │
├──────────────┼──────────────────────────────────────────────────┤
│  integration/│ Multiple modules working together.               │
│              │ Mock or real ES/Redis. Tests API request →        │
│              │ service → data layer round-trips.                │
│              │ Moderate: < 120 s total.                         │
├──────────────┼──────────────────────────────────────────────────┤
│  smoke/      │ App boots, routes are registered, health is 200. │
│              │ No business logic. Catches wiring regressions.   │
│              │ Fast: < 30 s total.                              │
├──────────────┼──────────────────────────────────────────────────┤
│  property/   │ Invariants that must hold for ALL inputs.        │
│              │ Uses Hypothesis strategies to generate inputs.   │
│              │ Catches edge cases unit tests miss.              │
│              │ Variable: depends on max_examples profile.       │
├──────────────┼──────────────────────────────────────────────────┤
│  contract/   │ External payload shape → internal document shape.│
│              │ Uses real fixture files. Verifies HMAC,          │
│              │ idempotency, and field mapping.                  │
│              │ Fast: < 10 s total.                              │
└──────────────┴──────────────────────────────────────────────────┘
```

### Decision Guide

- **"Does this function return the right value given these inputs?"** → `unit/`
- **"Does this API endpoint call the service, hit ES, and return the right response?"** → `integration/`
- **"Is this route registered and reachable?"** → `smoke/`
- **"Does this invariant hold for every possible tenant ID / payload / state?"** → `property/`
- **"Does this external webhook produce the right internal document?"** → `contract/`

## Adding New Tests

1. Pick the directory that matches the concern (see decision guide above).
2. Name the file `test_<module_or_concern>.py`.
3. If the test needs shared fixtures, add them to `tests/conftest.py` (global)
   or `tests/<directory>/conftest.py` (directory-scoped).
4. Property tests must link to requirements: `**Validates: Requirements X.Y**`.
5. Run the full suite locally before pushing: `pytest --cov=. --cov-report=term-missing`.
