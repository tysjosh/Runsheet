"""Ops ingestion reports a cross-tenant id collision as a 409, not an outage.

``OpsElasticsearchService._scripted_upsert`` sent every exception through
``_handle_elasticsearch_error``, which raises ``ELASTICSEARCH_UNAVAILABLE``
(503). A tenant-B event whose natural id collides with tenant A's then looked
like a database outage, which webhook senders retry forever (tenant-leak
review issue 1). Issue 5: the refusal no longer echoes the internal index.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from ops.services.ops_es_service import OpsElasticsearchService
from persistence.document_store import CrossTenantWriteError


@pytest.mark.asyncio
async def test_cross_tenant_collision_surfaces_as_409():
    es = MagicMock()
    es.upsert_if_newer = AsyncMock(
        side_effect=CrossTenantWriteError("shipments_current", "SHP-1")
    )
    es._handle_elasticsearch_error = MagicMock()
    service = OpsElasticsearchService(es)

    with pytest.raises(CrossTenantWriteError) as exc:
        await service.upsert_shipment_current(
            {"shipment_id": "SHP-1", "tenant_id": "tenant-b"}
        )

    assert exc.value.status_code == 409
    es._handle_elasticsearch_error.assert_not_called()


def test_refusal_details_omit_the_internal_index():
    err = CrossTenantWriteError("shipments_current", "SHP-1")
    assert err.details == {"doc_id": "SHP-1"}
    assert err.index == "shipments_current"
