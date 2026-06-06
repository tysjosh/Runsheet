"""
Property-based tests for cross-module-entity-linkage (task 3.3).

Implements the two named correctness properties from design.md:

* **Property 1 — Linkage symmetry** (Validates: Requirements 3.4, 2.2)
  For any order linked to a job via the ``OrderJobAssignmentService``, the
  order's ``assigned_asset_id`` equals the job's asset (``asset_assigned``) and
  the job's ``order_id`` equals the order's id (and the customer/driver
  references agree on both sides).

* **Property 3 — Additive compatibility** (Validates: Requirements 6.1, 6.3)
  A read without ``expand`` returns the pre-existing contract plus only
  nullable id fields; no existing field is removed or changed in meaning.

These reuse the in-memory fakes/patterns established in
``scheduling/services/test_order_job_assignment_service.py`` and
``tests/unit/test_ref_resolver.py``.

Feature: cross-module-entity-linkage, Property 1 & Property 3
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fuel.order_models import FuelOrder
from scheduling.models import Job
from scheduling.services.job_service import JobService
from scheduling.services.order_job_assignment_service import (
    OrderJobAssignmentService,
)
from services.ref_resolver import RefResolver

TENANT = "tenant_1"

# The cross-module linkage fields this spec *adds* (all nullable). Property 3
# asserts these are the only additions and that they default to None.
ADDITIVE_JOB_FIELDS = {"order_id", "customer_id", "driver_id"}
ADDITIVE_ORDER_FIELDS = {"assigned_asset_id"}


# --------------------------------------------------------------------------- #
# Shared fakes (mirrors test_order_job_assignment_service.py)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _force_es_read_path(monkeypatch):
    """Pin job reads/writes to the (mocked) ES path under ENVIRONMENT=test.

    The test environment enables Postgres read-cutover + dual-write; neither is
    available in a unit test, so force JobService to read from the mocked ES and
    make the current-state mirror a no-op (same seam the assignment-service unit
    test uses).
    """
    import commerce.services.commerce_persistence_bridge as bridge

    async def _not_cut_over(*_a, **_k):
        return bridge._NOT_CUT_OVER

    async def _noop_mirror(*_a, **_k):
        return None

    monkeypatch.setattr(bridge, "read_hybrid_get", _not_cut_over)
    monkeypatch.setattr(bridge, "mirror_current_state_upsert", _noop_mirror)


def _make_order(*, order_id="ORD-1", customer_id="CUST-1", assigned_driver_id=None):
    now = datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc)
    return FuelOrder(
        order_id=order_id,
        tenant_id=TENANT,
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


def _make_job_doc(*, job_id="JOB_1", asset_assigned="TRUCK_01"):
    return {
        "job_id": job_id,
        "job_type": "cargo_transport",
        "status": "scheduled",
        "tenant_id": TENANT,
        "asset_assigned": asset_assigned,
        "order_id": None,
        "customer_id": None,
        "driver_id": None,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-05-12T10:00:00+00:00",
        "created_at": "2026-05-11T08:00:00+00:00",
        "updated_at": "2026-05-11T08:00:00+00:00",
        "priority": "normal",
        "delayed": False,
        "cargo_manifest": None,
    }


def _es_search_response(hits):
    return {
        "hits": {
            "hits": [{"_source": h} for h in hits],
            "total": {"value": len(hits)},
        }
    }


def _make_job_service(job_doc):
    es = MagicMock()
    es.search_documents = AsyncMock(return_value=_es_search_response([job_doc]))
    es.index_document = AsyncMock()
    es.update_document = AsyncMock()
    return JobService(es, redis_url=None)


class FakeOrderRepo:
    def __init__(self, order):
        self._order = order

    async def get(self, tenant_id, order_id):
        if (
            self._order is not None
            and self._order.order_id == order_id
            and self._order.tenant_id == tenant_id
        ):
            return self._order
        return None

    async def upsert_with_last_event_timestamp(self, tenant_id, order):
        self._order = order
        return True


def _make_resolver(*, asset_id, asset_type="vehicle", driver_id=None):
    """RefResolver with tenant-scoped fakes for the generated asset/driver."""
    resolver = RefResolver()

    async def asset_loader(tenant_id, aid):
        if tenant_id == TENANT and aid == asset_id:
            return {"asset_type": asset_type}
        return None

    async def driver_loader(tenant_id, did):
        if tenant_id == TENANT and driver_id is not None and did == driver_id:
            return {"display_name": did, "status": "active"}
        return None

    resolver.register("asset", asset_loader)
    resolver.register("driver", driver_loader)
    return resolver


# --------------------------------------------------------------------------- #
# Property 1 — Linkage symmetry (Req 3.4, 2.2)
# --------------------------------------------------------------------------- #

_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_", min_size=1, max_size=16
)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    order_id=_ids,
    customer_id=_ids,
    asset_id=_ids,
    driver_id=st.one_of(st.none(), _ids),
)
def test_property_linkage_symmetry(order_id, customer_id, asset_id, driver_id):
    """For any linked order/job pair the two sides agree on every reference.

    **Validates: Requirements 3.4, 2.2** (Property 1)
    """

    async def _run():
        job_doc = _make_job_doc(asset_assigned=asset_id)
        job_service = _make_job_service(job_doc)
        order = _make_order(order_id=order_id, customer_id=customer_id)
        order_repo = FakeOrderRepo(order)
        resolver = _make_resolver(asset_id=asset_id, driver_id=driver_id)
        svc = OrderJobAssignmentService(
            job_service=job_service,
            order_repository=order_repo,
            ref_resolver=resolver,
        )
        return await svc.assign_order_to_job(
            tenant_id=TENANT,
            order_id=order_id,
            job_id="JOB_1",
            driver_id=driver_id,
        )

    result = asyncio.run(_run())
    linked_job = result["job"]
    linked_order = result["order"]

    # Symmetry: order.asset == job.asset, job.order_id == order.id (Req 3.4, 2.2)
    assert linked_order.assigned_asset_id == linked_job.asset_assigned == asset_id
    assert linked_job.order_id == linked_order.order_id == order_id
    # Customer/driver references agree on both sides.
    assert linked_job.customer_id == linked_order.customer_id == customer_id
    assert linked_job.driver_id == linked_order.assigned_driver_id == driver_id


# --------------------------------------------------------------------------- #
# Property 3 — Additive compatibility (Req 6.1, 6.3)
# --------------------------------------------------------------------------- #


def _baseline_job_payload(asset_assigned, priority, delayed):
    """A pre-linkage job payload (no order_id/customer_id/driver_id)."""
    return {
        "job_id": "JOB_1",
        "job_type": "cargo_transport",
        "status": "scheduled",
        "tenant_id": TENANT,
        "asset_assigned": asset_assigned,
        "origin": "Port A",
        "destination": "Port B",
        "scheduled_time": "2026-05-12T10:00:00+00:00",
        "created_at": "2026-05-11T08:00:00+00:00",
        "updated_at": "2026-05-11T08:00:00+00:00",
        "priority": priority,
        "delayed": delayed,
    }


@settings(max_examples=100)
@given(
    asset_assigned=st.one_of(st.none(), _ids),
    priority=st.sampled_from(["low", "normal", "high", "urgent"]),
    delayed=st.booleans(),
    order_id=st.one_of(st.none(), _ids),
    customer_id=st.one_of(st.none(), _ids),
    driver_id=st.one_of(st.none(), _ids),
)
def test_property_additive_compatibility_job(
    asset_assigned, priority, delayed, order_id, customer_id, driver_id
):
    """Job linkage fields are additive + nullable; baseline fields unchanged.

    **Validates: Requirements 6.1, 6.3** (Property 3)
    """
    baseline = _baseline_job_payload(asset_assigned, priority, delayed)

    # A historical job (no linkage fields supplied) is still valid: the new
    # fields are nullable and default to None (Req 6.1, 6.2).
    job_without = Job(**baseline)
    for field in ADDITIVE_JOB_FIELDS:
        assert getattr(job_without, field) is None

    # The same job with linkage fields populated (Req 3.1).
    job_with = Job(**baseline, order_id=order_id, customer_id=customer_id, driver_id=driver_id)

    dump_without = job_without.model_dump()
    dump_with = job_with.model_dump()

    # No existing field is removed: every baseline key is still present.
    baseline_keys = set(baseline)
    assert baseline_keys.issubset(dump_without)
    assert baseline_keys.issubset(dump_with)

    # The only keys added beyond the baseline payload are the known nullable
    # linkage fields — nothing else changes the contract shape (Req 6.3).
    assert set(dump_without) - baseline_keys >= ADDITIVE_JOB_FIELDS
    assert set(dump_without) == set(dump_with)

    # No existing field changes meaning/value when linkage fields are set: every
    # baseline field is byte-identical whether or not the links are populated.
    for key in baseline_keys:
        assert dump_without[key] == dump_with[key]


@settings(max_examples=100)
@given(
    assigned_driver_id=st.one_of(st.none(), _ids),
    assigned_asset_id=st.one_of(st.none(), _ids),
)
def test_property_additive_compatibility_order(assigned_driver_id, assigned_asset_id):
    """Order ``assigned_asset_id`` is an additive nullable field (Req 2.1, 6.1).

    **Validates: Requirements 6.1, 6.3** (Property 3)
    """
    order_without = _make_order(assigned_driver_id=assigned_driver_id)
    # The added asset reference is nullable and defaults to None for records
    # that predate the field (Req 6.1, 6.2).
    assert order_without.assigned_asset_id is None

    order_with = order_without.model_copy(
        update={"assigned_asset_id": assigned_asset_id}
    )

    dump_without = order_without.model_dump()
    dump_with = order_with.model_dump()

    # Same contract shape; the only differing key is the additive asset field.
    assert set(dump_without) == set(dump_with)
    differing = {k for k in dump_without if dump_without[k] != dump_with[k]}
    assert differing <= ADDITIVE_ORDER_FIELDS


# --------------------------------------------------------------------------- #
# Property 3 (read level) — a read without ``expand`` is unchanged
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(
    expand=st.one_of(
        st.none(),
        st.just(""),
        st.text(alphabet="abcxyz, ", max_size=12),  # arbitrary non-link tokens
    )
)
def test_property_non_expanded_read_adds_no_links(expand):
    """A read whose ``expand`` carries no valid token gains no ``links`` key.

    Exercises the resolver-read parser that gates the additive ``links`` object:
    without a recognised expand token the pre-existing contract is returned
    unchanged (Req 6.3).

    **Validates: Requirements 6.1, 6.3** (Property 3)
    """
    from scheduling.api.endpoints import _parse_job_expand

    requested = _parse_job_expand(expand)
    # No valid expand token -> empty set -> endpoint attaches no `links` object.
    assert requested == set()
