"""
Unit tests for the ``Work_Ref`` seam (`driver/services/work_ref.py`).

Covers the :func:`authorizes_job` disjunction, the ``WorkRef`` identifier
properties, and the ``resolve_job`` / ``resolve_order`` rejection paths.

Validates: Requirements 1.5, 1.6, 1.12, 7.21, 15.11, 15.14
"""

import pytest

from driver.services.work_ref import WorkRef, WorkRefResolver, authorizes_job
from errors.codes import ErrorCode
from errors.exceptions import AppException, resource_not_found
from ops.middleware.tenant_guard import TenantContext

DRIVER_USER = "st-user-1"
DRIVER_ID = "drv-1"


def _tenant(
    *,
    user_id: str = DRIVER_USER,
    driver_id: str | None = DRIVER_ID,
    roles: list[str] | None = None,
) -> TenantContext:
    return TenantContext(
        tenant_id="t1",
        user_id=user_id,
        has_pii_access=False,
        roles=["driver"] if roles is None else roles,
        driver_id=driver_id,
    )


class _FakeJobService:
    def __init__(self, doc=None, error: Exception | None = None):
        self._doc = doc
        self._error = error

    async def _get_job_doc(self, job_id: str, tenant_id: str) -> dict:
        if self._error is not None:
            raise self._error
        return self._doc


class _FakeOrderRepository:
    def __init__(self, doc=None):
        self._doc = doc

    async def get(self, tenant_id: str, order_id: str):
        return self._doc


# ---------------------------------------------------------------------------
# authorizes_job — the truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_doc,expected",
    [
        # unassigned in both namespaces -> permissive allow
        ({}, True),
        ({"asset_assigned": "", "assigned_driver_id": None}, True),
        # legacy namespace match
        ({"asset_assigned": DRIVER_USER}, True),
        # canonical namespace match
        ({"assigned_driver_id": DRIVER_ID}, True),
        # dual acceptance: legacy field names someone else, canonical matches
        ({"asset_assigned": "other-user", "assigned_driver_id": DRIVER_ID}, True),
        # denials
        ({"asset_assigned": "other-user"}, False),
        ({"assigned_driver_id": "drv-other"}, False),
        (
            {"asset_assigned": "other-user", "assigned_driver_id": "drv-other"},
            False,
        ),
    ],
)
def test_authorizes_job_truth_table(job_doc, expected):
    assert authorizes_job(job_doc, _tenant()) is expected


def test_authorizes_job_null_driver_id_does_not_match_unassigned_field():
    """A context with no driver_id cannot match on the canonical namespace."""
    tenant = _tenant(driver_id=None)
    assert authorizes_job({"assigned_driver_id": "drv-1"}, tenant) is False


# ---------------------------------------------------------------------------
# WorkRef identifier properties
# ---------------------------------------------------------------------------


def test_work_ref_identifier_properties():
    job_ref = WorkRef("t1", DRIVER_ID, "job", "job-1", job_doc={"order_id": "ord-9"})
    assert job_ref.job_id == "job-1"
    assert job_ref.order_id == "ord-9"

    order_ref = WorkRef("t1", DRIVER_ID, "order", "ord-1")
    assert order_ref.job_id is None
    assert order_ref.order_id == "ord-1"

    bare_job_ref = WorkRef("t1", DRIVER_ID, "job", "job-2")
    assert bare_job_ref.order_id is None


# ---------------------------------------------------------------------------
# resolve_job
# ---------------------------------------------------------------------------


async def test_resolve_job_requires_driver_role():
    resolver = WorkRefResolver()
    with pytest.raises(AppException) as exc:
        await resolver.resolve_job("job-1", _tenant(roles=["dispatcher"]))
    assert exc.value.error_code == ErrorCode.INSUFFICIENT_ROLE


async def test_resolve_job_requires_driver_identity():
    resolver = WorkRefResolver()
    with pytest.raises(AppException) as exc:
        await resolver.resolve_job("job-1", _tenant(driver_id=None))
    assert exc.value.error_code == ErrorCode.DRIVER_IDENTITY_MISSING


async def test_resolve_job_forbids_other_drivers_job_without_leaking_identity():
    resolver = WorkRefResolver(
        job_service=_FakeJobService({"asset_assigned": "other-user"})
    )
    with pytest.raises(AppException) as exc:
        await resolver.resolve_job("job-1", _tenant())
    assert exc.value.error_code == ErrorCode.FORBIDDEN
    assert exc.value.details == {"job_id": "job-1"}


async def test_resolve_job_propagates_not_found():
    resolver = WorkRefResolver(
        job_service=_FakeJobService(
            error=resource_not_found("Job 'job-1' not found", {"job_id": "job-1"})
        )
    )
    with pytest.raises(AppException) as exc:
        await resolver.resolve_job("job-1", _tenant())
    assert exc.value.error_code == ErrorCode.RESOURCE_NOT_FOUND


async def test_resolve_job_stays_non_blocking_on_lookup_failure():
    resolver = WorkRefResolver(
        job_service=_FakeJobService(error=RuntimeError("es down"))
    )
    ref = await resolver.resolve_job("job-1", _tenant())
    assert ref.job_doc is None
    assert (ref.kind, ref.work_id, ref.driver_id) == ("job", "job-1", DRIVER_ID)


async def test_resolve_job_without_job_service_resolves():
    ref = await WorkRefResolver().resolve_job("job-1", _tenant())
    assert ref == WorkRef("t1", DRIVER_ID, "job", "job-1")


# ---------------------------------------------------------------------------
# resolve_order
# ---------------------------------------------------------------------------


async def test_resolve_order_returns_ref_for_assigned_driver():
    doc = {
        "order_id": "ord-1",
        "tenant_id": "t1",
        "assigned_driver_id": DRIVER_ID,
    }
    resolver = WorkRefResolver(order_repository=_FakeOrderRepository(doc))
    ref = await resolver.resolve_order("ord-1", _tenant())
    assert ref.kind == "order"
    assert ref.order_id == "ord-1"
    assert ref.job_id is None
    assert ref.order_doc == doc


async def test_resolve_order_missing_order_is_404():
    resolver = WorkRefResolver(order_repository=_FakeOrderRepository(None))
    with pytest.raises(AppException) as exc:
        await resolver.resolve_order("ord-1", _tenant())
    assert exc.value.error_code == ErrorCode.RESOURCE_NOT_FOUND
    assert exc.value.details == {"order_id": "ord-1"}


async def test_resolve_order_cross_tenant_document_is_404():
    doc = {"order_id": "ord-1", "tenant_id": "t2", "assigned_driver_id": DRIVER_ID}
    resolver = WorkRefResolver(order_repository=_FakeOrderRepository(doc))
    with pytest.raises(AppException) as exc:
        await resolver.resolve_order("ord-1", _tenant())
    assert exc.value.error_code == ErrorCode.RESOURCE_NOT_FOUND


async def test_resolve_order_other_driver_is_403_with_no_identity_leak():
    doc = {"order_id": "ord-1", "tenant_id": "t1", "assigned_driver_id": "drv-other"}
    resolver = WorkRefResolver(order_repository=_FakeOrderRepository(doc))
    with pytest.raises(AppException) as exc:
        await resolver.resolve_order("ord-1", _tenant())
    assert exc.value.error_code == ErrorCode.FORBIDDEN
    assert exc.value.details == {"order_id": "ord-1"}


async def test_resolve_order_unassigned_order_is_403():
    """No dual acceptance and no permissive unassigned case on this path."""
    doc = {"order_id": "ord-1", "tenant_id": "t1", "assigned_driver_id": None}
    resolver = WorkRefResolver(order_repository=_FakeOrderRepository(doc))
    with pytest.raises(AppException) as exc:
        await resolver.resolve_order("ord-1", _tenant())
    assert exc.value.error_code == ErrorCode.FORBIDDEN
