"""Tests for canonical customer-tank and telemetry imports."""

from datetime import datetime, timezone

import pytest

from fuel.customer_tank_models import CustomerTank
from fuel.services.tank_import_service import TankImportService


pytestmark = pytest.mark.asyncio


def _tank(**overrides) -> CustomerTank:
    payload = {
        "customer_tank_id": "tank-100",
        "tenant_id": "tenant-a",
        "source_system": "erp",
        "external_tank_id": "EXT-100",
        "customer_id": "customer-100",
        "customer_type": "commercial",
        "fuel_type": "diesel",
        "fuel_product_code": "DIESEL_2",
        "capacity_gallons": 1000,
        "current_level_gallons": 400,
        "last_reading_at": datetime(2026, 7, 29, 12, tzinfo=timezone.utc),
        "location_lat": 39.7684,
        "location_lon": -86.1581,
        "zip_code": "46201",
        "status": "active",
    }
    payload.update(overrides)
    return CustomerTank.model_validate(payload)


class _TankRepository:
    def __init__(self, tank: CustomerTank | None = None):
        self.tank = tank
        self.created_payload = None
        self.updates = []

    async def get_by_external_id(self, tenant_id, source_system, external_tank_id):
        if (
            self.tank
            and self.tank.tenant_id == tenant_id
            and self.tank.source_system == source_system
            and self.tank.external_tank_id == external_tank_id
        ):
            return self.tank
        return None

    async def get(self, tenant_id, customer_tank_id):
        if (
            self.tank
            and self.tank.tenant_id == tenant_id
            and self.tank.customer_tank_id == customer_tank_id
        ):
            return self.tank
        return None

    async def create(self, tenant_id, payload):
        self.created_payload = dict(payload)
        self.tank = _tank(
            **payload,
            tenant_id=tenant_id,
            customer_tank_id=payload.get("customer_tank_id") or "tank-created",
        )
        return self.tank

    async def update(self, tenant_id, customer_tank_id, patch):
        assert self.tank is not None
        assert self.tank.tenant_id == tenant_id
        assert self.tank.customer_tank_id == customer_tank_id
        self.updates.append(dict(patch))
        self.tank = CustomerTank.model_validate(
            {**self.tank.model_dump(), **patch}
        )
        return self.tank


class _Elasticsearch:
    def __init__(self):
        self.documents = {}

    async def get_document(self, index, document_id):
        return self.documents.get((index, document_id))

    async def index_document(self, index, document_id, document):
        self.documents[(index, document_id)] = dict(document)
        return {"result": "created"}


def _tank_payload(**overrides):
    payload = {
        "source_system": "erp",
        "external_tank_id": "EXT-100",
        "customer_id": "customer-100",
        "customer_type": "commercial",
        "fuel_type": "diesel",
        "fuel_product_code": "DIESEL_2",
        "capacity_gallons": 1000,
        "current_level_gallons": 400,
        "last_reading_at": "2026-07-29T12:00:00Z",
        "location_lat": 39.7684,
        "location_lon": -86.1581,
        "zip_code": "46201",
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _reading_payload(**overrides):
    payload = {
        "source_system": "erp",
        "external_tank_id": "EXT-100",
        "source_reading_id": "reading-100",
        "volume_gallons": 350,
        "reading_at": "2026-07-29T13:00:00Z",
    }
    payload.update(overrides)
    return payload


async def test_import_tank_creates_source_linked_master_record():
    repository = _TankRepository()
    service = TankImportService(
        es_service=_Elasticsearch(),
        customer_tank_repository=repository,
    )

    outcome = await service.import_tank("tenant-a", _tank_payload())

    assert outcome.status == "created"
    assert repository.created_payload["source_system"] == "erp"
    assert repository.created_payload["external_tank_id"] == "EXT-100"


async def test_import_tank_does_not_regress_level_from_older_master_snapshot():
    repository = _TankRepository(_tank())
    service = TankImportService(
        es_service=_Elasticsearch(),
        customer_tank_repository=repository,
    )

    outcome = await service.import_tank(
        "tenant-a",
        _tank_payload(
            current_level_gallons=150,
            last_reading_at="2026-07-29T11:00:00Z",
            zip_code="46202",
        ),
    )

    assert outcome.status == "updated"
    assert repository.tank.current_level_gallons == 400
    assert repository.tank.zip_code == "46202"
    assert "current_level_gallons" not in repository.updates[0]
    assert "last_reading_at" not in repository.updates[0]


async def test_import_reading_is_idempotent_and_updates_current_level_once():
    repository = _TankRepository(_tank())
    es = _Elasticsearch()
    service = TankImportService(
        es_service=es,
        customer_tank_repository=repository,
    )

    first = await service.import_reading("tenant-a", _reading_payload())
    second = await service.import_reading("tenant-a", _reading_payload())

    assert first.status == "created"
    assert first.current_level_updated is True
    assert second.status == "duplicate"
    assert second.reading_id == first.reading_id
    assert len(repository.updates) == 1
    assert repository.tank.current_level_gallons == 350


async def test_older_reading_is_retained_without_regressing_current_level():
    repository = _TankRepository(_tank())
    es = _Elasticsearch()
    service = TankImportService(
        es_service=es,
        customer_tank_repository=repository,
    )

    outcome = await service.import_reading(
        "tenant-a",
        _reading_payload(
            source_reading_id="older-reading",
            volume_gallons=250,
            reading_at="2026-07-29T11:00:00Z",
        ),
    )

    assert outcome.status == "stored_stale"
    assert outcome.current_level_updated is False
    assert repository.updates == []
    assert repository.tank.current_level_gallons == 400
    assert len(es.documents) == 1


async def test_reading_above_tank_capacity_is_rejected():
    service = TankImportService(
        es_service=_Elasticsearch(),
        customer_tank_repository=_TankRepository(_tank()),
    )

    with pytest.raises(ValueError, match="cannot exceed tank capacity"):
        await service.import_reading(
            "tenant-a",
            _reading_payload(volume_gallons=1001),
        )
