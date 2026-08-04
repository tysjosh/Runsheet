"""
Unit tests for the stop check-in volume-unit boundary and stop record contract.

Two halves of one path:

* ``POST /api/fuel/mvp/plan/{plan_id}/checkin`` accepts US gallons on
  ``actual_quantities_gallons`` with ``quantity_unit: "us_gallon"``, converts
  once through ``Agents.support.volume_units.us_gallons_to_liters``, and returns
  gallons only (Requirements 6.13–6.18, 6.23).
* ``PlanExecutionService.record_checkin`` persists the driver context, the unit
  discriminators, and the per-grade litre variance on the
  ``mvp_plan_executions`` stop record, and rejects an unknown sequence, an
  already-completed stop, and an unassigned driver (Requirements 6.1–6.9, 6.19,
  6.20, 6.21).

The service under test is the real ``PlanExecutionService`` over a fake ES, so
the assertions cover the whole handler → service → document path rather than a
mocked seam.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.13, 6.14, 6.15,
6.16, 6.17, 6.18, 6.19, 6.20, 6.21, 6.23
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.support.mvp_endpoints import configure_mvp_endpoints, router
from Agents.support.plan_execution_service import PlanExecutionService
from Agents.support.volume_units import LITERS_PER_US_GALLON
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

TEST_TENANT_ID = "tenant-1"
TEST_DRIVER_ID = "driver-1"
TEST_TRUCK_ID = "truck-1"
PLAN_ID = "plan-1"
ROUTE_ID = "route-1"
STATION_ID = "station-1"


# ---------------------------------------------------------------------------
# Fake Elasticsearch
# ---------------------------------------------------------------------------


class _FakeES:
    """Minimal in-memory ES honouring the ``must``/``term`` queries used here."""

    def __init__(self, docs=None):
        self.docs = {index: list(rows) for index, rows in (docs or {}).items()}
        self.by_id = {}
        self.updates = []
        self.indexed = []

    async def search_documents(self, index, query, size=10):
        must = query.get("query", {}).get("bool", {}).get("must", [])
        terms = {}
        for clause in must:
            for field, value in (clause.get("term") or {}).items():
                terms[field] = value
        hits = [
            {"_source": doc}
            for doc in self.docs.get(index, [])
            if all(doc.get(field) == value for field, value in terms.items())
        ]
        return {
            "hits": {"hits": hits[:size], "total": {"value": len(hits)}}
        }

    async def update_document(self, index, doc_id, fields):
        self.updates.append((index, doc_id, fields))
        for doc in self.docs.get(index, []):
            if doc_id in (doc.get("execution_id"), doc.get("plan_id")):
                doc.update(fields)
        return True

    async def index_document(self, index, doc_id, doc):
        self.indexed.append((index, doc_id, doc))
        self.docs.setdefault(index, []).append(doc)
        self.by_id[(index, doc_id)] = doc
        return True

    async def get_document(self, index, doc_id):
        return self.by_id.get((index, doc_id))


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _plan(status="dispatched", truck_id=TEST_TRUCK_ID):
    return {
        "plan_id": PLAN_ID,
        "truck_id": truck_id,
        "status": status,
        "tenant_id": TEST_TENANT_ID,
    }


def _execution(stops=None):
    return {
        "execution_id": "exec-1",
        "plan_id": PLAN_ID,
        "route_id": ROUTE_ID,
        "tenant_id": TEST_TENANT_ID,
        "stops": stops
        if stops is not None
        else [
            {
                "station_id": STATION_ID,
                "sequence": 0,
                "status": "pending",
                "planned_eta": "2024-01-01T08:00:00+00:00",
                "actual_arrival": None,
                # 1000 litres of PMS planned
                "planned_quantities": {"PMS": 1000.0},
                "actual_quantities": {},
                "actual_quantities_unit": "liter",
            },
            {
                "station_id": "station-2",
                "sequence": 1,
                "status": "pending",
                "planned_eta": "2024-01-01T09:00:00+00:00",
                "actual_arrival": None,
                "planned_quantities": {"PMS": 500.0},
                "actual_quantities": {},
                "actual_quantities_unit": "liter",
            },
        ],
        "completed_stops": 0,
        "total_stops": 2,
        "status": "in_progress",
        "created_at": "2024-01-01T07:00:00+00:00",
        "updated_at": "2024-01-01T07:00:00+00:00",
    }


def _driver(assigned_truck_id=TEST_TRUCK_ID):
    return {
        "driver_id": TEST_DRIVER_ID,
        "tenant_id": TEST_TENANT_ID,
        "assigned_truck_id": assigned_truck_id,
        "status": "active",
    }


def _make_es(*, plan=None, execution=None, driver=None, pods=()):
    return _FakeES(
        {
            "mvp_load_plans": [plan or _plan()],
            "mvp_plan_executions": [execution or _execution()],
            "drivers_current": [driver or _driver()],
            "proof_of_delivery": list(pods),
        }
    )


def _tenant_context(driver_id=TEST_DRIVER_ID):
    def _override() -> TenantContext:
        return TenantContext(
            tenant_id=TEST_TENANT_ID,
            user_id="user-1",
            has_pii_access=True,
            roles=["driver"],
            driver_id=driver_id,
        )

    return _override


def _make_app(es, *, driver_id=TEST_DRIVER_ID):
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = _tenant_context(driver_id)

    ws_manager = MagicMock()
    ws_manager.broadcast_execution_update = AsyncMock()

    configure_mvp_endpoints(
        pipeline=MagicMock(),
        es_service=es,
        plan_execution_service=PlanExecutionService(es),
        plan_execution_ws_manager=ws_manager,
    )
    return app, ws_manager


@pytest.fixture(autouse=True)
def _reset_idempotency_middleware():
    """Keep the module-level idempotency singleton out of other tests."""
    from driver.middleware import idempotency as idem

    original = idem._idempotency_middleware
    idem._idempotency_middleware = None
    yield
    idem._idempotency_middleware = original


#: Sentinel meaning "leave this key out of the request body entirely".
_OMIT = object()


def _body(**overrides):
    body = {
        "route_id": ROUTE_ID,
        "station_id": STATION_ID,
        "sequence": 0,
        "actual_quantities_gallons": {"PMS": 100.0},
        "quantity_unit": "us_gallon",
        "geotag": {"lat": 6.45, "lng": 3.39},
        "event_timestamp": "2024-01-01T08:05:00+00:00",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not _OMIT}


def _stop_record(es, sequence=0):
    execution = es.docs["mvp_plan_executions"][0]
    return next(
        stop for stop in execution["stops"] if stop["sequence"] == sequence
    )


# ---------------------------------------------------------------------------
# Request contract (Requirements 6.14–6.18)
# ---------------------------------------------------------------------------


class TestVolumeUnitRequestContract:
    def test_gallons_are_converted_once_into_litres(self):
        """100 US gal is persisted as 378.5411784 litres, converted once."""
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        )

        assert resp.status_code == 200, resp.text
        stored = _stop_record(es)["actual_quantities"]
        assert stored["PMS"] == pytest.approx(100.0 * LITERS_PER_US_GALLON)
        # A second conversion would land at 1432.6 litres.
        assert stored["PMS"] == pytest.approx(378.5411784)

    def test_response_is_gallons_only(self):
        """Gallons out, unit stated, no litre value echoed (R6.23)."""
        es = _make_es()
        app, _ = _make_app(es)

        data = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        ).json()

        assert data["quantity_unit"] == "us_gallon"
        assert data["actual_quantities_gallons"]["PMS"] == pytest.approx(100.0)
        assert data["planned_quantities_gallons"]["PMS"] == pytest.approx(
            1000.0 / LITERS_PER_US_GALLON
        )
        assert "actual_quantities" not in data
        assert "planned_quantities" not in data
        assert "quantity_variance" not in data

    def test_both_volume_fields_is_ambiguous(self):
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(actual_quantities={"PMS": 378.5}),
        )

        assert resp.status_code == 422
        assert resp.json()["error_code"] == "AMBIGUOUS_VOLUME_UNIT"

    def test_neither_volume_field_is_rejected(self):
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(actual_quantities_gallons=_OMIT, quantity_unit=_OMIT),
        )

        assert resp.status_code == 422
        assert resp.json()["error_code"] == "VOLUME_QUANTITIES_REQUIRED"

    def test_gallons_without_quantity_unit_is_rejected(self):
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(quantity_unit=_OMIT),
        )

        assert resp.status_code == 422
        assert resp.json()["error_code"] == "VALIDATION_ERROR"

    def test_quantity_unit_rejects_any_other_unit(self):
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(quantity_unit="liter"),
        )

        assert resp.status_code == 422

    def test_deprecated_litres_field_is_not_converted(self):
        """``actual_quantities`` keeps its litres meaning (R6.15)."""
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(
                actual_quantities={"PMS": 900.0},
                actual_quantities_gallons=_OMIT,
                quantity_unit=_OMIT,
            ),
        )

        assert resp.status_code == 200, resp.text
        assert _stop_record(es)["actual_quantities"]["PMS"] == pytest.approx(900.0)

    @pytest.mark.parametrize("field", ["geotag", "event_timestamp"])
    def test_driver_context_fields_are_required(self, field):
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body(**{field: _OMIT})
        )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Stop record persistence (Requirements 6.1–6.3, 6.8, 6.9, 6.20, 6.21)
# ---------------------------------------------------------------------------


class TestStopRecordPersistence:
    def test_records_driver_context_and_units(self):
        es = _make_es(
            pods=[
                {
                    "pod_id": "pod-9",
                    "order_id": "order-7",
                    "tenant_id": TEST_TENANT_ID,
                    "timestamp": "2024-01-01T08:04:00+00:00",
                }
            ]
        )
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(order_id="order-7"),
        )
        assert resp.status_code == 200, resp.text

        stop = _stop_record(es)
        assert stop["driver_id"] == TEST_DRIVER_ID
        assert stop["geotag"] == {"lat": 6.45, "lon": 3.39}
        assert stop["event_timestamp"] == "2024-01-01T08:05:00+00:00"
        assert stop["server_received_at"]
        assert stop["server_received_at"] != stop["event_timestamp"]
        assert stop["order_id"] == "order-7"
        assert stop["pod_id"] == "pod-9"
        assert stop["actual_quantities_unit"] == "liter"
        assert stop["variance_unit"] == "liter"

    def test_variance_is_per_grade_litres(self):
        """1000 planned litres less 378.54 delivered litres (R6.9, R6.20)."""
        es = _make_es()
        app, _ = _make_app(es)

        data = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        ).json()

        stop = _stop_record(es)
        assert stop["quantity_variance"]["PMS"] == pytest.approx(
            100.0 * LITERS_PER_US_GALLON - 1000.0
        )
        # The response carries the same variance expressed in gallons.
        assert data["variance_gallons"]["PMS"] == pytest.approx(
            100.0 - 1000.0 / LITERS_PER_US_GALLON
        )

    def test_variance_covers_a_grade_present_on_one_side_only(self):
        es = _make_es()
        app, _ = _make_app(es)

        TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin",
            json=_body(actual_quantities_gallons={"PMS": 100.0, "AGO": 10.0}),
        )

        variance = _stop_record(es)["quantity_variance"]
        assert variance["AGO"] == pytest.approx(10.0 * LITERS_PER_US_GALLON)

    def test_pod_id_is_null_without_an_order_reference(self):
        es = _make_es()
        app, _ = _make_app(es)

        TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        )

        stop = _stop_record(es)
        assert stop["order_id"] is None
        assert stop["pod_id"] is None

    def test_unknown_sequence_is_404(self):
        es = _make_es()
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body(sequence=99)
        )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    def test_already_completed_stop_is_409(self):
        execution = _execution()
        execution["stops"][0]["status"] = "completed"
        es = _make_es(execution=execution)
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        )

        assert resp.status_code == 409
        assert resp.json()["error_code"] == "STOP_ALREADY_COMPLETED"

    def test_driver_on_another_truck_is_403(self):
        es = _make_es(driver=_driver(assigned_truck_id="truck-other"))
        app, _ = _make_app(es)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"
        # The rejection happened before the stop was touched.
        assert _stop_record(es)["status"] == "pending"

    def test_driverless_caller_skips_the_assignment_check(self):
        """A dispatcher-originated check-in has no driver to compare (R6.7)."""
        es = _make_es(driver=_driver(assigned_truck_id="truck-other"))
        app, _ = _make_app(es, driver_id=None)

        resp = TestClient(app).post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body()
        )

        assert resp.status_code == 200, resp.text
        assert _stop_record(es)["driver_id"] is None


# ---------------------------------------------------------------------------
# Outcome document (Requirements 6.20, 6.21)
# ---------------------------------------------------------------------------


class TestOutcomeVarianceUnit:
    @pytest.mark.asyncio
    async def test_outcome_carries_liter_variance_unit(self):
        execution = _execution()
        execution["stops"][0].update(
            {
                "status": "completed",
                "actual_arrival": "2024-01-01T08:05:00+00:00",
                "actual_quantities": {"PMS": 900.0},
            }
        )
        # A pre-feature stop record: no ``actual_quantities_unit`` at all.
        execution["stops"][1].pop("actual_quantities_unit")
        execution["stops"][1].update(
            {
                "status": "completed",
                "actual_arrival": "2024-01-01T09:05:00+00:00",
                "actual_quantities": {"PMS": 500.0},
            }
        )
        es = _make_es(execution=execution)

        outcome = await PlanExecutionService(es).compute_outcomes(
            PLAN_ID, TEST_TENANT_ID
        )

        assert outcome["variance_unit"] == "liter"
        # R6.21 — a record with no discriminator is read as litres, not skipped.
        assert [sv["variance_unit"] for sv in outcome["stop_variances"]] == [
            "liter",
            "liter",
        ]


# ---------------------------------------------------------------------------
# Idempotency (Requirement 6.13)
# ---------------------------------------------------------------------------


class TestCheckinIdempotency:
    def test_repeated_key_replays_the_stored_response(self):
        from driver.middleware.idempotency import (
            configure_idempotency_middleware,
        )

        es = _make_es()
        app, ws_manager = _make_app(es)
        configure_idempotency_middleware(es_service=es)
        client = TestClient(app)

        headers = {"X-Idempotency-Key": "key-1"}
        first = client.post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body(), headers=headers
        )
        second = client.post(
            f"/api/fuel/mvp/plan/{PLAN_ID}/checkin", json=_body(), headers=headers
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200
        assert second.json() == first.json()
        assert second.headers["X-Idempotent-Replayed"] == "true"
        # The replay never re-ran the check-in.
        assert ws_manager.broadcast_execution_update.await_count == 1
