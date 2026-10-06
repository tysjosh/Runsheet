# Requirements: Tenant data export (CSV), v1

## Summary

Runsheet has no operational data export. The only CSV download is the blank import template (`Runsheet-backend/import_endpoints.py:301-321`, `download_template`). The 2026-10-04 audit (`.agents/tasks/spreadsheet-export-audit-2026-10-04/report.md`) and its 2026-10-06 status check (`status.md`) found every export item PENDING (F1, E1–E5, G1–G4, C1–C7).

This spec adds CSV export for five tenant-facing lists, a shared streaming helper that enforces the security and volume rules once, the date filters two exports depend on (G1, G2), tenant-wide admin list endpoints for HOS and DVIR data (G3), and an "Export CSV" button on the five matching UI pages. It also corrects a stale spec checkbox (G4).

All scope, format, role and PII choices below were decided by the owner. They are recorded as decisions, not open questions. Where the owner's wording left a detail to pick, the pick is listed under Assumptions.

### Codebase grounding (worktree `.worktrees/data-export`, branch `production-readiness/data-export`, base `7cae435`)

| Item | Source of truth | Current filters / guard |
|---|---|---|
| IFTA report | `compliance/api/ifta_endpoints.py:160` `get_ifta_report` (`GET /api/compliance/ifta/report?quarter=`) | `quarter` (validated by `_validate_quarter`). Router-level `compliance_ops_dependency` = `("admin", "dispatcher")` (`ifta_endpoints.py:60-63`, `auth/router_guards.py:17-18`) |
| Orders | `fuel/api/order_endpoints.py:781` `list_orders` (`GET /api/orders`) | `status`, `customer_id`, `driver_id`, `call_type`, `product_code`, `start_date`, `end_date`, `intake_channel`, `q`, `sort`; `size ≤ 100`; `require_role(tenant, "dispatcher", "admin")` |
| Jobs | `scheduling/api/endpoints.py:248` `list_jobs` (`GET /api/scheduling/jobs`) | `job_type`, `status`, `asset_assigned`, `origin`, `destination`, `start_date`, `end_date`, `sort_by`, `sort_order`; `size ≤ 100`; `@limiter.limit`; `require_role(tenant, "dispatcher", "admin")` |
| Reconciliation | `fuel/api/fuel_ops_endpoints.py:3941` `list_reconciliation_records` (`GET /api/fuel/mvp/reconciliation`) (status.md cites `:3986` at `e8893c6`; line drift) | `order_id`, `plan_id`, `pod_id`, `min_variance_pct`, `page`, `size ≤ 500`. No date filter (G1). No explicit role check found in the handler |
| Invoices | `commerce/api/invoice_endpoints.py:170` `list_invoices` (`GET /api/commerce/invoices`) | `status` (enum `InvoiceStatus`), `customer_id`, `account_id`, `qbo_push_state`, `cursor`, `limit ≤ 200`. No date filter (G2). Feature-flag 404 via `require_invoicing_enabled` (`:82`), commerce ops roles `("admin", "dispatcher")` (`commerce/api/_authz.py:44`) |
| HOS | `driver/api/hos_endpoints.py:190` `GET /api/driver/hos` (self only). Data in `duty_status_events` and `hos_gate_overrides` (`driver/services/driver_es_mappings.py:27,32`) | No tenant-wide list (G3) |
| DVIR | `driver/api/inspection_endpoints.py:218` `POST /api/driver/inspections` only. Data in `vehicle_inspections` (`driver_es_mappings.py:30`) | No list route (G3) |
| Roles | `auth/supertokens_init.py:109` `CANONICAL_ROLES` = `admin`, `dispatcher`, `driver`, `platform_admin`. The tenant owner is `admin` | Guards: `auth.authorization.require_role` (exact match) and `auth/router_guards.py:43` `roles_dependency` |
| Rate limiter | `middleware/rate_limiter.py:174` `limiter` (slowapi, default key `get_client_ip`). Precedent for a custom key: `driver_rate_key` (`:86`) | — |
| Download pattern | `import_endpoints.py:301-321`: `StreamingResponse`, `media_type="text/csv"`, `Content-Disposition: attachment` | — |
| UI pages | `components/compliance/IFTAReportPage.tsx`, `app/dashboard/orders/page.tsx`, `app/ops/scheduling/page.tsx`, `components/ops/ReconciliationPage.tsx`, `components/commerce/InvoicesListPage.tsx` | Jest + Testing Library (`runsheet/package.json`) |

## Owner decisions (locked)

| ID | Decision |
|---|---|
| D1 | Format: CSV only, UTF-8 with BOM for Excel. No XLSX. No new dependencies (stdlib `csv` + FastAPI `StreamingResponse`). |
| D2 | v1 scope, in this build order: (1) IFTA quarterly report, (2) orders list, (3) jobs / daily dispatch sheet, (4) delivery / reconciliation register, (5) invoice register. |
| D3 | Deferred: T2 next-tier exports, Failure Analytics CSV (E6), AR / payments / price books (ERP is system of record), rack prices / notifications / fleet list. |
| D4 | API shape: `GET <existing list endpoint>/export`, reusing that endpoint's filters, tenant context and role guard. One shared streaming helper module providing CSV-injection escaping, search_after/scroll paging, a 50,000-row hard cap, a per-user rate limit via the existing limiter, a structured audit log line, and the filename `<type>_<tenant>_<YYYYMMDD>.csv`. |
| D5 | Roles: invoice register = `admin` only. IFTA, orders, jobs, reconciliation = `admin` + `dispatcher`. `driver` and customers never. |
| D6 | PII: exclude driver phone, email and license numbers and customer contact emails and phones from all v1 exports. Customer name and delivery address are operational and are included. |
| D7 | G1/G2: add optional `start_date` / `end_date` (typed, 422 on invalid) to `/api/fuel/mvp/reconciliation` and `/api/commerce/invoices` (additive, backward compatible) and use them in the exports. |
| D8 | G3: add tenant-wide, admin-only list endpoints for HOS records and vehicle inspections (DVIR) with date and driver filters and paging. Backend only, no UI in v1. |
| D9 | G4: uncheck or annotate the stale `[x]` on frontend-feature-parity task 10.3 (`tasks.md:142`), pointing to commit `76a9ef2`. |
| D10 | Frontend: an accessible "Export CSV" button on the five matching pages, shown only to permitted roles, passing the current filters, downloading via authenticated fetch to a Blob, with clear messages for 413, 429 and 403, following existing component and style conventions. |
| D11 | Tests: per export, a tenant-isolation test, role allow/deny tests, a CSV-injection test, a row-cap test and date-filter tests. Frontend unit tests per the existing Jest setup. |

## Functional Requirements

### FR1 Shared export helper (C1, C4, C5, C7)

1. One backend module owns CSV serialization, escaping, paging, the row cap, the filename, and the audit log line. Every v1 export uses it; no export writes CSV or sets `Content-Disposition` on its own.
2. Output is RFC 4180 CSV written with the stdlib `csv` module, UTF-8 encoded, prefixed with a BOM (`EF BB BF`), with a header row of stable column names.
3. Cells are escaped against CSV/formula injection: any cell whose string value starts with `=`, `+`, `-`, `@`, tab (`\t`) or carriage return (`\r`) is prefixed with a single quote (`'`).
4. Rows are read with search_after (or scroll) paging, not through the list endpoint's capped `size`/`limit`, so an export is not limited to 100/200/500 rows.
5. An export never exceeds 50,000 data rows. When the filtered result would exceed the cap, the request fails with an error that tells the user to narrow the filters. The file is never silently truncated.
6. Export routes are rate limited per user through the existing slowapi `limiter`.
7. Each export emits one structured log line with tenant ID, user ID, export type, the applied filters, row count and outcome.
8. The response is a `StreamingResponse` with `media_type` `text/csv; charset=utf-8` and `Content-Disposition: attachment; filename="<type>_<tenant>_<YYYYMMDD>.csv"`.

### FR2 Export endpoints (E1–E5, F1)

| # | Export | Route | Filters (same names and validation as the list endpoint) | Roles |
|---|---|---|---|---|
| 1 | IFTA quarterly report | `GET /api/compliance/ifta/report/export` | `quarter` (required) | `admin`, `dispatcher` |
| 2 | Orders | `GET /api/orders/export` | `status`, `customer_id`, `driver_id`, `call_type`, `product_code`, `start_date`, `end_date`, `intake_channel`, `q`, `sort` | `admin`, `dispatcher` |
| 3 | Jobs / dispatch sheet | `GET /api/scheduling/jobs/export` | `job_type`, `status`, `asset_assigned`, `origin`, `destination`, `start_date`, `end_date`, `sort_by`, `sort_order` | `admin`, `dispatcher` |
| 4 | Reconciliation register | `GET /api/fuel/mvp/reconciliation/export` | `order_id`, `plan_id`, `pod_id`, `min_variance_pct`, `start_date`, `end_date` | `admin`, `dispatcher` |
| 5 | Invoice register | `GET /api/commerce/invoices/export` | `status`, `customer_id`, `account_id`, `qbo_push_state`, `start_date`, `end_date` | `admin` only |

1. Each export resolves the tenant only through `get_tenant_context` (or the dependency that wraps it, e.g. `require_invoicing_enabled`), exactly as the list endpoint does. No export runs a separate unscoped Elasticsearch query.
2. Each export returns the same rows the list endpoint would return for the same filters, across all pages, up to the cap.
3. Paging params (`page`, `size`, `limit`, `cursor`) are not accepted on export routes.
4. Columns follow the audit's per-module list (report §3), minus the PII fields in FR4. The exact column list per export is fixed in the design.
5. The IFTA export includes trucks flagged `ifta_data_incomplete`, with that flag as a column, so the bookkeeper sees what the automated return excluded.
6. Invoice export keeps the feature-flag 404 from `require_invoicing_enabled` ahead of the role check (ordering note in `auth/router_guards.py`).

### FR3 Roles (C3)

1. Role checks use the codebase's role names (`admin`, `dispatcher`, `driver`, `platform_admin`) and existing mechanisms (`require_role` / `roles_dependency`).
2. A caller holding only `driver` gets 403 on every export. Customer users never receive any export.
3. The invoice export accepts `admin` only, even though `list_invoices` accepts `admin` and `dispatcher`.
4. Staff-only commerce data (accounts, AR aging, payments, price books; `COMMERCE_STAFF_ROLES`, `_authz.py:47`) is not exported.

### FR4 PII (C6)

1. No v1 export contains driver phone, driver email, driver license number (CDL), customer contact email or customer contact phone.
2. Customer name and delivery address / ship-to are included where the row has them.

### FR5 Date filters on existing list endpoints (G1, G2)

1. `GET /api/fuel/mvp/reconciliation` and `GET /api/commerce/invoices` accept optional `start_date` and `end_date`.
2. The params are typed; an unparseable value, or `start_date` after `end_date`, returns 422.
3. Omitting both params returns exactly what the endpoint returns today.
4. The matching export endpoints accept and apply the same params.

### FR6 Tenant-wide HOS and DVIR lists (G3)

1. A new admin-only `GET` list endpoint returns HOS records (duty-status events) for the caller's tenant.
2. A new admin-only `GET` list endpoint returns vehicle inspections (DVIR) for the caller's tenant.
3. Both accept optional `driver_id`, `start_date`, `end_date` and page through results with a bounded page size.
4. Both are tenant-scoped via `get_tenant_context`. `dispatcher` and `driver` get 403.
5. Backend only. No UI and no CSV export in v1.
6. The existing self-only `GET /api/driver/hos` and `POST /api/driver/inspections` behave as today.

### FR7 Stale checkbox (G4)

1. `.kiro/specs/frontend-feature-parity/tasks.md:142` (task 10.3 "Add CSV export functionality") is unchecked, or annotated, with a note that the feature was removed in `76a9ef2` and that the replacement is tracked as E6 (deferred) in this spec.

### FR8 Frontend export button (D10)

1. Each of the five pages shows an "Export CSV" button: IFTA report page, orders list, scheduling/jobs list, reconciliation page, invoices list.
2. The button renders only for roles allowed to export from that page (invoice: `admin`; others: `admin`, `dispatcher`).
3. The request carries the page's current filters, mapped to the export route's query params.
4. The download goes through an authenticated `fetch` with the existing cookie session (`credentials: "include"`, as in `services/api.ts`), into a `Blob`, then saves with the server-supplied filename.
5. While the export runs, the button is disabled and shows a busy state. A second click doesn't start a second request.
6. Errors produce a clear, visible message: 413 says the result is over 50,000 rows and to narrow the filters; 429 says to wait and retry; 403 says the user lacks permission; other failures show a generic retry message.
7. The button follows existing component and style conventions (existing button component and Tailwind classes, existing toast/alert component) and is accessible: native `<button>`, an accessible name that says what is exported, keyboard operable, visible focus, `aria-busy` while running, and errors announced through a live region (or the existing toast, if it already announces).

## Non-Functional Requirements

1. No new backend or frontend dependency (D1).
2. Export memory use is bounded by the page size used for search_after, not by the row count. Rows stream as they're read.
3. Existing list endpoints keep their response shapes, defaults and caps.
4. Audit log lines contain filter values and IDs only, never row contents or PII.
5. Export routes must not be shadowed by existing path-param routes (`/api/orders/{order_id}`, `/api/commerce/invoices/{invoice_id}`, `/api/scheduling/jobs/{id}`).

## Acceptance Criteria (EARS)

### Format and helper

1. The system shall serve every v1 export as CSV encoded in UTF-8 with a leading BOM (`EF BB BF`). (D1)
2. The system shall not produce XLSX in v1, and the change shall add no entry to `Runsheet-backend/requirements*.txt` or `runsheet/package.json` dependencies. (D1)
3. The system shall build every export with the stdlib `csv` module and FastAPI `StreamingResponse`. (D1)
4. When a cell value starts with `=`, `+`, `-`, `@`, tab or carriage return, the system shall prefix it with a single quote `'` in the CSV output. (D4)
5. When a cell value does not start with one of those characters, the system shall write it unchanged apart from standard CSV quoting.
6. When the filtered result has more rows than one list page, the system shall include all rows by paging with search_after or scroll, not limited by the list endpoint's `size`/`limit` cap. (D4)
7. If the filtered result would exceed 50,000 rows, then the system shall return HTTP 413 with a message telling the user to narrow the filters, and shall return no CSV body. (D4; see Assumption A1)
8. The system shall never return a CSV with fewer rows than the filtered result. (D4)
9. When a user exceeds the export rate limit, the system shall return HTTP 429 through the existing `limiter`, keyed per user. (D4)
10. When an export completes or fails, the system shall emit one structured log line with tenant ID, user ID, export type, filters, row count and outcome. (D4)
11. The system shall set `Content-Disposition: attachment; filename="<type>_<tenant>_<YYYYMMDD>.csv"`, with `<type>` one of `ifta`, `orders`, `jobs`, `reconciliation`, `invoices`, `<tenant>` the caller's tenant ID made filename-safe, and `<YYYYMMDD>` the UTC export date. (D4; see Assumption A3)

### Scope and routes

12. The system shall provide, in build order, `GET /api/compliance/ifta/report/export`, `GET /api/orders/export`, `GET /api/scheduling/jobs/export`, `GET /api/fuel/mvp/reconciliation/export` and `GET /api/commerce/invoices/export`. (D2, D4)
13. When an export is called with filters, the system shall apply the same filter names, types and 422 validation as the corresponding list endpoint. (D4)
14. When an export and its list endpoint are called with the same filters, the system shall return the same set of records in the export as across all list pages.
15. When `GET /api/compliance/ifta/report/export` is called without a valid `quarter`, the system shall return 422.
16. The system shall include trucks flagged `ifta_data_incomplete` in the IFTA export with the flag as a column.
17. When `GET /api/orders/export` (or `/api/commerce/invoices/export`, `/api/scheduling/jobs/export`) is requested, the system shall route it to the export handler, not to the `/{id}` detail handler.
18. The system shall not provide v1 exports for T2 next-tier data, Failure Analytics (E6), AR aging, payments, price books, rack prices, notifications or the fleet list. (D3)

### Tenant isolation

19. The system shall resolve the tenant for every export from `get_tenant_context` (directly or through the list endpoint's wrapping dependency) and shall scope every query to that tenant.
20. When a tenant-A user exports any of the five types, the system shall return no row belonging to tenant B, given seeded rows for both tenants that match the filters. (D11, one test per export)
21. If a non-`platform_admin` caller passes a `tenant_id` query param to an export, then the system shall ignore it or reject it, and shall not return another tenant's rows.

### Roles

22. When a caller with `admin` or `dispatcher` requests the IFTA, orders, jobs or reconciliation export, the system shall return 200 with CSV. (D5)
23. When a caller with `admin` requests the invoice export, the system shall return 200 with CSV. (D5)
24. If a caller with `dispatcher` but not `admin` requests the invoice export, then the system shall return 403 `INSUFFICIENT_ROLE`. (D5)
25. If a caller holding only `driver` requests any export, then the system shall return 403. (D5)
26. If a customer user (any session without `admin` or `dispatcher`) requests any export, then the system shall return 403. (D5)
27. While commerce invoicing is disabled for the tenant, when any caller requests the invoice export, the system shall return 404, matching `require_invoicing_enabled`.
28. The system shall enforce the export role on the reconciliation export even though the current list handler has no explicit role check. (See Assumption A5)

### PII

29. The system shall not include driver phone, driver email or driver license number columns or values in any v1 export. (D6)
30. The system shall not include customer contact email or customer contact phone columns or values in any v1 export. (D6)
31. The system shall include customer name and delivery address in exports whose rows carry them. (D6)

### Date filters (G1, G2)

32. When `GET /api/fuel/mvp/reconciliation` or `GET /api/commerce/invoices` is called with valid `start_date` and/or `end_date`, the system shall return only records inside that range. (D7)
33. If `start_date` or `end_date` is not a valid date/datetime, then the system shall return 422. (D7)
34. If `start_date` is after `end_date`, then the system shall return 422.
35. When neither date param is supplied, the system shall return the same results as before this change. (D7)
36. When the reconciliation or invoice export is called with `start_date`/`end_date`, the system shall apply the same range. (D7)

### HOS and DVIR lists (G3)

37. The system shall provide a tenant-wide, admin-only `GET` list endpoint for HOS records and one for vehicle inspections (DVIR). (D8)
38. When either endpoint is called with `driver_id`, `start_date` and/or `end_date`, the system shall return only the caller's tenant records matching those filters. (D8)
39. The system shall page both endpoints with a bounded page size and return paging metadata. (D8)
40. If a `dispatcher` or `driver` calls either endpoint, then the system shall return 403. (D8)
41. If a date param is invalid, then the system shall return 422.
42. When a tenant-A admin calls either endpoint, the system shall return no tenant-B records.
43. The system shall add no UI for these endpoints in v1. (D8)
44. The system shall leave `GET /api/driver/hos` and `POST /api/driver/inspections` behavior unchanged.

### Stale checkbox (G4)

45. The system's spec `.kiro/specs/frontend-feature-parity/tasks.md` task 10.3 shall no longer read `[x]` without qualification, and shall reference commit `76a9ef2` as the removal. (D9; see Assumption A6)

### Frontend

46. The UI shall show an "Export CSV" button on the IFTA report, orders, scheduling/jobs, reconciliation and invoices list pages. (D10)
47. While the signed-in user lacks the export role for that page, the UI shall not render the button (invoices: `admin`; others: `admin`, `dispatcher`). (D10)
48. When the user clicks the button, the UI shall request the export route with the page's current filters as query params. (D10)
49. When the export succeeds, the UI shall fetch with the cookie session, read the body as a `Blob` and save it under the `Content-Disposition` filename. (D10)
50. While an export is in flight, the UI shall disable the button, set `aria-busy="true"` and ignore further clicks.
51. If the export returns 413, then the UI shall show a message saying the result exceeds 50,000 rows and to narrow the filters. (D10)
52. If the export returns 429, then the UI shall show a message saying too many exports were requested and to try again shortly. (D10)
53. If the export returns 403, then the UI shall show a message saying the user doesn't have permission to export this data. (D10)
54. If the export fails for any other reason, then the UI shall show a generic retry message.
55. The button shall be a native `<button>` with an accessible name naming the data exported, operable by keyboard with visible focus, and errors shall be announced to assistive technology. (D10)
56. The button shall reuse the project's existing button and toast/alert components and styling conventions. (D10)

### Tests

57. The backend test suite shall include, for each of the five exports: a tenant-isolation test, role allow and deny tests, a CSV-injection test, a row-cap test, and a date-filter test where the export takes dates (orders, jobs, reconciliation, invoices; IFTA covers `quarter` validation instead). (D11)
58. The backend test suite shall include tests for the G1/G2 params (valid range, invalid value 422, reversed range 422, omitted = unchanged) and for the G3 endpoints (admin allow, dispatcher/driver deny, filters, paging, tenant isolation).
59. The backend test suite shall include tests for BOM presence, header row, filename format and the audit log fields.
60. The frontend test suite shall include Jest + Testing Library tests per button: role visibility, filters passed, Blob download, and the 413/429/403 messages.

## Assumptions

- A1 Row-cap status code: the owner allowed "413/422". This spec picks 413 (Payload Too Large) because the frontend error contract (D10) names 413, and 422 is already used for invalid filters. The cap must be enforced before the first byte is streamed so the client never gets a partial file; how (count query first, or buffering) is a design decision.
- A2 Per-user rate limit: the existing `limiter` keys by client IP by default (`rate_limiter.py:164`). "Per user" means a key of the form `tenant:user`, following the `driver_rate_key` precedent (`:86`), with IP fallback. The limit value is a design decision.
- A3 Filename `<tenant>` is the tenant ID, reduced to `[A-Za-z0-9_-]`, because tenant display names may contain characters unsafe in headers.
- A4 Date params are inclusive ISO-8601 dates or datetimes. The date field each range applies to (e.g. delivery date vs created date for reconciliation; invoice date for invoices) is fixed in the design.
- A5 `list_reconciliation_records` has no explicit role check in the handler that I could find (`fuel_ops_endpoints.py:3941` onward). The export adds `admin` + `dispatcher` regardless. Tightening the existing list endpoint is out of scope here and should be raised separately, since it would change what a driver session can read today.
- A6 `.kiro/` is gitignored (`.gitignore:122`), so the frontend-feature-parity spec does not exist in this worktree. G4 is a local edit to `/Users/olukotunjosh/Downloads/Runsheet/.kiro/specs/frontend-feature-parity/tasks.md:142` in the main checkout and is not committed.
- A7 "Customers" in D5 means any session without `admin` or `dispatcher`. There is no `customer` role in `CANONICAL_ROLES`; this covers any future customer-portal session too.
- A8 "HOS records" means duty-status events (`duty_status_events`). Including HOS gate overrides (`hos_gate_overrides`) is a design decision.
- A9 `platform_admin` keeps whatever cross-tenant behavior `get_tenant_context` already gives it on each list endpoint. No new staff capability is added.
- A10 Export sort is limited to immutable, always-populated keyset fields (orders: `created_at`; jobs: `scheduled_time`, `created_at`). Other values that are valid for the list return 422 `not_supported_for_export` on the export. AC13 applies to every other filter. AC14 compares record sets, not order. (Added in design revision 2, review finding 4.)
- A11 AC4 applies to string-typed values. Numbers produced by the models (`int`, `float`, `Decimal`) are written unescaped, so negatives stay numeric in spreadsheets (design DD-3). (Added in design revision 2, review finding 14.)

## Out of Scope

- XLSX or any formatted workbook, and any new library (`openpyxl`, `pandas`, `xlsxwriter`, SheetJS, papaparse).
- Async or emailed export jobs. Over-cap requests are rejected, not queued.
- T2 exports (driver utilization/hours, driver qualification expiry, tank inventory/K-factor, meter calibration audit, terminal BOLs), Failure Analytics CSV (E6), AR aging, payments, price books, rack prices, notifications, fleet list.
- UI for the HOS and DVIR list endpoints, and CSV export of them.
- Changing roles or response shapes of existing list endpoints, apart from the additive G1/G2 date params.
- Adding a role check to the existing reconciliation list endpoint (see A5).
- The CSV import side (`import_endpoints.py`, `services/import_service.py`, `fuel/intake/csv_adapter.py`), owned by the data-integrations investigation.
- Staging deploy. Deploys follow `.kiro/steering/autonomy.md` (pinned worktree, CI green on the exact commit, UI ancestor check, CodeBuild ownership by the other agent).
