# Requirements: Cost / margin (COGS) feed for RevenueGuard

## Summary

The May product audit (`.kiro/audits/product-owner-audit-2026-05-08.md` #24, "Unnecessary" list, recommendation 10) found that `RevenueGuard` "guards nothing": it has no cost source. This is the margin half of OI-06 in `.agents/tasks/open-issues-register-2026-10-06/report.md`. The owner put it in go-live scope on 2026-10-07. The customer-portal half of OI-06 is a separate spec.

This spec adds four things:

1. A per-tenant, effective-dated product cost basis per product and terminal. It is built from data already in the system (terminal BOLs, supplier contracts and rack prices) plus admin entry and CSV import.
2. A landed-cost resolver: weighted-average cost over a configurable window, falling back to rack price plus adders, with an explicit "no cost" state.
3. Margin records per quote, order, delivery and invoice line. Each record stores the cost snapshot it used.
4. A RevenueGuard rewire that flags negative margin, below-floor margin and missing cost, plus admin-only views and an admin-only CSV export.

Dispatchers, drivers, customers and the ERP push never see cost or margin.

### Codebase grounding (worktree `.worktrees/margin-feed`, branch `production-readiness/margin-feed`, written at base `0f82def`, re-checked at `a99bfd9`)

| Item | Where | What it means for this spec |
|---|---|---|
| RevenueGuard | `Agents/overlay/revenue_guard.py` | Computes `(revenue − fuel_cost − sla_penalty)/revenue` from `jobs_current` fields that nothing writes, so the margin is always skipped. Its state `_route_margins` is keyed by `route_id` only, not by tenant. It uses a percentage target of 15%, which is meaningless for fuel: a 20 cpg margin on $2.50 rack is 8%. It subscribes to `fuel_management_agent` RiskSignals and `OutcomeRecord`s and returns `[]` when no signals arrive. Proposals are `PolicyChangeProposal`, HIGH risk, human-approved. Weekly report goes to `agent_revenue_reports`. |
| Invoice pricing | `commerce/services/invoice_service.py` `generate_from_order` (≈L675–1145) | Lines are canonicalized to `unit_price_micros` and `subtotal_cents`, then repriced by `SalesPricingEngine.resolve_price` when the factory is wired (`bootstrap/compliance.py` `_sales_pricing_engine_factory`, ≈L269, which calls `build_sales_pricing_engine` with the price-protection resolver since OI-14 `b6428e2`). A partial contract splits a line into a contract-priced and a market-priced line (`_contract_split_lines`), each with its own `quantity_gallons`, `unit_price_micros`, `unit_price_cents` and `subtotal_cents`. If resolution fails for a line, the existing price or `market_price_cents` is kept. Tax is separate (`tax_cents`, not in `subtotal_cents`). Invoices are visible to `admin` and `dispatcher` (`commerce/api/_authz.py` `COMMERCE_OPS_ROLES`), broadcast over WS and pushed to QBO. |
| Money helpers | `services/money.py` | Integer micro-dollars per gallon (`MICROS_PER_CENT = 10_000`). `line_subtotal_cents` uses `Decimal` and rounds once, half-up. |
| Sales pricing / rack | `commerce/services/sales_pricing_engine.py` `get_rack_price` | Reads the latest `rack_prices` row for (tenant, terminal, product), with no as-of bound, and rounds `price_per_gallon_usd` (a float) to whole cents. That precision loss is why margin does not reuse it. |
| Rack prices | `integrations/rack_price_provider_base.py` `RackPrice`, `integrations/rack_price_sync.py`, `rack_prices` index | Comes from OPIS or a CSV fallback. The tenant rack CSV upload does not exist: `bootstrap/agents.py:1237` `_uploaded_csv_loader` raises `FileNotFoundError`. In practice rack rows exist only with OPIS or seeds. |
| Terminal BOLs | `compliance/models/terminal_bol.py`, `compliance/services/terminal_bol_ingestion_service.py`, `terminal_bols` index (strict mapping) | Carries `net_gallons`, `gross_gallons`, `product_code` (free text such as ULSD), `supplier_name`, `terminal_id` (nullable), `timestamp`, `load_plan_id`, `status` and `needs_operator_confirmation`. It has no cost field. |
| Supplier contracts | `fuel/terminal_models.py` `SupplierContract` | `contract_price_per_gallon_usd` (optional), `effective_from` and `effective_to`. |
| Loading plans | `Agents/support/compartment_models.py` `LoadingPlan` | `run_id`, `truck_id`, `terminal_id` (optional) and `contract_id` (optional). The assignments carry the `FuelOrder`. |
| Orders | `fuel/order_models.py` `FuelOrder` | `unit_price_micros`, `gallons_requested`, `assigned_run_id`, `assigned_asset_id` and `delivery_result.actual_gallons` / `delivered_at`. There is no terminal field and no quote entity. |
| CSV export | `services/csv_export.py` | BOM, formula-injection escaping, keyset paging, 50,000-row cap with a 413 before the first byte, per-user rate limit, `export_guard`, filename and one audit line. `ExportType` is a closed `Literal`. |
| Product codes | `fuel/services/fuel_product_catalog.py` `canonicalize` | Maps aliases such as ULSD to canonical codes. |
| Audit | `telemetry/service.py` `log_audit_event` | Structured log line only, not durable. |

## Decisions (owner-delegated, 2026-10-07)

The owner delegated these choices. Each one has a one-line rationale. None was both high-impact and without a defensible default, so none was escalated.

| ID | Decision | Rationale |
|---|---|---|
| D1 | Cost and margin are visible to tenant `admin` only. `platform_admin` implies nothing and needs `admin` alongside it. This is unlike pricing rules, which are staff-only. | The owner said admin-only. Margin is the distributor's own business data, not an ERP-owned price. |
| D2 | API namespace is `/api/commerce/margin/...`, behind a `commerce_margin_feed_enabled` setting. The flag check runs before the role check (404 when disabled), following `commerce_invoicing_enabled`. | Commerce owns invoices and pricing. This reuses the existing flag-then-role ordering. |
| D3 | Margin data lives in its own tenant-scoped store and is never written onto order, invoice, BOL or job documents. | Those documents reach dispatchers, WebSocket clients, QBO and customers. A separate store is the only way to make D1 hold structurally. |
| D4 | Landed cost per gallon is product cost plus adders. Product cost resolves in this order: (1) an active admin cost override, (2) weighted-average cost (WAC) of cost lots in the window, (3) the latest non-stale rack price, (4) "no cost". | An explicit admin statement beats inference. WAC is the requested method. Rack is the requested fallback. "No cost" is never zero. |
| D5 | There are two kinds of cost lot. BOL-derived lots use the BOL's net gallons, priced from a purchase entry that references the BOL, else the linked supplier contract price, else the rack price in effect at lift time. Purchase entries that reference no BOL are standalone lots. A purchase entry that references a BOL prices that BOL and never adds its own gallons. | This reuses existing BOL data. Rack-at-lift is how unbranded rack purchases are billed. The reference rule prevents double counting. |
| D6 | WAC window defaults to 30 days, configurable per tenant from 1 to 365. Lots dated after the as-of time are excluded. | 30 days covers a typical inventory turn. Excluding later lots prevents look-ahead. |
| D7 | Rack fallback staleness defaults to 4 days, configurable from 1 to 30. A rack row older than that is not used. | Rack prices post daily, and a Friday price must still cover Monday. |
| D8 | Adders (freight, fees, other) are effective-dated cost entries in integer micros per gallon. They are scoped per (product, terminal) or tenant-wide per product, and the terminal-specific value wins. They apply to every product-cost method, including overrides. If none are configured, adders are 0, and the snapshot records `adders_configured=false`. | One rule for every method avoids hidden double-adding. Recording the flag keeps zero adders visible. |
| D9 | Taxes are excluded from margin on both the price and the cost side. | Invoice tax is outside `subtotal_cents` and is a pass-through. |
| D10 | Cost gallons (BOL net) and sold gallons (POD `actual_gallons`) are treated as the same unit. There is no net/gross conversion in margin. | Shrink belongs to reconciliation (`ReconciliationService`). Mixing it into margin would hide it. |
| D11 | The terminal for a sale comes from the order's loading plan `terminal_id`, else from a terminal BOL linked to that plan. If neither exists, the record uses tenant-wide product cost: WAC across all terminals or a tenant-wide override, with no rack fallback, labelled `terminal_unattributed`. | This gives a usable cost while saying plainly that the terminal is unknown. |
| D12 | Margin stages are `quote` (preview, not persisted), `order_estimate`, `delivery` and `invoice` (one per invoice line). Summaries prefer `invoice`, then `delivery`, then `order_estimate`. | This covers the requested quote, order and invoice flags without inventing a quote entity. |
| D13 | Invoice-stage margin is computed on draft generation, recomputed on finalize and then frozen. After that it changes only by explicit admin recompute (versioned, with a reason). On void the record is marked void. | The finalized invoice is the commercial fact. Recompute is the path for late BOLs. |
| D14 | The margin floor is set in cents per gallon. The tenant default is 10 cpg (100,000 micros), with optional per-product overrides from 0 to $5.00 per gallon. The percentage target is removed. | Fuel margins are judged per gallon. 10 cpg is a conservative low bar for commercial and heating-oil distributors. |
| D15 | Flags are computed once by the margin service and stored on the record. RevenueGuard consumes the flags. It does not recompute them. | Flags have one owner and are deterministic and unit-testable. The agent stays a consumer. |
| D16 | Leakage proposals group by (customer, product), not by route. | `route_id` is not set on fuel orders. Customer and product is the unit that pricing rules act on. |
| D17 | Cost entries are immutable. An "edit" supersedes the old entry and a "delete" voids it, each with a reason. The entry history and a `log_audit_event` line form the audit trail. Settings changes are audit-logged and snapshotted onto every margin record. | This reuses the existing audit seam with no new audit store, and history stays reconstructible. |
| D18 | Rows that come from a backfill or recompute get flags but no per-record alerts. RevenueGuard sends one digest per recompute run. | This avoids the alert flood the owner was warned about for OI-33 (one alert per old record). |
| D19 | The margin CSV export is `GET /api/commerce/margin/records/export`, type `margin`, built with `services/csv_export.py`, admin only. | This reuses all the existing safeguards. |
| D20 | There is no new third-party dependency. USD only. | Owner constraint. Every tenant is USD today. |

### Dependency on OI-14 (landed as `b6428e2` on `production-readiness/go-live-blockers`)

OI-14 (`b6428e2`, the rebased form of the original `9d94f7d`) prices invoice lines through the same resolver as `POST /api/commerce/pricing/resolve`, so price-protection contracts apply. Both call `build_sales_pricing_engine(es_service, tenant_id)` in `commerce/services/sales_pricing_engine.py`. When a contract covers only part of a delivery, `_contract_split_lines` replaces the line with a contract-priced line and a market-priced line. Tax is still computed on the unsplit lines.

Invoice-stage margin must therefore read the final persisted line values in `doc["line_items"]` (`unit_price_micros`, or legacy `unit_price_cents`, plus `subtotal_cents` and `quantity_gallons`, or `quantity`), after every pricing step in `generate_from_order`, including the split. It must never read the order's raw price or re-run its own resolver. Quote-stage preview resolves through the same `SalesPricingEngine` factory `pricing/resolve` uses.

Implementation of this spec must build on OI-14, which has landed on `production-readiness/go-live-blockers` as `b6428e2`, and re-run the AC-13 and AC-14 tests against the merged code.

## Functional Requirements

### FR1 Cost entries (admin input)

1. An admin can create cost entries of three kinds: `purchase` (a cost lot), `override` (a pinned product cost) and `adder` (freight, fee or other).
2. Entry fields:

   | Field | Rule |
   |---|---|
   | `kind` | Required. |
   | `product_code` | Required. Canonicalized through `canonicalize`. Must be in the catalog. |
   | `terminal_id` | Optional. Must be one of the tenant's terminals. Blank means tenant-wide. A `purchase` must have a terminal. |
   | `supplier_name` | Optional. At most 128 characters. |
   | `effective_at` | Required. ISO-8601 date or datetime. A date means 00:00 in the tenant's timezone. No more than 1 day in the future. |
   | `effective_to` | Optional. Overrides and adders only. Must be after `effective_at`. |
   | `unit_cost_usd` | Required. Decimal string from 0 to 100.000000 with at most 6 decimal places. Stored as integer micros. More precision is rejected, never rounded. |
   | `gallons` | Required and greater than 0 for `purchase`, at most 1,000,000. Not allowed on other kinds. At most 3 decimals. |
   | `adder_type` | Required for `adder`: `freight`, `fee` or `other`. |
   | `bol_id` / `reference` | Optional. At most 128 characters. A `bol_id` must name a terminal BOL of the same tenant, product and terminal. |
   | `notes` | Optional. At most 500 characters. |

3. An invalid field gets 422 with field-level messages in the standard error envelope.
4. CSV import accepts the same columns and runs in two modes, `dry_run=true` (the default) and `dry_run=false`. A file may be at most 5 MB and 10,000 data rows, UTF-8 with or without BOM. Validation is all-or-nothing: any invalid row rejects the whole file with per-row errors.
5. A duplicate is a row whose natural key, (kind, product, terminal, effective_at, bol_id/reference, adder_type), matches an active entry. Duplicates are reported and skipped, never overwritten.
6. Entries are immutable. Supersede creates a new entry linked to the old one. Void marks the entry voided. Both need a reason of 1 to 500 characters and record the actor and time.
7. Every create, supersede, void, import (counts only) and settings change emits `log_audit_event`, with tenant, user, action, entry ids and before/after values for supersede.

### FR2 Cost basis resolution

1. `resolve_landed_cost(tenant, product, terminal | None, as_of)` returns the following:
   - `method`: `override`, `wac`, `rack_fallback` or `none`.
   - `product_cost_micros`, or null.
   - `adders_micros` and their breakdown.
   - `landed_cost_micros`, or null.
   - The lot ids, override id, rack row id and contract ids used.
   - The window and staleness settings used.
   - `terminal_unattributed`, as a boolean.
   - `no_cost_reason` when the method is `none`.
2. An override is active when `effective_at ≤ as_of` and either `effective_to` is null or `as_of < effective_to`. If several are active, the latest `effective_at` wins, then the latest `created_at`.
3. WAC = Σ(gallonsᵢ × unit_costᵢ) / Σ gallonsᵢ over eligible lots with `as_of − window < effective_at ≤ as_of`. It is computed in `Decimal` and quantized once to integer micros, half-up.
4. A BOL is an eligible lot when all of these hold:
   - It belongs to the tenant.
   - `needs_operator_confirmation` is false and `status` ≠ `pending_confirmation`.
   - `terminal_id` is set.
   - `product_code` canonicalizes.
   - `net_gallons` > 0.
   - A price resolves through D5: a referencing purchase entry, else a contract active at the BOL `timestamp`, else a rack row for (tenant, terminal, product) with `effective_at ≤ timestamp` within the staleness limit.

   An ineligible BOL is left out and counted, by reason, in the diagnostics.
5. Rack fallback uses the latest `rack_prices` row for (tenant, terminal, product) with `effective_at ≤ as_of` and age within the staleness limit. `price_per_gallon_usd` is converted with `Decimal(str(value))` and quantized to micros half-up. Rack fallback is not used when the terminal is unattributed.
6. If no method yields a cost, `method=none`, and `no_cost_reason` is one of `product_unknown`, `no_lots_no_rack`, `rack_stale`, `terminal_unattributed_no_cost` or `computation_error`. A missing cost is never represented as 0. A real zero cost is allowed only from an explicit entry.

### FR3 Margin records

1. Margin record fields:
   - `tenant_id`, `stage`, source ids (`order_id`, `invoice_id`, `line_index`), `customer_id`, `account_id`, `product_code`, `terminal_id`.
   - `gallons` (Decimal, 3 dp, from `Decimal(str(value))`), `unit_price_micros`, `revenue_cents`.
   - The full cost snapshot from FR2.
   - `cost_cents` = round_half_up(gallons × landed_cost_micros / 10,000), or null.
   - `margin_cents` = revenue_cents − cost_cents, or null.
   - `margin_per_gallon_micros` = unit_price_micros − landed_cost_micros, or null.
   - `margin_pct` (Decimal string, 2 dp, half-up; null when revenue is 0 or cost is null).
   - `flags`, `floor_micros_used`, `as_of`, `version`, `status` (`active`, `superseded`, `void`), `origin` (`live`, `recompute`) and `computed_at`.
2. Inputs and as-of time per stage:

   | Stage | Price | Gallons | As-of |
   |---|---|---|---|
   | `order_estimate` | order `unit_price_micros` | `gallons_requested` | `delivery_window_start`, else creation time |
   | `delivery` | order `unit_price_micros` | `delivery_result.actual_gallons` | `delivered_at` |
   | `invoice` | the persisted line's `unit_price_micros` and `subtotal_cents` (the line's `revenue_cents` is its `subtotal_cents`) | line gallons | `delivery_result.delivered_at`, else invoice `created_at` |

   An order without price or gallons gets no `order_estimate` record, and a debug line is logged.
3. Records are idempotent per (tenant, stage, source ids, version). Reprocessing the same event does not create a duplicate.
4. Margin computation runs after the order or invoice is persisted and can never fail, roll back or block that operation. On error the record is written with `method=none, no_cost_reason=computation_error`, and an ERROR line is logged with tenant, stage and source ids.
5. Recompute is `POST /api/commerce/margin/recompute`, admin only. It takes a date range of at most 92 days and `only_missing` (default true). It creates new versions with `origin=recompute`, marks the old ones superseded and is audit-logged. It is how existing invoices get margin when the feed is first turned on. There is no automatic backfill.

### FR4 Flags (owned by the margin service)

1. `missing_cost`: the method is `none`.
2. `negative_margin`: `margin_per_gallon_micros` < 0.
3. `below_floor`: 0 ≤ `margin_per_gallon_micros` < the floor, using the per-product floor if one is set, else the tenant floor.
4. `terminal_unattributed`: informational. It is not an alert by itself.
5. The flags on a record depend only on the stored record and the floor snapshot.

### FR5 RevenueGuard

1. RevenueGuard reads margin records, not `jobs_current` revenue or fuel-cost fields. The percentage-target path is removed.
2. The margin service publishes one `RiskSignal` (`source_agent="margin_feed"`, `entity_type` = stage, `entity_id` = record id, `tenant_id`) for each live record that has a flag. RevenueGuard subscribes to these signals.
3. RevenueGuard alerts tenant admins on live `delivery` and `invoice` records flagged `negative_margin` (severity high) or `missing_cost` (medium). It sends at most one alert per record and respects the overlay mode: `disabled` sends nothing, and `shadow` writes to the activity log only.
4. `below_floor` does not alert per record. When 3 consecutive live records for the same (tenant, customer, product) are below floor, RevenueGuard produces a `PolicyChangeProposal` (HIGH risk, human approval, 60-minute cooldown per key). The threshold stays configurable as `leakage_threshold`.
5. Quote and order-estimate flags are returned inline to the admin caller and do not alert.
6. All agent state is keyed by tenant. No in-memory structure mixes tenants.
7. The weekly report adds, per tenant, revenue, cost and margin cents over records with cost; record and gallon counts by flag; and the missing-cost share.
8. A recompute run produces one digest per tenant per run instead of per-record alerts (D18).
9. RevenueGuard never changes prices or pricing rules on its own.

### FR6 Admin API and UI

1. Endpoints, all admin-only behind D2:
   - `GET`/`POST /api/commerce/margin/cost-entries`
   - `POST .../cost-entries/{id}/supersede`
   - `POST .../cost-entries/{id}/void`
   - `POST .../cost-entries/import`
   - `GET /api/commerce/margin/cost-basis?product_code&terminal_id&as_of`, which returns the FR2 result and the lot list
   - `GET /api/commerce/margin/records` (filters: `start_date`, `end_date`, `customer_id`, `product_code`, `terminal_id`, `stage`, `flag`, `status`; keyset paging, page size at most 200)
   - `GET /api/commerce/margin/summary`, grouped by day, customer, product or terminal
   - `POST /api/commerce/margin/preview`
   - `POST /api/commerce/margin/recompute`
   - `GET`/`PUT /api/commerce/margin/settings` (window, staleness, tenant floor, per-product floors)
2. Preview input: `product_code` (required), `gallons` (required, greater than 0), optional `terminal_id`, and either `unit_price_usd` or `customer_id` (plus optional `account_id`), resolved through the `pricing/resolve` engine. Supplying both or neither returns 422. Preview persists nothing.
3. The Commerce hub gets a "Margin" area that renders only for `admin`. It holds the records list with flag filters, the summary, a cost-basis viewer, cost-entry list/add/supersede/void, CSV import with a dry-run preview, settings, and the shared "Export CSV" button. It follows existing component conventions and is accessible: native controls, labels, keyboard operation, visible focus and live-region errors.

### FR7 Export

1. `GET /api/commerce/margin/records/export` accepts the records filters, except paging, and uses `services/csv_export.py`. That covers the BOM, injection escaping, keyset paging, the 50,000-row cap (413), the rate limit, the audit line and the filename `margin_<tenant>_<YYYYMMDD>.csv`. `margin` is added to `ExportType`.
2. Columns: the record fields from FR3.1, with money in both cents and decimal USD, plus cost method, `no_cost_reason` and flags. There are no customer contact emails or phones, per data-export D6.

### FR8 Non-exposure

1. No cost or margin value appears on any of these surfaces:
   - Order, invoice, BOL or job API responses, including the existing invoice export.
   - WebSocket broadcasts, QBO/ERP payloads and customer notifications.
   - The driver app.
   - Approval-queue items, activity-log entries or agent chat answers visible to non-admins.
2. RevenueGuard proposals and alerts that carry cost or margin figures go only to admin-visible channels. Otherwise they carry ids and flag names only.

## Non-Functional Requirements

1. Precision:
   - All money arithmetic uses integers (cents, micros) or `Decimal`. No float multiplication, division or accumulation touches money.
   - Gallons enter through `Decimal(str(value))`.
   - Each derived amount is rounded once, half-up.
2. Performance:
   - Margin computation adds no more than 200 ms p95 to invoice generation, or runs asynchronously after persistence.
   - Cost basis resolution reads at most the window's lots, bounded by the paged store reads.
3. Every query is tenant-scoped with `inject_tenant_filter` or the Postgres tenant predicate. Terminal ids are never trusted without the tenant scope.
4. There are no new dependencies, and no change to invoice amounts, tax or order behaviour.
5. Logs carry tenant, ids, stage and outcome. Margin values may appear only at DEBUG.

## Acceptance Criteria (EARS)

### Cost method correctness

1. When an active override exists for (product, terminal) at the as-of time, the system shall use it as the product cost with `method=override`, ignoring lots and rack.
2. When no override is active and eligible lots exist in the window, the system shall set product cost to Σ(gallons × unit cost)/Σ gallons, quantized once to micros half-up, with `method=wac`. Example: 1,000 gal at $2.500000 plus 3,000 gal at $2.600000 gives 2,575,000 micros.
3. The system shall exclude lots dated after the as-of time and lots at or before as-of minus the window from WAC.
4. When a purchase entry references a BOL, the system shall price that BOL lot at the entry's unit cost and shall not add the entry's gallons as a second lot.
5. When a BOL has no referencing entry, the system shall price it from the supplier contract active at the BOL timestamp, else from the latest rack row at or before the timestamp and within staleness. If neither exists, it shall exclude the BOL and count it in diagnostics.
6. The system shall exclude BOLs that need operator confirmation, have no terminal, or have a product code that does not canonicalize, and shall report counts by reason in `cost-basis` diagnostics.
7. When no override or lots apply, the system shall use the latest rack row at or before as-of within the staleness limit, converted with `Decimal(str())`, with `method=rack_fallback`.
8. If no override, lot or non-stale rack row exists, then the system shall set `method=none`, `landed_cost_micros=null`, `cost_cents=null`, `margin_cents=null`, flag `missing_cost` and give a `no_cost_reason`. It shall never store 0.
9. When an explicit entry sets the unit cost to $0.000000, the system shall treat the cost as 0 and not as missing.
10. The system shall add the active adders (terminal-specific before tenant-wide, per `adder_type`) to the product cost for every method, and shall set `adders_configured=false` when none apply.
11. When a sale's terminal cannot be attributed, the system shall use tenant-wide product cost without rack fallback and set `terminal_unattributed=true`.
12. When a cost entry is superseded or voided, the system shall exclude the old entry from resolutions whose computation happens afterwards. It shall leave frozen records unchanged.

### Margin computation and OI-14 dependency

13. When an invoice draft is generated, the system shall create one `invoice` record per line from the line's persisted `unit_price_micros` and `subtotal_cents`, after all pricing resolution. Test: a resolver that changes the line price yields margin on the resolved price.
14. The system shall not re-resolve price for invoice-stage margin. After OI-14 merges, the AC-13 test shall pass with a price-protection contract applied (re-run gate).
15. When an invoice is finalized, the system shall recompute and then freeze its records. When an invoice is voided, it shall mark them `void`.
16. If margin computation raises, then the invoice or order operation shall still succeed, a record with `no_cost_reason=computation_error` shall be written, and an ERROR log shall be emitted.
17. When the same order or invoice event is processed twice, the system shall keep one active record per (stage, source ids).
18. When an admin runs recompute over at most 92 days, the system shall write new versions with `origin=recompute`, mark previous versions `superseded`, and audit-log the run. If the range exceeds 92 days, it shall return 422.

### Money precision

19. The system shall store unit prices and costs as integer micros and totals as integer cents. It shall compute cost and revenue with `Decimal`, rounding once, half-up.
20. Given 4,321.456 gal at a landed cost of 2,987,654 micros, the system shall store `cost_cents` = 1,291,102. This is 4,321.456 × 298.7654 = 1,291,101.53…, which rounds half-up to 1,291,102.
21. If `unit_cost_usd` has more than 6 decimal places, is negative, is non-numeric or exceeds 100, then the system shall return 422 without rounding.
22. Given a property test over random gallons (0.001–100,000) and prices, the system shall satisfy `margin_cents == revenue_cents − cost_cents` exactly, and summary totals shall equal the sum of record values with no drift.
23. The system shall serialize `margin_pct` as a 2-dp decimal string and return null when revenue is 0 or cost is null.

### Tenant isolation

24. Given tenants A and B with identical product codes and terminal ids, when A's cost basis is resolved, the system shall use no entry, BOL, contract or rack row belonging to B.
25. When a tenant-A admin calls any margin endpoint, including export, recompute and preview, the system shall return or modify no tenant-B data. If a `tenant_id` parameter is passed by a non-`platform_admin`, the system shall ignore or reject it.
26. If a cost entry references a `terminal_id` or `bol_id` of another tenant, then the system shall return 422.
27. The system shall keep RevenueGuard state, signals, alerts, proposals and reports per tenant. A two-tenant test shall show that A's below-floor records never contribute to B's leakage count.

### Admin-only access

28. While `commerce_margin_feed_enabled` is false, the system shall return 404 on every margin endpoint for every role, and shall write no margin records.
29. When a caller with `admin` calls a margin endpoint, the system shall serve it.
30. If a caller has `dispatcher`, `driver` or only `platform_admin`, or is a customer session, then the system shall return 403 on every margin endpoint, including the export.
31. The system shall not include cost or margin fields in order, invoice, BOL or job responses, the invoice export, WS invoice broadcasts, QBO payloads, customer notifications, driver-app payloads, or non-admin-visible approval items and activity entries. A field-scan test shall verify each.
32. The system shall render the Margin area and its nav entry only for `admin`. A dispatcher session shall not see it.
33. When cost entries are created, superseded, voided or imported, or settings change, the system shall emit `log_audit_event` with tenant, user, action and ids, and shall keep superseded and voided entries readable by admins.

### Import and export

34. When a CSV import is submitted with `dry_run=true`, the system shall validate every row and return per-row results without writing.
35. If any row is invalid, then the system shall reject the whole file with per-row errors and write nothing.
36. The system shall reject files over 5 MB or 10,000 rows with 413, and shall report and skip rows that duplicate an active entry's natural key.
37. The margin export shall use `services/csv_export.py`, shall be admin-only, and shall pass the shared helper's BOM, injection, 50,000-row cap, rate-limit and tenant-isolation tests.

### RevenueGuard flags

38. When a live `delivery` or `invoice` record has `margin_per_gallon_micros` < 0, the system shall flag `negative_margin`, and RevenueGuard shall send one high-severity admin alert for that record.
39. When a live record has 0 ≤ margin per gallon < the floor, the system shall flag `below_floor` and shall send no per-record alert. Floor resolution is per product, else tenant, with a default of 100,000 micros.
40. When 3 consecutive live records for one (tenant, customer, product) are `below_floor`, RevenueGuard shall emit one HIGH-risk `PolicyChangeProposal` that requires approval, with a 60-minute cooldown per key.
41. When a live `delivery` or `invoice` record has `method=none`, the system shall flag `missing_cost`, and RevenueGuard shall send one medium-severity admin alert.
42. When a margin preview is requested, the system shall return the margin, cost snapshot and flags inline and persist nothing. If both or neither of `unit_price_usd` and `customer_id` are given, it shall return 422.
43. While the RevenueGuard overlay mode is `disabled`, it shall send nothing. While the mode is `shadow`, it shall write only activity-log entries. Records shall still carry flags in both modes.
44. When a recompute run produces flagged records, RevenueGuard shall send one digest per tenant per run and no per-record alerts.
45. RevenueGuard shall not read `jobs_current` revenue or fuel-cost fields, and shall not apply a percentage margin target.
46. The weekly report shall include revenue, cost and margin totals over records with cost, record and gallon counts per flag, and the missing-cost share, per tenant.

## Assumptions

- A1: The tenant's terminals exist in the `terminals` index, so `terminal_id` validation is possible. If a tenant has none, it can still use tenant-wide overrides and purchase lots attributed to a terminal it creates.
- A2: A BOL's `timestamp` is the lift time used for rack and contract lookup.
- A3: Sold gallons on invoice lines equal POD `actual_gallons`, which `generate_from_order` already enforces.
- A4: Tenant-admin users are trusted with all cost data in their tenant. There is no finer cost permission.
- A5: Staging verification uses `demo-tenant` with `QA-` fixtures, and `qa-tenant-b` for isolation (owner exception, 2026-10-07).

## Out of Scope

- The customer portal (other half of OI-06). Customers never see cost or margin either way.
- Driver, truck, labor, compliance or overhead cost allocation. Fully loaded P&L needs payroll and fleet cost sources the system does not have. Per-gallon freight and fee adders are the proxy.
- FIFO/LIFO, perpetual inventory valuation, GL posting, and pushing COGS to QBO or ERP.
- A tenant rack-price CSV upload for the `rack_csv` category. That is a separate gap feeding the sourcing recommender. Admin-supplied costs use the FR1 import.
- Net/gross volume conversion in margin (D10).
- Implementing OI-14 itself. This spec depends on it but does not change invoice pricing.
- Automatic price changes by RevenueGuard, multi-currency, and changes to invoice amounts or tax.
