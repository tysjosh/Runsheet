"""Focused tests for the canonical fuel import workflow."""

from types import SimpleNamespace

import pytest

from ops.middleware.tenant_guard import TenantContext
from services.import_service import ImportService
from services.schema_templates import SchemaTemplates


pytestmark = pytest.mark.asyncio


class _Elasticsearch:
    def __init__(self):
        self.documents = {}
        self.bulk_calls = []

    async def index_document(self, index, document_id, document):
        self.documents[(index, document_id)] = dict(document)
        return {"result": "created"}

    async def get_document(self, index, document_id):
        return self.documents.get((index, document_id))

    async def bulk_index_documents(self, index, documents):
        self.bulk_calls.append((index, list(documents)))
        return {"successful": len(documents), "failed": 0, "errors": []}


class _OrderPipeline:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or ["processed", "processed"])
        self.calls = []

    async def ingest_csv(self, **kwargs):
        self.calls.append(kwargs)
        status = self.statuses.pop(0)
        return SimpleNamespace(status=status)


def _tenant(tenant_id="tenant-a"):
    return TenantContext(
        tenant_id=tenant_id,
        user_id="dispatcher-a",
        has_pii_access=True,
        roles=["dispatcher"],
    )


async def _parse_and_validate(service, data_type, tenant_id="tenant-a"):
    content = SchemaTemplates().generate_csv_template(data_type).encode()
    parsed = await service.parse_csv(
        content,
        data_type,
        tenant_id=tenant_id,
        source_name=f"{data_type}.csv",
    )
    validated = await service.validate(
        parsed.session_id,
        parsed.suggested_mapping,
        tenant_id=tenant_id,
    )
    assert validated.error_count == 0
    return parsed.session_id


async def test_order_import_routes_through_intake_pipeline_not_generic_bulk_index():
    es = _Elasticsearch()
    pipeline = _OrderPipeline()
    service = ImportService(es, order_intake_pipeline=pipeline)
    session_id = await _parse_and_validate(service, "orders")

    result = await service.commit(session_id, tenant=_tenant())

    assert result.imported_records == 2
    assert result.es_index == "fuel_orders_current"
    assert es.bulk_calls == []
    assert [call["client_event_id"] for call in pipeline.calls] == [
        "csv:sample_erp:SO-1001:2026-07-29T12:00:00Z",
        "csv:sample_erp:SO-1002:2026-07-29T12:05:00Z",
    ]
    assert pipeline.calls[0]["payload"]["source_updated_at"].endswith("Z")
    assert pipeline.calls[0]["import_batch_id"] == session_id


async def test_duplicate_source_order_is_counted_as_skipped():
    es = _Elasticsearch()
    pipeline = _OrderPipeline(["processed", "duplicate"])
    service = ImportService(es, order_intake_pipeline=pipeline)
    session_id = await _parse_and_validate(service, "orders")

    result = await service.commit(session_id, tenant=_tenant())

    assert result.imported_records == 1
    assert result.skipped_records == 1
    assert result.error_count == 0


async def test_active_session_survives_service_restart_and_remains_tenant_scoped():
    es = _Elasticsearch()
    first_service = ImportService(es)
    content = SchemaTemplates().generate_csv_template("inventory").encode()
    parsed = await first_service.parse_csv(
        content,
        "inventory",
        tenant_id="tenant-a",
        source_name="inventory.csv",
    )

    restarted_service = ImportService(es)
    with pytest.raises(ValueError, match="not found"):
        await restarted_service.validate(
            parsed.session_id,
            parsed.suggested_mapping,
            tenant_id="tenant-b",
        )

    validated = await restarted_service.validate(
        parsed.session_id,
        parsed.suggested_mapping,
        tenant_id="tenant-a",
    )
    assert validated.valid_rows == 3
