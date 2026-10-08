/**
 * In-process fake of the Dispatch Board API and socket for
 * `dispatch-board.spec.ts` (plan tasks 37–40). It answers every request the
 * board makes through Playwright routing, so no test reaches a backend or
 * staging. One `FakeBoard` can serve several browser contexts: a command
 * from one is broadcast to the others' sockets as `board_lanes_updated`
 * (the second-dispatcher conflict case).
 *
 * The command handling is a small stand-in for the backend engine: enough
 * placement rules (index, new load, best fit = first load) and version
 * checks to drive the UI, not a copy of `dispatch_board_engine.py`.
 */
import type { Page, Request, Route, WebSocketRoute } from "@playwright/test";
import {
  makeDriver,
  makeLane,
  makeLoad,
  makeSnapshot,
  makeStop,
  makeTrayOrder,
} from "../src/components/dispatch-board/state/testFixtures";
import type {
  BoardCommand,
  BoardSnapshot,
  CommandTarget,
  DriverSummary,
  LaneView,
  Load,
  PublishPreview,
  PublishStatus,
  TrayOrder,
  ValidateResponse,
} from "../src/services/dispatchBoardApi";

export const API = "http://localhost:8080/api";
export const ZONE = "America/Chicago";

export function todayInZone(zone = ZONE): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

export interface FixtureSize {
  lanes: number;
  stopsPerLane: number;
  trayOrders: number;
  drivers: number;
}

export interface Actor {
  userId: string;
  name: string;
}

interface Client {
  ws: WebSocketRoute;
  actor: Actor;
}

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export function truckId(i: number): string {
  return `T${String(i + 1).padStart(2, "0")}`;
}

export class FakeBoard {
  lanes = new Map<string, LaneView>();
  tray: TrayOrder[] = [];
  drivers: DriverSummary[] = [];
  draftVersion = 1;
  commands: { actor: Actor; body: BoardCommand }[] = [];
  validations = 0;
  publishes: { body: Record<string, unknown> }[] = [];
  private clients: Client[] = [];
  /** Socket messages held back per user until that user's next command. */
  private held = new Map<string, string[]>();
  private loadSeq = 100;
  /** Lane states to report, in order, on each publish status poll. */
  publishScript: PublishStatus["lanes"][number]["state"][] = [
    "publishing",
    "published",
  ];
  private publishPolls = 0;
  private publishTrucks: string[] = [];

  constructor(size: FixtureSize) {
    let n = 0;
    for (let i = 0; i < size.lanes; i++) {
      const t = truckId(i);
      const ids: string[] = [];
      for (let k = 0; k < size.stopsPerLane; k++) ids.push(`${1000 + n++}`);
      const loads = ids.length
        ? [
            makeLoad(`L-${t}`, ids, {
              stops: ids.map((id) =>
                makeStop(id, {
                  snapshot: {
                    ...makeStop(id).snapshot,
                    gallons_requested: 1500,
                  },
                }),
              ),
            }),
          ]
        : [];
      this.lanes.set(
        t,
        makeLane(t, 1, {
          loads,
          compartments: [1, 2, 3, 4].map((c) => ({
            compartment_id: `${t}-C${c}`,
            position_index: c - 1,
            capacity_l: 10000,
            accepts_products: [],
            state: "clean",
            last_loaded_product: null,
          })),
        }),
      );
    }
    for (let k = 0; k < size.trayOrders; k++) {
      const id = `${5000 + k}`;
      this.tray.push(
        makeTrayOrder(id, {
          customer_name: `QA- Customer ${k + 1}`,
          gallons_requested: 1200,
        }),
      );
    }
    for (let d = 0; d < size.drivers; d++) {
      this.drivers.push(
        makeDriver(`D${d + 1}`, { name: `QA- Driver ${d + 1}` }),
      );
    }
  }

  snapshot(serviceDate: string, only?: string[]): BoardSnapshot {
    const lanes = [...this.lanes.values()].filter(
      (l) => !only || only.includes(l.truck_id),
    );
    return makeSnapshot({
      service_date: serviceDate,
      timezone: ZONE,
      draft_version: this.draftVersion,
      lanes: clone(lanes),
      trays: only
        ? { orders: [], orders_truncated: false, drivers: [], trucks: [] }
        : {
            orders: clone(this.tray),
            orders_truncated: false,
            drivers: clone(this.drivers),
            trucks: [
              { truck_id: "T99", compartment_count: 4, capacity_l: 40000 },
            ],
          },
    });
  }

  validate(body: { candidates: string[] }): ValidateResponse {
    this.validations += 1;
    const results: ValidateResponse["results"] = {};
    for (const t of body.candidates) {
      results[t] = {
        outcome: "pass",
        worst_checks: [],
        reason: null,
        preview: {
          fill_by_compartment: {},
          insertion_index: null,
          load_id: null,
          eta_delta_minutes: null,
        },
      };
    }
    return { results, degraded_sources: [] };
  }

  private broadcast(
    lanes: LaneView[],
    actor: Actor,
    type: string,
    except: WebSocketRoute | null,
  ) {
    const msg = JSON.stringify({
      type: "board_lanes_updated",
      data: {
        service_date: todayInZone(),
        draft_version: this.draftVersion,
        actor: { user_id: actor.userId, name: actor.name },
        command_type: type,
        lanes: clone(lanes),
      },
    });
    for (const c of this.clients) {
      if (c.ws === except) continue;
      const queue = this.held.get(c.actor.userId);
      if (queue) queue.push(msg);
      else c.ws.send(msg);
    }
  }

  /**
   * Hold `userId`'s socket messages; they are delivered while that user's
   * next command is in flight, just before its answer (the R14.2 race).
   */
  holdBroadcastsFor(userId: string): void {
    this.held.set(userId, []);
  }

  private async releaseHeld(actor: Actor): Promise<void> {
    const queue = this.held.get(actor.userId);
    if (!queue) return;
    this.held.delete(actor.userId);
    const ws = this.clients.find((c) => c.actor.userId === actor.userId)?.ws;
    for (const msg of queue) ws?.send(msg);
    await new Promise((r) => setTimeout(r, 300));
  }

  private takeStops(orderIds: string[]): {
    stops: Load["stops"];
    from: string[];
  } {
    const stops: Load["stops"] = [];
    const from = new Set<string>();
    for (const id of orderIds) {
      const trayIdx = this.tray.findIndex((o) => o.order_id === id);
      if (trayIdx >= 0) {
        const o = this.tray.splice(trayIdx, 1)[0];
        stops.push(
          makeStop(id, {
            snapshot: {
              ...makeStop(id).snapshot,
              product_code: o.product_code,
              gallons_requested: o.gallons_requested,
            },
          }),
        );
        continue;
      }
      for (const lane of this.lanes.values()) {
        for (const load of lane.loads) {
          const i = load.stops.findIndex((s) => s.order_id === id);
          if (i >= 0) {
            stops.push(load.stops.splice(i, 1)[0]);
            from.add(lane.truck_id);
          }
        }
      }
    }
    return { stops, from: [...from] };
  }

  private place(lane: LaneView, stops: Load["stops"], target: CommandTarget) {
    let load =
      target.load_id && target.load_id !== "new"
        ? lane.loads.find((l) => l.load_id === target.load_id)
        : target.load_id === "new"
          ? undefined
          : lane.loads[0];
    if (!load) {
      load = makeLoad(`L-${this.loadSeq++}`, []);
      lane.loads.push(load);
    }
    const index = Math.min(
      target.index ?? load.stops.length,
      load.stops.length,
    );
    load.stops.splice(index, 0, ...stops);
  }

  private touched(cmd: BoardCommand): string[] {
    switch (cmd.type) {
      case "assign_orders":
      case "move_stops": {
        const from = [...this.lanes.values()]
          .filter((l) =>
            l.loads.some((ld) =>
              ld.stops.some((s) => cmd.order_ids.includes(s.order_id)),
            ),
          )
          .map((l) => l.truck_id);
        return [...new Set([...from, cmd.truck_id])];
      }
      case "move_load": {
        const from = [...this.lanes.values()].find((l) =>
          l.loads.some((ld) => ld.load_id === cmd.load_id),
        );
        return [...new Set([from?.truck_id ?? cmd.truck_id, cmd.truck_id])];
      }
      case "pair_driver": {
        const others = [...this.lanes.values()]
          .filter((l) => cmd.driver_id && l.driver_id === cmd.driver_id)
          .map((l) => l.truck_id);
        return [...new Set([cmd.truck_id, ...others])];
      }
      case "unassign_orders":
        return [...this.lanes.values()]
          .filter((l) =>
            l.loads.some((ld) =>
              ld.stops.some((s) => cmd.order_ids.includes(s.order_id)),
            ),
          )
          .map((l) => l.truck_id);
      default:
        return "truck_id" in cmd ? [cmd.truck_id] : [];
    }
  }

  command(
    body: BoardCommand,
    actor: Actor,
    from: WebSocketRoute | null,
  ): { status: number; json: unknown } {
    this.commands.push({ actor, body: clone(body) });
    const touched = this.touched(body);
    const stale = touched.filter(
      (t) =>
        this.lanes.has(t) &&
        body.expected_lane_versions[t] !== undefined &&
        body.expected_lane_versions[t] !== this.lanes.get(t)?.version,
    );
    if (stale.length) {
      return {
        status: 409,
        json: {
          error_code: "BOARD_LANE_CONFLICT",
          message: "The lane changed.",
          details: {
            lanes: clone(touched.map((t) => this.lanes.get(t))),
            missing_lanes: [],
          },
        },
      };
    }
    switch (body.type) {
      case "assign_orders":
      case "move_stops": {
        const lane = this.lanes.get(body.truck_id);
        if (!lane) return { status: 404, json: { error_code: "NOT_FOUND" } };
        const { stops } = this.takeStops(body.order_ids);
        this.place(lane, stops, body.target ?? {});
        break;
      }
      case "unassign_orders": {
        const { stops } = this.takeStops(body.order_ids);
        for (const s of stops) this.tray.unshift(makeTrayOrder(s.order_id));
        break;
      }
      case "pair_driver": {
        for (const l of this.lanes.values()) {
          if (body.driver_id && l.driver_id === body.driver_id) {
            l.driver_id = null;
            l.driver = null;
          }
        }
        const lane = this.lanes.get(body.truck_id);
        if (!lane) return { status: 404, json: { error_code: "NOT_FOUND" } };
        lane.driver_id = body.driver_id;
        lane.driver =
          this.drivers.find((d) => d.driver_id === body.driver_id) ?? null;
        break;
      }
      case "move_load": {
        let moved: Load | undefined;
        for (const l of this.lanes.values()) {
          const i = l.loads.findIndex((ld) => ld.load_id === body.load_id);
          if (i >= 0) moved = l.loads.splice(i, 1)[0];
        }
        const lane = this.lanes.get(body.truck_id);
        if (!moved || !lane)
          return { status: 404, json: { error_code: "NOT_FOUND" } };
        lane.loads.splice(Math.min(body.index, lane.loads.length), 0, moved);
        break;
      }
      case "add_lane": {
        this.lanes.set(body.truck_id, makeLane(body.truck_id, 0));
        break;
      }
      default:
        break;
    }
    const lanes: LaneView[] = [];
    for (const t of touched) {
      const lane = this.lanes.get(t);
      if (!lane) continue;
      lane.loads = lane.loads.filter((ld) => ld.stops.length > 0);
      lane.version += 1;
      lanes.push(lane);
    }
    this.draftVersion += 1;
    this.broadcast(lanes, actor, body.type, from);
    return {
      status: 200,
      json: {
        data: {
          draft_version: this.draftVersion,
          lanes: clone(lanes),
          checks: {},
          already_applied: false,
          audit_degraded: false,
        },
      },
    };
  }

  preview(trucks: string[]): PublishPreview {
    return {
      ready: true,
      not_ready: [],
      groups: trucks.map((t) => ({
        truck_ids: [t],
        kind: "first_publish",
        added_lanes: [],
      })),
      loads: trucks.flatMap((t) =>
        (this.lanes.get(t)?.loads ?? []).map((ld) => ({
          truck_id: t,
          load_id: ld.load_id,
          class: "new" as const,
          info: [],
        })),
      ),
      notifications: trucks
        .map((t) => this.lanes.get(t)?.driver)
        .filter((d): d is DriverSummary => Boolean(d))
        .map((d) => ({
          driver_id: d.driver_id,
          name: d.name,
          revoke_order_ids: [],
          assign_order_ids: [],
          route_updated: false,
        })),
      open_warnings: [],
      already_published: [],
    };
  }

  /** Each poll reports the next scripted state; the last one repeats. */
  publishStatus(publishId: string): PublishStatus {
    const state =
      this.publishScript[
        Math.min(this.publishPolls++, this.publishScript.length - 1)
      ];
    if (state === "published") {
      for (const t of this.publishTrucks) {
        const lane = this.lanes.get(t);
        if (!lane) continue;
        lane.state = "published";
        lane.ever_published = true;
        lane.publish = {
          ...lane.publish,
          state: "published",
          published_version: lane.version,
          plans: Object.fromEntries(
            lane.loads.map((ld, i) => [
              ld.load_id,
              {
                plan_id: `P-${t}-${i}`,
                route_id: `R-${t}-${i}`,
                run_id: `RUN-${t}-${i}`,
                revision: 1,
              },
            ]),
          ),
        };
      }
    }
    return {
      publish_id: publishId,
      lanes: this.publishTrucks.map((t) => ({
        truck_id: t,
        state,
        last_result: null,
      })),
      done: state !== "publishing",
    };
  }

  /** Routes every API call and socket of `page` to this fake. */
  async install(page: Page, actor: Actor): Promise<void> {
    // Every other socket (orders, plan execution, fleet): accept and stay
    // quiet. Registered first: the later, more specific route wins.
    await page.routeWebSocket(/\/ws\//, () => {});
    await page.routeWebSocket(/\/ws\/dispatch-board/, (ws) => {
      const client = { ws, actor };
      this.clients.push(client);
      ws.onClose(() => {
        this.clients = this.clients.filter((c) => c !== client);
      });
      ws.onMessage(() => {
        // Presence frames: nothing to answer.
      });
    });
    await page.route(`${API}/**`, (route) => this.handle(route, actor));
  }

  private async handle(route: Route, actor: Actor): Promise<void> {
    const req: Request = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api/, "");
    const json = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    const m = path.match(/^\/fuel\/board\/(\d{4}-\d{2}-\d{2})(\/.*)?$/);
    if (path === "/fuel/board/status")
      return json(200, { data: { mode: "active_gated" } });
    if (!m) return json(404, { error_code: "NOT_FOUND", message: "mock" });
    const [, date, rest = ""] = m;
    const body = req.postDataJSON?.() as Record<string, unknown> | null;
    if (req.method() === "GET" && rest === "") {
      const only = url.searchParams.get("lanes")?.split(",");
      return json(200, { data: this.snapshot(date, only) });
    }
    if (rest === "/validate")
      return json(200, {
        data: this.validate(body as { candidates: string[] }),
      });
    if (rest === "/commands") {
      await this.releaseHeld(actor);
      const from =
        this.clients.find((c) => c.actor.userId === actor.userId)?.ws ?? null;
      const res = this.command(body as unknown as BoardCommand, actor, from);
      return json(res.status, res.json);
    }
    if (rest === "/publish") {
      const lanes = (body?.lanes as { truck_id: string }[]) ?? [];
      const trucks = lanes.map((l) => l.truck_id);
      if (body?.dry_run === true)
        return json(200, { data: this.preview(trucks) });
      this.publishes.push({ body: body ?? {} });
      this.publishTrucks = trucks;
      this.publishPolls = 0;
      return json(202, {
        data: {
          publish_id: "PUB-1",
          lanes: trucks.map((t) => ({ truck_id: t, state: "queued" })),
          groups: trucks.map((t) => ({
            truck_ids: [t],
            kind: "first_publish",
            added_lanes: [],
          })),
          already_published: [],
        },
      });
    }
    const pub = rest.match(/^\/publish\/([^/]+)$/);
    if (pub) return json(200, { data: this.publishStatus(pub[1]) });
    if (rest.startsWith("/history"))
      return json(200, { data: { items: [], next_cursor: null } });
    return json(404, { error_code: "NOT_FOUND", message: "mock" });
  }
}
