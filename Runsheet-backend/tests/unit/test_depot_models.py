"""
Unit tests for :mod:`fuel.depot_models`.

Covers Capability 2 / Requirement 2.2.1 of the fuel-ops hardening spec:

* :class:`Depot` model validation — field shapes, coordinate bounds,
  IANA-timezone validation, fuel-product canonicalization in
  ``fuel_types_supported``, and rejection of blank/whitespace required
  strings.
* :class:`DepotRepository` async CRUD, all tenant-scoped:
    - create → writes to ES with canonicalized ``fuel_types_supported``,
      stamps ``updated_at`` / ``created_at``, mints a uuid when id is
      omitted, rejects cross-tenant payloads.
    - get → returns the model, ``None`` when missing, ``None`` when
      owned by another tenant (no existence leak).
    - list_for_tenant → filters by status / fuel_type with alias
      canonicalization, drops mis-labelled records with a warning,
      never returns another tenant's data.
    - update → tenant-scoped, strips immutable fields, canonicalizes
      fuel_types_supported, raises CrossTenantAccessError on cross-tenant
      writes, returns None for missing.
    - delete → returns True on success, False when missing, raises
      CrossTenantAccessError when owned by a different tenant.

The ElasticsearchService dependency is replaced with a recording async mock
so tests never touch a real cluster.

Validates: Requirements 2.2.1.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from fuel.depot_models import (
    CrossTenantAccessError,
    Depot,
    DepotRepository,
    _safe_model_load,
)
from fuel.services.fuel_ops_es_mappings import DEPOTS_INDEX
from fuel.services.fuel_product_catalog import UnknownFuelProductError


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """In-memory recording mock for ElasticsearchService.

    Stores indexed documents keyed by ``doc_id`` and provides the subset of
    the ``ElasticsearchService`` async API used by
    :class:`DepotRepository`:

        * ``index_document``
        * ``update_document``
        * ``delete_document``
        * ``search_documents``
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.search_calls: List[Dict[str, Any]] = []
        self.index_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.index_calls.append({"index": index, "id": doc_id, "doc": dict(document)})
        self.docs[doc_id] = dict(document)
        return {"_id": doc_id, "result": "created"}

    async def update_document(
        self, index: str, doc_id: str, partial_doc: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.update_calls.append(
            {"index": index, "id": doc_id, "partial": dict(partial_doc)}
        )
        existing = self.docs.get(doc_id, {})
        self.docs[doc_id] = {**existing, **partial_doc}
        return {"_id": doc_id, "result": "updated"}

    async def delete_document(self, index: str, doc_id: str) -> bool:
        self.delete_calls.append(doc_id)
        return self.docs.pop(doc_id, None) is not None

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 100
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        matched = [doc for doc in self.docs.values() if _matches_query(doc, query)]
        return {"hits": {"hits": [{"_source": dict(d)} for d in matched[:size]]}}


def _matches_query(doc: Dict[str, Any], query: Dict[str, Any]) -> bool:
    """Minimal ES bool-filter matcher supporting ``term`` clauses."""

    inner = query.get("query", {})
    must = inner.get("bool", {}).get("must", [])
    # Support the shorthand {"query": {"term": {...}}} used by _fetch_source.
    if not must and "term" in inner:
        must = [inner]
    for clause in must:
        if "term" in clause:
            for field, expected in clause["term"].items():
                actual = doc.get(field)
                # ``fuel_types_supported`` is an array; a term clause on it
                # matches when the array contains the expected value.
                if isinstance(actual, list):
                    if expected not in actual:
                        return False
                elif actual != expected:
                    return False
    return True


def _base_depot_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "depot_id": "depot_001",
        "tenant_id": "tenant-A",
        "name": "Central Loading Rack",
        "location_lat": 41.5,
        "location_lon": -72.5,
        "address": "100 Rack Road, Hartford, CT",
        "timezone": "America/New_York",
        "fuel_types_supported": ["DIESEL_2", "PROPANE"],
        "status": "active",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def repo(es: _FakeESService) -> DepotRepository:
    return DepotRepository(es_service=es)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestDepotModel:
    def test_valid_payload_round_trips(self):
        depot = Depot(**_base_depot_kwargs())
        assert depot.depot_id == "depot_001"
        assert depot.tenant_id == "tenant-A"
        assert depot.status == "active"
        assert depot.fuel_types_supported == ["DIESEL_2", "PROPANE"]

    def test_rejects_latitude_out_of_range(self):
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(location_lat=100.0))
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(location_lat=-100.0))

    def test_rejects_longitude_out_of_range(self):
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(location_lon=200.0))
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(location_lon=-200.0))

    def test_accepts_boundary_coordinates(self):
        # Boundary values should be accepted since they are valid WGS84.
        Depot(**_base_depot_kwargs(location_lat=90.0, location_lon=-180.0))
        Depot(**_base_depot_kwargs(location_lat=-90.0, location_lon=180.0))

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(name="   "))
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(address=""))
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(tenant_id="   "))
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(depot_id=""))

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(status="retired"))

    def test_rejects_invalid_iana_timezone(self):
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(timezone="Not/A/Zone"))
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(timezone="EST/EDT"))

    def test_accepts_valid_iana_timezones(self):
        for tz in ["America/Chicago", "UTC", "Europe/London", "Pacific/Auckland"]:
            Depot(**_base_depot_kwargs(timezone=tz))

    def test_canonicalizes_legacy_fuel_aliases(self):
        depot = Depot(
            **_base_depot_kwargs(fuel_types_supported=["LPG", "AGO", "PMS"])
        )
        # LPG→PROPANE, AGO→DIESEL_2, PMS→GASOLINE_REG
        assert depot.fuel_types_supported == ["PROPANE", "DIESEL_2", "GASOLINE_REG"]

    def test_canonicalizes_deduplicates_fuel_types(self):
        # LPG and PROPANE both canonicalize to PROPANE — the list should
        # be collapsed to a single entry, preserving first-seen order.
        depot = Depot(
            **_base_depot_kwargs(fuel_types_supported=["LPG", "PROPANE", "DIESEL_2"])
        )
        assert depot.fuel_types_supported == ["PROPANE", "DIESEL_2"]

    def test_canonicalizes_case_insensitive(self):
        depot = Depot(**_base_depot_kwargs(fuel_types_supported=["  lpg ", "diesel_2"]))
        assert depot.fuel_types_supported == ["PROPANE", "DIESEL_2"]

    def test_rejects_unknown_fuel_product(self):
        with pytest.raises((ValidationError, UnknownFuelProductError)):
            Depot(**_base_depot_kwargs(fuel_types_supported=["UNOBTAINIUM"]))

    def test_empty_fuel_types_supported_allowed(self):
        depot = Depot(**_base_depot_kwargs(fuel_types_supported=[]))
        assert depot.fuel_types_supported == []

    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            Depot(**_base_depot_kwargs(extra_unknown_field="x"))

    def test_default_status_is_active(self):
        kwargs = _base_depot_kwargs()
        kwargs.pop("status")
        depot = Depot(**kwargs)
        assert depot.status == "active"


# ---------------------------------------------------------------------------
# Repository: construction + input validation
# ---------------------------------------------------------------------------


class TestRepositoryConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError):
            DepotRepository(es_service=None)  # type: ignore[arg-type]

    def test_rejects_empty_index_name(self, es: _FakeESService):
        with pytest.raises(ValueError):
            DepotRepository(es_service=es, index_name="")

    @pytest.mark.asyncio
    async def test_defaults_to_canonical_index(self, es: _FakeESService):
        repo = DepotRepository(es_service=es)
        # Exercise a search so we can inspect the index it used.
        await repo.list_for_tenant("tenant-A")
        assert es.search_calls[-1]["index"] == DEPOTS_INDEX


# ---------------------------------------------------------------------------
# Repository: create
# ---------------------------------------------------------------------------


class TestRepositoryCreate:
    @pytest.mark.asyncio
    async def test_create_persists_canonical_payload(
        self, repo: DepotRepository, es: _FakeESService
    ):
        depot = Depot(**_base_depot_kwargs())
        result = await repo.create("tenant-A", depot)
        assert result.depot_id == "depot_001"
        assert len(es.index_calls) == 1
        call = es.index_calls[0]
        assert call["index"] == DEPOTS_INDEX
        assert call["id"] == "depot_001"
        assert call["doc"]["tenant_id"] == "tenant-A"
        assert call["doc"]["fuel_types_supported"] == ["DIESEL_2", "PROPANE"]
        # Bookkeeping timestamps are stamped on write.
        assert call["doc"]["created_at"]
        assert call["doc"]["updated_at"]

    @pytest.mark.asyncio
    async def test_create_canonicalizes_legacy_aliases_from_dict(
        self, repo: DepotRepository, es: _FakeESService
    ):
        payload = _base_depot_kwargs(fuel_types_supported=["LPG", "AGO"])
        result = await repo.create("tenant-A", payload)
        assert result.fuel_types_supported == ["PROPANE", "DIESEL_2"]
        assert es.index_calls[0]["doc"]["fuel_types_supported"] == [
            "PROPANE",
            "DIESEL_2",
        ]

    @pytest.mark.asyncio
    async def test_create_stamps_tenant_when_omitted(
        self, repo: DepotRepository, es: _FakeESService
    ):
        payload = _base_depot_kwargs()
        payload.pop("tenant_id")
        result = await repo.create("tenant-A", payload)
        assert result.tenant_id == "tenant-A"

    @pytest.mark.asyncio
    async def test_create_mints_id_when_omitted(
        self, repo: DepotRepository, es: _FakeESService
    ):
        payload = _base_depot_kwargs()
        payload.pop("depot_id")
        result = await repo.create("tenant-A", payload)
        assert result.depot_id.startswith("depot_")
        assert es.index_calls[0]["id"] == result.depot_id

    @pytest.mark.asyncio
    async def test_create_rejects_cross_tenant_payload(self, repo: DepotRepository):
        payload = _base_depot_kwargs(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await repo.create("tenant-A", payload)

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_fuel_product(self, repo: DepotRepository):
        payload = _base_depot_kwargs(fuel_types_supported=["UNOBTAINIUM"])
        with pytest.raises((ValidationError, UnknownFuelProductError)):
            await repo.create("tenant-A", payload)

    @pytest.mark.asyncio
    async def test_create_rejects_empty_tenant_id(self, repo: DepotRepository):
        with pytest.raises(ValueError):
            await repo.create("  ", _base_depot_kwargs())

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_depot_type(self, repo: DepotRepository):
        with pytest.raises(TypeError):
            await repo.create("tenant-A", 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository: get
# ---------------------------------------------------------------------------


class TestRepositoryGet:
    @pytest.mark.asyncio
    async def test_get_returns_model_for_owned_depot(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_depot_kwargs())
        got = await repo.get("tenant-A", "depot_001")
        assert got is not None
        assert got.depot_id == "depot_001"
        assert got.tenant_id == "tenant-A"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_depot(self, repo: DepotRepository):
        got = await repo.get("tenant-A", "does-not-exist")
        assert got is None

    @pytest.mark.asyncio
    async def test_get_returns_none_for_cross_tenant_depot(
        self, repo: DepotRepository, es: _FakeESService
    ):
        # Tenant B owns it.
        await repo.create("tenant-B", _base_depot_kwargs(tenant_id="tenant-B"))
        # Tenant A sees nothing — no existence leak.
        got = await repo.get("tenant-A", "depot_001")
        assert got is None

    @pytest.mark.asyncio
    async def test_get_rejects_empty_depot_id(self, repo: DepotRepository):
        with pytest.raises(ValueError):
            await repo.get("tenant-A", "")

    @pytest.mark.asyncio
    async def test_get_rejects_empty_tenant_id(self, repo: DepotRepository):
        with pytest.raises(ValueError):
            await repo.get("", "depot_001")


# ---------------------------------------------------------------------------
# Repository: list_for_tenant
# ---------------------------------------------------------------------------


class TestRepositoryList:
    @pytest.mark.asyncio
    async def test_list_filters_to_requesting_tenant_only(
        self, repo: DepotRepository
    ):
        await repo.create("tenant-A", _base_depot_kwargs(depot_id="a1"))
        await repo.create("tenant-A", _base_depot_kwargs(depot_id="a2"))
        await repo.create(
            "tenant-B",
            _base_depot_kwargs(depot_id="b1", tenant_id="tenant-B"),
        )

        got = await repo.list_for_tenant("tenant-A")
        ids = sorted(d.depot_id for d in got)
        assert ids == ["a1", "a2"]

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self, repo: DepotRepository):
        await repo.create(
            "tenant-A", _base_depot_kwargs(depot_id="on", status="active")
        )
        await repo.create(
            "tenant-A", _base_depot_kwargs(depot_id="off", status="inactive")
        )
        got = await repo.list_for_tenant("tenant-A", status="active")
        assert [d.depot_id for d in got] == ["on"]

    @pytest.mark.asyncio
    async def test_list_filters_by_fuel_type_with_alias_canonicalization(
        self, repo: DepotRepository
    ):
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(
                depot_id="prop", fuel_types_supported=["PROPANE"]
            ),
        )
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(
                depot_id="diesel", fuel_types_supported=["DIESEL_2"]
            ),
        )
        # Legacy alias LPG→PROPANE should match the propane depot.
        got = await repo.list_for_tenant("tenant-A", fuel_type="LPG")
        assert [d.depot_id for d in got] == ["prop"]

    @pytest.mark.asyncio
    async def test_list_unknown_fuel_type_returns_empty(self, repo: DepotRepository):
        await repo.create("tenant-A", _base_depot_kwargs())
        got = await repo.list_for_tenant("tenant-A", fuel_type="UNOBTAINIUM")
        assert got == []

    @pytest.mark.asyncio
    async def test_list_drops_corrupt_records_without_raising(
        self, repo: DepotRepository, es: _FakeESService
    ):
        # Seed one valid record via the repo, then a corrupt one via the mock.
        await repo.create("tenant-A", _base_depot_kwargs(depot_id="good"))
        es.docs["bad"] = {
            "depot_id": "bad",
            "tenant_id": "tenant-A",
            # Missing a raft of required fields — will fail Pydantic
            # validation, the repo should log + drop it.
        }
        got = await repo.list_for_tenant("tenant-A")
        assert [d.depot_id for d in got] == ["good"]

    @pytest.mark.asyncio
    async def test_list_rejects_non_positive_size(self, repo: DepotRepository):
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", size=0)
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", size=-5)


# ---------------------------------------------------------------------------
# Repository: update
# ---------------------------------------------------------------------------


class TestRepositoryUpdate:
    @pytest.mark.asyncio
    async def test_update_applies_partial_patch(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_depot_kwargs())
        updated = await repo.update(
            "tenant-A", "depot_001", {"name": "Renamed Depot"}
        )
        assert updated is not None
        assert updated.name == "Renamed Depot"
        partial = es.update_calls[-1]["partial"]
        assert "name" in partial
        assert "updated_at" in partial
        assert "tenant_id" not in partial
        assert "depot_id" not in partial

    @pytest.mark.asyncio
    async def test_update_canonicalizes_fuel_types(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_depot_kwargs())
        updated = await repo.update(
            "tenant-A",
            "depot_001",
            {"fuel_types_supported": ["LPG", "PMS"]},
        )
        assert updated is not None
        assert updated.fuel_types_supported == ["PROPANE", "GASOLINE_REG"]

    @pytest.mark.asyncio
    async def test_update_validates_iana_timezone(self, repo: DepotRepository):
        await repo.create("tenant-A", _base_depot_kwargs())
        with pytest.raises(ValidationError):
            await repo.update(
                "tenant-A", "depot_001", {"timezone": "Not/A/Zone"}
            )

    @pytest.mark.asyncio
    async def test_update_rejects_cross_tenant_write(self, repo: DepotRepository):
        await repo.create("tenant-B", _base_depot_kwargs(tenant_id="tenant-B"))
        with pytest.raises(CrossTenantAccessError):
            await repo.update("tenant-A", "depot_001", {"name": "x"})

    @pytest.mark.asyncio
    async def test_update_returns_none_for_missing_depot(
        self, repo: DepotRepository
    ):
        got = await repo.update("tenant-A", "missing", {"name": "x"})
        assert got is None

    @pytest.mark.asyncio
    async def test_update_strips_immutable_fields_silently(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_depot_kwargs())
        patch = {
            "tenant_id": "tenant-X",  # ignored
            "depot_id": "depot_999",  # ignored
            "created_at": "1970-01-01T00:00:00+00:00",  # ignored
            "name": "Renamed",  # applied
        }
        updated = await repo.update("tenant-A", "depot_001", patch)
        assert updated is not None
        assert updated.tenant_id == "tenant-A"
        assert updated.depot_id == "depot_001"
        assert updated.name == "Renamed"
        partial = es.update_calls[-1]["partial"]
        assert partial.keys() == {"name", "updated_at"}

    @pytest.mark.asyncio
    async def test_update_no_op_patch_returns_current_model(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_depot_kwargs())
        before_updates = len(es.update_calls)
        # Only immutable fields → clean_patch is empty → no ES write.
        updated = await repo.update(
            "tenant-A",
            "depot_001",
            {"tenant_id": "tenant-X", "depot_id": "depot_999"},
        )
        assert updated is not None
        assert updated.depot_id == "depot_001"
        assert len(es.update_calls) == before_updates

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_coordinates(self, repo: DepotRepository):
        await repo.create("tenant-A", _base_depot_kwargs())
        with pytest.raises(ValidationError):
            await repo.update("tenant-A", "depot_001", {"location_lat": 100.0})
        with pytest.raises(ValidationError):
            await repo.update("tenant-A", "depot_001", {"location_lon": 200.0})

    @pytest.mark.asyncio
    async def test_update_rejects_non_dict_patch(self, repo: DepotRepository):
        await repo.create("tenant-A", _base_depot_kwargs())
        with pytest.raises(TypeError):
            await repo.update("tenant-A", "depot_001", "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository: delete
# ---------------------------------------------------------------------------


class TestRepositoryDelete:
    @pytest.mark.asyncio
    async def test_delete_owned_depot_returns_true(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_depot_kwargs())
        result = await repo.delete("tenant-A", "depot_001")
        assert result is True
        assert "depot_001" in es.delete_calls
        assert "depot_001" not in es.docs

    @pytest.mark.asyncio
    async def test_delete_missing_depot_returns_false(
        self, repo: DepotRepository, es: _FakeESService
    ):
        result = await repo.delete("tenant-A", "missing")
        assert result is False
        # Repository never reached ES delete because the pre-check failed.
        assert es.delete_calls == []

    @pytest.mark.asyncio
    async def test_delete_cross_tenant_raises(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create("tenant-B", _base_depot_kwargs(tenant_id="tenant-B"))
        with pytest.raises(CrossTenantAccessError):
            await repo.delete("tenant-A", "depot_001")
        # Depot still exists because the delete was blocked.
        assert "depot_001" in es.docs

    @pytest.mark.asyncio
    async def test_delete_rejects_empty_ids(self, repo: DepotRepository):
        with pytest.raises(ValueError):
            await repo.delete("", "depot_001")
        with pytest.raises(ValueError):
            await repo.delete("tenant-A", "")


# ---------------------------------------------------------------------------
# Single-default-per-tenant semantics (is_default)
# ---------------------------------------------------------------------------


class TestDefaultDepotFlag:
    """is_default enforces at most one default depot per tenant."""

    @pytest.mark.asyncio
    async def test_create_default_clears_other_defaults(
        self, repo: DepotRepository, es: _FakeESService
    ):
        # First default depot.
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(depot_id="depot_001", is_default=True),
        )
        assert es.docs["depot_001"]["is_default"] is True

        # Creating a second default clears the first.
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(depot_id="depot_002", is_default=True),
        )
        assert es.docs["depot_002"]["is_default"] is True
        assert es.docs["depot_001"]["is_default"] is False

    @pytest.mark.asyncio
    async def test_update_default_clears_other_defaults(
        self, repo: DepotRepository, es: _FakeESService
    ):
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(depot_id="depot_001", is_default=True),
        )
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(depot_id="depot_002", is_default=False),
        )

        # Promote depot_002 to default via update.
        await repo.update("tenant-A", "depot_002", {"is_default": True})

        assert es.docs["depot_002"]["is_default"] is True
        assert es.docs["depot_001"]["is_default"] is False

    @pytest.mark.asyncio
    async def test_default_is_scoped_per_tenant(
        self, repo: DepotRepository, es: _FakeESService
    ):
        # Two tenants can each have their own default depot.
        await repo.create(
            "tenant-A",
            _base_depot_kwargs(depot_id="depot_a", tenant_id="tenant-A", is_default=True),
        )
        await repo.create(
            "tenant-B",
            _base_depot_kwargs(depot_id="depot_b", tenant_id="tenant-B", is_default=True),
        )

        # Neither default was cleared because they belong to different tenants.
        assert es.docs["depot_a"]["is_default"] is True
        assert es.docs["depot_b"]["is_default"] is True


# ---------------------------------------------------------------------------
# _safe_model_load: tolerate ES-only / dual-write convenience fields
# ---------------------------------------------------------------------------


class TestSafeModelLoadStripsExtraFields:
    """Regression: a depot doc carrying ES-only fields must still load.

    The persisted ``depots`` document (whether served from ES or the
    Postgres source-of-truth) historically carried a ``location``
    geo_point convenience field that the strict ``Depot`` model
    (``extra="forbid"``) does not define. Before the strip fix, every such
    row was silently dropped on read, leaving the Depots tab empty even
    though the data existed. ``_safe_model_load`` now drops unknown keys
    before validation so a row written with the geo_point survives.
    """

    def test_strips_nested_location_geo_point(self):
        source = {
            "depot_id": "DEPOT-HOU",
            "tenant_id": "demo-tenant",
            "name": "Houston Main Depot",
            "location_lat": 29.7604,
            "location_lon": -95.3698,
            "address": "1500 Industrial Blvd, Houston, TX 77020",
            "timezone": "America/Chicago",
            "fuel_types_supported": ["DIESEL_2", "GASOLINE_REG"],
            "status": "active",
            # ES-only convenience field absent from the strict model.
            "location": {"lat": 29.7604, "lon": -95.3698},
        }
        model = _safe_model_load(source)
        assert model is not None
        assert model.depot_id == "DEPOT-HOU"
        assert model.location_lat == 29.7604
        assert model.location_lon == -95.3698

    def test_drops_record_missing_required_flat_coords(self):
        # A document that lacks the required flat coords (and only has the
        # nested location) cannot be salvaged by stripping — it is dropped
        # rather than raising, so one corrupt row never kills the list.
        source = {
            "depot_id": "DEPOT-BAD",
            "tenant_id": "demo-tenant",
            "name": "Broken Depot",
            "address": "nowhere",
            "timezone": "America/Chicago",
            "fuel_types_supported": [],
            "status": "active",
            "location": {"lat": 1.0, "lon": 2.0},
        }
        assert _safe_model_load(source) is None

    def test_canonicalizes_legacy_fuel_aliases_on_load(self):
        source = {
            "depot_id": "DEPOT-NG",
            "tenant_id": "demo-tenant",
            "name": "Legacy Depot",
            "location_lat": 6.5,
            "location_lon": 3.3,
            "address": "Lagos",
            "timezone": "Africa/Lagos",
            "fuel_types_supported": ["AGO", "PMS"],
            "status": "active",
        }
        model = _safe_model_load(source)
        assert model is not None
        assert model.fuel_types_supported == ["DIESEL_2", "GASOLINE_REG"]
