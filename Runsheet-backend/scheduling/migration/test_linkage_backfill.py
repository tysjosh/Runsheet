"""
Unit tests for the cross-module linkage backfill (cross-module-entity-linkage
task 5.1).

Covers the pure derivation logic (:func:`plan_order_backfill`) and the
orchestration (:class:`LinkageBackfill.run`):

* Derives order ``assigned_asset_id`` from the job and job ``order_id`` /
  ``customer_id`` from the order, written via the consistency-preserving
  assignment service (Req 6.4).
* Records that cannot be derived (no run link, dangling job, job without an
  asset, or a write-time reference rejection) remain "unlinked" — the run
  never fails on them (Req 6.2).
* Idempotent: an already-linked order/job pair is skipped with no write.
* Dry-run writes nothing.
* Tenant-scoping: every read/write is performed with the run's tenant_id.

Validates: Requirements 6.2, 6.4
"""

from datetime import datetime, timezone

import pytest

from errors.exceptions import resource_not_found, validation_error
from fuel.order_models import FuelOrder
from scheduling.migration.linkage_backfill import (
    ACTION_LINK,
    ACTION_SKIP,
    REASON_ALREADY_LINKED,
    REASON_JOB_MISSING_ASSET,
    REASON_JOB_NOT_FOUND,
    REASON_LINKABLE,
    REASON_NO_RUN_LINK,
    LinkageBackfill,
    plan_order_backfill,
)

TENANT = "tenant_1"


# --------------------------------------------------------------------------- #
# Builders / fakes
# --------------------------------------------------------------------------- #


def _make_order(
    *,
    order_id="ORD-1",
    tenant_id=TENANT,
    customer_id="CUST-1",
    assigned_run_id=None,
    assigned_asset_id=None,
    assigned_driver_id=None,
) -> FuelOrder:
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
        assigned_run_id=assigned_run_id,
        assigned_asset_id=assigned_asset_id,
        assigned_driver_id=assigned_driver_id,
        source_schema_version="legacy",
        trace_id="trace-1",
        created_at=now,
        updated_at=now,
        last_event_timestamp=now,
    )


def _job_doc(*, job_id="JOB_1", asset_assigned="TRUCK_01", order_id=None, customer_id=None):
    return {
        "job_id": job_id,
        "job_type": "cargo_transport",
        "status": "scheduled",
        "tenant_id": TENANT,
        "asset_assigned": asset_assigned,
        "order_id": order_id,
        "customer_id": customer_id,
    }


class FakeOrderRepo:
    def __init__(self, orders):
        self._orders = list(orders)
        self.list_calls = []

    async def list_for_tenant(self, tenant_id, *, size=500):
        self.list_calls.append((tenant_id, size))
        return [o for o in self._orders if o.tenant_id == tenant_id]


class FakeJobService:
    """Serves job docs by (job_id, tenant_id); raises 404 when absent."""

    def __init__(self, jobs):
        # jobs: dict job_id -> (tenant_id, job_doc)
        self._jobs = jobs
        self.lookups = []

    async def get_job_doc(self, job_id, tenant_id):
        self.lookups.append((job_id, tenant_id))
        entry = self._jobs.get(job_id)
        if entry is None or entry[0] != tenant_id:
            raise resource_not_found(f"Job '{job_id}' not found")
        return entry[1]


class FakeAssignmentService:
    """Records assign calls; optionally raises for configured order ids."""

    def __init__(self, *, reject=None):
        # reject: dict order_id -> AppException to raise
        self._reject = reject or {}
        self.calls = []

    async def assign_order_to_job(self, *, tenant_id, order_id, job_id):
        self.calls.append((tenant_id, order_id, job_id))
        if order_id in self._reject:
            raise self._reject[order_id]
        return {"job": {"job_id": job_id, "order_id": order_id}, "order": {}}


# --------------------------------------------------------------------------- #
# Pure plan logic
# --------------------------------------------------------------------------- #


def test_plan_no_run_link_skips():
    plan = plan_order_backfill(_make_order(assigned_run_id=None), None)
    assert plan.action == ACTION_SKIP
    assert plan.reason == REASON_NO_RUN_LINK


def test_plan_job_not_found_stays_unlinked():
    order = _make_order(assigned_run_id="JOB_9")
    plan = plan_order_backfill(order, None)
    assert plan.action == ACTION_SKIP
    assert plan.reason == REASON_JOB_NOT_FOUND
    assert plan.job_id == "JOB_9"


def test_plan_job_missing_asset_stays_unlinked():
    order = _make_order(assigned_run_id="JOB_1")
    plan = plan_order_backfill(order, _job_doc(asset_assigned=None))
    assert plan.action == ACTION_SKIP
    assert plan.reason == REASON_JOB_MISSING_ASSET


def test_plan_derivable_link():
    order = _make_order(assigned_run_id="JOB_1", customer_id="CUST-1")
    plan = plan_order_backfill(order, _job_doc(asset_assigned="TRUCK_01"))
    assert plan.action == ACTION_LINK
    assert plan.reason == REASON_LINKABLE
    assert plan.asset_id == "TRUCK_01"
    assert plan.customer_id == "CUST-1"
    assert plan.job_id == "JOB_1"


def test_plan_already_linked_is_idempotent_skip():
    order = _make_order(
        order_id="ORD-1",
        assigned_run_id="JOB_1",
        assigned_asset_id="TRUCK_01",
        customer_id="CUST-1",
    )
    job = _job_doc(asset_assigned="TRUCK_01", order_id="ORD-1", customer_id="CUST-1")
    plan = plan_order_backfill(order, job)
    assert plan.action == ACTION_SKIP
    assert plan.reason == REASON_ALREADY_LINKED


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_links_derivable_records():
    order = _make_order(order_id="ORD-1", assigned_run_id="JOB_1", customer_id="CUST-1")
    repo = FakeOrderRepo([order])
    jobs = FakeJobService({"JOB_1": (TENANT, _job_doc(job_id="JOB_1"))})
    assign = FakeAssignmentService()

    backfill = LinkageBackfill(
        order_repository=repo, job_service=jobs, assignment_service=assign
    )
    report = await backfill.run(TENANT)

    assert report["status"] == "success"
    assert report["scanned"] == 1
    assert report["linked"] == 1
    assert report["unlinked"] == 0
    # The link was written through the consistency-preserving service,
    # tenant-scoped.
    assert assign.calls == [(TENANT, "ORD-1", "JOB_1")]


@pytest.mark.asyncio
async def test_run_dry_run_writes_nothing():
    order = _make_order(order_id="ORD-1", assigned_run_id="JOB_1")
    repo = FakeOrderRepo([order])
    jobs = FakeJobService({"JOB_1": (TENANT, _job_doc(job_id="JOB_1"))})
    assign = FakeAssignmentService()

    backfill = LinkageBackfill(
        order_repository=repo, job_service=jobs, assignment_service=assign
    )
    report = await backfill.run(TENANT, dry_run=True)

    assert report["linked"] == 1
    assert assign.calls == []  # nothing written


@pytest.mark.asyncio
async def test_run_dangling_run_stays_unlinked_without_failing():
    # Order references a run that no longer resolves to a job for this tenant.
    order = _make_order(order_id="ORD-1", assigned_run_id="JOB_GONE")
    repo = FakeOrderRepo([order])
    jobs = FakeJobService({})  # no jobs
    assign = FakeAssignmentService()

    backfill = LinkageBackfill(
        order_repository=repo, job_service=jobs, assignment_service=assign
    )
    report = await backfill.run(TENANT)

    assert report["status"] == "success"
    assert report["unlinked"] == 1
    assert report["reasons"].get(REASON_JOB_NOT_FOUND) == 1
    assert assign.calls == []


@pytest.mark.asyncio
async def test_run_reference_rejection_stays_unlinked():
    # Plan is derivable, but the assignment service rejects the asset (e.g.
    # retired). The order remains unlinked; the run does not fail (Req 6.2).
    order = _make_order(order_id="ORD-1", assigned_run_id="JOB_1")
    repo = FakeOrderRepo([order])
    jobs = FakeJobService({"JOB_1": (TENANT, _job_doc(job_id="JOB_1"))})
    assign = FakeAssignmentService(
        reject={
            "ORD-1": validation_error(
                "asset gone", details={"reason": "asset_not_found", "id": "TRUCK_01"}
            )
        }
    )

    backfill = LinkageBackfill(
        order_repository=repo, job_service=jobs, assignment_service=assign
    )
    report = await backfill.run(TENANT)

    assert report["status"] == "success"
    assert report["linked"] == 0
    assert report["unlinked"] == 1
    assert report["reasons"].get("asset_not_found") == 1
    # The optimistic linkable bump was reverted.
    assert report["reasons"].get(REASON_LINKABLE, 0) == 0


@pytest.mark.asyncio
async def test_run_already_linked_is_skipped_idempotently():
    order = _make_order(
        order_id="ORD-1",
        assigned_run_id="JOB_1",
        assigned_asset_id="TRUCK_01",
        customer_id="CUST-1",
    )
    repo = FakeOrderRepo([order])
    jobs = FakeJobService(
        {
            "JOB_1": (
                TENANT,
                _job_doc(job_id="JOB_1", order_id="ORD-1", customer_id="CUST-1"),
            )
        }
    )
    assign = FakeAssignmentService()

    backfill = LinkageBackfill(
        order_repository=repo, job_service=jobs, assignment_service=assign
    )
    report = await backfill.run(TENANT)

    assert report["skipped"] == 1
    assert report["linked"] == 0
    assert assign.calls == []  # idempotent — no rewrite


@pytest.mark.asyncio
async def test_run_no_run_link_is_skipped():
    order = _make_order(order_id="ORD-1", assigned_run_id=None)
    repo = FakeOrderRepo([order])
    jobs = FakeJobService({})
    assign = FakeAssignmentService()

    backfill = LinkageBackfill(
        order_repository=repo, job_service=jobs, assignment_service=assign
    )
    report = await backfill.run(TENANT)

    assert report["skipped"] == 1
    assert report["reasons"].get(REASON_NO_RUN_LINK) == 1
    assert jobs.lookups == []  # no run id -> no job lookup
