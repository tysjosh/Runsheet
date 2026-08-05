"""Dual-write tests for the three formerly Elasticsearch-only fuel assets.

``customer_tanks``, ``truck_compartments`` and ``fuel_stations`` had no Postgres
table, so recreating the Elasticsearch cluster destroyed them permanently — and
did, during an end-to-end test of the MVP pipeline, leaving the A1 tank
forecasting and A3 compartment loading stages with no input while the endpoint
still reported success.

These tests cover what the migration has to get right, which is more than "the
row lands":

* each of the three has a different primary-key rule, and two of them cannot use
  the document's obvious id field. Getting that wrong loses rows silently.
* ``last_loaded_product`` must survive as a typed column, because it is the
  input to the cross-contamination guard and an absent value reads as "no
  history", i.e. permitted.
* the projector must round-trip the document byte-identically, since the read
  path returns it verbatim and the rebuild writes it back to Elasticsearch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from persistence.database import session_scope
from persistence.models import (
    CustomerTankORM,
    FuelStationORM,
    OutboxEventORM,
    TruckCompartmentORM,
)
from persistence.projections import _document_passthrough
from persistence.read_repositories import HybridReadRepository
from persistence.repositories import CurrentStateRepository

TENANT = "demo-tenant"


def _tank_doc(**over):
    doc = {
        "customer_tank_id": "TANK-001",
        "customer_id": "CUST-001",
        "tenant_id": TENANT,
        "status": "active",
        "fuel_type": "DIESEL_2",
        "fuel_product_code": "DIESEL_2",
        "customer_type": "commercial",
        "zip_code": "07030",
        "capacity_gallons": 1000.0,
        "current_level_gallons": 420.0,
        "k_factor": 3.14,
        "use_case": "heating",
    }
    doc.update(over)
    return doc


def _compartment_doc(**over):
    doc = {
        "truck_id": "TNK-002",
        "compartment_id": "C1",
        "tenant_id": TENANT,
        "capacity_liters": 8000.0,
        "allowed_grades": ["DIESEL_2", "GASOLINE_REG"],
        "position_index": 1,
        "state": "loaded",
        "last_loaded_product": "DIESEL_2",
        "last_loaded_at": "2026-08-01T10:00:00+00:00",
    }
    doc.update(over)
    return doc


def _station_doc(**over):
    doc = {
        "station_id": "FS-001",
        "tenant_id": TENANT,
        "name": "Hoboken Depot",
        "status": "healthy",
        "fuel_type": "DIESEL_2",
        "fuel_grade": "DIESEL_2",
        "capacity_liters": 50000.0,
        "current_stock_liters": 31000.0,
    }
    doc.update(over)
    return doc


# ---------------------------------------------------------------------------
# customer_tank — keyed on the tank id, NOT the customer id
# ---------------------------------------------------------------------------


async def test_customer_tank_upsert_keys_on_tank_id(engine):
    repo = CurrentStateRepository("customer_tank")
    doc = _tank_doc()
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc)
        assert row.customer_tank_id == "TANK-001"
        assert row.customer_id == "CUST-001"
        assert row.zip_code == "07030"
        assert _document_passthrough(row) == doc

    async with session_scope() as s:
        events = (await s.execute(select(OutboxEventORM))).scalars().all()
    assert [e.aggregate_type for e in events] == ["customer_tank"]
    assert events[0].target_index == "customer_tanks"
    assert events[0].aggregate_id == "TANK-001"


async def test_two_tanks_for_one_customer_are_two_rows(engine):
    """The collision the Elasticsearch key made possible cannot happen here.

    In Elasticsearch these documents share an ``_id`` of ``CUST-001`` (the
    seeder's id resolver preferred the foreign key), so the second silently
    overwrote the first. It stayed latent only because no fixture gave one
    customer two tanks; the domain allows it.
    """
    repo = CurrentStateRepository("customer_tank")
    async with session_scope() as s:
        await repo.upsert(s, doc=_tank_doc(customer_tank_id="TANK-001"))
        await repo.upsert(s, doc=_tank_doc(customer_tank_id="TANK-002"))

    async with session_scope() as s:
        rows = (await s.execute(select(CustomerTankORM))).scalars().all()
    assert sorted(r.customer_tank_id for r in rows) == ["TANK-001", "TANK-002"]
    assert {r.customer_id for r in rows} == {"CUST-001"}


async def test_customer_tank_set_fields_merges_and_lifts(engine):
    """The ATG connector and the k-factor service both write partial updates."""
    repo = CurrentStateRepository("customer_tank")
    async with session_scope() as s:
        await repo.upsert(s, doc=_tank_doc())
    async with session_scope() as s:
        row = await repo.set_fields(
            s, TENANT, "TANK-001",
            current_level_gallons=88.0, k_factor=2.5, status="low",
        )
        assert row.status == "low"  # lifted into the typed column
        assert row.document["current_level_gallons"] == 88.0
        assert row.document["k_factor"] == 2.5
        # Untouched fields survive the merge.
        assert row.document["capacity_gallons"] == 1000.0


async def test_customer_tank_cross_tenant_set_fields_is_a_no_op(engine):
    repo = CurrentStateRepository("customer_tank")
    async with session_scope() as s:
        await repo.upsert(s, doc=_tank_doc())
    async with session_scope() as s:
        assert await repo.set_fields(s, "other-tenant", "TANK-001", status="low") is None
    async with session_scope() as s:
        row = await s.get(CustomerTankORM, "TANK-001")
    assert row.status == "active"


# ---------------------------------------------------------------------------
# truck_compartment — composite key, and the guard's input field
# ---------------------------------------------------------------------------


async def test_compartment_key_is_derived_when_no_doc_id_is_passed(engine):
    """The document has no single id field; the composite must be rebuilt.

    Readers fetch compartments by ``f"{truck_id}_{compartment_id}"``, so a row
    stored under anything else is present but unreachable.
    """
    repo = CurrentStateRepository("truck_compartment")
    async with session_scope() as s:
        row = await repo.upsert(s, doc=_compartment_doc())
    assert row.compartment_key == "TNK-002_C1"
    assert row.truck_id == "TNK-002"
    assert row.compartment_id == "C1"


async def test_explicit_doc_id_wins_over_the_derived_key(engine):
    repo = CurrentStateRepository("truck_compartment")
    async with session_scope() as s:
        row = await repo.upsert(
            s, doc=_compartment_doc(), doc_id="TNK-002_C1",
        )
    assert row.compartment_key == "TNK-002_C1"


async def test_last_loaded_product_survives_the_round_trip(engine):
    """The cross-contamination guard reads this; an absent value reads as OK.

    Asserted on the typed column *and* in the document, because the guard's
    Elasticsearch query reads the document while a SQL audit reads the column,
    and a migration that populated only one of them would look correct from
    whichever side happened to be checked.
    """
    repo = CurrentStateRepository("truck_compartment")
    async with session_scope() as s:
        row = await repo.upsert(s, doc=_compartment_doc())
        assert row.last_loaded_product == "DIESEL_2"
        assert row.document["last_loaded_product"] == "DIESEL_2"

    async with session_scope() as s:
        doc = await HybridReadRepository("truck_compartment").get(
            s, TENANT, "TNK-002_C1"
        )
    assert doc["last_loaded_product"] == "DIESEL_2"
    assert doc == _compartment_doc()


async def test_mark_cleaned_clears_the_product_without_losing_the_row(engine):
    """``mark_cleaned`` sets ``last_loaded_product`` to None deliberately."""
    repo = CurrentStateRepository("truck_compartment")
    async with session_scope() as s:
        await repo.upsert(s, doc=_compartment_doc())
    async with session_scope() as s:
        row = await repo.set_fields(
            s, TENANT, "TNK-002_C1", last_loaded_product=None, state="clean",
        )
        assert row.last_loaded_product is None
        assert row.state == "clean"
        assert "last_loaded_product" in row.document


async def test_compartment_without_truck_or_compartment_id_is_rejected(engine):
    """No derivable key: fail at the seam, not at COMMIT.

    Letting a NULL primary key reach the database raises ``IntegrityError`` when
    the transaction commits, which attributes the failure to whatever else the
    transaction touched and hides the real cause — a writer that did not pass
    ``doc_id``.
    """
    repo = CurrentStateRepository("truck_compartment")
    async with session_scope() as s:
        with pytest.raises(ValueError, match="cannot derive primary key"):
            await repo.upsert(s, doc=_compartment_doc(truck_id=None))


async def test_fuel_station_falls_back_to_the_bare_station_id(engine):
    """``station_key`` is not a document field, so a fallback is required.

    The bare id is the convention the ATG connector and every seeded document
    use, so it is the safer default for a writer that does not pass ``doc_id``.
    """
    repo = CurrentStateRepository("fuel_station")
    async with session_scope() as s:
        row = await repo.upsert(s, doc=_station_doc())
    assert row.station_key == "FS-001"
    assert row.station_id == "FS-001"


async def test_fuel_station_without_any_id_is_rejected(engine):
    repo = CurrentStateRepository("fuel_station")
    async with session_scope() as s:
        with pytest.raises(ValueError, match="cannot derive primary key"):
            await repo.upsert(s, doc=_station_doc(station_id=None))


# ---------------------------------------------------------------------------
# fuel_station — two id conventions in one index
# ---------------------------------------------------------------------------


async def test_fuel_station_bare_and_composite_ids_coexist(engine):
    """``station_id`` cannot be the primary key.

    ``FuelService.create_station`` writes ``f"{station_id}::{fuel_type}"`` while
    seeded documents and the ATG connector use the bare ``station_id``. Keyed on
    ``station_id`` these two rows would collide and one product's inventory would
    silently disappear.
    """
    repo = CurrentStateRepository("fuel_station")
    async with session_scope() as s:
        await repo.upsert(s, doc=_station_doc(), doc_id="FS-001")
        await repo.upsert(
            s,
            doc=_station_doc(fuel_type="GASOLINE_REG", fuel_grade="GASOLINE_REG"),
            doc_id="FS-001::GASOLINE_REG",
        )

    async with session_scope() as s:
        rows = (await s.execute(select(FuelStationORM))).scalars().all()
    assert sorted(r.station_key for r in rows) == [
        "FS-001", "FS-001::GASOLINE_REG",
    ]
    # Both still resolve to the same station for a "all docs for this station"
    # scan, which is what the indexed non-unique column is for.
    assert {r.station_id for r in rows} == {"FS-001"}
    assert {r.fuel_type for r in rows} == {"DIESEL_2", "GASOLINE_REG"}


async def test_fuel_station_upsert_projects_verbatim(engine):
    repo = CurrentStateRepository("fuel_station")
    doc = _station_doc()
    async with session_scope() as s:
        row = await repo.upsert(s, doc=doc, doc_id="FS-001")
        assert _document_passthrough(row) == doc
        assert row.status == "healthy"

    async with session_scope() as s:
        events = (await s.execute(select(OutboxEventORM))).scalars().all()
    assert events[0].target_index == "fuel_stations"
    assert events[0].aggregate_id == "FS-001"


async def test_fuel_station_consumption_update_lifts_status(engine):
    repo = CurrentStateRepository("fuel_station")
    async with session_scope() as s:
        await repo.upsert(s, doc=_station_doc(), doc_id="FS-001")
    async with session_scope() as s:
        row = await repo.set_fields(
            s, TENANT, "FS-001",
            current_stock_liters=900.0, status="critical", days_until_empty=0.4,
        )
        assert row.status == "critical"
        assert row.document["days_until_empty"] == 0.4


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


async def test_tenant_scoped_delete(engine):
    repo = CurrentStateRepository("customer_tank")
    async with session_scope() as s:
        await repo.upsert(s, doc=_tank_doc())
    async with session_scope() as s:
        assert await repo.delete(s, "other-tenant", "TANK-001") is False
    async with session_scope() as s:
        assert await repo.delete(s, TENANT, "TANK-001") is True
    async with session_scope() as s:
        assert await s.get(CustomerTankORM, "TANK-001") is None


async def test_compartment_delete_uses_the_composite_key(engine):
    repo = CurrentStateRepository("truck_compartment")
    async with session_scope() as s:
        await repo.upsert(s, doc=_compartment_doc())
    async with session_scope() as s:
        assert await repo.delete(s, TENANT, "TNK-002_C1") is True
    async with session_scope() as s:
        assert await s.get(TruckCompartmentORM, "TNK-002_C1") is None
