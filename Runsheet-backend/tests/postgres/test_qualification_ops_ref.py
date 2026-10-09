"""Qualification summary resolves an ops driver id on the real store (B10).

``GET /api/ops/drivers/{driver_id}/profile`` asks the compliance service for a
qualification summary keyed by the *ops* driver id (``DRV-001``). DQ records
are keyed ``driver_<uuid>``, so the direct lookup never matched and the profile
always showed ``qualification: unresolved``. The fix falls back to a
tenant-scoped ``term external_refs.ops_driver_id`` lookup.

That term is a nested-key containment on the store's translator, so it is
tested against real PostgreSQL rather than a mock — see ``conftest.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict

import pytest

from errors.exceptions import AppException

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


class _IndexFacade:
    """Route every index name to the per-test index."""

    def __init__(self, store, index_name: str) -> None:
        self._store = store
        self._index = index_name

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100, **kw):
        return await self._store.search_documents(self._index, query, size)


def _dq_driver(driver_id: str, tenant_id: str, ops_id: str | None, name: str) -> Dict[str, Any]:
    far = (date.today() + timedelta(days=365)).isoformat()
    return {
        "driver_id": driver_id,
        "tenant_id": tenant_id,
        "full_name": name,
        "cdl_number": "CDL-QA",
        "cdl_state": "TX",
        "cdl_class": "A",
        "cdl_expiry_date": far,
        "medical_card_expiry_date": far,
        "status": "active",
        "external_refs": {"ops_driver_id": ops_id} if ops_id else None,
    }


@pytest.fixture
def service(store, index_name):
    from compliance.services.driver_qualification_service import (
        DriverQualificationService,
    )

    return DriverQualificationService(_IndexFacade(store, index_name))


async def test_ops_id_resolves_the_linked_dq_driver(service, store, index_name):
    dq_id = "driver_11111111-1111-1111-1111-111111111111"
    await store.index_document(
        index_name, dq_id, _dq_driver(dq_id, TENANT_A, "DRV-001", "QA Alice")
    )

    summary = await service.get_qualification_summary(TENANT_A, "DRV-001")

    assert summary.driver_id == dq_id
    assert summary.full_name == "QA Alice"
    assert summary.overall_status == "valid"


async def test_another_tenants_link_is_not_found(service, store, index_name):
    dq_id = "driver_22222222-2222-2222-2222-222222222222"
    await store.index_document(
        index_name, dq_id, _dq_driver(dq_id, TENANT_B, "DRV-002", "QA Bob")
    )

    with pytest.raises(AppException) as exc_info:
        await service.get_qualification_summary(TENANT_A, "DRV-002")

    assert exc_info.value.status_code == 404


async def test_dq_id_still_resolves_directly(service, store, index_name):
    dq_id = "driver_33333333-3333-3333-3333-333333333333"
    await store.index_document(
        index_name, dq_id, _dq_driver(dq_id, TENANT_A, None, "QA Carol")
    )

    summary = await service.get_qualification_summary(TENANT_A, dq_id)

    assert summary.driver_id == dq_id
    assert summary.full_name == "QA Carol"


async def test_unlinked_ops_id_is_not_found(service, store, index_name):
    dq_id = "driver_44444444-4444-4444-4444-444444444444"
    await store.index_document(
        index_name, dq_id, _dq_driver(dq_id, TENANT_A, "DRV-OTHER", "QA Dan")
    )

    with pytest.raises(AppException) as exc_info:
        await service.get_qualification_summary(TENANT_A, "DRV-404")

    assert exc_info.value.status_code == 404
