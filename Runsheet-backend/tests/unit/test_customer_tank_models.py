"""
Unit tests for :mod:`Agents.support.customer_tank_models`.

Covers Capability 1 / Requirement 1.1 of the fuel-ops hardening spec:

* :class:`CustomerTank` model validation — field shapes, coordinate bounds,
  capacity/level relationship, and fuel-product canonicalization on write.
* :class:`CustomerTankRepository` async CRUD, all tenant-scoped:
    - create → writes to ES with canonicalized ``fuel_product_code``,
      stamps ``updated_at`` / ``created_at``, mints a uuid when id is
      omitted, rejects cross-tenant payloads.
    - get → returns the model, ``None`` when missing, ``None`` when
      owned by another tenant (no existence leak).
    - list_for_tenant → filters by status / customer / fuel_type / etc,
      drops mis-labelled records with a warning, never returns another
      tenant's data.
    - update → tenant-scoped, strips immutable fields, canonicalizes
      fuel_product_code, raises CrossTenantAccessError on cross-tenant
      writes, returns None for missing.
    - delete → returns True on success, False when missing, raises
      CrossTenantAccessError when owned by a different tenant.

The ElasticsearchService dependency is replaced with a recording async mock
so tests never touch a real cluster.

Validates: Requirements 1.1.1, 1.1.6.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from fuel.customer_tank_models import (
    CrossTenantAccessError,
    CustomerTank,
    CustomerTankRepository,
    _safe_model_load,
)
from fuel.services.fuel_ops_es_mappings import CUSTOMER_TANKS_INDEX
from fuel.services.fuel_product_catalog import UnknownFuelProductError


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """In-memory recording mock for ElasticsearchService.

    Stores indexed documents keyed by ``doc_id`` and provides the subset of
    the ``ElasticsearchService`` async API used by
    :class:`CustomerTankRepository`:

        * ``index_document``
        * ``update_document``
        * ``delete_document``
        * ``search_documents``
        * ``get_document`` (unused by the repository but implemented for
          completeness so drop-in use in other tests keeps working)
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.search_calls: List[Dict[str, Any]] = []
        self.index_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []
        # Forced-error hooks for negative-path tests.
        self.search_raises: Optional[Exception] = None

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]) -> Dict[str, Any]:
        self.index_calls.append({"index": index, "id": doc_id, "doc": dict(document)})
        self.docs[doc_id] = dict(document)
        return {"_id": doc_id, "result": "created"}

    async def update_document(self, index: str, doc_id: str, partial_doc: Dict[str, Any]) -> Dict[str, Any]:
        self.update_calls.append({"index": index, "id": doc_id, "partial": dict(partial_doc)})
        existing = self.docs.get(doc_id, {})
        self.docs[doc_id] = {**existing, **partial_doc}
        return {"_id": doc_id, "result": "updated"}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        self.delete_calls.append(doc_id)
        return self.docs.pop(doc_id, None) is not None

    async def search_documents(self, index: str, query: Dict[str, Any], size: int = 100) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        if self.search_raises is not None:
            raise self.search_raises
        matched = [doc for doc in self.docs.values() if _matches_query(doc, query)]
        return {"hits": {"hits": [{"_source": dict(d)} for d in matched[:size]]}}

    async def get_document(self, index: str, doc_id: str) -> Dict[str, Any]:  # pragma: no cover
        doc = self.docs.get(doc_id)
        if doc is None:
            raise KeyError(doc_id)
        return dict(doc)


def _matches_query(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """Minimal ES bool-filter matcher supporting ``term`` clauses."""

    must = query.get("query", {}).get("bool", {}).get("must", [])
    # Support the shorthand {"query": {"term": {...}}} used by _fetch_source.
    if not must and "term" in query.get("query", {}):
        must = [query["query"]]
    for clause in must:
        if "term" in clause:
            for field, expected in clause["term"].items():
                if doc.get(field) != expected:
                    return False
    return True


def _base_tank_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "customer_tank_id": "tank_001",
        "tenant_id": "tenant-A",
        "customer_id": "CUST-1",
        "customer_type": "residential",
        "fuel_type": "propane",
        "fuel_product_code": "PROPANE",
        "capacity_gallons": 500.0,
        "current_level_gallons": 250.0,
        "location_lat": 41.5,
        "location_lon": -72.5,
        "zip_code": "06001",
        "status": "active",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def repo(es: _FakeESService) -> CustomerTankRepository:
    return CustomerTankRepository(es_service=es)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestCustomerTankModel:
    def test_valid_payload_round_trips(self):
        tank = CustomerTank(**_base_tank_kwargs())
        assert tank.customer_tank_id == "tank_001"
        assert tank.fuel_product_code == "PROPANE"
        assert tank.status == "active"

    def test_rejects_latitude_out_of_range(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(location_lat=100.0))

    def test_rejects_longitude_out_of_range(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(location_lon=200.0))

    def test_rejects_negative_capacity(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(capacity_gallons=-10.0))

    def test_rejects_zero_capacity(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(capacity_gallons=0.0))

    def test_rejects_negative_level(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(current_level_gallons=-1.0))

    def test_rejects_level_exceeding_capacity(self):
        with pytest.raises(ValidationError):
            CustomerTank(
                **_base_tank_kwargs(
                    capacity_gallons=100.0, current_level_gallons=200.0
                )
            )

    def test_rejects_unknown_customer_type(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(customer_type="unknown_type"))

    def test_rejects_unknown_fuel_type(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(fuel_type="rocket_fuel"))

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(status="retired"))

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(customer_tank_id="   "))
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(tenant_id=""))

    def test_canonicalizes_legacy_alias(self):
        tank = CustomerTank(**_base_tank_kwargs(fuel_product_code="LPG"))
        assert tank.fuel_product_code == "PROPANE"

    def test_canonicalizes_mixed_case_alias(self):
        tank = CustomerTank(**_base_tank_kwargs(fuel_product_code="  lpg "))
        assert tank.fuel_product_code == "PROPANE"

    def test_rejects_unknown_fuel_product_code(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(fuel_product_code="UNOBTAINIUM"))

    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(extra_unknown_field="x"))

    def test_optional_k_factor_and_use_case_nullable(self):
        tank = CustomerTank(**_base_tank_kwargs())
        assert tank.k_factor is None
        assert tank.use_case is None

    def test_use_case_generator_permitted(self):
        tank = CustomerTank(**_base_tank_kwargs(use_case="generator"))
        assert tank.use_case == "generator"

    def test_rejects_unknown_use_case(self):
        with pytest.raises(ValidationError):
            CustomerTank(**_base_tank_kwargs(use_case="submarine"))


# ---------------------------------------------------------------------------
# Repository: construction + input validation
# ---------------------------------------------------------------------------


class TestRepositoryConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError):
            CustomerTankRepository(es_service=None)  # type: ignore[arg-type]

    def test_rejects_empty_index_name(self, es: _FakeESService):
        with pytest.raises(ValueError):
            CustomerTankRepository(es_service=es, index_name="")

    async def test_defaults_to_canonical_index(self, es: _FakeESService):
        repo = CustomerTankRepository(es_service=es)
        # Exercise a search so we can inspect the index it used.
        await repo.list_for_tenant("tenant-A")
        assert es.search_calls[-1]["index"] == CUSTOMER_TANKS_INDEX


# ---------------------------------------------------------------------------
# Repository: create
# ---------------------------------------------------------------------------


class TestRepositoryCreate:
    async def test_create_persists_canonical_payload(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        tank = CustomerTank(**_base_tank_kwargs())
        result = await repo.create("tenant-A", tank)
        assert result.customer_tank_id == "tank_001"
        assert len(es.index_calls) == 1
        call = es.index_calls[0]
        assert call["index"] == CUSTOMER_TANKS_INDEX
        assert call["id"] == "tank_001"
        assert call["doc"]["tenant_id"] == "tenant-A"
        assert call["doc"]["fuel_product_code"] == "PROPANE"
        # Bookkeeping timestamps are stamped on write.
        assert call["doc"]["created_at"]
        assert call["doc"]["updated_at"]

    async def test_create_canonicalizes_legacy_alias_from_dict(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        payload = _base_tank_kwargs(fuel_product_code="LPG")
        result = await repo.create("tenant-A", payload)
        assert result.fuel_product_code == "PROPANE"
        assert es.index_calls[0]["doc"]["fuel_product_code"] == "PROPANE"

    async def test_create_stamps_tenant_when_omitted(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        payload = _base_tank_kwargs()
        payload.pop("tenant_id")
        result = await repo.create("tenant-A", payload)
        assert result.tenant_id == "tenant-A"

    async def test_create_mints_id_when_omitted(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        payload = _base_tank_kwargs()
        payload.pop("customer_tank_id")
        result = await repo.create("tenant-A", payload)
        assert result.customer_tank_id.startswith("tank_")
        # ES was called with the minted id, not some placeholder.
        assert es.index_calls[0]["id"] == result.customer_tank_id

    async def test_create_rejects_cross_tenant_payload(
        self, repo: CustomerTankRepository
    ):
        payload = _base_tank_kwargs(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await repo.create("tenant-A", payload)

    async def test_create_rejects_unknown_product_code(
        self, repo: CustomerTankRepository
    ):
        payload = _base_tank_kwargs(fuel_product_code="UNOBTAINIUM")
        # Either a ValidationError (Pydantic) or the more specific
        # UnknownFuelProductError (raised by the eager canonicalize step)
        # is acceptable — both are subclasses of ValueError.
        with pytest.raises((ValidationError, UnknownFuelProductError)):
            await repo.create("tenant-A", payload)

    async def test_create_rejects_empty_tenant_id(
        self, repo: CustomerTankRepository
    ):
        with pytest.raises(ValueError):
            await repo.create("  ", _base_tank_kwargs())

    async def test_create_rejects_invalid_tank_type(
        self, repo: CustomerTankRepository
    ):
        with pytest.raises(TypeError):
            await repo.create("tenant-A", 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository: get
# ---------------------------------------------------------------------------


class TestRepositoryGet:
    async def test_get_returns_model_for_owned_tank(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_tank_kwargs())
        got = await repo.get("tenant-A", "tank_001")
        assert got is not None
        assert got.customer_tank_id == "tank_001"
        assert got.tenant_id == "tenant-A"

    async def test_get_returns_none_for_missing_tank(
        self, repo: CustomerTankRepository
    ):
        got = await repo.get("tenant-A", "does-not-exist")
        assert got is None

    async def test_get_returns_none_for_cross_tenant_tank(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        # Tenant B owns it.
        await repo.create("tenant-B", _base_tank_kwargs(tenant_id="tenant-B"))
        # Tenant A sees nothing — no existence leak.
        got = await repo.get("tenant-A", "tank_001")
        assert got is None

    async def test_get_rejects_empty_tank_id(self, repo: CustomerTankRepository):
        with pytest.raises(ValueError):
            await repo.get("tenant-A", "")

    async def test_get_rejects_empty_tenant_id(self, repo: CustomerTankRepository):
        with pytest.raises(ValueError):
            await repo.get("", "tank_001")


# ---------------------------------------------------------------------------
# Repository: list_for_tenant
# ---------------------------------------------------------------------------


class TestRepositoryList:
    async def test_list_filters_to_requesting_tenant_only(
        self, repo: CustomerTankRepository
    ):
        await repo.create("tenant-A", _base_tank_kwargs(customer_tank_id="a1"))
        await repo.create("tenant-A", _base_tank_kwargs(customer_tank_id="a2"))
        await repo.create(
            "tenant-B",
            _base_tank_kwargs(customer_tank_id="b1", tenant_id="tenant-B"),
        )

        got = await repo.list_for_tenant("tenant-A")
        ids = sorted(t.customer_tank_id for t in got)
        assert ids == ["a1", "a2"]

    async def test_list_filters_by_customer_type(self, repo: CustomerTankRepository):
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(customer_tank_id="res1", customer_type="residential"),
        )
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(customer_tank_id="com1", customer_type="commercial"),
        )
        got = await repo.list_for_tenant("tenant-A", customer_type="commercial")
        assert [t.customer_tank_id for t in got] == ["com1"]

    async def test_list_filters_by_fuel_type(self, repo: CustomerTankRepository):
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(
                customer_tank_id="prop",
                fuel_type="propane",
                fuel_product_code="PROPANE",
            ),
        )
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(
                customer_tank_id="oil",
                fuel_type="heating_oil",
                fuel_product_code="HEATING_OIL",
            ),
        )
        got = await repo.list_for_tenant("tenant-A", fuel_type="heating_oil")
        assert [t.customer_tank_id for t in got] == ["oil"]

    async def test_list_filters_by_status(self, repo: CustomerTankRepository):
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(customer_tank_id="on", status="active"),
        )
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(customer_tank_id="off", status="inactive"),
        )
        got = await repo.list_for_tenant("tenant-A", status="active")
        assert [t.customer_tank_id for t in got] == ["on"]

    async def test_list_filters_by_customer_id_and_zip(
        self, repo: CustomerTankRepository
    ):
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(
                customer_tank_id="c1a", customer_id="CUST-1", zip_code="06001"
            ),
        )
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(
                customer_tank_id="c2", customer_id="CUST-2", zip_code="06002"
            ),
        )
        got = await repo.list_for_tenant("tenant-A", customer_id="CUST-1")
        assert [t.customer_tank_id for t in got] == ["c1a"]
        got_zip = await repo.list_for_tenant("tenant-A", zip_code="06002")
        assert [t.customer_tank_id for t in got_zip] == ["c2"]

    async def test_list_drops_corrupt_records_without_raising(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        # Seed one valid record via the repo, then a corrupt one via the mock.
        await repo.create("tenant-A", _base_tank_kwargs(customer_tank_id="good"))
        es.docs["bad"] = {
            "customer_tank_id": "bad",
            "tenant_id": "tenant-A",
            # Missing a raft of required fields — will fail Pydantic
            # validation, the repo should log + drop it.
        }
        got = await repo.list_for_tenant("tenant-A")
        assert [t.customer_tank_id for t in got] == ["good"]

    async def test_list_rejects_non_positive_size(
        self, repo: CustomerTankRepository
    ):
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", size=0)
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", size=-5)


# ---------------------------------------------------------------------------
# Repository: update
# ---------------------------------------------------------------------------


class TestRepositoryUpdate:
    async def test_update_applies_partial_patch(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_tank_kwargs())
        updated = await repo.update(
            "tenant-A", "tank_001", {"current_level_gallons": 123.4}
        )
        assert updated is not None
        assert updated.current_level_gallons == 123.4
        # ES received only the delta plus updated_at, not the whole record.
        partial = es.update_calls[-1]["partial"]
        assert "current_level_gallons" in partial
        assert "updated_at" in partial
        assert "tenant_id" not in partial
        assert "customer_tank_id" not in partial

    async def test_update_canonicalizes_fuel_product_code(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(
                fuel_type="diesel", fuel_product_code="DIESEL_2"
            ),
        )
        updated = await repo.update(
            "tenant-A", "tank_001", {"fuel_product_code": "AGO"}
        )
        assert updated is not None
        assert updated.fuel_product_code == "DIESEL_2"

    async def test_update_rejects_cross_tenant_write(
        self, repo: CustomerTankRepository
    ):
        await repo.create(
            "tenant-B",
            _base_tank_kwargs(tenant_id="tenant-B"),
        )
        with pytest.raises(CrossTenantAccessError):
            await repo.update("tenant-A", "tank_001", {"current_level_gallons": 1.0})

    async def test_update_returns_none_for_missing_tank(
        self, repo: CustomerTankRepository
    ):
        got = await repo.update("tenant-A", "missing", {"current_level_gallons": 1.0})
        assert got is None

    async def test_update_strips_immutable_fields_silently(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_tank_kwargs())
        patch = {
            "tenant_id": "tenant-X",  # ignored
            "customer_tank_id": "tank_999",  # ignored
            "created_at": "1970-01-01T00:00:00+00:00",  # ignored
            "current_level_gallons": 50.0,  # applied
        }
        updated = await repo.update("tenant-A", "tank_001", patch)
        assert updated is not None
        assert updated.tenant_id == "tenant-A"
        assert updated.customer_tank_id == "tank_001"
        assert updated.current_level_gallons == 50.0
        partial = es.update_calls[-1]["partial"]
        assert partial.keys() == {"current_level_gallons", "updated_at"}

    async def test_update_no_op_patch_returns_current_model(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_tank_kwargs())
        before_updates = len(es.update_calls)
        # Only immutable fields → clean_patch is empty → no ES write.
        updated = await repo.update(
            "tenant-A",
            "tank_001",
            {"tenant_id": "tenant-X", "customer_tank_id": "tank_999"},
        )
        assert updated is not None
        assert updated.customer_tank_id == "tank_001"
        assert len(es.update_calls) == before_updates

    async def test_update_rejects_invalid_capacity_level_relationship(
        self, repo: CustomerTankRepository
    ):
        await repo.create(
            "tenant-A",
            _base_tank_kwargs(capacity_gallons=100.0, current_level_gallons=50.0),
        )
        with pytest.raises(ValidationError):
            await repo.update(
                "tenant-A", "tank_001", {"current_level_gallons": 500.0}
            )

    async def test_update_rejects_non_dict_patch(
        self, repo: CustomerTankRepository
    ):
        await repo.create("tenant-A", _base_tank_kwargs())
        with pytest.raises(TypeError):
            await repo.update("tenant-A", "tank_001", "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository: delete
# ---------------------------------------------------------------------------


class TestRepositoryDelete:
    async def test_delete_owned_tank_returns_true(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_tank_kwargs())
        result = await repo.delete("tenant-A", "tank_001")
        assert result is True
        assert "tank_001" in es.delete_calls
        assert "tank_001" not in es.docs

    async def test_delete_missing_tank_returns_false(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        result = await repo.delete("tenant-A", "missing")
        assert result is False
        # Repository never reached ES delete because the pre-check failed.
        assert es.delete_calls == []

    async def test_delete_cross_tenant_raises(
        self, repo: CustomerTankRepository, es: _FakeESService
    ):
        await repo.create("tenant-B", _base_tank_kwargs(tenant_id="tenant-B"))
        with pytest.raises(CrossTenantAccessError):
            await repo.delete("tenant-A", "tank_001")
        # Tank still exists because the delete was blocked.
        assert "tank_001" in es.docs

    async def test_delete_rejects_empty_ids(self, repo: CustomerTankRepository):
        with pytest.raises(ValueError):
            await repo.delete("", "tank_001")
        with pytest.raises(ValueError):
            await repo.delete("tenant-A", "")


# ---------------------------------------------------------------------------
# _safe_model_load: tolerate ES-only / dual-write convenience fields
# ---------------------------------------------------------------------------


class TestSafeModelLoadStripsExtraFields:
    """Regression: a customer-tank doc carrying ES-only fields must load.

    The ``customer_tanks`` mapping includes a ``location`` geo_point
    convenience field absent from the strict ``CustomerTank`` model
    (``extra="forbid"``). Before the strip fix the geo_point caused every
    seeded row to be dropped on read, leaving the Customer Tanks tab empty
    even though the data existed. ``_safe_model_load`` now drops unknown
    keys before validation.
    """

    def test_strips_nested_location_geo_point(self):
        source = _base_tank_kwargs()
        source["location"] = {"lat": 41.5, "lon": -72.5}
        model = _safe_model_load(source)
        assert model is not None
        assert model.customer_tank_id == "tank_001"
        assert model.location_lat == 41.5
        assert model.location_lon == -72.5

    def test_drops_record_missing_required_fields(self):
        # A genuinely malformed row (missing required coords) is dropped,
        # not raised, so one bad record never kills the whole list.
        source = _base_tank_kwargs()
        del source["location_lat"]
        del source["location_lon"]
        assert _safe_model_load(source) is None
