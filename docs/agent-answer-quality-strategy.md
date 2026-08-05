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

## Decisions this needs

- **Read-only or not.** Whether commerce agent tools may write at all, given the
  ERP is authoritative and the pricing surfaces are deliberately staff-only.
- **Tool-count budget.** Every tool added enters the prompt. Going from ~9 tools
  per specialist to ~20 will affect routing accuracy and cost, and at some point
  the specialists need splitting rather than growing. Worth a ceiling.
- **Driver chat at all.** A3 is only worth building if drivers are meant to have
  a conversational surface; if the mobile app is task-driven, the cheaper answer
  is for the driver persona to have no chat rather than a chat that cannot see
  their own work.
- **How much history to trust.** `search_tools` was written against an order
  schema that no longer exists. I checked the rest of that module and the damage
  is contained: `orders` is the **only** dead index in it — `trucks`,
  `inventory` and `support_tickets` all exist. So Phase 1 really is small. Worth
  deciding whether to audit the remaining tool modules for document-shape drift
  (the field names, not just the index names) before Phase 3 builds on them,
  since a tool that reads absent fields returns `N/A` rather than failing.
