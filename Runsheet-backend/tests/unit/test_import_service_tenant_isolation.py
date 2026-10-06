"""C1: non-canonical imports are tenant-stamped and never overwrite another tenant.

Document ids are global in the store, so two tenants importing the same
``item_id`` used to share (and overwrite) one row. These tests drive the real
``ImportService`` against an in-memory store with create-if-absent semantics.
"""

import pytest

from ops.middleware.tenant_guard import TenantContext
from services.import_service import ImportService


pytestmark = pytest.mark.asyncio


class _Store:
    """In-memory store: ids are global per index, like the Postgres document store."""

    def __init__(self):
        self.documents: dict[tuple[str, str], dict] = {}

    async def index_document(self, index, doc_id, document):
        self.documents[(index, doc_id)] = dict(document)
        return {"result": "created"}

    async def create_document(self, index, doc_id, document):
        if (index, doc_id) in self.documents:
            return False
        self.documents[(index, doc_id)] = dict(document)
        return True

    async def get_document(self, index, doc_id):
        doc = self.documents.get((index, doc_id))
        return dict(doc) if doc is not None else None

    async def search_documents(self, index, query, size=10):
        return {"hits": {"hits": [], "total": {"value": 0}}}

    async def bulk_index_documents(self, index, documents):  # pragma: no cover
        raise AssertionError("non-canonical imports must not bulk upsert")


def _tenant(tenant_id):
    return TenantContext(
        tenant_id=tenant_id,
        user_id=f"admin-{tenant_id}",
        has_pii_access=True,
        roles=["admin"],
    )


INVENTORY_HEADER = "item_id,name,category,quantity,unit,location,status,last_updated\n"


def _inventory_csv(quantity):
    return (
        INVENTORY_HEADER
        + f"INV-1,Brake Pads,spare_parts,{quantity},sets,Warehouse A,in_stock,"
        "2024-03-15T08:00:00Z\n"
    ).encode()


async def _import(service, data_type, content, tenant_id):
    parsed = await service.parse_csv(
        content, data_type, tenant_id=tenant_id, source_name=f"{data_type}.csv"
    )
    validated = await service.validate(
        parsed.session_id, parsed.suggested_mapping, tenant_id=tenant_id
    )
    assert validated.error_count == 0, validated.errors
    return await service.commit(parsed.session_id, tenant=_tenant(tenant_id))


async def test_two_tenants_cannot_overwrite_each_others_rows():
    store = _Store()
    service = ImportService(store)

    first = await _import(service, "inventory", _inventory_csv(150), "tenant-a")
    assert first.imported_records == 1
    assert first.error_count == 0
    stored_a = dict(store.documents[("inventory", "INV-1")])
    assert stored_a["tenant_id"] == "tenant-a"
    assert stored_a["quantity"] == 150

    second = await _import(service, "inventory", _inventory_csv(1), "tenant-b")
    assert second.imported_records == 0
    assert second.error_count == 1
    assert second.status.value == "failed"
    assert "item_id" in second.errors[0]
    assert "tenant-a" not in second.errors[0]
    # Tenant A's row is untouched: body and owner.
    assert store.documents[("inventory", "INV-1")] == stored_a


async def test_tenant_reimport_updates_its_own_row():
    store = _Store()
    service = ImportService(store)
    await _import(service, "inventory", _inventory_csv(150), "tenant-a")

    again = await _import(service, "inventory", _inventory_csv(75), "tenant-a")

    assert again.imported_records == 1
    assert again.error_count == 0
    stored = store.documents[("inventory", "INV-1")]
    assert stored["quantity"] == 75
    assert stored["tenant_id"] == "tenant-a"


async def test_legacy_row_without_tenant_is_not_adopted():
    store = _Store()
    store.documents[("inventory", "INV-1")] = {"item_id": "INV-1", "quantity": 9}
    service = ImportService(store)

    result = await _import(service, "inventory", _inventory_csv(150), "tenant-a")

    assert result.error_count == 1
    assert store.documents[("inventory", "INV-1")] == {"item_id": "INV-1", "quantity": 9}


async def test_fuel_stations_import_is_keyed_by_station_id():
    store = _Store()
    service = ImportService(store)
    content = (
        "station_id,name,location,coordinates,fuel_types,capacity_gallons,"
        "current_stock_gallons,price_per_gallon,status,operating_hours,last_restocked\n"
        'FS-1,Central,"4500 Industrial Blvd","29.76,-95.36",diesel,50000,35000,'
        "3.85,open,06:00-22:00,2024-03-14T06:00:00Z\n"
    ).encode()

    result = await _import(service, "fuel_stations", content, "tenant-a")

    assert result.imported_records == 1
    assert store.documents[("fuel_stations", "FS-1")]["tenant_id"] == "tenant-a"


async def test_missing_tenant_context_is_refused():
    store = _Store()
    service = ImportService(store)
    parsed = await service.parse_csv(
        _inventory_csv(1), "inventory", tenant_id="", source_name="inventory.csv"
    )
    await service.validate(parsed.session_id, parsed.suggested_mapping)

    with pytest.raises(ValueError, match="tenant context is required"):
        await service.commit(parsed.session_id)
    assert store.documents.get(("inventory", "INV-1")) is None
