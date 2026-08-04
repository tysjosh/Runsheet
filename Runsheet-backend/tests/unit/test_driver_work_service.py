"""
Unit tests for ``DriverWorkService`` (``driver/services/work_service.py``).

Covers the one-round-trip list, the fixed-budget detail read and its cached
form, explicit degradation when a plan or route is absent, the 404 on another
driver's order, PII omission, ``pod_otp`` stripping, and gallons normalization.

Validates: Requirements 3.1, 3.2, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.14, 3.15,
5.26, 6.11, 15.6, 15.8, 15.11
"""

import json

import pytest

from driver.services.work_service import (
    CACHE_TTL_SECONDS,
    QUANTITY_UNIT,
    DriverWorkService,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException

TENANT = "t1"
OTHER_TENANT = "t2"
DRIVER = "drv-1"
ORDER = "ord-1"
TRUCK = "truck-1"
RUN = "run-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _order_doc(**overrides) -> dict:
    doc = {
        "order_id": ORDER,
        "tenant_id": TENANT,
        "status": "dispatched",
        "assigned_driver_id": DRIVER,
        "assigned_asset_id": TRUCK,
        "assigned_run_id": RUN,
        "delivery_window_start": "2026-01-05T08:00:00+00:00",
        "delivery_window_end": "2026-01-05T12:00:00+00:00",
        "ship_to_address": "1 Depot Rd",
        "ship_to_lat": 29.76,
        "ship_to_lon": -95.37,
        "customer_name": "Acme Fuel",
        "customer_phone": "+15550001111",
        "product_code": "DIESEL_2",
        "gallons_requested": 3200.0,
        # Never allowed off the server on a /api/driver/* response (R5.26).
        "pod_otp": "845213",
    }
    doc.update(overrides)
    return doc


class _FakeOrderRepository:
    """Stands in for ``FuelOrderRepository`` on both read paths."""

    def __init__(self, *, orders=None, order=None, total=None):
        self._orders = orders if orders is not None else []
        self._order = order
        self._total = len(self._orders) if total is None else total
        self.search_calls: list[dict] = []
        self.get_calls: list[tuple[str, str]] = []

    async def search_for_driver(
        self,
        tenant_id,
        driver_id,
        *,
        statuses=(),
        window_start=None,
        window_end=None,
        page=1,
        size=50,
    ):
        self.search_calls.append(
            {
                "tenant_id": tenant_id,
                "driver_id": driver_id,
                "statuses": statuses,
                "window_start": window_start,
                "window_end": window_end,
                "page": page,
                "size": size,
            }
        )
        return {
            "orders": list(self._orders),
            "total": self._total,
            "page": page,
            "size": size,
        }

    async def get(self, tenant_id, order_id):
        self.get_calls.append((tenant_id, order_id))
        return self._order


def _hits(sources):
    return {"hits": {"hits": [{"_source": s} for s in sources]}}


class _FakeES:
    """Counts round trips so the budget in R3.15 is actually asserted."""

    def __init__(self, *, by_index=None, multi_responses=None):
        self._by_index = by_index or {}
        self._multi_responses = multi_responses or []
        self.searches: list[str] = []
        self.multi_calls: list[list] = []

    async def search_documents(self, index, query, size=100):
        self.searches.append(index)
        return _hits(self._by_index.get(index, []))

    async def multi_search(self, searches):
        self.multi_calls.append(searches)
        return {
            "responses": [
                _hits(sources) for sources in self._multi_responses
            ]
        }

    @property
    def round_trips(self) -> int:
        return len(self.searches) + len(self.multi_calls)


class _FakeRedis:
    def __init__(self, seed=None):
        self.store = dict(seed or {})
        self.setex_calls: list[tuple[str, int]] = []
        self.deleted: list[str] = []

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, payload):
        self.setex_calls.append((key, ttl))
        self.store[key] = payload

    async def delete(self, *keys):
        for key in keys:
            self.deleted.append(key)
            self.store.pop(key, None)

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]


class _FakeFileStorage:
    """``validate_ref`` semantics: True, or ``PermissionError``."""

    def __init__(self, *, allowed_prefix=TENANT):
        self._prefix = allowed_prefix

    def validate_ref(self, *, tenant_id, file_ref, actor=None):
        if not file_ref.startswith(f"{self._prefix}/"):
            raise PermissionError("tenant prefix mismatch")
        return True


def _plan(**overrides):
    plan = {
        "plan_id": "plan-1",
        "tenant_id": TENANT,
        "truck_id": TRUCK,
        "run_id": RUN,
        "assignments": [
            {
                "compartment_id": "c1",
                "station_id": "stn-1",
                "fuel_grade": "DIESEL_2",
                # 7003.0118 L is exactly 1850.0 US gallons.
                "quantity_liters": 7003.0118,
            }
        ],
    }
    plan.update(overrides)
    return plan


def _route(**overrides):
    route = {
        "route_id": "route-1",
        "plan_id": "plan-1",
        "tenant_id": TENANT,
        "truck_id": TRUCK,
        "run_id": RUN,
        "stops": [
            {
                "station_id": "stn-2",
                "sequence": 1,
                "eta": "2026-01-05T11:00:00+00:00",
                "drop": {"DIESEL_2": 3785.4118},
            },
            {
                "station_id": "stn-1",
                "sequence": 0,
                "eta": "2026-01-05T09:00:00+00:00",
                "drop": {"DIESEL_2": 7003.0118},
            },
        ],
    }
    route.update(overrides)
    return route


def _execution(**overrides):
    execution = {
        "execution_id": "exec-1",
        "tenant_id": TENANT,
        "plan_id": "plan-1",
        "route_id": "route-1",
        "stops": [
            {"station_id": "stn-1", "sequence": 0, "status": "completed"},
        ],
    }
    execution.update(overrides)
    return execution


def _resolved_es(**overrides):
    by_index = {
        "mvp_load_plans": [_plan()],
        "mvp_routes": [_route()],
        "mvp_plan_executions": [_execution()],
    }
    by_index.update(overrides)
    return _FakeES(
        by_index=by_index,
        multi_responses=[
            # truck_compartments
            [
                {
                    "tenant_id": TENANT,
                    "compartment_id": "c1",
                    "last_loaded_product": "DIESEL_1_DYED",
                    "last_cleaned_at": None,
                }
            ],
            # fuel_stations,customer_tanks
            [
                {
                    "tenant_id": TENANT,
                    "station_id": "stn-1",
                    "latitude": 29.7,
                    "longitude": -95.4,
                },
                {
                    "tenant_id": TENANT,
                    "customer_tank_id": "stn-2",
                    "location_lat": 30.1,
                    "location_lon": -95.9,
                },
            ],
        ],
    )


def _service(*, es=None, repository=None, redis=None, file_storage=None):
    return DriverWorkService(
        es_service=es if es is not None else _FakeES(),
        order_repository=repository,
        redis_client=redis,
        file_storage_service=file_storage,
    )


# ---------------------------------------------------------------------------
# list_work — one round trip, no plan resolution (R3.1-R3.5, R3.14, R3.15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_work_is_one_round_trip_and_resolves_no_plans():
    es = _resolved_es()
    repository = _FakeOrderRepository(orders=[_order_doc()], total=1)
    service = _service(es=es, repository=repository)

    result = await service.list_work(TENANT, DRIVER, page=1, size=50)

    assert len(repository.search_calls) == 1
    # No plan, route, execution, compartment, or destination lookup happens.
    assert es.round_trips == 0
    assert len(result["data"]) == 1
    assert result["data"][0]["order_id"] == ORDER
    assert "compartment_manifest" not in result["data"][0]


@pytest.mark.asyncio
async def test_list_work_projection_and_pagination_envelope():
    repository = _FakeOrderRepository(orders=[_order_doc()], total=12)
    service = _service(repository=repository)

    result = await service.list_work(
        TENANT,
        DRIVER,
        statuses=("in_transit",),
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-01-31T00:00:00Z",
        page=1,
        size=50,
        has_pii_access=True,
    )

    item = result["data"][0]
    assert item == {
        "order_id": ORDER,
        "status": "dispatched",
        "delivery_window_start": "2026-01-05T08:00:00+00:00",
        "delivery_window_end": "2026-01-05T12:00:00+00:00",
        "destination": {"address": "1 Depot Rd", "lat": 29.76, "lon": -95.37},
        "customer_name": "Acme Fuel",
        "product_grade": "DIESEL_2",
        "ordered_gallons": 3200.0,
        "quantity_unit": QUANTITY_UNIT,
        "customer_phone": "+15550001111",
    }
    assert result["pagination"] == {
        "page": 1,
        "size": 50,
        "total": 12,
        "total_pages": 1,
    }
    # Filters reach the repository untouched (R3.4, R3.5).
    assert repository.search_calls[0]["statuses"] == ("in_transit",)
    assert repository.search_calls[0]["window_end"] == "2026-01-31T00:00:00Z"


@pytest.mark.asyncio
async def test_list_work_omits_customer_phone_without_pii_access():
    repository = _FakeOrderRepository(orders=[_order_doc()], total=1)
    service = _service(repository=repository)

    result = await service.list_work(TENANT, DRIVER)

    # Omitted entirely, not nulled (R15.6).
    assert "customer_phone" not in result["data"][0]


@pytest.mark.asyncio
async def test_list_work_total_pages_ceils():
    repository = _FakeOrderRepository(orders=[], total=51)
    service = _service(repository=repository)

    result = await service.list_work(TENANT, DRIVER, size=50)

    assert result["pagination"]["total_pages"] == 2


# ---------------------------------------------------------------------------
# get_work — the fixed round-trip budget (R3.15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_work_cold_read_is_five_round_trips():
    es = _resolved_es()
    repository = _FakeOrderRepository(order=_order_doc())
    service = _service(es=es, repository=repository, redis=_FakeRedis())

    await service.get_work(TENANT, DRIVER, ORDER)

    # 1 order fetch (repository) + plan + route + execution + one _msearch.
    assert len(repository.get_calls) == 1
    assert es.searches == [
        "mvp_load_plans",
        "mvp_routes",
        "mvp_plan_executions",
    ]
    assert len(es.multi_calls) == 1
    assert len(repository.get_calls) + es.round_trips == 5


@pytest.mark.asyncio
async def test_get_work_fan_out_is_one_msearch_with_terms_filters():
    es = _resolved_es()
    service = _service(
        es=es, repository=_FakeOrderRepository(order=_order_doc())
    )

    await service.get_work(TENANT, DRIVER, ORDER)

    bodies = es.multi_calls[0]
    assert [b["index"] for b in bodies] == [
        "truck_compartments",
        "fuel_stations,customer_tanks",
    ]
    compartment_filters = bodies[0]["query"]["query"]["bool"]["must"][0][
        "bool"
    ]["filter"]
    assert {"terms": {"compartment_id": ["c1"]}} in compartment_filters


@pytest.mark.asyncio
async def test_get_work_manifest_and_stops():
    es = _resolved_es()
    service = _service(
        es=es, repository=_FakeOrderRepository(order=_order_doc())
    )

    payload = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert payload["manifest_available"] is True
    assert payload["compartment_manifest"] == [
        {
            "compartment_id": "c1",
            "product_grade": "DIESEL_2",
            "planned_gallons": 1850.0,
            "prior_product_grade": "DIESEL_1_DYED",
            "cross_contamination_warning": True,
            "last_cleaned_at": None,
        }
    ]

    assert payload["route_available"] is True
    # Stops come back sequence-ordered even though the route stored them out of
    # order, and coordinates come from the station / customer-tank documents.
    assert [s["sequence"] for s in payload["stops"]] == [0, 1]
    assert payload["stops"][0] == {
        "sequence": 0,
        "station_id": "stn-1",
        "lat": 29.7,
        "lon": -95.4,
        "planned_arrival": "2026-01-05T09:00:00+00:00",
        "planned_gallons_by_grade": {"DIESEL_2": 1850.0},
        "status": "completed",
    }
    # No execution record for sequence 1, so it degrades to pending.
    assert payload["stops"][1]["status"] == "pending"
    assert payload["stops"][1]["lat"] == 30.1
    assert payload["quantity_unit"] == QUANTITY_UNIT


@pytest.mark.asyncio
async def test_get_work_strips_pod_otp_and_omits_phone():
    service = _service(
        es=_resolved_es(), repository=_FakeOrderRepository(order=_order_doc())
    )

    payload = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert "pod_otp" not in json.dumps(payload)
    assert "customer_phone" not in payload


# ---------------------------------------------------------------------------
# Degradation is explicit, not silent (R3.11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_work_without_loading_plan_reports_manifest_unavailable():
    es = _resolved_es(mvp_load_plans=[])
    service = _service(
        es=es, repository=_FakeOrderRepository(order=_order_doc())
    )

    payload = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert payload["manifest_available"] is False
    assert payload["compartment_manifest"] == []
    # Not an error: the order itself still comes back.
    assert payload["order_id"] == ORDER


@pytest.mark.asyncio
async def test_get_work_without_route_reports_route_unavailable():
    es = _resolved_es(mvp_routes=[])
    service = _service(
        es=es, repository=_FakeOrderRepository(order=_order_doc())
    )

    payload = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert payload["route_available"] is False
    assert payload["stops"] == []
    assert payload["manifest_available"] is True


@pytest.mark.asyncio
async def test_get_work_without_run_or_asset_resolves_nothing():
    es = _resolved_es()
    order = _order_doc(assigned_asset_id=None, assigned_run_id=None)
    service = _service(es=es, repository=_FakeOrderRepository(order=order))

    payload = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert payload["manifest_available"] is False
    assert payload["route_available"] is False
    assert es.searches == []


# ---------------------------------------------------------------------------
# Scope: absent, another tenant's, another driver's all 404 (R3.6, R15.11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        None,
        _order_doc(assigned_driver_id="drv-other"),
        _order_doc(tenant_id=OTHER_TENANT),
        _order_doc(assigned_driver_id=None),
    ],
)
async def test_get_work_rejects_work_that_is_not_this_drivers(order):
    service = _service(
        es=_resolved_es(), repository=_FakeOrderRepository(order=order)
    )

    with pytest.raises(AppException) as exc:
        await service.get_work(TENANT, DRIVER, ORDER)

    assert exc.value.error_code == ErrorCode.RESOURCE_NOT_FOUND
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Cache (60 s TTL, absent Redis is a permanent miss)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_read_serves_bundle_from_cache():
    es = _resolved_es()
    redis = _FakeRedis()
    repository = _FakeOrderRepository(order=_order_doc())
    service = _service(es=es, repository=repository, redis=redis)

    first = (await service.get_work(TENANT, DRIVER, ORDER))["data"]
    cold_trips = es.round_trips
    second = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert second == first
    # A warm read is the order fetch plus one Redis GET, nothing more.
    assert es.round_trips == cold_trips
    key = f"driver_work:{TENANT}:{ORDER}:{RUN}"
    assert redis.setex_calls == [(key, CACHE_TTL_SECONDS)]


@pytest.mark.asyncio
async def test_absent_redis_is_a_permanent_miss_not_an_error():
    es = _resolved_es()
    service = _service(
        es=es, repository=_FakeOrderRepository(order=_order_doc()), redis=None
    )

    first = (await service.get_work(TENANT, DRIVER, ORDER))["data"]
    trips_after_first = es.round_trips
    second = (await service.get_work(TENANT, DRIVER, ORDER))["data"]

    assert first == second
    # Every read resolves from scratch, and nothing raises.
    assert es.round_trips == trips_after_first * 2


@pytest.mark.asyncio
async def test_invalidate_drops_every_run_bundle_for_the_order():
    key = f"driver_work:{TENANT}:{ORDER}:{RUN}"
    other_run = f"driver_work:{TENANT}:{ORDER}:run-2"
    other_order = f"driver_work:{TENANT}:ord-2:{RUN}"
    redis = _FakeRedis(seed={key: "{}", other_run: "{}", other_order: "{}"})
    service = _service(redis=redis)

    await service.invalidate(TENANT, ORDER)

    assert sorted(redis.deleted) == sorted([key, other_run])
    assert other_order in redis.store


@pytest.mark.asyncio
async def test_invalidate_with_run_id_targets_one_key():
    key = f"driver_work:{TENANT}:{ORDER}:{RUN}"
    redis = _FakeRedis(seed={key: "{}"})
    service = _service(redis=redis)

    await service.invalidate(TENANT, ORDER, assigned_run_id=RUN)

    assert redis.deleted == [key]


@pytest.mark.asyncio
async def test_invalidate_without_redis_is_a_no_op():
    await _service().invalidate(TENANT, ORDER)


# ---------------------------------------------------------------------------
# Artifact references (R15.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_file_ref_is_dropped_from_the_payload():
    service = _service(file_storage=_FakeFileStorage())

    payload = service._sanitize_file_refs(
        {
            "signature_ref": f"{OTHER_TENANT}/pod/sig.png",
            "photo_refs": [
                f"{TENANT}/pod/a.jpg",
                f"{OTHER_TENANT}/pod/b.jpg",
            ],
        },
        tenant_id=TENANT,
        actor=DRIVER,
    )

    assert "signature_ref" not in payload
    assert payload["photo_refs"] == [f"{TENANT}/pod/a.jpg"]


@pytest.mark.asyncio
async def test_file_refs_are_dropped_when_there_is_nothing_to_validate_with():
    service = _service(file_storage=None)

    payload = service._sanitize_file_refs(
        {"signature_ref": f"{TENANT}/pod/sig.png"},
        tenant_id=TENANT,
        actor=DRIVER,
    )

    assert "signature_ref" not in payload


# ---------------------------------------------------------------------------
# get_identity (R1.11, R13.10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_identity_returns_the_drivers_current_record():
    es = _FakeES(
        by_index={
            "drivers_current": [
                {
                    "tenant_id": TENANT,
                    "driver_id": DRIVER,
                    "driver_name": "Dana Ruiz",
                    "assigned_truck_id": TRUCK,
                    "status": "active",
                    "duty_status_updated_at": "2026-01-05T07:55:00+00:00",
                }
            ]
        }
    )
    service = _service(es=es)

    result = await service.get_identity(TENANT, DRIVER)

    assert result["data"] == {
        "driver_id": DRIVER,
        "tenant_id": TENANT,
        "driver_name": "Dana Ruiz",
        "assigned_truck_id": TRUCK,
        "duty_status": "active",
        "duty_status_updated_at": "2026-01-05T07:55:00+00:00",
    }


@pytest.mark.asyncio
async def test_get_identity_404s_without_a_driver_record():
    service = _service(es=_FakeES(by_index={"drivers_current": []}))

    with pytest.raises(AppException) as exc:
        await service.get_identity(TENANT, DRIVER)

    assert exc.value.error_code == ErrorCode.RESOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_get_identity_suppresses_a_cross_tenant_record():
    es = _FakeES(
        by_index={
            "drivers_current": [
                {"tenant_id": OTHER_TENANT, "driver_id": DRIVER}
            ]
        }
    )
    service = _service(es=es)

    with pytest.raises(AppException):
        await service.get_identity(TENANT, DRIVER)
