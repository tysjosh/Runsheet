"""Long-running role-played soak: admin, dispatcher and driver personas.

Drives the live API the way three real users would, continuously, for a fixed
duration. Distinct from ``scripts/loadtest.py``: that one measures throughput by
hammering a fixed endpoint mix as one identity. This one holds a **separate
authenticated session per role** and walks realistic workflows, so it exercises
the parts that only appear over time — session expiry, the transactional-outbox
relay, the ~15 periodic sweeps, per-tenant feature-flag reads, and the order
state machine end to end.

Why role-played rather than one super-user: ``auth.authorization.require_role``
is exact-match with no implication graph, so an ``admin`` genuinely cannot reach
the driver surface and a ``driver`` genuinely cannot read orders. Verified live
before this script was written:

    admin       GET /api/orders        200      GET /api/driver/work   403
    dispatcher  GET /api/orders        200      GET /api/driver/work   403
    driver      GET /api/orders        403      GET /api/driver/work   200

So a persona reaching past its role is a **finding**, not noise. Every action
records the status it got, and ``unexpected_denials`` counts a 401/403 on an
action the role was supposed to be allowed — which is the signal worth watching.

Durability. This is designed to outlive the shell that started it: every action
appends to a JSONL file, a summary JSON is rewritten every minute, and SIGINT /
SIGTERM writes a final report before exiting. Nothing is held only in memory.

Data footprint. Orders are the only thing created in volume, capped by
``--orders-per-hour``, and every one is tagged with the run id in ``po_number``
so the run can be identified and cleaned up afterwards.

Persona setup (once). The four accounts must exist in ``auth_users``, be pushed
into SuperTokens, and share a password. Only the driver needs creating — the
others ship with the demo tenant — and it must carry a ``driver_id`` or the
dispatcher's assignments never reach its queue::

    psql -c "INSERT INTO auth_users (email, tenant_id, roles, has_pii_access, driver_id)
             VALUES ('driver1@demo.runsheet.test','demo-tenant',ARRAY['driver'],false,'DRV-001')
             ON CONFLICT (email) DO UPDATE SET roles=ARRAY['driver'], driver_id='DRV-001';"

    python -m scripts.provision_auth_users
    for e in admin@runsheet.com dispatcher@demo.runsheet.test \\
             driver1@demo.runsheet.test staff@demo.runsheet.test; do
        python -m scripts.set_user_password "$e" --password "$SOAK_PASSWORD"
    done

Usage:

    ENVIRONMENT=development ./venv/bin/python -m scripts.soak_personas \\
        --duration-hours 12 --password "$SOAK_PASSWORD" --out-dir ./soak-out

    # look at a run in progress
    cat soak-out/<run-id>/summary.json | python -m json.tool
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:8080"

#: Personas and the account each signs in as. Roles are what the backend
#: actually grants; the scenario menus below never intentionally cross them.
PERSONA_ACCOUNTS: Dict[str, str] = {
    "admin": "admin@runsheet.com",
    "dispatcher": "dispatcher@demo.runsheet.test",
    "driver": "driver1@demo.runsheet.test",
    # Runsheet staff. Present because the pricing and billing surfaces
    # (accounts, AR aging, price books) require ``platform_admin`` via
    # COMMERCE_STAFF_ROLES — a tenant's own admin is deliberately refused there,
    # since the customer's ERP owns price and invoice. Without this persona
    # those surfaces would either go uncovered or show up as false findings.
    "staff": "staff@demo.runsheet.test",
}

#: Duty states a *driver* may set on themselves. ``DUTY_STATUSES`` in
#: driver/services/duty_status_service.py also contains ``inactive``, but the
#: endpoint refuses it with 403 "inactive is set by an administrator, not by a
#: driver" — so the driver-settable vocabulary is these three.
DRIVER_SETTABLE_DUTY_STATUSES = ("active", "on_break", "off_duty")

#: The driver persona's linked driver record, so dispatcher-assigned work lands
#: in this persona's queue.
DRIVER_ID = "DRV-001"

#: Order lifecycle, from fuel/order_state_machine.py::VALID_STATUS_TRANSITIONS.
#: ``scheduled``/``dispatched``/``in_transit`` additionally require a delivery
#: window (STATUSES_REQUIRING_WINDOW), which is why created orders always carry
#: one — without it the order cannot legally leave ``confirmed``.
DISPATCHER_ADVANCE = {
    "placed": "confirmed",
    "confirmed": "scheduled",
    "scheduled": "dispatched",
}
DRIVER_ADVANCE = {
    "dispatched": "in_transit",
    "in_transit": "delivered",
}
TERMINAL_STATUSES = {"delivered", "failed", "cancelled"}

#: Sampled from live data so payloads use values the API already accepts.
PRODUCT_CODES = ("DIESEL_2", "HEATING_OIL", "GASOLINE_REG")
CALL_TYPES = ("will_call", "keep_full")
DEPOT_LAT, DEPOT_LON = 29.76, -95.36

#: Role-flavoured prompts for the real LLM surface (POST /api/chat). These cost
#: money per call, so they are rate-limited separately from everything else.
LLM_PROMPTS: Dict[str, Sequence[str]] = {
    "admin": (
        "Summarise our accounts receivable aging and flag anything over 60 days.",
        "Which customers are closest to their credit limit right now?",
        "How many invoices are still in draft, and what is the total value?",
        "Give me a short health summary of the fleet.",
    ),
    "dispatcher": (
        "Which orders are unassigned and closest to their delivery window?",
        "Are any deliveries running late today?",
        "Which trucks are available for a diesel run this afternoon?",
        "Show me the tanks most at risk of running out in the next 48 hours.",
    ),
    "driver": (
        "What deliveries do I have left today?",
        "What are the safety steps before loading gasoline into a compartment?",
        "How many hours do I have left before I hit my limit?",
    ),
    "staff": (
        "Which tenants have invoices stuck pending an ERP push?",
        "Summarise rack price coverage across our terminals.",
        "Are there accounts over their credit limit this week?",
    ),
}


# ---------------------------------------------------------------------------
# Metrics + event log
# ---------------------------------------------------------------------------


@dataclass
class ActionStat:
    calls: int = 0
    ok: int = 0
    denied: int = 0
    errors: int = 0
    guard_rejected: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    status_codes: Counter = field(default_factory=Counter)

    def record(
        self,
        status: Optional[int],
        elapsed_ms: float,
        error: Optional[str],
        expected_statuses: Sequence[int] = (),
    ) -> None:
        self.calls += 1
        # Keep the latency list bounded: a 12-hour run would otherwise grow it
        # without limit, and percentiles over a reservoir are good enough.
        if len(self.latencies_ms) < 5000:
            self.latencies_ms.append(elapsed_ms)
        if error is not None:
            self.errors += 1
            self.status_codes["exception"] += 1
            return
        self.status_codes[str(status)] += 1
        if status is None:
            self.errors += 1
        elif status in (401, 403):
            self.denied += 1
        elif status < 400:
            self.ok += 1
        elif status in expected_statuses:
            # A guard rejecting a racing or invalid transition is the system
            # working. Counted separately so it never inflates the error rate.
            self.guard_rejected += 1
        else:
            self.errors += 1

    def percentile(self, p: float) -> Optional[float]:
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        idx = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
        return round(ordered[idx], 1)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "ok": self.ok,
            "denied": self.denied,
            "errors": self.errors,
            "guard_rejected": self.guard_rejected,
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
            "status_codes": dict(self.status_codes),
        }


class Recorder:
    """Owns the on-disk record so a run survives the shell that launched it."""

    def __init__(self, out_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self.dir = out_dir / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.summary_path = self.dir / "summary.json"
        self._events = self.events_path.open("a", buffering=1)
        self.started_at = datetime.now(timezone.utc)
        self.stats: Dict[str, Dict[str, ActionStat]] = defaultdict(
            lambda: defaultdict(ActionStat)
        )
        self.unexpected_denials: List[Dict[str, Any]] = []
        self.notes: List[Dict[str, Any]] = []
        self.orders_created = 0
        self.llm_calls = 0
        self.reauths = 0

    def event(self, **payload: Any) -> None:
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        payload["run_id"] = self.run_id
        self._events.write(json.dumps(payload, default=str) + "\n")

    def record_action(
        self,
        persona: str,
        action: str,
        status: Optional[int],
        elapsed_ms: float,
        error: Optional[str],
        *,
        expect_allowed: bool = True,
        detail: Optional[Dict[str, Any]] = None,
        expected_statuses: Sequence[int] = (),
    ) -> None:
        self.stats[persona][action].record(
            status, elapsed_ms, error, expected_statuses
        )
        entry = {
            "kind": "action",
            "persona": persona,
            "action": action,
            "status": status,
            "ms": round(elapsed_ms, 1),
        }
        if error:
            entry["error"] = error[:300]
        if detail:
            entry["detail"] = detail
        self.event(**entry)

        if expect_allowed and status in (401, 403):
            # The role was supposed to be able to do this. Either the scenario
            # menu is wrong or authorization changed — both worth surfacing.
            record = {
                "persona": persona,
                "action": action,
                "status": status,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            if len(self.unexpected_denials) < 500:
                self.unexpected_denials.append(record)
            self.event(kind="unexpected_denial", **record)

    def note(self, message: str, **extra: Any) -> None:
        entry = {"message": message, **extra}
        if len(self.notes) < 500:
            self.notes.append({**entry, "at": datetime.now(timezone.utc).isoformat()})
        self.event(kind="note", **entry)

    def summary(self, *, finished: bool = False) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        totals = Counter()
        for actions in self.stats.values():
            for stat in actions.values():
                totals["calls"] += stat.calls
                totals["ok"] += stat.ok
                totals["denied"] += stat.denied
                totals["errors"] += stat.errors
                totals["guard_rejected"] += stat.guard_rejected
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "updated_at": now.isoformat(),
            "elapsed_seconds": round((now - self.started_at).total_seconds(), 1),
            "finished": finished,
            "totals": dict(totals),
            "orders_created": self.orders_created,
            "llm_calls": self.llm_calls,
            "reauths": self.reauths,
            "unexpected_denials": len(self.unexpected_denials),
            "unexpected_denial_sample": self.unexpected_denials[:20],
            "per_persona": {
                persona: {action: stat.as_dict() for action, stat in actions.items()}
                for persona, actions in self.stats.items()
            },
            "notes_tail": self.notes[-15:],
        }

    def flush_summary(self, *, finished: bool = False) -> None:
        tmp = self.summary_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.summary(finished=finished), indent=2, default=str))
        tmp.replace(self.summary_path)

    def close(self) -> None:
        self.flush_summary(finished=True)
        self._events.close()


# ---------------------------------------------------------------------------
# Persona session
# ---------------------------------------------------------------------------


class Persona:
    """One authenticated role, its HTTP client, and its scenario loop.

    Re-authenticates on 401. That is not defensive padding: sessions are one
    hour (``SESSION_LIFETIME_SECONDS=3600``), so a twelve-hour run without this
    would spend eleven hours reporting 401s and prove nothing.
    """

    def __init__(
        self,
        name: str,
        email: str,
        password: str,
        base_url: str,
        recorder: Recorder,
        rng: random.Random,
    ) -> None:
        self.name = name
        self.email = email
        self.password = password
        self.recorder = recorder
        self.rng = rng
        self.client = httpx.AsyncClient(base_url=base_url, timeout=60.0)
        self.token: Optional[str] = None
        # Order ids this persona is shepherding, with last-known status.
        self.tracked: Dict[str, str] = {}

    # -- auth ---------------------------------------------------------------

    async def sign_in(self) -> bool:
        try:
            resp = await self.client.post(
                "/auth/signin",
                json={
                    "formFields": [
                        {"id": "email", "value": self.email},
                        {"id": "password", "value": self.password},
                    ]
                },
                headers={"rid": "emailpassword"},
            )
        except Exception as exc:  # noqa: BLE001 — retried by the caller
            self.recorder.note(f"{self.name}: signin transport error: {exc}")
            return False
        token = resp.headers.get("st-access-token")
        if not token:
            self.recorder.note(
                f"{self.name}: signin returned no token",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        self.token = token
        return True

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- request plumbing ---------------------------------------------------

    async def call(
        self,
        action: str,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        expect_allowed: bool = True,
        detail: Optional[Dict[str, Any]] = None,
        expected_statuses: Sequence[int] = (),
    ) -> Tuple[Optional[int], Any]:
        """Issue one request, record it, and hand back ``(status, parsed_body)``.

        ``expected_statuses`` names 4xx results that are a guard doing its job
        rather than a defect — a 409 from the order state machine when two
        actors race the same order, for instance. Counting those as errors would
        bury the real ones in a twelve-hour report.
        """
        started = time.monotonic()
        status: Optional[int] = None
        error: Optional[str] = None
        body: Any = None
        try:
            resp = await self.client.request(
                method, path, json=json_body, headers=self.headers
            )
            status = resp.status_code
            if status == 401 and self.token is not None:
                # Session almost certainly expired. Re-auth once and retry so a
                # long run keeps working instead of flatlining after an hour.
                if await self.sign_in():
                    self.recorder.reauths += 1
                    self.recorder.note(f"{self.name}: re-authenticated", action=action)
                    resp = await self.client.request(
                        method, path, json=json_body, headers=self.headers
                    )
                    status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                # Generous cap: /api/chat streams SSE, and truncating at a few
                # hundred characters would silently discard model output.
                body = resp.text[:8000]
        except Exception as exc:  # noqa: BLE001 — one action must never kill the loop
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = (time.monotonic() - started) * 1000.0
        self.recorder.record_action(
            self.name,
            action,
            status,
            elapsed_ms,
            error,
            expect_allowed=expect_allowed,
            detail=detail,
            expected_statuses=expected_statuses,
        )
        return status, body

    async def close(self) -> None:
        await self.client.aclose()


def _items(body: Any) -> List[Dict[str, Any]]:
    """Pull a list of records out of the several envelope shapes in this API."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ("items", "orders", "data", "results", "jobs", "work", "forecasts"):
            value = body.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _jitter_coords(rng: random.Random) -> Tuple[float, float]:
    """A delivery point a few km from the depot, so routing has real geometry."""
    return (
        round(DEPOT_LAT + rng.uniform(-0.25, 0.25), 5),
        round(DEPOT_LON + rng.uniform(-0.25, 0.25), 5),
    )


# ---------------------------------------------------------------------------
# Scenarios — dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """Order intake and progression, plus the planning surface.

    Owns order creation because that is the only volume-producing action, and
    keeping it in one persona makes the rate cap meaningful.
    """

    def __init__(self, persona: Persona, budget: "RateBudget") -> None:
        self.p = persona
        self.budget = budget

    async def browse_orders(self) -> None:
        status, body = await self.p.call(
            "orders.list", "GET", "/api/orders?limit=25"
        )
        if status and status < 300:
            for order in _items(body):
                oid, st = order.get("order_id"), order.get("status")
                # Adopt in-flight orders so progression is not limited to ones
                # this run created — the backlog is real work too.
                if oid and st and st not in TERMINAL_STATUSES and len(self.p.tracked) < 60:
                    self.p.tracked.setdefault(oid, st)

    async def create_order(self) -> None:
        if not self.budget.take():
            return
        lat, lon = _jitter_coords(self.p.rng)
        # A delivery window is attached at creation because scheduled /
        # dispatched / in_transit are gated on one (STATUSES_REQUIRING_WINDOW);
        # without it the order could never leave `confirmed`.
        start = datetime.now(timezone.utc) + timedelta(hours=self.p.rng.randint(1, 6))
        payload = {
            "customer_id": "CUST-001",
            "customer_name": f"Soak Customer {self.p.rng.randint(100, 999)}",
            "ship_to_address": f"{self.p.rng.randint(1, 999)} Soak Ave, Houston, TX",
            "ship_to_lat": lat,
            "ship_to_lon": lon,
            "product_code": self.p.rng.choice(PRODUCT_CODES),
            "gallons_requested": float(self.p.rng.randrange(100, 900, 50)),
            "call_type": self.p.rng.choice(CALL_TYPES),
            "delivery_window_start": start.isoformat(),
            "delivery_window_end": (start + timedelta(hours=4)).isoformat(),
            # Run id on every created order so this run's data is identifiable
            # and removable afterwards.
            "po_number": f"SOAK-{self.p.recorder.run_id}",
            "client_event_id": str(uuid.uuid4()),
        }
        status, body = await self.p.call(
            "orders.create", "POST", "/api/orders", json_body=payload
        )
        if status and status < 300 and isinstance(body, dict):
            oid = body.get("order_id")
            if oid:
                self.p.tracked[oid] = body.get("status", "placed")
                self.p.recorder.orders_created += 1

    async def advance_order(self) -> None:
        """Walk one tracked order one legal step toward dispatch."""
        candidates = [
            (oid, st) for oid, st in self.p.tracked.items() if st in DISPATCHER_ADVANCE
        ]
        if not candidates:
            return
        oid, st = self.p.rng.choice(candidates)
        new_status = DISPATCHER_ADVANCE[st]
        status, body = await self.p.call(
            "orders.status",
            "PATCH",
            f"/api/orders/{oid}/status",
            json_body={"new_status": new_status, "reason": "soak progression"},
            detail={"from": st, "to": new_status},
            # 409 = the state machine refused a stale or racing transition, 422 =
            # a guard (e.g. missing delivery window) rejected it. Both are the
            # system defending itself, not a fault.
            expected_statuses=(409, 422),
        )
        if status and status < 300:
            self.p.tracked[oid] = new_status
            # Hand it to the driver persona the moment it is schedulable, so the
            # driver has real assigned work rather than synthetic ids.
            if new_status == "scheduled":
                await self.p.call(
                    "orders.assign",
                    "PATCH",
                    f"/api/orders/{oid}/assign",
                    json_body={"driver_id": DRIVER_ID},
                    # 409 when the order already carries an assignment — the
                    # guard working, not a defect.
                    expected_statuses=(409,),
                )
        elif status in (409, 422):
            # Lost a race, or the guard rejected it. Re-read rather than guess.
            _, fresh = await self.p.call("orders.get", "GET", f"/api/orders/{oid}")
            if isinstance(fresh, dict) and fresh.get("status"):
                self.p.tracked[oid] = fresh["status"]

    async def hold_and_release(self) -> None:
        """Exercise the hold path, which is a distinct guard from status moves."""
        holdable = [oid for oid, st in self.p.tracked.items() if st == "placed"]
        if not holdable:
            return
        oid = self.p.rng.choice(holdable)
        status, _ = await self.p.call(
            "orders.hold",
            "POST",
            f"/api/orders/{oid}/hold",
            # The field is hold_reason, not reason — the latter was a 422.
            json_body={"hold_reason": "soak hold check"},
        )
        if status and status < 300:
            self.p.tracked[oid] = "on_hold"
            await asyncio.sleep(self.p.rng.uniform(1.0, 4.0))
            # The body is required (ReleaseHoldRequest) even though every field
            # in it is optional; omitting it entirely is a 422.
            released, _ = await self.p.call(
                "orders.release_hold",
                "POST",
                f"/api/orders/{oid}/release-hold",
                json_body={"notes": "soak release"},
            )
            if released and released < 300:
                self.p.tracked[oid] = "placed"

    async def read_dashboards(self) -> None:
        for action, path in (
            ("fleet.summary", "/api/fleet/summary"),
            ("jobs.active", "/api/scheduling/jobs/active"),
            ("jobs.delayed", "/api/scheduling/jobs/delayed"),
            ("forecasts.list", "/api/fuel/mvp/forecasts?limit=10"),
            ("tanks.list", "/api/fuel/mvp/customer-tanks?limit=10"),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.4, 1.5))

    async def generate_plan(self) -> None:
        """The planning pipeline — four agents, ~1s single-shot, never soaked."""
        status, body = await self.p.call(
            "plan.generate", "POST", "/api/fuel/mvp/plan/generate", json_body={}
        )
        if status and status < 300 and isinstance(body, dict):
            plan_id = body.get("plan_id") or body.get("run_id")
            if plan_id:
                await self.p.call(
                    "plan.get", "GET", f"/api/fuel/mvp/plan/{plan_id}"
                )

    def menu(self) -> Sequence[Tuple[int, str, Callable]]:
        return (
            (26, "browse_orders", self.browse_orders),
            (16, "create_order", self.create_order),
            (26, "advance_order", self.advance_order),
            (18, "read_dashboards", self.read_dashboards),
            (6, "hold_and_release", self.hold_and_release),
            (8, "generate_plan", self.generate_plan),
        )


# ---------------------------------------------------------------------------
# Scenarios — driver
# ---------------------------------------------------------------------------


class Driver:
    """The mobile-app surface: own work queue, telemetry, duty status, POD."""

    def __init__(self, persona: Persona) -> None:
        self.p = persona
        self.on_duty = False

    async def check_in(self) -> None:
        await self.p.call("driver.me", "GET", "/api/driver/me")
        # Values come from DUTY_STATUSES; "on_duty"/"driving" are not in the
        # service's vocabulary and were rejected with 400.
        next_status = (
            "active"
            if not self.on_duty
            else self.p.rng.choice(DRIVER_SETTABLE_DUTY_STATUSES)
        )
        status, _ = await self.p.call(
            "driver.duty_status",
            "POST",
            "/api/driver/duty-status",
            json_body={"status": next_status},
            detail={"duty_status": next_status},
        )
        if status and status < 300:
            self.on_duty = next_status in ("active", "on_break")

    async def read_work(self) -> None:
        status, body = await self.p.call("driver.work", "GET", "/api/driver/work")
        if status and status < 300:
            for item in _items(body):
                oid = item.get("order_id") or item.get("id")
                st = item.get("status")
                if oid and st and st not in TERMINAL_STATUSES:
                    self.p.tracked[oid] = st

    async def advance_delivery(self) -> None:
        candidates = [
            (oid, st) for oid, st in self.p.tracked.items() if st in DRIVER_ADVANCE
        ]
        if not candidates:
            return
        oid, st = self.p.rng.choice(candidates)
        new_status = DRIVER_ADVANCE[st]
        status, _ = await self.p.call(
            "driver.order_status",
            "POST",
            f"/api/driver/orders/{oid}/status",
            json_body={"status": new_status},
            detail={"from": st, "to": new_status},
            expected_statuses=(409, 422),
        )
        if status and status < 300:
            self.p.tracked[oid] = new_status
            if new_status == "delivered":
                self.p.tracked.pop(oid, None)

    async def send_breadcrumbs(self) -> None:
        lat, lon = _jitter_coords(self.p.rng)
        now = datetime.now(timezone.utc)
        # Field names are latitude / longitude / sample_timestamp. The obvious
        # lat / lon / recorded_at guess was rejected 422 on every call.
        samples = [
            {
                "latitude": round(lat + i * 0.001, 6),
                "longitude": round(lon + i * 0.001, 6),
                "sample_timestamp": (
                    now - timedelta(seconds=30 * (3 - i))
                ).isoformat(),
            }
            for i in range(3)
        ]
        await self.p.call(
            "driver.breadcrumbs",
            "POST",
            "/api/driver/telemetry/breadcrumbs",
            json_body={"samples": samples},
        )

    async def read_status_surfaces(self) -> None:
        # duty-status/history requires an explicit range; omitting it is a 422.
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        # Three separate requirements, each found by being rejected:
        #   * range_start / range_end are mandatory (422 without them)
        #   * driver_id is mandatory (400, details={"required": "driver_id"})
        #   * the bounds must be DATE-only. A full ISO-8601 timestamp is
        #     rejected 400 "range_start and range_end must be ISO-8601
        #     timestamps", which is the opposite of what the message says.
        history = (
            "/api/driver/duty-status/history"
            f"?range_start={start.date().isoformat()}"
            f"&range_end={end.date().isoformat()}"
            f"&driver_id={DRIVER_ID}"
        )
        for action, path in (
            ("driver.hos", "/api/driver/hos"),
            ("driver.qualifications", "/api/driver/qualifications"),
            ("driver.duty_history", history),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.3, 1.0))

    async def raise_exception(self) -> None:
        """Occasional real-world friction: an exception against live work."""
        active = [oid for oid, st in self.p.tracked.items() if st == "in_transit"]
        if not active:
            return
        oid = self.p.rng.choice(active)
        await self.p.call(
            "driver.exception",
            "POST",
            f"/api/driver/orders/{oid}/exceptions",
            # exception_type, severity and note are all required, and the first
            # two are enums — invented values ("access_blocked", "traffic_delay")
            # and a "notes" key were a 422.
            json_body={
                "exception_type": self.p.rng.choice(
                    (
                        "road_closure",
                        "customer_unavailable",
                        "access_denied",
                        "weather",
                        "other",
                    )
                ),
                "severity": self.p.rng.choice(("low", "medium", "high")),
                "note": "soak run synthetic exception",
            },
        )

    def menu(self) -> Sequence[Tuple[int, str, Callable]]:
        return (
            (30, "read_work", self.read_work),
            (24, "advance_delivery", self.advance_delivery),
            (20, "send_breadcrumbs", self.send_breadcrumbs),
            (12, "read_status_surfaces", self.read_status_surfaces),
            (8, "check_in", self.check_in),
            (6, "raise_exception", self.raise_exception),
        )


# ---------------------------------------------------------------------------
# Scenarios — admin
# ---------------------------------------------------------------------------


class Admin:
    """Commercial and oversight surfaces, plus the agent approval queue.

    Deliberately read-heavy on money. Invoice finalize and void are reachable by
    this role but create or destroy financial records with real numbering side
    effects, so a twelve-hour unattended run does not touch them — the approval
    queue gives a genuine write path without that consequence.
    """

    def __init__(self, persona: Persona) -> None:
        self.p = persona

    async def review_commerce(self) -> None:
        # Customers and invoices only: COMMERCE_OPS_ROLES is ("admin",
        # "dispatcher"). Accounts, AR aging and price books need platform_admin
        # and belong to the staff persona.
        for action, path in (
            ("commerce.customers", "/api/commerce/customers?limit=10"),
            ("commerce.invoices", "/api/commerce/invoices?limit=10"),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.4, 1.6))

    async def probe_role_boundary(self) -> None:
        """Confirm the tenant admin is still refused the ERP-owned surfaces.

        Marked ``expect_allowed=False``, so a 403 here is the pass and a 200
        would be the finding — a tenant admin gaining an editable second copy of
        price and invoice data would be a real authorization regression.
        """
        for action, path in (
            ("boundary.accounts", "/api/commerce/accounts?limit=1"),
            ("boundary.price_books", "/api/commerce/price-books"),
        ):
            status, _ = await self.p.call(
                action, "GET", path, expect_allowed=False
            )
            if status is not None and status < 300:
                self.p.recorder.note(
                    "AUTHORIZATION REGRESSION: tenant admin reached a "
                    "platform_admin-only commerce surface",
                    path=path,
                    status=status,
                )
            await asyncio.sleep(self.p.rng.uniform(0.3, 0.9))

    async def create_customer(self) -> None:
        """A low-volume, clearly-tagged master-data write."""
        suffix = self.p.rng.randint(1000, 9999)
        await self.p.call(
            "commerce.create_customer",
            "POST",
            "/api/commerce/customers",
            json_body={
                "display_name": f"Soak Co {suffix} [{self.p.recorder.run_id}]",
                "status": "active",
            },
        )

    async def review_agents(self) -> None:
        for action, path in (
            ("agent.health", "/api/agent/health"),
            ("agent.activity", "/api/agent/activity?limit=20"),
            ("agent.activity_stats", "/api/agent/activity/stats"),
            ("agent.autonomy", "/api/agent/config/autonomy"),
            ("agent.feedback_stats", "/api/agent/feedback/stats"),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.4, 1.2))

    async def clear_approval_queue(self) -> None:
        """Approve or reject whatever the autonomous agents have queued.

        This is the write path that matters most over a long run: the approval
        expiry sweep runs every 5 minutes, so a queue that is being worked keeps
        that loop honest.
        """
        status, body = await self.p.call(
            "agent.approvals", "GET", "/api/agent/approvals"
        )
        if not status or status >= 300:
            return
        pending = [
            item
            for item in _items(body)
            if str(item.get("status", "")).lower() in ("pending", "awaiting_approval", "")
        ]
        if not pending:
            return
        item = self.p.rng.choice(pending)
        action_id = item.get("action_id") or item.get("approval_id") or item.get("id")
        if not action_id:
            return
        # Mostly approve, sometimes reject — both branches deserve exercise.
        if self.p.rng.random() < 0.7:
            await self.p.call(
                "agent.approve",
                "POST",
                f"/api/agent/approvals/{action_id}/approve",
                json_body={"notes": "soak run approval"},
            )
        else:
            await self.p.call(
                "agent.reject",
                "POST",
                f"/api/agent/approvals/{action_id}/reject",
                json_body={"reason": "soak run rejection"},
            )

    async def review_fleet_and_compliance(self) -> None:
        for action, path in (
            ("fleet.assets", "/api/fleet/assets?limit=10"),
            ("fleet.trucks", "/api/fleet/trucks"),
            ("compliance.kfactor", "/api/compliance/kfactor/dashboard"),
            ("inventory.items", "/api/inventory/items?limit=10"),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.4, 1.4))

    def menu(self) -> Sequence[Tuple[int, str, Callable]]:
        return (
            (26, "review_commerce", self.review_commerce),
            (24, "review_agents", self.review_agents),
            (18, "clear_approval_queue", self.clear_approval_queue),
            (20, "review_fleet_and_compliance", self.review_fleet_and_compliance),
            (8, "create_customer", self.create_customer),
            (4, "probe_role_boundary", self.probe_role_boundary),
        )


class Staff:
    """Runsheet staff (``admin`` + ``platform_admin``).

    Covers the pricing and billing surfaces the tenant admin is refused, and the
    cross-tenant capability that ``platform_admin`` exists to grant.
    """

    def __init__(self, persona: Persona) -> None:
        self.p = persona

    async def review_billing(self) -> None:
        for action, path in (
            ("staff.accounts", "/api/commerce/accounts?limit=10"),
            ("staff.ar_aging", "/api/commerce/ar-aging"),
            ("staff.ar_aging_history", "/api/commerce/ar-aging/history"),
            ("staff.price_books", "/api/commerce/price-books"),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.5, 1.8))

    async def inspect_account_aging(self) -> None:
        status, body = await self.p.call(
            "staff.accounts_for_aging", "GET", "/api/commerce/accounts?limit=10"
        )
        if not status or status >= 300:
            return
        accounts = _items(body)
        if not accounts:
            return
        account = self.p.rng.choice(accounts)
        account_id = account.get("account_id") or account.get("id")
        if account_id:
            await self.p.call(
                "staff.account_aging",
                "GET",
                f"/api/commerce/accounts/{account_id}/aging",
            )

    async def review_platform(self) -> None:
        for action, path in (
            ("staff.integrations", "/api/integrations/intake-channels"),
            ("staff.terminals", "/api/fuel/terminals?limit=10"),
            ("staff.supplier_contracts", "/api/fuel/supplier-contracts?limit=10"),
            # /api/fuel/storm-mode is not a route; the readable one is /status.
            ("staff.storm_mode", "/api/fuel/storm-mode/status"),
        ):
            await self.p.call(action, "GET", path)
            await asyncio.sleep(self.p.rng.uniform(0.5, 1.5))

    def menu(self) -> Sequence[Tuple[int, str, Callable]]:
        return (
            (40, "review_billing", self.review_billing),
            (30, "review_platform", self.review_platform),
            (30, "inspect_account_aging", self.inspect_account_aging),
        )


# ---------------------------------------------------------------------------
# Rate budgets
# ---------------------------------------------------------------------------


class RateBudget:
    """Token bucket for the actions whose cost is real: writes and LLM calls."""

    def __init__(self, per_hour: float) -> None:
        self.per_hour = max(0.0, per_hour)
        self.interval = (3600.0 / self.per_hour) if self.per_hour > 0 else None
        self._next_at = time.monotonic()

    def take(self) -> bool:
        if self.interval is None:
            return False
        now = time.monotonic()
        if now < self._next_at:
            return False
        self._next_at = now + self.interval
        return True


async def maybe_chat(persona: Persona, budget: RateBudget) -> None:
    """Hit the real LLM surface, rate-limited because it bills per call."""
    if not budget.take():
        return
    prompts = LLM_PROMPTS.get(persona.name) or ("Give me a status summary.",)
    prompt = persona.rng.choice(prompts)
    status, body = await persona.call(
        "chat.message",
        "POST",
        "/api/chat",
        json_body={"message": prompt, "session_id": f"soak-{persona.name}"},
        detail={"prompt": prompt[:80]},
    )
    if status and status < 300:
        persona.recorder.llm_calls += 1
        reply = _extract_chat_reply(body)
        persona.recorder.event(
            kind="llm",
            persona=persona.name,
            prompt=prompt,
            reply_chars=len(reply),
            reply_head=reply[:400],
        )


def _extract_chat_reply(body: Any) -> str:
    """Pull the model's text out of ``POST /api/chat``.

    The endpoint streams Server-Sent Events rather than returning a JSON object:

        data: {"type": "text", "content": "There are 3 unassigned orders."}
        data: {"type": "done"}

    so ``body`` arrives as raw text. Reading it as JSON and looking for a
    ``response`` key logged every reply as empty, which would have made a
    twelve-hour run look like the LLM was returning nothing.
    """
    if isinstance(body, dict):
        return str(body.get("response") or body.get("message") or "")
    if not isinstance(body, str):
        return ""
    chunks: List[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "text":
            chunks.append(str(event.get("content", "")))
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def weighted_pick(rng: random.Random, menu: Sequence[Tuple[int, str, Callable]]):
    total = sum(w for w, _, _ in menu)
    roll = rng.uniform(0, total)
    upto = 0.0
    for weight, name, fn in menu:
        upto += weight
        if roll <= upto:
            return name, fn
    return menu[-1][1], menu[-1][2]


async def persona_loop(
    persona: Persona,
    menu: Sequence[Tuple[int, str, Callable]],
    llm_budget: RateBudget,
    deadline: float,
    stop: asyncio.Event,
    think_range: Tuple[float, float],
) -> None:
    """Drive one persona until the deadline or a stop signal."""
    while not stop.is_set() and time.monotonic() < deadline:
        name, fn = weighted_pick(persona.rng, menu)
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 — a scenario must not end the run
            persona.recorder.note(
                f"{persona.name}: scenario {name} raised", error=f"{type(exc).__name__}: {exc}"
            )
        try:
            await maybe_chat(persona, llm_budget)
        except Exception as exc:  # noqa: BLE001
            persona.recorder.note(f"{persona.name}: chat raised", error=str(exc)[:200])

        # Human-paced think time; without it this becomes a load test, and the
        # point here is duration rather than throughput.
        delay = persona.rng.uniform(*think_range)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass


async def summary_writer(recorder: Recorder, stop: asyncio.Event, every: float = 60.0) -> None:
    while not stop.is_set():
        recorder.flush_summary()
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
            return
        except asyncio.TimeoutError:
            continue


async def run(args: argparse.Namespace) -> int:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recorder = Recorder(Path(args.out_dir), run_id)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    recorder.note(
        "soak starting",
        base_url=args.base_url,
        duration_hours=args.duration_hours,
        orders_per_hour=args.orders_per_hour,
        llm_calls_per_hour_per_persona=args.llm_per_hour,
    )
    print(f"run_id={run_id}")
    print(f"output={recorder.dir}")

    personas: Dict[str, Persona] = {}
    for name, email in PERSONA_ACCOUNTS.items():
        p = Persona(
            name,
            email,
            args.password,
            args.base_url,
            recorder,
            random.Random(f"{run_id}-{name}"),
        )
        if not await p.sign_in():
            recorder.note(f"{name}: could not sign in — persona disabled", email=email)
            print(f"WARNING: {name} ({email}) could not sign in; continuing without it")
            await p.close()
            continue
        personas[name] = p
        print(f"signed in: {name} <{email}>")

    if not personas:
        recorder.note("no personas could sign in — aborting")
        recorder.close()
        print("ERROR: no personas signed in. Is the backend up and are passwords set?")
        return 1

    order_budget = RateBudget(args.orders_per_hour)
    menus: Dict[str, Sequence[Tuple[int, str, Callable]]] = {}
    if "dispatcher" in personas:
        menus["dispatcher"] = Dispatcher(personas["dispatcher"], order_budget).menu()
    if "driver" in personas:
        menus["driver"] = Driver(personas["driver"]).menu()
    if "admin" in personas:
        menus["admin"] = Admin(personas["admin"]).menu()
    if "staff" in personas:
        menus["staff"] = Staff(personas["staff"]).menu()

    deadline = time.monotonic() + args.duration_hours * 3600.0
    tasks = [asyncio.create_task(summary_writer(recorder, stop))]
    for name, persona in personas.items():
        tasks.append(
            asyncio.create_task(
                persona_loop(
                    persona,
                    menus[name],
                    RateBudget(args.llm_per_hour),
                    deadline,
                    stop,
                    (args.think_min, args.think_max),
                )
            )
        )

    await asyncio.gather(*[t for t in tasks[1:]], return_exceptions=True)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)

    for persona in personas.values():
        await persona.close()

    recorder.close()
    summary = recorder.summary(finished=True)
    print("\n=== soak finished ===")
    print(f"elapsed_seconds   {summary['elapsed_seconds']}")
    print(f"totals            {summary['totals']}")
    print(f"orders_created    {summary['orders_created']}")
    print(f"llm_calls         {summary['llm_calls']}")
    print(f"reauths           {summary['reauths']}")
    print(f"unexpected_denials {summary['unexpected_denials']}")
    print(f"report            {recorder.summary_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--password", required=True, help="Shared persona password")
    ap.add_argument("--duration-hours", type=float, default=12.0)
    ap.add_argument("--out-dir", default="./soak-out")
    ap.add_argument("--run-id", default=None)
    ap.add_argument(
        "--orders-per-hour",
        type=float,
        default=15.0,
        help="Cap on created orders. This is the only volume-producing action.",
    )
    ap.add_argument(
        "--llm-per-hour",
        type=float,
        default=6.0,
        help="Real LLM (POST /api/chat) calls per persona per hour. Billed per call.",
    )
    ap.add_argument("--think-min", type=float, default=4.0)
    ap.add_argument("--think-max", type=float, default=20.0)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
