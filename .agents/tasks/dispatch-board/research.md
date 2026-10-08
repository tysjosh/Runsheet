# Dispatch Board: research

Main checkout `/Users/olukotunjosh/Downloads/Runsheet`, branch `production-readiness/go-live-blockers` at `ad72dde`. Paths below are relative to the repo root. Specs are read from `.kiro/specs/`. Web sources were fetched on 2026-10-07 and are numbered `[S#]`; the list is at the end. This is a documents-only step: no code, commits, deploys or staging changes.

## Summary

The board is a structured planning surface, not a free-form canvas. Dispatchers drag real objects (orders, drivers, trucks, stops) onto fixed slots (a truck's lane, a load, a stop position). Every drop is checked by the server before anything is committed.

Recommendations this research supports:

- **R1. Shape.** One service-day board. A horizontal lane per truck, a tray of unassigned work on the left, and a detail drawer on the right. Not a node graph (see 1.8).
- **R2. Lane orientation: rows.** One row per truck, time on the x-axis, and a fixed lane header holding the truck, its driver slot and a compartment gauge. Columns per truck are rejected (see Part 2).
- **R3. DnD library: Pragmatic drag and drop.** Use `@atlaskit/pragmatic-drag-and-drop` core plus the `hitbox`, `auto-scroll` and `live-region` packages, pinned to exact versions. Do not use the Atlaskit UI packages (`react-accessibility`, `react-drop-indicator`), because they pull in Emotion, Compiled and Atlaskit tokens. Draw indicators with the project's Tailwind `components/ui`. The fallback is React Aria DnD. Reasons are in 1.4.
- **R4. Accessibility.** Every drag has a non-drag equivalent: a "Move to…" or "Assign to…" menu on each card, and a click-to-select, click-to-place mode. Every outcome is announced in one polite live region. Targets are at least 24×24 px. This satisfies WCAG 2.2 SC 2.5.7 without arrow-key dragging [S17][S24].
- **R5. Validation.** Add a server dry-run endpoint (NEW) that runs the existing validators against a proposed lane state and returns `pass`/`warn`/`block` per check, each with a reason code and text. The client draws the result on the hovered lane while dragging and holds the commit until the result arrives.
- **R6. Invalid-drop policy, in three tiers.**
  - Regulatory and physical violations block, with no override.
  - Operational risks warn and can be overridden with a reason, which is audited. This copies the existing HOS-gate override pattern.
  - Advisories are informational only.
  - Details are in Part 3.
- **R7. Draft, then publish.** Board edits live in a server-side draft for each tenant and service day (NEW). They do not touch `mvp_load_plans` or order status until Publish. Publish reuses the existing apply → dispatch path (`LoadingPlanExecutor`, then `FuelPlanDispatchService`), which already notifies the driver app.
- **R8. Concurrency.** Each lane has a version, and each board command carries the lane version it expects. The server is authoritative and serializes per lane. The client applies changes optimistically and rolls back on conflict. Tenant-scoped WebSocket events push other dispatchers' changes (see 1.5).
- **R9. AI plans as a suggestion layer.** Agent loading and route plans appear as ghost cards with a diff against the draft. Dispatchers can accept per load, per lane or all at once, adjust, or reject. Accepted suggestions go through the same validation.

Top reuse:

- **Validators:**
  - `DriverQualificationService.is_dispatch_eligible`
  - `HOSChecker.is_eligible` and `HOSAdvisoryService.gate_verdict`
  - `AssetCertificationService.is_dispatch_eligible`
  - `check_compatibility` and `check_feasibility`
  - `DyedDieselEnforcer`
  - `assert_window_present_for_transition`
- **Commit path:** `LoadingPlanExecutor`, `FuelPlanDispatchService`, `PLAN_EXECUTION_LOCK`, and the guarded order writes.
- **Agent outputs and diff models:** `replan_diff_models.py` and `ReplanDiffPanel`.
- **Realtime:** WebSocket hooks and `broadcast_to_tenant`.
- **Frontend:** `components/ui` primitives, toasts, `useDialogA11y`, the compartment endpoints and the gallons helpers.

Top gaps (all NEW):

- A board snapshot read.
- A dry-run validate endpoint, with the agent-private checks extracted into a shared service.
- A draft store with lane commands.
- Shift-scoped driver-to-truck pairing that dispatchers are allowed to do.
- A service-day/shift and load-sequence identity on plans.
- A dispatcher-readable HOS figure.
- Post-publish reassignment that revokes fuel orders from the driver app.
- A fix to the driver manifest resolver, which today picks the newest plan for a truck in any status (G9).

## Method and limits

- **Code.**
  - I read the frontend (`runsheet/src`), backend (`Runsheet-backend`), driver app (`driver-app`), the listed `.kiro/specs/*`, and `.agents/tasks/loading-plan-executor/`.
  - Every path, class, function and route below was opened or grepped in this checkout. Line numbers are the definition lines at `ad72dde`.
  - Anything not present is marked **NEW**.
- **Web.**
  - Pages were fetched with curl. DuckDuckGo, Bing and Mojeek returned bot challenges or degraded results, so sources were fetched from known URLs, not discovered by search.
  - Package facts come from the npm registry JSON [S32].
  - Findings about vendor UIs come from their public product pages. They are vendor claims, not observed behaviour, because the products are behind demos.
- **Not found or not reachable:**
  - "Ascend": `ascendsoftware.com` is an accounts-payable product, and I found no petroleum dispatch page.
  - "Gray Matter": I could not identify a fuel-dispatch product under that name.
  - Routific: the features page returned 404 and the home page has no DnD detail.
  - Motive: the dispatch page returned 404.
  - Samsara KB, Onfleet support, FMCSA and Salesforce UX on Medium: 403 bot walls. eCFR was used instead of FMCSA.
- **Not verified:** No library was installed or run. Library compatibility with Next 15.5.22, React 19.1.0 and Turbopack dev is inferred from peer ranges, dependency lists and issue trackers. A short spike should confirm it (OQ1).

## Part 1: External research

### 1.1 Fuel and delivery dispatch software

| # | Finding (paraphrased) | How it shapes the design |
|---|---|---|
| F1 | PDI's fuel logistics suite pitches forecasting "optimal loads" and the delivery windows that maximize gallons per load. It promises fewer split loads and fewer preventable run-outs, and visibility of available drivers and vehicles with live field status [S1]. | Show gallons-per-load utilization and runout risk on cards and lanes. Show available drivers and trucks as first-class trays, not hidden in a picker. |
| F2 | Cargas Energy's dispatcher tools: one place for automatic, scheduled, will-call and monitored tickets; build routes visually on a map; dispatch to the driver's device; track progress; change routes already dispatched [S2]. | Put all call types (`keep_full`, `auto_fill`, `will_call`, `one_off`) in one tray with filter chips. Include a map preview of the lane. Allow edits after publish (G8). |
| F3 | ADD Systems (wholesale petroleum): orders are "assigned and dispatched based on fleet or carrier availability", alongside best-buy supply-point selection [S3]. | The terminal or supply point is part of a load, so the validation should include terminal/supply checks (sourcing recommender, terminal wait). |
| F4 | Vertrax sells one ERP plus dispatch platform for oil and propane. Its pitch is one source of truth instead of patched-together systems [S4]. | The board should be the dispatch surface over the same orders and plans, not a parallel data model. This argues for reusing `fuel_orders_current` and `mvp_load_plans`. |
| F5 | Samsara: "Most fleets plan routes in one tool, dispatch in another, and navigate in a third". Planners "see the impact on duration, miles, and cost before they commit" [S6]. | Preview before commit is the core interaction. Every hover shows the impact (miles, duration, fill, HOS) before the drop is committed (R5). |
| F6 | Onfleet stresses manual override and dynamic control: reassign deliveries, adjust routes, intervene live, plus automatic flagging of delays and failures [S7]. | Automation proposes and the dispatcher can always override. Exceptions (late, failed, HOS risk) show as lane badges that link to the fix. |
| F7 | OptimoRoute's "Intelligent Drag & Drop Timeline" [S8][S9]:<br>• Drop an order on a driver's *name* and it goes into the best position in that schedule.<br>• Move multiple, grouped or unscheduled orders.<br>• Undo and redo.<br>• The plan recalculates instantly on drag.<br>• The driver app updates right after the change. | Two drop modes: (a) on the lane header means best-fit insertion, computed by the server; (b) on a position in the lane means exact placement. Add multi-select drag, undo/redo of draft commands, and instant recompute through the validate endpoint. |
| F8 | ServiceTitan's dispatch board matches technicians to jobs "with a simple drag-and-drop", with a configurable schedule view [S10]. | Supports a resource × time board as the dominant field-service pattern. Lane density and visible fields should be configurable. |
| F9 | Descartes markets route planning, optimization and dispatch with real-time dispatch visibility [S5]. Its public page has no UI detail about compartments. | Compartment UX could not be benchmarked from public sources. The compartment fill design is derived from our domain model (`Compartment`, `CompartmentAssignment`) and the truck-compartment-loading spec. |

Dispatcher pain points drawn from these sources: tool switching (F4, F5), last-minute changes such as sick drivers, breakdowns and traffic (F6, F7), split loads and run-outs (F1), and having to replan everything for one small change (F7). Each one maps to a board capability: one surface, fast re-drag, a fill gauge with runout badges, and local edits that do not trigger a full replan.

### 1.2 Board and timeline interaction patterns

| # | Finding | How it shapes the design |
|---|---|---|
| F10 | Resource schedulers put one resource per row with time running left to right. Bryntum Scheduler offers "drag-and-drop event scheduling for resources" and lists flight dispatch and travel-time examples [S11]. DHTMLX's Timeline view shows events horizontally, with one timeline per section (y-axis) [S12]. DHTMLX has a separate "Units" view for per-resource columns [S12]. | This is the basis for R2 (rows). Columns stay an option only for a small "compare 2–4 trucks" mode, not the default. |
| F11 | DHTMLX Timeline "smart rendering" draws only visible rows, columns and events [S12]. Pragmatic advertises virtualization support [S23]. | Virtualize lanes vertically once there are more than about 30 trucks. The DnD library must keep working when drop targets mount and unmount (Pragmatic: "Can add, remove, or change drop targets while dragging" [S23]). |
| F12 | Linear's board view [S13]:<br>• Keyboard shortcuts move items to the top or bottom "to avoid dragging … up or down long columns".<br>• Items can be dragged into hidden columns.<br>• Shift X / Shift Click selects multiple items. | Keyboard and menu shortcuts are first-class ("Move to first stop", "Move to Truck 12"). Collapsed lanes still accept drops. Multi-select moves several orders in one command. |
| F13 | NN/g on drag and drop [S14]:<br>• Use clear signifiers (handles, grab cursor) and feedback at every stage.<br>• Use magnetism: drop zones grow and highlight and preview the snap position before the pointer arrives.<br>• Grab handles must be reachable by Tab.<br>• On touch, leave at least 1 cm × 1 cm of free space and start drags with a long press. | Cards get a visible grip and a grab cursor. Lanes highlight and show an insertion line plus a validation chip before the drop. The insertion position snaps to the nearest stop gap. On tablets, a long press starts the drag. |
| F14 | Atlassian moved away from react-beautiful-dnd's movement-based placement (shifting siblings), which "does not work well for other types of interfaces" and "can also feel slow". It now uses drop indicators that are cheap for the browser to render [S27]. | Do not reflow the timeline while dragging. Show a thin insertion indicator and a ghost of the drop result, and reflow only after the server accepts the drop. |
| F15 | NN/g heuristics [S15]:<br>• *User control and freedom* calls for clearly marked exits and undo.<br>• *Error prevention* says to remove error-prone conditions or ask for confirmation before commit. | Undo and redo the draft commands. Prevent invalid states up front (block tier) instead of explaining them afterwards. Confirm only at Publish. |
| F16 | NN/g on confirmation dialogs [S16]: no confirmations for routine actions, because people learn to click through. Be specific about consequences. Prefer undo. | Drops never open a dialog. A dialog appears only for overriding a warning (it needs a reason) and for Publish. The Publish dialog names the drivers who will be notified and the orders that will move to dispatched. |

### 1.3 Accessible drag and drop

| # | Finding | How it shapes the design |
|---|---|---|
| F17 | WCAG 2.2 SC 2.5.7 Dragging Movements (AA) [S17]:<br>• All dragging functionality must also be achievable "by a single pointer without dragging".<br>• Keyboard support does not satisfy it on its own.<br>• A text input or menu can count as the single-pointer alternative. | Every drag has a click path: open the card menu, choose "Assign to…", then pick a truck. Or select a card and then click a lane slot. This is required whichever library is used. |
| F18 | SC 2.5.8 Target Size (Minimum) (AA): pointer targets at least 24×24 CSS px, or enough spacing [S18]. | Card handles, lane-header slots, menu buttons and the compartment cells used as drop targets are at least 24 px. Dense mode cannot shrink below this. |
| F19 | MDN on live regions [S19]:<br>• `aria-live="polite"` is the normal setting; keep `assertive` for critical messages.<br>• Create the live region before you update it.<br>• Announcements are plain text. | One page-level polite region, mounted at load, announces every outcome. Example: "Order QA-123, 3,000 gal diesel, assigned to Truck 12, stop 3. Compartment 2 is 92% full. Warning: delivery window at risk." Assertive is used only for publish failure. |
| F20 | Pragmatic accessibility guidelines [S24]:<br>• Trigger outcomes with visible buttons and menus.<br>• Announce what happened through the live region, including item and old/new position.<br>• Return focus to the trigger.<br>• Avoid arrow-key "directional" dragging: it is hard to follow without sight, JAWS needs a mode change, and complex moves take many keystrokes. | Adopt the menu model rather than arrow-key dragging. Focus returns to the moved card in its new lane. Announcements say from where to where. |
| F21 | React Aria DnD [S31] gives keyboard and screen-reader parity:<br>• Enter starts a drag, Tab cycles only through *drop targets that accept the data*, Enter drops, Escape cancels.<br>• Touch screen-reader users double-tap, swipe and double-tap.<br>• An explicit drag affordance avoids clashes with selection. | This is the benchmark for the menu flow: the "Assign to…" list shows only the trucks that pass the block tier, so the keyboard path cannot choose an invalid target. It is the fallback library if accessibility review rejects the menu-only approach. |
| F22 | dnd-kit's (legacy) keyboard sensor [S20]: Space or Enter picks up, arrow keys move, Space or Enter drops, Escape cancels. Activators get `tabindex=0`. Screen-reader instructions and announcements are customizable. | Rejected as the primary keyboard model for a timeline, per F20: arrow movement across lanes and times takes too many keystrokes. Escape-to-cancel applies to every input. |

The full WCAG conformance claim still needs manual testing with assistive technologies and expert review.

### 1.4 DnD library choice

The project has no DnD library installed and no `draggable`/`onDragStart` usage (checked `runsheet/package.json` and grepped `runsheet/src`). It runs `next` 15.5.22, `react` and `react-dom` 19.1.0, `reactStrictMode: false` (`runsheet/next.config.ts` L10), Turbopack dev, Tailwind 4, Biome, Jest and Playwright.

| Library (npm latest, published) | Fit | Risks |
|---|---|---|
| **`@atlaskit/pragmatic-drag-and-drop` 4.0.0 (2026-09-24)**, plus `hitbox` 3.0.0, `auto-scroll` 3.2.1, `live-region` 2.1.0 [S32] | Fits well:<br>• Headless, framework-agnostic, "~4.7kB core".<br>• Supports conditional dropping (maps onto our `canDrop` and validation), sticky targets, auto-scroll, virtualization, and changing drop targets mid-drag.<br>• Full support in Firefox, Safari, Chrome, iOS and Android [S23].<br>• Powers Trello, Jira and Confluence [S26].<br>• Core depends only on `raf-schd`, `bind-event-listener` and `@babel/runtime`, with no React peer, so React 19 coupling is not a concern [S32].<br>• Its accessibility guidance matches R4 [S24]. | • Uses the native HTML5 drag preview: browser-applied opacity and shadow, and Windows dims previews larger than 280 px [S25]. Mitigation: a small custom preview chip, with the rich feedback drawn on the drop target.<br>• Three majors in 2026 (2.0.0 Jun, 3.0.0 Aug, 4.0.0 Sep), all entry-point and export-path reshuffles [S28][S32]. Mitigation: pin exact versions and use the new subpaths.<br>• `react-accessibility` 3.2.4 pulls `@emotion/react` and Atlaskit primitives, and `react-drop-indicator` pulls `@compiled/react` [S32]. Mitigation: do not use those two packages. |
| `@dnd-kit/core` 6.3.1 (2024-12-05) and `@dnd-kit/sortable` 10.0.0 [S32] | Mature React API with a keyboard sensor, `DragOverlay` (live React preview) and announcements [S20]. | • The docs now file it under "Legacy" [S20][S21].<br>• No release since 2024-12-05 [S32].<br>• Open issues titled "Support React 19 & Nextjs 15" and "With React 19 typings: Cannot find namespace 'JSX'" [S22]. |
| `@dnd-kit/react` 0.5.0 (2026-06-11) [S32] | New framework-agnostic architecture [S21]. | • Pre-1.0. 0.4.0 (Apr 2026) redesigned the event types [S21].<br>• Open issue "React 19: DragDropProvider manager is destroyed during Strict Mode replay" [S22]. |
| `react-aria-components` 1.21.1 (2026-09-04), React peer `^19.0.0-rc.1` included [S32] | Best built-in keyboard and screen-reader parity (F21) [S31]. | • Built around collection components (GridList, Table). Timeline x-position drops are custom.<br>• Adds a second component system next to our Tailwind `components/ui`. |
| `react-dnd` 16.0.1 (2022-04-19) [S32] | Separate HTML5 and touch backends [S30]. | Unmaintained for 4+ years. |
| `@hello-pangea/dnd` 18.0.1 (2025-02-09) [S32] | Community fork of react-beautiful-dnd. | Lists and lists-of-lists only, with movement-based placement (F14). It does not fit a timeline. `react-beautiful-dnd` itself is deprecated and archived [S29]. |

The recommendation is Pragmatic (R3). It is the only option that is actively maintained, React-version-independent, proven on large boards, and already aligned with our accessibility approach. Its main cost, the native preview, does not matter here because feedback belongs on the target (F14). The first implementation task should be a one-day spike in `runsheet` under Next 15.5 and Turbopack. It should prove four things:

- a tray → lane drop with a time-position read from `location.current.input`
- auto-scroll inside a horizontally and vertically scrolling board
- iPad Safari long-press
- jsdom compatibility for Jest, with the drag paths covered by Playwright

### 1.5 Optimistic UI and multi-user conflicts

| # | Finding | How it shapes the design |
|---|---|---|
| F23 | React 19 `useOptimistic` shows a temporary state while an Action runs and falls back to the real value when it settles. Setting it outside a Transition reverts immediately [S33]. | A drop is a React 19 Action. It shows the optimistic lane state (card in place, "checking…" chip) and settles to the server's lane. No new state library: the project has none and does not need TanStack Query or Zustand for this. |
| F24 | TanStack Query [S34]:<br>• Optimistic updates can be applied through the UI from the mutation variables (no rollback needed) or by editing the cache (rollback through the `onMutate` context).<br>• TkDodo: cancel in-flight refetches so they don't overwrite the optimistic state, and skip invalidation while related mutations are still running, or the UI flickers between old and new [S35]. | While a lane command is pending, ignore or queue WebSocket and poll refreshes for that lane. Refetch the lane once, after the last pending command settles. Drafts are server documents, so "rollback" means re-rendering the server's lane. |
| F25 | Figma's multiplayer [S36]:<br>• The server is the central authority.<br>• Conflicts are resolved last-writer-wins per object *property*, so edits to different properties don't conflict.<br>• Clients keep showing their own unacknowledged values so they don't flicker.<br>• The server rejects changes that would break invariants (parent cycles). | Our invariants (capacity, segregation, HOS) span a whole lane, so per-property LWW would let two dispatchers jointly overfill a truck. We use per-lane compare-and-set: each command carries `expected_lane_version`, and the server serializes per lane and re-validates. Unacknowledged local commands render over server pushes until they are acknowledged or rejected. |
| F26 | The repo already serializes plan execution per tenant with `PLAN_EXECUTION_LOCK` (`Runsheet-backend/persistence/plan_execution_lock.py` L217) and row-locked `atomic_update` (`persistence/document_store.py` L378, delegated by `services/elasticsearch_service.py` L355). | Reuse them: lane commands run as `atomic_update` on the draft document, and Publish runs under `PLAN_EXECUTION_LOCK`. This follows the standing "serialize instead of racing" rule. |

Presence: show who else has the board open, and a soft "Ana is editing Truck 12" marker on the lane (an advisory, not a lock). This uses the existing tenant-scoped WebSocket infrastructure (`websocket/base_ws_manager.py` `broadcast_to_tenant` L148).

### 1.6 AI suggestions

| # | Finding | How it shapes the design |
|---|---|---|
| F27 | Google PAIR, Explainability + Trust [S37]: help users *calibrate* trust, so they know when to rely on the system and when to use their own judgement. Plan for calibration over time and optimize for understanding. | Each suggested load shows why it was suggested: priority bucket and score, runout forecast, the window it meets, and its fill. Each also shows where it disagrees with the current draft. Accepting a suggestion does not skip validation. |
| F28 | OptimoRoute users send routes for approval, then move single orders by dragging instead of re-planning [S8]. | Accept the whole plan, then fine-tune by dragging. Partial accept (one load, one lane) is first-class. |

### 1.7 Regulatory rules that drive the hard checks

| # | Finding | How it shapes the design |
|---|---|---|
| F29 | 49 CFR 395.3 [S38]: no driving after the 14th consecutive hour after coming on duty (following 10 hours off); a maximum of 11 hours driving; a 30-minute break is required after 8 cumulative hours of driving. | Lanes shade the driver's 14-hour window and mark the 11-hour driving limit. A load that overruns it is a `warn` when the HOS data is advisory or stale, and a `block` when gating is enabled and the reading is fresh. This follows `HOSAdvisoryService.gate_verdict` outcomes (`passed`/`blocked`/`skipped`). |
| F30 | 49 CFR 383.93 [S39]: a CDL endorsement is required for tank vehicles (N) and for hazardous materials (H). | Driver-to-truck and order-to-lane drops check endorsements through `DriverQualificationService.is_dispatch_eligible` with `requires_tanker`, `requires_hazmat` and `min_cdl_class`. This is the block tier. |

### 1.8 Why not a node-graph "playground"

React Flow (`@xyflow/react` 12.12.0) is "a customizable React component for building node-based editors and interactive diagrams" [S40][S32]. A graph editor lets users create any edge: an order wired to two trucks, a driver wired to nothing, a stop before its terminal lift. Dispatch has a fixed shape: tenant → service day → truck lane → load (terminal lift, compartments) → ordered stops, with one driver per lane. Free wiring would let dispatchers build invalid structures and then need a validator to explain them, which goes against error prevention (F15). The board keeps the direct-manipulation feel but only offers drop targets that exist in that shape.

## Part 2: Lane orientation decision

**Decision: rows.** One horizontal lane per truck, with time on the x-axis for the selected shift.

- **Scale.** Tenant fleets have tens of trucks. Rows scroll and virtualize vertically (F11). Columns run out of width at about 6–8 trucks on a 1440 px screen once a 320 px tray and a detail drawer are on screen.
- **Convention.** Resource schedulers and dispatch timelines use rows × time (F7, F8, F10). DHTMLX keeps per-resource columns for a separate Units view (F10).
- **Reading order.** A fuel load reads left to right: terminal lift → stop 1 → stop n → return. The 14-hour HOS window is a horizontal span (F29).
- **Lane header.** The left header column holds the truck id and type, the driver slot (the drop target for driver chips), a compact compartment gauge for the selected load, and lane badges (HOS, cert, cleaning). Everything about the truck sits in one fixed place.
- **Zoom levels.**
  - *Timeline*: positioned by ETA from the route solver.
  - *Sequence*: evenly spaced cards in stop order, for planning before ETAs exist or when routing is degraded.
  - Both use the same drop targets.
- **Small-screen fallback.** Below about 1024 px wide, lanes collapse to a vertical list per truck in sequence mode, and the click-to-assign path is primary.

## Part 3: Invalid-drop policy (proposal for design)

Every check returns `{check, outcome: pass|warn|block, reason_code, message, source}`. The design should keep these tiers unless review overturns them.

- **Block, with no override.** The drop is refused and the target shows a red chip with the reason. In the "Assign to…" menu the target is disabled, and its accessible name includes the reason.
  - The product is not allowed in any compartment, or the segregation key conflicts (`compartment_accepts`, `check_feasibility` `no_compatible_compartments`).
  - The compatibility rule `blocked` applies (`check_compatibility`).
  - Total capacity or product capacity is short, or weight is exceeded (`check_feasibility` `total_overage`, `capacity_shortfall`, `weight_exceeded`).
  - The driver is not `active`, or the CDL, medical card, HAZMAT or tanker endorsement is missing or expired, or the CDL class is below the requirement (`is_dispatch_eligible`).
  - An asset certification has expired (`AssetCertificationService.is_dispatch_eligible`).
  - Dyed diesel is going to an ineligible customer (`DyedDieselEnforcer.validate_order` / `validate_load_plan`).
  - The order is terminal, on hold, or committed to another run (`order_committed_elsewhere`, as in `FuelPlanDispatchService`).
  - The order has no delivery window but needs one to become `scheduled` (`assert_window_present_for_transition`).
  - The HOS gate is `blocked` with no active override.
  - Rationale: these are regulatory or physical. An override would only push the failure to the terminal or the roadside. This matches error prevention (F15).
- **Warn, overridable with a reason.** The drop lands with an amber chip. The dispatcher can keep it, but Publish needs a reason for each open warning. The reason is stored on the draft and copied to the order events with the actor taken from the session.
  - The compatibility rule `requires_cleaning` (the dispatcher confirms a flush is planned).
  - ETA after the delivery window end.
  - HOS projected over the limit while the reading is stale, unknown or advisory-only.
  - Fill below the minimum drop (`below_min_drop`).
  - Long terminal wait, or a supply contract near its lift limit.
  - Precedent: `HOSGateOverride` (`driver/services/hos_advisory_service.py` L582) requires a non-blank reason and a future `expires_at`, and takes `actor_id` from the verified session (`POST /api/driver/hos/override`, `driver/api/hos_endpoints.py` L295).
- **Info, no action needed.** "A better slot exists on Truck 7 (−18 mi)", low utilization, an AI suggestion that disagrees.
- **Unknown.** A validator was unavailable (for example, the HOS data source is down). This shows as `warn` with reason `check_unavailable`, never as `pass`. The design should decide whether Publish needs an override for it (OQ5).

## Part 4: Codebase reuse map

### 4.1 Frontend (`runsheet/src`)

| Asset | Path | Reuse as |
|---|---|---|
| Dispatch route and tab shell | `app/dashboard/dispatch/page.tsx` (lazy `DispatchPage`), `components/DispatchPage.tsx` (tabs "Scheduling", "Fuel Distribution"), nav id `dispatch` in `components/Sidebar.tsx` L60 | Mount point. Add a "Board" tab (NEW) as the default, and keep the existing tabs. |
| Fuel plan dashboard | `components/ops/FuelDistributionPage.tsx`: `PlansTab` L2069, `PlanDetailView` L1747, `ExecutionProgress` L432, `ReplanDiffPanel` L1590 / `ReplanDiffBody` L1483, `RejectDialog` L217, `EmergencyStopModal` L1048 | Plan detail and execution progress for published lanes. The diff renderer is the basis for the AI suggestion diff. The reject and emergency-stop dialogs can be reused. The file is 3,673 lines, so pull shared pieces out instead of adding to it. |
| Today cockpit | `components/DispatchCockpit.tsx` (severity feed, `assignDriver`, `DriverPicker`) | Patterns for severity ranking and WebSocket-debounced refresh (`WS_REFRESH_DEBOUNCE_MS`). Link cockpit items into the board. |
| Compartments | `components/ops/TruckCompartmentsPage.tsx` (state badges clean/loaded/needs_cleaning); `services/fuelApi.ts`: `listTruckCompartments` L2591, `listCompartmentTrucks` L2607, `checkCompartmentLoadEligibility` L2850, `litersToGallons` L55, `getAssignmentQuantityGallons` L631, `getAssignmentCapacityGallons` L643 | Compartment gauge and badges in lane headers. Show gallons in the UI because storage is litres. Per-compartment eligibility preview. |
| Plans API client | `services/fuelApi.ts`: `listPlans` L899, `getPlan` L737, `approvePlan` L910, `rejectPlan` L926, `getReplanDiff` L1427, `generatePlan` L727 | Suggestion layer and publish (through approve). |
| Orders API client | `services/ordersApi.ts`: `listOrders` L302, `getOrder` L318, `holdOrder` L398, `releaseHoldOrder` L419, `cancelOrder` L384 | Card actions in the tray. Not used for assignment (see G2). |
| Drivers and fleet | `services/api.ts` `getDriverUtilization` L448 (`GET /api/ops/drivers/utilization`), `getTrucks` L569, `getAssets` L591; `services/complianceApi.ts` `getDrivers` L600; `components/ops/DriverPicker.tsx`, `AssetPicker.tsx` | Driver tray (status, assigned truck, qualification warnings, on-duty minutes) and truck tray. |
| Approvals | `services/agentApi.ts` `getApprovals` L224, `approveAction` L241, `rejectAction` L253; `components/ops/ApprovalQueue.tsx` | Accept or reject agent `apply_loading_plan` suggestions. |
| Realtime | `hooks/useWebSocket.ts` (reconnect with backoff, `getUrl` token refresh), `useOrdersWebSocket.ts` (`order_placed`, `order_status_changed`, `order_assigned`), `usePlanExecutionSocket.ts` (`execution_update`), `useFuelPlanningWebSocket.ts` (`replan_diff_ready`, `emergency_stop_inserted`, `cross_contamination_violation`, storm mode, sourcing), `useAgentWebSocket.ts` (`approval_*`) | Live tray and lane refresh. A NEW `useDispatchBoardSocket` handles lane events and presence. |
| Design system | `components/ui/index.ts` (`Badge`, `Button`, `Card`, `EmptyState`, `Modal`/`ModalFooter`, `PageHeader`, `SearchableSelect`, `TabNavigation`, `FilterBar`, `LoadErrorState`, `StatsBar`, `EntityLink`); `components/ui/toast/index.tsx` `useToasts` L74; `hooks/useDialogA11y.ts` L17; tokens in `app/globals.css` (`--rs-color-*`, AA-safe primary `#0b7d5e`, success, warning and error families) | All board chrome. Use the success, warning and error tokens for pass/warn/block. Drawers and dialogs use `useDialogA11y`. Toasts are for conflicts and publish results. |
| Module and role gating | `config/modules.ts` (`Role` L33, `canSee`) | Gate the Board tab to `admin` and `dispatcher`. |
| Maps | `components/MapView.tsx`, `components/ops/OperationsMap.tsx` (`@vis.gl/react-google-maps`) | Lane route preview in the detail drawer. |
| Test stack | Jest + RTL + jsdom, Playwright (`package.json` scripts), Biome | RTL covers the menu and keyboard paths and the live-region text. Playwright covers real pointer drags. |

There is no existing board, timeline or Gantt component. `components/ops/JobBoard.tsx` is a sortable table of legacy `Job`s, not a kanban.

### 4.2 Backend validators

| Check | Service and path | How it is exposed today | Board use |
|---|---|---|---|
| Driver qualification (status, CDL, medical card, HAZMAT/tanker endorsements, CDL class) | `DriverQualificationService.is_dispatch_eligible(tenant_id, driver_id, route_requirements)`, `compliance/services/driver_qualification_service.py` L579; returns `DriverEligibility{eligible, reasons}` | Self-only for drivers (`GET /api/driver/qualifications`, `driver/api/qualification_endpoints.py` L221). A summary is joined into `GET /api/ops/drivers/{id}/profile` (`fuel/api/driver_endpoints.py`, `_resolve_qualification_summary`). Used by the route planner and the driver transition gate stack (`driver/services/order_transition_service.py`). | Call it from the NEW validate service. |
| Route requirements derivation | `RoutePlanningAgent._build_route_requirements`, `Agents/overlay/route_planning_agent.py` L1624 | Private agent method | Extract into the NEW shared `fuel/services/dispatch_validation.py` so the agent and the board agree. |
| HOS projection | `HOSChecker.is_eligible(driver_id, estimated_drive_hours, estimated_total_hours, hos_exemption)`, `compliance/services/hos_checker.py` L211; the estimate comes from `RoutePlanningAgent._estimate_route_hours` L1698 (stop-count heuristic) and `_check_hos_eligibility` L1729 | Private to the agent. The signature has no `tenant_id`, which the design must account for. | Extract. Prefer the solver's route duration over the heuristic when it is available. |
| HOS gate and override | `HOSAdvisoryService.resolve` L666, `gate_verdict` L804, `record_override` L1036 (`driver/services/hos_advisory_service.py`) | `GET /api/driver/hos` is self-only (`driver/api/hos_endpoints.py` L190). `POST /api/driver/hos/override` is open to dispatcher and admin (L295). | Gate outcome per lane. A dispatcher-readable HOS figure is G6. |
| Asset certification | `AssetCertificationService.is_dispatch_eligible(tenant_id, asset_id)`, `compliance/services/asset_certification_service.py` L809 | Used by the route planner (`_check_asset_certification` L1837) | Validate truck-in-lane. |
| Compartment compatibility | `check_compatibility` L340 and `load_tenant_compatibility_rules` L444, `fuel/services/compatibility_matrix.py`; decisions `allowed`/`blocked`/`requires_cleaning` (L99) | `GET /api/fuel/mvp/compartments/{compartment_id}/load-eligibility?product_code=` (`fuel/api/fuel_ops_endpoints.py` L3022). The loading agent uses it in `_evaluate_assignment_compatibility` L2547 and `_enforce_cross_contamination` L2389. | Per-compartment cell state while dragging. Block or warn tier. |
| Capacity, segregation, weight, minimum drop | `check_feasibility` L270, `compartment_accepts` L204, `segregation_key` L169, `optimize_loading_plan` L362, `allocate_across_trucks` L569, all in `Agents/support/compartment_solver.py`; models in `Agents/support/compartment_models.py` (`Compartment`, `DeliveryRequest`, `LoadingPlan`, `FeasibilityResult`, `ConstraintViolation`) | Pure functions, used by the loading agent | Fill visualization and auto-allocation of an order into compartments when it is dropped on a lane. Violation types map directly onto reason codes. |
| Dyed diesel | `DyedDieselEnforcer.validate_order` L378, `validate_load_plan` L475, `compliance/services/dyed_diesel_enforcer.py` | Intake and the loading agent | Block tier. |
| Delivery filter (call type) | `DeliveryFilter.partition_candidates` L132, `compliance/services/delivery_filter.py` | Route planner | Tray grouping and exclusions. |
| Status and window guards | `assert_transition` L50, `assert_window_present_for_transition` L68, `is_terminal_status` L98 in `fuel/order_state_machine.py`; there is no reverse transition `scheduled → confirmed` (L22–30) | Order service | Precheck at drop time. Explains why the board needs a draft (R7). |
| Route geometry and SLA | `Agents/support/route_solver.py`: `optimize_route` L165, `check_sla_windows` L118, `insert_emergency_stop` L703, `_recompute_etas` L652 | Route agent and emergency-stop endpoint | ETAs and window checks for a proposed sequence, and best-fit insertion (F7). |
| Terminal and supply | `GET /api/fuel/sourcing/recommendations`, `GET /api/fuel/terminals/{id}/wait-summary`, `GET /api/fuel/rack-prices` (`fuel/api/fuel_ops_endpoints.py`, `router` prefix `/api/fuel` L424); `fuel/services/sourcing_recommender.py`, `terminal_wait_resolver.py`, `contract_lift_service.py` | Read endpoints | Terminal choice per load, and the warn tier for long waits or contract limits. |

### 4.3 Commit and dispatch path

| Step | Path | Reuse |
|---|---|---|
| Apply a plan to `scheduled` (links truck and run, all or nothing, idempotent, audited) | `LoadingPlanExecutor.execute` L396 and `resolve_mode` L366 (`fuel/services/loading_plan_executor.py`); entered through `ApprovalQueueService.approve` / `ConfirmationProtocol` (`Agents/approval_queue_service.py`, `Agents/confirmation_protocol.py`). Design: `.agents/tasks/loading-plan-executor/design.md` | The board's Publish calls the executor for each lane-load. The executor needs an entry point that does not go through the approval queue (OQ3). |
| Dispatch (resolve the driver, link, move to `dispatched`, create executions, notify the driver) | `FuelPlanDispatchService.dispatch` L148 (`fuel/services/plan_dispatch_service.py`); `POST /api/fuel/mvp/plan/{plan_id}/approve` (`Agents/support/mvp_endpoints.py` L806, accepts `draft`/`proposed`/`scheduled`/`dispatched`) | Publish step 2. It is idempotent on retry. |
| Order writes | `OrderService.apply_status_transition` L200 (`fuel/services/order_service.py`); `FuelOrderRepository.get_current` L361, `claim_assignment` L384, `release_assignment` L471, guarded `upsert_with_last_event_timestamp` L574, `append_event` L1153 (`fuel/order_repository.py`) | All board-originated order changes go through these, never through `PATCH /api/orders/{id}/assign` (G2). |
| Serialization | `PLAN_EXECUTION_LOCK` (`persistence/plan_execution_lock.py` L217); `atomic_update` (`persistence/document_store.py` L378) | Publish lock and the lane-command compare-and-set. |
| Plan and route documents | `mvp_load_plans` / `mvp_routes` mappings (`Agents/support/mvp_es_mappings.py` L130 / L195); `GET /api/fuel/mvp/plans` (status filter only, L752); `GET /api/fuel/mvp/plan/{id}` L314; reject L906 | The published result of a board lane-load. There is no service-day, shift or driver field (G5). |
| Execution tracking | `PlanExecutionService` (`Agents/support/plan_execution_service.py`), `/ws/plan-execution` | Lane progress after publish. |
| Replan and emergency stop | `GET /api/fuel/mvp/replans/{event_id}/diff` (L3502), `POST /api/fuel/mvp/routes/{route_id}/emergency-stop` (L4837), `Agents/overlay/exception_replanning_agent.py` | Post-publish lane edits and suggestion diffs. |

### 4.4 AI agents

| Agent | Path | Output the board consumes |
|---|---|---|
| Delivery prioritization (`agent_id` `delivery_prioritization`) | `Agents/overlay/delivery_prioritization_agent.py` L112; reads `fuel_orders_current` in `placed`/`confirmed`/`scheduled`; scores by call type (forecast for `keep_full`/`auto_fill`, window urgency for `will_call`/`one_off`) | Tray sort order, priority bucket badges, and the "why" text (F27). `GET /api/fuel/mvp/priorities`. |
| Compartment loading (`compartment_loading`) | `Agents/overlay/compartment_loading_agent.py` (`_build_proposal` L1868, unassigned-order reporting L679) | Suggested loads (`mvp_load_plans` `proposed`/`draft`) and `apply_loading_plan` approvals. |
| Route planning (`route_planning`) | `Agents/overlay/route_planning_agent.py` L462 (driver, HOS, cert and delivery-filter wiring L722–779; `_persist_route_plan` L4010) | Suggested stop sequences and ETAs (`mvp_routes`). |
| Diff models | `Agents/support/replan_diff_models.py` (`StopRef` L55, `ReorderedStop` L99, `ReassignedStop` L109, `QuantityChange` L119, `EtaShift` L137, `ReplanDiff` L169) | The diff between a suggestion and the draft: same vocabulary, NEW producer. |
| Flags | `overlay.{agent_id}` via `FeatureFlagService.get_overlay_state` L257 / `set_overlay_state` L330 (`ops/services/feature_flags.py`); default `overlay_default_mode="disabled"` (`config/settings.py` L146) | The suggestion layer is visible only when the agent's mode produces plans. Shadow-mode visibility is OQ6. |

### 4.5 Realtime

- **Socket endpoints** (`bootstrap/websockets.py`): `/ws/orders` L400, `/ws/plan-execution` L464, `/ws/fuel-planning` L479, `/ws/agent-activity` L423, `/ws/driver` L451, `/api/fleet/live` L434.
- **Tenant broadcast:** `BaseWSManager.broadcast_to_tenant` (`websocket/base_ws_manager.py` L148).
- **Board events:** a NEW `board_lane_updated` / `board_presence` channel, or new event types on `/ws/fuel-planning` (`fuel/services/fuel_planning_ws_manager.py` `broadcast_event` L112).
- **Driver delivery** (`driver/ws/driver_ws_manager.py`):
  - `send_assignment` L253, `send_new_route` L261, `send_assignment_revoked` L285.
  - The dispatch service calls `send_assignment` (`plan_dispatch_service.py` L387).
  - The `order.dispatched` subscriber sends the driver push (`bootstrap/driver.py` L818).

### 4.6 Driver app

- **Work read.** `driver-app/lib/work-api.ts` reads `/api/driver/work?status=dispatched&status=in_transit`. `driver-app/types/order.ts` carries `compartment_manifest` (`CompartmentManifestEntry`, US gallons, `cross_contamination_warning`) and `stops` (`RouteStop`). `driver-app/lib/websocket.ts` connects to `/ws/driver`.
- **Publish needs no driver-app change** for a single load per truck. The app reads what dispatch writes.
- **Manifest resolution** (`driver/services/work_service.py`):
  - `_fetch_loading_plan` (L457) returns the newest `mvp_load_plans` document for the truck, matched only on `truck_id`, with no status or run filter.
  - `_fetch_route_plan` (L476) matches on `run_id` and `truck_id`.
  - So any newer plan for the truck becomes the driver's manifest, including an agent `proposed` plan or a later load the same day. This is G9. It already affects agent proposals today and becomes certain once the board lets dispatchers create several loads per truck per day.

### 4.7 Auth, tenancy, roles

- **Canonical roles:** `admin`, `dispatcher`, `driver`, `platform_admin` (`auth/supertokens_init.py` L109). Guard factory `roles_dependency` (`auth/router_guards.py` L43). Agent ops roles are `admin`, `dispatcher` (`Agents/api_authz.py` L64).
- **MVP router:** declares `ROUTER_AUTH_POLICY = "jwt_required"` with no role dependency (`Agents/support/mvp_endpoints.py` L64–66). Board endpoints (NEW) should use `roles_dependency("admin", "dispatcher")`.
- **Orders list:** `GET /api/orders` requires dispatcher or admin and caps `size` at 100, with a single `status` value (`fuel/api/order_endpoints.py` L791).
- **Driver-to-truck assignment is admin-only today.** `PATCH /api/ops/drivers/{id}` calls `_require_admin_role` (`fuel/api/driver_endpoints.py` L854, L277). Dispatch resolves the lane driver from `drivers_current.assigned_truck_id`.
- **Tenant scope:** `TenantContext` / `get_tenant_context` (`ops/middleware/tenant_guard.py`). Every board read and write is scoped to `tenant.tenant_id`, and actors come from `tenant.user_id`, never from the request body.

### 4.8 Specs that constrain the board

- **truck-compartment-loading** Req 1–5: data model, grade segregation, feasibility, optimization, constraint validation. Cells and violations must use these rules.
- **plan-execution-lifecycle** Req 2: approval and rejection. Req 3: execution tracking. Published lanes keep these semantics.
- **driver-mobile-app**:
  - Req 3.7 and 3.9: manifest by `assigned_asset_id`, route by `assigned_run_id` (see G9).
  - Req 12: qualification visibility.
  - Req 17: HOS advisory and dispatch gating (block versus advisory tiers, F29).
- **fuel-compliance-backbone** Req 4 (HOS in routing), 5 (driver qualification), 6 (dyed diesel), 13 (vehicle/tanker certification), 14 (will-call vs auto-fill at routing).
- **order-intake-pipeline:**
  - Req 2.5: order APIs.
  - Req 3.2: driver assignment and utilization (`active_order_count` counters).
  - Req 4.1: order and driver WebSocket channels.
  - Req 5.1–5.3: agent consumption of orders.
- **closed-loop-communications** Req 11: reassignment invalidation (`assignment_revoked` to the previous driver). Today it is implemented only for jobs (`scheduling/services/job_service.py` L547). That is G8.
- **agent-overlay-architecture** Req 9: shadow mode never submits proposals or triggers actions. Req 12.6: drain in-flight proposals on disable.
- **ui-scaffolding-consolidation** Req 1–2: one shared toast system and shared HTTP scaffolding. The board must use them.
- **operations-ui-coverage** Req 11 and 12, and **frontend-feature-parity** Req 12: navigation completeness, and consistent error and loading states (`LoadErrorState`).
- **logistics-scheduling** and **cross-domain-agent-integration** cover the legacy `Job` model. The board targets fuel orders, not jobs, which matches the loading-plan-executor decision D5.
- **multi-asset-tracking**, **ops-intelligence-layer** and **fuel-distribution-mvp** define asset types, the read-model patterns and the pipeline that produces suggestions.

### 4.9 Gaps (all NEW)

| ID | Gap | Evidence | Proposed NEW piece |
|---|---|---|---|
| G1 | No single board read. Orders are capped at 100 per page with one status. There is no "unassigned" filter, plans have no service-day filter, and drivers, trucks, plans and routes would need 6 or more calls. | `order_endpoints.py` L791; `mvp_endpoints.py` L752 | `GET /api/fuel/board?service_date=&shift=`, returning trays, lanes, the draft, suggestions and per-lane versions. |
| G2 | No dry-run validation for a proposed assignment. The checks are private agent methods or self-only endpoints. `PATCH /api/orders/{id}/assign` checks only driver availability and does an unguarded write. | 4.2; `order_endpoints.py` L1090 | `POST /api/fuel/board/validate` taking `{lane, proposed_change}` and returning per-check `pass/warn/block`. A shared `fuel/services/dispatch_validation.py` is extracted from `RoutePlanningAgent` so the agent and the board agree. |
| G3 | No draft store. Writing drafts as `mvp_load_plans` would leak into the driver manifest (G9), and a `scheduled` order cannot be un-scheduled. | `work_service.py` L457; `order_state_machine.py` L22–30 | A `dispatch_board_drafts` document per tenant and service day, holding lanes, loads, stops, overrides and lane versions. Commands `assign_order`, `move_stop`, `unassign`, `assign_driver`, `accept_suggestion`, `set_terminal`, `override_warning`, applied with `atomic_update` and an expected lane version. |
| G4 | Dispatchers cannot pair a driver with a truck. Only the admin-only permanent `assigned_truck_id` exists. | `driver_endpoints.py` L854 / L277 | A shift-scoped driver ↔ truck pairing on the draft. At Publish it is applied through a dispatcher-allowed path that the dispatch service resolves (OQ4). |
| G5 | Plans carry no service day, shift, load sequence, driver or source (agent or dispatcher). | `mvp_es_mappings.py` L130 field list | Additive mapping fields: `service_date`, `shift_id`, `load_seq`, `source`, `board_draft_id`. |
| G6 | No dispatcher-readable HOS remaining time per driver. | `hos_endpoints.py` L190 is self-only | Include `HOSAdvisoryService.resolve` figures in the board read and validate response. |
| G7 | No board realtime channel or presence. | 4.5 | Tenant-scoped `board_lane_updated` and `board_presence` events. |
| G8 | Reassigning after publish does not revoke the fuel order from the previous driver. | `driver_ws_manager.py` L285 is used only by `job_service.py` L547 | Post-publish moves call `send_assignment_revoked` and release the claim through the guarded repository path. |
| G9 | The driver manifest resolves the newest plan for the truck in any status. | `work_service.py` L457–474 | Resolve by the order's `assigned_run_id` (or the plan id recorded at dispatch) and restrict to applied or dispatched plans. Needed before multiple loads per day. |
| G10 | Publish with no approval entry. The executor is reached through `ApprovalQueueService.approve`. | `.agents/tasks/loading-plan-executor/design.md` K1 | A board publish service that builds plan and route documents from the draft and runs executor → dispatch under `PLAN_EXECUTION_LOCK`, reusing the executor's preflight. |
| G11 | No diff producer between a suggestion and the draft. | `replan_diff_models.py` only covers replan events | A pure function from (draft lane, suggested plan and route) to `ReplanDiff`-shaped output. |
| G12 | No DnD dependency. | `runsheet/package.json` | Pragmatic core, hitbox, auto-scroll and live-region, pinned to exact versions (medium-risk dependency change, to be noted at implementation). |

## Open questions for design

- **OQ1.** Spike Pragmatic under Next 15.5 / Turbopack / React 19.1: time-position drops, nested auto-scroll, iPad long-press, and the Jest strategy. If it fails, fall back to React Aria DnD.
- **OQ2.** Draft scope. Proposed: one draft per tenant per service day, shared by all dispatchers, with per-lane versions. The alternative is per-dispatcher drafts merged at Publish. Per-tenant is simpler and fits "serialize instead of racing".
- **OQ3.** How Publish enters the executor. Options: (a) create `apply_loading_plan` approvals and auto-approve them as the dispatcher (one audit trail), or (b) a direct `LoadingPlanExecutor` entry for board publishes. The loading-plan-executor design (K1, K7) has to be extended carefully either way.
- **OQ4.** Driver pairing at Publish. The dispatch service resolves the driver from `drivers_current.assigned_truck_id`. Should the board write that field (which needs a dispatcher-allowed path), or should dispatch accept the draft's pairing explicitly? The product default proposed is that the pairing lasts for the shift only.
- **OQ5.** Can a `check_unavailable` warning be published with an override reason, or does it block? Proposed: overridable, except for qualification and certification checks, which block when unavailable.
- **OQ6.** Should shadow-mode agent output appear as read-only suggestions? Agent-overlay Req 9.2 forbids submitting them to the ConfirmationProtocol, but displaying them may still be allowed. This needs an owner decision because it changes what dispatchers see.
- **OQ7.** Multiple loads per truck per day. The timeline shows a sequence of loads per lane, and G9 must land first.
- **OQ8.** Units and time zone. The UI shows US gallons (storage is litres) and the tenant's local time. The service-day boundary follows the tenant's time zone, as the daily counter reset already does (order-intake-pipeline Req 3.2.4).

## Sources

- [S1] PDI Technologies, Logistics solutions. https://pditechnologies.com/increase-productivity/logistics-solutions/
- [S2] Cargas Energy, Fuel delivery software. https://cargasenergy.com/fuel-delivery-software/
- [S3] ADD Systems, Wholesale petroleum software. https://www.addsys.com/industries/wholesale-petroleum/
- [S4] Vertrax, home page. https://vertrax.com/
- [S5] Descartes, Route planning, optimization and dispatch. https://www.descartes.com/solutions/routing-mobile-and-telematics/route-planning-optimization-and-dispatch
- [S6] Samsara, Fleet routing and dispatch. https://www.samsara.com/products/telematics/routing
- [S7] Onfleet, Assignment and dispatching. https://onfleet.com/assignment-and-dispatching
- [S8] OptimoRoute, Intelligent drag and drop timeline. https://optimoroute.com/drag-and-drop/
- [S9] OptimoRoute, Features. https://optimoroute.com/features/
- [S10] ServiceTitan, Dispatch software. https://www.servicetitan.com/features/dispatch-software
- [S11] Bryntum, Scheduler Pro. https://bryntum.com/products/schedulerpro/
- [S12] DHTMLX Scheduler, Timeline view. https://docs.dhtmlx.com/scheduler/views/timeline/
- [S13] Linear Docs, Board layout. https://linear.app/docs/board-layout
- [S14] Nielsen Norman Group, Drag-and-drop: how to design for ease of use. https://www.nngroup.com/articles/drag-drop/
- [S15] Nielsen Norman Group, 10 usability heuristics. https://www.nngroup.com/articles/ten-usability-heuristics/
- [S16] Nielsen Norman Group, Confirmation dialogs can prevent user errors (if not overused). https://www.nngroup.com/articles/confirmation-dialog/
- [S17] W3C, Understanding SC 2.5.7 Dragging Movements. https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html
- [S18] W3C, Understanding SC 2.5.8 Target Size (Minimum). https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html
- [S19] MDN, ARIA live regions. https://developer.mozilla.org/en-US/docs/Web/Accessibility/ARIA/Guides/Live_regions
- [S20] dnd kit (legacy), Accessibility guide. https://dndkit.com/legacy/guides/accessibility/
- [S21] dnd kit, Overview and changelog. https://dndkit.com/ and https://dndkit.com/changelog
- [S22] dnd-kit GitHub issues, "react 19" query. https://github.com/clauderic/dnd-kit/issues?q=is%3Aissue+react+19
- [S23] Atlassian Design, Pragmatic drag and drop: About. https://atlassian.design/components/pragmatic-drag-and-drop/about
- [S24] Atlassian Design, Pragmatic drag and drop: Accessibility guidelines. https://atlassian.design/components/pragmatic-drag-and-drop/accessibility-guidelines
- [S25] Atlassian Design, Pragmatic drag and drop: Web platform design constraints. https://atlassian.design/components/pragmatic-drag-and-drop/web-platform-design-constraints
- [S26] GitHub, atlassian/pragmatic-drag-and-drop README. https://github.com/atlassian/pragmatic-drag-and-drop
- [S27] Atlassian blog, Designed for delight, built for performance: the journey of pragmatic drag and drop. https://www.atlassian.com/blog/how-we-build/designed-for-delight-built-for-performance
- [S28] Pragmatic drag and drop 4.0.0 CHANGELOG. https://unpkg.com/@atlaskit/pragmatic-drag-and-drop@4.0.0/CHANGELOG.md
- [S29] GitHub, react-beautiful-dnd is now deprecated (#2672). https://github.com/atlassian/react-beautiful-dnd/issues/2672
- [S30] React DnD, About. https://react-dnd.github.io/react-dnd/about
- [S31] React Aria, Drag and drop. https://react-aria.adobe.com/dnd
- [S32] npm registry metadata (latest version, publish date, dependencies, peer dependencies), fetched 2026-10-07: https://registry.npmjs.org/@atlaskit/pragmatic-drag-and-drop, …-hitbox, …-auto-scroll, …-live-region, …-react-accessibility, …-react-drop-indicator, https://registry.npmjs.org/@dnd-kit/core, …/@dnd-kit/sortable, …/@dnd-kit/react, …/react-dnd, …/@hello-pangea/dnd, …/react-beautiful-dnd, …/react-aria-components, …/@xyflow/react
- [S33] React, useOptimistic. https://react.dev/reference/react/useOptimistic
- [S34] TanStack Query, Optimistic updates. https://tanstack.com/query/latest/docs/framework/react/guides/optimistic-updates
- [S35] TkDodo, Concurrent optimistic updates in React Query. https://tkdodo.eu/blog/concurrent-optimistic-updates-in-react-query
- [S36] Figma blog, How Figma's multiplayer technology works. https://www.figma.com/blog/how-figmas-multiplayer-technology-works/
- [S37] Google PAIR Guidebook, Explainability + Trust. https://pair.withgoogle.com/chapter/explainability-trust/
- [S38] eCFR, 49 CFR 395.3 Maximum driving time for property-carrying vehicles. https://www.ecfr.gov/current/title-49/subtitle-B/chapter-III/subchapter-B/part-395/subpart-A/section-395.3
- [S39] eCFR, 49 CFR 383.93 Endorsements. https://www.ecfr.gov/current/title-49/subtitle-B/chapter-III/subchapter-B/part-383/subpart-F/section-383.93
- [S40] React Flow, home page. https://reactflow.dev/
