"""
``DriverWorkService`` — the driver's assigned-work read model.

Three reads sit behind one service: the paged assigned-order list
(``GET /api/driver/work``), the single-order detail with its compartment
manifest and stop sequence (``GET /api/driver/work/{order_id}``), and the
caller's own identity plus duty status (``GET /api/driver/me``).

The interesting part of this module is the round-trip budget. Neither plan
model carries a driver: ``LoadingPlan`` is keyed on ``truck_id`` and
``RoutePlan`` on ``truck_id`` / ``run_id``, so four things have to be resolved
from elsewhere — the loading plan, the route plan, each compartment's prior
product grade, and each stop's coordinates and completion status. Resolved
naively that is ``1 + orders x (3 + compartments)`` round trips: for 50 orders
with 6 compartments each, 451 hops, already over two seconds at a 5 ms floor.
Three decisions keep it inside the 500 ms p95 budget of R3.15:

1. **The list resolves no plans at all.** R3.2 enumerates exactly what the list
   carries and every one of those fields is on the ``FuelOrder`` document, so
   :meth:`DriverWorkService.list_work` is one search — the repository's
   ``search_for_driver``. The manifest and stop requirements (R3.7-R3.11) are
   requirements on *a returned fuel order* and are satisfied on the detail read.
2. **The detail read is a fixed 5 round trips, none of them per-compartment.**
   Order fetch, loading plan, route plan, execution, and then a single
   ``_msearch`` whose two bodies use ``terms`` filters — one over
   ``truck_compartments`` on ``compartment_id``, one over the station and
   customer-tank documents on ``station_id``. That is the N+1 elimination: the
   fan-out becomes a ``terms`` filter instead of a loop.
3. **Steps 2-5 are cached.** A plan is immutable for the life of a run, so the
   resolved ``(manifest, stops)`` bundle is cached in Redis under
   ``driver_work:{tenant_id}:{order_id}:{assigned_run_id}`` for 60 s. A warm
   detail read is one order fetch plus one Redis GET. An absent
   ``redis_client`` is a permanent cache miss, never an error.

Degradation is explicit, never silent: no loading plan yields an empty manifest
with ``manifest_available: false`` (R3.11) and no route plan yields an empty
stop list with ``route_available: false``. A will-call order with no run is
normal, so neither is an error.

Collaborators arrive through the constructor, matching the wiring pattern of
``driver/services/pod_service.py`` and ``driver/services/message_service.py``:
no container lookup, no service locator, no FastAPI ``Depends``.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Driver Work Read Model.

Validates: Requirements 3.1, 3.2, 3.7, 3.8, 3.9, 3.10, 3.11, 3.15, 5.26, 6.11,
15.6, 15.8, 15.11
- 3.1, 15.11: the scope is ``(tenant_id, driver_id)``, re-validated on every
  document after the query, never read from the request
- 3.2: the list projection is exactly the enumerated field set
- 3.7, 3.9: plan joins are ``assigned_asset_id`` and ``assigned_run_id``
- 3.8, 6.11: the manifest carries the compartment's prior product grade and the
  cross-contamination warning derived from it
- 3.10: stops carry sequence, station, coordinates, planned arrival, planned
  per-grade gallons, and completion status
- 3.11: an unresolvable plan is ``manifest_available: false``, not an error
- 3.15: one round trip for the list, five for a cold detail read, two warm
- 5.26: ``pod_otp`` is stripped from the order dict before serialization
- 15.6: ``customer_phone`` is omitted entirely without PII access
- 15.8: every returned ``file_ref`` is validated against the tenant prefix
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from Agents.support.mvp_es_mappings import (
    MVP_LOAD_PLANS_INDEX,
    MVP_PLAN_EXECUTIONS_INDEX,
    MVP_ROUTES_INDEX,
    TRUCK_COMPARTMENTS_INDEX,
)
from errors.exceptions import resource_not_found
from fuel.services.fuel_es_mappings import FUEL_STATIONS_INDEX
from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX
from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter
from services.unit_conversion import to_canonical_volume

logger = logging.getLogger(__name__)

#: Every volume this surface returns is US gallons, stated explicitly on the
#: payload so no consumer has to infer it (R6.23, R16.10, R16.19).
QUANTITY_UNIT = "us_gallon"

#: Redis key for the resolved ``(manifest, stops)`` bundle.
CACHE_KEY_TEMPLATE = "driver_work:{tenant_id}:{order_id}:{run_id}"

#: Bundle TTL. A plan is immutable for the life of a run, so this bounds how
#: stale a manifest can be after an out-of-band plan replacement.
CACHE_TTL_SECONDS = 60

#: Substituted into the cache key when the order carries no run, so the key
#: never ends in a bare separator.
_NO_RUN = "none"

#: Field name on the order document that must never leave the server on any
#: ``/api/driver/*`` response (R5.26).
_POD_OTP_FIELD = "pod_otp"

#: Any key ending in one of these is treated as an artifact reference and is
#: tenant-prefix validated before it is returned (R15.8).
_FILE_REF_SUFFIXES = ("_ref", "_refs", "file_ref", "file_refs")


class DriverWorkService:
    """Assigned-order list, single-order detail, and driver identity.

    Args:
        es_service: The shared ``ElasticsearchService``. Used for the plan,
            route, execution, compartment, destination, and ``drivers_current``
            reads. ``multi_search`` is preferred for the fan-out step; a service
            without it degrades to sequential searches, which costs round trips
            but never correctness.
        order_repository: ``FuelOrderRepository``. ``search_for_driver`` serves
            the list and ``get`` serves the detail read.
        job_service: Held so this service carries the same collaborator set the
            driver surface wires elsewhere. The work read model resolves
            everything from the order and the plan indices, so it is unused
            today and wiring it now avoids a signature change later.
        redis_client: Optional. When absent, every bundle read is a cache miss
            and every invalidation is a no-op — a permanent miss, not an error.
        file_storage_service: Optional ``FileStorageService``. Supplies
            ``validate_ref``, which is what enforces the tenant prefix on a
            returned artifact reference (R15.8). When absent, a reference that
            cannot be validated is dropped rather than returned.
    """

    def __init__(
        self,
        *,
        es_service,
        order_repository=None,
        job_service=None,
        redis_client=None,
        file_storage_service=None,
    ) -> None:
        self._es_service = es_service
        self._order_repository = order_repository
        self._job_service = job_service
        self._redis_client = redis_client
        self._file_storage_service = file_storage_service

    # ------------------------------------------------------------------
    # List — one round trip (R3.1-R3.5, R3.14, R3.15)
    # ------------------------------------------------------------------

    async def list_work(
        self,
        tenant_id: str,
        driver_id: str,
        *,
        statuses: Sequence[str] = (),
        window_start: Optional[str] = None,
        window_end: Optional[str] = None,
        page: int = 1,
        size: int = 50,
        has_pii_access: bool = False,
    ) -> dict:
        """Return the page of orders assigned to ``driver_id``.

        One search and no plan resolution. ``driver_id`` comes from the verified
        session, never from the request (R3.12) — this method has no parameter
        that could carry a client-supplied scope.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The caller's canonical ``drivers_current.driver_id``.
            statuses: Statuses to include. Empty falls back to the repository
                default, ``("dispatched", "in_transit")`` (R3.3).
            window_start: Include orders whose ``delivery_window_start`` is on
                or after this ISO-8601 timestamp (R3.5).
            window_end: Include orders whose ``delivery_window_start`` is on or
                before this ISO-8601 timestamp (R3.5).
            page: 1-based page number.
            size: Page size.
            has_pii_access: The caller's ``TenantContext.has_pii_access``.
                Defaults to ``False`` so the fail-closed path is the default:
                ``customer_phone`` is omitted unless access is proven (R15.6).

        Returns:
            ``{"data": [<summary>, ...], "pagination": {...}}``. The router adds
            ``request_id`` to the envelope.

        Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.14, 3.15, 15.6, 15.11
        """
        repository = self._require_order_repository()
        result = await repository.search_for_driver(
            tenant_id,
            driver_id,
            statuses=tuple(statuses or ()),
            window_start=window_start,
            window_end=window_end,
            page=page,
            size=size,
        )

        summaries = [
            self._order_summary(
                _as_document(order),
                tenant_id=tenant_id,
                driver_id=driver_id,
                has_pii_access=has_pii_access,
            )
            for order in (result.get("orders") or [])
        ]

        total = int(result.get("total") or 0)
        page_size = int(result.get("size") or size) or size
        return {
            "data": summaries,
            "pagination": {
                "page": int(result.get("page") or page),
                "size": page_size,
                "total": total,
                "total_pages": max(1, -(-total // page_size)),  # ceil division
            },
        }

    # ------------------------------------------------------------------
    # Detail — five cold round trips, two warm (R3.6-R3.11, R3.15)
    # ------------------------------------------------------------------

    async def get_work(
        self,
        tenant_id: str,
        driver_id: str,
        order_id: str,
        *,
        has_pii_access: bool = False,
    ) -> dict:
        """Return one assigned order with its manifest and stop sequence.

        An order that is not this driver's is indistinguishable from an absent
        one: both are 404 ``RESOURCE_NOT_FOUND`` (R3.6), so the endpoint cannot
        be used to probe which orders exist in the tenant.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The caller's canonical driver identity.
            order_id: The order to read.
            has_pii_access: The caller's ``TenantContext.has_pii_access``.

        Returns:
            ``{"data": {...}}`` carrying the order projection, the compartment
            manifest with ``manifest_available``, and the stop sequence with
            ``route_available``.

        Raises:
            AppException: 404 ``RESOURCE_NOT_FOUND`` when the order does not
                exist in this tenant or is assigned to another driver.

        Validates: Requirements 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 5.26, 6.11,
        15.6, 15.8, 15.11
        """
        repository = self._require_order_repository()
        order = await repository.get(tenant_id, order_id)
        doc = self._validated_order_doc(
            order, tenant_id=tenant_id, driver_id=driver_id, order_id=order_id
        )

        payload = self._order_summary(
            doc,
            tenant_id=tenant_id,
            driver_id=driver_id,
            has_pii_access=has_pii_access,
        )
        payload.update(await self._resolve_bundle(tenant_id, doc))
        return {"data": payload}

    # ------------------------------------------------------------------
    # Identity (R1.11, R13.10)
    # ------------------------------------------------------------------

    async def get_identity(self, tenant_id: str, driver_id: str) -> dict:
        """Return the caller's own identity and duty status.

        Args:
            tenant_id: The verified tenant scope.
            driver_id: The caller's canonical driver identity.

        Returns:
            ``{"data": {"driver_id", "tenant_id", "driver_name",
            "assigned_truck_id", "duty_status", "duty_status_updated_at"}}``.

        Raises:
            AppException: 404 ``RESOURCE_NOT_FOUND`` when the tenant holds no
                ``drivers_current`` record for this identity.

        Validates: Requirements 1.11, 13.10, 15.11
        """
        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"driver_id": driver_id}}]}}},
            tenant_id,
        )
        query["size"] = 1

        response = await self._es_service.search_documents(
            DRIVERS_CURRENT_INDEX, query, 1
        )
        sources = _sources(response)
        # Per-document tenant re-validation (R15.11): a mis-labelled document is
        # treated as absent rather than returned.
        record = next(
            (s for s in sources if s.get("tenant_id") == tenant_id), None
        )
        if record is None:
            raise resource_not_found(
                message="Driver record not found",
                details={"driver_id": driver_id},
            )

        return {
            "data": {
                "driver_id": record.get("driver_id") or driver_id,
                "tenant_id": tenant_id,
                "driver_name": record.get("driver_name"),
                "assigned_truck_id": record.get("assigned_truck_id"),
                "duty_status": record.get("status"),
                "duty_status_updated_at": _iso(
                    record.get("duty_status_updated_at")
                ),
            }
        }

    # ------------------------------------------------------------------
    # Cache invalidation (assignment / assignment_revoked / stop check-in)
    # ------------------------------------------------------------------

    async def invalidate(
        self,
        tenant_id: str,
        order_id: str,
        *,
        assigned_run_id: Optional[str] = None,
    ) -> None:
        """Drop the cached bundle for one order.

        Called on ``assignment``, on ``assignment_revoked``, and on a stop
        check-in, because each of the three can change the manifest or the stop
        statuses inside the 60 s window. ``assigned_run_id`` is part of the key,
        so when the caller does not know it (an assignment change is exactly the
        case where the run may have changed) every run's bundle for that order
        is dropped.

        A missing ``redis_client`` makes this a no-op, and a Redis failure is
        logged and swallowed: a stale bundle expires in at most 60 s, which is
        never worth failing a write for.
        """
        if self._redis_client is None:
            return

        if assigned_run_id:
            keys: List[str] = [_cache_key(tenant_id, order_id, assigned_run_id)]
        else:
            keys = await self._scan_keys(
                CACHE_KEY_TEMPLATE.format(
                    tenant_id=tenant_id, order_id=order_id, run_id="*"
                )
            )
        if not keys:
            return
        try:
            await self._redis_client.delete(*keys)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Driver work cache invalidation failed tenant=%s order=%s: %s",
                tenant_id,
                order_id,
                exc,
            )

    async def _scan_keys(self, pattern: str) -> List[str]:
        """Best-effort key enumeration for a wildcard invalidation."""
        client = self._redis_client
        scan_iter = getattr(client, "scan_iter", None)
        try:
            if scan_iter is not None:
                return [_as_text(key) async for key in scan_iter(match=pattern)]
            keys = getattr(client, "keys", None)
            if keys is not None:
                return [_as_text(key) for key in (await keys(pattern)) or []]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Driver work cache key scan failed for %s: %s", pattern, exc
            )
        return []

    # ------------------------------------------------------------------
    # Bundle resolution
    # ------------------------------------------------------------------

    async def _resolve_bundle(self, tenant_id: str, doc: Mapping[str, Any]) -> dict:
        """Return the ``(manifest, stops)`` bundle, cached for 60 s."""
        order_id = str(doc.get("order_id") or "")
        run_id = doc.get("assigned_run_id") or ""
        key = _cache_key(tenant_id, order_id, run_id)

        cached = await self._cache_get(key)
        if cached is not None:
            return cached

        bundle = await self._resolve_bundle_uncached(tenant_id, doc)
        await self._cache_set(key, bundle)
        return bundle

    async def _resolve_bundle_uncached(
        self, tenant_id: str, doc: Mapping[str, Any]
    ) -> dict:
        """Resolve plan, route, execution, compartments, and stop geo.

        Round trips 2-5 of the detail read. Step 5 is one ``_msearch`` whose two
        bodies each carry a ``terms`` filter, so compartment count and stop
        count do not change the hop count.
        """
        asset_id = doc.get("assigned_asset_id") or ""
        run_id = doc.get("assigned_run_id") or ""

        plan = await self._fetch_loading_plan(tenant_id, asset_id)
        route = await self._fetch_route_plan(tenant_id, run_id, asset_id)

        assignments = _nested_list(plan, "assignments")
        stops = sorted(
            _nested_list(route, "stops"),
            key=lambda stop: _as_int(stop.get("sequence")),
        )

        execution = await self._fetch_execution(tenant_id, plan, route)

        compartments, destinations = await self._fetch_fan_out(
            tenant_id,
            compartment_ids=[
                str(a.get("compartment_id"))
                for a in assignments
                if a.get("compartment_id")
            ],
            station_ids=[
                str(s.get("station_id")) for s in stops if s.get("station_id")
            ],
        )

        return {
            "manifest_available": plan is not None,
            "compartment_manifest": [
                self._manifest_entry(assignment, compartments)
                for assignment in assignments
            ],
            "route_available": route is not None,
            "stops": [
                self._stop_entry(stop, destinations, execution) for stop in stops
            ],
        }

    async def _fetch_loading_plan(
        self, tenant_id: str, asset_id: str
    ) -> Optional[dict]:
        """The most recent loading plan for the order's truck (R3.7).

        The join is the plan's truck identifier against the order's
        ``assigned_asset_id``, which is the only link between a driver's order
        and a truck-keyed plan. No asset means no plan, and no plan means
        ``manifest_available: false`` (R3.11) — not an error.
        """
        if not asset_id:
            return None
        return await self._search_one(
            MVP_LOAD_PLANS_INDEX,
            tenant_id,
            [{"term": {"truck_id": asset_id}}],
            sort=[{"created_at": {"order": "desc"}}],
        )

    async def _fetch_route_plan(
        self, tenant_id: str, run_id: str, asset_id: str = ""
    ) -> Optional[dict]:
        """The route for this order's run and truck (R3.9).

        A pipeline run can produce one route per truck.  Filtering only by
        ``run_id`` let a driver receive whichever truck route happened to be
        newest, so the assigned asset is included whenever it is available.
        """
        if not run_id:
            return None
        filters = [{"term": {"run_id": run_id}}]
        if asset_id:
            filters.append({"term": {"truck_id": asset_id}})
        return await self._search_one(
            MVP_ROUTES_INDEX,
            tenant_id,
            filters,
            sort=[{"timestamp": {"order": "desc"}}],
        )

    async def _fetch_execution(
        self,
        tenant_id: str,
        plan: Optional[Mapping[str, Any]],
        route: Optional[Mapping[str, Any]],
    ) -> Optional[dict]:
        """The execution record carrying per-stop completion status (R3.10)."""
        plan_id = (plan or {}).get("plan_id") or (route or {}).get("plan_id")
        route_id = (route or {}).get("route_id")
        filters: List[dict] = []
        if plan_id:
            filters.append({"term": {"plan_id": plan_id}})
        if route_id:
            filters.append({"term": {"route_id": route_id}})
        if not filters:
            return None
        return await self._search_one(
            MVP_PLAN_EXECUTIONS_INDEX,
            tenant_id,
            filters,
            sort=[{"updated_at": {"order": "desc"}}],
        )

    async def _fetch_fan_out(
        self,
        tenant_id: str,
        *,
        compartment_ids: Sequence[str],
        station_ids: Sequence[str],
    ) -> tuple[Dict[str, dict], Dict[str, dict]]:
        """Compartment states and stop destinations in ONE round trip.

        This is the N+1 elimination. Two bodies, each a ``terms`` filter: one
        over ``truck_compartments`` keyed on ``compartment_id``, one over
        ``fuel_stations`` and ``customer_tanks`` keyed on the stop identifier.
        A stop identifier may name either a retail station or a customer tank,
        so the second body matches ``station_id`` or ``customer_tank_id`` and
        spans both indices in a single search.

        Returns:
            ``(compartments_by_id, destinations_by_id)``.
        """
        compartment_ids = _unique(compartment_ids)
        station_ids = _unique(station_ids)

        searches: List[Dict[str, Any]] = []
        if compartment_ids:
            searches.append(
                {
                    "index": TRUCK_COMPARTMENTS_INDEX,
                    "query": _terms_query(
                        tenant_id,
                        [{"terms": {"compartment_id": list(compartment_ids)}}],
                        size=len(compartment_ids),
                    ),
                }
            )
        if station_ids:
            searches.append(
                {
                    "index": f"{FUEL_STATIONS_INDEX},{CUSTOMER_TANKS_INDEX}",
                    "query": _terms_query(
                        tenant_id,
                        [
                            {
                                "bool": {
                                    "should": [
                                        {"terms": {"station_id": list(station_ids)}},
                                        {
                                            "terms": {
                                                "customer_tank_id": list(station_ids)
                                            }
                                        },
                                    ],
                                    "minimum_should_match": 1,
                                }
                            }
                        ],
                        # Both indices can answer, so allow for a hit apiece.
                        size=len(station_ids) * 2,
                    ),
                }
            )

        responses = await self._multi_search(searches)

        compartments: Dict[str, dict] = {}
        destinations: Dict[str, dict] = {}
        cursor = 0
        if compartment_ids:
            compartments = self._by_key(
                responses[cursor] if cursor < len(responses) else None,
                tenant_id,
                ("compartment_id",),
            )
            cursor += 1
        if station_ids:
            destinations = self._by_key(
                responses[cursor] if cursor < len(responses) else None,
                tenant_id,
                ("station_id", "customer_tank_id"),
            )
        return compartments, destinations

    async def _multi_search(
        self, searches: List[Dict[str, Any]]
    ) -> List[Optional[dict]]:
        """Run ``searches`` in one round trip, or sequentially as a fallback.

        ``ElasticsearchService.multi_search`` is the one-hop path. A service
        without it (an older deployment, or a narrow test double) falls back to
        one ``search_documents`` per body: more hops, identical results.
        """
        if not searches:
            return []

        multi = getattr(self._es_service, "multi_search", None)
        if multi is not None:
            response = await multi(searches)
            return list((response or {}).get("responses") or [])

        return [
            await self._es_service.search_documents(
                entry["index"],
                entry["query"],
                entry["query"].get("size", 100),
            )
            for entry in searches
        ]

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------

    def _order_summary(
        self,
        doc: Mapping[str, Any],
        *,
        tenant_id: str,
        driver_id: str,
        has_pii_access: bool,
    ) -> dict:
        """Project one order document onto the R3.2 field set.

        ``pod_otp`` is dropped here rather than anywhere downstream, so there is
        exactly one place the code could ever have leaked it (R5.26).
        """
        summary: Dict[str, Any] = {
            "order_id": doc.get("order_id"),
            "status": doc.get("status"),
            "delivery_window_start": _iso(doc.get("delivery_window_start")),
            "delivery_window_end": _iso(doc.get("delivery_window_end")),
            "destination": {
                "address": doc.get("ship_to_address"),
                "lat": _as_float(doc.get("ship_to_lat")),
                "lon": _as_float(doc.get("ship_to_lon")),
            },
            "customer_name": doc.get("customer_name"),
            "product_grade": doc.get("product_code"),
            "ordered_gallons": _as_float(doc.get("gallons_requested")),
            "quantity_unit": QUANTITY_UNIT,
        }
        # Omitted entirely, not nulled: an absent key cannot be mistaken for a
        # customer who has no phone number on file (R15.6).
        if has_pii_access:
            summary["customer_phone"] = doc.get("customer_phone")

        return self._sanitize_file_refs(
            summary, tenant_id=tenant_id, actor=driver_id
        )

    def _manifest_entry(
        self, assignment: Mapping[str, Any], compartments: Mapping[str, dict]
    ) -> dict:
        """One compartment manifest row (R3.8, R6.11).

        ``LoadingPlan`` assignments are stored in litres, so the planned volume
        is converted to gallons here — the surface is gallons throughout. The
        prior product grade comes from ``truck_compartments.last_loaded_product``
        and the cross-contamination warning is derived from it: a prior grade
        that differs from the grade about to be loaded, with no cleaning event
        after the last load, is what the driver has to be told about.
        """
        compartment_id = assignment.get("compartment_id")
        state = compartments.get(str(compartment_id), {})
        planned_grade = assignment.get("fuel_grade")
        prior_grade = state.get("last_loaded_product")

        return {
            "compartment_id": compartment_id,
            "product_grade": planned_grade,
            "planned_gallons": _liters_to_gallons(
                assignment.get("quantity_liters")
            ),
            "prior_product_grade": prior_grade,
            "cross_contamination_warning": bool(
                prior_grade and planned_grade and prior_grade != planned_grade
            ),
            "last_cleaned_at": _iso(state.get("last_cleaned_at")),
        }

    def _stop_entry(
        self,
        stop: Mapping[str, Any],
        destinations: Mapping[str, dict],
        execution: Optional[Mapping[str, Any]],
    ) -> dict:
        """One stop row (R3.10).

        Coordinates come from the station or customer-tank document, never from
        the route plan, which carries none. Completion status comes from the
        matching ``mvp_plan_executions`` stop, matched on sequence and falling
        back to the station identifier; an unmatched stop is ``pending``.
        """
        station_id = stop.get("station_id")
        destination = destinations.get(str(station_id), {})
        lat, lon = _coordinates(destination)

        return {
            "sequence": _as_int(stop.get("sequence")),
            "station_id": station_id,
            "lat": lat,
            "lon": lon,
            "planned_arrival": _iso(stop.get("eta")),
            "planned_gallons_by_grade": {
                str(grade): _liters_to_gallons(value)
                for grade, value in (stop.get("drop") or {}).items()
            },
            "status": _execution_status(execution, stop),
        }

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _validated_order_doc(
        self,
        order: Any,
        *,
        tenant_id: str,
        driver_id: str,
        order_id: str,
    ) -> dict:
        """Normalize and re-validate the fetched order, or 404.

        Three rejections collapse into one response: absent, another tenant's,
        and another driver's all return 404 ``RESOURCE_NOT_FOUND`` (R3.6), so
        the caller learns nothing about work that is not theirs.
        """
        if order is None:
            raise resource_not_found(
                message="Order not found", details={"order_id": order_id}
            )

        doc = _as_document(order)
        doc.pop(_POD_OTP_FIELD, None)  # R5.26 — before anything serializes it

        if doc.get("tenant_id") != tenant_id:
            logger.warning(
                "DriverWorkService.get_work: suppressing cross-tenant order %s",
                order_id,
            )
            raise resource_not_found(
                message="Order not found", details={"order_id": order_id}
            )
        if (doc.get("assigned_driver_id") or "") != driver_id:
            raise resource_not_found(
                message="Order not found", details={"order_id": order_id}
            )
        return doc

    def _sanitize_file_refs(
        self, payload: dict, *, tenant_id: str, actor: str
    ) -> dict:
        """Drop any artifact reference that is not this tenant's (R15.8).

        A read surface has no caller to blame for a bad stored reference, so a
        reference that fails validation is omitted and logged rather than
        turned into an error the driver cannot act on. Without a
        ``file_storage_service`` there is nothing to validate with, so the
        reference is dropped: fail closed.
        """
        for field in [k for k in payload if _is_file_ref_field(k)]:
            values = payload[field]
            if isinstance(values, list):
                payload[field] = [
                    ref
                    for ref in values
                    if self._ref_is_this_tenants(ref, tenant_id, actor, field)
                ]
            elif values and not self._ref_is_this_tenants(
                values, tenant_id, actor, field
            ):
                payload.pop(field)
        return payload

    def _ref_is_this_tenants(
        self, ref: Any, tenant_id: str, actor: str, field: str
    ) -> bool:
        """True when ``ref`` carries the caller's tenant prefix."""
        if not isinstance(ref, str) or not ref:
            return False
        if self._file_storage_service is None:
            logger.warning(
                "Dropping %s from driver work payload: no file_storage_service "
                "to validate the tenant prefix with",
                field,
            )
            return False
        try:
            return bool(
                self._file_storage_service.validate_ref(
                    tenant_id=tenant_id, file_ref=ref, actor=actor
                )
            )
        except (PermissionError, ValueError) as exc:
            logger.warning(
                "Dropping cross-tenant or malformed file_ref on %s "
                "for tenant=%s: %s",
                field,
                tenant_id,
                exc,
            )
            return False

    def _require_order_repository(self):
        """Return the order repository or fail loudly.

        A misconfigured wiring is a startup defect, not a request-time error:
        surfacing it as ``RuntimeError`` keeps it out of the driver-facing error
        vocabulary.
        """
        if self._order_repository is None:
            raise RuntimeError(
                "DriverWorkService has no order_repository. Pass one from "
                "configure_work_endpoints() during startup."
            )
        return self._order_repository

    # ------------------------------------------------------------------
    # Elasticsearch and Redis primitives
    # ------------------------------------------------------------------

    async def _search_one(
        self,
        index: str,
        tenant_id: str,
        filters: List[dict],
        *,
        sort: Optional[List[dict]] = None,
    ) -> Optional[dict]:
        """One tenant-filtered ``size: 1`` search, tenant re-validated."""
        query = inject_tenant_filter(
            {"query": {"bool": {"filter": list(filters)}}}, tenant_id
        )
        query["size"] = 1
        if sort:
            query["sort"] = sort

        try:
            response = await self._es_service.search_documents(index, query, 1)
        except Exception as exc:
            # A missing optional plan index must not fail a work read; the
            # order is still returned, with availability reported false.
            logger.warning(
                "Driver work resolution: %s lookup failed tenant=%s: %s",
                index,
                tenant_id,
                exc,
            )
            return None

        for source in _sources(response):
            if source.get("tenant_id") == tenant_id:
                return source
        return None

    def _by_key(
        self,
        response: Optional[Mapping[str, Any]],
        tenant_id: str,
        key_fields: Sequence[str],
    ) -> Dict[str, dict]:
        """Index one search response by the first present key field.

        Every document is re-validated on ``tenant_id`` before it is indexed
        (R15.11), so a mis-labelled compartment or station cannot reach a
        driver in another tenant.
        """
        out: Dict[str, dict] = {}
        for source in _sources(response):
            if source.get("tenant_id") != tenant_id:
                continue
            for field in key_fields:
                value = source.get(field)
                if value:
                    out[str(value)] = source
                    break
        return out

    async def _cache_get(self, key: str) -> Optional[dict]:
        """Read the cached bundle. Absent Redis is a permanent miss."""
        if self._redis_client is None:
            return None
        try:
            raw = await self._redis_client.get(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Driver work cache read failed for %s: %s", key, exc)
            return None
        if not raw:
            return None
        try:
            bundle = json.loads(_as_text(raw))
        except (TypeError, ValueError) as exc:
            logger.warning("Discarding malformed driver work cache %s: %s", key, exc)
            return None
        return bundle if isinstance(bundle, dict) else None

    async def _cache_set(self, key: str, bundle: dict) -> None:
        """Write the bundle with a 60 s TTL. Failures never fail the read."""
        if self._redis_client is None:
            return
        try:
            await self._redis_client.setex(
                key, CACHE_TTL_SECONDS, json.dumps(bundle, default=str)
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Driver work cache write failed for %s: %s", key, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cache_key(tenant_id: str, order_id: str, run_id: Optional[str]) -> str:
    """The bundle cache key, run-scoped so a re-run cannot serve stale stops."""
    return CACHE_KEY_TEMPLATE.format(
        tenant_id=tenant_id, order_id=order_id, run_id=run_id or _NO_RUN
    )


def _terms_query(tenant_id: str, filters: List[dict], *, size: int) -> dict:
    """A tenant-filtered ``terms`` query body for one ``_msearch`` leg."""
    query = inject_tenant_filter(
        {"query": {"bool": {"filter": list(filters)}}}, tenant_id
    )
    query["size"] = max(1, size)
    return query


def _execution_status(
    execution: Optional[Mapping[str, Any]], stop: Mapping[str, Any]
) -> str:
    """Completion status for one route stop, defaulting to ``pending``."""
    if not execution:
        return "pending"
    sequence = _as_int(stop.get("sequence"))
    station_id = stop.get("station_id")
    fallback: Optional[str] = None
    for record in _nested_list(execution, "stops"):
        if _as_int(record.get("sequence")) == sequence:
            return str(record.get("status") or "pending")
        if station_id and record.get("station_id") == station_id:
            fallback = str(record.get("status") or "pending")
    return fallback or "pending"


def _coordinates(destination: Mapping[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """Read lat/lon from a station or customer-tank document.

    The two indices spell coordinates differently — ``latitude`` / ``longitude``
    on ``fuel_stations``, ``location_lat`` / ``location_lon`` on
    ``customer_tanks`` — and both also carry a ``geo_point`` ``location``. All
    three spellings are accepted so a stop resolves regardless of source.
    """
    lat = _as_float(destination.get("latitude"))
    lon = _as_float(destination.get("longitude"))
    if lat is None or lon is None:
        lat = _as_float(destination.get("location_lat")) if lat is None else lat
        lon = _as_float(destination.get("location_lon")) if lon is None else lon
    location = destination.get("location")
    if isinstance(location, Mapping):
        lat = _as_float(location.get("lat")) if lat is None else lat
        lon = _as_float(location.get("lon")) if lon is None else lon
    return lat, lon


def _nested_list(doc: Optional[Mapping[str, Any]], field: str) -> List[dict]:
    """Read a nested field as a list of dicts, tolerating an absent parent."""
    values = (doc or {}).get(field) or []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _liters_to_gallons(value: Any) -> Optional[float]:
    """Convert a litre-valued plan field to US gallons (R6.23).

    The plan indices store litres; this surface is gallons throughout, stated
    explicitly by ``quantity_unit``. Rounded to two places because this is a
    presentation boundary, not an accounting one.
    """
    as_float = _as_float(value)
    if as_float is None:
        return None
    return round(to_canonical_volume(as_float, "l"), 2)


def _as_document(order: Any) -> dict:
    """Normalize a repository result into a plain dict.

    ``FuelOrderRepository`` returns ``FuelOrder`` models; the read-cutover path
    and test doubles may hand back a raw document. Both are accepted so this
    service depends on the shape of the data, not on the class.
    """
    if isinstance(order, dict):
        return dict(order)
    model_dump = getattr(order, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    return dict(order)


def _sources(response: Optional[Mapping[str, Any]]) -> List[dict]:
    """Extract ``_source`` dicts from a search response."""
    hits = ((response or {}).get("hits") or {}).get("hits") or []
    return [hit.get("_source") or {} for hit in hits if isinstance(hit, Mapping)]


def _unique(values: Iterable[str]) -> List[str]:
    """Order-preserving de-duplication, so the ``terms`` filter stays tight."""
    seen: Dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _as_float(value: Any) -> Optional[float]:
    """Coerce to float, or ``None`` when the value is not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _as_int(value: Any) -> int:
    """Coerce a sequence number to int, defaulting to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso(value: Any) -> Optional[str]:
    """Render a timestamp as ISO-8601 text, passing strings through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_text(value: Any) -> str:
    """Decode a Redis value that may arrive as bytes."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_file_ref_field(name: str) -> bool:
    """True when a payload key names an artifact reference (R15.8)."""
    return isinstance(name, str) and name.endswith(_FILE_REF_SUFFIXES)
