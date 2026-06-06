"""
Unit tests for the consistency-preserving order→job assignment service
(cross-module-entity-linkage task 3.1).

Covers:
* Both sides written consistently — order.assigned_asset_id == job.asset and
  job.order_id == order.id (linkage symmetry, Property 1 / Req 2.2, 3.2, 3.4).
* Write-time reference validation rejects invalid asset/driver/order references
  with validation_error (HTTP 400) carrying details.reason (Req 2.3, 3.3).
* Tenant containment: a cross-tenant asset id is rejected, never linked
  (Req 5.3).
* Partial-write protection: when the order write fails the job-side link is
  rolled back so the references never diverge (Req 3.4).

Validates: Requirements 2.2, 3.2, 3.3, 3.4
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from errors.codes import ErrorCode
from errors.exceptions import AppException
from fuel.order_models import FuelOrder
from scheduling.services.job_service import JobService
from scheduling.services.order_job_assignment_service import (
    OrderJobAssignmentService,
)
from services.ref_resolver import RefResolver

TENANT = "tenant_1"
OTHER_TENANT = "tenant_2"


@pytest.fixture(autouse=True)
def _force_es_read_path(monkeypatch):
    """Pin job reads/writes to the (mocked) ES path in unit tests.

    The test environment enables read-cutover (reads served from Postgres) and
    the Postgres dual-write. Neither is available here, so force the JobService
    to read from the mocked ES and make the current-state mirror a no-op. This
    keeps these tests focused on the assignment orchestration logic.
    """
    import commerce.services.commerce_persistence_bridge as bridge

    async def _not_cut_over(*_a, **_k):
        return bridge._NOT_CUT_OVER

    async def _noop_mirror(*_a, **_k):
        return None

    monkeypatch.setattr(bridge, "read_hybrid_get", _not_cut_over)
    monkeypatch.setattr(bridge, "mirror_current_state_upsert", _noop_mirror)


# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #


def _make_order(
    *,
    order_id: str = "ORD-1",
    tenant_id: str = TENANT,
    customer_id: str = "CUST-1",
    assigned_driver_id=None,
) -> FuelOrder:
    """Build a minimal valid FuelOrder (legacy channel relaxes product/window)."""
    now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)
    return FuelOrder(
        order_id=order_id,
        tenant_id=tenant_id,
        customer_id=customer_id,
        customer_name="Acme Fuel",
        ship_to_address="123 Main St, Houston TX",
        ship_to_lat=29.76,
        ship_to_lon=-95.36,
        call_type="will_call",
        intake_channel="legacy",
        intake_channel_id="legacy-1",
        assigned_driver_id=assigned_driver_id,
        source_schema_version="legacy",
        trace_id="trace-1",
        created_at=now,
        updated_at=now,
        last_event_timestamp=now,
    )


def _make_job_doc(
    *,
    job_id: str = "JOB_1",
    job_type: str = "cargo_transport",
    asset_assigned="TRUCK_01",
    order_id=None,
    customer_id=None,
    driver_id=None,
) -> dict:
    return {
        "job_id": job_id,
        "job_type": job_type,
        "status": "scheduled",
        "tenant_id": TENANT,
        "asset_assigned": asset_assigned,
        "order_id": order_id,
        "customer_id": customer_id,
        "driver_id": driver_id,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-05-12T10:00:00+00:00",
        "created_at": "2026-05-11T08:00:00+00:00",
        "updated_at": "2026-05-11T08:00:00+00:00",
        "priority": "normal",
        "delayed": False,
        "cargo_manifest": None,
    }


def _es_search_response(hits, total=None):
    if total is None:
        total = len(hits)
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": total},
        }
    }


def _make_job_service(job_doc: dict) -> JobService:
    """Real JobService with a mocked ES that serves ``job_doc`` for lookups."""
    es = MagicMock()
    es.search_documents = AsyncMock(return_value=_es_search_response([job_doc]))
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return JobService(es, redis_url=None)


class FakeOrderRepo:
    """Records upserts; can be made to fail to exercise the rollback path."""

    def __init__(self, order, *, fail_upsert: bool = False):
        self._order = order
        self.fail_upsert = fail_upsert
        self.upserts = []

    async def get(self, tenant_id, order_id):
        if (
            self._order is not None
            and self._order.order_id == order_id
            and self._order.tenant_id == tenant_id
        ):
            return self._order
        return None

    async def upsert_with_last_event_timestamp(self, tenant_id, order):
        if self.fail_upsert:
            raise RuntimeError("simulated order-store write failure")
        self.upserts.append(order)
        self._order = order
        return True


def _make_resolver(*, assets=None, drivers=None) -> RefResolver:
    """RefResolver with tenant-scoped fake asset/driver loaders.

    ``assets`` maps asset_id -> (tenant_id, asset_type).
    ``drivers`` maps driver_id -> tenant_id.
    """
    assets = assets or {}
    drivers = drivers or {}
    resolver = RefResolver()

    async def asset_loader(tenant_id, asset_id):
        entry = assets.get(asset_id)
        if entry is None:
            return None
        owner, asset_type = entry
        if owner != tenant_id:  # tenant-scoped: cross-tenant -> None
            return None
        return {"asset_type": asset_type}

    async def driver_loader(tenant_id, driver_id):
        owner = drivers.get(driver_id)
        if owner is None or owner != tenant_id:
            return None
        return {"display_name": driver_id, "status": "active"}

    resolver.register("asset", asset_loader)
    resolver.register("driver", driver_loader)
    return resolver


# --------------------------------------------------------------------------- #
# Happy path — linkage symmetry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_assignment_sets_both_sides_consistently():
    job_doc = _make_job_doc(asset_assigned="TRUCK_01")
    job_service = _make_job_service(job_doc)
    order = _make_order(assigned_driver_id=None)
    order_repo = FakeOrderRepo(order)
    resolver = _make_resolver(
        assets={"TRUCK_01": (TENANT, "vehicle")},
        drivers={"DRV-1": TENANT},
    )
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=resolver,
    )

    result = await svc.assign_order_to_job(
        tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1", driver_id="DRV-1"
    )

    linked_job = result["job"]
    linked_order = result["order"]

    # Job side carries the order/customer/driver references (Req 3.1, 3.2)
    assert linked_job.order_id == "ORD-1"
    assert linked_job.customer_id == "CUST-1"
    assert linked_job.driver_id == "DRV-1"

    # Order side carries the asset/driver references (Req 2.2)
    assert linked_order.assigned_asset_id == "TRUCK_01"
    assert linked_order.assigned_driver_id == "DRV-1"

    # Linkage symmetry (Property 1 / Req 3.4): order.asset == job.asset and
    # job.order_id == order.id
    assert linked_order.assigned_asset_id == linked_job.asset_assigned
    assert linked_job.order_id == linked_order.order_id
    assert len(order_repo.upserts) == 1


@pytest.mark.asyncio
async def test_driver_defaults_to_job_driver_when_not_explicit():
    job_doc = _make_job_doc(asset_assigned="TRUCK_01", driver_id="DRV-9")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    resolver = _make_resolver(
        assets={"TRUCK_01": (TENANT, "vehicle")}, drivers={"DRV-9": TENANT}
    )
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=resolver,
    )

    result = await svc.assign_order_to_job(
        tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1"
    )

    assert result["job"].driver_id == "DRV-9"
    assert result["order"].assigned_driver_id == "DRV-9"


@pytest.mark.asyncio
async def test_null_driver_is_allowed_asset_only_link():
    job_doc = _make_job_doc(asset_assigned="TRUCK_01")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    resolver = _make_resolver(assets={"TRUCK_01": (TENANT, "vehicle")})
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=resolver,
    )

    result = await svc.assign_order_to_job(
        tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1"
    )

    assert result["job"].driver_id is None
    assert result["order"].assigned_asset_id == "TRUCK_01"
    assert result["order"].assigned_driver_id is None


# --------------------------------------------------------------------------- #
# Write-time validation rejections (Req 2.3, 3.3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_order_not_found_rejected():
    job_service = _make_job_service(_make_job_doc())
    order_repo = FakeOrderRepo(None)
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(assets={"TRUCK_01": (TENANT, "vehicle")}),
    )

    with pytest.raises(AppException) as exc:
        await svc.assign_order_to_job(
            tenant_id=TENANT, order_id="MISSING", job_id="JOB_1"
        )

    assert exc.value.error_code == ErrorCode.VALIDATION_ERROR
    assert exc.value.status_code == 400
    assert exc.value.details["reason"] == "order_not_found"
    assert order_repo.upserts == []


@pytest.mark.asyncio
async def test_asset_not_found_rejected_and_no_writes():
    job_doc = _make_job_doc(asset_assigned="GHOST")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(assets={"TRUCK_01": (TENANT, "vehicle")}),
    )

    with pytest.raises(AppException) as exc:
        await svc.assign_order_to_job(
            tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1"
        )

    assert exc.value.error_code == ErrorCode.VALIDATION_ERROR
    assert exc.value.details["reason"] == "asset_not_found"
    # No side effects: neither the job nor the order were written.
    assert order_repo.upserts == []
    job_service._es.update_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_asset_type_incompatible_rejected():
    # cargo_transport requires a "vehicle"; assign a "vessel".
    job_doc = _make_job_doc(job_type="cargo_transport", asset_assigned="BARGE_1")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(assets={"BARGE_1": (TENANT, "vessel")}),
    )

    with pytest.raises(AppException) as exc:
        await svc.assign_order_to_job(
            tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1"
        )

    assert exc.value.details["reason"] == "asset_type_incompatible"
    assert order_repo.upserts == []


@pytest.mark.asyncio
async def test_asset_required_when_job_has_no_asset():
    job_doc = _make_job_doc(asset_assigned=None)
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(),
    )

    with pytest.raises(AppException) as exc:
        await svc.assign_order_to_job(
            tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1"
        )

    assert exc.value.details["reason"] == "asset_required"
    assert order_repo.upserts == []


@pytest.mark.asyncio
async def test_driver_not_found_rejected():
    job_doc = _make_job_doc(asset_assigned="TRUCK_01")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(
            assets={"TRUCK_01": (TENANT, "vehicle")}, drivers={}
        ),
    )

    with pytest.raises(AppException) as exc:
        await svc.assign_order_to_job(
            tenant_id=TENANT,
            order_id="ORD-1",
            job_id="JOB_1",
            driver_id="GHOST_DRIVER",
        )

    assert exc.value.details["reason"] == "driver_not_found"
    assert order_repo.upserts == []
    job_service._es.update_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_tenant_asset_rejected_never_linked():
    # The asset exists, but belongs to another tenant — must not link (Req 5.3).
    job_doc = _make_job_doc(asset_assigned="TRUCK_X")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order())
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(
            assets={"TRUCK_X": (OTHER_TENANT, "vehicle")}
        ),
    )

    with pytest.raises(AppException) as exc:
        await svc.assign_order_to_job(
            tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1"
        )

    assert exc.value.details["reason"] == "asset_not_found"
    assert order_repo.upserts == []


# --------------------------------------------------------------------------- #
# Partial-write protection (Req 3.4)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_order_write_failure_rolls_back_job_links():
    job_doc = _make_job_doc(asset_assigned="TRUCK_01")
    job_service = _make_job_service(job_doc)
    order_repo = FakeOrderRepo(_make_order(), fail_upsert=True)
    svc = OrderJobAssignmentService(
        job_service=job_service,
        order_repository=order_repo,
        ref_resolver=_make_resolver(
            assets={"TRUCK_01": (TENANT, "vehicle")}, drivers={"DRV-1": TENANT}
        ),
    )

    with pytest.raises(RuntimeError, match="simulated order-store write failure"):
        await svc.assign_order_to_job(
            tenant_id=TENANT, order_id="ORD-1", job_id="JOB_1", driver_id="DRV-1"
        )

    # The job side was written once (apply) then once more (rollback). The
    # final write must restore the prior (null) references so the two sides
    # cannot diverge.
    assert job_service._es.update_document.await_count == 2
    last_call = job_service._es.update_document.await_args_list[-1]
    rolled_back_fields = last_call.args[2]
    assert rolled_back_fields["order_id"] is None
    assert rolled_back_fields["customer_id"] is None
    assert rolled_back_fields["driver_id"] is None
