"""Shared fakes and builders for the Dispatch Board backend tests (plan tasks 7-14).

``BoardStore`` is the loading-plan ``InMemoryDocumentStore`` (real
``atomic_update`` semantics, ``document_matcher`` queries) plus
``create_document`` (insert-if-absent, like the Postgres store). The other
fakes stand in for the validators the board wraps; each records its calls and
can be told to fail or to hang.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fuel.services.dispatch_board_models import BoardDraft, CommandBody, draft_doc_id
from fuel.services.dispatch_board_service import DispatchBoardService
from fuel.services.dispatch_board_telemetry import BoardTelemetry
from fuel.services.dispatch_validation import DispatchValidationService, TTLCache
from tests.unit._loading_plan_fakes import InMemoryDocumentStore

T = "tenant-1"
OTHER = "tenant-2"
TZ = "America/Chicago"
#: 10:00 in Chicago on the service day.
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 8)
TOMORROW = date(2026, 10, 9)
DRAFTS = "dispatch_board_drafts"
COMMANDS = "dispatch_board_commands"
ORDERS = "fuel_orders_current"
COMPARTMENTS = "truck_compartments"
EXECUTIONS = "mvp_plan_executions"
PRIORITIES = "mvp_delivery_priorities"


class BoardStore(InMemoryDocumentStore):
    """``InMemoryDocumentStore`` with insert-if-absent ``create_document``."""

    async def create_document(self, index: str, doc_id: str, document: Dict[str, Any]) -> bool:
        await self._before("create_document", index, doc_id)
        if str(doc_id) in self.docs[index]:
            self._log("create_document", index, doc_id, False)
            return False
        self.docs[index][str(doc_id)] = copy.deepcopy(document)
        self._log("create_document", index, doc_id, True)
        return True

    def draft(self, tenant_id: str = T, service_date: date = TODAY) -> Optional[BoardDraft]:
        doc = self.doc(DRAFTS, draft_doc_id(tenant_id, service_date))
        return BoardDraft.model_validate(doc) if doc else None

    def command_docs(self) -> List[Dict[str, Any]]:
        return [copy.deepcopy(d) for d in self.docs[COMMANDS].values()]


def order(order_id: str, *, tenant_id: str = T, day: date = TODAY, **overrides: Any) -> Dict[str, Any]:
    doc = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "status": "confirmed",
        "customer_id": f"cust-{order_id}",
        "customer_name": "Acme Fuels Customer",
        "ship_to_address": "1 Private Road",
        "customer_tank_id": f"tank-{order_id}",
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "fill_to_full": False,
        "call_type": "one_off",
        "delivery_window_start": f"{day.isoformat()}T13:00:00+00:00",
        "delivery_window_end": f"{day.isoformat()}T23:00:00+00:00",
        "ship_to_lat": 41.88,
        "ship_to_lon": -87.63,
        "assigned_run_id": None,
        "assigned_asset_id": None,
        "assigned_driver_id": None,
    }
    doc.update(overrides)
    return doc


def compartment(truck_id: str, compartment_id: str, *, tenant_id: str = T, capacity: float = 10000.0, **extra: Any) -> Dict[str, Any]:
    doc = {
        "tenant_id": tenant_id,
        "truck_id": truck_id,
        "compartment_id": compartment_id,
        "capacity_liters": capacity,
        "allowed_grades": ["AGO"],
        "position_index": int(compartment_id[-1]) if compartment_id[-1].isdigit() else 0,
        "state": "clean",
    }
    doc.update(extra)
    return doc


def driver(driver_id: str, *, tenant_id: str = T, status: str = "active", **extra: Any) -> Dict[str, Any]:
    doc = {
        "driver_id": driver_id,
        "tenant_id": tenant_id,
        "driver_name": f"Driver {driver_id}",
        "status": status,
        "assigned_truck_id": None,
        "cdl_class": "A",
        "hazmat_endorsement": True,
    }
    doc.update(extra)
    return doc


class FakeOrderRepository:
    def __init__(self, store: BoardStore) -> None:
        self.store = store
        self.calls: List[str] = []

    async def get_current(self, tenant_id: str, order_id: str) -> Optional[Dict[str, Any]]:
        self.calls.append(order_id)
        doc = self.store.doc(ORDERS, order_id)
        if not doc or doc.get("tenant_id") != tenant_id:
            return None
        return doc


class FakeDriverRepository:
    def __init__(self, drivers: Optional[List[Dict[str, Any]]] = None) -> None:
        self.drivers: Dict[str, Dict[str, Any]] = {d["driver_id"]: d for d in drivers or []}
        self.search_calls: List[Dict[str, Any]] = []
        self.fail_search = False

    async def get(self, tenant_id: str, driver_id: str) -> Optional[Dict[str, Any]]:
        d = self.drivers.get(driver_id)
        return copy.deepcopy(d) if d and d["tenant_id"] == tenant_id else None

    async def search(self, tenant_id: str, *, status=None, availability=None, assigned_truck_id=None, page=1, size=20, sort=None):
        self.search_calls.append({"tenant_id": tenant_id, "status": status, "assigned_truck_id": assigned_truck_id})
        if self.fail_search:
            raise RuntimeError("driver store down")
        out = [
            copy.deepcopy(d)
            for d in self.drivers.values()
            if d["tenant_id"] == tenant_id
            and (status is None or d["status"] == status)
            and (assigned_truck_id is None or d.get("assigned_truck_id") == assigned_truck_id)
        ]
        return {"drivers": out[:size], "total": len(out)}


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[Any] = []
        self.fail: Optional[BaseException] = None
        self.hang = False

    async def _gate(self, *args: Any) -> None:
        self.calls.append(args)
        if self.hang:
            await asyncio.sleep(10)
        if self.fail is not None:
            raise self.fail


class FakeQualification(_Recorder):
    def __init__(self, ineligible: Optional[Dict[str, List[str]]] = None) -> None:
        super().__init__()
        self.ineligible = ineligible or {}

    async def is_dispatch_eligible(self, tenant_id, driver_id, route_requirements=None):
        await self._gate(tenant_id, driver_id, route_requirements)
        reasons = self.ineligible.get(driver_id, [])
        return {"driver_id": driver_id, "eligible": not reasons, "reasons": reasons}


class FakeCertification(_Recorder):
    def __init__(self, ineligible: Optional[Dict[str, List[str]]] = None) -> None:
        super().__init__()
        self.ineligible = ineligible or {}

    async def is_dispatch_eligible(self, tenant_id, asset_id):
        await self._gate(tenant_id, asset_id)
        reasons = self.ineligible.get(asset_id, [])
        return {"asset_id": asset_id, "eligible": not reasons, "reasons": reasons}


class FakeHOS(_Recorder):
    def __init__(self, blocked: Optional[set] = None, drive_left: Optional[float] = 10.0) -> None:
        super().__init__()
        self.blocked = blocked or set()
        self.drive_left = drive_left

    async def gate_verdict(self, tenant_id, driver_id):
        await self._gate("gate", tenant_id, driver_id)
        outcome = "blocked" if driver_id in self.blocked else "passed"
        return {"tenant_id": tenant_id, "driver_id": driver_id, "outcome": outcome, "blocked": outcome == "blocked", "gating_enabled": True}

    async def resolve(self, tenant_id, driver_id):
        await self._gate("resolve", tenant_id, driver_id)
        fig = {"availability": "available", "value": self.drive_left, "unit": "hours"} if self.drive_left is not None else {"availability": "unavailable"}
        return {
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "freshness_state": "fresh",
            "remaining_drive_time": fig,
            "remaining_on_duty_window": {"availability": "unavailable"},
            "cycle_hours": {"availability": "unavailable"},
        }


class FakeDyed(_Recorder):
    def __init__(self, clear_only: Optional[set] = None) -> None:
        super().__init__()
        self.clear_only = clear_only or set()

    async def validate_load_plan(self, tenant_id, compartment_id, product_code):
        await self._gate(tenant_id, compartment_id, product_code)
        if compartment_id in self.clear_only:
            return {"valid": False, "error_code": "dyed.compartment_incompatible", "message": "x"}
        return {"valid": True}


class FakeFlags:
    def __init__(self, states: Optional[Dict[str, str]] = None, *, error: bool = False) -> None:
        self.states = dict(states or {})
        self.error = error

    async def get_overlay_state(self, flag_key, tenant_id):
        if self.error:
            return "disabled"
        return self.states.get(tenant_id, "disabled")

    async def get_overlay_state_strict(self, flag_key, tenant_id):
        if self.error:
            raise ConnectionError("redis down")
        return self.states.get(tenant_id)

    async def set_overlay_state(self, flag_key, tenant_id, state, user_id):
        previous = self.states.get(tenant_id, "disabled")
        self.states[tenant_id] = state
        return previous


class SpyTelemetry:
    def __init__(self) -> None:
        self.metrics: List[tuple] = []
        self.audits: List[tuple] = []

    def record_metric(self, name, value, tags=None):
        self.metrics.append((name, value, dict(tags or {})))

    def log_audit_event(self, *args):
        self.audits.append(args)


class Harness:
    """A board service over the fake store and fake validators."""

    def __init__(self, *, now: datetime = NOW, drivers: Optional[List[Dict[str, Any]]] = None) -> None:
        self.store = BoardStore()
        self.now = now
        self.orders = FakeOrderRepository(self.store)
        self.drivers = FakeDriverRepository(drivers if drivers is not None else [driver("d1"), driver("d2"), driver("d3")])
        self.qualification = FakeQualification()
        self.certification = FakeCertification()
        self.hos = FakeHOS()
        self.dyed = FakeDyed()
        self.telemetry = SpyTelemetry()
        self.names: Dict[tuple, str] = {(T, "user-1"): "ana@example.com", (T, "user-2"): "ben@example.com"}
        self.name_calls: List[tuple] = []
        counter = itertools.count(1)
        self.validation = DispatchValidationService(
            es_service=self.store,
            order_repository=self.orders,
            driver_repository=self.drivers,
            qualification_service=self.qualification,
            hos_advisory_service=self.hos,
            asset_certification_service=self.certification,
            dyed_diesel_enforcer=self.dyed,
            cache=TTLCache(),
            clock=lambda: self.now,
        )

        async def lookup(tenant_id: str, user_id: str) -> Optional[str]:
            self.name_calls.append((tenant_id, user_id))
            return self.names.get((tenant_id, user_id))

        self.service = DispatchBoardService(
            es_service=self.store,
            validation=self.validation,
            driver_repository=self.drivers,
            telemetry=BoardTelemetry(telemetry=self.telemetry),
            name_lookup=lookup,
            clock=lambda: self.now,
            log_read_delay_s=0,
            new_id=lambda: f"L{next(counter)}",
        )
        for truck in ("T1", "T2", "T3"):
            self.store.seed(COMPARTMENTS, f"{truck}_C1", compartment(truck, "C1"))
            self.store.seed(COMPARTMENTS, f"{truck}_C2", compartment(truck, "C2"))

    def seed_order(self, order_id: str, **kw: Any) -> Dict[str, Any]:
        doc = order(order_id, **kw)
        self.store.seed(ORDERS, order_id, doc)
        return doc

    def draft(self, day: date = TODAY) -> BoardDraft:
        return self.store.draft(T, day) or BoardDraft(tenant_id=T, service_date=day, timezone=TZ)

    def versions(self, *trucks: str, day: date = TODAY) -> Dict[str, int]:
        d = self.draft(day)
        return {t: (d.lanes[t].version if t in d.lanes else 0) for t in trucks}

    def command(self, type_: str, *, versions: Optional[Dict[str, int]] = None, lanes: tuple = (), cid: Optional[str] = None, day: date = TODAY, **fields: Any):
        body = {
            "type": type_,
            "client_command_id": cid or str(uuid.uuid4()),
            "expected_lane_versions": versions if versions is not None else self.versions(*lanes, day=day),
            **fields,
        }
        return CommandBody.model_validate(body).root

    async def run(self, type_: str, *, user: str = "user-1", day: date = TODAY, **kw: Any) -> Dict[str, Any]:
        cmd = self.command(type_, day=day, **kw)
        return await self.service.handle_command(tenant_id=T, user_id=user, service_date=day, command=cmd, tz=TZ)

    async def send(self, cmd: Any, *, user: str = "user-1", day: date = TODAY) -> Dict[str, Any]:
        return await self.service.handle_command(tenant_id=T, user_id=user, service_date=day, command=cmd, tz=TZ)

    async def lane_with(self, truck: str, *order_ids: str, driver_id: Optional[str] = None, day: date = TODAY) -> None:
        """Add lane ``truck``, pair ``driver_id`` and assign ``order_ids`` as one new load."""
        await self.run("add_lane", truck_id=truck, lanes=(truck,), day=day)
        if driver_id:
            await self.run("pair_driver", truck_id=truck, driver_id=driver_id, lanes=(truck,), day=day)
        if order_ids:
            await self.run("assign_orders", order_ids=list(order_ids), truck_id=truck, target={"load_id": "new"}, lanes=(truck,), day=day)
