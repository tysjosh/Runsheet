"""Station CSV import 500 (qa-tenant-b probe on api:29, 0f82def).

A ``fuel_stations`` row imported through ``ImportService`` was stored in the
template shape (text ``location``, operational ``status``, no liters), and
``GET /api/fuel/stations`` built ``FuelStation(**src)`` for every hit, so the
one bad doc 500'd the tenant's whole station list. These tests cover both
sides: import now writes a loadable ``FuelStation`` (or refuses the row per
field), and the station and inventory lists skip a malformed doc instead of
crashing.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from fuel.models import FuelStation
from fuel.services.fuel_service import FuelService
from inventory.service import InventoryService
from ops.middleware.tenant_guard import TenantContext
from services.document_loading import load_valid_documents
from services.import_service import ImportService
from services.schema_templates import SchemaTemplates


GAL_TO_L = 3.785411784


class _Store:
    """In-memory store: global ids per index; search honors the tenant term."""

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
        must = (query.get("query") or {}).get("bool", {}).get("must", [])
        tenant = next(
            (c["term"]["tenant_id"] for c in must if "tenant_id" in c.get("term", {})),
            None,
        )
        hits = [
            {"_id": doc_id, "_source": dict(doc)}
            for (idx, doc_id), doc in self.documents.items()
            if idx == index and (tenant is None or doc.get("tenant_id") == tenant)
        ]
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}


@pytest.fixture(autouse=True)
def settings_mock():
    with patch("fuel.services.fuel_service.get_settings") as mock:
        s = MagicMock()
        s.fuel_consumption_rolling_window_days = 7
        s.fuel_critical_days_threshold = 3
        s.fuel_alert_default_threshold_pct = 20.0
        mock.return_value = s
        yield s


def _tenant(tenant_id):
    return TenantContext(
        tenant_id=tenant_id,
        user_id=f"admin-{tenant_id}",
        has_pii_access=True,
        roles=["admin"],
    )


async def _validate(service, content, tenant_id, data_type="fuel_stations"):
    parsed = await service.parse_csv(
        content, data_type, tenant_id=tenant_id, source_name=f"{data_type}.csv"
    )
    validated = await service.validate(
        parsed.session_id, parsed.suggested_mapping, tenant_id=tenant_id
    )
    return parsed.session_id, validated


async def _import(service, content, tenant_id):
    session_id, validated = await _validate(service, content, tenant_id)
    # Commit without skip_errors so the commit-time check runs on every row.
    result = await service.commit(session_id, tenant=_tenant(tenant_id))
    return validated, result


def _native_station(station_id, tenant_id, fuel_type="DIESEL_2"):
    return {
        "station_id": station_id,
        "name": f"{station_id} native",
        "fuel_type": fuel_type,
        "capacity_liters": 50000.0,
        "current_stock_liters": 25000.0,
        "daily_consumption_rate": 0.0,
        "days_until_empty": 99999.0,
        "alert_threshold_pct": 20.0,
        "status": "normal",
        "location": None,
        "location_name": "Yard",
        "tenant_id": tenant_id,
        "created_at": "2026-10-01T00:00:00+00:00",
        "last_updated": "2026-10-01T00:00:00+00:00",
    }


EVIDENCE_DOC = {
    "station_id": "QA-TB-A-ISTN-01",
    "name": "QA-TB-A Import Station",
    "location": "100 Main St",
    "status": "open",
    "tenant_id": "tenant-a",
}
EVIDENCE_CSV = (
    b"station_id,name,location,status\n"
    b"QA-TB-A-ISTN-01,QA-TB-A Import Station,100 Main St,open\n"
)
FULL_HEADER = (
    "station_id,name,location,coordinates,fuel_types,capacity_gallons,"
    "current_stock_gallons,price_per_gallon,status,operating_hours,last_restocked\n"
)


def _full_row(
    station_id="FS-1",
    *,
    coordinates="29.76,-95.36",
    fuel_types="diesel",
    capacity="50000",
    stock="35000",
):
    return (
        f'{station_id},Central,"4500 Industrial Blvd","{coordinates}",'
        f'"{fuel_types}",{capacity},{stock},3.85,open,06:00-22:00,'
        "2024-03-14T06:00:00Z\n"
    )


def _station_csv(**kwargs):
    return (FULL_HEADER + _full_row(**kwargs)).encode()


# ---------------------------------------------------------------------------
# 1. Evidence repro: the old document shape no longer 500s the list
# ---------------------------------------------------------------------------


async def test_evidence_doc_is_skipped_not_500(caplog):
    with pytest.raises(Exception) as excinfo:
        FuelStation.model_validate(EVIDENCE_DOC)
    # The CloudWatch error: "7 validation errors for FuelStation".
    assert excinfo.value.error_count() == 7

    store = _Store()
    store.documents[("fuel_stations", "QA-TB-A-ISTN-01")] = dict(EVIDENCE_DOC)
    store.documents[("fuel_stations", "ST-OK::DIESEL_2")] = _native_station(
        "ST-OK", "tenant-a"
    )

    with caplog.at_level(logging.WARNING):
        page = await FuelService(store).list_stations("tenant-a")

    assert [s.station_id for s in page.data] == ["ST-OK"]
    # total still counts the stored doc, so page math matches the store.
    assert page.pagination.total == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "QA-TB-A-ISTN-01" in m and "fuel_stations" in m for m in messages
    ), messages
    assert any("Skipped 1" in m for m in messages), messages
    assert not any("100 Main St" in m for m in messages)


# ---------------------------------------------------------------------------
# 2. The evidence payload through import is refused per field
# ---------------------------------------------------------------------------


async def test_evidence_payload_is_rejected_at_validate_and_commit():
    store = _Store()
    store.documents[("fuel_stations", "ST-OK::DIESEL_2")] = _native_station(
        "ST-OK", "tenant-a"
    )
    service = ImportService(store)

    validated, result = await _import(service, EVIDENCE_CSV, "tenant-a")

    fields = {(e.row_number, e.field_name) for e in validated.errors}
    assert {
        (1, "fuel_types"),
        (1, "capacity_gallons"),
        (1, "current_stock_gallons"),
    } <= fields
    assert validated.valid_rows == 0

    assert result.imported_records == 0
    assert result.status.value == "failed"
    assert result.errors and result.errors[0].startswith("row 1: fuel_types:")
    assert ("fuel_stations", "QA-TB-A-ISTN-01") not in store.documents

    page = await FuelService(store).list_stations("tenant-a")
    assert [s.station_id for s in page.data] == ["ST-OK"]


# ---------------------------------------------------------------------------
# 3. A valid row imports and lists correctly
# ---------------------------------------------------------------------------


async def test_valid_station_row_imports_and_lists():
    store = _Store()
    service = ImportService(store)

    validated, result = await _import(service, _station_csv(), "tenant-a")

    assert validated.error_count == 0, validated.errors
    assert result.imported_records == 1
    stored = store.documents[("fuel_stations", "FS-1")]
    FuelStation.model_validate(stored)
    assert stored["operational_status"] == "open"
    assert stored["tenant_id"] == "tenant-a"
    for key in ("fuel_types", "capacity_gallons", "current_stock_gallons", "coordinates"):
        assert key not in stored

    page = await FuelService(store).list_stations("tenant-a")
    assert len(page.data) == 1
    station = page.data[0]
    assert station.station_id == "FS-1"
    assert station.fuel_type == "DIESEL_2"
    assert station.capacity_liters == pytest.approx(50000 * GAL_TO_L)
    assert station.current_stock_liters == pytest.approx(35000 * GAL_TO_L)
    assert station.status == "normal"
    assert station.location.lat == 29.76
    assert station.location.lon == -95.36
    assert station.location_name == "4500 Industrial Blvd"


# ---------------------------------------------------------------------------
# 4. Field-level rejection at validate and at commit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, field, text",
    [
        ({"fuel_types": "DIESEL_2,PROPANE"}, "fuel_types", "one fuel type per row"),
        ({"fuel_types": "gasoline"}, "fuel_types", "GASOLINE_REG"),
        ({"fuel_types": "UNOBTAINIUM"}, "fuel_types", "Unknown fuel product"),
        ({"capacity": "0"}, "capacity_gallons", "greater than 0"),
        ({"stock": "60000"}, "current_stock_gallons", "between 0"),
        ({"coordinates": "95,10"}, "coordinates", ""),
    ],
)
async def test_bad_station_rows_are_rejected_per_field(kwargs, field, text):
    store = _Store()
    service = ImportService(store)

    _, validated = await _validate(service, _station_csv(**kwargs), "tenant-a")

    assert len(validated.errors) == 1, validated.errors
    error = validated.errors[0]
    assert error.field_name == field
    assert text in error.description
    assert validated.valid_rows == 0


@pytest.mark.parametrize(
    "kwargs, field",
    [
        ({"fuel_types": "DIESEL_2,PROPANE"}, "fuel_types"),
        ({"fuel_types": "gasoline"}, "fuel_types"),
        ({"fuel_types": "UNOBTAINIUM"}, "fuel_types"),
        ({"capacity": "0"}, "capacity_gallons"),
        ({"stock": "60000"}, "current_stock_gallons"),
        ({"coordinates": "95,10"}, "coordinates"),
    ],
)
async def test_commit_refuses_bad_station_rows_without_validate(kwargs, field):
    store = _Store()
    service = ImportService(store)
    session_id, _ = await _validate(service, _station_csv(**kwargs), "tenant-a")
    # Drop validate's verdict: the commit-time guard must hold on its own.
    session = await service._get_active_session(session_id, tenant_id="tenant-a")
    session.validation_result.errors = []
    await service._persist_active_session(session)

    result = await service.commit(session_id, tenant=_tenant("tenant-a"))

    assert result.imported_records == 0
    assert result.errors[0].startswith(f"row 1: {field}:")
    assert ("fuel_stations", "FS-1") not in store.documents


# ---------------------------------------------------------------------------
# 5. The downloadable template imports cleanly
# ---------------------------------------------------------------------------


async def test_downloaded_template_imports():
    store = _Store()
    service = ImportService(store)
    content = SchemaTemplates().generate_csv_template("fuel_stations").encode()

    validated, result = await _import(service, content, "tenant-a")

    assert validated.error_count == 0, validated.errors
    assert result.imported_records == 3
    stations = [
        doc for (idx, _), doc in store.documents.items() if idx == "fuel_stations"
    ]
    assert len(stations) == 3
    for doc in stations:
        FuelStation.model_validate(doc)


# ---------------------------------------------------------------------------
# 6. Same-tenant isolation
# ---------------------------------------------------------------------------


async def test_station_lists_and_imports_stay_in_tenant(caplog):
    store = _Store()
    service = ImportService(store)
    _, imported = await _import(service, _station_csv(station_id="FS-A"), "tenant-a")
    assert imported.imported_records == 1
    stored_a = dict(store.documents[("fuel_stations", "FS-A")])
    store.documents[("fuel_stations", "BAD-B")] = {
        **EVIDENCE_DOC,
        "station_id": "BAD-B",
        "tenant_id": "tenant-b",
    }
    store.documents[("fuel_stations", "ST-B::DIESEL_2")] = _native_station(
        "ST-B", "tenant-b"
    )
    fuel = FuelService(store)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        page_a = await fuel.list_stations("tenant-a")
    assert [s.station_id for s in page_a.data] == ["FS-A"]
    assert not any("Skipp" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        page_b = await fuel.list_stations("tenant-b")
    assert [s.station_id for s in page_b.data] == ["ST-B"]
    assert any("Skipped 1" in r.getMessage() for r in caplog.records)

    validated_b, result_b = await _import(
        service, _station_csv(station_id="FS-A"), "tenant-b"
    )
    assert any(
        "station_id 'FS-A' is already in use" in e.description
        for e in validated_b.errors
    )
    assert result_b.imported_records == 0
    assert "station_id 'FS-A' is already in use" in result_b.errors[0]
    assert store.documents[("fuel_stations", "FS-A")] == stored_a


# ---------------------------------------------------------------------------
# 7. Inventory lists are resilient too
# ---------------------------------------------------------------------------


def _inventory_doc(item_id, quantity, status, tenant_id="tenant-a"):
    return {
        "item_id": item_id,
        "name": f"{item_id} name",
        "category": "general",
        "quantity": quantity,
        "unit": "units",
        "min_threshold": 10,
        "max_capacity": 100,
        "location": "Warehouse A",
        "status": status,
        "tenant_id": tenant_id,
    }


async def test_inventory_list_items_skips_malformed(caplog):
    store = _Store()
    store.documents[("inventory", "INV-OK")] = _inventory_doc("INV-OK", 50, "in_stock")
    store.documents[("inventory", "INV-X")] = {
        "item_id": "INV-X",
        "quantity": 9,
        "tenant_id": "tenant-a",
    }

    with caplog.at_level(logging.WARNING):
        result = await InventoryService(store).list_items("tenant-a")

    assert [i.item_id for i in result["items"]] == ["INV-OK"]
    assert any("INV-X" in r.getMessage() for r in caplog.records)


async def test_inventory_low_stock_alerts_skip_malformed(caplog):
    store = _Store()
    store.documents[("inventory", "INV-LOW")] = _inventory_doc("INV-LOW", 2, "low_stock")
    store.documents[("inventory", "INV-X")] = {
        "item_id": "INV-X",
        "quantity": 9,
        "status": "low_stock",
        "tenant_id": "tenant-a",
    }

    with caplog.at_level(logging.WARNING):
        alerts = await InventoryService(store).get_low_stock_alerts("tenant-a")

    assert [i.item_id for i in alerts] == ["INV-LOW"]
    assert any("INV-X" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 8. Helper unit test
# ---------------------------------------------------------------------------


class _Thing(BaseModel):
    name: str
    size: int


@pytest.mark.parametrize("bad_count", [0, 1, 3])
def test_load_valid_documents_counts(bad_count, caplog):
    hits = [{"_id": "ok-1", "_source": {"name": "a", "size": 1}}]
    hits += [{"_id": f"bad-{n}", "_source": {"name": "b"}} for n in range(bad_count)]

    with caplog.at_level(logging.WARNING):
        loaded, skipped = load_valid_documents(hits, _Thing, index="things", tenant_id="t")

    assert [t.name for t in loaded] == ["a"]
    assert skipped == bad_count
    summaries = [r for r in caplog.records if "Skipped" in r.getMessage()]
    assert len(summaries) == (1 if bad_count else 0)


def test_load_valid_documents_tolerates_missing_source():
    loaded, skipped = load_valid_documents(
        [{"_id": "empty"}, {"_id": "none", "_source": None}],
        _Thing,
        index="things",
        tenant_id="t",
    )
    assert loaded == []
    assert skipped == 2
