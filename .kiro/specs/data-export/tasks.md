# Implementation Plan: Tenant data export (CSV), v1

Sources: `requirements.md` (approved, A1–A11) and `design.md` (revision 3, approved on review pass 3 with 3 NITs, folded into tasks 3 and 4 below). This plan sequences the design and does not re-decide it. The design's `§n` / `DD-n` references are the spec for each task.

## Ground rules for the coder

- Worktree: `/Users/olukotunjosh/Downloads/Runsheet/.worktrees/data-export`, branch `production-readiness/data-export`, base `7cae435`. Use absolute paths. A relative path resolves against the parent workspace `/Users/olukotunjosh/Downloads/Runsheet` and edits the wrong checkout.
- Commit locally after each task or pair of tasks with a Conventional Commit message. Do not push or deploy. Deploys belong to a later workflow step and go through CodeBuild (owned by the other agent).
- Do not touch these files (owned by the parallel data-integrations-fixes workflow or the codebuild worktree): `Runsheet-backend/services/import_service.py`, `Runsheet-backend/import_endpoints.py`, `runsheet/src/services/importApi.ts`, anything under `inventory/`, `notifications/`, the `integrations/` endpoints, `Runsheet-backend/data_endpoints.py`, `.worktrees/codebuild`. Read `importApi.ts` as a pattern only.
- `.kiro/` is gitignored (`.gitignore:122`). Spec files, this plan included, are never committed.
- Scratch files go in `/Users/olukotunjosh/Downloads/Runsheet/tmp/` (create it with `mkdir -p`), never `/tmp`. Delete them when done.
- Disk was at 2.2 GB free during planning. Run `df -h /Users/olukotunjosh/Downloads/Runsheet` before `npm ci`. If it's under 3 GB, clear regenerable caches first (`npm cache clean --force`, `pip cache purge`, `uv cache clean`, `brew cleanup`). Those are pre-approved.

## Commands (taken from `.github/workflows/ci.yml`)

Backend. The worktree has no venv, so use the main checkout's Python 3.11 venv (CI uses 3.11). Run from `/Users/olukotunjosh/Downloads/Runsheet/.worktrees/data-export/Runsheet-backend` with the CI environment:

```bash
export ENVIRONMENT=test JWT_SECRET=ci-test-jwt-secret JWT_ALGORITHM=HS256 REDIS_URL=redis://localhost:6379
PY=/Users/olukotunjosh/Downloads/Runsheet/Runsheet-backend/venv/bin/python
# Targeted runs. --no-cov is required: pytest.ini addopts + .coveragerc fail_under=70 fail any subset run.
$PY -m pytest <paths> -q --no-cov -p no:cacheprovider
# Full suite, exactly as CI's backend-tests job runs it:
$PY -m pytest --cov=. --cov-report=html:coverage_html --cov-report=xml:coverage.xml --cov-report=term-missing --cov-fail-under=70 -x -q
$PY scripts/check_coverage.py --threshold 0        # changed-file coverage gate (CI step 2)
# Endpoint registry job. Required because this change adds routes:
$PY scripts/generate_endpoint_registry.py && git -C .. diff --stat docs/endpoint-registry.md   # commit the regenerated file
# Document-store tests (CI migration-check job) need Postgres via POSTGRES_TEST_URL. Run them if a local Postgres is available; otherwise say they were not run.
$PY -m pytest tests/postgres -q --no-cov -p no:cacheprovider
```

git-hygiene job: `main.py` must stay at or under 200 effective lines (169 at base). Task 2's CORS edit adds a string to an existing list, so the count doesn't change. Re-check it with `grep -cvE '^\s*$|^\s*#|^\s*(import |from )' Runsheet-backend/main.py`.

Frontend (dispatcher-ui job). Run from `/Users/olukotunjosh/Downloads/Runsheet/.worktrees/data-export/runsheet` (run `npm ci` once first, since the worktree has no `node_modules`):

```bash
npx tsc --noEmit
npm run lint            # biome check
npm test -- --ci        # jest; targeted: npx jest <path> --ci
NEXT_PUBLIC_API_URL=http://localhost:8080/api NEXT_PUBLIC_WS_URL=ws://localhost:8080 \
NEXT_PUBLIC_SITE_URL=http://localhost:3000 NEXT_PUBLIC_ST_API_DOMAIN=http://localhost:8080 \
NEXT_PUBLIC_ST_WEBSITE_DOMAIN=http://localhost:3000 npm run build
```

Delete `runsheet/.next` and `Runsheet-backend/coverage_html` / `coverage.xml` after verifying, or leave them if they're gitignored. Check with `git status`.

## Workflow decision

This plan was not split into FEAT artifacts. The workflow already ends in `build-loop → release → ci-wait-loop → deploy-verify`. A `replace_remaining` would drop those three later steps, and their definitions can't be read back to re-create them faithfully. The existing implement-and-review loop runs this plan, with its gate unchanged: `/Users/olukotunjosh/Downloads/Runsheet/.agents/tasks/spreadsheet-export-audit-2026-10-04/data-export-review.json`, `verdict == "APPROVED"`.

---

# Implementation Plan

## A. Shared foundation

- [x] 1. Add the date-range parser `services/date_range.py` and its unit tests. (5e9e3e1)
      `DateRange(gte, lt, lte)` and `parse_date_range(start_date, end_date)` per design §1 "Date range parser". Accept `YYYY-MM-DD` or ISO datetime, with or without a trailing `Z`. Naive values are UTC and aware values are converted to UTC. Strip whitespace and reject values over 40 chars. A date-only end becomes `lt` = next midnight UTC. A datetime end becomes `lte`. Invalid input or start > end raises `AppException(ErrorCode.VALIDATION_ERROR, status_code=422, details={"field", "value"|"reason": "after_end_date"})`, the same shape as `_list_param_error` (`fuel/api/order_endpoints.py:748`). Add one formatter, `to_doc_bound(dt, *, z_suffix: bool) -> str`, that returns `dt.astimezone(timezone.utc).isoformat()`, with `+00:00` replaced by `Z` when `z_suffix` is set. Review NIT 3: reconciliation `generated_at` is stored with `Z` (`services/reconciliation_service.py:375-382`), so G1 passes `z_suffix=True`. Before choosing the suffix for the other doc-store fields, check how each one is written.
      Files: `Runsheet-backend/services/date_range.py` (new), `Runsheet-backend/tests/unit/test_date_range.py` (new)
      Verify: `$PY -m pytest tests/unit/test_date_range.py -q --no-cov`. All tests pass, covering date-only, datetime, `Z`, offset conversion, inclusive end day, invalid → 422, reversed → 422, both omitted → all `None`, over 40 chars → 422.

- [x] 2. Add the shared CSV export helper `services/csv_export.py`, plus the error code, setting and CORS header it needs. (5e9e3e1)
      Implement the design §1 public API: `MAX_EXPORT_ROWS=50_000`, `EXPORT_PAGE_SIZE=200`, `ExportType`, `ExportColumn`, `ExportSource`, `KeysetPage(rows, raw_count, total, last_key)`, `KeysetSource` (the loop stops on `raw_count < page_size or last_key is None`, and `count()` calls `fetch(None, 1, True).total` unless a `count` callable is passed), `StaticSource`, `escape_cell` (DD-3: only string values get the `'` prefix for `= + - @ \t \r`; numbers, bools, dates, lists and dicts follow the table), `export_filename`, `export_guard(*allowed, base=get_tenant_context)` (same shape as `roles_dependency`, `auth/router_guards.py:43`, and stamps `request.state.export_user_id`), `export_rate_key`, `EXPORT_RATE_LIMIT`, `_loggable_filters` (`q` → `{present, length}`), and `stream_csv_export`. That function counts, then returns 413 `EXPORT_TOO_LARGE` with `{row_count, max_rows}` before any byte is sent, then streams BOM + header + one chunk per page. It aborts with `ExportCapExceededDuringStream` on the cap race and writes one `data_export` audit line in `finally` with one of the outcomes `completed`, `rejected_too_large`, `aborted_cap_race`, `client_disconnected`, `failed`. Headers: `text/csv; charset=utf-8`, `Content-Disposition`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. Supporting edits:
      - `errors/codes.py`: add `ErrorCode.EXPORT_TOO_LARGE` and map it to 413 in `ERROR_CODE_STATUS_MAP` (`:423`).
      - `config/settings.py`: add `export_rate_limit: int = Field(default=5, ge=1, le=1000, ...)` next to `ops_api_rate_limit` (`:643`).
      - `main.py`: add `"Content-Disposition"` to `expose_headers`. Put it on an existing line so the effective line count doesn't grow.
      Then write the pure unit tests listed in design §10 `test_csv_export_helper.py`: escaping, BOM/header/CRLF, filename sanitization, 413 before the generator runs, cap race abort, KeysetSource paging (5 rows, page 2), a page with `raw_count=2` and one mapped row that doesn't stop the loop, the size-1 count probe, audit fields, the `q` redaction check with caplog, and the `export_rate_key` stamp/fallback.
      Files: `Runsheet-backend/services/csv_export.py` (new), `Runsheet-backend/errors/codes.py`, `Runsheet-backend/config/settings.py`, `Runsheet-backend/main.py`, `Runsheet-backend/tests/unit/test_csv_export_helper.py` (new)
      Verify: `$PY -m pytest tests/unit/test_csv_export_helper.py -q --no-cov` passes. `$PY -m pytest tests/unit -k "codes or settings or cors" -q --no-cov` still passes. The main.py effective-line check prints ≤ 200.

- [x] 3. Add explicit keyset paging to the orders and jobs read paths (design §2, DD-4). (5e9e3e1)
      - `persistence/read_repositories.py` `HybridReadRepository.search` (`:645`): add keyword-only `after: Optional[tuple[str, str]] = None` and `with_total: bool = True`. When `after` is set, force offset 0 and add the DESC/ASC predicate with the pk tie clause from §2. `with_total=False` skips the count. Return `raw_count` and `last_key`. Review NIT 1: `last_key = (rows[-1].document.get(sort_field), getattr(rows[-1], self.pk_attr)) if rows else None`, so the pk comes from the ORM row.
      - `commerce/services/commerce_persistence_bridge.py` `read_hybrid_search` (`:1222`): pass `after` and `with_total` through.
      - `fuel/order_repository.py` `FuelOrderRepository.search` (`:776`) and `scheduling/services/job_service.py` `JobService.list_jobs` (`:916`): add keyword-only `keyset: bool = False`, `after=None` and `with_total: bool = True`. `after` without `keyset` raises `ValueError`. In keyset mode, the hybrid branch forwards `after`/`with_total`. The doc-store branch uses sort `[{field: dir}, {order_id|job_id: asc}]`, `from=0` and `search_after` only when `after` is set. Compute `raw_count` and `last_key = tuple(hits[-1]["sort"])` from the raw response before `_extract_sources` and the drops. A length other than 2 raises `RuntimeError`. Review NIT 2: in the orders doc-store keyset branch, use `pg_sort_field`/`pg_sort_order` (`order_repository.py:841-846`), because `sort_field`/`sort_order` are only bound when `sort` is set. Both methods return `raw_count`/`last_key` in both modes. With `keyset=False` the existing output is unchanged.
      - Tests: `tests/persistence/test_export_keyset_paging.py` (new), following `tests/persistence/test_hybrid_read_cutover.py` fixtures. Cover hybrid paging with ties for DESC and ASC plus tenant B noise, `with_total=False`, and parity when `after` is absent. On the doc-store path, use the real `keyset=True` adapters from `after=None` with `page_size=2`, 5 rows for tenant A of which 3 tie, plus tenant B rows. Assert `count()==5` and that every `last_key` has length 2. Also cover `ValueError` on `after` without `keyset`, the orders validation-drop case (2nd row invalid, 4 written), and jobs ASC with 5 tied `scheduled_time` values. Test the repository methods directly here. The export-adapter-level doc-store tests are added in tasks 7 and 8, once `_orders_export_fetch` and `_jobs_export_fetch` exist.
      Files: `Runsheet-backend/persistence/read_repositories.py`, `Runsheet-backend/commerce/services/commerce_persistence_bridge.py`, `Runsheet-backend/fuel/order_repository.py`, `Runsheet-backend/scheduling/services/job_service.py`, `Runsheet-backend/tests/persistence/test_export_keyset_paging.py` (new)
      Verify: `$PY -m pytest tests/persistence/test_export_keyset_paging.py tests/persistence/test_hybrid_read_cutover.py -q --no-cov` passes, and the existing order/job list tests still pass: `$PY -m pytest tests -k "order_repository or list_orders or list_jobs or job_service" -q --no-cov`.

## B. G1/G2 date params on existing lists

- [x] 4. G1: add `start_date`/`end_date` to `GET /api/fuel/mvp/reconciliation`, and factor out the shared reconciliation query pieces (design §3.4 refactor and §4 G1). (5e9e3e1)
      In `fuel/api/fuel_ops_endpoints.py`, extract `_reconciliation_must_clauses(tenant_id, order_id, plan_id, pod_id, date_range)` and `_validated_reconciliation_rows(hits, tenant_id)` from `list_reconciliation_records` (`:3941`, body up to the tenant-mismatch/validation drop around `:4060-4100`), with no change to list behavior. Add the two `Query(None)` params, parse them with `parse_date_range`, and add `range generated_at` using `to_doc_bound(..., z_suffix=True)` (task 1). No params means no clause. Write G1 tests in `tests/unit/test_list_date_filters.py` (new): valid range, date-only end includes the whole day, invalid → 422, reversed → 422, omitted → body identical to a baseline captured on the same fixture without the params. Note: `fleet-fuel-fixes` may also edit this file. Keep the refactor minimal.
      Files: `Runsheet-backend/fuel/api/fuel_ops_endpoints.py`, `Runsheet-backend/tests/unit/test_list_date_filters.py` (new)
      Verify: `$PY -m pytest tests/unit/test_list_date_filters.py -q --no-cov` passes, and the existing reconciliation list tests pass: `$PY -m pytest tests -k reconciliation -q --no-cov`.

- [x] 5. G2: add `start_date`/`end_date` to `GET /api/commerce/invoices`, forward the ignored `qbo_push_state` (DD-8), and add `InvoiceService.count` (design §3.5 and §4 G2). (5e9e3e1)
      - `commerce/services/invoice_service.py` (`list` at `:1761`): factor out `_invoice_must_clauses(status, customer_id, account_id, order_id, qbo_push_state, date_range)` and use it in `list` and the new `count`. Add the kwargs `qbo_push_state`, `created_from`, `created_before`, `created_until`. The doc path gets `term qbo_push_state` and `range created_at` (match the suffix to how invoice `created_at` is stored, per task 1). `count(...)` uses the PG bridge first and falls back to a doc query with `size: 1` returning `hits.total.value`.
      - `persistence/read_repositories.py` `InvoiceReadRepository.list` (`:265`): add typed `InvoiceORM.created_at >= / < / <=` and `InvoiceORM.qbo_push_state ==` filters, and a new `count(session, tenant_id, **filters)` over the same filter list.
      - `commerce/services/commerce_persistence_bridge.py`: add `read_invoice_count(tenant_id, **filters)` mirroring `read_invoice_list` (`:654`), returning `_NOT_CUT_OVER` (`:586`) when not cut over.
      - `commerce/api/invoice_endpoints.py` `list_invoices` (`:170`): add the date params (parse → datetimes) and pass `qbo_push_state` and the date bounds to the service.
      - Extend `tests/unit/test_list_date_filters.py` with G2 cases: valid range on both read paths, invalid/reversed → 422, omitted → unchanged baseline, `qbo_push_state` now filters, and `count` equals the length of the full list on both paths.
      Files: `Runsheet-backend/commerce/services/invoice_service.py`, `Runsheet-backend/persistence/read_repositories.py`, `Runsheet-backend/commerce/services/commerce_persistence_bridge.py`, `Runsheet-backend/commerce/api/invoice_endpoints.py`, `Runsheet-backend/tests/unit/test_list_date_filters.py`
      Verify: `$PY -m pytest tests/unit/test_list_date_filters.py -q --no-cov` passes, and the existing invoice tests pass: `$PY -m pytest tests -k invoice -q --no-cov`.

## C. The five exports, in build order (D2)

Shared test file: `Runsheet-backend/tests/unit/test_data_export_endpoints.py` (new), created in task 6 and extended by each export task. Follow `tests/unit/test_asset_compliance_endpoints.py` (`TestClient`, `app.dependency_overrides[get_tenant_context]` → `TenantContext(tenant_id, user_id, roles)`). Use fakes for the repo, service and reporter. Include the design §10 harness: an autouse `limiter.reset()` before and after each test, and `app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)` on the test app. Per export, cover: tenant isolation with A and B rows, plus `?tenant_id=B` ignored (AC20–21); roles admin and dispatcher 200, driver 403, `[]` 403, `platform_admin`-only 403 (invoices differ, see task 10); CSV injection on the field named in §10; row cap with `MAX_EXPORT_ROWS` monkeypatched to 2 and 3 rows → 413; the header row has no `phone/email/license/cdl` substring and seeded contact values are absent from the body (AC29–31); the rate limit, where call `export_rate_limit + 1` is 429 with `Retry-After` and user `u2` then gets 200 (one parametrized test across the five routes is enough); paging params absent from the export route's OpenAPI parameters.

- [x] 6. IFTA quarterly export `GET /api/compliance/ifta/report/export` (design §3.1). (5e9e3e1)
      Add `export_ifta_report` after `get_ifta_report` (`compliance/api/ifta_endpoints.py:161`). The router-level `compliance_ops_dependency` stays, and the route adds `Depends(export_guard("admin", "dispatcher"))`, `request: Request` and `@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)`. `quarter` is required (missing → 422). Malformed values go through `_validate_quarter` (400). Wrap errors exactly as `get_ifta_report` does. Rows are flattened truck × jurisdiction, followed by the `incomplete_trucks` rows, through a `StaticSource`. Use the 11 columns in §3.1. Create `test_data_export_endpoints.py` with the harness and the IFTA cases: the common set, missing quarter 422, malformed 400, incomplete trucks with `ifta_data_incomplete=true` and a reason, and injection `truck_id="=1"`.
      Files: `Runsheet-backend/compliance/api/ifta_endpoints.py`, `Runsheet-backend/tests/unit/test_data_export_endpoints.py` (new)
      Verify: `$PY -m pytest tests/unit/test_data_export_endpoints.py tests/unit/test_compliance_api_authz.py -q --no-cov` passes, as do the existing IFTA tests: `$PY -m pytest tests -k ifta -q --no-cov`.

- [x] 7. Orders export `GET /api/orders/export` (design §3.2). (5e9e3e1)
      Add `export_orders` between `list_orders` (`fuel/api/order_endpoints.py:782`) and `get_order` (`:844`). Use the same params and aliases as `list_orders`, validated by `_validate_list_params` (`:757`). Then apply the DD-4 sort check: `sort` must be in `{None, "created_at", "created_at:asc", "created_at:desc"}`, otherwise 422 `{"field": "sort", "reason": "not_supported_for_export"}`. Guard with `export_guard("admin", "dispatcher")` plus the limiter decorator. Build the source with a module-level `_orders_export_fetch` adapter that calls `repo.search(..., keyset=True, after=..., with_total=..., size=page_size)` and takes `raw_count`/`last_key` from the result, never `len(orders)`. Use the 23 columns in §3.2, with `delivered_at`/`delivered_gallons` taken from `delivery_result`. Tests: the common set, injection `customer_name="=HYPERLINK(...)"`, `sort=updated_at:desc` → 422, the route-shadowing check (`/api/orders/export` reaches the export handler), and date params passed through unchanged (parity, DD-7). Add the doc-store adapter test from design §10 to `tests/persistence/test_export_keyset_paging.py`: `COMMERCE_READ_FROM_POSTGRES` off, `_orders_export_fetch` in a `KeysetSource` from `after=None`, `page_size=2`, ties, DESC and ASC, a validation-drop row, and no hand-built `search_after`.
      Files: `Runsheet-backend/fuel/api/order_endpoints.py`, `Runsheet-backend/tests/unit/test_data_export_endpoints.py`, `Runsheet-backend/tests/persistence/test_export_keyset_paging.py`
      Verify: `$PY -m pytest tests/unit/test_data_export_endpoints.py tests/persistence/test_export_keyset_paging.py -q --no-cov` passes, and the existing order endpoint tests pass: `$PY -m pytest tests -k "order_endpoints or orders" -q --no-cov`.

- [x] 8. Jobs / dispatch sheet export `GET /api/scheduling/jobs/export` (design §3.3). (5e9e3e1)
      Add `export_jobs` right after `list_jobs` (`scheduling/api/endpoints.py:250`), before `/jobs/active` (`:304`) and `/jobs/{job_id}` (`:396`). It carries only `@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)`, not `_scheduling_rate`. Params match the list. `sort_by` must be in `{scheduled_time, created_at}`, otherwise 422 `not_supported_for_export`. Other validation stays in `JobService.list_jobs` (400 parity). Add a module-level `_jobs_export_fetch` adapter (`keyset=True`). Use the 18 columns in §3.3. Tests: the common set, injection `notes="+cmd"`, `sort_by=priority` → 422, route shadowing, date pass-through parity, and the doc-store adapter test with 5 tied `scheduled_time` values in ASC order.
      Files: `Runsheet-backend/scheduling/api/endpoints.py`, `Runsheet-backend/tests/unit/test_data_export_endpoints.py`, `Runsheet-backend/tests/persistence/test_export_keyset_paging.py`
      Verify: the same two test files pass, and the existing scheduling tests pass: `$PY -m pytest tests -k "scheduling or jobs" -q --no-cov`.

- [x] 9. Reconciliation register export `GET /api/fuel/mvp/reconciliation/export` (design §3.4). (5e9e3e1)
      Add `export_reconciliation_records` on `mvp_router` (`fuel_ops_endpoints.py:419`) with `export_guard("admin", "dispatcher")` (A5: the list stays as it is) and the limiter. Params: `order_id`, `plan_id`, `pod_id`, `min_variance_pct` (`ge=0.0`), `start_date`, `end_date`. Build the query from `_reconciliation_must_clauses` (task 4). Push `min_variance_pct` down as `bool.should` of three `range gte` clauses with `minimum_should_match: 1`, and add no `exists` clause. Sort `[{generated_at: desc}, {reconciliation_id: asc}]` on every call. The fetch adapter sets `raw_count`/`last_key` from the raw hits, then calls `_validated_reconciliation_rows`. Use the 19 columns in §3.4. Tests: the common set (the driver gets 403 here although the list lets it through, AC28), injection `alert_flags=["@x"]`, date range in-range only, inclusive end day, invalid/reversed 422 (AC36). Add to `test_export_keyset_paging.py`: push-down parity against the list's post-hoc filter on a mixed fixture that includes `variance_invoiced_vs_delivered_pct=None`, and the validation-drop case (5 records, 2nd invalid → 4 written).
      Files: `Runsheet-backend/fuel/api/fuel_ops_endpoints.py`, `Runsheet-backend/tests/unit/test_data_export_endpoints.py`, `Runsheet-backend/tests/persistence/test_export_keyset_paging.py`
      Verify: the same two test files pass, plus `$PY -m pytest tests -k reconciliation -q --no-cov`.

- [x] 10. Invoice register export `GET /api/commerce/invoices/export` (design §3.5). (5e9e3e1)
      Add `export_invoices` between `list_invoices` (`commerce/api/invoice_endpoints.py:170`) and `get_invoice` (`:267`), guarded by `export_guard("admin", base=require_invoicing_enabled)` plus the limiter. The order of checks is: flag 404, then the ops-role 403, then the admin 403. Params: `status: Optional[InvoiceStatus]`, `customer_id`, `account_id`, `qbo_push_state`, `start_date`, `end_date`. The source is a `KeysetSource` with `count=InvoiceService.count(...)` (task 5). The fetch adapter loops `service.list(..., cursor=after[1] if after else None, limit=200)`, returning `last_key=(None, next_cursor)` or `None` and `raw_count=len(items)`. Use the 17 columns in §3.5, with `total_gallons` = the sum of `line_items[].quantity_gallons`. Tests: admin 200, dispatcher 403 `INSUFFICIENT_ROLE` with `required_roles == ["admin"]`, driver/`[]`/`platform_admin`-only 403, flag off → 404 for admin and dispatcher (AC27), injection `customer_id="-1+1"`, the date range (AC36), the cap, isolation, route shadowing.
      Files: `Runsheet-backend/commerce/api/invoice_endpoints.py`, `Runsheet-backend/tests/unit/test_data_export_endpoints.py`
      Verify: `$PY -m pytest tests/unit/test_data_export_endpoints.py -q --no-cov` passes, plus `$PY -m pytest tests -k invoice -q --no-cov`.

## D. G3: admin-only HOS and DVIR lists

- [x] 11. Add `compliance/api/driver_records_endpoints.py` with `GET /api/compliance/hos-records` and `GET /api/compliance/inspections` (design §5), and register it. (5e9e3e1)
      - `compliance/api/_authz.py`: add `COMPLIANCE_ADMIN_ROLES = ("admin",)` and `compliance_admin_dependency = roles_dependency(*COMPLIANCE_ADMIN_ROLES)`.
      - The router uses prefix `/api/compliance` with `dependencies=[Depends(compliance_ops_dependency), Depends(compliance_admin_dependency)]`, keeping the literal that `tests/unit/test_compliance_api_authz.py` greps for. Params: `driver_id` (max 128, stripped), `start_date`/`end_date` via `parse_date_range` (422), `page ≥ 1`, `size` 1–200 with default 50. `page*size > 10_000` → 422 `{"field": "page", "reason": "window_exceeded"}`. The query uses `inject_tenant_filter` + `term driver_id` + `range event_timestamp|inspection_timestamp`, with the 2-key sort from §5. Re-check `tenant_id` on each `_source` (drop + WARNING). Strip `tenant_id` from items. Respond with `paginated_response_dict(items=..., total=..., page=..., page_size=size, request_id=...)` (`schemas/common.py:104`). Indices are `duty_status_events` and `vehicle_inspections` (`driver/services/driver_es_mappings.py:27,30`). `hos_gate_overrides` is out of scope.
      - `bootstrap/compliance_routers.py`: add the router to `_compliance_routers()` (`:44`) and change "eight" to "nine" in the docstrings (`:4`, `:11`, `:45`).
      - `tests/unit/test_module_gating.py`: add `"driver_records_endpoints"` to the pinned tuple (`:153-162`) and rename the test to `test_compliance_module_registers_all_nine_endpoint_modules`.
      - Tests `tests/unit/test_driver_records_endpoints.py` (new): admin 200; dispatcher and driver 403; driver_id and date filters; invalid date 422; paging metadata; `size=201` → 422; window 422; tenant isolation, including a B doc that leaks past the fake query and gets dropped.
      Files: `Runsheet-backend/compliance/api/driver_records_endpoints.py` (new), `Runsheet-backend/compliance/api/_authz.py`, `Runsheet-backend/bootstrap/compliance_routers.py`, `Runsheet-backend/tests/unit/test_module_gating.py`, `Runsheet-backend/tests/unit/test_driver_records_endpoints.py` (new)
      Verify: `$PY -m pytest tests/unit/test_driver_records_endpoints.py tests/unit/test_module_gating.py tests/unit/test_compliance_api_authz.py -q --no-cov` passes, and the existing driver HOS/inspection tests pass unchanged (AC44): `$PY -m pytest tests -k "hos or inspection" -q --no-cov`.

- [x] 12. Regenerate the endpoint registry for the 7 new routes and run the full backend gate. (5e9e3e1)
      Run `$PY scripts/generate_endpoint_registry.py` and commit the updated `docs/endpoint-registry.md`. Then run the full CI backend command and `scripts/check_coverage.py --threshold 0`. Fix any regression or any changed file with zero coverage. Run `tests/postgres` if a local Postgres is reachable. Otherwise, record that it wasn't run.
      Files: `docs/endpoint-registry.md`
      Verify: the full suite passes with `--cov-fail-under=70`, `check_coverage.py` exits 0, and a rerun of the registry script leaves `git diff --exit-code docs/endpoint-registry.md` clean.

## E. Frontend

- [x] 13. Add `runsheet/src/services/exportApi.ts` and its tests (design §7). (5e9e3e1)
      `ExportType`, `EXPORT_PATHS`, `EXPORT_TIMEOUT_MS = 120_000`, `filenameFromContentDisposition(header, fallback)`, `downloadCsvExport(type, params)`. Mirror `services/importApi.ts:17-53` for `API_BASE_URL`, `fetchWithSession(fetchWithTimeout, ...)` (`services/api.ts:243`, `services/utils.ts:65`) and `apiErrorFromResponse` (`services/apiErrors.ts:105`). Build the query with `buildQueryString` (`services/utils.ts:48`). Send `Accept: text/csv`. Keep the `fetch` and the `blob()` read in one `try`, so a rejected `blob()` or a network error becomes `ApiError(status 0)`. Save through a temporary `<a download>` with `URL.createObjectURL`/`revokeObjectURL`. The fallback name is `${type}_export.csv`. Tests in `src/services/exportApi.test.ts`: URL and query per type with empty values dropped, `credentials: "include"`, header filename and fallback, non-OK → `ApiError` with status, and an ok response whose `blob()` rejects → status 0.
      Files: `runsheet/src/services/exportApi.ts` (new), `runsheet/src/services/exportApi.test.ts` (new)
      Verify: `npx jest src/services/exportApi.test.ts --ci` passes, and `npx tsc --noEmit` is clean.

- [x] 14. Add `runsheet/src/components/ui/ExportCsvButton.tsx`, export it from `components/ui/index.ts`, and write its tests (design §7, DD-12, DD-15). (5e9e3e1)
      Props are `type`, `params`, `allowedRoles`, `subject` and `rolesOverride`. Roles are resolved on mount via `getCurrentUserRoles()` (`utils/auth.ts:44`) with the cancel-on-unmount pattern from `components/SetupHub.tsx:59-68`. Render nothing while roles are unresolved or `hasAnyRole` (`config/modules.ts:99`) is false. Use the existing `Button` (`variant="secondary" size="sm"`, `loading`, `aria-busy`) with a `lucide-react` `Download` icon (`aria-hidden`) and the label `Export CSV<span className="sr-only">: {subject}</span>`, without an `aria-label`. A `useRef` in-flight guard prevents a second request. Show errors in an adjacent `<p role="alert">` and success in an sr-only `role="status"` message. Message text for 413/429/403/other comes from the design §7 table, verbatim. Tests in `src/components/ui/ExportCsvButton.test.tsx`: hidden when roles are unresolved or not allowed; shown when allowed; `getByRole("button", { name: "Export CSV: orders" })` matches and the button has no `aria-label`; Enter and Space activate it; `aria-busy` and disabled while in flight; a double click sends one request; each of the four messages appears in `role="alert"`.
      Files: `runsheet/src/components/ui/ExportCsvButton.tsx` (new), `runsheet/src/components/ui/index.ts`, `runsheet/src/components/ui/ExportCsvButton.test.tsx` (new)
      Verify: `npx jest src/components/ui/ExportCsvButton.test.tsx --ci` passes, and `npx tsc --noEmit` and `npm run lint` are clean.

- [x] 15. Put the button on the five pages, with per-page tests (design §7 page table, DD-11: no new date inputs). (5e9e3e1)
      - `components/compliance/IFTAReportPage.tsx`: in the `PageHeader` `actions` (`:311`), add `type="ifta"`, `params={{ quarter }}`, `subject="IFTA report"`, `allowedRoles={["admin","dispatcher"]}`. Extend `IFTAReportPage.test.tsx`.
      - `components/ops/OrdersPage.tsx`: in the `PageHeader` `actions` (`:389`), pass the `filters` memo (`:222-235`) minus `page`/`size`. Extend `OrdersPage.test.tsx`.
      - `app/ops/scheduling/page.tsx`: in the header row next to "Create Job" (`:204-214`), use the same mapping as `apiFilters` (`:59-65`). New `app/ops/scheduling/page.export.test.tsx`.
      - `components/ops/ReconciliationPage.tsx`: in the header row (`:1363-1373`), pass `{order_id, plan_id, pod_id, min_variance_pct}` from `filters` (`:165-170`). Extend `ReconciliationPage.test.tsx`.
      - `components/commerce/InvoicesListPage.tsx`: in the `PageHeader` `actions` (`:127`), pass `{ status: statusFilter, customer_id: customerFilter }` with `allowedRoles={["admin"]}`. Extend `components/commerce/__tests__/InvoicesListPage.test.tsx`.
      Each page test mocks `downloadCsvExport` and sets roles through `rolesOverride` or by mocking `getCurrentUserRoles`. It checks that the button shows for allowed roles and is hidden otherwise (for invoices, a dispatcher sees no button), and that changing one filter then clicking calls `downloadCsvExport` with that page's current params.
      Files: the five page files above, their test files, and `runsheet/src/app/ops/scheduling/page.export.test.tsx` (new)
      Verify: `npm test -- --ci` (the full jest suite) passes, along with `npx tsc --noEmit`, `npm run lint`, and the `npm run build` command from the Commands section.

## F. G4 and final gate

- [x] 16. G4: annotate the stale task 10.3 checkbox (design §6, requirements A6). (5e9e3e1)
      `.kiro/` is gitignored, and the file isn't present in the worktree (`.worktrees/data-export/.kiro/specs/` contains only `data-export`). Edit it in the main checkout, `/Users/olukotunjosh/Downloads/Runsheet/.kiro/specs/frontend-feature-parity/tasks.md`, line 142. Change `- [x] 10.3 Add CSV export functionality` to `- [ ] 10.3 Add CSV export functionality` and add an indented line: `- Removed in 76a9ef2. Replacement tracked as E6 (deferred) in .kiro/specs/data-export (Failure Analytics CSV).` Change nothing else in that file and don't commit it.
      Files: `/Users/olukotunjosh/Downloads/Runsheet/.kiro/specs/frontend-feature-parity/tasks.md`
      Verify: `sed -n 140,147p` on that file shows the unchecked box and the `76a9ef2` note. `git -C /Users/olukotunjosh/Downloads/Runsheet status --short .kiro` shows nothing, because the path is ignored.

- [x] 17. Run the cross-cutting final verification and record the evidence. (5e9e3e1)
      Run every CI job's command locally: full backend pytest with coverage, `check_coverage.py`, the registry diff, the main.py line count, the git-hygiene checks (`git ls-files -i --exclude-standard` returns nothing; no `.coverage`/`__pycache__` tracked), and frontend tsc, lint, test and build. Confirm `git diff 7cae435 -- Runsheet-backend/requirements*.txt runsheet/package.json` is empty (AC2). Confirm none of the off-limits files were changed: `git diff --stat 7cae435 -- Runsheet-backend/services/import_service.py Runsheet-backend/import_endpoints.py Runsheet-backend/data_endpoints.py runsheet/src/services/importApi.ts` is empty. Write the results (commands, pass counts, anything not run and why, such as `tests/postgres` without a local Postgres) to `/Users/olukotunjosh/Downloads/Runsheet/.agents/tasks/spreadsheet-export-audit-2026-10-04/data-export-evidence.md`. Delete scratch files and build output.
      Files: `/Users/olukotunjosh/Downloads/Runsheet/.agents/tasks/spreadsheet-export-audit-2026-10-04/data-export-evidence.md` (new)
      Verify: every command above exits 0, and `git status` in the worktree shows a clean tree, with all work committed locally.

## Notes and assumptions

- G4 path. The step prompt names `.worktrees/data-export/.kiro/specs/frontend-feature-parity/tasks.md:142`, but that file doesn't exist in the worktree. Requirements A6 and design §6 both name the main-checkout copy, and that copy has `- [x] 10.3 Add CSV export functionality` at line 142. Task 16 edits the main-checkout copy.
- Rebase. Before the PR (release step, not this plan), rebase onto the current `production-readiness/go-live-blockers` (now `8b50e68`). `fleet-fuel-fixes` may conflict in `fuel/api/fuel_ops_endpoints.py`.
- No staging deploy happens in this plan. The workflow's later `deploy-verify` step owns that, under the steering deploy rules (pinned worktree, CI green on the exact commit, UI ancestor check, CodeBuild owned by the other agent).
