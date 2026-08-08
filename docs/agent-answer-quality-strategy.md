# Agent answer quality — strategy

Two defects surfaced by the persona soak run
(`Runsheet-backend/scripts/soak_personas.py`), both in the conversational agent
surface rather than the deterministic overlay agents:

1. The agent disclaims capability the platform demonstrably has.
2. A single response contradicted itself with two different counts for one
   question.

Neither is a model-quality problem, and neither is fixed by prompt wording. Both
trace to tool wiring and result composition, which is where this strategy aims.

## What was actually observed

Verbatim replies captured by the soak, against a live backend with a real Gemini
key:

| Question | Reply |
|---|---|
| "Are there accounts over their credit limit this week?" | "I cannot identify accounts over their credit limit. My tools do not have access to credit limit information or financial data." |
| "What deliveries do I have left today?" (driver) | "I cannot provide a list of deliveries you have left today. My tools do not have access to real-time delivery schedules." |
| "Show me the tanks most at risk of running out in the next 48 hours?" | "I am unable to identify the specific tanks most at risk." |
| "Give me a short health summary of the fleet." | "Total Trucks: 10, On Time: 8, Delayed: 2, Performance: 8…" — correct |
| "How many orders are currently unassigned?" | "There are 3 unassigned orders." … "There are 912 unassigned orders." |

The fleet answer working matters: it proves the model, the tool-calling loop and
the tenant scoping are all fine. The failures are specific and local.

The multi-domain dispatcher question also ran the plan path end to end
("Plan: … Status: completed ✅ Step 1: Execute scheduling subtask"), which is the
read-step executor doing its job — so plan execution is not the problem either.
Result *composition* is.

## Issue A — tool coverage

The five specialists carry these tools:

| Specialist | Tools |
|---|---|
| fleet | `search_fleet_data`, `get_fleet_summary`, `find_truck_by_id`, `get_all_locations`, `assign_asset_to_job` |
| fuel | `search_fuel_stations`, `get_fuel_summary`, `get_fuel_consumption_history`, `generate_fuel_report`, `request_fuel_refill`, `update_fuel_threshold` |
| ops | `search_orders`, `search_drivers`, `get_order_events`, `get_orders_metrics`, `get_ops_metrics`, + 3 reports |
| reporting | 8 report generators |
| scheduling | `search_jobs`, `get_job_details`, `find_available_assets`, `get_scheduling_summary`, `generate_dispatch_report`, `assign_asset_to_job`, `update_job_status`, `cancel_job`, `create_job` |

That splits the gap into three different problems with three different costs.

### A1 — tools that exist and are wired to nobody

Present in `Agents/tools/` and absent from every specialist's `TOOLS`:

`search_inventory`, `get_inventory_summary`, `get_analytics_overview`,
`get_performance_insights`, `search_support_tickets`, `assign_driver_to_order`,
`update_order_status`, `cancel_order`, `escalate_shipment`, `reassign_rider`.

This is the cheap tier — adding a name to a list. Note that four of those are
**mutations**, so wiring them changes what the agent can *do*, not just what it
can answer; they should land behind the ConfirmationProtocol with risk-registry
entries, and `update_order_status` in particular must respect
`VALID_STATUS_TRANSITIONS` rather than becoming a second, unguarded way to move
an order.

### A2 — domains with no tools at all

Nothing in `Agents/tools/` touches these, so the disclaimers are accurate:

| Domain | Live API that exists | Question it would answer |
|---|---|---|
| Commerce | `/api/commerce/{customers,accounts,invoices,ar-aging}`, `/accounts/{id}/credit-override` | credit limits, AR aging, invoice status |
| Customer tanks + forecasts | `/api/fuel/mvp/{customer-tanks,forecasts}`, `mvp_tank_forecasts` (1,983 docs) | tanks at risk of run-out |
| Compliance | `/api/compliance/{kfactor,ifta,terminal-bols}` | driver qualification, IFTA, BOLs |
| Terminals + sourcing | `/api/fuel/{terminals,supplier-contracts}`, sourcing recommender | where to lift, at what price |
| Storm mode | `/api/fuel/storm-mode/status` | weather posture |

This is the real work: each needs a tool with a tenant-scoped repository call, a
docstring the model can route on, and a rendered result. The forecast one is the
highest value — `mvp_tank_forecasts` already holds 1,983 documents including
run-out risk, so "which tanks are at risk" is a formatting job over data that is
already computed.

### A3 — no notion of "me"

"What deliveries do I have *left*" is not answerable by any tenant-scoped tool,
because tools receive a tenant and no actor. `Agents/tools/_tenant_context.py`
establishes `set_current_tenant` / `require_tenant_id`; there is no equivalent
for the calling user or their `driver_id`, even though `auth_users.driver_id`
links them.

Until an actor context exists, driver-scoped questions cannot be answered
correctly, and — more importantly — a naive fix is a **tenant-wide data leak**: a
tool that answers "my deliveries" by listing all orders would show one driver
another's work. So A3 is ordered before any driver-facing tool, not after.

## Issue B — the self-contradiction

Three compounding causes, in order of how much they matter.

### B1 — two different tools share one name

`search_orders` is defined twice:

- `Agents/tools/search_tools.py` — exported as `search_orders` from the package
- `Agents/tools/order_tools.py` — exported as `search_fuel_orders`

But `ops_intelligence_agent.py` imports `search_orders` **directly from
`order_tools`**, bypassing the alias. So the tool named `search_orders` means one
thing to the ops specialist and a different thing anywhere the package export is
used. Two implementations, one name, different data — presented to the model as
if interchangeable.

### B2 — one of them is broken, and reports a page size as a total

`search_tools.search_orders` has three independent defects:

- It searches the index **`orders`**, which **does not exist**. The live order
  index is `fuel_orders_current` (988 documents and climbing during the soak).
- It hardcodes `size=5` and then returns `f"Found {len(results)} orders"` — a
  page length described to the model as a count. Even against a healthy index it
  could never report more than 5.
- It renders `customer`, `value`, `items`, `priority`. The real order document
  has `customer_name`, `gallons_requested`, `product_code`, `status`. It is
  written against a schema this platform no longer has.

So one tool structurally cannot produce a true count, while the other
(`order_tools`, on `fuel_orders_current`) reports a real total. That is the shape
of "3 … 912": a capped or empty page reported as a count, beside a genuine one.

I have not pinned the exact provenance of the "3" — it may equally have come from
a scheduling tool counting unassigned *jobs*. The mechanism is established; that
one number's origin is not, and the fix does not depend on it.

### B3 — plan results are concatenated, never reconciled

`_format_plan_result` walks the executed steps and appends each result under a
status icon. `_synthesize` (simple path) joins results with `\n\n`. Neither looks
at whether two steps answered the same question differently. Two steps disagreeing
is therefore rendered verbatim as one self-contradicting answer.

## Strategy

Ordered so that each phase is provable on its own, and so the cheap fixes do not
depend on the expensive ones.

### Phase 1 — stop the contradiction at its source (small, high value)

1. **Delete or repair `search_tools.search_orders`.** It queries a
   non-existent index against a stale schema. Deleting it is the honest option:
   `order_tools.search_orders` already does the job against live data. If it is
   kept, it must target `fuel_orders_current`, return a true total, and render
   real fields.
2. **Make tool names unique.** One name, one implementation, package-wide.
3. **Never report a page length as a total.** Any tool that caps results should
   return both — `"showing 5 of 988"` — so the model cannot mistake one for the
   other.

Phase 1 alone removes the observed contradiction, because it removes the
disagreement rather than asking the model to notice it.

### Phase 2 — guard rails that would have caught this statically

4. A test asserting every index name referenced by a tool exists in the known
   index set. `orders` would have failed it the day the index was renamed.
5. A test asserting tool names are unique across `Agents/tools/`.
6. A test asserting each specialist's `TOOLS` are importable and non-empty, and
   that no tool is defined but unreferenced by any specialist — the A1 list is
   exactly what that test would print today.

These are cheap and they convert both classes of defect from "found by a
twelve-hour soak" into "found by CI".

### Phase 3 — close the coverage gap, highest value first

7. **Actor context** (A3) before any driver-facing tool, with a test that a
   driver-scoped tool cannot return another driver's rows.
8. **Tank forecast tools** — the data already exists, so this is the cheapest
   real capability win.
9. **Commerce read tools** — credit limits, AR aging, invoice status. Read-only
   to begin with; the ERP owns the authoritative copy, and `COMMERCE_STAFF_ROLES`
   already restricts the pricing surfaces to `platform_admin`, so agent tools
   must not become a way around that.
10. **A1 wiring**, mutations behind the ConfirmationProtocol with risk-registry
    entries.
11. Compliance, terminals/sourcing and storm mode last — real work, least
    frequently asked.

### Phase 4 — reconciliation, only if still needed

12. Replace concatenation in `_format_plan_result` with a synthesis step that
    reconciles overlapping answers, and says so when it cannot.

Deliberately last. With Phase 1 done, the steps should not disagree; a
reconciliation layer over disagreeing tools hides the defect instead of fixing
it, and it costs an extra model call per plan.

## How to prove it

The soak harness is the natural regression suite: it already drives real LLM
calls per persona and records every reply. Extend it with a **capability probe**
— a fixed question set with assertions rather than free-form prompts:

- "How many orders are unassigned?" → the reply must contain exactly one number,
  and it must match a direct API count taken at the same moment.
- "Which tanks are at risk in the next 48 hours?" → must not contain "I cannot"
  or "do not have access".
- Driver "what deliveries do I have left?" → must reference only orders assigned
  to that driver.

That turns both of these defects into a check that fails loudly, and gives the
Phase 3 work a definition of done beyond "the answer reads better".

## Recommended resolutions, given the market

The buyer is a **US fuel marketer — propane, heating oil, diesel, generator and
farm fuel** (`fuel-ops-hardening` introduction), pivoted from Nigerian petroleum
retail. That market decides three of the four open questions, and it decides them
more narrowly than a general-purpose answer would.

Four facts about it drive everything below:

- **The ERP is the book of record.** These marketers run billing and customer
  accounts on established industry systems. They will not move their books, and
  their controller has veto power in a deal.
- **Run-out is the business.** For keep-full and auto-fill customers a stockout
  is a contract breach and, in winter, a no-heat call. Degree-day and K-factor
  forecasting is the domain's standard language, and the platform already
  computes it (`mvp_tank_forecasts`, 1,983 documents).
- **Drivers are not desk users.** Gloves, cold, rural dead zones, high turnover,
  and DOT distracted-driving exposure.
- **It is an audited industry.** DOT hours-of-service, IFTA, BOLs, and
  cross-contamination rules are inspected. A confidently wrong number costs more
  than an honest "I don't know".

### 1. Commerce tools: read-only, permanently

Not a phase-one compromise — a product position. `COMMERCE_STAFF_ROLES` already
restricts pricing and AR to `platform_admin` precisely because the ERP owns the
authoritative copy; agent tools must not become the way around that.

An agent that can move a credit limit or void an invoice is a deal blocker in
this market. Read-only credit context answers the question the dispatcher
actually has — *can I deliver to this customer today?* — without touching the
ledger. Credit enforcement already exists deterministically and audited
(`COMMERCE_CREDIT_HOLDS_ENABLED`, the credit-check row lock); leave it there
rather than routing it through a model.

So build one read tool, `get_customer_delivery_eligibility`, returning credit
status, hold state and AR age together. Wire no commerce mutation tool to any
specialist.

### 2. Tool budget: task-shaped, not entity-shaped — cap ~10 per specialist

A dispatcher at a heating-oil marketer asks a small, highly repetitive set of
questions, and asks them hardest in a cold snap. Giving the model twenty
CRUD-style search tools to compose is both more prompt and more risk; it is
exactly how two steps end up disagreeing.

Prefer a few composite tools that each return **one authoritative answer**:

| Tool | The question it settles |
|---|---|
| `get_runout_risk_list(hours)` | who is about to run dry — the single most valuable question in propane and heating oil |
| `get_customer_delivery_eligibility(customer_id)` | credit + hold + AR in one answer |
| `get_todays_dispatch_status()` | late, unassigned, unstarted — one number each |
| `get_driver_day(driver_id)` | stops remaining and HOS margin |
| `get_best_terminal(product, volume)` | rack sourcing, once OPIS is configured |
| `get_storm_posture()` | during a cold snap this is the whole business |

This structurally prevents the contradiction class: one tool, one number, nothing
to reconcile. Add a `fuel_planning` specialist to own the tank/forecast/sourcing
tools rather than growing `fuel_agent` past a routable size.

### 3. Driver chat: do not build it

The platform already ships the right driver channel — a **voice surface**
(`/voice/drivers/verify`, `/voice/drivers/{id}/active-assignment`,
`POST /voice/drivers/{id}/assignments/{id}/reports`). Hands-free is the correct
interface for someone in a truck in January, and a chat box there is a support
burden and a distracted-driving liability.

So: drop the driver from the conversational surface, or restrict it to
procedure and safety questions with no data access. Spend the effort instead on
the actor context (A3) — which is still required, because the voice and REST
driver endpoints need to answer "my stops" safely — plus three or four fixed
buttons in the app. A3 stays ordered before any driver-facing data tool, for the
leak reason already given.

### 4. Sequence: correctness before coverage

In an audited industry, "I cannot identify that" is survivable and a wrong gallon
figure is not. So Phase 1 and Phase 2 ship first even though Phase 3 is the
visible win, and the capability probe should assert **numeric agreement with a
direct API count**, not merely the absence of a disclaimer.

### 5. Keep the disclaimers, but make them informative

Removing "I cannot" wholesale would be the wrong lesson. A tool that finds no
data should say which data is missing — "no tank-monitor readings for that
customer" is, in this market, a monitoring upsell (Veeder-Root, and the
integration framework already exists for it) rather than a failure. Tools should
therefore return an explicit no-data signal the agent can pass through, distinct
from "I have no tool for this".

## Decisions still genuinely open

- **Whether the conversational surface is a selling feature at all**, or a
  convenience over a dispatch board that is already the product. The recommended
  tools above are useful either way, but the answer changes how much to invest in
  the plan/synthesis path versus the deterministic UI.
- **Which tank-monitor integration to lead with.** `get_runout_risk_list` is only
  as good as the readings behind it, and forecast confidence collapses to 0.1
  with no history (Requirement 1.7). Veeder-Root has a reference connector;
  whether that is the right first partner for propane is a commercial call.
- **How much history to trust.** `search_tools` was written against an order
  schema that no longer exists. I checked the rest of that module and the damage
  is contained: `orders` is the **only** dead index in it — `trucks`,
  `inventory` and `support_tickets` all exist. So Phase 1 really is small. Worth
  deciding whether to audit the remaining tool modules for document-shape drift
  (the field names, not just the index names) before Phase 3 builds on them,
  since a tool that reads absent fields returns `N/A` rather than failing.
