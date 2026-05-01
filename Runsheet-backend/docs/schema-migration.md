# Schema Migration Guide — Unified Response Schemas

## Overview

This document describes the migration from domain-specific response
envelopes to the unified `PaginatedResponse` and `ErrorResponse` schemas
defined in `schemas/common.py`.

During the **60-day deprecation window**, all paginated list endpoints
return **both** the old field names and the new unified field names in
the same response body (dual-field approach). After the window closes,
the old field names will be removed.

## Timeline

| Milestone | Date |
|-----------|------|
| Deprecation window opens | 2025-07-14 |
| Old fields removed | 2025-09-12 (60 days) |

## Affected Endpoints

### Paginated List Endpoints

All endpoints below now return dual-field responses.

| Router | Endpoint | Old Shape | New Shape |
|--------|----------|-----------|-----------|
| Ops | `GET /api/ops/shipments` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Ops | `GET /api/ops/shipments/sla-breaches` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Ops | `GET /api/ops/shipments/failures` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Ops | `GET /api/ops/riders` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Ops | `GET /api/ops/riders/utilization` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Ops | `GET /api/ops/events` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Fuel | `GET /api/fuel/stations` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Scheduling | `GET /api/scheduling/jobs` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Scheduling | `GET /api/scheduling/jobs/active` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Scheduling | `GET /api/scheduling/jobs/delayed` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Scheduling | `GET /api/scheduling/jobs/{job_id}/events` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Scheduling | `GET /api/scheduling/cargo/search` | `{data, pagination, request_id}` | `{items, total, page, page_size, has_next}` |
| Agent | `GET /api/agent/approvals` | `{data, pagination, ...}` | `{items, total, page, page_size, has_next}` |
| Agent | `GET /api/agent/activity` | `{data, pagination, ...}` | `{items, total, page, page_size, has_next}` |
| Agent | `GET /api/agent/memory` | `{data, pagination, ...}` | `{items, total, page, page_size, has_next}` |
| Agent | `GET /api/agent/feedback` | `{data, pagination, ...}` | `{items, total, page, page_size, has_next}` |

### Field Mapping

| Old Field | New Field | Notes |
|-----------|-----------|-------|
| `data` | `items` | List of result items |
| `pagination.page` | `page` | Current page number (1-indexed) |
| `pagination.size` | `page_size` | Number of items per page |
| `pagination.total` | `total` | Total number of matching items |
| `pagination.total_pages` | *(removed)* | Replaced by `has_next` boolean |
| *(new)* | `has_next` | Whether more pages exist |
| `request_id` | *(kept at top level during deprecation)* | Moved to response headers post-deprecation |

### Error Responses

All error responses now conform to the `ErrorResponse` schema:

```json
{
  "error_code": "VALIDATION_ERROR",
  "message": "Human-readable error message",
  "details": {"field": "name", "reason": "too short"},
  "request_id": "req-abc123"
}
```

The `errors/handlers.py` module uses the unified `schemas.common.ErrorResponse`
model for all exception handlers.

## Migration Guide for Consumers

### During the Deprecation Window (now through removal date)

Both old and new fields are present in every paginated response:

```json
{
  "items": [...],
  "total": 142,
  "page": 1,
  "page_size": 20,
  "has_next": true,
  "data": [...],
  "pagination": {"page": 1, "size": 20, "total": 142, "total_pages": 8},
  "request_id": "req-abc123"
}
```

**Action required:** Update your client code to read from the new fields
(`items`, `total`, `page`, `page_size`, `has_next`) instead of the old
fields (`data`, `pagination`).

### After the Deprecation Window

Only the new unified fields will be returned:

```json
{
  "items": [...],
  "total": 142,
  "page": 1,
  "page_size": 20,
  "has_next": true
}
```

## Schema Definitions

The unified schemas are defined in `Runsheet-backend/schemas/common.py`:

- **`PaginatedResponse[T]`** — Generic paginated list response
- **`ErrorResponse`** — Structured error response
- **`ListEnvelope[T]`** — Simple list wrapper with count
- **`TenantScopedRequest`** — Base model for tenant-scoped requests
