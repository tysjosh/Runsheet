# Dispatch Board: requirements

Main checkout `/Users/olukotunjosh/Downloads/Runsheet`, branch `production-readiness/go-live-blockers` at `ad72dde`. Paths are relative to the repo root. Evidence for every code reference is in `research.md` (same folder); IDs `F#`, `G#`, `S#`, `OQ#` below refer to it. Requirement IDs here are `R#.n` (functional) and `N#` (non-functional). Open product questions are `Q#` (section at the end) so they don't collide with research `OQ#`.

Revision 2 (after design review pass 1): changed D8, R2.9, R6.5, R7.5, R10.5, R13.3, R13.4, R14.2, N7; added R2.10, R3.7, R3.8, R10.6, R12.9, R12.10, Q12, Q13. Revision 3 (after design review pass 2): changed R3.3, R3.6, R12.2, R12.6, R13.3, R13.7, R14.2; added R13.8, R13.9, Q14, Q15. Revision 4 (after design review pass 3): changed N7; added R13.10, R13.11, Q16. The product concept and scope are unchanged.

## Summary

The Dispatch Board is a structured, drag-and-drop planning surface for one service day. Each truck is a horizontal lane with time on the x-axis. Trays hold unassigned orders, available drivers and trucks. Dispatchers drag an order onto a truck to load it, drag a driver onto a truck to pair them for the shift, drag stops to reorder them, and drag between trucks to reassign. Every drop is validated by the backend before it commits, and the result shows on the target as pass, warn or block with a reason. Agent-generated loading and route plans appear as suggestions with a diff against the draft. Publishing a lane commits its loads (`scheduled`) and dispatches them to the driver app. Every drag has a click and keyboard equivalent.

It is not a free-form node graph (research 1.8). Dispatch has a fixed shape (tenant → service day → truck lane → load → ordered stops, one driver per lane), so the board only offers drop targets that exist in that shape.

## Decisions taken from research and the code

These are fixed unless the design review overturns them.

- **D1. Layout: rows.** One lane per truck, time left to right, fixed lane header (truck, driver slot, compartment gauge, badges). Left tray, right detail drawer. Research Part 2.
- **D2. Server is authoritative.** All checks run in the backend through the existing validators (research 4.2). The client may pre-check with data from the board snapshot only to colour a target while the server answer is pending; the server answer always replaces it.
- **D3. Three-tier invalid-drop policy.** `block` prevents the drop with no override; `warn` lets the drop land but Publish needs a reason per open warning; `info` needs nothing; a check that cannot run reports `warn` with `check_unavailable`, except for the safety-critical checks listed in R9.5, which block. Research Part 3.
- **D4. Draft, then publish.** Board edits live in a server-side draft per tenant and service day. Order status and `mvp_load_plans` are untouched until Publish. Publish reuses `LoadingPlanExecutor` (to `scheduled`) and `FuelPlanDispatchService` (to `dispatched` + driver notification). Research R7, G3, G10.
- **D5. Concurrency: shared draft, per-lane versions, server-serialized commands.** One draft per tenant and service day, shared by all dispatchers. Each command names the lane versions it expects; a stale version is refused and the client re-renders the server lane. Presence is advisory, not a lock. Research R8, OQ2.
- **D6. Shift-scoped driver pairing.** Pairing a driver with a truck on the board applies to that service day only. It does not change the permanent `drivers_current.assigned_truck_id`, which stays admin-only. Research G4, OQ4.
- **D7. Fuel orders only.** The board plans `fuel_orders_current` orders, not legacy `Job`s (loading-plan-executor D5).
- **D8. Units and time.** UI shows US gallons (storage stays litres) and the tenant's local time. The service day boundary is midnight in the tenant's time zone, resolved as `fuel/services/driver_daily_reset.py` `_get_tenant_timezone` does from the session's tenant settings. `TenantSettings` has no time-zone field today, so every tenant resolves to `America/Chicago` until one is added (design K2.5). Research OQ8.

## Glossary

- **Board**: the Dispatch Board UI and its backend.
- **Service day**: a calendar date in the tenant's time zone.
- **Shift**: a named time window inside a service day used to filter the view and tag loads (Q2).
- **Draft**: the server document holding the board state for one tenant and service day.
- **Lane**: one truck's row in the draft for the service day: its shift driver pairing and its ordered loads.
- **Load**: one terminal lift plus its ordered stops, with compartment allocations. A lane may have several loads in a day.
- **Stop**: one order delivered within a load.
- **Command**: one board mutation (assign, move, unassign, pair, accept suggestion, and so on) sent to the server.
- **Check**: one validation result: `{check, outcome: pass|warn|block|info, reason_code, message, source}`.
- **Suggestion**: an agent-produced load or route proposal shown on the board but not yet in the draft.
- **Publish**: commit a lane's loads to orders and plans and dispatch them to the lane's driver.
- **Published lane**: a lane whose draft content equals what was last published. **Modified lane**: a published lane edited since.

## Functional requirements and acceptance criteria

### R1. Entry, flag and visibility

**User story:** As a dispatcher, I want the board inside the existing Dispatch area, so I plan where I already work.

1. THE Dispatch page (`components/DispatchPage.tsx`) SHALL show a "Board" tab before "Scheduling" and "Fuel Distribution" WHEN the tenant's board flag is not `disabled` AND the user holds `admin` or `dispatcher`.
2. WHEN the board flag is `active_gated` or `active_auto`, THE Board SHALL be the default tab of the Dispatch page.
3. WHEN the board flag is `disabled`, THE Board endpoints SHALL return 404 with error code `DISPATCH_BOARD_DISABLED`, AND the tab SHALL be hidden.
4. WHEN the board flag is `shadow`, THE Board SHALL be read-only: snapshot, validation preview and suggestions work; commands and Publish return 409 `DISPATCH_BOARD_READ_ONLY`, AND the UI SHALL show a "Preview mode, changes are not saved" banner and disable drag sources.
5. THE Board SHALL treat `active_auto` exactly as `active_gated`. The board never publishes without a dispatcher action.
6. THE Board SHALL accept the deep link `/dashboard/dispatch?tab=board&date=YYYY-MM-DD&truck=<id>&order=<id>` and open the given day, scroll to the truck and select the order.

### R2. Board snapshot, service day and shift view

**User story:** As a dispatcher, I want to open a day and see every truck, driver and unassigned order at once.

1. WHEN the board opens, THE Board SHALL load one snapshot for the selected service day containing: lanes (truck, compartments with capacity and current state, shift pairing, loads, stops, checks, lane version, publish state), the unassigned-order tray, the driver tray, the truck tray, open suggestions, the draft version, and the board mode.
2. THE Board SHALL default to today's service day in the tenant time zone and offer previous/next day and a date picker, limited to 7 days back and 14 days ahead.
3. WHERE a past service day is selected, THE Board SHALL be read-only.
4. THE Board SHALL offer a shift selector with "All day" plus the tenant's shifts (Q2 default: Day 06:00–18:00, Night 18:00–06:00 next day). The selected shift sets the visible time window and the default shift tag for new loads.
5. THE Board SHALL offer two zoom levels: Timeline (stops positioned by ETA) and Sequence (stops evenly spaced in order). Both SHALL accept the same drops.
6. WHEN route ETAs are unavailable for a lane, THE Board SHALL render that lane in Sequence layout and show an "ETAs unavailable" badge.
7. THE Board SHALL display volumes in US gallons and times in the tenant time zone with the zone abbreviation in the header.
8. THE lane header SHALL show: truck id and type, driver slot, a compartment gauge for the selected load, and badges for HOS, certification, compartment cleaning state, and open warnings count.
9. WHERE the selected service day is today, THE lane body SHALL shade the paired driver's 14-hour on-duty window and mark the 11-hour driving limit when HOS figures are available (49 CFR 395.3, F29).
10. WHERE the selected service day is in the future, THE Board SHALL NOT apply today's HOS gate or figures to it; the HOS check SHALL report `info` "Hours of service are checked on the day" (`hos_not_projected`).

### R3. Trays

**User story:** As a dispatcher, I want unassigned work, drivers and trucks in trays, so nothing is hidden behind a picker.

1. THE order tray SHALL list every tenant order for the service day that is `placed`, `confirmed`, or `scheduled`-but-unlinked, and is not on a load in the draft, with no page cap below 1,000 orders.
2. EACH order card SHALL show customer, product, gallons requested (or "Fill"), delivery window, call type (`keep_full`, `auto_fill`, `will_call`, `one_off`), priority bucket from delivery prioritization when available, and badges for hold, missing window, and dyed diesel.
3. THE order tray SHALL sort by priority score descending by default (score from the newest delivery-prioritization run of the last 24 hours; orders with no score after scored ones), then window start ascending; the dispatcher may sort by window, customer or product. WHEN the tray is truncated (R3.8), it SHALL hold the orders with the earliest delivery windows, AND the banner SHALL say so.
4. THE driver tray SHALL list tenant drivers with status, shift pairing (if any), qualification summary (CDL class, HAZMAT/tanker endorsements, nearest expiry), and HOS remaining when available. Drivers ineligible for dispatch SHALL show the reason and remain visible.
5. THE truck tray SHALL list trucks without a lane on the selected day; dragging one onto the board (or "Add lane") creates an empty lane.
6. Orders `on_hold` for the service day SHALL be returned with the tray (counted toward the 1,000 cap), listed in a collapsed "On hold" group, and SHALL NOT be draggable; their card menu offers "Release hold" via the existing `releaseHoldOrder` client. A command naming an on-hold order SHALL be `block` with `order_state` reason `on_hold`.
7. AN order already on another service day's draft (today to 14 days ahead) SHALL NOT appear in the order tray, AND assigning it on this day SHALL be `block` with `order_on_other_day` naming the other date.
8. WHEN more than 1,000 orders match, THE tray SHALL say the list is truncated, AND the call type, product and window filters SHALL then be applied by the server.

### R4. Filters, search and density

1. THE Board SHALL offer filter chips for call type, product, priority bucket, window (overdue, today, later), status, and "has warnings", applied to trays and lanes.
2. THE Board SHALL offer one search box matching order id, customer name, truck id and driver name; matching lanes and cards are highlighted and non-matching cards dimmed, not removed.
3. THE Board SHALL offer Comfortable and Compact density. Compact SHALL NOT reduce any interactive target below 24×24 CSS px (N5).
4. Filters, search, density, zoom and shift SHALL persist per user in local storage and SHALL be reflected in the URL query so a view can be shared.
5. A collapsed lane SHALL still accept drops (F12).

### R5. Load an order onto a truck

**User story:** As a dispatcher, I want to drop an order on a truck and see how it fits the compartments before it commits.

1. WHEN an order is dropped on a lane header, THE Board SHALL insert it at the best-fit position computed by the server (least added time that keeps windows), into the load that has capacity, or a new load if none has.
2. WHEN an order is dropped on a position inside a load, THE Board SHALL insert it at exactly that position.
3. WHEN an order is placed on a load, THE server SHALL allocate it to compartments with the existing solver (`compartment_accepts`, `check_feasibility`, `optimize_loading_plan`) and return per-compartment product, gallons and fill percentage.
4. WHILE an order is dragged over a lane, THE lane header gauge SHALL preview the post-drop compartment fill, colouring cells that would be blocked (product not allowed, segregation, `blocked` compatibility) and cells needing cleaning (`requires_cleaning`).
5. THE dispatcher SHALL be able to override the automatic compartment allocation in the detail drawer by assigning the order to specific compartments; the override is validated like any command.
6. THE dispatcher SHALL be able to set or change a load's terminal; the drawer SHALL show sourcing recommendations, terminal wait and contract lift status from the existing `/api/fuel` read endpoints.
7. Multiple selected orders (Shift/Cmd-click or checkbox) SHALL be droppable together as one command; the result is all-or-nothing.

### R6. Pair a driver with a truck

1. WHEN a driver is dropped on a lane's driver slot, THE Board SHALL pair that driver with that truck for the service day.
2. IF the driver is already paired with another truck that day, THEN THE Board SHALL move the pairing (one driver per lane, one lane per driver per shift window), and the announcement SHALL name both trucks.
3. THE pairing SHALL be validated with driver qualification for every load in the lane (`requires_tanker`, `requires_hazmat`, `min_cdl_class`), HOS, and driver status.
4. THE Board SHALL NOT modify `drivers_current.assigned_truck_id`.
5. WHEN a lane has no pairing AND exactly one active driver has `assigned_truck_id` equal to the lane's truck AND that driver is not paired to another lane that day, THE Board SHALL show that driver as a suggested pairing ("Pair {name}") that the dispatcher confirms with one click, validated like any pairing. Zero or several matches SHALL show no suggestion.

### R7. Rearrange work

1. Dragging a stop within a load SHALL reorder it; dragging it to another load or lane SHALL move it; dragging it to the order tray SHALL unassign it.
2. Dragging a whole load (by its terminal chip) to another lane SHALL move the load; dragging it within the lane SHALL reorder loads.
3. EVERY rearrange SHALL be validated for both source and destination lanes before commit.
4. A lane MAY hold multiple loads in a day. The board SHALL show them left to right in time order with a return-to-terminal gap.
5. "Remove lane" SHALL be available only for lanes never published, as a normal undoable command that returns their stops to the tray. FOR a lane that was ever published (Published, Modified, or Publish failed after a prior publish) the action SHALL be disabled with the reason "Move or reassign this truck's stops first" (R13.5).

### R8. Live validation

**User story:** As a dispatcher, I want to know if a drop is valid, and why, before I let go.

1. THE server SHALL run these checks for a proposed lane state: driver status and qualification, HOS projection and gate, asset certification, product/compartment compatibility and cleaning, capacity/weight/minimum drop, dyed diesel eligibility, order status/hold/committed-elsewhere, delivery window presence and ETA within window, scheduling conflicts (driver paired twice in overlapping windows, loads overlapping in time), and terminal/supply (wait, contract lift limit).
2. EACH check SHALL return `{check, outcome, reason_code, message, source}`; `message` SHALL be built from fixed templates and ids, never from exception text.
3. WHEN a drag starts, THE Board SHALL request one batch validation of the dragged item against every lane currently rendered and mark each lane pass/warn/block within 500 ms p95 (N1).
4. WHILE hovering a specific insertion position, THE Board SHALL request position-specific validation (window and ETA) debounced to 150 ms and show the chip on the insertion indicator.
5. WHEN the pointer is released, THE Board SHALL send the command; the server SHALL re-validate and commit only if no check is `block`.
6. WHILE the server answer is pending, THE Board SHALL show the item in its optimistic position with a "Checking…" chip; on refusal it SHALL return the item to its origin and show the reason.
7. THE detail drawer SHALL list all checks for the selected lane, load or stop, grouped by outcome, each with its reason and a link to the record that fixes it (driver profile, certification, order).

### R9. Invalid-drop policy and overrides

1. IF any check is `block`, THEN THE Board SHALL refuse the drop, AND the target SHALL show a red chip with the first blocking reason and a count of others.
2. IF the worst check is `warn`, THEN THE Board SHALL commit the drop with an amber chip and add the warning to the lane's open warnings.
3. WHEN the dispatcher acknowledges a warning, THE Board SHALL require a reason of 3–500 characters, store it on the draft with the actor from the session and the time, and close the warning.
4. THE Board SHALL NOT open any dialog on a drop. Warning reasons are collected inline from the chip, from the drawer, or in the Publish review (R12.2).
5. IF a check cannot run, THEN its outcome SHALL be `warn` with `check_unavailable`, EXCEPT driver qualification, asset certification and dyed diesel, whose outcome SHALL be `block` with `check_unavailable` (Q5).
6. IF the HOS gate returns `blocked` and no active `HOSGateOverride` exists, THEN the HOS check SHALL be `block`; the drawer SHALL link to the existing HOS override flow (`POST /api/driver/hos/override`), which is the only way to lift it.
7. WHEN a lane changes such that an acknowledged warning's condition changes (different reason code or value), THE Board SHALL reopen it.

### R10. Draft and published states

1. Board commands SHALL change only the draft. They SHALL NOT change order status, order links or `mvp_load_plans` / `mvp_routes`.
2. EACH lane SHALL show one state: Draft (never published), Published, Modified (published and edited since), Publishing, or Publish failed.
3. THE draft SHALL persist across reloads and sessions, and SHALL be shared by all dispatchers of the tenant.
4. THE Board SHALL offer "Discard changes" for a Modified lane, restoring its last published content as a normal command.
5. WHEN an order in the draft becomes `cancelled`, `on_hold`, or committed elsewhere outside the board, or its product, customer or customer tank changes, THE affected lanes SHALL show a `block` check on that stop until the dispatcher removes it (and re-adds it, for a changed order).
6. WHEN only an order's requested gallons or fill-to-full flag changes after it was placed on a load, THE Board SHALL show an `info` check, AND Publish SHALL recompute the load's compartment allocations from the current order.

### R11. Undo and redo

1. THE Board SHALL offer Undo and Redo for the current user's own draft commands in the current session, up to 50 steps.
2. Undo SHALL send the inverse command; it is validated like any command and SHALL be refused, with the reason announced, when a lane it touches was changed by another dispatcher since, or when the inverse would be blocked.
3. Publish SHALL clear undo history for the published lanes. Publishing is not undoable; post-publish changes follow R13.
4. Undo and Redo SHALL be reachable by toolbar buttons and by Cmd/Ctrl+Z and Shift+Cmd/Ctrl+Z.

### R12. Publish

**User story:** As a dispatcher, I want to send the plan to drivers when I'm ready, and know exactly who gets notified.

1. THE Board SHALL offer "Publish lane" on each lane and "Publish all ready" in the toolbar.
2. WHEN Publish is requested, THE Board SHALL show one review dialog, built from a server dry run of the publish that makes no writes, listing per lane: driver to notify, loads, orders that become dispatched, and every open warning with a reason field. Publish SHALL be disabled until each open warning has a reason.
3. IF a lane has any `block` check, no paired driver, or no loads, THEN it SHALL NOT be publishable and the dialog SHALL say why.
4. WHEN confirmed, THE server SHALL, per lane: re-validate; write one `mvp_load_plans` and one `mvp_routes` document per load (with board provenance fields); apply each plan through `LoadingPlanExecutor`; dispatch each plan through `FuelPlanDispatchService` to the paired driver; and mark the lane Published.
5. Publish SHALL be idempotent per lane and draft version: retrying a lane already published at that version SHALL make no writes and return the stored result.
6. IF publishing one lane fails, THEN other lanes SHALL still publish, except lanes in the same re-publish group (R13.8), which fail together; AND each failed lane SHALL show Publish failed with the reason, whether any writes were made, and a Retry action.
7. WHEN dispatch succeeds, THE driver SHALL receive the assignment through the existing `/ws/driver` `send_assignment` and push path, and the driver app SHALL show the load, its compartment manifest and stops with no driver-app change.
8. THE Publish result SHALL be announced (polite for success, assertive for failure) and shown as a toast via `useToasts`.
9. THE plans and routes the board writes SHALL carry the exact fields the driver app, dispatch and plan execution read (design K7.3a), so that for every published stop the driver's work read returns coordinates, planned arrival and planned gallons by product.
10. Plans written by the board SHALL be hidden from the Fuel Distribution plan list by default, AND approve, reject and replan on them from the existing MVP endpoints SHALL be refused with 409 `BOARD_OWNED_PLAN`, AND the exception-replanning agent SHALL NOT act on them.

### R13. Changes after publish

1. THE Board SHALL allow editing a published lane: the lane becomes Modified and the changes stay in the draft until re-published.
2. A stop whose order is `in_transit`, `delivered` or `failed` SHALL be pinned and SHALL NOT be draggable.
3. WHILE a load is started (any of its orders is `in_transit`, `delivered` or `failed`, or its plan execution has `completed_stops > 0`), THE Board SHALL allow only reordering and removing its not-yet-started stops; adding orders to it, changing its compartment allocation or terminal, or changing the lane's driver SHALL be `block` with `load_started` (Q12).
4. WHEN a Modified lane is re-published, THE server SHALL, for each changed load that is not started, retire its published plan, route and execution and create a new revision; for each changed started load, rewrite its route and execution in place keeping every started stop's sequence number; relink moved orders; clear the driver work cache for every affected order; and then notify drivers: `assignment_revoked` only for orders a driver lost, and an updated assignment or route to each driver who keeps or gains work.
5. WHEN a stop is removed from a published load and not placed elsewhere, THE Board SHALL keep it on the lane's "To reassign" shelf, not in the order tray, because a `dispatched` order cannot return to `scheduled` or `confirmed`. Re-publish SHALL be refused with `unplaced_dispatched_order` until each shelved order is placed on a load or cancelled through the existing cancel flow with a reason.
6. WHEN the driver pairing of a published lane changes, THE re-publish SHALL revoke from the previous driver and assign to the new one.
7. THE re-publish review dialog SHALL list each affected driver and what they will be told, every lane added to the publish because a dispatched order moved from or to it, AND, for each load that will be re-issued while its planned start is already past, the information "The driver may already be loading at the terminal. Call before re-publishing." (`driver_may_be_loading`; information only, no acknowledgement).
8. WHEN a publish includes any order that is already `dispatched` (moved to another load, to a new load, or to a lane that was never published), THE server SHALL process every lane that order moved from or to as one re-publish group, relink each such order to its new plan before applying it, AND notify drivers only after every write of the group has succeeded.
9. Loading at the terminal before the driver marks the first order `in_transit` is not observable; such a load SHALL count as not started (R13.3), and a change to it SHALL re-issue it as a new revision.
10. IF a re-publish fails before any order status has changed, THEN THE server SHALL restore every plan, route, execution and order link of the group to what it was before the attempt, AND the lanes SHALL become editable with Retry available. IF it fails after an order status has changed, THEN THE server SHALL complete it forward (automatically retrying first), AND the lanes SHALL stay read-only with Retry as the only action, AND the board SHALL say which drivers can't see their routes until it completes (Q16). In both cases, after recovery every order not yet finished SHALL be on a plan the driver app shows.
11. WHILE an order is linked to a board plan that is not `dispatched` (being re-issued), THE driver app's "start delivery" (`in_transit`) SHALL be refused with "Your dispatcher is updating this route. Try again in a moment." and retried automatically. A driver status change based on an out-of-date read of the order SHALL be refused and retried, never applied over a newer change. Check-ins SHALL never overwrite a concurrent board change to the same route, and vice versa.

### R14. Multi-dispatcher concurrency and presence

1. EVERY command SHALL carry the expected version of each lane it touches and a client command id.
2. IF a lane version differs from the expected one, THEN THE server SHALL refuse the command with 409 `BOARD_LANE_CONFLICT` and return the current lanes; THE Board SHALL re-render those lanes and announce "Truck 12 was changed by ana. Your change was not applied.", where the name is the other dispatcher's display name (Q13), or "Another dispatcher" when none resolves.
3. A repeated client command id SHALL return the original result without re-applying (idempotent retry).
4. THE Board SHALL show who else has the same service day open (avatars) and an "Ana is editing" marker on lanes another user changed in the last 30 seconds or has open in the drawer. Presence SHALL NOT block anyone.
5. WHILE a lane is Publishing, commands touching it SHALL be refused with 409 `BOARD_PUBLISH_IN_PROGRESS`.

### R15. Live updates

1. WHEN another dispatcher's command commits, THE other open boards for that tenant and service day SHALL update the affected lanes within 2 seconds p95.
2. WHEN an order is placed, changes status, or is assigned outside the board (existing `/ws/orders` events), THE trays and affected lanes SHALL update, and affected lanes SHALL be re-validated server-side.
3. WHEN a published load progresses (existing `/ws/plan-execution` `execution_update`), THE lane SHALL show stop progress (arrived, delivering, delivered) and pin completed stops.
4. WHEN telematics shows a truck behind its route ETA by more than the tenant threshold (default 15 minutes), THE lane SHALL show a "Running late" badge with the delta, using the existing `/api/fleet/live` feed and the route ETAs.
5. WHEN a driver's HOS, qualification or certification state changes, THE affected lanes SHALL re-validate on the next snapshot or event and show the new outcome.
6. WHILE the board socket is disconnected, THE Board SHALL show a "Live updates paused" indicator, poll the snapshot every 30 seconds, and refetch once on reconnect.
7. WHILE a command is pending on a lane, THE Board SHALL hold incoming updates for that lane and apply the server lane once the last pending command settles (F24).

### R16. Agent suggestions

**User story:** As a dispatcher, I want agent plans pre-filled so I start from a good draft, but I decide.

1. THE Board SHALL show open loading suggestions (`mvp_load_plans` from the compartment loading agent with status `proposed` or `draft`, and their pending `apply_loading_plan` approvals) and route suggestions (`mvp_routes` from the route planning agent) for the service day as ghost loads on their truck's lane.
2. EACH suggestion SHALL show why: priority bucket and score, runout forecast or window it meets, compartment fill, and the agent's name, with a diff against the lane's current draft (added, removed, reordered, reassigned stops, quantity changes, ETA shifts) using the `ReplanDiff` vocabulary.
3. THE dispatcher SHALL be able to accept a suggestion per load, per lane, or "Accept all"; accepted content is copied into the draft through the normal command path and validation. A suggestion with a `block` check SHALL be marked and only its passing loads accepted, after the dispatcher confirms the partial accept.
4. THE dispatcher SHALL be able to adjust by accepting then dragging, and to reject a suggestion with an optional reason; rejecting a suggestion that has a pending approval SHALL call the existing `ApprovalQueueService.reject` with that reason.
5. THE Board SHALL show suggestions only when the agent's overlay mode is `active_gated` or `active_auto`. Shadow-mode output SHALL NOT be shown (Q6).
6. THE Board SHALL offer "Generate plan" for the day, calling the existing `generatePlan` client, and show suggestions as they arrive.
7. Accepting a suggestion SHALL NOT execute or approve the agent's approval entry; the board's Publish is the only commit path from the board.

### R17. Map linkage

1. THE detail drawer SHALL show a map of the selected lane or load: terminal, stops in order, and the truck's live position, using the existing `@vis.gl/react-google-maps` components.
2. Selecting a stop on the map SHALL select it on the board and vice versa.
3. THE Board SHALL offer a split view (board above, map below) showing all visible lanes' routes, colour-coded per lane.
4. THE map SHALL NOT be a drop target in this scope; all edits happen on the board or by menu (Out of scope).

### R18. Non-drag and keyboard operation (WCAG 2.2 SC 2.5.7, 2.1.1)

**User story:** As a dispatcher who uses a keyboard, a screen reader or a single pointer, I want every board action without dragging.

1. EVERY drag action SHALL have a single-pointer, non-drag equivalent: card menu actions "Assign to truck…", "Move to…", "Move earlier / later", "Move to first / last stop", "Unassign", "Pair with truck…", "Accept", "Reject".
2. "Assign to truck…" and "Move to…" SHALL open a searchable list of lanes with each lane's validation outcome; lanes with a `block` SHALL be disabled and their accessible name SHALL include the reason.
3. THE Board SHALL offer Place mode: select a card (click or Enter), then click or press Enter on a lane header, insertion slot or driver slot to place it; Escape cancels.
4. ALL board elements SHALL be reachable by keyboard in this order: toolbar, tray, lanes; within the board, arrow keys move focus between lanes (Up/Down) and between cards in a lane (Left/Right); Tab leaves the grid.
5. THE Board SHALL provide shortcuts, listed in a "?" help dialog: `/` search, `A` assign selected, `M` move selected, `P` pair driver, `U` unassign, `[`/`]` previous/next day, `T` timeline/sequence toggle, `Cmd/Ctrl+Z` undo, `Shift+Cmd/Ctrl+Z` redo, `Esc` cancel. Single-key shortcuts SHALL be disabled while focus is in a text input and SHALL be turn-off-able in settings (SC 2.1.4).
6. Focus SHALL return to the moved card in its new place after every action, or to the origin if refused.

### R19. Screen reader announcements

1. THE Board SHALL mount one polite live region at page load and announce every outcome in plain text: what moved, from where, to where, the resulting fill, and the worst check. Example: "Order 1042, 3,000 gallons diesel, assigned to Truck 12, load 1, stop 3. Compartment 2 is 92% full. Warning: delivery window at risk."
2. Publish failures and conflict refusals SHALL use an assertive region.
3. Lanes SHALL expose an accessible name summarising truck, driver, loads, stop count, fill and warnings count.
4. Drag handles, chips and gauges SHALL have text alternatives; colour SHALL never be the only carrier of pass/warn/block (icon + text).

### R20. Tablet and small screens

1. ON touch devices, a long press (~300 ms) on a drag handle SHALL start a drag; a tap SHALL select (Place mode); scrolling SHALL not start drags.
2. BELOW 1024 px viewport width, THE Board SHALL switch to a stacked layout: lanes in Sequence layout as a vertical list, tray as a bottom sheet, drawer as a full-screen sheet; Place mode and menus are primary.
3. Touch targets SHALL be at least 24×24 CSS px; drag handles at least 44×44 CSS px on touch devices.

### R21. Empty, loading and error states

1. WHILE the snapshot loads, THE Board SHALL show skeleton lanes and trays, not a spinner over a blank page.
2. WHEN there are no trucks with compartments, THE Board SHALL show an `EmptyState` linking to Truck Compartments setup.
3. WHEN there are no orders for the day, THE order tray SHALL say so and link to Orders.
4. WHEN all orders are assigned, THE tray SHALL show "All orders are planned" with a count.
5. IF the snapshot fails, THEN THE Board SHALL show `LoadErrorState` classified by `classifyLoadError` (forbidden, module disabled, not found, error) with Retry.
6. IF a single validator is degraded, THEN THE Board SHALL show a banner naming it ("HOS data unavailable, HOS checks are warnings only") and keep working.

### R22. Audit trail

1. EVERY committed command SHALL be recorded append-only with: tenant, service day, command id, type, payload, actor user id (from the session), time, lanes and versions touched, check outcomes, overrides with reasons, and input modality.
2. EVERY Publish and re-publish SHALL be recorded with the plans written, orders transitioned, drivers notified, and per-lane result; orders SHALL also carry the existing `order_assigned` / status events written by the executor and dispatch service.
3. THE drawer SHALL show a History tab for the lane (who did what, when, why), newest first.
4. Audit records SHALL never contain secrets or tokens and SHALL be tenant-scoped.

### R23. Permissions and tenancy

1. ALL board endpoints and the board socket SHALL require role `admin` or `dispatcher` (exact match) and SHALL be scoped to the session tenant; tenant and actor ids SHALL never be read from the request body.
2. THE same roles SHALL be allowed to publish (Q7).
3. Records from another tenant SHALL be indistinguishable from not found.
4. Overriding a `block` SHALL NOT be possible for any role.

### R24. Telemetry

1. THE server SHALL record metrics per tenant for: snapshot latency, validate latency (batch and single), command latency and outcome (committed, blocked, conflict, error), publish latency and outcome per lane, conflict rate, override count by reason code, input modality share (drag, menu, place mode, keyboard), suggestion accept/adjust/reject counts, and undo usage.
2. Telemetry SHALL use the existing `telemetry/service.py` (`record_metric`, `log_audit_event`) and structured logs; no third-party analytics SHALL be added.
3. Telemetry SHALL NOT include customer names, addresses or free-text reasons.

### R25. Driver manifest correctness (prerequisite)

1. THE driver work service SHALL resolve an order's loading plan by the order's `assigned_run_id` and truck, restricted to plans with status `scheduled`, `dispatched` or `completed` and not superseded, instead of the newest plan for the truck in any status (G9).
2. WHEN no matching plan exists, THE driver work service SHALL return `manifest_available: false` as today.

## Non-functional requirements

- **N1. Performance.** Target tenant size: 60 lanes, 600 orders, 1,500 stops per day.
  - Snapshot p95 ≤ 2.0 s server, first meaningful paint ≤ 3.0 s on a 2020-class laptop.
  - Batch validate (one item, ≤ 60 lanes) p95 ≤ 500 ms; single position validate p95 ≤ 300 ms; command commit p95 ≤ 600 ms.
  - Dragging stays at ≥ 50 fps with 60 lanes (virtualized) on the same hardware; no layout reflow of the timeline during drag (F14).
  - Live update propagation p95 ≤ 2 s.
- **N2. Reliability.** No board action can leave an order in a state the state machine forbids. A failed publish is retryable and never reports success. The draft survives backend restarts.
- **N3. Security.** All inputs validated server-side (see design "External input validation"); no client-trusted tenant, actor or outcome; free-text reasons stored as text and rendered escaped.
- **N4. Dependencies.** New frontend dependencies limited to Pragmatic drag and drop core, hitbox, auto-scroll and live-region, pinned to exact versions after the spike (research R3, OQ1). No new backend dependency. No state-management or UI-kit library.
- **N5. Accessibility.** Targets WCAG 2.2 AA, including 2.1.1, 2.1.4, 2.4.7, 2.4.11, 2.5.7, 2.5.8, 4.1.3. Full conformance needs manual testing with assistive technologies (VoiceOver + Safari, NVDA + Firefox/Chrome) and expert review; automated checks are not sufficient.
- **N6. Rollout.** Per-tenant flag, default `disabled`; existing Fuel Distribution and Scheduling tabs unchanged.
- **N7. Compatibility.** Existing endpoints, approval queue, agents and driver app keep working unchanged except: R25; the additive fields in the design; and the board-plan guards of R12.10 (MVP plan list filter, `BOARD_OWNED_PLAN` on approve/reject/replan, exception-replanning filter); and the R13.11 serialization: check-ins persist with compare-and-set (same errors as today), driver status transitions use the existing guarded order write (a concurrent change now returns a 409 that the driver app's queue already retries instead of being silently overwritten), and `in_transit` on a board run whose plan isn't `dispatched` returns 409. Agent-written plans behave exactly as today, apart from the guarded transition, which only refuses writes that would have overwritten a newer change.

## Out of scope

- Free-form node graph or canvas (research 1.8).
- Dropping onto the map; drawing routes by hand.
- Trailers and tractor–trailer coupling as separate assets: the platform models compartments on the truck (`truck_compartments.truck_id`) and has no trailer entity (Q3).
- Legacy `Job` scheduling (the Scheduling tab remains).
- Automatic publish (`active_auto` does not auto-publish).
- Overriding `block` checks.
- Customer-facing ETA notifications triggered by board edits beyond what dispatch already sends.
- Offline editing.
- Per-dispatcher private drafts or branching drafts.
- Multi-day lanes (a load belongs to exactly one service day).
- Client-side analytics SDKs.

## Open questions

Decided 2026-10-06: owner accepted all proposed defaults (Q1–Q16 accepted as written).

- **Q1. Default tab.** Proposed: Board is default when the flag is active (R1.2). Rationale: it becomes the primary dispatch surface; old tabs remain one click away.
- **Q2. Shifts.** No shift entity exists. Proposed: built-in presets Day 06:00–18:00 and Night 18:00–06:00 (next day), stored as a tenant board setting later; a load belongs to the service day of its planned start. Rationale: covers common fuel ops without a new admin surface.
- **Q3. Trailers.** Proposed: out of scope; the "Trucks" tray lists compartment-bearing units. Rationale: no trailer model exists; adding one is a data-model project.
- **Q4. Past days.** Proposed: read-only after the day ends, 7 days back. Rationale: history comes from the audit trail; editing the past has no operational meaning.
- **Q5. Unavailable checks.** Proposed: `warn` (overridable) except qualification, certification and dyed diesel, which `block` (R9.5). Rationale: those are regulatory and the cost of a wrong pass is a roadside or tax violation.
- **Q6. Shadow agent output.** Proposed: not shown (R16.5). Rationale: agent-overlay Req 9 intent is that shadow output does not influence operations; showing it to dispatchers would.
- **Q7. Publish permission.** Proposed: same roles as editing (`admin`, `dispatcher`). Rationale: no supervisor role exists; adding one is an auth change.
- **Q8. Late threshold.** Proposed: 15 minutes, tenant-configurable later. Rationale: typical window tolerance.
- **Q9. Removed published stop disposition.** Proposed: stays on the lane's "To reassign" shelf until placed or cancelled (R13.5). Rationale: a dispatched order cannot go back to `scheduled`/`confirmed` in the state machine, so "back to the tray" would be a lie.
- **Q10. Warning reason at drop vs publish.** Proposed: collected at publish review, optionally earlier inline (R9.4). Rationale: NN/g: no dialog on routine actions (F16).
- **Q11. Undo scope.** Proposed: own commands, this session, 50 steps (R11). Rationale: undoing a colleague's change is a conflict, not an undo.
- **Q12. Driver swap mid-load.** Proposed: the board refuses a driver change on a lane whose load has started (R13.3); a mid-route swap is handled outside the board. Rationale: the driver is physically on the truck with fuel loaded; the plan execution, check-ins and HOS all belong to that driver, and a swap is rare.
- **Q13. Dispatcher display name.** Proposed: the local part of the user's sign-in email (`ana@example.com` → "ana"), shown only to the same tenant's admins and dispatchers, with "Another dispatcher" as the fallback (design K17.1). Rationale: no profile name exists in the session or the user table; the email is the only human-readable identifier, and the local part keeps the domain off screens. Switch to a profile name when one exists.
- **Q14. Re-issuing a load the driver may be loading.** Proposed: information only (`driver_may_be_loading`, R13.7), shown in the review dialog when a not-started load's planned start is past; no acknowledgement and no block. Rationale: the system can't see rack loading, so a warning would fire on every late-starting load and train dispatchers to click through; the dispatcher calls the driver when it matters. Switch to a `warn` if a rack check-in signal is added.
- **Q15. Which orders a truncated tray keeps.** Proposed: the 1,000 earliest delivery windows, will-call orders with no window last, with filters applied on the server after truncation (R3.3, R3.8). Rationale: priority isn't stored on the order, so the store can't rank by it; the earliest windows are the most urgent work in practice, and tenants above 1,000 open orders a day are rare in the target market.
- **Q16. What dispatchers and drivers see when a re-publish fails part-way.** Proposed:
  - When the failure came before any order status changed (the usual cause is a driver starting a moved order), the board silently restores the previous routes. The lane shows "Not published: {reason}. Your previous route is still with the driver." and Retry.
  - When the failure came later, the board retries automatically, then holds the lanes read-only as "Recovering" with Retry and an assertive banner naming how many drivers can't see their routes.
  - Drivers whose order is mid-re-issue get "Your dispatcher is updating this route. Try again in a moment." on Start, and the app retries automatically.

  Rationale: an undo the driver never notices is the least surprising outcome. A half-applied re-publish can't be undone (orders can't move backwards), so finishing it is the only safe direction, and freezing the lane keeps the dispatcher from editing on top of it. Refusing a start for a few seconds is safer than letting a driver start a delivery whose route is being replaced. Alternative if pilots object: let drivers start during forward recovery and amend afterwards, which needs a new amend path.
