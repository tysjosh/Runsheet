"""Real-world-style acceptance test for fuel order and tank imports.

The fixtures are fictional but intentionally model the operational messiness
seen in distributor exports: aliases, leading-zero ZIP codes, source revisions,
exact replays, stale telemetry, and unsafe records.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fuel.customer_tank_models import CustomerTankRepository
from fuel.intake.adapter_base import IntakeAdapterRegistry
from fuel.intake.csv_adapter import CsvIntakeAdapter
from fuel.services.order_intake_pipeline import OrderIntakePipeline
from fuel.services.tank_import_service import TankImportService
from ops.middleware.tenant_guard import TenantContext
from services.import_service import ImportService


pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

FIXTURES = (
    Path(__file__).parents[1] / "fixtures" / "fuel_import_real_world"
)
TENANT_ID = "tenant-pilot"


def _collect_terms(value):
    terms = {}
    if isinstance(value, dict):
        term = value.get("term")
        if isinstance(term, dict):
            terms.update(term)
        for nested in value.values():
            terms.update(_collect_terms(nested))
    elif isinstance(value, list):
        for nested in value:
            terms.update(_collect_terms(nested))
    return terms


class _SyncElasticsearchClient:
    def __init__(self, owner):
        self._owner = owner

    def update(self, *, index, id, body, refresh):
        key = (index, id)
        existed = key in self._owner.documents
        document = deepcopy(
            body.get("script", {}).get("params") or body.get("upsert") or {}
        )
        self._owner.documents[key] = document
        return {"result": "updated" if existed else "created"}

    def exists(self, *, index, id):
        return (index, id) in self._owner.documents

    def index(self, *, index, id, body, refresh):
        self._owner.documents[(index, id)] = deepcopy(body)
        return {"result": "created"}


class _InMemoryElasticsearch:
    """Small ES contract implementation used by the real domain services."""

    def __init__(self):
        self.documents = {}
        self.client = _SyncElasticsearchClient(self)

    async def index_document(self, index, document_id, document):
        self.documents[(index, document_id)] = deepcopy(document)
        return {"result": "created"}

    async def get_document(self, index, document_id):
        document = self.documents.get((index, document_id))
        return deepcopy(document) if document is not None else None

    async def update_document(self, index, document_id, patch):
        key = (index, document_id)
        if key not in self.documents:
            raise KeyError(document_id)
        self.documents[key].update(deepcopy(patch))
        return {"result": "updated"}

    async def delete_document(self, index, document_id):
        return self.documents.pop((index, document_id), None) is not None

    async def upsert_if_newer(
        self, index, document_id, document, *, timestamp_field="last_event_timestamp"
    ):
        """Timestamp-guarded upsert, matching the real facade's semantics.

        ``FuelOrderRepository`` used to build a painless ``scripted_upsert`` and
        call ``client.update`` directly, which would have kept writing to
        Elasticsearch after the document plane moved to Postgres. It now goes
        through ``ElasticsearchService.upsert_if_newer``, so this fake implements
        it — including the part that reads like an off-by-one: an EQUAL timestamp
        is discarded, because at-least-once delivery makes an equal timestamp the
        common case for a redelivery and applying it would undo a later event.
        """
        key = (index, document_id)
        current = self.documents.get(key)
        incoming = document.get(timestamp_field)
        if current is not None:
            stored = current.get(timestamp_field)
            if stored is not None and incoming is not None and incoming <= stored:
                return False
            merged = deepcopy(current)
            merged.update(deepcopy(document))
            self.documents[key] = merged
            return True
        self.documents[key] = deepcopy(document)
        return True

    async def search_documents(self, index, query, size=10):
        terms = _collect_terms(query)
        matches = []
        for (doc_index, document_id), document in self.documents.items():
            if doc_index != index:
                continue
            if all(document.get(field) == expected for field, expected in terms.items()):
                matches.append(
                    {
                        "_id": document_id,
                        "_source": deepcopy(document),
                    }
                )
        return {
            "hits": {
                "hits": matches[:size],
                "total": {"value": len(matches)},
            }
        }

    async def bulk_index_documents(self, index, documents):
        for position, document in enumerate(documents):
            await self.index_document(index, f"bulk-{position}", document)
        return {"successful": len(documents), "failed": 0, "errors": []}

    def docs_for(self, index):
        return [
            deepcopy(document)
            for (doc_index, _), document in self.documents.items()
            if doc_index == index
        ]


class _IdempotencyStore:
    def __init__(self):
        self._processed = set()

    async def is_duplicate(self, event_id, *, tenant_id):
        return (tenant_id, event_id) in self._processed

    async def mark_processed(self, event_id, *, tenant_id):
        self._processed.add((tenant_id, event_id))


class _ActiveFeatureFlags:
    async def get_overlay_state(self, flag_key, tenant_id):
        return "active_auto"


def _tenant(tenant_id=TENANT_ID):
    return TenantContext(
        tenant_id=tenant_id,
        user_id="pilot-dispatcher",
        has_pii_access=True,
        roles=["dispatcher"],
    )


def _build_import_service():
    es = _InMemoryElasticsearch()
    tank_repository = CustomerTankRepository(es)
    registry = IntakeAdapterRegistry()
    registry.register(CsvIntakeAdapter(), channel_type="csv", schema_version="1.0")
    order_pipeline = OrderIntakePipeline(
        es_service=es,
        intake_channel_repo=SimpleNamespace(),
        adapter_registry=registry,
        idempotency_service=_IdempotencyStore(),
        feature_flag_service=_ActiveFeatureFlags(),
        poison_queue_service=SimpleNamespace(
            store_failed_event=AsyncMock()
        ),
        ws_manager=SimpleNamespace(broadcast=AsyncMock()),
        credentials_vault=SimpleNamespace(),
        customer_tank_repo=tank_repository,
    )
    tank_importer = TankImportService(
        es_service=es,
        customer_tank_repository=tank_repository,
    )
    return (
        ImportService(
            es,
            order_intake_pipeline=order_pipeline,
            tank_import_service=tank_importer,
        ),
        es,
    )


async def _validate_fixture(service, filename, data_type, tenant_id=TENANT_ID):
    parsed = await service.parse_csv(
        (FIXTURES / filename).read_bytes(),
        data_type,
        tenant_id=tenant_id,
        source_name=filename,
    )
    validation = await service.validate(
        parsed.session_id,
        parsed.suggested_mapping,
        tenant_id=tenant_id,
    )
    return parsed, validation


async def _import_fixture(service, filename, data_type):
    parsed, validation = await _validate_fixture(service, filename, data_type)
    assert validation.error_count == 0, validation.errors
    result = await service.commit(
        parsed.session_id,
        skip_errors=True,
        tenant=_tenant(),
    )
    return result


async def test_distributor_pilot_import_end_to_end():
    service, es = _build_import_service()

    tank_result = await _import_fixture(
        service, "customer_tanks.csv", "customer_tanks"
    )
    order_result = await _import_fixture(service, "orders.csv", "orders")
    reading_result = await _import_fixture(
        service, "tank_readings.csv", "tank_readings"
    )

    assert (tank_result.imported_records, tank_result.skipped_records) == (5, 0)
    assert (order_result.imported_records, order_result.skipped_records) == (6, 1)
    assert (reading_result.imported_records, reading_result.skipped_records) == (
        6,
        1,
    )

    tanks = es.docs_for("customer_tanks")
    assert len(tanks) == 5
    diesel_tank = next(t for t in tanks if t["customer_tank_id"] == "tank-100")
    nj_tank = next(t for t in tanks if t["customer_tank_id"] == "tank-400")
    assert diesel_tank["fuel_product_code"] == "DIESEL_2"
    assert diesel_tank["current_level_gallons"] == 250
    assert nj_tank["zip_code"] == "07001"

    orders = es.docs_for("fuel_orders_current")
    assert len(orders) == 5
    revised = next(
        order
        for order in orders
        if order["intake_metadata"]["source_record_id"] == "PE-9001"
    )
    assert revised["gallons_requested"] == 900
    assert revised["product_code"] == "DIESEL_2"
    assert revised["order_id"].startswith("ord_import_")
    assert len(es.docs_for("fuel_order_events")) == 6

    readings = es.docs_for("atg_readings")
    assert len(readings) == 6
    assert any(
        reading["source_reading_id"] == "VR-T100-1100"
        for reading in readings
    )

    history = es.docs_for("import_sessions")
    assert len(history) == 3
    assert all(record["tenant_id"] == TENANT_ID for record in history)


async def test_unsafe_real_world_rows_are_rejected_during_validation():
    service, _ = _build_import_service()
    await _import_fixture(service, "customer_tanks.csv", "customer_tanks")

    _, order_validation = await _validate_fixture(
        service, "invalid_orders.csv", "orders"
    )
    _, tank_validation = await _validate_fixture(
        service, "invalid_customer_tanks.csv", "customer_tanks"
    )
    _, reading_validation = await _validate_fixture(
        service, "invalid_tank_readings.csv", "tank_readings"
    )

    assert {issue.row_number for issue in order_validation.errors} == {1, 2}
    assert {issue.row_number for issue in tank_validation.errors} == {1}
    assert {issue.row_number for issue in reading_validation.errors} == {1, 2}


async def test_import_session_cannot_cross_tenant_boundary():
    service, _ = _build_import_service()
    parsed = await service.parse_csv(
        (FIXTURES / "orders.csv").read_bytes(),
        "orders",
        tenant_id=TENANT_ID,
        source_name="orders.csv",
    )

    with pytest.raises(ValueError, match="not found"):
        await service.validate(
            parsed.session_id,
            parsed.suggested_mapping,
            tenant_id="tenant-other",
        )
