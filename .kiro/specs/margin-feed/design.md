# Design: Cost / margin (COGS) feed for RevenueGuard

Status: revision 4. Addresses design review pass 3 (`.agents/tasks/margin-feed/design-review.md`: 0 HIGH, 3 MEDIUM, 8 NIT) on top of revisions 2 and 3. Responses to all three passes are at the end. Simplifications 8, 11, 12 and 13 are closed freezes (see "Simplifications"). Base for implementation: `origin/production-readiness/go-live-blockers` at `a99bfd9` (`production-readiness/margin-feed` fast-forwarded to it 2026-10-07), which carries OI-14 as `b6428e2`. Implements `.kiro/specs/margin-feed/requirements.md` (D1–D20, FR1–FR8, AC-1–AC-46).

## Overview

The feed adds a margin subsystem with its own relational tables, inside the commerce backend. Admins enter cost data: purchase lots, overrides and freight/fee adders. They can type it in or import a CSV. At resolution time a pure resolver combines those entries with data the system already holds: terminal BOLs, supplier contracts, loading plans and `rack_prices` rows. The output is a landed cost per gallon, with an explicit `none` state when no cost can be found.

A margin service then turns each order estimate, delivery and final invoice line into a versioned margin record. Every record carries a full cost snapshot and flags that the service computes once. For invoices it reads the persisted line values after all pricing, including the OI-14 contract split. A flagged live record is written with `alert_state='pending'` in the same row. RevenueGuard, which only runs on the sweep leader, drains that database queue and writes admin-only alerts and leakage proposals. The service also publishes an ids-only `RiskSignal` per flagged record (FR5.2), but only as a hint. No alert depends on the in-process bus. RevenueGuard no longer computes anything from `jobs_current`.

Everything is served under `/api/commerce/margin/*`, behind a feature flag and then an `admin` role check. The admin UI is a new "Margin" tab in CommerceHub. Nothing is written onto order, invoice, BOL or job documents. Margin data lives only in its own Postgres tables, outside the `es_documents` store. That keeps it off every existing read surface, including the agent search tools.

### Technology stack (locked)

| Layer | Choice | Notes |
|---|---|---|
| Storage | PostgreSQL via the existing SQLAlchemy 2.x async ORM (`persistence/models.py`, `persistence/database.session_scope`) | New typed tables, not `es_documents`. SQLite in-memory for unit/persistence tests (`tests/persistence/conftest.py`). Real Postgres for lock/partial-index tests (`tests/postgres/`). |
| Migrations | Alembic, one revision `0011_margin_feed` | `alembic/versions/20261008_0001_margin_feed.py`, `down_revision = "0010_acct_override_audit"`. |
| API | FastAPI routers, Pydantic v2 models (`extra="forbid"`) | Same error envelope (`AppException`, `handle_request_validation_error` → 422). |
| Money | `int` cents, `int` micros per gallon, `int` micro-gallons, `decimal.Decimal` for intermediate maths | `services/money.py` helpers. No `float` arithmetic on money. |
| Background work | `asyncio.create_task` with a tracked task set (same pattern as `finalize_draft`'s external sync), periodic jobs through `persistence.leader_election.run_periodic` | No new queue. |
| Agent | Existing `OverlayAgentBase` (leader-only `monitor_cycle`, `_get_mode`, `_route_proposal`, `_log_shadow_proposal`) | RevenueGuard rewritten in place and owns its own `monitor_cycle` (Simplification 11). Work comes from a DB queue (`margin_records.alert_state`), not the in-process `SignalBus`. |
| CSV | `services/csv_export.py` (export), stdlib `csv` (import) | |
| UI | Next.js / React / TypeScript in `runsheet/`, Jest + Testing Library | Existing `ui/` components, `ExportCsvButton`, `modules.ts` gating. |
| Dependencies | None added | D20. |

### Branch and dependency plan (OI-14)

OI-14 has landed on `origin/production-readiness/go-live-blockers` as `b6428e2` (parent `3bb3399`; it is the rebased form of the original `9d94f7d`). No later commit up to `a99bfd9` touches `invoice_service.py`, `sales_pricing_engine.py` or `bootstrap/compliance.py`, so `b6428e2` is the invoice pricing code as merged. The earlier SHAs `9d94f7d`, `8b47be4`, `dd2c44c` and `f569d13` are on no branch and must not be used. OI-14 adds `build_sales_pricing_engine(es_service, tenant_id)` in `commerce/services/sales_pricing_engine.py`, which wires `PriceProtectionService` into `SalesPricingEngine`. The invoice factory (`bootstrap/compliance.py` `_sales_pricing_engine_factory`, ≈L265) and `POST /pricing/resolve` both use it.

OI-14 also adds `_contract_split_lines(item, resolution, contract_price_micros)` in `commerce/services/invoice_service.py`. When the resolution carries `split_gallons_at_contract_price` and a positive `split_gallons_at_market_price`, one line becomes up to two lines, each a copy of the original (`dict(item)`, so `product_code`, `market_price_cents` and other keys carry over):

| Key | Contract-priced line (first) | Market-priced line |
|---|---|---|
| `line_id` | kept from the original line | new `line_{uuid4()}` (kept from the original if the contract portion is 0 and this is the only line) |
| `quantity_gallons` (and `quantity`, only if the original had it) | `min(contract_gallons, total)` | `round(total - contract_qty, 6)` |
| `unit_price_micros` | the resolved contract price | `resolution.market_price_cents * MICROS_PER_CENT` |
| `unit_price_cents` | `legacy_unit_price_cents(micros)` | same |
| `subtotal_cents` | `line_subtotal_cents(qty, micros)` | same |

A line with zero gallons on either side is omitted. Tax is computed on the pre-split lines (`tax_basis_items`), so the split does not change `tax_cents`. The persisted `doc["line_items"]` is the split list, and its order is the enumerate order the `line_index` key uses.

`production-readiness/margin-feed` has no commits of its own apart from the spec commit. It was fast-forwarded from `0f82def` to `origin/production-readiness/go-live-blockers` at `a99bfd9` on 2026-10-07, with no history rewrite. That brings in OI-14 (`b6428e2`) together with OI-16 (`d3aea7e`), OI-19 (`91bb0cb`), OI-20 (`45a4b3f`), OI-57 (`e3454e0`), the owner-decision commits up to `1ce977b`, and `a99bfd9`. All of them are already reviewed and landed. `a99bfd9` makes `run_periodic` followers start a cycle as soon as they win the election. It also renames the `AutonomousAgentBase._group_by_tenant` helper to `_cycle_counts_by_tenant`, so `OverlayAgentBase._log_cycle` works again. The design does not depend on either name. AC-13 and AC-14 run directly against `b6428e2`'s `generate_from_order` (≈L675–1145), where `doc["line_items"]` is the final, possibly split, line list.

Margin edits are made on top of the landed versions of these files, which upstream already changed since `0f82def` (no reconcile step, just edit the current text):

- `runsheet/src/config/modules.ts` (OI-19 role gates kept; add the `margin` entry, admin only) and `components/CommerceHub.tsx` (add the tab).
- `services/csv_export.py`: `ExportType` now includes `"driver_hours"` and `"driver_qualifications"`; add `"margin"`.
- `runsheet/src/services/exportApi.ts`: add `margin` to `ExportType` and `EXPORT_PATHS`.
- `errors/codes.py` (OI-02, OI-16 and OI-41 codes present): add the two margin codes and their status-map entries.

If `go-live-blockers` moves again before this branch merges, merge it in (never rebase) and re-run AC-13/AC-14, the shared `csv_export` contract tests and the `ExportCsvButton` Jest tests.

## Module layout

New files (backend, `Runsheet-backend/`):

| File | Contents |
|---|---|
| `commerce/models/margin.py` | Pydantic request/response models and enums: `CostEntryKind`, `AdderType`, `MarginStage`, `CostMethod`, `NoCostReason`, `MarginFlag`, `RecordStatus`, `RecordOrigin`, `AlertType`, `AlertStatus`, and the decimal-string parsers (`parse_usd_micros`, `parse_gallons_milli`). |
| `commerce/services/margin_repository.py` | The only module that touches the margin tables. Every public method takes `tenant_id` as its first argument and adds `WHERE tenant_id = :tenant_id`. |
| `commerce/services/margin_cost_entry_service.py` | Validation, create, supersede, void, list, and CSV import of cost entries. Emits audit events. |
| `commerce/services/margin_cost_basis.py` | `CostBasisResolver.resolve(...)`: pure resolution logic over injected readers (BOLs, rack, contracts, plans, entries). |
| `commerce/services/margin_attribution.py` | Terminal attribution for an order (D11). |
| `commerce/services/margin_service.py` | `MarginService`: record computation, flags, the versioned write protocol, the hook object (`MarginHook`), signal publishing, recompute runs, preview and summary. |
| `commerce/services/margin_jobs.py` | `run_margin_gap_sweep_cycle`, `run_margin_weekly_report_cycle`. |
| `commerce/hooks/margin_order_subscriber.py` | `MarginOrderSubscriber`, registered on `order.dispatched`, `order.delivered`, `order.cancelled` and `order.failed`. |
| `commerce/api/margin_endpoints.py` | Router `/api/commerce/margin`, the guard dependencies and the export. |

Modified files:

| File | Change |
|---|---|
| `persistence/models.py` | Seven ORM classes in a new "Margin feed" section. |
| `alembic/versions/20261008_0001_margin_feed.py` | New migration. |
| `config/settings.py` | `commerce_margin_feed_enabled: bool = False`. |
| `.env.example` | `COMMERCE_MARGIN_FEED_ENABLED=false`. |
| `commerce/services/invoice_service.py` | `set_margin_hook()`, plus three hook calls (generate, finalize, void). An optional `updated_from` filter on `list`, `count` and `_invoice_must_clauses` (ES path), forwarded to `read_invoice_list` / `read_invoice_count` (Postgres path), used by the gap sweep. |
| `commerce/services/commerce_persistence_bridge.py` | None. `read_invoice_list` / `read_invoice_count` already forward `**kwargs`; the new kwarg passes through. |
| `persistence/read_repositories.py` | `InvoiceReadRepository._list_filters` and `list` gain keyword-only `updated_from=None` → `InvoiceORM.updated_at >= updated_from`. `count` inherits it through `**filters`. |
| `fuel/services/fuel_product_catalog.py` | Public `aliases_for(canonical) -> frozenset[str]`, built once from `_ALIAS_INDEX`. |
| `services/csv_export.py` | `ExportType` adds `"margin"`. |
| `errors/codes.py` | `MARGIN_IMPORT_TOO_LARGE` (413) and `MARGIN_RECOMPUTE_RUNNING` (409), each added to the status map. |
| `Agents/overlay/revenue_guard.py` | Rewritten (see RevenueGuard). |
| `bootstrap/core.py` | Construct `MarginService`, inject the hook, subscribe to order events, configure the API, start both jobs. |
| `bootstrap/agents.py` | `margin_service.set_signal_bus(signal_bus)` and `revenue_guard.set_margin_repository(repo)`. |
| `main.py` | `app.include_router(commerce_margin_router)` in the commerce block. |

Frontend (`runsheet/src/`): `services/marginApi.ts`; `components/commerce/margin/` (`MarginHub.tsx`, `MarginRecordsPage.tsx`, `MarginSummaryPanel.tsx`, `MarginAlertsPanel.tsx`, `CostBasisViewer.tsx`, `CostEntriesPage.tsx`, `CostEntryForm.tsx`, `CostImportDialog.tsx`, `MarginSettingsForm.tsx`, `MarginRecomputePanel.tsx`, `marginFormat.ts`). Also modified: `components/CommerceHub.tsx`, `config/modules.ts` and `services/exportApi.ts`.

## Data model

All amounts are integers. The column suffix states the unit:

- `_cents`: 1/100 USD.
- `_micros`: 1/1,000,000 USD per gallon (`MICROS_PER_CENT = 10_000`).
- `_milli`: 1/1,000 gallon, for cost-entry and lot gallons (FR1 allows 3 dp).
- `_ugal`: 1/1,000,000 gallon, for margin-record gallons.

Margin records use micro-gallons because OI-14 split lines carry up to 6 dp (`round(total - contract_qty, 6)`). Storing them at 3 dp would make `cost_cents` disagree with the line's own `subtotal_cents` basis. Storage, the API and the CSV export all carry record gallons at 6 dp. Only the admin UI rounds them to FR3.1's 3 dp for display. Cost-entry gallons stay 3 dp everywhere. Integer storage behaves the same on SQLite and Postgres, where `Numeric` on SQLite does not. Every timestamp is `DateTime(timezone=True)` in UTC.

### `margin_cost_entries` (ORM `MarginCostEntryORM`)

| Column | Type | Rule |
|---|---|---|
| `entry_id` | `String(64)` PK | `mce_<uuid4hex>` |
| `tenant_id` | `String(128)` not null | |
| `kind` | `String(16)` not null | `purchase` \| `override` \| `adder` |
| `product_code` | `String(64)` not null | Canonical. |
| `terminal_id` | `String(128)` null | Null means tenant-wide (not allowed for `purchase`). |
| `supplier_name` | `String(128)` null | |
| `effective_at` | `DateTime(tz)` not null | |
| `effective_to` | `DateTime(tz)` null | `override`/`adder` only. |
| `unit_cost_micros` | `BigInteger` not null | `0 ≤ v ≤ 100_000_000` |
| `gallons_milli` | `BigInteger` null | Required for `purchase`, `1 ≤ v ≤ 1_000_000_000`. |
| `adder_type` | `String(16)` null | Required for `adder`: `freight` \| `fee` \| `other`. |
| `bol_id` | `String(128)` null | `purchase` only. |
| `reference` | `String(128)` null | |
| `notes` | `String(500)` null | |
| `natural_key` | `String(64)` not null | sha256 hex of `kind\|product\|terminal or ''\|effective_at\|bol_id or reference or ''\|adder_type or ''`. `effective_at` is first converted to UTC and formatted `YYYY-MM-DDTHH:MM:SS.ffffffZ`, so one instant sent with different offsets gives the same key. |
| `status` | `String(16)` not null | `active` \| `superseded` \| `voided` |
| `supersedes_id` / `superseded_by_id` | `String(64)` null | Links between versions of an edited entry. |
| `status_reason` | `String(500)` null | |
| `status_changed_by` / `status_changed_at` | `String(255)` / `DateTime(tz)` null | |
| `source` | `String(16)` not null | `manual` \| `csv_import` |
| `import_batch_id` | `String(64)` null | |
| `created_by` / `created_at` | `String(255)` / `DateTime(tz)` not null | |

Constraints and indexes:

- `ck_mce_kind_fields`: `kind='purchase'` ⇒ `gallons_milli IS NOT NULL AND terminal_id IS NOT NULL AND effective_to IS NULL`. `kind<>'purchase'` ⇒ `gallons_milli IS NULL AND bol_id IS NULL`. `kind='adder'` ⇔ `adder_type IS NOT NULL`.
- Partial unique `uq_mce_active_natural_key` on (`tenant_id`, `natural_key`) where `status='active'`.
- Partial unique `uq_mce_active_bol` on (`tenant_id`, `bol_id`) where `status='active' AND bol_id IS NOT NULL`. One active price per BOL, so a BOL never has two competing prices.
- `ix_mce_lookup` on (`tenant_id`, `kind`, `product_code`, `terminal_id`, `effective_at`).

Partial indexes are declared on the ORM with both `postgresql_where` and `sqlite_where`, so `create_all` builds them in the SQLite test database. The migration declares them with `postgresql_where`.

### `margin_settings` (ORM `MarginSettingsORM`)

| Column | Type | Default / rule |
|---|---|---|
| `tenant_id` | PK `String(128)` | |
| `wac_window_days` | `Integer` | 30, from 1 to 365 (D6) |
| `rack_staleness_days` | `Integer` | 4, from 1 to 30 (D7) |
| `floor_micros` | `BigInteger` | 100,000, from 0 to 5,000,000 (D14) |
| `product_floors` | JSON | `{canonical_code: micros}`, at most 50 keys, each value from 0 to 5,000,000 |
| `timezone` | `String(64)` | `America/Chicago`, valid IANA name |
| `feed_activated_at` | `DateTime(tz)` null | Activation watermark (see "Activation watermark"). Set once, never by `PUT /settings`, never cleared. |
| `updated_by` / `updated_at` | null | `updated_by` stays null on a row created only for the watermark. |

A tenant with no row uses the defaults. A row created by `ensure_activated` carries the default settings values, so it reads the same as "no row" for every other column. The `timezone` column is an addition to FR6.1. FR1.2 interprets a date-only `effective_at` as midnight in the tenant's timezone, and the codebase has no tenant timezone source. `driver_daily_reset` hard-codes `America/Chicago` as its default, so this design uses the same default.

### `margin_records` (ORM `MarginRecordORM`)

| Column | Type | Notes |
|---|---|---|
| `record_id` | PK `String(64)` | `mr_<uuid4hex>` |
| `tenant_id` | not null | |
| `stage` | `String(16)` | `order_estimate` \| `delivery` \| `invoice` |
| `source_key` | `String(256)` | `order:{order_id}` for order stages, `invoice:{invoice_id}:line:{index}` for invoice lines. |
| `order_id`, `invoice_id`, `line_index`, `line_id` | null | `line_index` is the 0-based position in the persisted `line_items` and is the key. ES and Postgres agree on order (Postgres loads lines `order_by=position`, the enumerate index). `line_id` is informational only: ES lines usually carry none, and the Postgres mirror mints `line_<uuid>` when one is missing, so it differs between read paths. It is never used in `source_key`, `input_hash` or any lookup. |
| `customer_id`, `account_id` | `String(128)` null | |
| `product_code` | `String(64)` | Canonical, or the raw value truncated to 64 when unknown. |
| `terminal_id` | `String(128)` null | |
| `gallons_ugal` | `BigInteger` not null | |
| `unit_price_micros`, `revenue_cents` | `BigInteger` not null | |
| `method` | `String(16)` not null | `override` \| `wac` \| `rack_fallback` \| `none` |
| `product_cost_micros`, `adders_micros`, `landed_cost_micros` | `BigInteger` null | `adders_micros` is not null when `method≠none`. |
| `cost_cents`, `margin_cents`, `margin_per_gallon_micros` | `BigInteger` null | |
| `margin_bp` | `BigInteger` null | Basis points. Serialized as a 2-dp `margin_pct` string. `BigInteger` because a near-zero price on large gallons exceeds 32 bits, for example −3×10⁹ bp. |
| `no_cost_reason` | `String(40)` null | |
| `flag_missing_cost`, `flag_negative_margin`, `flag_below_floor`, `flag_terminal_unattributed` | `Boolean` not null | |
| `floor_micros_used` | `BigInteger` not null | |
| `cost_snapshot` | JSON not null | The full resolver output (FR2.1), including lot ids, diagnostics and the settings used. |
| `as_of` | `DateTime(tz)` not null | |
| `version` | `Integer` not null | |
| `status` | `String(16)` | `active` \| `superseded` \| `void` |
| `origin` | `String(16)` | `live` \| `recompute` |
| `frozen_at` | `DateTime(tz)` null | |
| `input_hash` | `String(64)` not null | See write protocol. |
| `recompute_run_id` | `String(64)` null | |
| `computed_at` | `DateTime(tz)` not null | |
| `alert_state` | `String(16)` not null, default `'none'` | `none` \| `pending` \| `done` \| `expired`. This is the RevenueGuard work queue (see Signals and RevenueGuard). |

Constraints and indexes:

- `ck_mr_cost_null_iff_none` covers every cost-derived column, so the database itself refuses a missing cost stored as 0 (AC-8). `adders_micros` is excluded on purpose, because it is reported even for `none`.

  ```sql
  CHECK ((method = 'none') = (landed_cost_micros IS NULL)
     AND (method = 'none') = (product_cost_micros IS NULL)
     AND (method = 'none') = (cost_cents IS NULL)
     AND (method = 'none') = (margin_cents IS NULL)
     AND (method = 'none') = (margin_per_gallon_micros IS NULL)
     AND (method <> 'none' OR margin_bp IS NULL))
  ```

  `margin_bp` may also be null with a cost (zero revenue), so it is a one-way rule. The last clause is written as `OR`, not boolean `<=`, so the same text works on SQLite and Postgres.
- `ck_mr_alert_state`: `alert_state IN ('none','pending','done','expired')`.
- `ck_mr_missing_flag`: `flag_missing_cost = (method = 'none')`.
- `uq_mr_version` on (`tenant_id`, `stage`, `source_key`, `version`).
- Partial unique `uq_mr_active` on (`tenant_id`, `stage`, `source_key`) where `status='active'` (AC-17).
- `ix_mr_tenant_asof` on (`tenant_id`, `as_of`, `record_id`), for keyset paging and range filters.
- `ix_mr_tenant_cust_prod` on (`tenant_id`, `customer_id`, `product_code`, `stage`, `as_of`), for leakage.
- `ix_mr_tenant_order` on (`tenant_id`, `order_id`), for stage preference.
- Partial `ix_mr_alert_pending` on (`tenant_id`, `computed_at`) where `alert_state='pending'`, for the RevenueGuard queue.

### `margin_alerts` (ORM `MarginAlertORM`)

`alert_id` PK, `tenant_id`, `alert_type` (`negative_margin` \| `missing_cost` \| `leakage_proposal` \| `recompute_digest`), `severity` (`high` \| `medium` \| `info`), `status` (`open` \| `acknowledged` \| `pending_review` \| `approved` \| `dismissed`), `dedupe_key` `String(256)`, `record_id` null, `order_id` null, `run_id` null, `proposal_id` null, `customer_id` null, `product_code` null, `details` JSON (ids and counts only), `created_at`, `resolved_by`, `resolved_at`, `resolution_note` `String(500)`.

- Unique `uq_malert_dedupe` on (`tenant_id`, `alert_type`, `dedupe_key`).
- `ix_malert_tenant_status` on (`tenant_id`, `status`, `created_at`).

### `margin_recompute_runs` (ORM `MarginRecomputeRunORM`)

`run_id` PK, `tenant_id`, `requested_by`, `start_date` (Date), `end_date` (Date), `stages` JSON, `only_missing`, `reason` `String(500)`, `status` (`running` \| `completed` \| `failed`), `counts` JSON, `started_at`, `finished_at`, `heartbeat_at`, `digest_state` `String(16)` not null, default `'none'` (`none` \| `pending` \| `done`).

- Partial unique `uq_mrun_running` on (`tenant_id`) where `status='running'`. One run per tenant at a time.
- Partial `ix_mrun_digest_pending` on (`tenant_id`) where `digest_state='pending'`.

### `margin_weekly_reports` (ORM `MarginWeeklyReportORM`)

PK (`tenant_id`, `iso_week` `String(8)`, for example `2026-W41`), `period_start`, `period_end`, `revenue_cents`, `cost_cents`, `margin_cents` (all three over records with cost), `revenue_cents_missing_cost` (revenue of `method=none` records, same meaning as in the summary), `records_total`, `gallons_ugal_total`, `flag_counts` JSON (`{flag: {"records": n, "gallons_ugal": g}}`), `missing_cost_share_bp`, `generated_at`.

`missing_cost_share_bp` is by record count: `missing_cost records × 10000 // records_total`, rounded half-up, and null when `records_total = 0`. The gallons share can be derived from `flag_counts.missing_cost.gallons_ugal / gallons_ugal_total`. The summary endpoint uses the same definition.

### `margin_skipped_sources` (ORM `MarginSkippedSourceORM`)

Durable record of phase-1 `invalid_inputs` skips (N6), so the gap is visible to admins across ECS tasks rather than only in one task's counter. PK (`tenant_id`, `stage`, `source_key`); `order_id`, `invoice_id`, `line_index` null; `reason` `String(32)` (`invalid_inputs`); `error_type` `String(64)` (an exception class name or a fixed code such as `gallons_not_positive`, never a value); `first_seen_at`, `last_seen_at`; `seen_count` `Integer`.

- Written by `MarginRepository.record_skip(...)`: select-for-update then insert or update `last_seen_at`/`seen_count`; a racing `IntegrityError` on the PK is retried once as an update.
- Deleted for a key by `write_record` whenever a record is inserted for that key (same transaction), so a fixed source clears itself.
- `no_inputs` skips (missing price, gallons or `delivery_result`) are not stored; they are expected states, not errors.

### Migration `0011_margin_feed`

- Follows the `0010_acct_override_audit` conventions: a docstring explaining why, a revision id under 32 characters, `down_revision = "0010_acct_override_audit"`, and plain `op.create_table` / `op.create_index` calls.
- JSON columns use `postgresql.JSONB`. They are stored, never queried, but this matches the 0008 note that new tables prefer `jsonb`. The ORM uses the existing `_JSONB` variant.
- `downgrade()` drops all seven tables and their indexes, with a docstring warning that this deletes margin data.
- Partial indexes (`uq_mce_*`, `uq_mr_active`, `ix_mr_alert_pending`, `uq_mrun_running`, `ix_mrun_digest_pending`) use `postgresql_where=sa.text(...)`. CHECK constraints are created inline in `op.create_table` through `sa.CheckConstraint(..., name=...)`.

Before merge, run `alembic heads`. The customer-portal design (`77449ed`) already claims `0011_customer_portal` in `20261008_0001_customer_portal.py` on the same parent, so whichever branch lands second must re-parent. If portal lands first, this revision becomes `0012_margin_feed` (file `20261008_0002_margin_feed.py`) with `down_revision = "0011_customer_portal"`. Never merge two heads.

## Money arithmetic (NFR1, AC-19–AC-23)

All parsing goes through `commerce/models/margin.py`:

- **Input type.** Money and gallon inputs from clients are typed `Union[StrictStr, StrictInt]` in Pydantic. A JSON float (`2.5`, `1e2`) fails with 422 `type=invalid_decimal`, message "send as a string". JSON numbers become `float` before any validator sees them, so `2.0000000000000001` would otherwise arrive as `2.0` and pass silently. This applies to `unit_cost_usd`, `gallons`, preview `unit_price_usd` and `gallons`, and the settings USD fields (`floor_usd_per_gallon`, `product_floors` values). CSV cells are strings already. `StrictInt` is accepted because an integer cannot lose precision.
- `parse_usd_micros(value, *, field, max_micros)`:
  1. A string must match `^\d{1,3}(\.\d{1,12})?$` after trimming (no sign, exponent, `NaN` or `Infinity`). Otherwise it fails with `invalid_decimal`.
  2. `d = Decimal(text)`, or `Decimal(int)` for an int.
  3. If `d != d.quantize(Decimal("0.000001"))`, it fails with `too_many_decimals`. This is the only dp rule. Trailing zeros that add no precision (`2.5000000`) pass, and real extra precision (`2.0000001`) fails. Nothing is ever rounded (AC-21).
  4. If `d × 1_000_000 > max_micros`, it fails with `out_of_range`.
  5. It returns `int(d * 1_000_000)`, which is exact after step 3.
- `parse_gallons_milli(value)` follows the same steps with the pattern `^\d{1,7}(\.\d{1,12})?$`, quantum `0.001`, and the rule > 0 and ≤ 1,000,000.
- Floats from documents use `Decimal(str(x))`: BOL `net_gallons`, contract and rack `*_usd`, and order or line gallons. Rack and contract USD prices are quantized to micros once, half-up. Record gallons are quantized to 6 dp half-up. BOL gallons are quantized to milli half-up.

Derived values use one rounding each:

- **WAC.** `Σ(gallons_milli_i × cost_micros_i)` is an exact `int`, divided as `Decimal` by `Σ gallons_milli_i` and quantized half-up to an int (AC-2: (1000×2.5 + 3000×2.6)/4000 → 2,575,000).
- **Revenue.** For invoices it is the persisted `subtotal_cents`. For order stages it is `line_subtotal_cents(gallons, unit_price_micros)`.
- **Cost.** `cost_cents = line_subtotal_cents(gallons_decimal, landed_cost_micros)`. This reuses the helper invoices already use (AC-20: 4,321.456 × 2,987,654 / 10,000 → 1,291,102).
- **Margin.** `margin_cents = revenue_cents − cost_cents` and `margin_per_gallon_micros = unit_price_micros − landed_cost_micros` are integer subtraction.
- **Margin percentage.** `margin_bp = quantize_half_up(Decimal(margin_cents) × 10000 / revenue_cents)`, and null if `revenue_cents == 0` or the cost is null. It is serialized as `f"{Decimal(bp) / 100:.2f}"` (AC-23). `ROUND_HALF_UP` rounds half away from zero, so a negative margin rounds symmetrically with a positive one (−12.5 bp → −13, +12.5 bp → +13). That is the intended rule. A boundary test covers both signs.
- **Summary totals.** These are integer `sum()` over stored integers, so they never drift (AC-22).

A unit test greps the new `margin_*` modules for `float(` and the `/` operator on money variables. The allowed exceptions are `Decimal(str(...))` inputs and the documented `Decimal` divisions.

## Cost entries (FR1)

`MarginCostEntryService` owns validation. Pydantic does the shape checks (`extra="forbid"`, string length limits, enums). The service does the semantic checks below, and collects every failure into `details.errors = [{"loc": [...], "msg", "type"}]` before raising a single `AppException(VALIDATION_ERROR, status_code=422)`. That is the same envelope as request validation.

| Field | Check | Failure `type` |
|---|---|---|
| `product_code` | `canonicalize()`. Unknown fails. | `unknown_product` |
| `terminal_id` | `TerminalRepository.get(tenant_id, id)` returns a terminal. Absent, or another tenant's (the repo is tenant-scoped), fails (AC-26). Required for `purchase`. | `unknown_terminal`, `required` |
| `effective_at` | `YYYY-MM-DD` means midnight in the settings timezone, or an ISO datetime with an offset. A naive datetime fails. Must be ≤ now + 1 day. | `invalid_datetime`, `too_far_future` |
| `effective_to` | `override`/`adder` only, and > `effective_at`. | `not_allowed`, `before_effective_at` |
| `unit_cost_usd` | `parse_usd_micros(max=100_000_000)` | `invalid_decimal`, `too_many_decimals`, `out_of_range` |
| `gallons` | `purchase`: required, `parse_gallons_milli`. Other kinds: must be absent. | |
| `adder_type` | Required if and only if `adder`. | |
| `bol_id` | `purchase` only. The doc is fetched from `terminal_bols` with a `tenant_id` term filter (no hit fails), then canonical product and `terminal_id` must equal the entry's. | `unknown_bol`, `bol_mismatch` |
| strings | `supplier_name`/`bol_id`/`reference` ≤ 128 characters, `notes` ≤ 500, `reason` 1–500. Control characters are rejected. | |

The gallons on a BOL-referencing purchase are kept for the admin's own reference. The lot always uses the BOL's net gallons (D5, AC-4). The create, supersede and import responses report this as `warnings: ["gallons_ignored_bol_net_used"]`, kept separate from the user-entered `notes`.

Operations:

- **Create.** `POST /cost-entries`. Inserts with `status=active`. A partial-unique `IntegrityError` on `natural_key` or `bol_id` returns 409 `CONFLICT` with `details.existing_entry_id`. The create endpoint rejects a duplicate. Only import reports and skips duplicates (FR1.5).
- **Supersede.** `POST /cost-entries/{id}/supersede`. The body is the full entry plus `reason`. In one `session_scope`:
  1. Lock the old row (`with_for_update()`). It must exist in this tenant (else 404) and be `active` (else 409), and the kind must match (else 422).
  2. Set the old row to `status=superseded` with reason, actor and time, and flush.
  3. Insert the new row with `supersedes_id` set, then set `superseded_by_id` on the old row.

  Flushing the old row first lets the new row reuse the same natural key.
- **Void.** `POST /cost-entries/{id}/void` with `{reason}`. Uses the same lock and status rules.
- **List.** `GET /cost-entries?kind&product_code&terminal_id&status=active|superseded|voided|all&cursor&limit≤200`. Keyset on (`created_at` desc, `entry_id` desc).
- **Import.** `POST /cost-entries/import?dry_run=true`, a multipart `file`.
  - The file is read in 64 KB chunks. More than 5 MB raises `MARGIN_IMPORT_TOO_LARGE` (413) before parsing.
  - It is decoded as `utf-8-sig`, and a decode error returns 422.
  - Columns are the FR1.2 field names. Required headers are `kind,product_code,effective_at,unit_cost_usd`. Unknown headers return 422. More than 10,000 data rows returns 413.
  - Every row is validated as for create, with two batching rules so a 10,000-row file stays cheap. Terminals are memoized per import (one `TerminalRepository.get` per distinct `terminal_id`). BOL ids are checked with one tenant-filtered `terms: {bol_id: [...]}` query per 1,000 rows, then matched in Python for product and terminal.
  - Duplicates are found both within the file (later rows duplicate earlier ones) and against active entries (one batched `SELECT natural_key ... IN (...)` per 1,000 rows). They are reported as `{row, natural_key, existing_entry_id?}` and skipped.
  - If any row is invalid, the response is 422 with `details.errors` listing `{row, loc, msg, type}`, and nothing is written (AC-35).
  - With `dry_run=false` and no errors, all non-duplicate rows are inserted in one transaction with one `import_batch_id`. A race `IntegrityError` returns 409 `CONFLICT` ("re-run the dry run").
  - The response is `{dry_run, rows_total, rows_valid, duplicates[], created_entry_ids[]}`.

Audit (FR1.7, AC-33): every mutation calls `get_telemetry_service().log_audit_event(...)`.

- `event_type` is one of `margin_cost_entry_created`, `_superseded`, `_voided`, `margin_cost_import` or `margin_settings_updated`.
- `user_id` is the acting user. `resource_type` is `margin_cost_entry`, `margin_settings` or `margin_recompute_run`. `resource_id` is the entry id, batch id, tenant id (settings) or run id.
- `action` (required by `log_audit_event`) is `create`, `supersede`, `void`, `import`, `update` (settings), `recompute_start` or `recompute_finish`.
- `details` holds `tenant_id`, the entry ids, and for supersede and settings changes `before` and `after` values. Import logs counts only.
- The call is wrapped in try/except. A failing audit sink logs WARNING and never fails the request, the same as `driver_endpoints.py:1299`.
- After the audit line, an INFO log carries the ids.

The entry history (superseded and voided rows stay readable) is the durable trail. The audit line is the event stream.

## Cost basis resolution (FR2)

`CostBasisResolver(readers, settings_snapshot).resolve(tenant_id, product_code, terminal_id | None, as_of) -> CostBasis`. `CostBasis` is a frozen dataclass that serializes to `cost_snapshot`:

```text
method, product_cost_micros, adders_micros, adders: [{adder_type, entry_id, micros, scope}],
adders_configured, landed_cost_micros, override_entry_id, lots: [{lot_type, id, gallons_milli,
unit_cost_micros, priced_by: entry|contract|rack, price_ref_id, rack_selection?,
contract_status_at_compute?}], rack_price_id,
rack_selection, contract_ids, wac_window_days, rack_staleness_days, window_start, as_of,
terminal_unattributed, no_cost_reason, diagnostics: {excluded: {reason: count}, bols_scanned,
lot_cap_exceeded, rack_cap_exceeded, zero_price_ignored}
```

The readers sit behind a small protocol so unit tests can pass fakes:

- `entries`: the margin repository.
- `bols`: a `search_documents(TERMINAL_BOLS_INDEX, inject_tenant_filter(...))` reader.
- `rack`: `RACK_PRICES_INDEX`.
- `plans`: `MVP_LOAD_PLANS_INDEX` (`mvp_load_plans`), batched `terms` reads.
- `contracts`: `SUPPLIER_CONTRACTS_INDEX` (`supplier_contracts`), batched tenant-filtered `terms` reads, with docs parsed through the `SupplierContract` model.

Algorithm (all reads tenant-scoped, AC-24):

1. **Product.** If `canonicalize(product_code)` fails, return `none` with `product_unknown`.
2. **Adders (D8, AC-10).** For each `adder_type`, take active `adder` entries for the product that are in effect at `as_of` (`effective_at ≤ as_of` and either no `effective_to` or `as_of < effective_to`). Terminal-specific beats tenant-wide. Within a scope, the latest `effective_at` wins, then the latest `created_at`, then the highest `entry_id`. When the terminal is unattributed, only tenant-wide adders are eligible. Sum the chosen adders. `adders_configured` is true if any adder was chosen.
3. **Override (AC-1).** Uses the same eligibility and ordering rules over `override` entries. A terminal-specific override beats a tenant-wide one, consistent with adders. When the terminal is unattributed, only tenant-wide overrides are eligible. If one is found, `method=override`.
4. **WAC (AC-2–AC-6).** The window is `(as_of − wac_window_days, as_of]`. The resolver collects lots:
   - **Standalone lots.** Active `purchase` entries with no `bol_id`, for the product, with `effective_at` in the window, at terminal T, or at any terminal when unattributed.
   - **BOL lots.** `terminal_bols` docs with `timestamp` in the window. The read is per (tenant, terminal or `*`, window) and covers **all products**: it filters on `terminal_id = T`, or `exists terminal_id` when unattributed, and has no product term. `TerminalBOL.product_code` is free text from OCR/EDI (`compliance/models/terminal_bol.py`), so a store-side canonical filter is impossible; the product match happens in Python (checks 3–4 below). It sorts by `timestamp` asc, `bol_id` asc, pages through `search_after` 200 at a time, and scans at most `bol_scan_cap = 5,000` docs of any product. `diagnostics.bols_scanned` counts every doc read.

   Each BOL is checked in order, and the first failing reason is counted in `diagnostics.excluded`:
   1. `needs_confirmation`: `needs_operator_confirmation` is true or `status == "pending_confirmation"`.
   2. `no_terminal`.
   3. `product_unknown`: `canonicalize` fails.
   4. A different canonical product is silently skipped. It is not an exclusion.
   5. `non_positive_gallons`.
   6. `unpriced`, when the pricing steps below find nothing.

   A BOL is priced by D5, first match wins. A rack or contract price ≤ 0 counts as absent at every step. Only `margin_cost_entries` may contribute a real zero (FR2.6).
   1. The active purchase entry with `bol_id` equal to this BOL. Entries are fetched in one batched `IN` query per BOL page.
   2. The linked contract. "Linked" means `plan.contract_id` and nothing else: no inference from `supplier_name` (D5, AC-5). The plan is the `mvp_load_plans` doc with `plan_id = bol.load_plan_id`. The contract is the one with that id. Dates govern, not the admin toggle: it counts when `effective_from ≤ lift_date ≤ effective_to` (or `effective_to` is null) and `contract_price_per_gallon_usd` is not null and > 0. `status` is **ignored**, because `SupplierContract.status` (`active`/`inactive`) is a current-state toggle with no history, and deactivating an ended contract must not re-price lots lifted under it. The lot records `contract_status_at_compute` for audit. `lift_date` is the BOL timestamp's date in the settings timezone. A contract priced 0 falls through to step 3 and increments `diagnostics.zero_price_ignored`.
   3. Rack at lift, chosen by the rack selection rule below with `t = bol.timestamp` and the supplier preference set to `bol.supplier_name`.

   If no step prices the BOL, it is excluded as `unpriced`.

   **Batched reads (per BOL page of 200).** After a BOL page is read, the resolver issues:
   - one `IN` query for the purchase entries with those `bol_id`s;
   - one `terms: {plan_id: [...]}` query on `mvp_load_plans` (tenant-scoped, `size` = number of distinct ids) for plans not already cached;
   - one `terms: {contract_id: [...]}` query on the supplier contracts index (tenant-scoped) for contracts not already cached. It is a tenant-filtered search, not `SupplierContractRepository.get` in a loop. Returned docs whose `tenant_id` differs from the caller's are dropped and logged at ERROR. This is the same defensive check `get` performs.

   **Rack reader.** Rack rows are read per (tenant, terminal, canonical product), never for a whole terminal:
   - The filter is `term tenant_id`, `term terminal_id`, `terms product_code ∈ aliases(canonical)` and `range effective_at ∈ [window_start − rack_staleness_days, as_of]`. `aliases(canonical)` is the canonical code plus every alias that maps to it in `fuel_product_catalog._ALIAS_INDEX`, each in its catalog form and its lower-case form. It is exposed as a new public helper, `fuel_product_catalog.aliases_for(canonical) -> frozenset[str]`. `RackPriceSyncService` already persists canonical codes, so the alias terms only matter for manual uploads. Each returned row is re-checked with `canonicalize()` in Python, and mismatches are dropped.
   - The read sorts by `effective_at` desc, `rack_price_id` desc, and pages with `search_after`, 500 rows per page.
   - The cap is 10,000 rows. If the cap is reached, the resolver stops. It returns `none` with `computation_error` and `diagnostics.rack_cap_exceeded=true`, and logs ERROR (tenant, terminal, product, window). This is the same treatment as the BOL cap. The rows are needed oldest-to-newest across the whole window, so a truncated set would silently unprice early BOLs.
   - Rows with `price_per_gallon_usd ≤ 0` are dropped after the read and counted in `diagnostics.zero_price_ignored`.
   - For an unattributed sale, rack-at-lift needs the rows of each BOL's own terminal. The resolver issues one rack read per distinct (BOL terminal, product), cached under that (terminal, product) key.

   **Rack selection rule** (`select_rack(rows, t, supplier=None)`, a pure function, used for both rack-at-lift and rack fallback):
   1. Candidates are rows with `effective_at ≤ t` and a positive price.
   2. If `supplier` is given, prefer candidates whose `supplier_brand` case-folds equal to `supplier.casefold()` (`brand_match`). Otherwise use unbranded rows (`branded_flag` false, `unbranded_max`). Use branded rows (`branded_max`) only if no unbranded row exists.
   3. Within the chosen set, take the latest `effective_at`. Among rows at that instant, take the highest price, because that never overstates margin. Break ties by `rack_price_id` asc.
   4. The pick is fresh if `t − effective_at ≤ rack_staleness_days`. A stale pick does not price a BOL lot. For fallback it yields `rack_stale`.

   The snapshot records `rack_price_id` and `rack_selection` (`brand_match` \| `unbranded_max` \| `branded_max`) for the fallback row, and the same pair as `price_ref_id` / `priced_by` detail on each rack-priced lot. Store order cannot change the pick, so `input_hash` stays stable across runs.

   If more than `bol_scan_cap` (5,000) BOL docs of any product fall in the (terminal or `*`, window) read, the resolver stops. It returns `none` with `computation_error` and `diagnostics.lot_cap_exceeded=true` (with `bols_scanned`), and logs ERROR (tenant, terminal or `*`, window). A WAC over a truncated lot set would be wrong without anyone noticing. The cap is shared by every product at that terminal, so it fires for all of them at once; the remedy is a narrower `wac_window_days`. At 30 days the cap allows about 166 loads a day per terminal, and for unattributed sales about 166 a day tenant-wide.

   If total lot gallons > 0, `method=wac`.
5. **Rack fallback (AC-7).** Only when the terminal is attributed. Apply `select_rack(rows, as_of)` with no supplier preference to the rows already read for the window (`as_of` is the window's upper bound, so no second read is needed). A fresh pick gives `method=rack_fallback`. A stale pick gives `none` with `rack_stale`. No positive-price row gives `none` with `no_lots_no_rack`, which includes the case where only zero-price rows exist. If the window read found no positive row at all, one extra `size: 1` query with the same filters, no lower bound, and `price_per_gallon_usd > 0` decides between `rack_stale` (a row exists) and `no_lots_no_rack`.
6. **Unattributed with nothing found (AC-11).** `none` with `terminal_unattributed_no_cost`.
7. **Landed cost.** When `method≠none`, `landed = product_cost + adders`. A zero product cost from an explicit entry stays 0 (AC-9). When `method=none`, `adders_micros` is still reported, but `landed_cost_micros` is null.

**Read budget and `ReaderCache`.** `CostBasisResolver` takes an optional `ReaderCache`, a plain per-run dict holder with no TTL and no cross-run sharing:

- **Who creates it.** One cache is created per gap-sweep tenant pass and per recompute run, and discarded at the end. Live hooks, `/cost-basis` and preview pass none, so they always read fresh.
- **What it caches.**
  - Plan docs by `plan_id`.
  - Contract docs by `contract_id`.
  - Rack rows by (terminal, product), with the widest window read so far. A narrower request is served by filtering in memory. A wider one re-reads.
  - BOL pages by (terminal or `*`, `window_start`, `window_end`), **without product**, because the read covers all products. Two products at one terminal with the same `as_of` date share one read, and each filters the cached docs in Python.
  - Entry query results by (kind, product, terminal, `as_of` date).
- **Memory.** Bounded by the caps: 5,000 BOLs and 10,000 rack rows per key. The cache holds at most 64 rack keys and 64 BOL keys, with LRU eviction.
- **Budget.** One uncached `resolve()` issues at most `ceil(bols_scanned/200) × 4 + Σ_terminals ceil(rack_rows_t/500) + 1 + 3` store queries. That is BOL page, entry IN, plan terms and contract terms per page (`bols_scanned` counts all products); rack pages, one rack read per distinct (terminal, product) needed, which for an attributed sale is one terminal and for an unattributed sale is one per distinct BOL terminal that needs rack-at-lift; one optional stale probe; and three entry queries (adders, override, standalone purchases). For an attributed sale with 1,000 scanned BOLs and 2,000 rack rows that is ≤ 28. With a warm cache, a second source with the same keys issues ≤ 3 (the entry queries, which are cheap indexed SQL).

Superseded or voided entries are never read (AC-12). Frozen records keep their snapshot because records are never recomputed implicitly (see the write protocol).

Resolver errors: a reader exception propagates. The caller (`MarginService.compute`) converts it into `none` with `computation_error` and logs ERROR with tenant, stage, source key and exception type (FR3.4, AC-16). For `GET /cost-basis` the same exception returns 503 `ELASTICSEARCH_UNAVAILABLE`, the existing "database connection failed" code. It is logged at ERROR, because an admin diagnostic should fail visibly.

## Terminal attribution (D11)

`MarginAttribution.terminal_for_order(tenant_id, order)`:

1. With no `assigned_run_id`, return `(None, "no_plan")`.
2. Query `mvp_load_plans` exactly as `PodService._resolve_loading_plan` does: tenant term, `should` on `plan_id`/`run_id` equal to `assigned_run_id`, size 5. Take the first plan whose `assignments[].station_id` matches the order's `customer_tank_id` or `order_id`.
3. If `plan.terminal_id` is set, return it.
4. Otherwise search `terminal_bols` for `load_plan_id = plan.plan_id` (tenant-scoped, size 20). If the BOLs carry exactly one distinct non-null `terminal_id`, return it.
5. Otherwise return `(None, "unattributed")`.

The lookup duplicates POD's query on purpose. It does not import POD's private method. The duplicated query is three lines and covered by a test.

## Margin records (FR3)

### Stage inputs

| Stage | Trigger | Price / revenue | Gallons | As-of | Customer / account |
|---|---|---|---|---|---|
| `order_estimate` | `order.dispatched` subscriber | `unit_price_micros_from_record(order)` / `line_subtotal_cents(gallons_requested, price)` | `gallons_requested` | `delivery_window_start`, else `created_at`, clamped to ≤ now | order |
| `delivery` | `order.delivered` subscriber, including the `reconcile_delivery_result` replay | same price / `line_subtotal_cents(actual, price)` | `delivery_result.actual_gallons` | `delivery_result.delivered_at` (no fallback; absent or unparseable → `Skip("invalid_inputs", "delivered_at_missing")`) | order |
| `invoice` (one per persisted line) | `InvoiceService` hook on generate, finalize and void | `unit_price_micros_from_record(line)` (falls back to legacy `unit_price_cents`) / line `subtotal_cents` | line `quantity_gallons` (else `quantity`) | `doc.delivered_at`, else `doc.created_at` | invoice |

`order_estimate` runs on `order.dispatched` because that is the first status at which a loading plan, and therefore a terminal, exists. Running at intake would always be terminal-unattributed. Orders delivered without passing through `dispatched` get no estimate. That is acceptable because their `delivery` record exists.

An order with no price or no gallons gets no `order_estimate` record, and a DEBUG line is logged. The same is true for `delivery` with no `delivery_result`. `order.cancelled` and `order.failed` void the order's `order_estimate` record.

The invoice stage never calls a pricing engine (AC-14). It reads `line_items[i]` from the doc returned by `generate_from_order`, `finalize_draft` or `void`, or from `InvoiceService.get` in the gap sweep and recompute. After OI-14 a split produces two lines, so two records with `line_index` 0 and 1.

### Computation

Computation has two phases, so a `computation_error` record always has valid inputs to store.

**Phase 1: `extract_inputs(stage, source) -> Inputs | Skip`.** A pure function over the deep-copied source. It produces `tenant_id`, `stage`, `source_key`, ids, canonical-or-raw product, `gallons_ugal`, `unit_price_micros`, `revenue_cents` and `as_of`, using the conversions above.

- A missing order price, gallons or `delivery_result` returns `Skip("no_inputs")`, logged at DEBUG as before.
- Any malformed value returns `Skip("invalid_inputs", error_type)`. That covers unparseable or non-finite gallons, gallons ≤ 0, a negative or out-of-range price (`line_subtotal_cents` raising), an invoice line without `subtotal_cents`, an invoice line with neither `unit_price_micros` nor `unit_price_cents` (persisted lines always carry a price, so its absence is recorded, not silently dropped), and a `delivery_result` whose `delivered_at` is absent or unparseable (`delivered_at_missing`).
- A skip writes no margin record. `invalid_inputs` logs ERROR once with `{tenant_id, stage, source_key, error_type, reason: "invalid_inputs"}`, increments the in-process `margin_skips{reason}` counter, and upserts a `margin_skipped_sources` row. Values are never logged or stored. `GET /summary` returns `skipped_sources: {count, sample: [source_key, ...≤20]}` for the tenant, so admins see sources with no margin at all. This is a deliberate deviation from the literal FR3.4/AC-16 "record written on error" (see Assumptions): a record cannot hold inputs that do not parse.
- The gap sweep and recompute call phase 1 too. They count `invalid_inputs` skips per cycle and log one WARNING with the count. The key is not retried within the cycle, and it is re-attempted at most once per later cycle. Phase 1 is a cheap pure function, so there is no retry storm against the store.

**Void needs no cost (N8).** For `mode=void` (invoice void, order cancelled or failed), `_run` first reads the latest row for the key. If one exists, phase 2 is skipped and `write_record(..., mode=void)` applies the transition with the latest row as-is (`MarginRepository.void_latest(tenant_id, stage, source_key)`, same lock and rule table). No resolver read happens, so a void can never produce a `computation_error`. Only the "no latest row → insert v1 `void`" cell runs phase 2 to build a tombstone candidate. If that phase 2 raises, the tombstone is written as `none` / `computation_error`; it is `status=void`, so it never alerts and never counts in summaries. If phase 1 skips for a void with no latest row, nothing is written, and the gap sweep's void reconciliation covers any later replay. Test: voiding an invoice whose resolver fake raises leaves the existing record `void` with its original cost fields and logs no ERROR.

**Phase 2: `compute(inputs, cache=None) -> Candidate`.** Only exceptions here (reader failures, resolver caps) produce a `computation_error` candidate, built from the phase-1 inputs with `method=none`. The steps:

1. Resolve the terminal: attribution for orders, and for invoices the order fetched with `FuelOrderRepository.get(tenant_id, order_id)`. If the order is missing, the terminal is unattributed.
2. Load the settings snapshot. If this raises, the `computation_error` candidate stores `floor_micros_used = DEFAULT_FLOOR_MICROS` (100,000) and `cost_snapshot.settings_unavailable = true`, so the NOT NULL column always has a defined value.
3. Call `resolver.resolve(...)`.
4. Compute the money fields.
5. Compute the flags (FR4):
   - `missing_cost`: `method == none`.
   - `negative_margin`: margin per gallon < 0.
   - `below_floor`: 0 ≤ margin per gallon < floor, where the floor is `product_floors.get(product)`, else `floor_micros`.
   - `terminal_unattributed`.

   Flags are a pure function of the candidate (AC-15, FR4.5), in `margin_service.compute_flags(candidate, floor)`.
6. Compute `input_hash = sha256(canonical_json({stage, source_key, gallons_ugal, unit_price_micros, revenue_cents, as_of, customer_id, product_code, terminal_id, method, product_cost_micros, adders_micros, override_entry_id, lot ids, rack_price_id, floor_micros_used}))`.

### Versioned write protocol (single serialization point)

`MarginRepository.write_record(tenant_id, candidate, mode)` runs in one `session_scope`. `mode` is one of `live`, `finalize`, `void` or `recompute`.

1. `SELECT ... WHERE tenant_id, stage, source_key ORDER BY version DESC LIMIT 1 FOR UPDATE`. On SQLite `with_for_update()` does nothing, so the unique indexes alone provide the guarantee there.
2. Apply the rules in order:

| Latest row | `live` | `finalize` | `void` | `recompute` |
|---|---|---|---|---|
| none | insert v1 active | insert v1 active + frozen | insert v1 `void` | insert v1 active (frozen if the source invoice is not draft) |
| `void` | skip | skip | skip | skip |
| active + frozen, same `input_hash` | skip | skip | set `void` | skip |
| active + frozen, different hash | skip | skip | set `void` | supersede, insert v+1 (frozen) |
| active, same `input_hash` | skip (AC-17) | set `frozen_at` in place | set `void` | skip |
| active, different hash | supersede, insert v+1 | supersede, insert v+1 frozen | set `void` | supersede, insert v+1 |

3. An `IntegrityError` on `uq_mr_version` or `uq_mr_active` means a concurrent first insert. The writer retries the whole write once. If the retry also fails, it logs ERROR (tenant, stage, source key) and gives up. The gap sweep repairs it later.
4. Every inserted row gets `alert_state='pending'` when `origin=live`, `stage ∈ {delivery, invoice}`, `status=active`, and at least one of `flag_negative_margin`, `flag_missing_cost` or `flag_below_floor` is set. Every other inserted row gets `'none'`. This happens in the same insert, so a record and its alert work are durable together, whichever ECS task wrote them. In-place transitions (`set frozen_at`) leave `alert_state` unchanged. `set void`, and marking the old row `superseded`, both change `pending` to `done`, because only active records alert.
5. Any insert also deletes the key's `margin_skipped_sources` row, in the same transaction.
6. The writer returns `WriteResult(written: bool, record)`.

This table is the whole concurrency story. Background tasks for draft, finalize and void of one invoice may run in any order, and the result is still "void wins, frozen is final, a recompute is the only thing that replaces frozen data" (AC-15, D13). No in-process lock is needed, and it works across ECS tasks, both for records and, through `alert_state`, for alerts.

A frozen record can only change through an explicit recompute. Live writers never re-resolve a frozen key, so a cost entry superseded later does not change it (AC-12).

### Execution and isolation from the business operation (FR3.4, NFR2, AC-16)

`MarginHook` methods are synchronous and return immediately: `invoice_generated(doc)`, `invoice_finalized(doc)`, `invoice_voided(doc)`, `order_event(order, event)`. They:

1. Return immediately if `commerce_margin_feed_enabled` is false or `is_persistence_enabled()` is false (AC-28).
2. Take `copy.deepcopy(doc)` (or of the order) synchronously, inside the hook method, before scheduling. Caller mutations after the hook returns therefore cannot leak into the computation.
3. Otherwise schedule `MarginService._run(stage, source, mode)` with `asyncio.create_task`. The task is added to `self._tasks` and removed by a done-callback, and a semaphore of 8 limits concurrency. When `len(self._tasks) >= MARGIN_HOOK_MAX_PENDING_TASKS` (2,000, a module constant), the hook schedules nothing and logs WARNING with `{tenant_id, stage, source_key}`. Delivery and invoice keys are repaired by the gap sweep. A dropped `order_estimate` is accepted, because the delivery record supersedes it.
4. Catch every exception around the copy and the scheduling, and log it at ERROR.

The invoice or order call never awaits margin work, so latency added to invoice generation is under 1 ms.

`_run` behaviour:

- Before its first write for a tenant, a live `_run` awaits `repo.ensure_activated(tenant_id)` (memoized per process once a non-null value is read). A failure there logs ERROR and the write still proceeds; the next hook or sweep pass retries activation.
- If phase 1 skips, nothing is written (see Computation).
- If phase 2 raises, the service builds a `computation_error` candidate from the phase-1 inputs (`method=none`, flag `missing_cost`) and writes it. It logs ERROR with `{tenant_id, stage, source_key, error_type}`, and no margin values.
- If the write itself fails because the database is unavailable, it logs ERROR and nothing else happens. The gap sweep repairs it later.
- On success it logs INFO with ids, stage and outcome (`written`/`skipped`). Margin values appear only at DEBUG (NFR5).

On shutdown, bootstrap awaits `margin_service.drain(timeout=10)`, and any tasks still running are cancelled and logged at WARNING.

Hook placement in `invoice_service.py`:

- In `generate_from_order`, the hook is the last statement before `logger.info("Generated invoice ...")`. That is after `mirror_invoice_create`, `mark_processed` and the dyed-diesel and meter post-checks. Those post-checks catch every exception and never raise today (`_run_dyed_diesel_post_check`); a future change that makes one raise would skip the hook, and the gap sweep then repairs the key. The idempotent-skip early return does not call the hook.
- In `finalize_draft` it goes after `_broadcast_invoice_ws(merged)`.
- In `void` it goes after `_broadcast_invoice_ws(merged)`.

### Gap sweep (durability)

A process that dies between persisting an invoice and running the margin task loses that task. ECS rolling deploys make this realistic. `run_margin_gap_sweep_cycle` runs every 6 hours through `run_periodic("commerce.margin-gap-sweep", 21_600, ...)`, which is leader-elected.

- It does nothing when the flag or persistence is off.
- Tenants are found the way `price_protection_expiry_job` finds them: a `terms` aggregation on `invoices_current.tenant_id`, plus one on `fuel_orders_current.tenant_id`.
- Each tenant pass starts with `activated_at = await repo.ensure_activated(tenant_id)` (see "Activation watermark"). The lower bounds below use it.
- For each tenant, the sweep reconciles state, not just existence:
  - **Invoices.** It reads invoices with `updated_at ≥ max(now − 72h, activated_at)` (`InvoiceService.list(updated_from=...)`, cursor). That catches drafts finalized or voided long after creation. `updated_from: Optional[datetime]` is added on **both** read paths, mirroring `created_from`:
    - ES: `InvoiceService.list`, `count` and `_invoice_must_clauses` add `range updated_at gte`.
    - Postgres (staging runs `COMMERCE_READ_FROM_POSTGRES=true`): `InvoiceService.list`/`count` pass `updated_from=` to `read_invoice_list`/`read_invoice_count`, which forward `**kwargs` unchanged to `InvoiceReadRepository.list`/`count`. `InvoiceReadRepository._list_filters` and the keyword-only `list` signature gain `updated_from=None` → `InvoiceORM.updated_at >= updated_from`. `TimestampMixin.updated_at` has `onupdate=_utcnow`.
    - It is a filter only. Paging stays on the existing keyset (`created_at` desc, `invoice_id`) on both paths, so cursors and sort order are unchanged.

    Every ES invoice write path sets `updated_at`. That the Postgres mirror moves `updated_at` on every invoice write (ORM updates, not a Core `update()` without it) is unverified; the dual-path test below covers it. For each page, one `SELECT ... WHERE status IN ('active','void') AND source_key IN (...)` loads the latest record per line key. Then for each line key:
    - key missing and the invoice's event time is before `activated_at`: no action (pre-activation source; only recompute reaches it). The event time is `voided_at` for a void invoice, else `finalized_at` when set, else `created_at`. This is what the hook would have seen, so an unrelated later touch of an old invoice (a payment update moving `updated_at`) never creates a record;
    - key missing: write with mode `void` for a void invoice, `live` for a draft, and `finalize` otherwise;
    - invoice `void`, record not `void`: write with mode `void`;
    - invoice not draft and not void, record active with `frozen_at IS NULL`: write with mode `finalize`. The rule table either freezes in place (same hash) or supersedes;
    - anything else: no action.

    For a void invoice, keys of lines that are missing are written as `void` v1, so a later replay cannot create an active row.
  - **Orders.** It reads delivered orders with `start_date = (now − 14 days).date().isoformat()` (a `YYYY-MM-DD` string, the type the repository expects; `FuelOrderRepository.search(status="delivered", start_date=..., keyset=True)`, which filters on `created_at`), keeps only orders whose `delivery_result.delivered_at ≥ activated_at` (an order with no `delivered_at` is kept only if its `updated_at ≥ activated_at`), and writes missing `delivery` keys. It also reads `cancelled` and `failed` orders created in the last 14 days, using the same `search` with no new filter, and voids any active `order_estimate` record they still have. Voids never alert, so they need no activation filter. Estimates exist only from `dispatched` onward, so the 14-day creation window covers realistic cancellations.
  - The sweep passes one `ReaderCache` per tenant pass to `compute`.
- Records written by the sweep get `origin=live`, so they alert like the event they replace.
- Each tenant is capped at 5,000 sources per cycle. Hitting the cap logs WARNING.
- A failure in one tenant is logged and does not stop the others.

### Activation watermark (FR3.5, D18)

Without a watermark, the first sweep after `COMMERCE_MARGIN_FEED_ENABLED` flips on would find a missing key for every invoice touched in the last 72 h and every order delivered in the last 14 days, and write them as `origin=live`, `alert_state='pending'`. Tenants have no cost entries on day one, so nearly all of those are `method=none`: one `missing_cost` alert per old delivery and line. That is an automatic backfill (FR3.5 says there is none) and the per-record flood D18 exists to prevent.

- `MarginRepository.ensure_activated(tenant_id) -> datetime` runs in one `session_scope`: select the `margin_settings` row for update; if absent, insert one with default values and `feed_activated_at = now()`; if present with `feed_activated_at IS NULL`, set it to `now()`; return the stored value. A racing `IntegrityError` on the PK is retried once as the select-and-update path (same pattern as `record_skip`). The value is never moved once set.
- It is called by the live hook path before its first write for a tenant (memoized per process) and at the start of every sweep tenant pass.
- The live hook applies no watermark filter. Hook events happen after the flag is on, so they are post-activation by definition. This includes `reconcile_delivery_result` replays, which are late POD syncs of real post-enable deliveries.
- The sweep writes a missing key only for a source whose event time is at or after `feed_activated_at` (rules above). Repairs of keys that already have a record (finalize, void) are unaffected.
- Sources from before activation are reached only through `POST /recompute` (`origin=recompute`, no per-record alerts, at most one digest).
- Turning the flag off and on again keeps the original `feed_activated_at`. Events during the off-window fired no hook; the sweep repairs those within its 72 h / 14 d lookbacks, **with** per-record alerts, because they happened after activation. Older off-window sources need a recompute. This is intended; Rollout step 2 exercises it.

### Recompute (FR3.5, AC-18)

`POST /recompute` takes `{start_date, end_date, stages?: ["invoice","delivery"], only_missing: true, reason}`.

- Dates are `YYYY-MM-DD` and mean **`as_of` dates in the settings timezone**, the same axis as `GET /records`, `/summary`, the export and the weekly report. The half-open instant range is `[start 00:00, (end + 1 day) 00:00)` in that timezone, converted to UTC. The inclusive span must be at most 92 days, else 422. `reason` is 1–500 characters. `order_estimate` cannot be recomputed, since estimates are superseded by deliveries.
- The service inserts a `margin_recompute_runs` row with `status=running`. If a run is already running, the response is 409 `MARGIN_RECOMPUTE_RUNNING`. The exception is a running row whose `heartbeat_at` is more than 1 hour old: that row is first marked `failed` (WARNING log), and the new run proceeds.
- The endpoint returns 202 `{run_id}`. Progress is at `GET /recompute/{run_id}`.

The background task enumerates a widened creation window, because both source stores filter on `created_at` (`order_repository.py` `range_field="created_at"`), then filters on `as_of`:

- Delivered orders: `FuelOrderRepository.search(status="delivered", start_date=(start_utc_date − 31 days).isoformat(), end_date=(end + 2 days).isoformat(), keyset=True)`, both `YYYY-MM-DD` strings, where `start_utc_date` is the UTC date of the range's first instant. The repository compares `created_at` against bare ISO date strings (ES fills in `00:00:00`; Postgres compares strings lexically), so `end_date` must be at least the UTC date after the last local instant in range; `end + 2 days` covers every IANA offset. A delivery's `as_of` is `delivered_at`, which is never before `created_at`; 31 days covers 30 days of order-to-delivery lead time plus the timezone shift. The query bounds only widen the candidate set: the phase-1 `as_of` filter, not the query bound, decides membership.
- Invoices: `InvoiceService.list(created_from=start − 14 days, created_before=min(now, end + 15 days))`. An invoice's `as_of` is `doc.delivered_at`, which normally precedes `created_at` by minutes (the `order.delivered` subscriber), but a draft or replayed invoice can be created days later, so the upper bound is widened as well as the lower. The lower widening covers `as_of = created_at` fallbacks across the timezone boundary.
- Phase 1 computes `as_of` for each source; any source whose `as_of` is outside the half-open range is dropped before phase 2 and counted in `counts.out_of_range`. Sources outside the widened windows are not reached; that limit is stated in the recompute panel's help text (see Admin UI) and under Assumptions.

Void invoices are skipped. With `only_missing=true`, a key is processed only if it has no active record or its active record has `method=none`. That includes frozen ones, which is the late-BOL case. Each key is written with `mode=recompute`, `origin=recompute` and `recompute_run_id`.

- The heartbeat is updated every 100 sources.
- On finish, the run is set to `completed` with counts: `{sources, out_of_range, written, skipped, by_flag: {...}}`. `sources` counts in-range sources only.
- On an unexpected exception, the run is set to `failed` and an ERROR is logged.
- The run start and end are audit-logged (`margin_recompute_started` / `_finished`) with tenant, user, range and counts.
- Recompute records are `origin=recompute`, so the writer gives them `alert_state='none'` and no per-record alert or signal ever follows (D18, AC-44). On `completed`, the same transaction that sets the counts sets `digest_state='pending'` when `sum(by_flag.values()) > 0`, and `digest_state='done'` otherwise (AC-44 asks for a digest only when a run produces flagged records). RevenueGuard turns a pending state into the single digest alert. One ids-only digest signal is also published as a hint, only for pending digests.
- With `only_missing=false`, a key whose recomputed `input_hash` equals its frozen record's hash is skipped (rule table), so an unchanged record does not churn versions.
- The run passes one `ReaderCache` to every `compute` call.

There is no automatic backfill when the flag is first enabled: the activation watermark keeps the gap sweep off pre-activation sources. The admin runs recompute.

## Signals and RevenueGuard (FR5)

### Delivery model (freeze: the database is the queue)

`SignalBus` is in-process, and `BaseAgent._run_loop` runs `monitor_cycle` only on the sweep leader. A margin record can be written on any ECS task: whichever handled the invoice, POD or order request, or ran the recompute. A bus-driven RevenueGuard would therefore miss every flagged record written on a non-leader task, and the non-leader's `_signal_buffer` would grow without bound. Alert work is instead carried in the database:

- `margin_records.alert_state='pending'` is set by `write_record` in the same insert as the record (see the write protocol).
- `margin_recompute_runs.digest_state='pending'` is set on run completion.
- RevenueGuard drains both on the leader. The bus signal is a hint for other consumers only.

This removes the "which process holds the signal" class of problem entirely, and it survives restarts and leader changes.

### Publishing (margin service)

FR5.2 is kept as written. After a successful write with `written=True`, `origin=live`, `stage ∈ {delivery, invoice}`, `status=active` and at least one of `negative_margin`, `missing_cost` or `below_floor` (exactly the rows written with `alert_state='pending'`), the service publishes one ids-only `RiskSignal`. Nothing depends on its delivery:

- `source_agent="margin_feed"`, `entity_type` = the stage, `entity_id` = the record id, `tenant_id`.
- `severity`: HIGH for negative margin, MEDIUM for missing cost, LOW for below floor.
- `confidence=1.0`, `ttl_seconds=86400`.
- `context={"flags": [...], "stage": ..., "record_id": ..., "customer_id": ..., "product_code": ...}`.

The context holds ids and flag names only, because `SignalBus` persists every signal to `agent_signals` (FR8.2).

A recompute run publishes one signal: `entity_type="margin_recompute_run"`, `entity_id=run_id`, `severity=LOW` (the enum has no INFO), and `context={"run_id", "counts_by_flag"}`. If no bus is set (`set_signal_bus` not called), the service logs INFO once and publishes nothing. A publish exception logs WARNING. In both cases the alert still happens, because it comes from `alert_state`.

### RevenueGuard rewrite

- **Subscriptions.** Exactly `[{"message_type": RiskSignal, "filters": {"source_agent": "margin_feed"}}]`, which satisfies FR5.2's "RevenueGuard subscribes". The `fuel_management_agent` and `OutcomeRecord` subscriptions are removed. `_on_signal` is overridden to discard the signal without buffering. It only increments a DEBUG counter. So no process, leader or not, accumulates a buffer, and work always comes from the queue.
- **Cooldown.** The constructor keeps passing `cooldown_minutes=60` to the base class explicitly. The base default is 15, and AC-40 needs 60.
- **Removed code.** `DEFAULT_MARGIN_TARGET_PCT`, `_compute_route_margins`, `_route_margins`, `_detect_leakage` over percentages, every `jobs_current` read, and `_maybe_generate_weekly_report` (AC-45).
- **Constructor.** It keeps `leakage_threshold` (default 3). The repository is injected with `set_margin_repository(repo)`. If no repository is set, `monitor_cycle()` logs WARNING once and returns `([], [])`.
- **State.** The agent holds no tenant data in memory (FR5.6, AC-27). The only in-memory structure is the base cooldown tracker, keyed `leak:{tenant_id}:{customer_id}:{product_code}`. Leakage history is read from the database every time, always with the tenant filter.

**Cycle (freeze: RevenueGuard owns its loop, Simplification 11).** The base `OverlayAgentBase.monitor_cycle` calls `evaluate(tenant_signals)` with no tenant id, and for a queue-driven agent that list is always empty, so `evaluate` cannot know which tenant it serves. RevenueGuard therefore overrides `monitor_cycle` completely and never calls `super().monitor_cycle()`:

```python
async def monitor_cycle(self):
    if self._repo is None:
        self._warn_no_repo_once(); return [], []
    started = time.monotonic()
    try:
        await self._repo.expire_stale_pending(older_than=timedelta(days=7))
        tenants = await self._repo.pending_work_tenants()
    except Exception:
        self.logger.exception("revenue_guard queue query failed"); return [], []
    proposals_all = []
    for tenant_id in tenants:
        try:
            mode = await self._get_mode(tenant_id)
        except Exception:
            self.logger.exception("revenue_guard mode read failed tenant=%s", tenant_id)
            continue                              # rows stay pending
        if mode == "disabled":
            continue                              # rows stay pending (AC-43)
        commit = mode in ("active_gated", "active_auto")   # fail closed
        try:
            proposals = await self._evaluate_tenant(tenant_id, commit)
        except Exception:
            self.logger.exception("revenue_guard tenant pass failed tenant=%s", tenant_id)
            continue
        for p in proposals:
            if mode == "shadow":
                await self._log_shadow_proposal(p)
            else:
                await self._route_proposal(p, mode)
        proposals_all.extend(proposals)
    self._cycle_metrics.update({"signals_consumed": 0,
        "proposals_generated": len(proposals_all),
        "cycle_duration_ms": (time.monotonic() - started) * 1000})
    return [], proposals_all

async def evaluate(self, signals):    # abstract-method stub; never does work
    return []
```

- `expire_stale_pending` is `UPDATE ... SET alert_state='expired' WHERE alert_state='pending' AND computed_at < now − 7d`. It logs INFO with a count per tenant.
- `pending_work_tenants` is `SELECT DISTINCT tenant_id FROM margin_records WHERE alert_state='pending'`, unioned with `SELECT DISTINCT tenant_id FROM margin_recompute_runs WHERE digest_state='pending'`. Both are served by the partial indexes.
- The mode is read **once per tenant per cycle** and both the skip and `commit` derive from that one value, so a flag flip mid-cycle cannot mix modes for one tenant. An unknown mode string is not `commit` (fail closed, matching `_is_active_commit_mode`). Every repository call inside `_evaluate_tenant` takes that `tenant_id` explicitly.
- RevenueGuard does not override `_pending_work_tenants()` and has no `_pending_tenants` field. Neither is used.

`monitor_cycle` only runs on the sweep leader, because `_run_loop` gates it. If the queue queries raise, it logs ERROR and returns `([], [])`, and the next cycle retries. A failing tenant is logged and skipped; the others proceed.

The 7-day expiry bounds the queue while a tenant is `disabled`. It also means re-enabling RevenueGuard does not flood admins with alerts about old sales. Expired records stay visible and filterable in the Records list by their stored flags. Nothing about the record changes.

`_evaluate_tenant(tenant_id, commit)` does:

- **Digests.** For each run with `digest_state='pending'`: if `commit`, insert alert `recompute_digest` (severity `info`, `dedupe_key=run_id`, details = counts). Otherwise write an activity-log entry. Then set `digest_state='done'`.
- **Records.** Load up to 500 records with `alert_state='pending'` for the tenant, ordered by `computed_at`, `record_id`. More remain for the next cycle (60 s). For each record:
  - If it is no longer `active`, set `done` and stop. The writer normally does this already. This is a guard.
  - If it is flagged `negative_margin` or `missing_cost`, create one alert per record and type: `dedupe_key=record_id`, severity `high` for negative and `medium` for missing (AC-38, AC-41). The unique constraint makes "at most one alert per record" hold even across replays. A duplicate insert is a no-op. With `commit` false, write an activity-log entry instead.
  - If it is flagged `below_floor` and `record.stage == leakage_stage`, run the leakage check below (AC-39, AC-40). There is no per-record alert.
  - Mark the record `done`. With `commit`, `repo.record_alert_outcome(tenant_id, record_id, alerts=[...])` inserts the alerts (each in a savepoint, so a dedupe `IntegrityError` is a no-op) and sets `alert_state='done'` in one transaction. In shadow mode the activity-log write happens first, then `done` is set. A crash between the two can repeat a shadow activity entry, never an alert.
  - An exception on one record logs ERROR (tenant, record id, error type) and leaves it `pending` for the next cycle. Processing continues with the next record.

`leakage_stage` is `"invoice"` when `commerce_invoicing_enabled`, else `"delivery"`, so one sale is never counted twice, once as a delivery and once as an invoice.

**Reading "live" in FR5.4 (freeze).** The trigger is live-only, but the window counts any origin:

- **Trigger.** Only records written with `origin=live` enter the queue, so only a live record can start a leakage check.
- **Window.** The N records examined are the latest N **active** records of either origin. A recompute record that corrects a sale stands for that sale, and the superseded live version is no longer active.

This is the only reading that neither double-counts nor ignores corrections. Tests: two recompute-origin below-floor records plus one new live below-floor record trigger exactly one proposal. A recompute run on its own (three below-floor recompute records, no live record) triggers none.

Leakage check:

1. Read the last `leakage_threshold` **sales** for (tenant, customer, product, `leakage_stage`), of either origin. A sale is one `invoice_id` at the invoice stage (an OI-14 split yields two line records for one sale) and one `order_id` at the delivery stage. The repository reads active records ordered by `as_of` desc, `record_id` desc, groups them by sale key in Python, and stops once it has `leakage_threshold` sales (reading at most `leakage_threshold × 4` rows). A sale is below floor when any of its lines is `flag_below_floor`.
2. Leakage holds when there are exactly `leakage_threshold` sales and all are below floor. The evidence is every record id in those sales.
3. Skip when the cooldown key is active, or when an alert of type `leakage_proposal` with status `pending_review` already exists for (customer, product).
4. Otherwise build a `PolicyChangeProposal`:
   - `source_agent=self.agent_id` (required by `data_contracts.py`), `tenant_id=tenant_id`
   - `parameter=f"margin.review.{customer_id}.{product_code}"`
   - `old_value={"flag": "below_floor", "consecutive": n}`
   - `new_value={"action": "review_pricing"}`
   - `evidence` = the record ids
   - `rollback_plan={"action": "none", "reason": "advisory only"}`
   - `confidence=0.9`
5. If `commit`, insert alert `leakage_proposal` with status `pending_review`, severity `high`, `dedupe_key=f"{customer}|{product}|{newest_record_id}"` and the proposal id, in the same `record_alert_outcome` transaction as the record's `done`.
6. Set the cooldown and return the proposal. In active mode the base `_route_proposal` publishes it on the bus. A `PolicyChangeProposal` is not an `InterventionProposal`, so nothing goes to the ConfirmationProtocol or approval queue. Shadow mode is covered below.

**Shadow mode writes only activity-log entries (AC-43).** RevenueGuard overrides `_log_shadow_proposal` to write an activity-log entry (`action="margin_leakage_shadow"`, `proposal_id`, the evidence record ids, customer and product ids) instead of indexing into `agent_shadow_proposals`. FR5.3 says shadow writes to the activity log only, and the override makes that literal.

**How "HIGH risk, human approval" is represented.** `PolicyChangeProposal` has no risk field. HIGH risk is carried by the `leakage_proposal` alert (`severity=high`, `status=pending_review`). Human approval is the admin's approve or dismiss action on that alert in the Margin UI. Nothing executes on approval (FR5.9). Every alert transition (acknowledge, approve, dismiss) is audit-logged through `log_audit_event` with `event_type="margin_alert_resolved"`, `resource_type="margin_alert"`, `resource_id=alert_id`, `action` = `acknowledge` \| `approve` \| `dismiss`, and `details={tenant_id, alert_type, proposal_id?, from_status, to_status}` (no note text, no amounts), wrapped like the other audit calls.

The proposal carries ids and flag names, never cents. That matters because it reaches `agent_signals` (FR8.2). Approving or dismissing it is an admin action in the Margin UI. Approval only records the decision. RevenueGuard never changes prices or rules (FR5.9).

Activity-log entries go through `activity_log_service.log({...})` with `agent_id`, `tenant_id`, `action` (`margin_alert_shadow` / `margin_leakage_shadow`), `record_id`, `flags`, and no amounts. Activity entries are visible to dispatchers (`Agents/api_authz.py`), so a unit test asserts the payload keys.

**One sale can produce two alerts.** With invoicing on, a negative or missing-cost sale can alert once on its `delivery` record (priced at the order price, before contract repricing) and once on its `invoice` record. Each record is a distinct FR5.3 subject, so both are kept. The two alerts can also disagree, for example when the contract price fixes the margin. The alerts list therefore returns `order_id` on each alert (from the record), and the UI groups alerts by `order_id`.

**Alerts are pull-only.** Alerts are read through `GET /alerts`. There is no WebSocket push or toast, because every existing push channel is dispatcher-visible. The Margin tab shows a count badge of `open` plus `pending_review` alerts, from `GET /alerts?status=open,pending_review&limit=1`, which returns `total`. The badge is fetched when CommerceHub mounts for an admin and refreshed every 5 minutes while the tab is visible.

### Weekly report (FR5.7, AC-46)

`run_margin_weekly_report_cycle` runs daily through `run_periodic("commerce.margin-weekly-report", 86_400, ...)`. It moves out of RevenueGuard because the old report fired only when signals arrived, used one process-wide timestamp for all tenants, and wrote to a strict-mapped document index that cannot hold the new fields.

- It returns immediately, touching nothing, when `commerce_backbone_enabled`, `commerce_margin_feed_enabled` or `is_persistence_enabled()` is false, exactly as the gap sweep does (AC-28).
- Tenants are found with `SELECT DISTINCT tenant_id FROM margin_records`.
- For each tenant, the previous ISO week runs from Monday 00:00 to Monday 00:00 in the settings timezone. If no report row exists for that week, the job runs the summary aggregation over it (with the stage preference below) and inserts the row.
- The primary key makes reruns no-ops.
- Reports are readable at `GET /api/commerce/margin/reports`. The old `agent_revenue_reports` index is no longer written. Its mapping stays, so existing documents keep resolving.

## Admin API (FR6)

### Guard (D1, D2, AC-28–AC-30)

```python
async def require_margin_enabled(tenant=Depends(get_tenant_context)) -> TenantContext:
    s = get_settings()
    if not (s.commerce_backbone_enabled and s.commerce_margin_feed_enabled
            and is_persistence_enabled()):
        raise AppException(ErrorCode.COMMERCE_DISABLED, "Margin feed is not enabled",
                           status_code=404)
    return tenant

async def require_margin_admin(tenant=Depends(require_margin_enabled)) -> TenantContext:
    require_role(tenant, "admin")   # exact match; platform_admin alone -> 403
    return tenant
```

The router is `APIRouter(prefix="/api/commerce/margin", dependencies=[Depends(require_margin_admin)])`. A router-level dependency means a route added later still inherits the gate, the same reasoning as `commerce_staff_dependency`. Handlers also take `tenant: TenantContext = Depends(require_margin_admin)` to read `tenant_id`, and FastAPI caches the dependency per request.

The export uses `export_guard("admin", base=require_margin_enabled)`, so the flag 404 still comes before the role 403.

Customer-portal sessions: the portal design (`77449ed`) refuses a `customer` role in `AuthEnforcementMiddleware` with a central default-deny (403 `PORTAL_ROUTE_FORBIDDEN`) before routing. So a customer session gets 403 on every margin route whatever the flag state; the "404 when the flag is off" rule (AC-28) applies to staff roles only. Until the portal lands, the router guard alone returns 403 for any session without `admin`.

Route order: `GET /records/export` is declared **before** `GET /records/{record_id}` in `margin_endpoints.py`. Otherwise FastAPI matches `export` as a record id and the request bypasses `export_guard` and the limiter. A route-order test asserts `/records/export` resolves to the export endpoint.

`tenant_id` only ever comes from `TenantContext` (AC-25):

- Bodies use `extra="forbid"`, so a body `tenant_id` returns 422.
- No route declares a `tenant_id` query parameter, so FastAPI ignores one.

### Endpoints

All responses use `{"data": ..., "request_id": ...}`. Lists return `{"items": [...], "next_cursor": str | null}`. Cursors are opaque URL-safe base64 of the keyset tuple, and a cursor that fails to decode returns 422.

| Method / path | Body / query | Success | Errors |
|---|---|---|---|
| `GET /cost-entries` | see FR1 list | 200 | 422 bad filter |
| `POST /cost-entries` | entry | 201 entry | 422, 409 duplicate |
| `POST /cost-entries/{id}/supersede` | entry + `reason` | 201 new entry | 404, 409 not active, 422 |
| `POST /cost-entries/{id}/void` | `{reason}` | 200 | 404, 409, 422 |
| `POST /cost-entries/import` | multipart `file`, `dry_run` (default true) | 200 report | 413, 422, 409 race |
| `GET /cost-basis` | `product_code` (required), `terminal_id?`, `as_of?` (ISO, default now) | 200 `CostBasis` + lots | 422 unknown product or terminal, 503 resolver failure |
| `GET /records` | `start_date`, `end_date` (`parse_date_range` on `as_of`), `customer_id`, `product_code`, `terminal_id`, `stage`, `flag` (one `MarginFlag`), `status` (default `active`), `cursor`, `limit` (1–200, default 50) | 200 | 422 |
| `GET /records/{record_id}` | | 200, including `cost_snapshot` and version history | 404 |
| `GET /records/export` | same filters, no paging | CSV stream | 413 over 50k, 429 |
| `GET /summary` | `group_by` = `day`\|`customer`\|`product`\|`terminal`, `start_date`, `end_date` (default the last 30 days, span ≤ 92 days) | 200 | 422 |
| `POST /preview` | see below | 200 | 422 |
| `POST /recompute` | see above | 202 `{run_id}` | 409, 422 |
| `GET /recompute/{run_id}` | | 200 run | 404 |
| `GET /alerts` | `status` (comma-separated `AlertStatus` values), `alert_type`, `cursor`, `limit` | 200, items include `order_id`, and the response includes `total` | 422 |
| `POST /alerts/{id}/acknowledge` | `{note?}` | 200 | 404, 409 wrong state |
| `POST /alerts/{id}/approve` and `/dismiss` | `{note?}`, `leakage_proposal` only | 200 | 404, 409 |
| `GET /reports` | `limit` ≤ 52 | 200 | |
| `GET /settings` / `PUT /settings` | `{wac_window_days, rack_staleness_days, floor_usd_per_gallon, product_floors: {code: usd}, timezone}` | 200; `PUT` returns `warnings: ["wac_window_may_exceed_bol_scan_cap"]` when `wac_window_days > 30` (accepted, not rejected) | 422 |

The `alerts` and `reports` endpoints are additions to FR6.1. RevenueGuard needs an admin-only channel for its alerts and proposals, because the approval queue and activity log are dispatcher-visible (`Agents/api_authz.py`) and FR8.1 forbids margin data there. The existing notification service is customer-facing.

Summary stage preference (D12) is computed in Python over the active records in the range:

- Invoice records count for their `order_id`.
- A `delivery` record counts only if its order has no active invoice record in the result set.
- An `order_estimate` record counts only if its order has neither.

Rows are streamed in pages of 5,000, selecting only the integer columns. Totals (per group and overall):

- `revenue_cents`: every counted record.
- `revenue_cents_with_cost` and `revenue_cents_missing_cost`: the split of `revenue_cents` by `method≠none` / `method=none`. Their sum always equals `revenue_cents`.
- `cost_cents` and `margin_cents`: over records with cost only, so they pair with `revenue_cents_with_cost`, never with `revenue_cents`. The response field docs and the UI labels say so ("Revenue (costed)", "Revenue (no cost)", "Cost (costed records)").
- `records` and `gallons_ugal` totals, per-flag counts, and `missing_cost_share_bp`.
- `skipped_sources: {count, sample}` from `margin_skipped_sources` (tenant-wide, not range-filtered), so sources with no record at all are visible.

Day grouping uses the settings timezone. The 92-day cap bounds the scan to a few hundred thousand rows at most, and keeps the aggregation portable between SQLite and Postgres.

Preview (FR6.2, AC-42) takes `{product_code, gallons, terminal_id?, as_of?, unit_price_usd? | customer_id? + account_id?}`:

- Exactly one of `unit_price_usd` and `customer_id` must be set, else 422.
- `unit_price_usd` is parsed with `parse_usd_micros(max_micros=100_000_000)`, the same bound as cost entries; over-max or malformed → 422.
- `terminal_id`, when given, is validated with `TerminalRepository.get(tenant_id, terminal_id)`; not found → 422 `unknown_terminal`, as `/cost-basis` does. Resolver reads are tenant-scoped anyway, so this prevents a confusing result, not a leak.
- With `customer_id`, the price comes from `build_sales_pricing_engine(es, tenant_id).resolve_price(customer_id=..., product_code=..., gallons=float(gallons_decimal), terminal_id=terminal_id or "", route_miles=0.0, effective_date=as_of.date(), market_price_cents=None, account_id=...)`. `resolve_price` takes `gallons: float`; the float is used only for tier selection inside the engine. All margin maths uses the parsed `Decimal` gallons. It uses `effective_price_micros` when present, else `effective_price_cents × 10,000`.
- Preview with `customer_id` shows a tenant `admin` the resolved sell price, which `POST /pricing/resolve` (staff-only) does not expose to every role. That follows from FR6.2 and D1, and it is the same price the admin already sees on that customer's invoices.
- Pricing errors map to the same 422 codes as `/pricing/resolve`.
- With no terminal, the cost is the unattributed tenant-wide cost.
- The response is the candidate (money fields, flags, `cost_snapshot`). Nothing is persisted, and no signal or alert is created (FR5.5).
- If the resolution reports a contract split (`split_gallons_at_market_price > 0`), the preview prices all gallons at the effective price and adds `warnings: ["contract_split_not_modeled"]`.

## Export (FR7, AC-37)

`GET /records/export` is decorated with `@limiter.limit(EXPORT_RATE_LIMIT, key_func=export_rate_key)` and guarded by `export_guard("admin", base=require_margin_enabled)`. It builds a `KeysetSource` over `repo.list_records(tenant_id, filters, after, size, with_total)`, keyed on (`as_of`, `record_id`), with `count=repo.count_records(...)`. It calls `stream_csv_export(export_type="margin", ...)`, which produces the filename `margin_<tenant>_<YYYYMMDD>.csv`.

Columns, in order:

- Identifiers: `record_id, stage, status, origin, version, as_of, order_id, invoice_id, line_index, customer_id, account_id, product_code, terminal_id`.
- Quantities and money: `gallons` (6-dp decimal string), `unit_price_micros, unit_price_usd, revenue_cents, revenue_usd, method, no_cost_reason, product_cost_micros, adders_micros, landed_cost_micros, landed_cost_usd, cost_cents, cost_usd, margin_cents, margin_usd, margin_per_gallon_micros, margin_per_gallon_usd, margin_pct`.
- Flags and settings: `flags` (`; `-joined), `floor_micros_used, adders_configured, terminal_unattributed, computed_at`.

USD columns are `Decimal` values, which `escape_cell` writes unprefixed. The export has no contact fields.

Missing cost in the CSV: for `method=none` every cost-derived cell (`product_cost_micros`, `landed_cost_*`, `cost_*`, `margin_*`, `margin_pct`) is an empty cell (`escape_cell(None)`), never `0`. `method` and `no_cost_reason` sit immediately after `revenue_usd`, so the reason is next to the gap. An empty cell is the honest representation; a spreadsheet that `SUM`s the column is summing costed records only, which matches the summary's `cost_cents` definition. A contract test asserts a `none` row has empty cost cells and `method=none` in the column after `revenue_usd`.

## Admin UI (FR6.3, AC-32)

- **Gating.** `modules.ts` gets `{ id: "margin", tier: 2, requiredRoles: ["admin"], note: "Cost and margin; tenant admin only (margin-feed D1)." }`. `CommerceHub.tsx` adds tab `margin` ("Margin", `TrendingUp` icon), lazy-loading `MarginHub`. It is gated through the existing `canSee(id, { roles })`, so a dispatcher never sees the tab and `/dashboard/billing?tab=margin` falls back to the default tab.
- **`MarginHub`.** An accessible sub-tab list (`role="tablist"`, arrow-key navigation following the existing hub tab pattern): Records, Summary, Alerts, Cost basis, Cost entries, Settings.
- **Records.** Filter form (date range, customer, product, terminal, stage, flag, status), a table with flag badges (text and colour, never colour only), keyset "Load more", a row detail drawer with the snapshot and version history, and `ExportCsvButton type="margin" allowedRoles={["admin"]} subject="margin records"`.
- **Summary.** Grouping select and totals table.
- **Alerts.** A list grouped by `order_id` (alerts without an order, such as digests, are listed under "Other"), with acknowledge, approve and dismiss buttons. The Margin tab label carries the open-alert count badge, which has an `aria-label`, for example "3 open margin alerts".
- **Cost basis.** Product, terminal and as-of form. Shows the method, lots table and exclusion counts.
- **Cost entries.** List with status filter, an add form (`CostEntryForm`), supersede (same form prefilled plus reason) and void (reason dialog).
- **CSV import.** `CostImportDialog` uploads with `dry_run=true`, shows per-row errors and duplicates, then "Import N rows" with `dry_run=false`.
- **Settings.** `MarginSettingsForm`.
- **Recompute** (addition to FR6.3; recompute is the only backfill and late-BOL path, so admins need it outside curl). `MarginRecomputePanel.tsx` under the Settings sub-tab: two date inputs labelled "Sale date (as of) from" / "to", stage checkboxes (invoice, delivery), an "Only records with no cost" checkbox (default on), a required reason, and a submit button. Help text: "Uses the sale date shown in Records. Invoices created more than 15 days after the end date, or orders delivered more than 30 days after creation, are not reached." After 202 it polls `GET /recompute/{run_id}` every 5 s until `completed` or `failed` and shows the counts in `role="status"`; 409 shows "A recompute is already running" in `role="alert"`.

Formatting lives in `marginFormat.ts`. It does integer-only formatting from cents and micros, with no float division (`Math.trunc` plus string padding). Null handling is explicit, so a missing cost can never render as `$0.00` (`Math.trunc(null)` is `0`):

- `formatCents(v: number | null)`, `formatMicros(v: number | null)` and `formatPct(v: string | null)` return the literal `"No cost"` for `null` and throw `TypeError` on `undefined` (a missing field is a bug, not a missing cost). Only finite integers are formatted; anything else throws.
- Records rows and the detail drawer render `method=none` with a "Missing cost" badge (text plus colour) and the `no_cost_reason` text, in place of the cost, margin and percentage cells.
- The Summary panel labels revenue as "Revenue (costed)" and "Revenue (no cost)", and shows `skipped_sources.count` as "Sources not computed" when it is above 0.

Accessibility: native `<input>`, `<select>` and `<button>` elements; every control has a `<label>`; errors render in `role="alert"`; and success messages use `role="status"`. These follow the `ExportCsvButton` conventions.

`exportApi.ts` adds `"margin"` to `ExportType` and `margin: "/commerce/margin/records/export"` to `EXPORT_PATHS`. `marginApi.ts` uses `fetchWithSession`, as `commerceApi.ts` does.

## Non-exposure (FR8, AC-31)

Structural controls:

1. Margin data lives only in the seven tables, read only through `margin_repository.py`.
2. A unit test parses imports with `ast` across `Runsheet-backend/` and asserts only these modules import `margin_repository` or the margin ORM classes: `commerce/services/margin_*`, `commerce/api/margin_endpoints.py`, `commerce/hooks/margin_order_subscriber.py`, `Agents/overlay/revenue_guard.py`, `bootstrap/*`, `persistence/models.py`, `alembic/*` and tests.
3. The tables are not in `es_documents`, so `search_documents`, the agent search tools, `persistence/rebuild_document_store` and outbox projections cannot reach them.
4. Signals, proposals and activity entries carry ids and flag names only.
5. Invoice and order docs are never mutated by the margin code. The hook receives the doc, and `compute` reads from a `copy.deepcopy` of it.

A field-scan test checks every existing surface for the forbidden keys, including on a tenant that has margin records:

- The surfaces: order, invoice, BOL and job GET and list responses, the invoice export CSV header, the `InvoiceWSManager` payload built by `_broadcast_invoice_ws`, and driver-app order payloads. Two more need explicit seams:
  - `_build_qbo_push_payload(invoice)` in `commerce/services/commerce_external_sync.py`, called directly on an invoice that has margin records.
  - The `notification` dict passed to a channel `dispatch(notification)`. A capturing fake dispatcher is registered and `NotificationService` sends an invoice-related event through `_render_by_event_and_channel`.
- The forbidden keys are derived, not hand-listed: the column names of `MarginRecordORM` and `MarginCostEntryORM` (read from `__table__.columns`), plus the export's derived names (`cost_usd`, `margin_usd`, `landed_cost_usd`, `margin_per_gallon_usd`, `margin_pct`; the generic `flags` column name is not included because existing docs may use it), minus the shared ids and fields that legitimately appear on existing surfaces: `tenant_id`, `order_id`, `invoice_id`, `customer_id`, `account_id`, `product_code`, `terminal_id`, `unit_price_micros`, `revenue_cents`, `status`, `version`, `line_index`, `line_id`, `created_at`, `created_by`, `notes`, `reference`, `supplier_name`, `bol_id`, `effective_at`, `effective_to`, `source`, `stage`. The test asserts the resulting set includes at least `unit_cost_micros`, `cost_cents`, `margin_cents`, `landed_cost_micros`, `floor_micros_used`, `flag_negative_margin`, `flag_missing_cost`, `flag_below_floor` and `cost_snapshot`, so a future column rename cannot silently empty it.

## Configuration

- `config/settings.py`: `commerce_margin_feed_enabled: bool = Field(default=False, description=...)`. Every margin path also requires `commerce_backbone_enabled` and an active persistence layer, which is mandatory in staging and production already.
- Job intervals are module constants in `margin_jobs.py`: `MARGIN_GAP_SWEEP_INTERVAL_SECONDS = 21_600` and `MARGIN_WEEKLY_REPORT_INTERVAL_SECONDS = 86_400`. `MARGIN_HOOK_MAX_PENDING_TASKS = 2_000` is a module constant in `margin_service.py`.
- Defaults for window, staleness and floor are constants in `commerce/models/margin.py`.

## Error handling summary

| Operation | Failure | Recoverable? | Caller receives | Log |
|---|---|---|---|---|
| Hook scheduling | Any exception | Yes (gap sweep) | Nothing; the business call continues | ERROR |
| Hook scheduling | ≥ 2,000 pending margin tasks | Yes (gap sweep for delivery/invoice; estimate dropped) | Nothing; the business call continues | WARNING (ids) |
| `ensure_activated` | DB exception | Yes (next hook or sweep pass) | Hook: write proceeds. Sweep: that tenant's pass is skipped this cycle (no watermark, no safe lower bound) | ERROR (tenant) |
| Input extraction (phase 1) | Malformed gallons, price or subtotal | Fix the source doc | No record; the key is counted in `margin_skips` | ERROR once per live event; one WARNING with the count per sweep or recompute cycle |
| Record compute (phase 2) | Resolver/reader exception | Yes | A `computation_error` record is written | ERROR (ids, error type) |
| Rack cap exceeded | > 10,000 rack rows for (terminal, product, window) | Admin narrows `wac_window_days` | `none` / `computation_error` record, `rack_cap_exceeded` | ERROR |
| Record write | DB unavailable / second `IntegrityError` | Yes (gap sweep) | None (background) | ERROR |
| BOL scan cap exceeded | > `bol_scan_cap` (5,000) BOL docs of **any** product at the terminal (or tenant-wide when unattributed) in the window | Admin narrows `wac_window_days` (`PUT /settings` warns above 30 days) | `none` / `computation_error` record for every product resolved against that read, `lot_cap_exceeded`, `bols_scanned` | ERROR (tenant, terminal or `*`, window) |
| Phase-1 skip persisted | `margin_skipped_sources` upsert fails | Yes (next event or sweep) | Nothing; the business call continues | WARNING |
| Alert acknowledge / approve / dismiss | Not found / wrong state | Re-read | 404 / 409 | INFO; success is audit-logged |
| Signal publish | Bus exception / no bus | Yes, the alert comes from `alert_state` | None | WARNING / INFO once |
| RevenueGuard queue query | DB exception | Next cycle | Cycle returns `([], [])` | ERROR |
| RevenueGuard mode read / tenant pass | Exception for one tenant | Next cycle (rows stay `pending`) | Other tenants continue | ERROR (tenant) |
| RevenueGuard per-record processing / alert insert | DB exception | Next cycle (row stays `pending`) | Other records continue | ERROR (tenant, record id, error type) |
| Pending row older than 7 days | Tenant disabled, or a persistent failure | n/a | Set `expired`, no alert | INFO with a count |
| Alert insert duplicate | `IntegrityError` on dedupe | n/a | No-op | DEBUG |
| Cost entry create/supersede/void | Validation | Fix input | 422 field errors | WARNING (request validation handler) |
| | Duplicate / not active / race | Re-read | 409 | INFO |
| | Entry not in tenant | n/a | 404 | INFO |
| Import | > 5 MB / > 10k rows | Split file | 413 `MARGIN_IMPORT_TOO_LARGE` | INFO |
| | Decode/header/row errors | Fix file | 422 with per-row errors | INFO |
| Audit sink | Exception | n/a | Request succeeds | WARNING |
| Cost-basis GET | Reader exception | Retry | 503 | ERROR |
| Preview | Pricing errors | Fix input | 422 (pricing codes) | INFO |
| Recompute | Already running | Wait | 409 `MARGIN_RECOMPUTE_RUNNING` | INFO |
| | Range > 92 days / bad dates | Fix input | 422 | INFO |
| | Crash mid-run | Rerun (idempotent) | Run `failed`; stale after 1 h | ERROR |
| Export | > 50k rows | Narrow filters | 413 before the first byte | audit line |
| Any endpoint | Flag off / persistence dormant | n/a | 404 `COMMERCE_DISABLED` | DEBUG |
| | Not `admin` | n/a | 403 `INSUFFICIENT_ROLE` | existing |
| Jobs | One tenant fails | Next cycle | Others proceed | ERROR per tenant |

## Invariants and their owners

| Invariant | Enforced by | Why there |
|---|---|---|
| Missing cost is never 0 | Resolver (`none` ⇒ null), plus the DB CHECK `ck_mr_cost_null_iff_none` (every cost-derived column) and `ck_mr_missing_flag` | The DB is the last line. No code path can store a zero placeholder. |
| One active record per (stage, source) | DB partial unique `uq_mr_active`, plus the write-protocol lock | Works across processes. |
| Frozen and void records change only through recompute | `write_record` rule table | The single write path. |
| Tenant isolation | `MarginRepository` (mandatory `tenant_id` on every method), resolver readers (`inject_tenant_filter` / tenant-scoped repos), `TenantContext`-only tenant | The data layer, so no handler can forget it. |
| Admin only | Router-level `require_margin_admin` | Routes added later inherit it. |
| No float money | `services/money.py` helpers, `margin.py` parsers, grep test | One parsing layer. |
| Entries are immutable | Repository exposes only insert and status transitions | No update method exists to misuse. |
| One active price per BOL, no duplicate entries | DB partial uniques `uq_mce_active_bol` and `uq_mce_active_natural_key` | Race-proof. |
| Flags have one owner | `compute_flags` in `margin_service`. RevenueGuard reads stored booleans. | D15. |
| One alert per record | DB unique `uq_malert_dedupe` | Replays and multiple agents are safe. |
| Every flagged live record is evaluated by RevenueGuard, whichever task wrote it | `write_record` sets `alert_state='pending'` in the record insert. The leader-only RevenueGuard drains it. | Independent of the in-process bus and of process restarts. |
| A rack or contract price ≤ 0 never becomes a cost | Resolver (`select_rack`, contract step) | Only explicit entries may be zero (FR2.6). |
| Resolver results are deterministic | `select_rack` total order, sorted reads, caps that error rather than truncate | `input_hash` stability, so no version churn. |
| A past lot's contract price does not change when an admin toggles the contract | Resolver contract step (dates govern, `status` ignored) | The toggle has no history; dates do. |
| RevenueGuard never processes a `disabled` tenant or mixes tenants in one pass | RevenueGuard's own `monitor_cycle` (one mode read per tenant, explicit `tenant_id` into every repository call) | The base cycle cannot pass a tenant to `evaluate`. |
| No automatic backfill: a source from before activation never gets a live (alerting) record from the sweep | `run_margin_gap_sweep_cycle` lower bounds and event-time filter against `margin_settings.feed_activated_at`, set once by `ensure_activated` | The sweep is the only automatic writer that looks backwards; hooks only see post-enable events. |
| Recompute covers exactly the `as_of` range the admin sees in Records | Recompute's widened enumeration plus the phase-1 `as_of` filter | One date axis for every admin surface. |
| A missing cost never displays or exports as 0 | `marginFormat` (null → "No cost", undefined throws), CSV empty cells, summary revenue split | The storage invariant has to survive rendering. |

## Simplifications (pre-frozen to keep review loops convergent)

These are fixed choices. Each one removes a class of edge cases the reviewers would otherwise keep raising.

1. **Write serialization.** All record writes go through one locked read-decide-write with the rule table above, with no in-process locks or ordering assumptions. Tests: the rule table is parameterized over every (latest state × mode) cell on SQLite, plus a Postgres test with two concurrent writers for one key that ends with exactly one active row.
2. **Leakage stage.** Leakage counts one stage only (`invoice` if invoicing is on, else `delivery`). Test: a delivery and an invoice for the same order count once.
3. **Summary range cap.** Summaries are capped at 92 days and aggregated in Python. Test: totals equal the sum of the listed records, and 93 days returns 422.
4. **Preview and contract splits.** Preview does not model contract splits and returns a warning instead. Test: a resolver fake with split fields yields `contract_split_not_modeled`.
5. **Read-time lot assembly.** Lots are assembled from BOLs at read time, never copied into margin tables. BOL confirmation or terminal fixes therefore show up on the next computation without a sync job, and frozen records keep their snapshot. Test: confirming a BOL changes `/cost-basis` but not a frozen record.
6. **Lot cap.** A 5,000-BOL cap yields `computation_error` rather than a partial WAC. Test: a fake reader returning 5,001 docs.
7. **Alerts come from the database, not the bus (H1).** `alert_state` and `digest_state` are the only alert work queue. The bus signal is a hint, and RevenueGuard discards it. Tests: a flagged record written with no bus wired, then one leader cycle, gives exactly one alert. `_on_signal` on a non-leader instance leaves `_signal_buffer` empty. A second cycle creates no second alert. A `disabled` tenant's rows stay `pending` and become `expired` after 7 days with no alert.
8. **Cost-basis reads (pass 1: H2, M1, M3, M4, M6; pass 2: M1, M2). Closed.** Seven review findings across two passes landed in one area: rack truncation, zero prices, ambiguous rack rows, contract inference, read amplification, the all-product BOL read and contract status. The class is "the resolver silently picks or drops data". The fix is one rule set. Pass 2's two MEDIUMs were answered inside it (no new mechanism). Per the workspace convergence rule, this area is now closed: a further MEDIUM here is answered by pointing to these rules or backlogged, not by extending them.
   - every rack read is per (terminal, product) and sorted; the BOL read is per (terminal or `*`, window), covers all products, is sorted, and matches product in Python;
   - every cap errors, never truncates;
   - every pick goes through one total order (`select_rack`);
   - only explicit entries may be zero;
   - "linked" means a stored id, never a name match, and stored dates govern, never a current-state toggle;
   - batching and `ReaderCache` are the only performance tools.

   Tests: 2,500 rack rows for one product over the window, where the earliest BOL is still priced. 10,001 rows gives `computation_error` with `rack_cap_exceeded`. Two rows at the same `effective_at` (unbranded 2.40 and 2.45) give 2.45 whatever the store order. A brand match beats a higher unbranded row. A contract at 0.0 plus rack 2.50 gives a lot at 2.50. A zero-only rack gives `none` / `no_lots_no_rack`. A BOL with no plan contract, from a supplier holding a contract, is priced at rack. A counting fake reader shows 1,000 BOLs and 2,000 rack rows within 28 queries, and a warm-cache second source within 3. Pass-2 additions: 4,000 BOLs of product X plus 1,500 of product Y at one terminal in the window gives `lot_cap_exceeded` for both products; two products at one terminal resolved with one cache issue one BOL read; an unattributed sale with BOLs at two terminals issues one rack read per (terminal, product); a linked contract set `inactive` after the lift still prices the lot (snapshot `contract_status_at_compute="inactive"`); a lift outside the contract dates falls to rack; `PUT /settings` with `wac_window_days=31` succeeds with the scan-cap warning.
9. **Two-phase compute (M7).** Invalid inputs never produce a record, and only resolver failures produce `computation_error`. Tests: a line with `quantity_gallons="abc"` produces no record and one ERROR. Running the sweep twice over it produces two WARNING counts, no record and no exception.
10. **Gap sweep reconciles state (M5).** The sweep uses the same rule table as live writes, driven by `updated_at`. Tests: a draft record whose finalize task was lost becomes frozen after one sweep. A void invoice whose void task was lost becomes `void`. An invoice created 5 days ago and finalized 1 hour ago is frozen. Every sweep test runs twice, with `commerce_read_from_postgres=False` (ES fake) and `True` (SQLite mirror), so the `updated_from` filter is proven on both read paths (pass-2 M3).
11. **RevenueGuard owns its loop (pass-2 H1).** RevenueGuard overrides `monitor_cycle` completely, never calls the base cycle, reads mode once per tenant, and passes `tenant_id` explicitly to `_evaluate_tenant`. `evaluate()` is a stub. No other overlay agent changes. Tests: two tenants, A `disabled` and B `active_gated`, each with a pending negative record → one alert for B, A's row still `pending`, and a recording fake repository shows no call carrying A's id after its mode read; `evaluate([])` returns `[]` and touches nothing; a tenant whose mode read raises is skipped and the next tenant is processed; a `shadow` tenant writes activity entries and no alerts.
12. **Missing cost is rendered, never coerced (pass-2 M4).** Storage nulls stay null through every layer: `marginFormat` maps null to "No cost" and throws on undefined; the CSV writes empty cells with `method`/`no_cost_reason` beside `revenue_usd`; the summary splits revenue into costed and uncosted. Tests: Jest — `formatCents(null) === "No cost"`, `formatCents(undefined)` throws, a `method=none` row renders the "Missing cost" badge and never the text `$0.00`; API — `revenue_cents_with_cost + revenue_cents_missing_cost == revenue_cents` per group and overall; CSV — a `none` row has empty cost cells.
13. **Source selection for sweep and recompute (pass 1: M5; pass 2: M3; pass 3: M2, M3). Closed.** Three passes have raised MEDIUMs about which sources the background writers pick (`updated_at` vs `created_at`, the first-enable backfill, the `created_at` vs `as_of` axis). The class is "a background writer selects sources on a different basis from what the hook or the admin sees". The fix is two rules, with no new mechanism beyond one nullable column:
   - the sweep only creates records for sources whose hook-equivalent event time is at or after `feed_activated_at`; everything older is recompute-only;
   - recompute's range is the half-open `as_of` range `[start 00:00, (end + 1 day) 00:00)` in the settings timezone, the same axis as Records; enumeration is widened by fixed margins and filtered on phase-1 `as_of`. Orders use `start_date=(start_utc_date − 31 days).isoformat()` and `end_date=(end + 2 days).isoformat()` as `YYYY-MM-DD` strings (the repository's bare-date bound stops at 00:00 UTC of `end_date`, so `end + 2 days` is the smallest bound that covers the whole local end day at every IANA offset); invoices use `created_from = start − 14 d`, `created_before = min(now, end + 15 d)`. The query bounds never decide membership; phase-1 `as_of` does.

   Further MEDIUMs about source selection are answered against these two rules or backlogged (for example, sources beyond the fixed margins), not by extending them.

   Tests: (a) 10 delivered orders and 10 invoices created before the flag is enabled → the first sweep writes no record, creates no alert, and sets `feed_activated_at`; (b) an order delivered after activation whose margin task was lost is repaired by the next sweep and produces exactly one alert; (c) flag off → on keeps the original `feed_activated_at`, and an order delivered during the off-window within 14 days is repaired with an alert; (d) a pre-activation invoice whose `updated_at` moves after activation (payment update) gets no record from the sweep; a pre-activation draft finalized after activation does; (e) two concurrent `ensure_activated` calls for a new tenant return the same value (Postgres); (f) recompute over `[day 0, day 6]`: an order created on day −3 and delivered on day +1 is recomputed, one delivered on day −1 is not and is counted in `out_of_range`; an invoice created on day 9 for a delivery on day 5 is recomputed; (g) a recompute whose range crosses a DST change in `America/Chicago`, run through the real `FuelOrderRepository.search` filter on both read paths (`commerce_read_from_postgres=False` with the ES fake honouring `lte` on a bare date as `00:00:00`, and `True` with the SQLite mirror's lexical compare), the same dual-path pattern as Simplification 10: order A created and delivered at 10:00 UTC on `end` is recomputed; order B created at 23:00 Chicago on `end` (the next UTC date) and delivered at 23:30 Chicago on `end` is recomputed; order C delivered at 00:30 Chicago on `end + 1` is enumerated, not written, and counted in `counts.out_of_range`. See the Design freeze section.

## Testing

Unit tests (`tests/unit/commerce/margin/`, fakes for the readers, SQLite where the repository is needed):

| Area | Tests | ACs |
|---|---|---|
| Parsers | Hypothesis: valid ≤ 6 dp strings and ints round-trip exactly. `"2.5000000"` passes as 2,500,000. `"2.0000001"`, negative, `"1e2"`, `"NaN"` and over-max values return 422 with no rounding. A JSON float (`2.5`) returns 422 `invalid_decimal`. The same matrix runs for gallons (3 dp), preview and settings fields. | 19, 21 |
| Resolver | One test per AC for override precedence, the WAC example, window bounds (exclusive start, inclusive end, after as-of excluded), BOL-referenced entry (no double gallons), contract-linked via `plan.contract_id`, BOL with no plan contract → rack at lift, zero-price contract → rack, rack-at-lift staleness, each exclusion reason counted, rack fallback, stale rack (including the probe query), no data, explicit zero, adders precedence and `adders_configured`, unattributed (no rack, tenant-wide only), superseded entries ignored, lot cap, plus the freeze-8 tests. `select_rack` gets a Hypothesis test: a permutation of the input never changes the pick. | 1–12 |
| Read budget | Counting fake reader: query budget for 1,000 BOLs, warm-cache reuse, live hook path uses no cache. | NFR2 |
| Money | AC-20 example. Hypothesis over gallons (0.001–100,000) and micros: `margin_cents == revenue − cost`, and summary totals equal the record sums. `margin_pct` null cases and 2 dp. Extreme bound: price 1 micro, 1,000,000 gal, cost 100,000,000 micros persists, with `margin_bp` beyond 32 bits (also run on Postgres). | 19, 20, 22, 23 |
| Flags | Boundaries at floor − 1, floor, 0 and −1. Per-product floor beats tenant floor. | 38, 39, 41 |
| Write protocol | Parameterized rule table (including frozen and same-hash recompute → skip), idempotent replay, retry on `IntegrityError`, `alert_state` set on insert for exactly the flagged live delivery and invoice rows, and `pending` → `done` on void and supersede. Void with a latest row never calls the resolver (a raising resolver fake leaves the row `void` with its cost fields and no ERROR). An insert deletes the key's `margin_skipped_sources` row. | 15, 17 |
| Skipped sources | An `invalid_inputs` line upserts one `margin_skipped_sources` row (twice → `seen_count=2`), `GET /summary` reports it in `skipped_sources`, and fixing the line then re-running the sweep clears it. | 16 (deviation) |
| Rounding | `margin_bp` at ±12.5 bp → ±13. | 23 |
| Weekly report | Flag off or persistence off → the job returns without a query. | 28 |
| Hook isolation | `generate_from_order` with a margin service whose phase 2 raises: the invoice is returned, a `computation_error` record is written, an ERROR is logged. A hook whose scheduling raises: the invoice is still returned. Mutating the doc after the hook returns does not change the computed record (deepcopy at call time). | 16 |
| Two-phase compute | Freeze-9 tests. | 16 |
| Gap sweep | Freeze-10 and freeze-13 (a)–(d) tests. `InvoiceService.list(updated_from=...)` filter unit test. Cancelled order with an active estimate → estimate voided. `ensure_activated` raising → that tenant's pass is skipped and the next tenant runs. | 15, 16, FR3.5 |
| Hook backpressure | With 2,000 pending tasks, a further hook call schedules nothing, logs one WARNING with ids and returns; a later sweep repairs the delivery key. | NFR2 |
| Settings failure | Phase-2 settings load raising → `computation_error` record with `floor_micros_used=100_000` and `cost_snapshot.settings_unavailable=true`. | 16 |
| Invoice stage | A resolver fake that changes the line price yields margin on the resolved price. With OI-14 merged, a price-protection contract split yields two records from the persisted split lines (re-run gate). | 13, 14 |
| Recompute | Range 92/93 days, `only_missing` semantics, superseded versions, audit lines, one digest signal. Freeze-13 (f)–(g) `as_of` boundary tests and `counts.out_of_range`. A run with no flagged records ends with `digest_state='done'`, no digest alert and no digest signal. | 18, 44 |
| Cost entries / import | Field validation matrix, cross-tenant terminal and BOL → 422, supersede/void state machine, dry-run writes nothing, any invalid row → nothing written, 5 MB / 10k rows → 413, duplicate reporting, audit lines with before/after. A counting fake shows a 10,000-row import with 5 distinct terminals and 3,000 BOL ids issues 5 terminal reads and 3 BOL queries. | 26, 33–36 |
| RevenueGuard | Subscriptions exact. `cooldown_minutes == 60`. No `jobs_current` access (a fake ES fails on any call). Queue-driven: negative → one high alert, a second cycle → still one. Missing → medium. Below floor → no alert. 3 consecutive → one proposal; cooldown and pending-review dedupe. The M10 origin tests. Shadow → activity entries only, `agent_shadow_proposals` never indexed, rows set `done`. Disabled → no repository call for that tenant after its mode read, rows stay `pending`, then `expired` after 7 days. The freeze-7 and freeze-11 tests. Leakage groups by sale: one invoice with two OI-14 split lines (one below floor) counts as one below-floor sale, and three such invoices trigger exactly one proposal. The proposal carries `source_agent`. Approve, dismiss and acknowledge each emit one `margin_alert_resolved` audit event. Digest: a completed run → one `recompute_digest` alert. One failing record does not block the others. Two tenants: A's below-floor records never count for B. Proposal and activity payloads contain no money keys. A published `margin.review.*` `PolicyChangeProposal` reaching `LearningPolicyAgent` (which subscribes to every `PolicyChangeProposal`) produces no `_log_experiment` call. | 27, 38–41, 43, 44, 45 |
| Weekly report | Content fields and the stage preference, `missing_cost_share_bp` by record count, one row per tenant per week, rerun no-op. | 46 |
| Non-exposure | Import-graph test and field-scan test. | 31 |

API tests (`tests/integration/commerce/test_margin_endpoints.py`, TestClient with dependency overrides, SQLite):

- Flag off → 404 on every route, including the export, for staff roles (admin, dispatcher, driver), and no records are written when hooks fire (AC-28). Customer sessions are excluded from this matrix: the portal's central deny returns 403 first.
- Admin → 200 (AC-29). Dispatcher, driver and `platform_admin` alone → 403 (AC-30). The customer case is built with `configure_session_verifier` (not `override_auth`), as the portal design requires, once portal code is on the branch; before then it is built with a session carrying only the `customer` role and expects the router's 403.
- Route order: `GET /records/export` reaches the export handler (limiter and `export_guard` applied), not `/records/{record_id}`.
- Summary: `revenue_cents_with_cost + revenue_cents_missing_cost == revenue_cents`; `skipped_sources` present.
- `tenant_id` in a body → 422. A `tenant_id` query parameter is ignored (AC-25).
- Two tenants with identical terminal and product ids: every list, summary, export, preview, recompute and cost-basis call stays in its own tenant (AC-24, AC-25).
- Preview validation (AC-42), including `unit_price_usd` over 100.000000 → 422 and an unknown or other-tenant `terminal_id` → 422 `unknown_terminal`.
- The export passes the shared `csv_export` contract tests (BOM, injection, 50k cap, rate limit, audit) by adding `"margin"` to their parameter list (AC-37).

Postgres tests (`tests/postgres/test_margin_concurrency.py`):

- migration upgrade and downgrade round-trip;
- concurrent writers;
- the partial-unique races for entries and runs;
- concurrent `ensure_activated` for a tenant with no settings row (freeze-13 (e));
- the CHECK constraints: an insert with `method='none', cost_cents=0` fails, and so do the same with `margin_cents=0`, `product_cost_micros=0` and `margin_per_gallon_micros=0`;
- the `margin_bp` extreme-bound insert;
- `ix_mr_alert_pending` used by the queue query (`EXPLAIN` contains the index name).

Frontend (Jest):

- `CommerceHub` hides the Margin tab for a dispatcher and shows it for an admin (AC-32).
- `CostImportDialog` shows the dry-run errors and then imports.
- `ExportCsvButton` renders with type `margin`.
- `marginFormat` formats with integers only, plus the freeze-12 null tests.
- Summary panel shows "Revenue (costed)" and "Revenue (no cost)".
- Records filters map to query params.
- `MarginRecomputePanel`: date fields labelled "Sale date (as of) from/to", reason required, 409 renders in `role="alert"`, completed counts render in `role="status"`.

## Rollout and staging verification

1. Start from the fast-forwarded base (`a99bfd9`, which carries OI-14 as `b6428e2`), implement, and get CI green on the exact commit. Deploy to staging with `COMMERCE_MARGIN_FEED_ENABLED=false`, through whichever path is on `go-live-blockers` at that time: CodeBuild once the other agent's integration has landed, otherwise `staging_aws.sh`. Deploy from a pinned worktree, with the live-UI ancestor check. Migration `0011` runs as part of the deploy.
2. Enable the flag in staging. In `demo-tenant`, create `QA-` fixtures: a `QA-` customer and order, `QA-` cost entries, and a BOL whose reference carries `QA-`. Set RevenueGuard to `shadow`, then `active_gated`, recording the baseline first and restoring it afterwards. Verify alerts, the export and the dispatcher 403. Count `terminal_bols` per terminal over the last 30 days in `demo-tenant` (read-only) and record it against `bol_scan_cap`. Count `demo-tenant` invoices in ES (`invoices_current`) versus Postgres (read-only) and record the difference; that difference is invoices the sweep and recompute cannot see on the Postgres read path. After enabling, confirm the first sweep writes no records and no alerts for pre-existing deliveries (`feed_activated_at` set; no `origin=live` record whose source event precedes it. Records for post-enable events, including the other agent's `QA-SWEEP-*` orders, are expected), then run a recompute over the QA order's sale date and confirm one digest at most.
3. Run isolation checks with `qa-tenant-b` under the owner's 2026-10-07 exception.
4. Delete the fixtures with dry run → delete → verify. Leave `QA-SWEEP-*` records untouched.

## Assumptions added by this design

- `order.dispatched` is a reliable estimate trigger. Orders that skip it get no estimate.
- `RackPriceSyncService` persists canonical `product_code`s. Manually uploaded rows may use aliases, so the rack read filters on the alias set and then re-canonicalizes in Python. A manual row whose code differs from every catalog alias in more than case (for example with stray whitespace) is not found, and so it is never logged either. That is accepted.
- Unverified: whether the OPIS feed fills `supplier_brand` and `branded_flag` consistently. If `supplier_brand` is empty, the brand-match tier never fires, and selection falls to `unbranded_max` / `branded_max`. That is still deterministic and conservative. The staging check in Rollout step 2 inspects real `rack_prices` rows for `demo-tenant`.
- Customer-portal sessions follow the portal design (`77449ed`): central default-deny 403 `PORTAL_ROUTE_FORBIDDEN` before routing. Margin routes get 403 for them in every flag state.
- **Deliberate deviation from FR3.4 / AC-16 (literal).** A phase-1 `invalid_inputs` skip writes no margin record, because a record cannot hold inputs that do not parse (the CHECKs and non-null gallons/price columns would reject it). The gap is made visible instead through `margin_skipped_sources` and `GET /summary.skipped_sources`, plus an ERROR log. Phase-2 failures (the case AC-16 tests) still write `computation_error` records exactly as required.
- Unverified: that every Postgres-mirror invoice write moves `updated_at` (ORM update with `onupdate`). The dual-path sweep test covers it; a Core update that misses it would show as a failing test, fixed by setting `updated_at` explicitly in that write.
- Unverified: BOL volume per terminal per 30 days in staging and real tenants, which decides how often `bol_scan_cap` fires. Rollout step 2 counts it.
- `mirror_invoice_create` writes nothing when the invoice's parent customer or account is not yet mirrored (`commerce_persistence_bridge.py`). With `COMMERCE_READ_FROM_POSTGRES=true` (staging), `InvoiceService.list` never returns those invoices, so the sweep and recompute cannot see them. The live hook still covers them, because it reads the ES doc. Fixing the mirror is out of scope; Rollout step 2 measures the gap.
- Recompute does not reach invoices created more than 15 days after the end date, or orders delivered more than 30 days after creation (Simplification 13). Sources outside those margins need a wider or later recompute range. Accepted, since invoices are generated by the `order.delivered` subscriber within minutes.
- A post-enable event whose live margin task is lost (rolling deploy, or the 2,000-task backpressure skip) before the tenant's first `ensure_activated` call can fall before the watermark the first sweep then sets. The sweep treats it as pre-activation and never repairs it; such events are recompute-only. Accepted: the window is at most one sweep interval (6 h) after enable, and recompute reaches them.
- Pre-activation deliveries and invoices get margin only through recompute. Turning the flag off and on again does not move the watermark, so off-window events within the sweep lookbacks alert normally.
- Leakage counts one sale per `invoice_id` (invoice stage) or `order_id` (delivery stage), so an OI-14 split does not count twice.
- Contract `effective_from` and `effective_to` are compared against the lift date in the tenant's timezone.
- The approval queue cannot carry margin proposals, because it is dispatcher-visible. Admin approval therefore happens in the Margin alerts list. That is a functional "human approval" without an approval-queue entry.

## Review responses (pass 1 → revision 2)

Every finding is addressed. None is backlogged or ignored.

| Finding | Response |
|---|---|
| H1 | Addressed with a freeze (Simplification 7). `margin_records.alert_state` and `margin_recompute_runs.digest_state` are the work queue. RevenueGuard prefetches pending tenants in an async `monitor_cycle` wrapper, which feeds the synchronous `_pending_work_tenants()`. It drains up to 500 rows per tenant per cycle, and `_on_signal` discards signals so no buffer grows. FR5.2's signal is still published, as a hint. A 7-day `expired` state bounds the queue while a tenant is disabled. |
| H2 | Addressed. The rack read is per (tenant, terminal, canonical product) with the alias set (new `aliases_for`), paged with `search_after` 500 at a time, capped at 10,000, and the cap yields `computation_error` + `rack_cap_exceeded`. Folded into Simplification 8. |
| M1 | Addressed. A rack or contract price ≤ 0 is absent everywhere, counted in `zero_price_ignored`. Only entries may be zero. |
| M2 | Addressed. The CHECK covers every cost-derived column. `margin_bp` is one-way (it is null on zero revenue). Postgres tests per column. |
| M3 | Addressed. `select_rack`: brand match, then unbranded, then branded; latest `effective_at`; highest price; `rack_price_id` asc. `rack_selection` is recorded. A permutation property test. |
| M4 | Addressed. The supplier-name contract step is deleted. "Linked" means `plan.contract_id` only. Tests updated. |
| M5 | Addressed. The sweep enumerates invoices by `updated_at` (new additive `updated_from` filter) and reconciles missing, unfrozen and unvoided records through the rule table. Cancelled and failed orders void stray estimates. Simplification 10. |
| M6 | Addressed. Plans and contracts are batched per BOL page with `terms`. `ReaderCache` is per sweep tenant pass and per recompute run. The query budget is stated (≤ 28 for 1,000 BOLs), with a counting-reader test. |
| M7 | Addressed. Two-phase compute: invalid inputs write no record and log ERROR. Only phase-2 failures write `computation_error`. Simplification 9. |
| M8 | Addressed. `margin_bp` is `BigInteger`, with an extreme-bound test on Postgres. |
| M9 | Addressed. `StrictStr`/`StrictInt` only, JSON floats → 422, a regex before `Decimal`, applied to every client money and gallons field. |
| M10 | Addressed with a freeze. The trigger is live-only (only live rows enter the queue), and the window counts the latest N active records of any origin. Both tests specified. |
| M11 | Addressed. `git merge --ff-only 9d94f7d`, with the reconcile steps once owner-decisions lands on `go-live-blockers`. The doc names the commit, not a count. Re-checked 2026-10-07: not landed. |
| N1 | `action` values specified. |
| N2 | `cooldown_minutes=60` kept explicitly, and asserted in a test. |
| N3 | Storage, API and export carry 6 dp. Only the UI rounds to 3 dp. |
| N4 | `warnings: ["gallons_ignored_bol_net_used"]`. |
| N5 | `natural_key` uses UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`. |
| N6 | `_log_shadow_proposal` is overridden to write an activity-log entry. `agent_shadow_proposals` is never written. |
| N7 | Documented. Alerts carry `order_id`, and the UI groups by it. |
| N8 | The deepcopy happens in the hook method, synchronously. |
| N9 | A recompute on a frozen record with an equal hash skips (new rule-table row). |
| N10 | `missing_cost_share_bp` is by record count. The gallons are in `flag_counts`. |
| N11 | HIGH risk is the `leakage_proposal` alert with `severity=high`. Approval is the admin action on it. |
| N12 | Pull-only, stated. A count badge on the Margin tab. |
| N13 | The quantize comparison is the single dp rule. The regex only bounds the length, so trailing zeros pass. |
| Unverified items | QBO and notification seams are now named (`_build_qbo_push_payload`, the channel `dispatch(notification)` dict via `_render_by_event_and_channel`). The `price_protection_expiry_job` tenant discovery was read: it is a `terms` aggregation on `tenant_id`, size 10,000, as assumed. The OPIS brand fields and the portal session are listed under Assumptions. |

Convergence note: five of the eleven MEDIUM findings (and H2) were in cost-basis resolution. Rather than answer each separately on later passes, Simplification 8 freezes one rule set for that area. A future finding there should be checked against those rules, not answered with a new mechanism.

## Review responses (pass 2 → revision 3)

Every finding is addressed. None is backlogged or ignored.

| Finding | Response |
|---|---|
| H1 | Addressed with a freeze (Simplification 11). RevenueGuard overrides `monitor_cycle` completely (no `super()`): prefetch pending tenants, read mode once per tenant, skip `disabled`, `_evaluate_tenant(tenant_id, commit)`, then route through the base `_log_shadow_proposal` / `_route_proposal`. `evaluate()` is a stub. The `_pending_work_tenants` override and `_pending_tenants` field are removed. Per-tenant failures are isolated. Two-tenant disabled/active test and `evaluate([])` test added. |
| M1 | Addressed inside Simplification 8. The BOL read is per (tenant, terminal or `*`, window), all products, matched in Python. The cap is `bol_scan_cap = 5,000` on scanned docs. The `ReaderCache` BOL key drops product. The budget formula counts scanned BOLs. `PUT /settings` warns above 30 days. Error table row and tests added. |
| M2 | Addressed inside Simplification 8. Dates govern a linked contract; `status` is ignored; `contract_status_at_compute` is recorded per lot. Tests added. |
| M3 | Addressed. `updated_from` is added to `InvoiceReadRepository._list_filters`/`list` (Postgres) and passes through the existing `**kwargs` of `read_invoice_list`/`read_invoice_count`. Filter only; paging stays on the (`created_at`, `invoice_id`) keyset. Sweep tests run with read cutover off and on. |
| M4 | Addressed with a freeze (Simplification 12). `marginFormat` null → "No cost", undefined throws; "Missing cost" badge with reason; CSV empty cells with `method`/`no_cost_reason` after `revenue_usd`; summary adds `revenue_cents_with_cost` and `revenue_cents_missing_cost` (also on weekly reports). Jest, API and CSV tests added. |
| N1 | Reconcile table added to the branch plan (OI-19, OI-20 `8b47be4`, OI-57 `dd2c44c`, OI-16 `f569d13`), with the tests to re-run. `--ff-only 9d94f7d` kept. |
| N2 | `source_agent=self.agent_id` and `tenant_id` listed in the proposal fields. |
| N3 | Export route declared before `/records/{record_id}`; route-order test. |
| N4 | Forbidden keys derived from the two ORM tables' columns plus export-derived names, minus shared ids, with a minimum-set assertion. |
| N5 | Weekly report job returns early when the flag or persistence is off; test. |
| N6 | Listed as a deliberate deviation under Assumptions. Made durable and visible with a small `margin_skipped_sources` table (seventh table) and `GET /summary.skipped_sources`, because an in-process counter is per ECS task and lost on restart. |
| N7 | `ROUND_HALF_UP` away from zero stated as intended for negatives; ±12.5 bp test. |
| N8 | Void with a latest row skips phase 2 and transitions directly; only the tombstone cell builds a candidate. Test. |
| N9 | Leakage groups by sale (`invoice_id` / `order_id`); a sale is below floor when any line is. Test. |
| N10 | Customer sessions excluded from the AC-28 404 matrix (portal central 403); AC-30 customer case via `configure_session_verifier`; `0011_customer_portal` collision and the `0012` re-parent spelled out. |
| N11 | Import memoizes terminals and batches BOL ids per 1,000 rows; counting test. |
| N12 | `float(gallons_decimal)` passed to `resolve_price` for tiering only; admin-visible resolved price noted as following from FR6.2/D1. |
| N13 | `line_id` documented as informational; key stays `line_index`. |
| N14 | Alert acknowledge/approve/dismiss audit-logged (`margin_alert_resolved`). |
| N15 | Budget formula sums rack reads over distinct terminals; unattributed rack-at-lift cache key stated. |

Convergence note (pass 2): M1 and M2 were again in the cost-basis area and were answered inside Simplification 8 with no new mechanism, so that freeze is now closed (see its text). H1 and M4 each got their own freeze (11, 12) with required tests. If pass 3 raises MEDIUMs in these areas with no HIGH, they should be backlogged against the frozen rules rather than extend the document.

## Review responses (pass 3 → revision 4)

Every finding is addressed. None is backlogged or ignored. Git facts in M1 were re-checked on 2026-10-07 (`origin/production-readiness/go-live-blockers` = `1ce977b`, contains `b6428e2`; `0f82def` is an ancestor).

| Finding | Response |
|---|---|
| M1 | Addressed. The branch plan now fast-forwards to `origin/production-readiness/go-live-blockers`, which carries OI-14 as `b6428e2` plus the landed OI-16/19/20/57. The reconcile table is replaced by a list of files to edit on top of their landed versions (`ExportType` now includes `driver_hours`/`driver_qualifications`; add `margin`). AC-13/14 run against `b6428e2`. Orphaned SHAs are named as not-to-use. Status line and Rollout step 1 updated. |
| M2 | Addressed with the reviewer's watermark, folded into a new freeze (Simplification 13). `margin_settings.feed_activated_at`, set once by `ensure_activated` (hook path before first write, start of each sweep pass). Sweep lower bounds use it, and a missing key is created only when the source's hook-equivalent event time (`voided_at` / `finalized_at` / `created_at`, or `delivered_at` for orders) is at or after it. One refinement over the review: the invoice check uses that event time, not just `updated_at`, so a payment update on an old invoice cannot create an alerting record. Flag off/on behaviour named. Tests (a)–(e). |
| M3 | Addressed inside Simplification 13. Recompute dates are `as_of` dates in the settings timezone. Orders are enumerated from `start − 30 d` to `end`. For invoices the review proposed `created_before = end + 1 d`; because an invoice's `as_of` (`delivered_at`) precedes its `created_at`, the upper bound is widened instead to `min(now, end + 15 d)`, with `created_from = start − 14 d`. Out-of-range sources are dropped after phase 1 and counted in `counts.out_of_range`. Boundary and DST tests (f)–(g). The design had no recompute UI; a small `MarginRecomputePanel` is added (an addition to FR6.3) with fields labelled "Sale date (as of)". |
| N1 | The OI-02 sentence is replaced: the invoice post-checks never raise today; a future raising change would skip the hook and the sweep repairs the key. |
| N2 | `requirements.md` corrected to AC-13/AC-14, and its merge sentence now points at `b6428e2` on `go-live-blockers`. |
| N3 | Settings-load failure stores `DEFAULT_FLOOR_MICROS` and `cost_snapshot.settings_unavailable=true`. Test added. |
| N4 | Preview `unit_price_usd` uses `max_micros=100_000_000`; `terminal_id` is validated with `TerminalRepository.get` → 422 `unknown_terminal`. API tests added. |
| N5 | Zero-flag runs set `digest_state='done'` and publish no digest signal. Test added. |
| N6 | Listed under Assumptions; Rollout step 2 adds the read-only ES-vs-Postgres invoice count. |
| N7 | `MARGIN_HOOK_MAX_PENDING_TASKS = 2,000`; above it the hook skips with a WARNING and the sweep repairs. Error-table row and test added. |
| N8 | Test added: a `margin.review.*` proposal causes no `LearningPolicyAgent._log_experiment` call. |

Convergence note (pass 3): this pass had no HIGH, and both behavioural MEDIUMs (M2, M3) were about background source selection, an area that also drew pass-1 M5 and pass-2 M3. Per the workspace review-loop rule, that area is now frozen as Simplification 13 with its required tests. With Simplifications 8, 11, 12 and 13 closed, a further MEDIUM in any of those areas should be answered against the frozen rules or backlogged.

## Review responses (pass 4 → revision 5)

| Finding | Response |
|---|---|
| M1 | Fixed inside Simplification 13 with no new mechanism. Recompute order enumeration passes `start_date=(start_utc_date − 31 days).isoformat()` and `end_date=(end + 2 days).isoformat()` as `YYYY-MM-DD` strings, matching the repository's bare-date semantics on both paths. Phase-1 `as_of` alone decides membership. The gap sweep passes `start_date=(now − 14 days).date().isoformat()`, and the no-op `max(…, activated_at − 14d)` is gone. Rule 2 and test (g) are updated. |
| N1 | Invoice-line price is `unit_price_micros_from_record(line)`, which falls back to `unit_price_cents`. A line with neither is an `invalid_inputs` skip and is recorded. |
| N2 | The delivery `as_of` "event time" fallback is removed. If `delivered_at` is absent or unparseable, the result is `Skip("invalid_inputs", "delivered_at_missing")`. |
| N3 | Accepted and named under Assumptions: a lost live task before the tenant's first `ensure_activated` is recompute-only. |
| N4 | The help text and the Assumptions line now read "Invoices created more than 15 days after the end date, or orders delivered more than 30 days after creation, are not reached." |

## Design freeze (2026-10-07)

The design review loop ran four passes, with findings going 2 HIGH / 11 MEDIUM → 1 HIGH / 4 MEDIUM → 0 HIGH / 3 MEDIUM → 0 HIGH / 1 MEDIUM. The workspace review-loop convergence rule applies. Pass 4 had no HIGH, and its one MEDIUM (M1) was a parameter error inside the already-frozen Simplification 13. With the revision-5 fix above, the design is approved and frozen. This is an orchestrator freeze after the pass-4 fix, not a fifth review pass. A later finding in the frozen areas (Simplifications 8, 11, 12 and 13) is answered against the frozen rules or backlogged.

Required boundary test. This pins M1, is part of Simplification 13 test (g), and blocks merge:

- Setup: settings timezone `America/Chicago`. The recompute range is `[start, end]`. The test runs once with `commerce_read_from_postgres=False` (ES path; the fake honours `range.created_at.lte` on a bare date as `00:00:00` UTC) and once with `True` (Postgres path via `read_hybrid_search`; SQLite mirror with lexical string compare on `created_at`).
- Order A: created and delivered on `end`, UTC (10:00Z). It must be recomputed.
- Order B: created at 23:00 Chicago on `end` and delivered at 23:30 Chicago on `end`. Both are on the next UTC date. It must be recomputed.
- Order C: delivered at 00:30 Chicago on `end + 1`. It must be enumerated, written to no record, and counted in `counts.out_of_range`.
- On each path the assertions are `written` includes A and B, `out_of_range ≥ 1` includes C, and the repository call received `end_date == (end + 2 days).isoformat()`.
