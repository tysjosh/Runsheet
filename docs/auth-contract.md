# Authentication & Tenant Policy Contract

This document defines the centralized authentication and tenant scoping policy for the Runsheet Logistics API. Every HTTP and WebSocket endpoint has an explicit `AuthPolicy` declaration that determines how authentication is enforced.

**Source of truth:** `Runsheet-backend/middleware/auth_policy.py`

## AuthPolicy Enum

| Value | Description |
|-------|-------------|
| `JWT_REQUIRED` | Request must include a valid `Authorization: Bearer <token>` header with a JWT signed by the platform secret. |
| `API_KEY_REQUIRED` | Request must include a valid `X-API-Key` header. Used for machine-to-machine integrations. |
| `WEBHOOK_HMAC` | Request is verified using HMAC signature validation (e.g., Dinee webhook payloads). |
| `PUBLIC` | No authentication required. Open to all callers. |

## Policy Matrix

The policy matrix maps route prefixes to their default `AuthPolicy`. Per-route exceptions override the default for specific method + path combinations.

| Router | Default Policy | Exceptions |
|--------|---------------|------------|
| `/api/scheduling/*` | `JWT_REQUIRED` | none |
| `/api/ops/*` | `JWT_REQUIRED` | none |
| `/api/ops/admin/*` | `JWT_REQUIRED` (admin role) | none |
| `/api/fuel/*` | `JWT_REQUIRED` | none |
| `/api/agent/*` | `JWT_REQUIRED` | `GET /api/agent/health` → `PUBLIC` |
| `/api/chat` | `JWT_REQUIRED` | none |
| `/api/chat/clear` | `JWT_REQUIRED` | none |
| `/api/data/*` | `JWT_REQUIRED` | none |
| `/ws/*` | `JWT_REQUIRED` (via query param or first message) | `/ws/agent-activity` → `PUBLIC` for read-only |
| `/health` | `PUBLIC` | — |
| `/docs`, `/openapi.json` | `PUBLIC` | — |

## Enforcement

Authentication is enforced via the `enforce_auth_policy` FastAPI dependency defined in `middleware/auth_policy.py`. The dependency:

1. Determines the effective policy for the current route using `get_policy_for_route(method, path)`.
2. Checks `POLICY_EXCEPTIONS` first (exact method + path match).
3. Falls back to `POLICY_MATRIX` prefix matching (longest prefix wins).
4. If no match is found, defaults to `JWT_REQUIRED`.

### Rejection Behavior

Unauthenticated requests to protected routes receive a `401 Unauthorized` response conforming to the `ErrorResponse` schema:

```json
{
  "error_code": "AUTH_REQUIRED",
  "message": "Missing or invalid Authorization header. Expected: Bearer <token>",
  "details": null,
  "request_id": "<correlation-id>"
}
```

Invalid or expired tokens return:

```json
{
  "error_code": "AUTH_INVALID_TOKEN",
  "message": "Invalid or expired JWT token",
  "details": null,
  "request_id": "<correlation-id>"
}
```

## Tenant Scoping

The `require_tenant` dependency extracts tenant context from JWT claims:

- **`tenant_id`** — Required. Identifies the tenant for data isolation.
- **`user_id`** — Extracted from the `sub` or `user_id` claim.
- **`roles`** — List of roles from the `roles` claim.

If the JWT does not contain a `tenant_id` claim, the request is rejected with:

```json
{
  "error_code": "TENANT_REQUIRED",
  "message": "JWT does not contain a tenant_id claim",
  "details": null,
  "request_id": "<correlation-id>"
}
```

## Startup Validation

At application startup, `validate_policy_matrix(app)` compares declared policies against all registered routes. Any route without an explicit policy match is logged as a warning and defaults to `JWT_REQUIRED`.

## Router Declarations

Each router module declares its default auth policy via a `ROUTER_AUTH_POLICY` constant:

| Module | Constant Value |
|--------|---------------|
| `ops/api/endpoints.py` | `jwt_required` |
| `fuel/api/endpoints.py` | `jwt_required` |
| `scheduling/api/endpoints.py` | `jwt_required` |
| `agent_endpoints.py` | `jwt_required` |
| `data_endpoints.py` | `jwt_required` |
| `inline_endpoints.py` | `jwt_required` |
| `ops/webhooks/receiver.py` | `webhook_hmac` |

## WebSocket Authentication

WebSocket endpoints authenticate via:

- **Query parameter**: `?token=<jwt>` on the WebSocket URL.
- **First message**: The client sends a JSON message with a `token` field as the first frame after connection.

The `/ws/agent-activity` endpoint is an exception — it allows unauthenticated read-only connections.
