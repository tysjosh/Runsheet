"""N6: depot delete on the cut-over read path (relational ``depots`` rows).

Staging serves depot get/list from Postgres (``read_hybrid_get``/``list``),
but ``DepotRepository.delete`` removed only the document-store copy, so a
"deleted" depot stayed listed, active and the tenant default. These tests run
with reads cut over and dual-write on, seed the relational row through the
same repository the mirror writes, and check what the API's read path sees
after the delete.
"""
from __future__ import annotations

import pytest

from config.settings import clear_settings_cache, get_settings
from fuel.depot_models import CrossTenantAccessError, DepotRepository
from tests.persistence.test_hybrid_read_cutover import TENANT, _seed
from tests.unit.test_depot_models import _FakeESService


@pytest.fixture
def cut_over(monkeypatch):
    """Reads from Postgres and the relational mirror written (staging)."""
    monkeypatch.setenv("COMMERCE_READ_FROM_POSTGRES", "true")
    monkeypatch.setenv("COMMERCE_DUAL_WRITE_POSTGRES", "true")
    clear_settings_cache()
    settings = get_settings()
    assert settings.commerce_read_from_postgres is True
    assert settings.commerce_dual_write_postgres is True
    yield
    clear_settings_cache()


def _depot_doc(depot_id: str, tenant_id: str = TENANT, **extra):
    doc = {
        "depot_id": depot_id,
        "tenant_id": tenant_id,
        "name": f"Depot {depot_id}",
        "status": "active",
        "is_default": True,
        "fuel_types_supported": ["DIESEL_2"],
        "address": "1 Rack Rd",
        "location_lat": 29.76,
        "location_lon": -95.37,
        "timezone": "America/Chicago",
    }
    doc.update(extra)
    return doc


async def test_delete_removes_the_depot_from_get_and_list(engine, cut_over):
    es = _FakeESService()
    doc = _depot_doc("depot_del")
    es.docs["depot_del"] = dict(doc)
    await _seed("depot", doc)
    await _seed("depot", _depot_doc("depot_keep", is_default=False))
    repo = DepotRepository(es)
    assert await repo.get(TENANT, "depot_del") is not None

    assert await repo.delete(TENANT, "depot_del") is True

    assert await repo.get(TENANT, "depot_del") is None
    listed = {d.depot_id for d in await repo.list_for_tenant(TENANT)}
    assert listed == {"depot_keep"}
    assert "depot_del" not in es.docs


async def test_relational_only_depot_is_deleted(engine, cut_over):
    """A row missing from the document store is still deletable (not 404)."""
    await _seed("depot", _depot_doc("depot_zombie"))
    repo = DepotRepository(_FakeESService())

    assert await repo.delete(TENANT, "depot_zombie") is True

    assert await repo.get(TENANT, "depot_zombie") is None
    assert await repo.list_for_tenant(TENANT) == []


async def test_unknown_id_is_a_miss_on_the_cut_over_path(engine, cut_over):
    repo = DepotRepository(_FakeESService())

    assert await repo.delete(TENANT, "depot_nowhere") is False


async def test_relational_only_depot_of_another_tenant_is_refused(
    engine, cut_over
):
    await _seed("depot", _depot_doc("depot_theirs", tenant_id="other-tenant"))
    repo = DepotRepository(_FakeESService())

    with pytest.raises(CrossTenantAccessError):
        await repo.delete(TENANT, "depot_theirs")

    assert await repo.get("other-tenant", "depot_theirs") is not None
