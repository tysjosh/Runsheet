"""Dispatch validation helpers shared by the route planning agent and the Dispatch Board.

Dispatch-board design K3 (task 6). These were private methods on
``Agents.overlay.route_planning_agent.RoutePlanningAgent``; they are module
functions here so the board's ``DispatchValidationService`` (task 10) and the
agent compute route requirements, route-hour estimates and stop locations the
same way. ``RoutePlanningAgent`` calls these; its behaviour is unchanged, which
the golden tests in ``tests/unit/test_dispatch_validation_helpers.py`` pin.

The helpers are pure functions with no I/O. The second half of the module is
the board's :class:`DispatchValidationService` (task 10): ``build_context``
does every read, in parallel with a 2 s timeout per source and a 30 s TTL
cache, and ``validate_lane`` is a pure function of the context and the lane
(K3.1, P3).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from fuel.services.fuel_product_catalog import UnknownFuelProductError, canonicalize

# Board imports. The engine and models import nothing from this module, so
# these don't cycle; ``RoutePlanningAgent`` imports this module for the
# helpers above and pays only the import cost.
from fuel.services import dispatch_board_engine as engine
from fuel.services import dispatch_board_eta as eta
from fuel.services.dispatch_board_models import (
    AssignOrdersCommand,
    BoardDraft,
    CandidatePreview,
    CandidateResult,
    Check,
    DragItem,
    FixLink,
    Lane,
    Load,
    MoveLoadCommand,
    MoveStopsCommand,
    PairDriverCommand,
    Scope,
    Target,
)

logger = logging.getLogger(__name__)

#: Product categories that require a HAZMAT endorsement per DOT/FMCSA
#: regulations when transported in bulk. All petroleum fuels and LPG are
#: Class 3 (flammable liquids) or Class 2.1 (flammable gas).
HAZMAT_CATEGORIES: Tuple[str, ...] = (
    "diesel",
    "gasoline",
    "propane",
    "kerosene",
    "heating_oil",
    "off_road",
    "ethanol",
)

#: Average speed in mph used to estimate drive hours from route distance. Fuel
#: delivery trucks in urban/suburban areas average roughly 25 mph including
#: stops and traffic.
AVERAGE_SPEED_MPH: float = 25.0

#: Average time per delivery stop in hours (loading, unloading, paperwork,
#: safety checks). Used to estimate total on-duty hours.
HOURS_PER_STOP: float = 0.5

#: Average distance between stops in miles (used when actual route distance is
#: not yet computed). Conservative estimate for fuel delivery routes.
AVG_MILES_BETWEEN_STOPS: float = 15.0


def lookup_product(product_code: str) -> Optional[Any]:
    """Look up a FuelProduct from the catalog by product_code."""
    from fuel.services.fuel_product_catalog import FUEL_PRODUCT_CATALOG

    for product in FUEL_PRODUCT_CATALOG:
        if product.product_code == product_code:
            return product
    return None


def build_route_requirements(
    assignments: List[Dict[str, Any]],
    *,
    hazmat_categories: Sequence[str] = HAZMAT_CATEGORIES,
) -> Dict[str, Any]:
    """Derive route requirements from loading plan assignments.

    Inspects the fuel grades/product codes in the assignments to determine:

    - ``requires_hazmat``: True if any assignment carries a HAZMAT-classified
      product (all bulk petroleum fuels). An unidentifiable grade counts as
      HAZMAT (conservative).
    - ``requires_tanker``: True if any assignment is present (all fuel
      deliveries use cargo tank vehicles).
    - ``min_cdl_class``: "A" for cargo tank vehicles (standard for fuel tanker
      trucks in the US).
    """
    requires_hazmat = False
    requires_tanker = bool(assignments)  # All fuel deliveries use tankers

    for assignment in assignments:
        fuel_grade = assignment.get("fuel_grade", "")
        if not fuel_grade:
            continue
        try:
            product_code = canonicalize(fuel_grade)
            product = lookup_product(product_code)
            if product and product.category in hazmat_categories:
                requires_hazmat = True
                break
        except (UnknownFuelProductError, Exception):
            # If we can't identify the product, assume HAZMAT for safety
            # (conservative approach for unknown fuels).
            requires_hazmat = True
            break

    return {
        "requires_hazmat": requires_hazmat,
        "requires_tanker": requires_tanker,
        "min_cdl_class": "A" if requires_tanker else None,
    }


def estimate_route_hours(
    assignments: List[Dict[str, Any]],
    *,
    average_speed_mph: float = AVERAGE_SPEED_MPH,
    hours_per_stop: float = HOURS_PER_STOP,
    avg_miles_between_stops: float = AVG_MILES_BETWEEN_STOPS,
) -> Tuple[float, float]:
    """Estimate drive hours and total on-duty hours for a route.

    - Drive hours = (num_stops + 1) * avg_miles_between_stops / avg_speed_mph
      (depot → stops → depot)
    - Total hours = drive_hours + num_stops * hours_per_stop

    Returns ``(estimated_drive_hours, estimated_total_hours)``.
    """
    num_stops = len(assignments)
    if num_stops == 0:
        return (0.0, 0.0)

    total_miles = (num_stops + 1) * avg_miles_between_stops
    estimated_drive_hours = total_miles / average_speed_mph
    estimated_total_hours = estimated_drive_hours + (num_stops * hours_per_stop)
    return (estimated_drive_hours, estimated_total_hours)


def resolve_stop_locations(
    *,
    station_ids: List[str],
    station_locations: Dict[str, Dict[str, float]],
    order_ids_by_station: Dict[str, List[str]],
    order_stop_locations: Dict[str, Dict[str, float]],
) -> Dict[str, Dict[str, float]]:
    """Merge order-derived and station-derived stop coordinates.

    Order coordinates win: they come from the order's own ship-to
    (``ship_to_lat``/``ship_to_lon``, or a geocoded ``ship_to_address``) and
    therefore work whether the demand behind the stop is a retail
    ``fuel_stations`` document or a ``customer_tanks`` row. ``fuel_stations``
    remains the fallback so legacy retail tenants — whose orders may carry no
    coordinates at all — keep routing.
    """
    resolved: Dict[str, Dict[str, float]] = {}
    for station_id in station_ids:
        for order_id in order_ids_by_station.get(station_id, []):
            order_location = order_stop_locations.get(order_id)
            if order_location:
                resolved[station_id] = dict(order_location)
                break
        if station_id in resolved:
            continue
        station_location = station_locations.get(station_id)
        if station_location:
            resolved[station_id] = dict(station_location)
    return resolved


# ===========================================================================
# DispatchValidationService (dispatch-board task 10, design K3)
# ===========================================================================

#: Per-source fetch timeout (K3.1).
SOURCE_TIMEOUT_S: float = 2.0
#: In-process cache lifetime for qualification, HOS, certification, compartments and rules (K3.1).
CACHE_TTL_S: float = 30.0
#: ``long_terminal_wait`` threshold (K3.2).
LONG_WAIT_MINUTES: float = 45.0
#: ``contract_lift_limit`` threshold, percent used (K3.2).
CONTRACT_LIMIT_PCT: float = 90.0
#: ``expires_within_7_days`` horizon.
EXPIRY_INFO_DAYS: int = 7
#: Other service days searched for the cross-day guard (K5.1).
OTHER_DAY_HORIZON_DAYS: int = 14

#: Checks whose unavailable source blocks; the rest warn (K3.4, Q5).
BLOCKING_UNAVAILABLE = frozenset({"driver_qualification", "asset_certification", "dyed_diesel"})
#: Which check each source feeds (for ``check_unavailable``).
SOURCE_CHECKS: Dict[str, str] = {
    "orders": "order_state",
    "other_day": "order_state",
    "drivers": "driver_pairing",
    "qualification": "driver_qualification",
    "hos": "hos",
    "certification": "asset_certification",
    "compartments": "compartment_fit",
    "rules": "compartment_compatibility",
    "dyed": "dyed_diesel",
    "terminals": "terminal_supply",
    "terminal_waits": "terminal_supply",
    "contracts": "terminal_supply",
    "executions": "post_publish",
}

OUTCOME_RANK: Dict[str, int] = {"pass": 0, "info": 1, "warn": 2, "block": 3}

_DYED_CODES = frozenset({"OFF_ROAD_DIESEL", "DYED_DIESEL", "DYED_ULSD", "OFF_ROAD_ULSD"})
_TERMINAL_ORDER_STATES = frozenset({"cancelled", "delivered", "failed"})
_TRUCK_COMPARTMENTS_INDEX = "truck_compartments"
_PLAN_EXECUTIONS_INDEX = "mvp_plan_executions"
_TERMINALS_INDEX = "terminals"
_SUPPLIER_CONTRACTS_INDEX = "supplier_contracts"
_FUEL_STATIONS_INDEX = "fuel_stations"
_TRUCK_TELEMETRY_INDEX = "truck_telemetry"
_DRAFTS_INDEX = "dispatch_board_drafts"


class TTLCache:
    """A small in-process TTL cache keyed by tuples that always include the tenant id."""

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_S,
        *,
        clock: Callable[[], float] = _time.monotonic,
        max_entries: int = 10_000,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._max = max_entries
        self._data: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}

    def get(self, key: Tuple[Any, ...]) -> Tuple[bool, Any]:
        entry = self._data.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if expires < self._clock():
            self._data.pop(key, None)
            return False, None
        return True, value

    def set(self, key: Tuple[Any, ...], value: Any) -> None:
        if len(self._data) >= self._max:
            now = self._clock()
            for k in [k for k, (exp, _v) in self._data.items() if exp < now]:
                self._data.pop(k, None)
            if len(self._data) >= self._max:
                self._data.pop(next(iter(self._data)))
        self._data[key] = (self._clock() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class Lazy:
    """A collaborator resolved at call time (it may be built by a later bootstrap module)."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory

    def resolve(self) -> Any:
        try:
            return self._factory()
        except Exception:
            return None


@dataclass
class ValidationContext:
    """Everything the checks and the engine read for one request (K3.1)."""

    tenant_id: str
    service_date: date
    timezone: str
    now: datetime
    today: date
    fresh: bool = False
    orders: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    drivers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    qualification: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    hos_verdicts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    hos_advisories: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    certification: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    compartments: Dict[str, List[Any]] = field(default_factory=dict)
    truck_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    compartment_states: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    compat_rules: Optional[Dict[Any, Any]] = None
    dyed: Dict[Tuple[str, str, str], Dict[str, Any]] = field(default_factory=dict)
    terminals: Optional[Set[str]] = None
    terminal_locations: Dict[str, Dict[str, float]] = field(default_factory=dict)
    terminal_waits: Dict[str, Optional[float]] = field(default_factory=dict)
    contract_usage: Dict[str, float] = field(default_factory=dict)
    order_locations: Dict[str, Optional[Dict[str, float]]] = field(default_factory=dict)
    truck_positions: Dict[str, Dict[str, float]] = field(default_factory=dict)
    executions: Dict[str, int] = field(default_factory=dict)
    other_day: Dict[str, date] = field(default_factory=dict)
    suggestions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    unavailable: Set[str] = field(default_factory=set)
    new_id: Optional[Callable[[], str]] = None

    @property
    def is_today(self) -> bool:
        return self.service_date == self.today

    @property
    def is_future(self) -> bool:
        return self.service_date > self.today

    def degraded_sources(self) -> List[str]:
        return sorted(self.unavailable)


def worst_outcome(checks: Iterable[Check]) -> str:
    """K3.3: block > warn > info > pass."""
    worst = "pass"
    for check in checks:
        if OUTCOME_RANK[check.outcome] > OUTCOME_RANK[worst]:
            worst = check.outcome
    return worst


def warning_id_for(check: str, reason_code: str, scope: Scope, key: Any = None) -> str:
    """Deterministic warning id (K2.3): a persisting warning keeps its acknowledgement."""
    raw = "|".join(
        [check, reason_code, scope.truck_id, scope.load_id or "", scope.order_id or "", str(key if key is not None else "")]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _mk(
    check: str,
    outcome: str,
    reason_code: str,
    message: str,
    source: str,
    scope: Scope,
    *,
    key: Any = None,
    fix_link: Optional[FixLink] = None,
) -> Check:
    return Check(
        check=check,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason_code,
        message=message,
        source=source,
        scope=scope,
        warning_id=warning_id_for(check, reason_code, scope, key) if outcome == "warn" else None,
        fix_link=fix_link,
    )


def _unavailable(check: str, source: str, scope: Scope) -> Check:
    outcome = "block" if check in BLOCKING_UNAVAILABLE else "warn"
    return _mk(check, outcome, "check_unavailable", "This check could not run. Try again shortly.", source, scope)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _canonical(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    try:
        return canonicalize(code)
    except (UnknownFuelProductError, TypeError, ValueError):
        return str(code).strip().upper()


def parse_compartment_source(source: Dict[str, Any], tenant_id: str) -> Tuple[Optional[Any], Dict[str, Any]]:
    """A ``truck_compartments`` hit as a solver ``Compartment`` plus its state.

    Mirrors ``CompartmentLoadingAgent._query_trucks`` so the board and the
    loading agent read compartments the same way: explicit
    ``allowed_product_codes`` are authoritative, US codes found in
    ``allowed_grades`` are kept exact, and the legacy family grade is derived
    when only codes are stored.
    """
    from Agents.support.compartment_models import Compartment
    from Agents.support.compartment_solver import legacy_grade_for_product
    from Agents.support.fuel_distribution_models import FuelGrade
    from fuel.services.fuel_product_catalog import is_known_product

    truck_id = source.get("truck_id") or ""
    explicit: List[str] = [str(c) for c in (source.get("allowed_product_codes") or [])]
    grades: List[Any] = []
    for g in source.get("allowed_grades") or []:
        try:
            grades.append(FuelGrade(g))
            continue
        except ValueError:
            pass
        if is_known_product(g):
            code = canonicalize(g)
            if code not in explicit:
                explicit.append(code)
    if not grades and explicit:
        for code in explicit:
            family = legacy_grade_for_product(code)
            if family and FuelGrade(family) not in grades:
                grades.append(FuelGrade(family))
        if not grades:
            grades = [FuelGrade.AGO]
    state = {
        "state": source.get("state") or "clean",
        "last_loaded_product": source.get("last_loaded_product"),
        "last_loaded_at": source.get("last_loaded_at"),
        "last_cleaned_at": source.get("last_cleaned_at"),
    }
    if not grades or not truck_id:
        return None, state
    try:
        compartment = Compartment(
            compartment_id=source.get("compartment_id", ""),
            truck_id=truck_id,
            capacity_liters=source.get("capacity_liters", 0.0),
            allowed_grades=grades,
            allowed_product_codes=explicit or None,
            position_index=source.get("position_index", 0) or 0,
            tenant_id=tenant_id,
        )
    except Exception:
        return None, state
    return compartment, state


class _StateView:
    """Duck-typed compartment state for ``check_compatibility``."""

    def __init__(self, state: Dict[str, Any]) -> None:
        self.last_loaded_at = _parse_dt(state.get("last_loaded_at"))
        self.last_cleaned_at = _parse_dt(state.get("last_cleaned_at"))
        self.state = state.get("state")
        self.last_loaded_product = state.get("last_loaded_product")


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _hits(resp: Any) -> List[Dict[str, Any]]:
    return [h.get("_source") or {} for h in ((resp or {}).get("hits") or {}).get("hits") or []]


class DispatchValidationService:
    """Wraps the existing validators for the board (K3.1).

    ``build_context`` does all I/O; ``validate_lane`` and
    ``validate_candidates`` only read the context. Collaborators may be
    :class:`Lazy` (resolved per call) because some are built by bootstrap
    modules that run after the board is wired.
    """

    def __init__(
        self,
        *,
        es_service: Any,
        order_repository: Any = None,
        driver_repository: Any = None,
        qualification_service: Any = None,
        hos_advisory_service: Any = None,
        asset_certification_service: Any = None,
        dyed_diesel_enforcer: Any = None,
        terminal_wait_resolver: Any = None,
        contract_lift_service: Any = None,
        tenant_config: Any = None,
        cache: Optional[TTLCache] = None,
        clock: Optional[Callable[[], datetime]] = None,
        source_timeout_s: float = SOURCE_TIMEOUT_S,
    ) -> None:
        self._es = es_service
        self._deps: Dict[str, Any] = {
            "order_repository": order_repository,
            "driver_repository": driver_repository,
            "qualification_service": qualification_service,
            "hos_advisory_service": hos_advisory_service,
            "asset_certification_service": asset_certification_service,
            "dyed_diesel_enforcer": dyed_diesel_enforcer,
            "terminal_wait_resolver": terminal_wait_resolver,
            "contract_lift_service": contract_lift_service,
            "tenant_config": tenant_config,
        }
        self.cache = cache if cache is not None else TTLCache()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timeout = source_timeout_s

    def _dep(self, name: str) -> Any:
        value = self._deps.get(name)
        return value.resolve() if isinstance(value, Lazy) else value

    def now(self) -> datetime:
        return self._clock()

    # -- context ---------------------------------------------------------

    async def build_context(
        self,
        tenant_id: str,
        service_date: date,
        *,
        truck_ids: Iterable[str],
        driver_ids: Iterable[str],
        order_ids: Iterable[str],
        plan_ids: Iterable[str] = (),
        terminal_ids: Iterable[str] = (),
        fresh: bool = False,
        timezone_name: str = "America/Chicago",
        suggestions: Optional[Dict[str, Dict[str, Any]]] = None,
        include_other_day: bool = True,
    ) -> ValidationContext:
        """Fetch every source in parallel, 2 s each; failures mark the source unavailable."""
        now = self.now()
        ctx = ValidationContext(
            tenant_id=tenant_id,
            service_date=service_date,
            timezone=timezone_name,
            now=now,
            today=eta.today_in(timezone_name, now),
            fresh=fresh,
            suggestions=dict(suggestions or {}),
        )
        trucks = sorted({t for t in truck_ids if t})
        drivers = sorted({d for d in driver_ids if d})
        orders = sorted({o for o in order_ids if o})
        plans = sorted({p for p in plan_ids if p})
        terminals = sorted({t for t in terminal_ids if t})

        first = [
            self._run(ctx, "orders", self._fetch_orders(ctx, orders)),
            self._run(ctx, "drivers", self._fetch_drivers(ctx, drivers)),
            self._run(ctx, "compartments", self._fetch_compartments(ctx, trucks)),
            self._run(ctx, "terminals", self._fetch_terminals(ctx)),
            self._run(ctx, "certification", self._fetch_certification(ctx, trucks)),
            self._run(ctx, "rules", self._fetch_rules(ctx)),
            self._run(ctx, "truck_positions", self._fetch_truck_positions(ctx, trucks)),
        ]
        if plans:
            first.append(self._run(ctx, "executions", self._fetch_executions(ctx, plans)))
        if include_other_day and orders:
            first.append(self._run(ctx, "other_day", self._fetch_other_day(ctx, orders)))
        await asyncio.gather(*first)

        second = [
            self._run(ctx, "qualification", self._fetch_qualification(ctx)),
            self._run(ctx, "locations", self._fetch_locations(ctx)),
            self._run(ctx, "dyed", self._fetch_dyed(ctx)),
        ]
        if ctx.is_today:
            second.append(self._run(ctx, "hos", self._fetch_hos(ctx)))
        if terminals:
            second.append(self._run(ctx, "terminal_waits", self._fetch_terminal_waits(ctx, terminals)))
            second.append(self._run(ctx, "contracts", self._fetch_contracts(ctx, terminals)))
        await asyncio.gather(*second)
        return ctx

    async def _run(self, ctx: ValidationContext, source: str, coro: Awaitable[None]) -> None:
        try:
            await asyncio.wait_for(coro, timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if source not in ctx.unavailable:
                ctx.unavailable.add(source)
                logger.warning(
                    "dispatch_board validation source unavailable: source=%s error=%s",
                    source,
                    type(exc).__name__,
                )

    async def _cached(self, ctx: ValidationContext, key: Tuple[Any, ...], fetch: Callable[[], Awaitable[Any]]) -> Any:
        full = (ctx.tenant_id,) + key
        if not ctx.fresh:
            hit, value = self.cache.get(full)
            if hit:
                return value
        value = await fetch()
        self.cache.set(full, value)
        return value

    async def _fetch_orders(self, ctx: ValidationContext, order_ids: List[str]) -> None:
        if not order_ids:
            return
        repo = self._dep("order_repository")
        if repo is None:
            raise RuntimeError("order repository not configured")
        sem = asyncio.Semaphore(25)

        async def one(order_id: str) -> None:
            async with sem:
                doc = await repo.get_current(ctx.tenant_id, order_id)
            if doc and doc.get("tenant_id") == ctx.tenant_id:
                ctx.orders[order_id] = doc

        await asyncio.gather(*(one(o) for o in order_ids))

    async def _fetch_drivers(self, ctx: ValidationContext, driver_ids: List[str]) -> None:
        if not driver_ids:
            return
        repo = self._dep("driver_repository")
        if repo is None:
            raise RuntimeError("driver repository not configured")

        async def one(driver_id: str) -> None:
            driver = await repo.get(ctx.tenant_id, driver_id)
            doc = _dump(driver)
            if doc and doc.get("tenant_id") == ctx.tenant_id:
                ctx.drivers[driver_id] = doc

        await asyncio.gather(*(one(d) for d in driver_ids))

    async def _fetch_compartments(self, ctx: ValidationContext, truck_ids: List[str]) -> None:
        if not truck_ids:
            return
        missing: List[str] = []
        for truck_id in truck_ids:
            hit, value = (False, None) if ctx.fresh else self.cache.get((ctx.tenant_id, "compartments", truck_id))
            if hit:
                self._apply_compartments(ctx, truck_id, value)
            else:
                missing.append(truck_id)
        if not missing:
            return
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": ctx.tenant_id}}, {"terms": {"truck_id": missing}}]}},
            "size": 1000,
        }
        resp = await self._es.search_documents(_TRUCK_COMPARTMENTS_INDEX, query, 1000)
        grouped: Dict[str, List[Dict[str, Any]]] = {t: [] for t in missing}
        for source in _hits(resp):
            if source.get("tenant_id") not in (None, ctx.tenant_id):
                continue
            truck_id = source.get("truck_id")
            if truck_id in grouped:
                grouped[truck_id].append(source)
        for truck_id, sources in grouped.items():
            self.cache.set((ctx.tenant_id, "compartments", truck_id), sources)
            self._apply_compartments(ctx, truck_id, sources)

    def _apply_compartments(self, ctx: ValidationContext, truck_id: str, sources: List[Dict[str, Any]]) -> None:
        comps: List[Any] = []
        states: Dict[str, Dict[str, Any]] = {}
        meta: Dict[str, Any] = {}
        for source in sources:
            compartment, state = parse_compartment_source(source, ctx.tenant_id)
            if compartment is None:
                continue
            comps.append(compartment)
            states[compartment.compartment_id] = state
            meta.setdefault("max_weight_kg", source.get("max_weight_kg"))
            meta.setdefault("tare_weight_kg", source.get("tare_weight_kg") or 0.0)
        if comps:
            comps.sort(key=lambda c: (c.position_index, c.compartment_id))
            ctx.compartments[truck_id] = comps
            ctx.compartment_states[truck_id] = states
            ctx.truck_meta[truck_id] = meta

    async def _fetch_terminals(self, ctx: ValidationContext) -> None:
        async def fetch() -> List[Dict[str, Any]]:
            query = {"query": {"bool": {"filter": [{"term": {"tenant_id": ctx.tenant_id}}]}}, "size": 500}
            return _hits(await self._es.search_documents(_TERMINALS_INDEX, query, 500))

        sources = await self._cached(ctx, ("terminals",), fetch)
        ctx.terminals = set()
        for source in sources:
            terminal_id = source.get("terminal_id")
            if not terminal_id or source.get("tenant_id") not in (None, ctx.tenant_id):
                continue
            ctx.terminals.add(terminal_id)
            lat, lon = source.get("location_lat"), source.get("location_lon")
            loc = source.get("location")
            if (lat is None or lon is None) and isinstance(loc, dict):
                lat, lon = loc.get("lat"), loc.get("lon")
            if lat is not None and lon is not None:
                ctx.terminal_locations[terminal_id] = {"lat": float(lat), "lon": float(lon)}

    async def _fetch_certification(self, ctx: ValidationContext, truck_ids: List[str]) -> None:
        if not truck_ids:
            return
        svc = self._dep("asset_certification_service")
        if svc is None:
            raise RuntimeError("certification service not configured")

        async def one(truck_id: str) -> None:
            async def fetch() -> Any:
                return _dump(await svc.is_dispatch_eligible(ctx.tenant_id, truck_id))

            ctx.certification[truck_id] = await self._cached(ctx, ("certification", truck_id), fetch)

        await asyncio.gather(*(one(t) for t in truck_ids))

    async def _fetch_rules(self, ctx: ValidationContext) -> None:
        from fuel.services.compatibility_matrix import load_tenant_compatibility_rules

        async def fetch() -> Any:
            return await load_tenant_compatibility_rules(ctx.tenant_id, self._dep("tenant_config"))

        ctx.compat_rules = await self._cached(ctx, ("rules",), fetch)

    async def _fetch_truck_positions(self, ctx: ValidationContext, truck_ids: List[str]) -> None:
        async def one(truck_id: str) -> None:
            async def fetch() -> Optional[Dict[str, float]]:
                query = {
                    "query": {"bool": {"filter": [{"term": {"tenant_id": ctx.tenant_id}}, {"term": {"truck_id": truck_id}}]}},
                    "sort": [{"recorded_at": {"order": "desc"}}],
                    "size": 1,
                }
                hits = _hits(await self._es.search_documents(_TRUCK_TELEMETRY_INDEX, query, 1))
                if not hits:
                    return None
                src = hits[0]
                lat, lon = src.get("location_lat"), src.get("location_lon")
                loc = src.get("location")
                if (lat is None or lon is None) and isinstance(loc, dict):
                    lat, lon = loc.get("lat"), loc.get("lon")
                if lat is None or lon is None:
                    return None
                return {"lat": float(lat), "lon": float(lon)}

            position = await self._cached(ctx, ("truck_position", truck_id), fetch)
            if position:
                ctx.truck_positions[truck_id] = position

        await asyncio.gather(*(one(t) for t in truck_ids))

    async def _fetch_executions(self, ctx: ValidationContext, plan_ids: List[str]) -> None:
        """``completed_stops`` of the published loads on the touched lanes (K8.1)."""
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": ctx.tenant_id}}, {"terms": {"plan_id": plan_ids}}]}},
            "_source": ["plan_id", "route_id", "completed_stops", "tenant_id"],
            "size": max(len(plan_ids) * 2, 10),
        }
        for source in _hits(await self._es.search_documents(_PLAN_EXECUTIONS_INDEX, query, len(plan_ids) * 2)):
            plan_id = source.get("plan_id")
            if not plan_id:
                continue
            completed = int(source.get("completed_stops") or 0)
            ctx.executions[plan_id] = max(ctx.executions.get(plan_id, 0), completed)

    async def _fetch_other_day(self, ctx: ValidationContext, order_ids: List[str]) -> None:
        """K5.1: orders already on another service day's draft, today to today + 14."""
        start = ctx.today
        end = ctx.today + timedelta(days=OTHER_DAY_HORIZON_DAYS)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": ctx.tenant_id}},
                        {"range": {"service_date": {"gte": start.isoformat(), "lte": end.isoformat()}}},
                        {"terms": {"order_ids": order_ids}},
                    ],
                    "must_not": [{"term": {"service_date": ctx.service_date.isoformat()}}],
                }
            },
            "_source": ["service_date", "order_ids"],
            "size": OTHER_DAY_HORIZON_DAYS + 1,
        }
        wanted = set(order_ids)
        for source in _hits(await self._es.search_documents(_DRAFTS_INDEX, query, OTHER_DAY_HORIZON_DAYS + 1)):
            try:
                other = date.fromisoformat(str(source.get("service_date"))[:10])
            except ValueError:
                continue
            if other == ctx.service_date:
                continue
            for order_id in source.get("order_ids") or []:
                if order_id in wanted and (order_id not in ctx.other_day or other < ctx.other_day[order_id]):
                    ctx.other_day[order_id] = other

    async def _fetch_qualification(self, ctx: ValidationContext) -> None:
        if not ctx.drivers:
            return
        svc = self._dep("qualification_service")
        if svc is None:
            raise RuntimeError("qualification service not configured")
        requirements = build_route_requirements(
            [{"fuel_grade": o.get("product_code")} for o in ctx.orders.values() if o.get("product_code")]
        )
        req_key = (requirements["requires_hazmat"], requirements["requires_tanker"], requirements["min_cdl_class"])

        async def one(driver_id: str) -> None:
            async def fetch() -> Any:
                return _dump(await svc.is_dispatch_eligible(ctx.tenant_id, driver_id, requirements))

            ctx.qualification[driver_id] = await self._cached(ctx, ("qualification", driver_id, req_key), fetch)

        await asyncio.gather(*(one(d) for d in ctx.drivers))

    async def _fetch_hos(self, ctx: ValidationContext) -> None:
        """Today only (K3.2): the gate verdict and the advisory figures."""
        if not ctx.drivers:
            return
        svc = self._dep("hos_advisory_service")
        if svc is None:
            raise RuntimeError("HOS advisory service not configured")

        async def one(driver_id: str) -> None:
            async def verdict() -> Any:
                return _dump(await svc.gate_verdict(ctx.tenant_id, driver_id))

            async def advisory() -> Any:
                return _dump(await svc.resolve(ctx.tenant_id, driver_id))

            ctx.hos_verdicts[driver_id] = await self._cached(ctx, ("hos_verdict", driver_id), verdict)
            ctx.hos_advisories[driver_id] = await self._cached(ctx, ("hos_advisory", driver_id), advisory)

        await asyncio.gather(*(one(d) for d in ctx.drivers))

    async def _fetch_dyed(self, ctx: ValidationContext) -> None:
        products = {_canonical(o.get("product_code")) for o in ctx.orders.values()} & _DYED_CODES
        if not products or not ctx.compartments:
            return
        enforcer = self._dep("dyed_diesel_enforcer")
        if enforcer is None:
            raise RuntimeError("dyed diesel enforcer not configured")

        async def one(truck_id: str, compartment_id: str, product: str) -> None:
            async def fetch() -> Any:
                return _dump(await enforcer.validate_load_plan(ctx.tenant_id, compartment_id, product))

            ctx.dyed[(truck_id, compartment_id, product)] = await self._cached(
                ctx, ("dyed", truck_id, compartment_id, product), fetch
            )

        await asyncio.gather(
            *(
                one(truck_id, comp.compartment_id, product)
                for truck_id, comps in ctx.compartments.items()
                for comp in comps
                for product in sorted(products)
            )
        )

    async def _fetch_locations(self, ctx: ValidationContext) -> None:
        """Order ship-to coordinates, else ``fuel_stations`` by customer id (K3.6)."""
        need_station: Dict[str, List[str]] = {}
        for order_id, order in ctx.orders.items():
            lat, lon = order.get("ship_to_lat"), order.get("ship_to_lon")
            if lat is not None and lon is not None:
                ctx.order_locations[order_id] = {"lat": float(lat), "lon": float(lon)}
            else:
                ctx.order_locations[order_id] = None
                station = order.get("customer_id")
                if station:
                    need_station.setdefault(str(station), []).append(order_id)
        if not need_station:
            return
        query = {
            "query": {"bool": {"filter": [{"term": {"tenant_id": ctx.tenant_id}}, {"terms": {"station_id": sorted(need_station)}}]}},
            "_source": ["station_id", "latitude", "longitude", "location"],
            "size": 200,
        }
        station_locations: Dict[str, Dict[str, float]] = {}
        for source in _hits(await self._es.search_documents(_FUEL_STATIONS_INDEX, query, 200)):
            sid = source.get("station_id")
            lat, lon = source.get("latitude"), source.get("longitude")
            loc = source.get("location")
            if (not lat and not lon) and isinstance(loc, dict):
                lat, lon = loc.get("lat"), loc.get("lon")
            if sid and lat is not None and lon is not None and (lat or lon):
                station_locations[sid] = {"lat": float(lat), "lon": float(lon)}
        resolved = resolve_stop_locations(
            station_ids=sorted(need_station),
            station_locations=station_locations,
            order_ids_by_station=need_station,
            order_stop_locations={},
        )
        for station_id, order_ids in need_station.items():
            for order_id in order_ids:
                ctx.order_locations[order_id] = resolved.get(station_id)

    async def _fetch_terminal_waits(self, ctx: ValidationContext, terminal_ids: List[str]) -> None:
        resolver = self._dep("terminal_wait_resolver")
        if resolver is None:
            raise RuntimeError("terminal wait resolver not configured")

        resolve = resolver.resolve if hasattr(resolver, "resolve") else resolver

        async def one(terminal_id: str) -> None:
            ctx.terminal_waits[terminal_id] = await resolve(ctx.tenant_id, terminal_id)

        await asyncio.gather(*(one(t) for t in terminal_ids))

    async def _fetch_contracts(self, ctx: ValidationContext, terminal_ids: List[str]) -> None:
        """Highest monthly-lift percent of the active contracts preferring each terminal."""
        svc = self._dep("contract_lift_service")
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": ctx.tenant_id}},
                        {"term": {"status": "active"}},
                        {"terms": {"preferred_terminal_ids": terminal_ids}},
                    ]
                }
            },
            "size": 100,
        }
        contracts = _hits(await self._es.search_documents(_SUPPLIER_CONTRACTS_INDEX, query, 100))
        if contracts and svc is None:
            raise RuntimeError("contract lift service not configured")
        for contract in contracts:
            minimum = contract.get("minimum_lift_gallons_per_month")
            if not contract.get("contract_id") or not minimum:
                continue
            summary = _dump(await svc.get_summary(ctx.tenant_id, contract["contract_id"], minimum))
            percent = (summary or {}).get("percent_of_minimum")
            if percent is None:
                continue
            for terminal_id in contract.get("preferred_terminal_ids") or []:
                if terminal_id in terminal_ids:
                    ctx.contract_usage[terminal_id] = max(ctx.contract_usage.get(terminal_id, 0.0), float(percent))

    # -- checks ----------------------------------------------------------

    async def validate_lane(
        self,
        ctx: ValidationContext,
        lane: Lane,
        *,
        draft: Optional[BoardDraft] = None,
        focus: Optional[Scope] = None,
    ) -> List[Check]:
        """All checks for one proposed lane (K3.2). Reads only ``ctx`` (async for interface stability)."""
        return lane_checks(ctx, lane, draft=draft, focus=focus)

    async def validate_candidates(
        self,
        ctx: ValidationContext,
        draft: BoardDraft,
        item: DragItem,
        truck_ids: Sequence[str],
        position: Optional[Target] = None,
    ) -> Dict[str, CandidateResult]:
        """Apply the item to each candidate lane with the engine and validate it (K3.1)."""
        return candidate_results(ctx, draft, item, truck_ids, position)


# ---------------------------------------------------------------------------
# Pure check functions (P3)
# ---------------------------------------------------------------------------


def lane_checks(
    ctx: ValidationContext,
    lane: Lane,
    *,
    draft: Optional[BoardDraft] = None,
    focus: Optional[Scope] = None,
) -> List[Check]:
    draft = draft or BoardDraft(tenant_id=ctx.tenant_id, service_date=ctx.service_date, timezone=ctx.timezone)
    checks: List[Check] = []
    checks += _post_publish_checks(ctx, lane, draft)
    checks += _order_state_checks(ctx, lane, draft)
    checks += _window_checks(ctx, lane)
    checks += _pairing_checks(ctx, lane, draft)
    checks += _qualification_checks(ctx, lane)
    checks += _hos_checks(ctx, lane)
    checks += _certification_checks(ctx, lane)
    checks += _compartment_checks(ctx, lane)
    checks += _schedule_checks(ctx, lane)
    checks += _terminal_checks(ctx, lane)
    if focus is not None:
        checks = [
            c
            for c in checks
            if (focus.load_id is None or c.scope.load_id in (None, focus.load_id))
            and (focus.order_id is None or c.scope.order_id in (None, focus.order_id))
        ]
    return checks


def _scope(lane: Lane, load: Optional[Load] = None, order_id: Optional[str] = None) -> Scope:
    return Scope(truck_id=lane.truck_id, load_id=load.load_id if load else None, order_id=order_id)


def _post_publish_checks(ctx: ValidationContext, lane: Lane, draft: BoardDraft) -> List[Check]:
    """Pinned stops and started loads (K8.1, Q12, freeze rule 11 (e)).

    * A pinned order (in_transit/delivered/failed) linked to a published load
      must sit on that load. Anywhere else it blocks ``stop_pinned`` with the
      move-back fix; the load it left also blocks.
    * A started load keeps its terminal, allocation and published orders and
      gains no new ones (``load_started``), and the lane keeps its driver.
    """
    out: List[Check] = []
    runs = engine.published_run_ids(draft)
    runs.update({p.run_id: (lane.truck_id, lid) for lid, p in lane.publish.plans.items()})
    source = "BoardEngine"
    for load in lane.loads:
        for stop in load.stops:
            order = ctx.orders.get(stop.order_id)
            if not engine.is_pinned(order):
                continue
            home = runs.get((order or {}).get("assigned_run_id") or "")
            if home is None or home == (lane.truck_id, load.load_id):
                continue
            home_truck, home_load = home
            out.append(
                _mk(
                    "post_publish",
                    "block",
                    "stop_pinned",
                    f"Started on {home_truck}. Move it back.",
                    source,
                    _scope(lane, load, stop.order_id),
                    fix_link=FixLink(kind="move_back", id=stop.order_id, truck_id=home_truck, load_id=home_load),
                )
            )
    for order_id in lane.shelf:
        order = ctx.orders.get(order_id)
        home = runs.get((order or {}).get("assigned_run_id") or "")
        if engine.is_pinned(order) and home is not None:
            out.append(
                _mk(
                    "post_publish",
                    "block",
                    "stop_pinned",
                    f"Started on {home[0]}. Move it back.",
                    source,
                    _scope(lane, None, order_id),
                    fix_link=FixLink(kind="move_back", id=order_id, truck_id=home[0], load_id=home[1]),
                )
            )
    current = {l.load_id: l for l in lane.loads}
    started_any = False
    for load_id in lane.publish.plans:
        if not engine.load_started(lane, load_id, ctx):
            continue
        started_any = True
        published = engine.published_load(lane, load_id)
        now_load = current.get(load_id)
        if now_load is None:
            out.append(
                _mk("post_publish", "block", "load_started", f"Load {load_id} has started and can't move.", source, Scope(truck_id=lane.truck_id, load_id=load_id))
            )
            continue
        if published is None:
            continue
        published_ids = [s.order_id for s in published.stops]
        for stop in now_load.stops:
            if stop.order_id not in published_ids:
                out.append(
                    _mk("post_publish", "block", "load_started", f"Load {load_id} has started. Order {stop.order_id} can't be added.", source, _scope(lane, now_load, stop.order_id))
                )
        now_ids = {s.order_id for s in now_load.stops}
        for order_id in published_ids:
            order = ctx.orders.get(order_id)
            if engine.is_pinned(order) and order_id not in now_ids:
                out.append(
                    _mk(
                        "post_publish",
                        "block",
                        "stop_pinned",
                        f"Order {order_id} has started on {lane.truck_id}. Move it back.",
                        source,
                        _scope(lane, now_load, order_id),
                        fix_link=FixLink(kind="move_back", id=order_id, truck_id=lane.truck_id, load_id=load_id),
                    )
                )
        if now_load.terminal_id != published.terminal_id:
            out.append(_mk("post_publish", "block", "load_started", f"Load {load_id} has started. Its terminal can't change.", source, _scope(lane, now_load)))
        if engine.overrides_signature(now_load) != engine.overrides_signature(published):
            out.append(_mk("post_publish", "block", "load_started", f"Load {load_id} has started. Its compartments can't change.", source, _scope(lane, now_load)))
    published_content = lane.publish.published_content
    if started_any and published_content is not None and lane.driver_id != published_content.driver_id:
        out.append(
            _mk("post_publish", "block", "load_started", f"{lane.truck_id} has a started load. The driver can't change.", source, _scope(lane))
        )
    return out


def _order_state_checks(ctx: ValidationContext, lane: Lane, draft: BoardDraft) -> List[Check]:
    out: List[Check] = []
    runs = engine.published_run_ids(draft)
    runs.update({p.run_id: (lane.truck_id, lid) for lid, p in lane.publish.plans.items()})
    source = "FuelOrderRepository"
    for load in lane.loads:
        for stop in load.stops:
            scope = _scope(lane, load, stop.order_id)
            if "orders" in ctx.unavailable:
                out.append(_unavailable("order_state", source, scope))
                continue
            order = ctx.orders.get(stop.order_id)
            oid = stop.order_id
            fix = FixLink(kind="order", id=oid)
            if order is None:
                out.append(_mk("order_state", "block", "order_not_found", f"Order {oid} no longer exists.", source, scope, fix_link=fix))
                continue
            status = order.get("status")
            run = order.get("assigned_run_id") or ""
            linked_here = bool(run) and run in runs
            if status == "on_hold":
                out.append(_mk("order_state", "block", "on_hold", f"Order {oid} is on hold.", source, scope, fix_link=fix))
            elif status in _TERMINAL_ORDER_STATES and not (linked_here and status in ("delivered", "failed")):
                out.append(_mk("order_state", "block", "order_terminal", f"Order {oid} is {status}.", source, scope, fix_link=fix))
            elif run and not linked_here:
                out.append(_mk("order_state", "block", "order_committed_elsewhere", f"Order {oid} is already planned on another route.", source, scope, fix_link=fix))
            other = ctx.other_day.get(oid)
            if other is not None:
                out.append(
                    _mk("order_state", "block", "order_on_other_day", f"Order {oid} is planned on {other.strftime('%b')} {other.day}. Remove it there first.", "DispatchBoardDrafts", scope, fix_link=fix)
                )
            snap = stop.snapshot
            if (
                _canonical(order.get("product_code")) != _canonical(snap.product_code)
                or (order.get("customer_id") or None) != (snap.customer_id or None)
                or (order.get("customer_tank_id") or None) != (snap.customer_tank_id or None)
            ):
                out.append(
                    _mk("order_state", "block", "order_identity_changed", f"Order {oid} changed since it was placed. Remove it and add it again.", source, scope, fix_link=fix)
                )
            elif _gallons(order.get("gallons_requested")) != _gallons(snap.gallons_requested) or bool(order.get("fill_to_full")) != bool(snap.fill_to_full):
                out.append(
                    _mk("order_state", "info", "order_quantity_changed", f"Order {oid} quantity changed. Compartments are recomputed on publish.", source, scope)
                )
    if "other_day" in ctx.unavailable and any(l.stops for l in lane.loads):
        out.append(_unavailable("order_state", "DispatchBoardDrafts", _scope(lane)))
    return out


def _gallons(value: Any) -> Optional[float]:
    try:
        return round(float(value), 3) if value is not None else None
    except (TypeError, ValueError):
        return None


def _window_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    from fuel.order_state_machine import assert_window_present_for_transition

    out: List[Check] = []
    for load in lane.loads:
        for stop in load.stops:
            scope = _scope(lane, load, stop.order_id)
            order = ctx.orders.get(stop.order_id) or {
                "order_id": stop.order_id,
                "delivery_window_start": stop.snapshot.window.start,
                "delivery_window_end": stop.snapshot.window.end,
            }
            try:
                assert_window_present_for_transition(order, "scheduled")
            except Exception:
                out.append(_mk("delivery_window", "block", "missing_delivery_window", f"Order {stop.order_id} has no delivery window.", "OrderStateMachine", scope, fix_link=FixLink(kind="order", id=stop.order_id)))
                continue
            start = _parse_dt(order.get("delivery_window_start"))
            end = _parse_dt(order.get("delivery_window_end"))
            late = eta.lateness_minutes(stop.eta, end)
            if late > 0:
                bucket = int((late + 14) // 15) * 15
                out.append(_mk("delivery_window", "warn", "eta_after_window", f"Order {stop.order_id} arrives about {int(round(late))} min after its window.", "DispatchBoardEta", scope, key=bucket))
            elif eta.early_minutes(stop.eta, start) > 0:
                out.append(_mk("delivery_window", "info", "early_arrival", f"Order {stop.order_id} arrives before its window opens.", "DispatchBoardEta", scope))
    return out


def _pairing_checks(ctx: ValidationContext, lane: Lane, draft: BoardDraft) -> List[Check]:
    out: List[Check] = []
    has_loads = any(l.stops for l in lane.loads)
    if not lane.driver_id:
        if has_loads:
            out.append(_mk("driver_pairing", "warn", "no_driver", f"{lane.truck_id} has loads and no driver.", "BoardEngine", _scope(lane)))
        return out
    if "drivers" in ctx.unavailable:
        return [_unavailable("driver_pairing", "DriverRepository", _scope(lane))]
    if lane.driver_id not in ctx.drivers:
        out.append(_mk("driver_pairing", "block", "driver_not_in_tenant", "The paired driver was not found.", "DriverRepository", _scope(lane), fix_link=FixLink(kind="driver", id=lane.driver_id)))
        return out
    probe = draft.model_copy(deep=False)
    probe.lanes = dict(draft.lanes)
    probe.lanes[lane.truck_id] = lane
    others = engine.double_booked(probe, lane.truck_id)
    if others:
        out.append(
            _mk("driver_pairing", "block", "driver_double_booked", f"This driver is also on {', '.join(sorted(others))} at the same time.", "BoardEngine", _scope(lane), fix_link=FixLink(kind="driver", id=lane.driver_id))
        )
    return out


def _qualification_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    if not lane.driver_id or lane.driver_id not in ctx.drivers:
        return []
    scope = _scope(lane)
    source = "DriverQualificationService"
    if "qualification" in ctx.unavailable or lane.driver_id not in ctx.qualification:
        return [_unavailable("driver_qualification", source, scope)]
    result = ctx.qualification[lane.driver_id] or {}
    out: List[Check] = []
    fix = FixLink(kind="driver", id=lane.driver_id)
    if not result.get("eligible", False):
        for reason in result.get("reasons") or ["driver_ineligible"]:
            code = str(reason).split(":")[0].strip().lower().replace(" ", "_")[:64] or "driver_ineligible"
            out.append(_mk("driver_qualification", "block", code, f"The driver can't be dispatched ({code}).", source, scope, fix_link=fix))
    driver = ctx.drivers.get(lane.driver_id) or {}
    expiry = _parse_dt(driver.get("medical_card_expiry"))
    if expiry is not None and ctx.now <= expiry <= ctx.now + timedelta(days=EXPIRY_INFO_DAYS):
        out.append(_mk("driver_qualification", "info", "expires_within_7_days", "The driver's medical card expires within 7 days.", source, scope, fix_link=fix))
    return out


def _lane_hours(ctx: ValidationContext, lane: Lane) -> Tuple[float, float]:
    drive = 0.0
    on_duty = 0.0
    previous: Optional[Load] = None
    for load in lane.loads:
        times = engine.load_times(ctx, lane.truck_id, load, previous)
        if times.available:
            drive += times.drive_minutes / 60.0
        else:
            drive += estimate_route_hours([{} for _ in load.stops])[0]
        on_duty += times.on_duty_minutes / 60.0
        previous = load
    return drive, on_duty


def _hos_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    if not lane.driver_id or lane.driver_id not in ctx.drivers:
        return []
    scope = _scope(lane)
    source = "HOSAdvisoryService"
    if ctx.is_future:
        return [_mk("hos", "info", "hos_not_projected", "Hours of service are checked on the day.", source, scope)]
    if not ctx.is_today:
        return []
    if "hos" in ctx.unavailable or lane.driver_id not in ctx.hos_verdicts:
        return [_unavailable("hos", source, scope)]
    verdict = ctx.hos_verdicts.get(lane.driver_id) or {}
    advisory = ctx.hos_advisories.get(lane.driver_id) or {}
    fix = FixLink(kind="hos_override", id=lane.driver_id)
    if verdict.get("outcome") == "blocked":
        return [_mk("hos", "block", "hos_gate_blocked", "The driver is out of hours. An HOS override is the only way to lift this.", source, scope, fix_link=fix)]
    out: List[Check] = []
    drive_h, duty_h = _lane_hours(ctx, lane)
    drive_left = _figure(advisory.get("remaining_drive_time"))
    duty_left = _figure(advisory.get("remaining_on_duty_window"))
    over = (drive_left is not None and drive_h > drive_left) or (duty_left is not None and duty_h > duty_left)
    if over or (verdict.get("outcome") == "skipped" and verdict.get("gating_enabled")):
        out.append(_mk("hos", "warn", "hos_projected_over", "The planned work may exceed the driver's hours today.", source, scope, key=round(drive_h, 0)))
    elif drive_left is not None:
        out.append(_mk("hos", "info", "hos_remaining", f"{drive_left:.1f} h of driving left today.", source, scope))
    return out


def _figure(value: Any) -> Optional[float]:
    if isinstance(value, dict) and value.get("availability") == "available" and value.get("value") is not None:
        try:
            return float(value["value"])
        except (TypeError, ValueError):
            return None
    return None


def _certification_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    if not any(l.stops for l in lane.loads):
        return []
    scope = _scope(lane)
    source = "AssetCertificationService"
    if "certification" in ctx.unavailable or lane.truck_id not in ctx.certification:
        return [_unavailable("asset_certification", source, scope)]
    result = ctx.certification[lane.truck_id] or {}
    if result.get("eligible", False):
        return []
    fix = FixLink(kind="asset", id=lane.truck_id)
    return [
        _mk("asset_certification", "block", str(r).split(":")[0].strip().lower().replace(" ", "_")[:64] or "asset_ineligible", f"{lane.truck_id} can't be dispatched (certification).", source, scope, fix_link=fix)
        for r in (result.get("reasons") or ["asset_ineligible"])
    ]


def _compartment_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    from Agents.support.compartment_solver import check_feasibility, compartment_accepts, segregation_key
    from fuel.services.compatibility_matrix import check_compatibility

    out: List[Check] = []
    comps = ctx.compartments.get(lane.truck_id, [])
    by_id = {c.compartment_id: c for c in comps}
    states = ctx.compartment_states.get(lane.truck_id, {})
    meta = ctx.truck_meta.get(lane.truck_id, {})
    last_product: Dict[str, Optional[str]] = {cid: s.get("last_loaded_product") for cid, s in states.items()}
    for load in lane.loads:
        if not load.stops:
            continue
        scope = _scope(lane, load)
        if "compartments" in ctx.unavailable:
            out.append(_unavailable("compartment_fit", "TruckCompartments", scope))
            continue
        requests = engine.delivery_requests(load, ctx)
        if not comps:
            out.append(_mk("compartment_fit", "block", "no_compatible_compartments", f"{lane.truck_id} has no compartments set up.", "CompartmentSolver", scope, fix_link=FixLink(kind="compartment", id=lane.truck_id)))
            continue
        # Dispatcher overrides must follow the same rules.
        override_bad = False
        used: Dict[str, str] = {}
        for order_id, shares in load.allocation_overrides.items():
            product = _canonical((ctx.orders.get(order_id) or {}).get("product_code"))
            for share in shares:
                comp = by_id.get(share.compartment_id)
                key = segregation_key(product_code=product)
                if comp is None or not compartment_accepts(comp, product) or share.liters > comp.capacity_liters or used.get(share.compartment_id, key) != key:
                    override_bad = True
                used[share.compartment_id] = key
        if override_bad:
            out.append(_mk("compartment_fit", "block", "override_invalid", "The compartment split breaks a loading rule.", "CompartmentSolver", scope, fix_link=FixLink(kind="compartment", id=lane.truck_id)))
        if requests:
            result = check_feasibility(
                comps,
                requests,
                max_weight_kg=meta.get("max_weight_kg"),
                tare_weight_kg=float(meta.get("tare_weight_kg") or 0.0),
            )
            for v in result.violations:
                outcome = "warn" if v.violation_type == "below_min_drop" else "block"
                message = {
                    "no_compatible_compartments": f"No compartment on {lane.truck_id} takes {v.fuel_grade}.",
                    "capacity_shortfall": f"Not enough room for {v.fuel_grade}.",
                    "total_overage": "The load is larger than the truck.",
                    "weight_exceeded": "The load is over the truck's weight limit.",
                    "below_min_drop": "A drop is below the minimum.",
                }.get(v.violation_type, "The load doesn't fit.")
                out.append(_mk("compartment_fit", outcome, v.violation_type, message, "CompartmentSolver", scope, key=v.fuel_grade))
            if result.feasible:
                out.append(_mk("compartment_fit", "info", "fill_percent", f"{result.max_utilization_pct:.0f}% full.", "CompartmentSolver", scope))
        # Compatibility with what each compartment last carried.
        rules_down = "rules" in ctx.unavailable
        dyed_flagged = False
        for alloc in load.allocations:
            product = _canonical(alloc.product_code)
            if not product:
                continue
            previous = last_product.get(alloc.compartment_id)
            if rules_down:
                out.append(_unavailable("compartment_compatibility", "CompatibilityMatrix", scope))
                rules_down = False
            elif "rules" not in ctx.unavailable:
                try:
                    decision = check_compatibility(previous, product, _StateView(states.get(alloc.compartment_id, {})), ctx.compat_rules)
                except Exception:
                    decision = {"decision": "allowed"}
                verdict = decision.get("decision")
                if verdict == "blocked":
                    out.append(_mk("compartment_compatibility", "block", "compatibility_blocked", f"Compartment {alloc.compartment_id} can't carry {product} after {previous}.", "CompatibilityMatrix", scope, fix_link=FixLink(kind="compartment", id=alloc.compartment_id)))
                elif verdict == "requires_cleaning":
                    out.append(_mk("compartment_compatibility", "warn", "requires_cleaning", f"Compartment {alloc.compartment_id} needs cleaning first.", "CompatibilityMatrix", scope, key=alloc.compartment_id))
            if product in _DYED_CODES:
                if "dyed" in ctx.unavailable or (lane.truck_id, alloc.compartment_id, product) not in ctx.dyed:
                    if not dyed_flagged:
                        out.append(_unavailable("dyed_diesel", "DyedDieselEnforcer", scope))
                        dyed_flagged = True
                else:
                    verdict = ctx.dyed[(lane.truck_id, alloc.compartment_id, product)] or {}
                    if not verdict.get("valid", False):
                        code = str(verdict.get("error_code") or "dyed_violation").replace(".", "_")[:64]
                        out.append(_mk("dyed_diesel", "block", code, f"Compartment {alloc.compartment_id} can't carry dyed diesel.", "DyedDieselEnforcer", scope, fix_link=FixLink(kind="compartment", id=alloc.compartment_id)))
        for alloc in load.allocations:
            if alloc.product_code:
                last_product[alloc.compartment_id] = _canonical(alloc.product_code)
    return out


def _schedule_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    out: List[Check] = []
    previous: Optional[Load] = None
    for load in lane.loads:
        if not load.stops:
            previous = load
            continue
        scope = _scope(lane, load)
        times = engine.load_times(ctx, lane.truck_id, load, previous)
        if not times.available:
            out.append(_mk("schedule", "warn", "eta_unavailable", "ETAs are unavailable for this load.", "DispatchBoardEta", scope))
        if previous is not None and previous.planned_end is not None and load.planned_start is not None and previous.planned_end > load.planned_start:
            out.append(_mk("schedule", "warn", "load_overlap", "This load starts before the previous one ends.", "DispatchBoardEta", scope))
        if load.shift_id != "all" and load.planned_start is not None and load.planned_end is not None:
            window = eta.shift_window(ctx.service_date, load.shift_id, ctx.timezone)
            if eta.outside_window((load.planned_start, load.planned_end), window):
                out.append(_mk("schedule", "warn", "outside_shift", "This load runs past its shift.", "DispatchBoardEta", scope))
        previous = load
    return out


def _terminal_checks(ctx: ValidationContext, lane: Lane) -> List[Check]:
    out: List[Check] = []
    for load in lane.loads:
        if not load.stops:
            continue
        scope = _scope(lane, load)
        if not load.terminal_id:
            out.append(_mk("terminal_supply", "info", "terminal_unset", "No terminal is set for this load.", "TerminalWaitResolver", scope, fix_link=FixLink(kind="terminal", id=load.load_id)))
            continue
        if "terminal_waits" in ctx.unavailable or "contracts" in ctx.unavailable:
            out.append(_unavailable("terminal_supply", "TerminalWaitResolver", scope))
        wait = ctx.terminal_waits.get(load.terminal_id)
        if wait is not None and wait > LONG_WAIT_MINUTES:
            out.append(_mk("terminal_supply", "warn", "long_terminal_wait", f"Terminal {load.terminal_id} wait is about {int(round(wait))} min.", "TerminalWaitResolver", scope, key=int(wait // 15)))
        used = ctx.contract_usage.get(load.terminal_id)
        if used is not None and used >= CONTRACT_LIMIT_PCT:
            out.append(_mk("terminal_supply", "warn", "contract_lift_limit", f"Contract lift at terminal {load.terminal_id} is {int(used)}% used.", "ContractLiftService", scope, key=int(used // 5)))
    return out


# ---------------------------------------------------------------------------
# Candidates (K3.1, K3.5)
# ---------------------------------------------------------------------------

_PROBE_ID = "00000000-0000-4000-8000-000000000000"


def _probe_command(draft: BoardDraft, item: DragItem, truck_id: str, position: Optional[Target]) -> Optional[Any]:
    versions = {t: lane.version for t, lane in draft.lanes.items()}
    common = {"client_command_id": _PROBE_ID, "expected_lane_versions": versions, "input_modality": "system"}
    target = position or Target()
    if item.kind == "order":
        on_board = all(o in draft.order_index for o in item.ids)
        cls = MoveStopsCommand if on_board else AssignOrdersCommand
        return cls(type="move_stops" if on_board else "assign_orders", order_ids=item.ids, truck_id=truck_id, target=target, **common)
    if item.kind == "stop":
        return MoveStopsCommand(type="move_stops", order_ids=item.ids, truck_id=truck_id, target=target, **common)
    if item.kind == "load":
        lane = draft.lanes.get(truck_id)
        index = len(lane.loads) if lane else 0
        return MoveLoadCommand(type="move_load", load_id=item.ids[0], truck_id=truck_id, index=index, **common)
    if item.kind == "driver":
        return PairDriverCommand(type="pair_driver", truck_id=truck_id, driver_id=item.ids[0], **common)
    return None


def candidate_results(
    ctx: ValidationContext,
    draft: BoardDraft,
    item: DragItem,
    truck_ids: Sequence[str],
    position: Optional[Target] = None,
) -> Dict[str, CandidateResult]:
    results: Dict[str, CandidateResult] = {}
    for truck_id in truck_ids:
        lane = draft.lanes.get(truck_id)
        if lane is None:
            results[truck_id] = CandidateResult(outcome="block", reason="unknown_truck")
            continue
        command = _probe_command(draft, item, truck_id, position)
        if command is None:
            results[truck_id] = CandidateResult(outcome="block", reason="unsupported_item")
            continue
        try:
            applied = engine.apply(draft, command, ctx)
        except engine.EngineError as exc:
            results[truck_id] = CandidateResult(outcome="block", reason=exc.reason)
            continue
        checks: List[Check] = []
        for touched in applied.touched:
            new_lane = applied.draft.lanes.get(touched)
            if new_lane is not None:
                checks.extend(lane_checks(ctx, new_lane, draft=applied.draft))
        flagged = sorted(
            (c for c in checks if c.outcome in ("block", "warn")),
            key=lambda c: -OUTCOME_RANK[c.outcome],
        )
        preview = CandidatePreview()
        new_lane = applied.draft.lanes[truck_id]
        if applied.placed is not None:
            _t, load_id, index = applied.placed
            preview.load_id = load_id
            preview.insertion_index = index
            load = next((l for l in new_lane.loads if l.load_id == load_id), None)
            if load is not None:
                caps = {c.compartment_id: c.capacity_liters for c in ctx.compartments.get(truck_id, [])}
                fill: Dict[str, float] = {}
                for alloc in load.allocations:
                    fill[alloc.compartment_id] = fill.get(alloc.compartment_id, 0.0) + alloc.liters
                preview.fill_by_compartment = {
                    cid: round(100.0 * liters / caps[cid], 1) for cid, liters in fill.items() if caps.get(cid)
                }
        before = engine.lane_window(lane)
        after = engine.lane_window(new_lane)
        if before is not None and after is not None:
            preview.eta_delta_minutes = round(eta.minutes_between(before[1], after[1]), 1)
        results[truck_id] = CandidateResult(outcome=worst_outcome(checks), worst_checks=flagged[:3], preview=preview)
    return results
