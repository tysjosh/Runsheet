# Design: Tenant data export (CSV), v1

Requirements: `.kiro/specs/data-export/requirements.md` (approved; A10–A11 added in revision 2). Revision 3 of this design (answers review pass 2). Worktree `.worktrees/data-export`, branch `production-readiness/data-export`, base `7cae435`. All `file:line` references below are to that worktree (`Runsheet-backend/` and `runsheet/src/` prefixes omitted where obvious) at `7cae435`.

## Overview

One new backend module, `Runsheet-backend/services/csv_export.py`, owns everything that makes an export safe: cell escaping, BOM, header row, keyset paging driver, the 50,000-row cap (checked before the first byte), per-user rate-limit key, role guard dependency, filename and the audit line. Each of the five exports is a thin `GET <list route>/export` handler next to its list handler. It reuses the list handler's parameters and validation, builds an `ExportSource` (a count plus an async page iterator over the same filtered query the list uses), declares a fixed column list, and calls `stream_csv_export(...)`. No handler writes CSV or sets `Content-Disposition` itself.

To page past the list caps (100/200/500) without offset scans, the repositories gain an additive keyset option: `HybridReadRepository.search` (Postgres relational read path) and the document-store/ES query path both accept an `after` boundary of `(sort_value, id)`. Invoices already page by keyset on both paths, so their export loops the existing cursor. IFTA is computed in memory by `IFTAReporter`, so its source is a single page.

G1/G2 add typed `start_date`/`end_date` to the reconciliation and invoice lists through one shared parser, `services/date_range.py`. G3 adds an admin-only compliance router with tenant-wide HOS and DVIR lists. The frontend gets one `ExportCsvButton` component and one `exportApi.downloadCsv` service function, dropped into the five pages.

Technology stack (locked): Python 3 / FastAPI `StreamingResponse`, stdlib `csv`, `io`, `json`, `re`, `datetime`; slowapi via the existing `middleware.rate_limiter.limiter`; SQLAlchemy via the existing read repositories. Frontend: Next.js/React + TypeScript, Tailwind, `lucide-react` icons, Jest + Testing Library. No new dependency on either side.

## Codebase facts this design relies on

| Fact | Where |
|---|---|
| The ES facade is the Postgres document store. `search_documents` returns an exact `hits.total` computed over the full query scope (not the page), honors `search_after`, and caps a page at 10,000 | `services/elasticsearch_service.py:113-119,484-500`; `persistence/document_store.py:85,605-700` (total `:667-672`, `search_after` `:658-666`) |
| `search_after` compiles to a strict lexicographic keyset over the sort keys; the ORDER BY gets a doc-id tiebreak but the keyset does not, so the sort itself must end on a unique id field | `persistence/document_query.py:774-796` (`build_order_by`, tiebreak), `:842-866` (`sort_values_of`), `:869-899` (`build_search_after`) |
| Orders and jobs have two read paths: `read_hybrid_search` (relational, when `COMMERCE_READ_FROM_POSTGRES` is on and the aggregate is registered) else the document-store query | `fuel/order_repository.py:776-958`; `scheduling/services/job_service.py:916-1046`; `commerce/services/commerce_persistence_bridge.py:1130-1145,1222-1265` |
| `HybridReadRepository.search` is offset-only, clamps `size` to 200, orders by `(document field, pk ASC)` | `persistence/read_repositories.py:43-50,519-541,645-760` |
| `_keyset_page` already implements `(sort DESC, id ASC)` keyset for invoices; the doc-store path uses `search_after_for_cursor` | `persistence/read_repositories.py:77-120,265-290`; `commerce/services/invoice_service.py:1761-1840`; `services/keyset_pagination.py:113-165` |
| `list_invoices` accepts `qbo_push_state` but never passes it to the service, so the filter is a no-op today | `commerce/api/invoice_endpoints.py:170-215` |
| Invoice feature-flag 404 then ops-role 403 | `commerce/api/invoice_endpoints.py:82-123` (`require_commerce_ops` at `:120`); `commerce/api/_authz.py:44-47` |
| Router-level role gates, and the "flag before role" ordering rule | `auth/router_guards.py:1-75` (`roles_dependency` `:43`) |
| Exact-match role check; rejects with `INSUFFICIENT_ROLE` 403 and `details.required_roles` | `auth/authorization.py:36-72`; `errors/codes.py:126` |
| `get_tenant_context` stamps `request.state.tenant_id` (not `user_id`) | `ops/middleware/tenant_guard.py:299-313,371-377` |
| Limiter is IP-keyed; per-identity precedent `driver_rate_key`; 429 body `RATE_LIMITED` with `Retry-After` | `middleware/rate_limiter.py:86-111,164-176,290-330` |
| Error body shape `{error_code, message, details, request_id}` | `errors/handlers.py:104-150`; status map `errors/codes.py:423` |
| CORS does not expose `Content-Disposition`, so a cross-origin `fetch` cannot read the filename today | `main.py:172-196` |
| Download precedent | `import_endpoints.py:301-321` |
| IFTA router gated `admin`+`dispatcher`; malformed quarter is 400 `ifta.invalid_quarter_format` | `compliance/api/ifta_endpoints.py:58-61,98-116,160-213`; `compliance/api/_authz.py` |
| IFTA report shape | `compliance/services/ifta_reporter.py:244-310` (`JurisdictionIFTAEntry`, `TruckIFTASummary`), `IncompleteDataFlag` (~`:355`), `IFTAReport` `:386` |
| HOS / DVIR indices and fields | `driver/services/driver_es_mappings.py:27,30,214,310`; `driver/services/duty_status_service.py:475-560` |
| Frontend session fetch, query string, timeout fetch, error parsing | `runsheet/src/services/api.ts:186-262`; `services/utils.ts:48,65`; `services/apiErrors.ts:105`; pattern `services/importApi.ts:21-53` |
| Frontend roles | `runsheet/src/utils/auth.ts:44` `getCurrentUserRoles`; `config/modules.ts:99` `hasAnyRole`; resolve-on-mount pattern `components/SetupHub.tsx:59-68` |
| Button with `loading` spinner and focus ring; shared toast has no live region | `runsheet/src/components/ui/Button.tsx`; `components/ui/toast/index.tsx:18-21,35-70` |

## 1. Shared helper: `Runsheet-backend/services/csv_export.py`

### Public API

```python
MAX_EXPORT_ROWS: int = 50_000
EXPORT_PAGE_SIZE: int = 200          # == HybridReadRepository clamp (_MAX_PAGE_LIMIT)

ExportType = Literal["ifta", "orders", "jobs", "reconciliation", "invoices"]

@dataclass(frozen=True)
class ExportColumn:
    header: str                                   # stable snake_case header
    value: Callable[[Mapping[str, Any]], Any]     # row -> raw value

class ExportSource(Protocol):
    async def count(self) -> int: ...
    def pages(self) -> AsyncIterator[Sequence[Mapping[str, Any]]]: ...

class KeysetSource:
    """Generic ExportSource over a fetch(after, size, with_total) callable."""
    def __init__(self, fetch: KeysetFetch, *, page_size: int = EXPORT_PAGE_SIZE,
                 count: Optional[Callable[[], Awaitable[int]]] = None): ...
        # count=None -> fetch(None, 1, True).total; invoices pass InvoiceService.count

class StaticSource:
    """ExportSource over an in-memory row list (IFTA)."""
    def __init__(self, rows: Sequence[Mapping[str, Any]]): ...

def export_guard(*allowed: str, base: Callable = get_tenant_context) -> Callable
def export_rate_key(request: Request) -> str
EXPORT_RATE_LIMIT: str            # f"{settings.export_rate_limit}/minute"
def escape_cell(value: Any) -> str
def export_filename(export_type: ExportType, tenant_id: str, now: datetime) -> str

async def stream_csv_export(
    *,
    request: Request,
    tenant: TenantContext,
    export_type: ExportType,
    columns: Sequence[ExportColumn],
    source: ExportSource,
    filters: Mapping[str, Any],
    max_rows: int = MAX_EXPORT_ROWS,
) -> StreamingResponse
```

`KeysetFetch` is `Callable[[Optional[tuple[Any, str]], int, bool], Awaitable[KeysetPage]]`:

```python
@dataclass(frozen=True)
class KeysetPage:
    rows: list[Mapping[str, Any]]          # after validation/mapping (what gets written)
    raw_count: int                         # hits/rows the store returned BEFORE any drop
    total: Optional[int]                   # exact scope total when with_total=True
    last_key: Optional[tuple[Any, str]]    # from the LAST RAW hit/row, never the last valid row
```

Every fetch adapter runs its store query in explicit keyset mode (`keyset=True`, §2) on every call, including `count()` and the first page, so the sort and `last_key` shape never depend on whether `after` is set. `KeysetSource.count()` calls `fetch(None, 1, True)` and returns `.total` (both repositories coerce `size <= 0` to a default page, `read_repositories.py:686-688`, `order_repository.py:823-824`, so size 1 is the cheapest real request). `pages()`:

```python
after = None
while True:
    page = await self._fetch(after, self._page_size, False)
    if page.rows:
        yield page.rows
    if page.raw_count < self._page_size or page.last_key is None:
        break
    after = page.last_key
```

The stop condition uses the raw store count, not the mapped rows. Orders drop documents that fail `FuelOrder(**source)` (`_safe_order_load`, `order_repository.py:184-195`, used at `:865`, `:953`) and reconciliation drops tenant-mismatched/invalid rows (`fuel_ops_endpoints.py:4060-4100`). Stopping on the post-validation count would end the export at the first page with a bad document and silently truncate it (review finding 1). Every fetch adapter therefore computes `raw_count` and `last_key` from the store result before validation.

### Cell formatting and CSV-injection escaping

`escape_cell` stringifies, then escapes:

| Raw value | Output |
|---|---|
| `None` | `""` |
| `bool` | `"true"` / `"false"` |
| `int`, `float`, `Decimal` | `str(value)` (floats via `repr`-style `str`), never escaped |
| `datetime` / `date` | `isoformat()` (UTC aware stays `+00:00`) |
| `list` / `tuple` | items formatted recursively, joined with `"; "`, then escaped as a string |
| `dict` | `json.dumps(value, sort_keys=True, default=str)`, then escaped |
| `str` (and anything else via `str()`) | escaped |

String escaping: if the string's first character is one of `=`, `+`, `-`, `@`, `\t`, `\r`, prefix `'`. Nothing else is altered; quoting of commas, quotes and newlines is left to `csv.writer` (`QUOTE_MINIMAL`, `lineterminator="\r\n"`, RFC 4180). Numbers are exempt because they are produced by our own models, cannot carry a formula, and prefixing would turn negative mileage adjustments and tax values into text in Excel (decision DD-3).

### Paging, the cap, and streaming

`stream_csv_export` runs in this order:

1. `total = await source.count()`. Exceptions here propagate as the usual error response (no body has been sent). An `AppException` keeps its status; anything else becomes 500 `INTERNAL_ERROR` through the existing handler and is logged at ERROR by the helper with `export_type`, `tenant_id`, `user_id`.
2. If `total > max_rows`: write the audit line with `outcome="rejected_too_large"` and raise
   `AppException(ErrorCode.EXPORT_TOO_LARGE, message, status_code=413, details={"row_count": total, "max_rows": max_rows})`. Body, through `errors/handlers.py:104`:
   ```json
   {"error_code": "EXPORT_TOO_LARGE",
    "message": "This export has 61,234 rows, over the 50,000-row limit. Narrow the filters and try again.",
    "details": {"row_count": 61234, "max_rows": 50000},
    "request_id": "..."}
   ```
   `EXPORT_TOO_LARGE` is added to `ErrorCode` (`errors/codes.py:16`) and mapped to 413 in `ERROR_CODE_STATUS_MAP` (`:423`).
3. Otherwise return `StreamingResponse(gen(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})`.
4. `gen()` yields `"\ufeff"` + header row as the first chunk (UTF-8, so the bytes start `EF BB BF`), then one chunk per page: each page's rows go through `columns[i].value(row)` → `escape_cell` → `csv.writer` into an `io.StringIO`, encoded UTF-8. Memory is one page (≤ 200 rows) at a time.
5. Race guard: `count` and paging are not one snapshot. If rows written would exceed `max_rows` (rows inserted after the count), the generator raises `ExportCapExceededDuringStream` before writing row `max_rows + 1`. Starlette aborts the response, so the client sees a failed transfer, never a shorter file presented as complete. Rows inserted ahead of the keyset cursor (newer `created_at` under a DESC sort) are simply not included, which is consistent "as of start" behavior.
6. `finally:` write the single audit line with the final outcome. If the client disconnects before Starlette starts iterating the body, the generator never runs and no line is written. That case is rare and accepted (DD-9).

Outcomes: `completed`, `rejected_too_large`, `aborted_cap_race`, `client_disconnected` (`asyncio.CancelledError` / `GeneratorExit`, re-raised), `failed` (any other exception mid-stream, logged at ERROR with the exception type, re-raised so the transfer aborts).

Requests rejected before the helper runs (403, 404, 422, 400, 429) are already logged by `errors/handlers.py` and the rate-limit handler; they get no export audit line (decision DD-9).

### Audit log line

```python
logger.info(
    "data_export",
    extra={"extra_data": {
        "event": "data_export",
        "export_type": export_type,
        "tenant_id": tenant.tenant_id,
        "user_id": tenant.user_id,
        "filters": _loggable_filters(filters),
        "row_count": rows_written,      # or total, for rejected_too_large
        "outcome": outcome,
        "duration_ms": elapsed_ms,
        "request_id": getattr(request.state, "request_id", "unknown"),
    }},
)
```

```python
_FREE_TEXT_FILTERS = frozenset({"q"})

def _loggable_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: ({"present": True, "length": len(str(v))} if k in _FREE_TEXT_FILTERS else v)
        for k, v in filters.items() if v is not None
    }
```

The orders `q` searches customer name, id and ship-to address (`order_endpoints.py:791-797`), so dispatchers type names, addresses and phone numbers into it. It is logged only as presence and length (review finding 6). Every other filter is an ID, enum, number or date and is logged as given. Row contents are never logged (NFR4).

`outcome="failed"` and `aborted_cap_race` log at WARNING (`failed` also logs the exception at ERROR once). The `extra_data` convention matches `middleware/rate_limiter.py:322-330`.

### Filename

`export_filename(t, tenant_id, now)` returns `f"{t}_{safe}_{now:%Y%m%d}.csv"` where `safe = re.sub(r"[^A-Za-z0-9_-]", "-", tenant_id)[:64] or "tenant"` and `now` is `datetime.now(timezone.utc)`. Because `safe` can't contain `"`, `\r`, `\n` or `;`, the header can't be split or injected.

### Rate limit, per user

- New setting `export_rate_limit: int = Field(default=5, ge=1, le=1000, description="CSV exports per minute per user")` in `config/settings.py` next to `ops_api_rate_limit` (`:643`). `EXPORT_RATE_LIMIT = f"{get_settings().export_rate_limit}/minute"`.
- `export_guard(*allowed, base=get_tenant_context)` builds a FastAPI dependency in the same shape as `roles_dependency` (`auth/router_guards.py:43-75`): it resolves `base` (so invoices can pass `require_invoicing_enabled` and keep 404-before-403), calls `require_role(tenant, *allowed)`, then stamps `request.state.export_user_id = tenant.user_id`. Empty `allowed` raises `ValueError` at import time, as `roles_dependency` does.
- `export_rate_key(request)` returns `f"export:{request.state.tenant_id}:{request.state.export_user_id}"` when both are set, else `get_client_ip(request)` (precedent `driver_rate_key`, `rate_limiter.py:86-111`).
- Each export route is decorated `@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)` and takes `request: Request`. slowapi evaluates the key inside the endpoint wrapper, after FastAPI has resolved dependencies, so the stamp is present. The export route does not also carry the list route's limit (jobs' `_scheduling_rate`, `scheduling/api/endpoints.py:40`).
- Scope: slowapi includes the endpoint in its storage key, so the limit is per user **per export route** (5 orders exports and 5 invoice exports in the same minute are both allowed). That is acceptable for v1: the goal is to stop one user hammering one expensive query.
- `EXPORT_RATE_LIMIT` is built at import time, the same pattern as `_scheduling_rate` (`scheduling/api/endpoints.py:38-39`). Tests therefore do not override it; they call the route `export_rate_limit + 1` times (§10).
- Over-limit returns the existing 429 `RATE_LIMITED` body with `Retry-After` (`rate_limiter.py:290-330`). Unauthenticated callers fail in `get_tenant_context` (401) before the limiter is reached.

### CORS

Add `"Content-Disposition"` to `expose_headers` in `main.py:184-195`, so the UI can read the server filename on a cross-origin response.

### Date range parser: `Runsheet-backend/services/date_range.py`

```python
@dataclass(frozen=True)
class DateRange:
    gte: Optional[datetime]   # inclusive lower bound, UTC
    lt: Optional[datetime]    # exclusive upper bound, UTC (date-only end -> next midnight)
    lte: Optional[datetime]   # inclusive upper bound, UTC (datetime end)

def parse_date_range(start_date: Optional[str], end_date: Optional[str]) -> DateRange
```

Rules: each value is optional; whitespace-stripped; max 40 chars; accepts `YYYY-MM-DD` or ISO-8601 datetime (trailing `Z` allowed), parsed with `date.fromisoformat` / `datetime.fromisoformat`. Naive datetimes are UTC; aware datetimes are converted with `.astimezone(timezone.utc)`. Every bound is emitted to queries as `.astimezone(timezone.utc).isoformat()`, because the document store (`document_query.py:427-450`) and the hybrid path (`read_repositories.py:690-700`) compare timestamps as text, and an un-normalized `-05:00` bound would compare wrongly. A date-only `start_date` is midnight UTC (inclusive). A date-only `end_date` becomes `lt = next day 00:00 UTC`, so "end_date=2026-10-31" includes all of the 31st whatever the stored timestamp's precision. A datetime `end_date` is an inclusive `lte`. Failure raises `AppException(ErrorCode.VALIDATION_ERROR, status_code=422, details={"field": ..., "value": ...})`, the same shape as `_list_param_error` (`fuel/api/order_endpoints.py:747-754`). `start > end` (comparing the effective bounds) raises 422 with `details={"field": "start_date", "reason": "after_end_date"}`. Both omitted returns an all-`None` range, and callers add no clause (backward compatible).

## 2. Keyset paging additions to existing read paths

All additions are keyword-only with defaults that preserve current behavior, so the list endpoints are untouched when the new arguments are absent.

**Keyset mode is explicit.** `FuelOrderRepository.search` and `JobService.list_jobs` gain three keyword-only parameters: `keyset: bool = False`, `after: Optional[tuple[Any, str]] = None`, `with_total: bool = True`. Passing `after` without `keyset=True` raises `ValueError` (programming error, not a request error), so there is no implicit mode. Every export fetch adapter passes `keyset=True` on every call: the `count()` probe, the first page (`after=None`) and every later page. The list endpoints pass none of the three and are byte-for-byte unchanged. This replaces revision 2's "keyset when `after` is given" rule, which left page 1 on the list's one-key sort (review pass 2, finding 1).

**`HybridReadRepository.search`** (`persistence/read_repositories.py:645`): add `after: Optional[tuple[str, str]] = None` and `with_total: bool = True` (the hybrid ORDER BY already ends on the pk, `:740-749`, so it needs no `keyset` flag; the order/job wrappers forward `after`/`with_total` when `keyset=True`). When `after=(v, pk)` is given, offset is forced to 0 and the WHERE gains a predicate that matches the existing ORDER BY `(f <dir>, pk ASC)` (`:740-749`):

```python
pk_col = getattr(self.model, self.pk_attr)
f = self._doc_field(sort_field)          # same expression the ORDER BY uses
v, pk = after
if sort_order == "desc":
    where.append(or_(f < v, and_(f == v, pk_col > pk)))
else:
    where.append(or_(f > v, and_(f == v, pk_col > pk)))
```

Both directions carry the tie clause, so rows sharing a `scheduled_time` (common on dispatch sheets) are not skipped. `with_total=False` skips the `count()` query. The result dict gains `"raw_count": len(rows)` and `"last_key": (doc[sort_field], doc[pk])` of the last raw row (before any caller-side validation), or `None` on an empty page. `read_hybrid_search` (`commerce_persistence_bridge.py:1222`) passes both through.

**Document-store path** (orders `order_repository.py:906-965`, jobs `job_service.py:1012-1046`). Today page 1 uses the list's one-key sort (`order_repository.py:935-941`, `job_service.py:1040`), and `PostgresDocumentStore` stamps each hit with one value per sort key (`document_store.py:702`, `sort_values_of` at `document_query.py:842-866`). A 1-value `last_key` fed back as a 2-key `search_after` is rejected by `build_search_after` (`document_query.py:869-899`). So in keyset mode the query is built the same way on every call:

```python
# FuelOrderRepository.search (id_field="order_id") / JobService.list_jobs (id_field="job_id")
if keyset:
    query["sort"] = [{sort_field: {"order": sort_order}}, {id_field: {"order": "asc"}}]
    query["from"] = 0                       # replaces the (page - 1) * size offset
    query["size"] = size
    if after is not None:
        query["search_after"] = [after[0], after[1]]
elif after is not None:
    raise ValueError("after requires keyset=True")
...
hits = resp["hits"]["hits"]                 # raw, before tenant drop / _safe_order_load
raw_count = len(hits)
last_key = tuple(hits[-1]["sort"]) if hits else None
if last_key is not None and len(last_key) != 2:
    raise RuntimeError(f"keyset sort returned {len(last_key)} value(s), expected 2")
```

`raw_count` and `last_key` are computed from the raw `resp` before `_extract_sources` and the per-row drops (`order_repository.py:152,943-958`). The `RuntimeError` is a fail-loud guard: it surfaces mid-stream as `outcome=failed` (aborted transfer, ERROR log), never as a short file. Jobs keep `track_total_hits: True` (`job_service.py:1043`), which is in `_IGNORED_BODY_KEYS` (`document_store.py:96-104`). The keyset branch adds no top-level key other than `sort`, `from`, `size` and `search_after`, all in `_HONOURED_BODY_KEYS` (`:88-90`), so `_assert_body_understood` (`:107-130`) accepts the body (review pass 2, finding 4).

With the keyset sort on every page, ORDER BY is `(sort_field, id_field, doc_id)` (`build_order_by` appends the `doc_id` tiebreak, `document_query.py:773-806`) and the `search_after` predicate is over `(sort_field, id_field)`. Pages agree with each other whether or not `doc_id == id_field`. They do match today (orders are indexed with `doc_id=model.order_id`, `order_repository.py:560-561`; jobs with `job_id`, `job_service.py:208`), so `id_field` is unique within the index and the `doc_id` tiebreak never decides anything. The export does not rely on that.

`with_total=False` has no effect on this path: `PostgresDocumentStore.search_documents` always runs its count (`document_store.py:667-672`), so a 50k export runs about 250 count queries. That is bounded and accepted; the flag exists for the hybrid path only.

Both methods return `raw_count` and `last_key` in the result dict alongside the current keys, in both modes (unused by the list endpoints). Reconciliation and invoices are not affected by this issue: the reconciliation export builds its own query with the 2-key sort from the first call (§3.4), and invoices already sort on `(created_at, invoice_id)` on every page (`invoice_service.py:1812-1815`).

Null sort keys would break a keyset (a NULL comparison drops the row), and a sort key that changes during the export moves rows across the cursor (an order updated mid-export under `updated_at desc` jumps ahead of the cursor and is never written). The export therefore pages only on fields that are always populated and immutable after creation (decision DD-4, requirements A10):

| Export | Keyset sort | Allowed user sort |
|---|---|---|
| orders | `(created_at, order_id)` | `sort` validated by the list's `_validate_list_params`; export accepts only `None`, `created_at`, `created_at:asc`, `created_at:desc`. Any other list-valid value, `updated_at` included → 422 `{"field": "sort", "reason": "not_supported_for_export"}`. Default `created_at:desc` |
| jobs | `(scheduled_time, job_id)` or `(created_at, job_id)` | `sort_by ∈ {scheduled_time, created_at}`, else 422 same shape. `sort_order` validated as the list does. Default `scheduled_time asc` (list default) |
| reconciliation | `(generated_at, reconciliation_id)` DESC | none (the list has none) |
| invoices | existing `(created_at DESC, invoice_id ASC)` | none |

`created_at` (`FuelOrder`, `fuel/order_models.py`), `scheduled_time`, `created_at` (`Job`, `scheduling/models.py:165+`), `generated_at` (`ReconciliationRecord`) and invoice `created_at` are required model fields and are written only at creation (`scheduled_time`: `job_service.py:185`, no update path). The orders UI never sends `sort` (`components/ops/OrdersPage.tsx:222-235`), and the scheduling UI sends no `sort_by` (`app/ops/scheduling/page.tsx:59-65`).

## 3. Export endpoints

Route declaration order: every `/export` route is declared before the sibling `/{id}` route in its module (orders before `order_endpoints.py:844`, invoices before `invoice_endpoints.py:266`, jobs before `scheduling/api/endpoints.py:396`), plus a test per module that `GET .../export` reaches the export handler (AC17, NFR5). Paging params (`page`, `size`, `limit`, `cursor`) are not declared on export routes; FastAPI ignores unknown query params, and so does `tenant_id` (AC21).

Every export resolves the tenant only via `export_guard(...)` → `get_tenant_context` (or `require_invoicing_enabled` → `get_tenant_context`). Every query carries the tenant: the hybrid path's typed `tenant_id` column, `inject_tenant_filter` / `term tenant_id` on the document path, plus the existing per-row `tenant_id != caller` drop that orders and reconciliation already do. The export keeps it.

Columns are whitelisted (built from explicit `ExportColumn`s), so a new model field never leaks into an export by default. PII exclusions (FR4) hold by construction. None of the lists below contains `customer_phone`, `customer_email`, a driver phone/email, a CDL/license number, or `pod_otp`.

### 3.1 IFTA quarterly: `GET /api/compliance/ifta/report/export`

- File: `compliance/api/ifta_endpoints.py`, new handler `export_ifta_report` after `get_ifta_report` (`:160`).
- Guard: the router-level `compliance_ops_dependency` (`:58-61`) still applies; the route adds `Depends(export_guard("admin", "dispatcher"))` for the user stamp. Roles: `admin`, `dispatcher`.
- Params: `quarter: str = Query(...)`. Missing gives FastAPI 422 (AC15). Malformed goes through the existing `_validate_quarter` (`:98`), giving 400 `ifta.invalid_quarter_format`, the same as the report endpoint (parity, decision DD-7).
- Source: `report = await svc.generate_quarterly_report(tenant.tenant_id, quarter)`. Errors are wrapped exactly as `get_ifta_report` does (`:181-200`: `AppException` re-raised, else 500 `ifta.report_failed`). Rows are flattened into a list, then `StaticSource(rows)`, so the count is `len(rows)`.
- Rows: one per `truck × jurisdiction` from `report.trucks[].jurisdictions[]`, then one per `report.incomplete_trucks[]` with jurisdiction columns empty.
- Columns: `quarter, truck_id, jurisdiction, total_miles, taxable_miles, tax_paid_gallons, net_taxable_gallons, tax_rate, tax_due, ifta_data_incomplete, incomplete_reason`. `ifta_data_incomplete` is `false` on summary rows and `true` on flag rows. `incomplete_reason` is `IncompleteDataFlag.reason` (AC16).

### 3.2 Orders: `GET /api/orders/export`

- File: `fuel/api/order_endpoints.py`, new handler `export_orders` placed between `list_orders` (`:781`) and `get_order` (`:844`).
- Guard: `export_guard("admin", "dispatcher")`, the same roles as `require_role(tenant, "dispatcher", "admin")` in `list_orders`.
- Params: same names, types and aliases as `list_orders`: `status` (alias), `customer_id`, `driver_id`, `call_type`, `product_code`, `start_date`, `end_date`, `intake_channel`, `q`, `sort`. Validation: the existing `_validate_list_params` (`:757`), plus the DD-4 sort restriction. Dates keep the list's semantics (raw ISO strings, `range created_at gte/lte`) so the export returns exactly what the list returns (AC14).
- Source: `KeysetSource` whose fetch calls `repo.search(tenant_id=..., <filters>, size=page_size, sort=sort, keyset=True, after=after, with_total=with_total)` (always `keyset=True`, including the first page and the `count()` probe) and maps `orders` (already `_safe_order_load`-validated `FuelOrder`s) to `model_dump(mode="json")`. `KeysetPage.raw_count` and `last_key` are the repository's new `raw_count`/`last_key` result keys (pre-validation), never `len(orders)`. The DD-4 sort check runs before the source is built: `sort not in {None, "created_at", "created_at:asc", "created_at:desc"}` → 422 `not_supported_for_export`.
- Columns: `order_id, created_at, status, customer_id, customer_name, ship_to_address, customer_tank_id, product_code, gallons_requested, fill_to_full, call_type, intake_channel, delivery_window_start, delivery_window_end, assigned_driver_id, assigned_asset_id, po_number, delivered_at, delivered_gallons, total_cents, hold_reason, refusal_reason_code, updated_at`. `delivered_at` and `delivered_gallons` come from `delivery_result.delivered_at` and `delivery_result.actual_gallons`. `special_instructions`, `recipient_name` and `intake_metadata` are excluded (free text and contact data).

### 3.3 Jobs / dispatch sheet: `GET /api/scheduling/jobs/export`

- File: `scheduling/api/endpoints.py`, new handler `export_jobs` declared right after `list_jobs` (`:248-301`), before `/jobs/active` (`:304`) and `/jobs/{job_id}` (`:396`).
- Guard: `export_guard("admin", "dispatcher")`, matching `require_role(tenant, "dispatcher", "admin")` in `list_jobs`.
- Params: `job_type`, `status`, `asset_assigned`, `origin`, `destination`, `start_date`, `end_date`, `sort_by` (default `scheduled_time`), `sort_order` (default `asc`). Validation is done by `JobService.list_jobs` itself (`job_service.py:952-975`, 400 `VALIDATION_ERROR` for bad `job_type`/`status`/`sort_order`), plus the DD-4 `sort_by` restriction (422). The list does not validate dates, and the export keeps parity (DD-7).
- Source: `KeysetSource` over `svc.list_jobs(..., size=page_size, keyset=True, after=after, with_total=...)` → `result["data"]`, with `raw_count`/`last_key` from the result (always `keyset=True`).
- Columns: `job_id, scheduled_time, status, job_type, priority, origin, destination, asset_assigned, driver_id, order_id, customer_id, estimated_arrival, started_at, completed_at, delayed, delay_duration_minutes, failure_reason, notes`. `notes` is kept as the dispatcher's own operational text and is escaped like every string.

### 3.4 Reconciliation register: `GET /api/fuel/mvp/reconciliation/export`

- File: `fuel/api/fuel_ops_endpoints.py`. Refactor the body of `list_reconciliation_records` (`:3940-4130`) into two module functions used by both handlers, with no behavior change for the list:
  - `_reconciliation_must_clauses(tenant_id, order_id, plan_id, pod_id, date_range) -> list[dict]`
  - `_validated_reconciliation_rows(hits, tenant_id) -> list[ReconciliationRecord]` (the tenant-mismatch drop, the persistence-field strip and the `ValidationError` drop at `:4060-4100`)
- New handler `export_reconciliation_records` on `mvp_router` (`:419`). There is no `/reconciliation/{id}` GET to shadow.
- Guard: `export_guard("admin", "dispatcher")`. The list has no role check (requirements A5). That is deliberately left alone here.
- Params: `order_id`, `plan_id`, `pod_id`, `min_variance_pct` (`ge=0.0`, as the list), `start_date`, `end_date` (G1 parser).
- Query: the shared must-clauses. `min_variance_pct` is pushed into the query as `bool.should` of three `range gte` clauses on `variance_load_vs_order_pct`, `variance_delivered_vs_loaded_pct`, `variance_invoiced_vs_delivered_pct` with `minimum_should_match: 1`. That is equivalent to the list's post-hoc filter because the model constrains all three to `ge=0.0` and a missing/`None` invoiced variance fails a range clause, just as the list skips `None`. Concretely, a numeric `range` bound compiles to `CAST(doc->>field AS FLOAT) >= bound` (`document_query.py:422-446`); a JSON `null` or missing field yields SQL NULL, the comparison is not true, and the row is excluded, the same as the list's `None` skip. Do not add an `exists` clause to "fix" this. It also lets `count()` be exact. The query's sort is the 2-key `[{generated_at: desc}, {reconciliation_id: asc}]` on every call, first page included, so `last_key` always has two values. Sort `[{generated_at: desc}, {reconciliation_id: asc}]`, `search_after` paging through `es.search_documents(MVP_RECONCILIATION_INDEX, ...)`. Rows that fail validation are dropped exactly as in the list, so `count` can slightly overstate rows, and the cap check errs safe. The fetch adapter sets `raw_count = len(hits)` and `last_key` from the last raw hit's `sort` values before calling `_validated_reconciliation_rows`, so a dropped row never ends paging early.
- Columns: `generated_at, reconciliation_id, order_id, plan_id, pod_id, customer_id, assigned_asset_id, assigned_driver_id, ordered_gallons, loaded_gallons, delivered_gallons, invoiced_gallons, variance_load_vs_order_pct, variance_delivered_vs_loaded_pct, variance_invoiced_vs_delivered_pct, invoice_id, canonical_invoice_id, qbo_invoice_id, alert_flags`. Customer name, site, product and BOL from the audit's wish-list are not on `ReconciliationRecord`. v1 does no cross-entity enrichment (DD-6).

### 3.5 Invoice register: `GET /api/commerce/invoices/export`

- File: `commerce/api/invoice_endpoints.py`, new handler `export_invoices` between `list_invoices` (`:170`) and `get_invoice` (`:266`).
- Guard: `export_guard("admin", base=require_invoicing_enabled)`. Order is flag 404, then the ops-role 403 inside `require_invoicing_enabled` (`:120`), then `admin` 403. A dispatcher gets 403 `INSUFFICIENT_ROLE` with `required_roles: ["admin"]` (AC24). A disabled tenant gets 404 for everyone (AC27).
- Params: `status: Optional[InvoiceStatus]` (enum, 422 as the list), `customer_id`, `account_id`, `qbo_push_state`, `start_date`, `end_date` (G2 parser).
- Source: `KeysetSource` over the existing id cursor. The fetch adapter calls `service.list(..., cursor=after[1] if after else None, limit=200)` and returns `KeysetPage(rows=items, raw_count=len(items), total=None, last_key=(None, next_cursor) if next_cursor else None)`. The invoice list does no post-fetch drop, so `raw_count=len(items)` is exact. `count()` is overridden for this source to call the new `InvoiceService.count(...)` with the same filters: the PG path goes through a new bridge function `read_invoice_count(tenant_id, **filters) -> int | _NOT_CUT_OVER` in `commerce_persistence_bridge.py`, mirroring `read_invoice_list` (`:654-660`, sentinel `_NOT_CUT_OVER` at `:586`), which calls the new `InvoiceReadRepository.count(session, tenant_id, **filters)` (`select count()` over the same `filters` list as `list`, `read_repositories.py:265-290`). When the bridge returns `_NOT_CUT_OVER`, the doc path builds its query from a new module function `_invoice_must_clauses(status, customer_id, account_id, order_id, qbo_push_state, date_range) -> list[dict]`, factored out of `InvoiceService.list` (`invoice_service.py:1799-1807`) and used by both `list` and `count` so the two cannot drift. `count` wraps those clauses exactly as `list` does (`{"bool": {"must": clauses or [match_all]}}`), applies `inject_tenant_filter(..., tenant_id)`, sets `size: 1`, no `search_after`, no cursor, then calls `search_documents(INVOICES_CURRENT_INDEX, query, size=1)` and returns `hits.total.value` (total is computed over the full scope, `document_store.py:667-672`).
- Columns: `invoice_number, invoice_id, created_at, issued_at, due_date, status, customer_id, account_id, order_id, total_gallons, subtotal_cents, tax_cents, total_cents, amount_paid_cents, remaining_cents, qbo_push_state, voided_at`. `total_gallons` is the sum of `line_items[].quantity_gallons` (`commerce/models/invoice.py:88`). Invoices carry no customer name or address, so none is added (DD-6).

## 4. G1/G2: date params on existing lists

**G1, `GET /api/fuel/mvp/reconciliation`** (`fuel_ops_endpoints.py:3940`): add `start_date: Optional[str] = Query(None)` and `end_date: Optional[str] = Query(None)`, parsed with `parse_date_range` (422 on invalid or reversed). The range applies to `generated_at` (the field the list already sorts by, and the time the record was produced at delivery) as `range generated_at {gte, lt|lte}` with `isoformat()` bounds. Omitted means no clause, which is today's behavior.

**G2, `GET /api/commerce/invoices`** (`invoice_endpoints.py:170`): add the same two params. The range applies to `created_at`, the field both paths already key on (`_keyset_page` sort column; doc sort `created_at desc`). `issued_at` is null on drafts, so filtering on it would silently drop them (DD-5). Plumbing: `InvoiceService.list(..., created_from: Optional[datetime]=None, created_before: Optional[datetime]=None, created_until: Optional[datetime]=None)` → `read_invoice_list` kwargs → `InvoiceReadRepository.list` adds `InvoiceORM.created_at >= / < / <=` (typed DateTime column), and the doc path adds a `range created_at` clause.

**`qbo_push_state` fix**: `list_invoices` accepts `qbo_push_state` but drops it (`:186-205`). This change forwards it to `InvoiceService.list` (new `qbo_push_state` kwarg → `InvoiceORM.qbo_push_state ==` / `term qbo_push_state`) so the list and the export agree. This changes list output only for callers who already pass the documented filter (DD-8).

## 5. G3: tenant-wide HOS and DVIR lists (backend only)

New module `compliance/api/driver_records_endpoints.py`, registered in `bootstrap/compliance_routers.py:43-70` alongside the other compliance routers:

```python
# compliance/api/_authz.py (addition)
COMPLIANCE_ADMIN_ROLES: tuple[str, ...] = ("admin",)
compliance_admin_dependency = roles_dependency(*COMPLIANCE_ADMIN_ROLES)

router = APIRouter(
    prefix="/api/compliance", tags=["Compliance"],
    dependencies=[Depends(compliance_ops_dependency), Depends(compliance_admin_dependency)],
)
```

Both gates are attached. `tests/unit/test_compliance_api_authz.py:130-146` globs `compliance/api/*_endpoints.py` and requires the literal `Depends(compliance_ops_dependency)` in each module; `admin` is a subset of the ops roles, so the effective rule is still admin only. Registration also updates the pins: add `"driver_records_endpoints"` to the tuple in `tests/unit/test_module_gating.py:153-162` (and rename the test from `..._all_eight_...` to `..._all_nine_...`), add the router to `_compliance_routers()` (`bootstrap/compliance_routers.py:44`), and change "eight" to "nine" in that module's docstrings (`:4`, `:11`, `:45`).

| Route | Index | Date field | Sort |
|---|---|---|---|
| `GET /api/compliance/hos-records` | `duty_status_events` (`driver_es_mappings.py:27`) | `event_timestamp` | `[{event_timestamp: desc}, {event_id: asc}]` |
| `GET /api/compliance/inspections` | `vehicle_inspections` (`:30`) | `inspection_timestamp` | `[{inspection_timestamp: desc}, {inspection_id: asc}]` |

- Params: `driver_id: Optional[str]` (max 128 chars, stripped, `term driver_id`), `start_date`, `end_date` (`parse_date_range`, 422), `page: int = Query(1, ge=1)`, `size: int = Query(50, ge=1, le=200)`. Offset paging, the same contract as the orders/jobs lists. `page * size` is capped at 10,000 (`MAX_RESULT_WINDOW`). Beyond that the endpoint returns 422 `{"field": "page", "reason": "window_exceeded"}`.
- Query: `inject_tenant_filter` with the filters above, `es.search_documents(index, query, size)`. Each `_source` is re-checked for `tenant_id == caller` (drop + WARNING, the same defense as reconciliation).
- Dates: G3 range and sort assume UTC-stamped values (the driver app sends `toISOString()`). `event_timestamp` and `inspection_timestamp` are stored as `client_timestamp.isoformat()` with the client's offset kept (`duty_status_service.py:161-176,338`; `inspection_service.py:404`), so values stored with another offset compare lexically and are approximate. Normalizing on write is backlogged.
- Response: `paginated_response_dict(items=items, total=total, page=page, page_size=size, request_id=...)` (`schemas/common.py:104`), matching `list_jobs` (`scheduling/api/endpoints.py:288-301`). Items are the stored documents minus `tenant_id`. These docs carry no phone/email/license fields (mapping `:214`, `:310`).
- Roles: router-level `admin` only, so `dispatcher` and `driver` get 403 (AC40). The guard is attached to the router so a later route inherits it (`auth/router_guards.py:1-20`).
- `hos_gate_overrides` is not included in v1 (requirements A8): it is a separate document shape with its own meaning. Backlogged.
- Availability follows the compliance master flag that gates every router in `compliance_routers.py` (DD-10).
- `GET /api/driver/hos` and `POST /api/driver/inspections` are not touched (AC44).

## 6. G4: stale checkbox

Edit `/Users/olukotunjosh/Downloads/Runsheet/.kiro/specs/frontend-feature-parity/tasks.md:142` in the main checkout (`.kiro/` is gitignored, requirements A6). Change `- [x] 10.3 Add CSV export functionality` to `- [ ] 10.3 Add CSV export functionality` with an indented note: `Removed in 76a9ef2. Replacement tracked as E6 (deferred) in .kiro/specs/data-export (Failure Analytics CSV).` Not committed.

## 7. Frontend

### `runsheet/src/services/exportApi.ts` (new)

Mirrors `services/importApi.ts:17-53`: the same `API_BASE_URL` resolution, `fetchWithSession(fetchWithTimeout, url, init, EXPORT_TIMEOUT_MS)` (`services/api.ts:243`, `services/utils.ts:65`), and `apiErrorFromResponse` (`services/apiErrors.ts:105`) for non-2xx.

```ts
export type ExportType = "ifta" | "orders" | "jobs" | "reconciliation" | "invoices";
export const EXPORT_PATHS: Record<ExportType, string> = {
  ifta: "/compliance/ifta/report/export",
  orders: "/orders/export",
  jobs: "/scheduling/jobs/export",
  reconciliation: "/fuel/mvp/reconciliation/export",
  invoices: "/commerce/invoices/export",
};
export const EXPORT_TIMEOUT_MS = 120_000;
export async function downloadCsvExport(
  type: ExportType,
  params: Record<string, string | number | boolean | undefined | null>,
): Promise<{ filename: string }>;
export function filenameFromContentDisposition(header: string | null, fallback: string): string;
```

`downloadCsvExport` builds the query with `buildQueryString` (`services/utils.ts:48`, drops empty values), sends `GET` with `Accept: text/csv`, and on `response.ok` reads `await response.blob()`. The `fetch` and the `blob()` read sit in the same `try`, whose `catch` maps any non-`ApiError` failure to `ApiError(status 0)`. `fetchWithTimeout` clears its timer once headers arrive (`services/utils.ts:65-88`), so the timeout does not bound the body read; a server-side abort mid-stream (`failed`, `aborted_cap_race`) shows up as `blob()` rejecting with a `TypeError` after `ok` was true, and lands in that catch, giving the generic message. It then saves through a temporary `<a download={filename}>` with `URL.createObjectURL`, clicks it, removes it, and calls `URL.revokeObjectURL`. `filename` comes from `Content-Disposition` (exposed by the CORS change). The fallback is `${type}_export.csv`. Non-OK responses throw the `ApiError` from `apiErrorFromResponse` (status kept). Network/timeouts become `ApiError(status 0)` / `ApiTimeoutError`, as in `importApi`. A 403 goes through `fetchWithSession`'s one refresh-and-retry (`api.ts:243-262`). A role 403 survives the retry and is surfaced.

### `runsheet/src/components/ui/ExportCsvButton.tsx` (new, exported from `components/ui/index.ts`)

```ts
interface ExportCsvButtonProps {
  type: ExportType;
  /** Current page filters, already mapped to the export route's param names. */
  params: Record<string, string | number | boolean | undefined | null>;
  /** Roles allowed to see the button (exact match via hasAnyRole). */
  allowedRoles: readonly string[];
  /** Screen-reader context appended to the visible label, e.g. "orders" -> name "Export CSV: orders". */
  subject: string;
  /** Test seam; defaults to getCurrentUserRoles(). */
  rolesOverride?: readonly string[] | null;
}
```

- Roles: resolved on mount with `getCurrentUserRoles()` (`utils/auth.ts:44`) using the cancel-on-unmount pattern in `SetupHub.tsx:59-68`. Until roles resolve (`null`), or when `hasAnyRole(roles, allowedRoles)` (`config/modules.ts:99`) is false, the component renders nothing (AC47).
- Markup (no `aria-label`, so the accessible name starts with the visible label, WCAG 2.5.3 Label in Name; Tailwind `sr-only` is already used in `components/commerce/AccountsListPage.tsx`):
  ```tsx
  <Button variant="secondary" size="sm"
          icon={<Download className="w-4 h-4" aria-hidden="true" />}
          loading={busy} aria-busy={busy} onClick={onClick}>
    Export CSV<span className="sr-only">: {subject}</span>
  </Button>
  ```
  Subjects: `"IFTA report"`, `"orders"`, `"jobs"`, `"reconciliation"`, `"invoices"`. `Button` renders a native `<button>`, disables itself while `loading`, and already has the focus ring. A `useRef` in-flight flag makes a second click a no-op even before the re-render (AC50).
- Messages: an adjacent `<p role="alert" className="text-xs text-error mt-1">` holds the current error, cleared on the next click. The shared toast has no live region (`components/ui/toast/index.tsx:18-21`), so it cannot satisfy AC55. Success shows a visually hidden `role="status"` "Export downloaded" message.

| Status | Message |
|---|---|
| 413 | "This export is over 50,000 rows. Narrow the filters and try again." |
| 429 | "Too many exports in a short time. Wait a minute and try again." |
| 403 | "You don't have permission to export this data." |
| other / network / timeout | "The export didn't complete. Try again." |

### Page integration (the `params` each page passes)

| Page | Placement | `params` | `allowedRoles` |
|---|---|---|---|
| `components/compliance/IFTAReportPage.tsx` | `PageHeader` `actions` (`:311`; `actions` prop `components/ui/PageHeader.tsx:14`) | `{ quarter }` (state `:46`) | `["admin","dispatcher"]` |
| `components/ops/OrdersPage.tsx` (rendered by `app/dashboard/orders/page.tsx`) | `PageHeader` `actions` (`:389`) | `filters` memo (`:222-235`) minus `page`/`size` | `["admin","dispatcher"]` |
| `app/ops/scheduling/page.tsx` | header button row next to "Create Job" (`:204-214`) | the same mapping as `apiFilters` (`:59-65`) | `["admin","dispatcher"]` |
| `components/ops/ReconciliationPage.tsx` | header flex row (`:1363-1373`) | `{order_id, plan_id, pod_id, min_variance_pct}` from `filters` (`:165-170`) | `["admin","dispatcher"]` |
| `components/commerce/InvoicesListPage.tsx` | `PageHeader` `actions` (`:127`) | `{ status: statusFilter, customer_id: customerFilter }` (`:38-50`) | `["admin"]` |

Reconciliation and invoices pages have no date inputs today. Adding them changes what users see, so v1 does not add them. The API date params are usable directly, and UI date inputs are backlogged (DD-11).

## 8. Error handling summary

| Operation | Failure | Recoverable? | Caller receives | Logged |
|---|---|---|---|---|
| Auth / tenant | no session | yes (sign in) | 401 (existing) | existing |
| Role guard | missing role | no | 403 `INSUFFICIENT_ROLE`, `details.required_roles` | existing handler |
| Invoice flag | module off | no | 404 `COMMERCE_DISABLED` / `INVOICING_DISABLED` | DEBUG (existing) |
| Param validation | bad enum / sort / date / reversed range | yes | 422 `VALIDATION_ERROR` (`{field,value|reason}`); IFTA malformed quarter 400; jobs bad enum 400 (parity) | existing handler |
| Rate limit | over `export_rate_limit`/min per user | yes (wait) | 429 `RATE_LIMITED` + `Retry-After` | WARNING (existing) |
| Count query | store error | yes (retry) | 500 `INTERNAL_ERROR` (IFTA: 500 `ifta.report_failed`) | ERROR, helper |
| Cap | `total > 50,000` | yes (narrow) | 413 `EXPORT_TOO_LARGE` `{row_count,max_rows}` | INFO audit `rejected_too_large` |
| Mid-stream store error | page fetch raises | yes (retry) | aborted transfer (no complete file) | ERROR + WARNING audit `failed` |
| Mid-stream cap race | rows > cap after count | yes (narrow/retry) | aborted transfer | WARNING audit `aborted_cap_race` |
| Client disconnect | cancelled | n/a | n/a | INFO audit `client_disconnected` |
| G3 window | `page*size > 10,000` | yes | 422 `{field:"page",reason:"window_exceeded"}` | none |
| Frontend | any non-2xx / network | per row above | inline `role="alert"` message | none (browser) |

## 9. Invariants and their owners

- **Tenant scope.** Owned by the data layer call each source makes (typed `tenant_id` column / `inject_tenant_filter`), with the per-row tenant re-check in orders and reconciliation as defense in depth. The export never builds a tenant-free query. Why: the list endpoints already enforce it there, and exports reuse those calls.
- **Role.** Owned by `export_guard` at the route (dependency), because the same rule must hold before any query runs and must be visible in the route signature. The UI's role gate is presentational only.
- **No silent truncation.** Owned by `stream_csv_export`. The pre-stream count gives 413, and the in-stream counter aborts the transfer.
- **Injection-safe cells and PII whitelist.** Owned by `escape_cell` and each export's explicit column list. No code path serializes a whole document.

## 10. Test plan

Backend tests run with `pytest` (`Runsheet-backend/pytest.ini`). Endpoint tests follow `tests/unit/test_asset_compliance_endpoints.py` (FastAPI `TestClient`, `app.dependency_overrides[get_tenant_context]` returning a `TenantContext(tenant_id=..., user_id=..., roles=[...])`). Keyset tests follow `tests/persistence/test_hybrid_read_cutover.py` (SQLite-backed relational and document stores).

**`tests/unit/test_csv_export_helper.py`** (pure unit)
- `escape_cell`: each of `= + - @ \t \r` gets the `'` prefix; `"a=b"`, `""`, `None` unchanged; `-5` (int) and `-1.5` (float) not prefixed; list/dict formatting; a comma/quote/newline value round-trips through `csv.reader` (AC4, AC5).
- BOM is the first 3 bytes, the header row is second, `\r\n` terminators (AC1, AC59).
- `export_filename`: format, UTC date, sanitization of `acme/"x"\r\n` (AC11, AC59).
- Cap: `max_rows=3` with a 4-row source gives 413 with `details`, generator never started, and the audit line has `rejected_too_large` (AC7). With a source whose `count()` says 3 but pages yield 4, the stream aborts and the audit says `aborted_cap_race` (AC8).
- Paging: a `KeysetSource` over a fake fetch with `page_size=2` and 5 rows yields all 5, in order, once each (AC6). A fake page with `raw_count=2` but one mapped row does not stop the loop. `count()` calls fetch with size 1.
- Audit line: fields `tenant_id, user_id, export_type, filters, row_count, outcome` present, no row values (caplog) (AC10). With `q="Jane Doe 555-0100"`, the captured record contains neither `Jane` nor `555-0100`, and does contain `"length": 17`.
- `export_rate_key`: stamped gives `export:t:u`; unstamped falls back to IP.

**`tests/persistence/test_export_keyset_paging.py`**
- `HybridReadRepository.search(after=...)`: 7 orders for tenant A with three sharing one `created_at` plus 3 for tenant B. Paging with size 2 returns all 7 A rows exactly once, none from B, for DESC and ASC. `with_total=False` skips the count. No `after` returns output identical to today's.
- Document-store path, through the real export path (review pass 2, finding 1): with `COMMERCE_READ_FROM_POSTGRES` off and a `PostgresDocumentStore` behind the facade, build the orders and jobs export `KeysetSource`s exactly as the handlers do (`keyset=True` adapters over `FuelOrderRepository.search` / `JobService.list_jobs`), `page_size=2`, starting from `after=None`. Fixture: 5 orders and 5 jobs for tenant A, 3 of each tying on the sort key, plus tenant B rows. Assert each export yields all 5 A rows, once each, in `(sort_field, id)` order, no B rows, for DESC and ASC. The test must not hand-build `search_after`. Also assert `count()` returns 5 and that every page's `last_key` has length 2.
- `after` without `keyset=True` raises `ValueError`; `keyset=False` with no `after` returns output identical to today's for both methods (list parity).
- Reconciliation `min_variance_pct` push-down returns the same set as the list's post-hoc filter on a mixed fixture (including `variance_invoiced_vs_delivered_pct=None`).
- Validation drop (review finding 1): with `page_size=2` and 5 orders where the 2nd fails `FuelOrder` validation, the export writes the other 4; same for 5 reconciliation records with the 2nd invalid.
- Jobs ASC with 5 jobs sharing one `scheduled_time`, `page_size=2`: all 5 written once (tie clause).

**`tests/unit/test_data_export_endpoints.py`** (parametrized over the five exports, fakes for repo/service/reporter)
- Tenant isolation: seeded A and B rows matching the filters; the A caller's CSV contains only A ids. `?tenant_id=B` is ignored (AC20, AC21).
- Roles: `admin` 200 and `dispatcher` 200 (invoices: `dispatcher` 403 `INSUFFICIENT_ROLE`, `required_roles == ["admin"]`); `driver` 403; `[]` (customer-like) 403; `platform_admin` alone 403 (AC22-26).
- Invoice flag off returns 404 for `admin` and `dispatcher` (AC27). Reconciliation `driver` gets 403 although the list lets it through (AC28).
- CSV injection: a row with `customer_name="=HYPERLINK(...)"` (orders), `notes="+cmd"` (jobs), `alert_flags=["@x"]` (reconciliation), `customer_id="-1+1"` (invoices), and `truck_id="=1"` (IFTA) comes out prefixed (AC4).
- Row cap: `MAX_EXPORT_ROWS` monkeypatched to 2 with 3 matching rows gives 413 per export (AC7).
- Date filters: orders/jobs pass through to the repo/service (parity). Reconciliation/invoices: in-range rows only, date-only `end_date` inclusive of that day, invalid 422, reversed 422 (AC32-36).
- IFTA: missing quarter 422, malformed 400, incomplete trucks present with `ifta_data_incomplete=true` (AC15, AC16).
- PII: the header row of each export contains none of `customer_phone, customer_email, phone, email, license, cdl` (case-insensitive substring), and seeded phone/email values appear nowhere in the body (AC29-31).
- Route shadowing: `/api/orders/export`, `/api/commerce/invoices/export`, `/api/scheduling/jobs/export` hit the export handler, not the `{id}` handler (AC17).
- Rate-limit harness: an autouse fixture `from middleware.rate_limiter import limiter; limiter.reset(); yield; limiter.reset()`, because under `ENVIRONMENT=test` the limiter is a process-wide `memory://` store (`rate_limiter.py:117-140`) and the suite calls each route more than 5 times as one user. The test app registers `app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)` (`rate_limiter.py:284,292`); without it slowapi's exception surfaces as 500.
- Rate limit: `get_settings().export_rate_limit + 1` calls (6 by default) as user `u1`; the last is 429 `RATE_LIMITED` with `Retry-After`. A following call as `u2` returns 200 (AC9).
- Unsupported sort gives 422 `not_supported_for_export`, including orders `sort=updated_at:desc` and jobs `sort_by=priority`. Paging params are not in the OpenAPI schema for export routes.
- Dependency guard: `requirements*.txt` unchanged is covered by review, not a test (AC2).

**`tests/unit/test_list_date_filters.py`** (G1/G2, AC32-35, AC58): valid range filters; invalid 422; reversed 422; omitted returns a body byte-identical to a pre-change snapshot of the same fixture; `qbo_push_state` now filters.

**`tests/unit/test_driver_records_endpoints.py`** (G3, AC37-42, AC44): `admin` 200; `dispatcher` and `driver` 403; `driver_id`/date filters; paging metadata and `size` max 200 (`201` gives 422); window 422; tenant isolation; the existing `GET /api/driver/hos` test suite still passes unchanged; `test_compliance_api_authz.py` and `test_module_gating.py` pass with the new module (drift guards).

**Frontend (Jest + Testing Library, `runsheet/jest.config.js`, jsdom)**
- `src/services/exportApi.test.ts`: URL and query per type (empty params dropped); `credentials: "include"` via a mocked `fetch`; Blob saved via stubbed `URL.createObjectURL` / anchor click with the header filename; fallback filename; non-OK throws `ApiError` with status; `ok` response whose `blob()` rejects throws `ApiError` status 0 (generic message path).
- `src/components/ui/ExportCsvButton.test.tsx`: hidden while roles unresolved / not allowed, shown for allowed; `getByRole("button", { name: "Export CSV: orders" })`, no `aria-label` attribute, native button, keyboard activation (Enter/Space); `aria-busy` and disabled during the request, double click sends one request; 413/429/403/500 messages in `role="alert"`.
- Per page (extend the existing tests where present, e.g. `components/ops/OrdersPage.test.tsx`; add `*.export.test.tsx` beside the others): the button appears for the right roles, and clicking it calls `downloadCsvExport` with that page's current filters after changing one filter (AC46-48, AC60).

Verification before PR: `pytest` (full backend suite), `npm test` and `npm run lint` / type-check in `runsheet/`.

## 11. Files touched

Backend, new: `services/csv_export.py`, `services/date_range.py`, `compliance/api/driver_records_endpoints.py`, and the five test files above.
Backend, modified (`fuel/order_repository.py` and `scheduling/services/job_service.py` gain `keyset`/`after`/`with_total`; `commerce/services/invoice_service.py` gains `_invoice_must_clauses` and `count`): `errors/codes.py` (code + 413), `config/settings.py` (`export_rate_limit`), `main.py` (CORS expose), `persistence/read_repositories.py` (`HybridReadRepository.search` keyset; `InvoiceReadRepository.list` dates/qbo, `count`), `commerce/services/commerce_persistence_bridge.py` (pass-through), `fuel/order_repository.py`, `fuel/api/order_endpoints.py`, `scheduling/services/job_service.py`, `scheduling/api/endpoints.py`, `fuel/api/fuel_ops_endpoints.py`, `commerce/services/invoice_service.py`, `commerce/api/invoice_endpoints.py`, `compliance/api/ifta_endpoints.py`, `compliance/api/_authz.py`, `bootstrap/compliance_routers.py`, `tests/unit/test_module_gating.py` (pin list). `commerce_persistence_bridge.py` also gains `read_invoice_count`. Spec: `requirements.md` gains assumptions A10 and A11.
Frontend, new: `services/exportApi.ts`, `components/ui/ExportCsvButton.tsx`, and tests. Modified: `components/ui/index.ts` and the five pages.
Not touched (owned elsewhere): `services/import_service.py`, `import_endpoints.py`, `runsheet/src/services/importApi.ts`, `inventory/`, `notifications/`, `integrations/` endpoints, `data_endpoints.py`, `.worktrees/codebuild`. Branch `production-readiness/fleet-fuel-fixes` may also touch `fuel/api/fuel_ops_endpoints.py`. Rebase onto the current `go-live-blockers` before opening the PR and resolve conflicts there.

## Decisions

### Owner decisions (locked, from requirements)

D1 CSV only, UTF-8 + BOM, stdlib `csv` + `StreamingResponse`, no new deps. D2 Scope and build order: IFTA, orders, jobs, reconciliation, invoices. D3 Deferred: T2, E6, AR/payments/price books, rack prices, notifications, fleet. D4 `GET <list>/export` reusing filters, tenant context and guard, with one shared helper (escaping, keyset paging, 50k cap, per-user limit, audit line, `<type>_<tenant>_<YYYYMMDD>.csv`). D5 Invoices `admin` only; others `admin` + `dispatcher`; never `driver`/customers. D6 No driver phone/email/license, no customer contact email/phone; customer name and delivery address included. D7 G1/G2 typed date params, 422, additive. D8 G3 admin-only HOS/DVIR lists, backend only. D9 G4 checkbox annotated to `76a9ef2`. D10 Accessible role-gated button, current filters, authenticated Blob download, 413/429/403 messages. D11 Per-export tests listed in §10.

### Design decisions (made here)

- DD-1 Row cap status 413 `EXPORT_TOO_LARGE` (requirements A1), enforced by an exact count before streaming. 422 stays reserved for bad filters.
- DD-2 Per-user key `export:{tenant}:{user}` from a stamp set by `export_guard`, rather than editing `tenant_guard.py`, which is security-core code. Default 5 exports/minute/user, configurable via `EXPORT_RATE_LIMIT`.
- DD-3 Escaping applies to string-typed values only. Numbers are never prefixed, so negative numerics stay numeric in Excel.
- DD-4 Freeze decision: keyset fields must be always populated and immutable after creation (table in §2; requirements A10). This removes the NULL-keyset and moving-key classes of bugs instead of handling them per backend; orders therefore export only by `created_at`. Paging stops on the raw store count, never the validated row count. Keyset mode is an explicit `keyset=True` flag passed on every export call, never inferred from `after`, so every page (first included) uses the same 2-key sort. Required tests: the tie-heavy ASC/DESC keyset tests and validation-drop tests in `test_export_keyset_paging.py`, and the 422 `not_supported_for_export` tests.
- DD-5 Date-range fields: reconciliation `generated_at`; invoices `created_at` (drafts have no `issued_at`); G3 `event_timestamp` / `inspection_timestamp`. Date-only `end_date` is inclusive of the whole day (exclusive next-midnight bound).
- DD-6 No cross-entity enrichment in v1. Columns are fields already on the row (IDs, plus `customer_name`/`ship_to_address` on orders).
- DD-7 Parity over strictness: export validation equals the list's (IFTA malformed quarter 400; jobs enum 400, dates unvalidated). Only G1/G2/G3 date params get the new 422 parser. Consequence, kept on purpose: orders and jobs compare a date-only `end_date=2026-10-31` lexically against full timestamps (`lte`), which excludes most of the 31st. That is existing list behavior; the export matches it and the parity tests encode it. It is not an export bug.
- DD-8 Forward the ignored `qbo_push_state` filter on `list_invoices`, so the list and the export agree.
- DD-9 Audit line only for requests that reach the helper. Auth/validation/rate-limit rejections are logged by existing handlers. A client that disconnects before the body is first iterated gets no audit line (the generator never starts); accepted as rare rather than adding a `BackgroundTask` fallback.
- DD-13 Free-text filters (`q`) are logged as `{present, length}` only, so audit lines carry no customer PII (NFR4).
- DD-14 Rate limit is per user per export route (slowapi keys by endpoint). Five different exports in a minute are allowed.
- DD-15 The export button's accessible name is "Export CSV: <subject>" via `sr-only` text, not `aria-label`, so it contains the visible label (WCAG 2.5.3).
- DD-10 G3 lives under `/api/compliance/*` (DOT records, next to IFTA) with a new `compliance_admin_dependency`, and inherits the compliance master flag.
- DD-11 No new date inputs on the reconciliation/invoice pages in v1 (a visible product change with no spec default). Backlog item.
- DD-12 Inline `role="alert"` error text instead of the shared toast, because the toast has no live region.

### Backlog

`hos_gate_overrides` list (A8); reconciliation list role check (A5); UI date inputs for reconciliation/invoices (DD-11); export sorting on nullable or mutable fields (`updated_at`, `delivery_window_start`); driver/customer name enrichment; job list date validation; normalizing HOS/DVIR timestamps to UTC on write (§5).

## Review responses

Revision 2, answering `.agents/tasks/spreadsheet-export-audit-2026-10-04/design-review.md` (pass 1: 1 HIGH, 7 MEDIUM, 9 NIT). All 17 findings are addressed; none backlogged or ignored.

| # | Sev | Response |
|---|---|---|
| 1 | HIGH | `KeysetPage` gains `raw_count`; `last_key` comes from the last raw hit; the loop stops on `raw_count < page_size` (§1). Orders, reconciliation and hybrid adapters compute both before validation (§2, §3.2, §3.4). Validation-drop tests added (§10). |
| 2 | MED | Orders export sort limited to `created_at[:asc|desc]`; `updated_at` → 422. DD-4 now requires immutability (§2, DD-4). Test added. |
| 3 | MED | Both ASC and DESC predicates spelled out with the `pk` tie clause (§2). Tie-heavy ASC jobs test added. |
| 4 | MED | Requirements A10 added; referenced from §2 and DD-4. |
| 5 | MED | G3 router carries `compliance_ops_dependency` and `compliance_admin_dependency`; module added to the `test_module_gating.py` pin list, `_compliance_routers()`, and the "eight"→"nine" docstrings (§5). |
| 6 | MED | `_loggable_filters` logs `q` as `{present, length}` (§1, DD-13). caplog test added. |
| 7 | MED | Autouse `limiter.reset()` fixture, `RateLimitExceeded` handler registered in the test app, `export_rate_limit + 1` calls; scope documented as per user per route (§1, §10, DD-14). |
| 8 | MED | `aria-label` dropped; `subject` prop with `sr-only` suffix gives "Export CSV: orders" (§7, DD-15). Test uses that name. |
| 9 | NIT | `count()` uses `fetch(None, 1, True).total` (§1). |
| 10 | NIT | Bounds normalized with `.astimezone(timezone.utc).isoformat()` (§1); G3 UTC assumption documented and normalization backlogged (§5). |
| 11 | NIT | `read_invoice_count` bridge named; id-cursor → `KeysetPage` adaptation spelled out; `KeysetSource` takes an optional `count` (§1, §3.5). |
| 12 | NIT | `blob()` read inside the mapping `try`; test for a rejected `blob()` (§7, §10). |
| 13 | NIT | Accepted and noted under DD-9. |
| 14 | NIT | Requirements A11 added. |
| 15 | NIT | `paginated_response_dict(..., page_size=size, ...)` (§5). |
| 16 | NIT | Stated in §2 that `with_total=False` is a no-op on the document-store path. |
| 17 | NIT | Stated in DD-7. |

Revision 3, answering pass 2 of the same review (1 HIGH, 0 MEDIUM, 3 NIT). All 4 findings are addressed; none backlogged or ignored.

| # | Sev | Response |
|---|---|---|
| 1 | HIGH | Keyset mode is now an explicit keyword-only `keyset=True` on `FuelOrderRepository.search` and `JobService.list_jobs`, passed by every export call (count probe, first page, later pages). In keyset mode the doc-store sort is always `[(sort_field, dir), (order_id|job_id, asc)]` with `from=0`; `search_after` only when `after` is set; `last_key = tuple(hits[-1]["sort"])` with a length-2 guard. `after` without `keyset` raises `ValueError`. The `doc_id == id_field` question no longer matters (stated, with the write sites that make it true today). Required doc-store test through the real export path from `after=None`, with ties, added (§2, §3.2, §3.3, §10, DD-4). |
| 2 | NIT | `CAST(... AS FLOAT)` NULL-exclusion equivalence stated in §3.4. |
| 3 | NIT | `_invoice_must_clauses(...)` factored and shared by `list` and `count`; the count query is spelled out (§3.5). |
| 4 | NIT | §2 states the keyset branch adds only `sort`/`from`/`size`/`search_after` and keeps `track_total_hits` (ignored-safe). |
