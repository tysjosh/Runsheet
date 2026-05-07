"""
Unit tests for :mod:`fuel.combinable_group_models`.

Covers Capability 3 / Requirements 3.2.1, 3.2.2, 3.2.3 of the fuel-ops
hardening spec:

* :class:`CombinableGroupEntry` validation — canonicalization of legacy
  aliases, rejection of mis-shaped destination entries, coordinate
  bounds.
* :func:`fuel_grades_compatible` — contamination-matrix-driven
  compatibility rule including legacy aliases, same-product chains, and
  unknown / None inputs.
* :func:`compute_combinable_groups` — Req 3.2.1 proximity+compatibility
  pair test, Req 3.2.2 connected-component output with group_id,
  members, centroid, fuel_grades, estimated_combined_gallons, and the
  partition / transitive-closure properties from Req 3.2.5.
* :class:`CombinableGroupRepository` async CRUD, all tenant-scoped:
    - persist_groups → stamps tenant_id and timestamps, rejects
      cross-tenant payloads, writes to the right index.
    - list_for_tenant → filters by run_id, fuel_grade (with alias
      canonicalization), and min_members; never leaks cross-tenant
      documents.
    - get → returns the model, None when missing, None when owned by
      another tenant (no existence leak).
    - delete → True on success, False when missing, raises
      CrossTenantAccessError when owned by a different tenant.

Validates: Requirements 3.2.1, 3.2.2, 3.2.3.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from fuel.combinable_group_models import (
    CombinableGroup,
    CombinableGroupEntry,
    CombinableGroupMember,
    CombinableGroupRepository,
    CrossTenantAccessError,
    compute_combinable_groups,
    fuel_grades_compatible,
)
from fuel.services.fuel_ops_es_mappings import MVP_COMBINABLE_GROUPS_INDEX


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """In-memory recording mock for ElasticsearchService.

    Implements just enough of the async API the repository consumes:
    ``index_document``, ``search_documents``, ``delete_document``.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.index_calls: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[str] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.index_calls.append({"index": index, "id": doc_id, "doc": dict(document)})
        self.docs[doc_id] = dict(document)
        return {"_id": doc_id, "result": "created"}

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
    if not must and "term" in inner:
        must = [inner]
    for clause in must:
        if "term" in clause:
            for field, expected in clause["term"].items():
                actual = doc.get(field)
                if isinstance(actual, list):
                    if expected not in actual:
                        return False
                elif actual != expected:
                    return False
    return True


def _entry(
    *,
    destination_id: str,
    fuel_grade: str = "DIESEL_2",
    lat: float = 41.5,
    lon: float = -72.5,
    gallons: float = 300.0,
    destination_type: str = "station",
) -> CombinableGroupEntry:
    """Build a valid entry for tests, keeping signatures tight."""

    return CombinableGroupEntry(
        destination_type=destination_type,  # type: ignore[arg-type]
        destination_id=destination_id,
        fuel_grade=fuel_grade,
        estimated_gallons=gallons,
        location_lat=lat,
        location_lon=lon,
    )


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def repo(es: _FakeESService) -> CombinableGroupRepository:
    return CombinableGroupRepository(es_service=es)


# ---------------------------------------------------------------------------
# Entry model validation
# ---------------------------------------------------------------------------


class TestCombinableGroupEntry:
    def test_station_entry_fills_station_id_from_destination_id(self):
        entry = _entry(destination_id="station_42")
        assert entry.station_id == "station_42"
        assert entry.customer_tank_id is None

    def test_customer_tank_entry_fills_customer_tank_id(self):
        entry = _entry(
            destination_id="tank_99", destination_type="customer_tank"
        )
        assert entry.customer_tank_id == "tank_99"
        assert entry.station_id is None

    def test_canonicalizes_legacy_alias(self):
        # LPG → PROPANE
        entry = _entry(destination_id="t1", fuel_grade="LPG")
        assert entry.fuel_grade == "PROPANE"

    def test_rejects_unknown_fuel_grade(self):
        with pytest.raises(ValidationError):
            _entry(destination_id="t1", fuel_grade="UNOBTAINIUM")

    def test_rejects_latitude_out_of_range(self):
        with pytest.raises(ValidationError):
            _entry(destination_id="t1", lat=100.0)

    def test_rejects_longitude_out_of_range(self):
        with pytest.raises(ValidationError):
            _entry(destination_id="t1", lon=200.0)

    def test_rejects_negative_gallons(self):
        with pytest.raises(ValidationError):
            _entry(destination_id="t1", gallons=-5.0)

    def test_station_entry_cannot_carry_customer_tank_id(self):
        with pytest.raises(ValidationError):
            CombinableGroupEntry(
                destination_type="station",
                destination_id="s1",
                customer_tank_id="t1",
                fuel_grade="DIESEL_2",
                estimated_gallons=100.0,
                location_lat=40.0,
                location_lon=-72.0,
            )

    def test_customer_tank_entry_cannot_carry_station_id(self):
        with pytest.raises(ValidationError):
            CombinableGroupEntry(
                destination_type="customer_tank",
                destination_id="t1",
                station_id="s1",
                fuel_grade="DIESEL_2",
                estimated_gallons=100.0,
                location_lat=40.0,
                location_lon=-72.0,
            )

    def test_station_id_must_match_destination_id(self):
        with pytest.raises(ValidationError):
            CombinableGroupEntry(
                destination_type="station",
                destination_id="s1",
                station_id="s2",
                fuel_grade="DIESEL_2",
                estimated_gallons=100.0,
                location_lat=40.0,
                location_lon=-72.0,
            )


# ---------------------------------------------------------------------------
# Compatibility predicate
# ---------------------------------------------------------------------------


class TestFuelGradesCompatible:
    def test_same_product_compatible(self):
        assert fuel_grades_compatible("DIESEL_2", "DIESEL_2") is True
        assert fuel_grades_compatible("PROPANE", "PROPANE") is True

    def test_legacy_aliases_resolve(self):
        # LPG → PROPANE, PMS → GASOLINE_REG
        assert fuel_grades_compatible("LPG", "PROPANE") is True
        assert fuel_grades_compatible("PMS", "GASOLINE_REG") is True

    def test_diesel_and_off_road_diesel_compatible(self):
        # Matrix: DIESEL_2 ↔ OFF_ROAD_DIESEL allowed → compatible.
        assert fuel_grades_compatible("DIESEL_2", "OFF_ROAD_DIESEL") is True

    def test_heating_oil_and_gasoline_not_compatible(self):
        # Matrix: HEATING_OIL → GASOLINE_REG blocked → not compatible.
        assert fuel_grades_compatible("HEATING_OIL", "GASOLINE_REG") is False
        assert fuel_grades_compatible("GASOLINE_REG", "HEATING_OIL") is False

    def test_propane_blocked_with_diesel(self):
        # Matrix: DIESEL_2 → PROPANE is blocked (reverse direction
        # guards the tank against diesel residue). Even though the
        # forward rule is requires_cleaning, the bidirectional block
        # check rejects the pair — a single truck run cannot legally
        # interleave the two.
        assert fuel_grades_compatible("PROPANE", "DIESEL_2") is False
        assert fuel_grades_compatible("DIESEL_2", "PROPANE") is False

    def test_gasoline_requires_cleaning_pair_compatible(self):
        # GASOLINE_REG ↔ GASOLINE_PREM are allowed both ways → compatible.
        assert fuel_grades_compatible("GASOLINE_REG", "GASOLINE_PREM") is True
        # GASOLINE_REG → DIESEL_2 is requires_cleaning in both
        # directions (neither blocked) → compatible.
        assert fuel_grades_compatible("GASOLINE_REG", "DIESEL_2") is True

    def test_def_blocked_with_everything_else(self):
        # DEF is always blocked with any non-DEF product.
        assert fuel_grades_compatible("DEF", "DIESEL_2") is False
        assert fuel_grades_compatible("DEF", "GASOLINE_REG") is False
        assert fuel_grades_compatible("DEF", "PROPANE") is False

    def test_unknown_product_returns_false(self):
        assert fuel_grades_compatible("DIESEL_2", "UNOBTAINIUM") is False
        assert fuel_grades_compatible("UNOBTAINIUM", "DIESEL_2") is False

    def test_none_input_returns_false(self):
        assert fuel_grades_compatible(None, "DIESEL_2") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_combinable_groups
# ---------------------------------------------------------------------------


class TestComputeCombinableGroups:
    def test_empty_input_returns_empty(self):
        assert compute_combinable_groups([]) == []

    def test_single_entry_returns_empty(self):
        # A group of one is not actionable — dropped per design.
        assert compute_combinable_groups([_entry(destination_id="s1")]) == []

    def test_two_nearby_compatible_entries_form_a_group(self):
        # Two stops ~0.3 mi apart in Hartford; same fuel.
        entries = [
            _entry(destination_id="s1", lat=41.76, lon=-72.67),
            _entry(destination_id="s2", lat=41.7642, lon=-72.6745),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert len(groups) == 1
        g = groups[0]
        assert len(g.members) == 2
        assert g.tenant_id == "tenant-A"
        assert g.fuel_grades == ["DIESEL_2"]
        assert g.estimated_combined_gallons == 600.0
        # Centroid equals the arithmetic mean.
        assert math.isclose(
            g.centroid["lat"], (41.76 + 41.7642) / 2, rel_tol=1e-12
        )

    def test_distant_entries_do_not_group(self):
        # Hartford vs Boston — well over 2 miles apart.
        entries = [
            _entry(destination_id="s1", lat=41.76, lon=-72.67),
            _entry(destination_id="s2", lat=42.36, lon=-71.06),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert groups == []

    def test_incompatible_fuel_grades_do_not_group(self):
        # Co-located but HEATING_OIL→GASOLINE is blocked.
        entries = [
            _entry(
                destination_id="s1",
                fuel_grade="HEATING_OIL",
                lat=41.76,
                lon=-72.67,
            ),
            _entry(
                destination_id="s2",
                fuel_grade="GASOLINE_REG",
                lat=41.76,
                lon=-72.67,
            ),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert groups == []

    def test_transitive_chain_forms_single_group(self):
        # A—B pair within radius, B—C pair within radius, A—C across
        # radius. Union-Find must merge all three via B.
        entries = [
            _entry(destination_id="A", lat=41.7600, lon=-72.6700),
            _entry(destination_id="B", lat=41.7700, lon=-72.6700),  # ~0.69 mi
            _entry(destination_id="C", lat=41.7800, lon=-72.6700),  # ~0.69 mi from B
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=1.0, tenant_id="tenant-A"
        )
        assert len(groups) == 1
        assert sorted(m.destination_id for m in groups[0].members) == ["A", "B", "C"]

    def test_two_clusters_produced_separately(self):
        entries = [
            _entry(destination_id="A", lat=41.76, lon=-72.67),
            _entry(destination_id="B", lat=41.7610, lon=-72.6705),
            _entry(destination_id="X", lat=42.36, lon=-71.06),
            _entry(destination_id="Y", lat=42.3610, lon=-71.0605),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert len(groups) == 2
        members_per_group = [
            sorted(m.destination_id for m in g.members) for g in groups
        ]
        assert ["A", "B"] in members_per_group
        assert ["X", "Y"] in members_per_group

    def test_mixed_station_and_customer_tank_members(self):
        entries = [
            _entry(
                destination_id="station_1",
                destination_type="station",
                lat=41.76,
                lon=-72.67,
            ),
            _entry(
                destination_id="tank_1",
                destination_type="customer_tank",
                lat=41.7605,
                lon=-72.6703,
            ),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert len(groups) == 1
        dest_types = {m.destination_type for m in groups[0].members}
        assert dest_types == {"station", "customer_tank"}

    def test_group_id_is_unique_per_group(self):
        entries = [
            _entry(destination_id="A", lat=41.76, lon=-72.67),
            _entry(destination_id="B", lat=41.7605, lon=-72.6703),
            _entry(destination_id="X", lat=42.36, lon=-71.06),
            _entry(destination_id="Y", lat=42.3605, lon=-71.0603),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        ids = [g.group_id for g in groups]
        assert len(set(ids)) == len(ids)
        for gid in ids:
            assert gid.startswith("group_")

    def test_rejects_non_positive_radius(self):
        with pytest.raises(ValueError):
            compute_combinable_groups(
                [_entry(destination_id="s1"), _entry(destination_id="s2")],
                radius_miles=0,
            )
        with pytest.raises(ValueError):
            compute_combinable_groups(
                [_entry(destination_id="s1")], radius_miles=-1.0
            )

    def test_accepts_dict_entries(self):
        entries = [
            {
                "destination_type": "station",
                "destination_id": "s1",
                "fuel_grade": "DIESEL_2",
                "estimated_gallons": 100.0,
                "location_lat": 41.76,
                "location_lon": -72.67,
            },
            {
                "destination_type": "station",
                "destination_id": "s2",
                "fuel_grade": "DIESEL_2",
                "estimated_gallons": 100.0,
                "location_lat": 41.7605,
                "location_lon": -72.6703,
            },
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert len(groups) == 1

    def test_groups_partition_input_entries(self):
        """Req 3.2.5: every entry appears in at most one output group."""

        entries = [
            _entry(destination_id=f"s{i}", lat=41.76 + i * 0.001, lon=-72.67)
            for i in range(6)
        ]
        # Far-away singleton that should not appear in any group.
        entries.append(
            _entry(destination_id="far", lat=45.0, lon=-90.0),
        )
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        seen: List[str] = []
        for g in groups:
            for m in g.members:
                seen.append(m.destination_id)
        # No duplicates across groups.
        assert len(seen) == len(set(seen))
        # Singletons excluded from output.
        assert "far" not in seen

    def test_fuel_grades_deduplicated_in_group(self):
        # Co-located DIESEL_2 + OFF_ROAD_DIESEL (compatible via matrix).
        entries = [
            _entry(
                destination_id="s1",
                fuel_grade="DIESEL_2",
                lat=41.76,
                lon=-72.67,
            ),
            _entry(
                destination_id="s2",
                fuel_grade="OFF_ROAD_DIESEL",
                lat=41.7605,
                lon=-72.6703,
            ),
            _entry(
                destination_id="s3",
                fuel_grade="DIESEL_2",
                lat=41.7610,
                lon=-72.6706,
            ),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert len(groups) == 1
        # Distinct, preserves first-seen order.
        assert groups[0].fuel_grades == ["DIESEL_2", "OFF_ROAD_DIESEL"]

    def test_custom_compatibility_override(self):
        # Agent/tests can inject their own predicate (e.g., allow any).
        allow_any = lambda a, b: True  # noqa: E731
        entries = [
            _entry(
                destination_id="s1",
                fuel_grade="HEATING_OIL",
                lat=41.76,
                lon=-72.67,
            ),
            _entry(
                destination_id="s2",
                fuel_grade="GASOLINE_REG",
                lat=41.7605,
                lon=-72.6703,
            ),
        ]
        groups = compute_combinable_groups(
            entries,
            radius_miles=2.0,
            tenant_id="tenant-A",
            compatibility=allow_any,
        )
        assert len(groups) == 1

    def test_centroid_equals_arithmetic_mean(self):
        entries = [
            _entry(destination_id="A", lat=41.76, lon=-72.67),
            _entry(destination_id="B", lat=41.7620, lon=-72.6710),
            _entry(destination_id="C", lat=41.7640, lon=-72.6720),
        ]
        groups = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )
        assert len(groups) == 1
        g = groups[0]
        mean_lat = sum(e.location_lat for e in entries) / 3
        mean_lon = sum(e.location_lon for e in entries) / 3
        assert math.isclose(g.centroid["lat"], mean_lat, rel_tol=1e-12)
        assert math.isclose(g.centroid["lon"], mean_lon, rel_tol=1e-12)

    def test_group_exposes_centroid_convenience_accessors(self):
        entries = [
            _entry(destination_id="A", lat=41.76, lon=-72.67),
            _entry(destination_id="B", lat=41.7605, lon=-72.6703),
        ]
        g = compute_combinable_groups(
            entries, radius_miles=2.0, tenant_id="tenant-A"
        )[0]
        assert g.group_centroid_lat == g.centroid["lat"]
        assert g.group_centroid_lon == g.centroid["lon"]


# ---------------------------------------------------------------------------
# Repository: persist
# ---------------------------------------------------------------------------


def _build_group(tenant_id: str = "tenant-A", run_id: str = "run-1") -> CombinableGroup:
    """Build a minimal valid group for repository tests."""

    return CombinableGroup(
        group_id="group_001",
        tenant_id=tenant_id,
        run_id=run_id,
        members=[
            CombinableGroupMember(
                destination_type="station",
                destination_id="s1",
                station_id="s1",
                fuel_grade="DIESEL_2",
                product_code="DIESEL_2",
                estimated_gallons=100.0,
                location={"lat": 41.76, "lon": -72.67},
            ),
            CombinableGroupMember(
                destination_type="station",
                destination_id="s2",
                station_id="s2",
                fuel_grade="DIESEL_2",
                product_code="DIESEL_2",
                estimated_gallons=200.0,
                location={"lat": 41.7605, "lon": -72.6703},
            ),
        ],
        fuel_grades=["DIESEL_2"],
        estimated_combined_gallons=300.0,
        centroid={"lat": 41.76025, "lon": -72.67015},
    )


class TestRepositoryPersist:
    async def test_persist_writes_to_canonical_index(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        group = _build_group()
        persisted = await repo.persist_groups("tenant-A", [group])
        assert len(persisted) == 1
        assert es.index_calls[0]["index"] == MVP_COMBINABLE_GROUPS_INDEX
        assert es.index_calls[0]["id"] == "group_001"

    async def test_persist_stamps_tenant_and_timestamps(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        group = _build_group(tenant_id="")
        persisted = await repo.persist_groups("tenant-A", [group])
        assert persisted[0].tenant_id == "tenant-A"
        assert persisted[0].created_at is not None
        assert persisted[0].updated_at is not None
        # Persisted doc carries the same fields.
        doc = es.index_calls[0]["doc"]
        assert doc["tenant_id"] == "tenant-A"
        assert doc["created_at"]
        assert doc["updated_at"]

    async def test_persist_rejects_cross_tenant_payload(
        self, repo: CombinableGroupRepository
    ):
        group = _build_group(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await repo.persist_groups("tenant-A", [group])

    async def test_persist_rejects_blank_tenant_id(
        self, repo: CombinableGroupRepository
    ):
        with pytest.raises(ValueError):
            await repo.persist_groups("  ", [_build_group()])

    async def test_persist_empty_iterable_is_noop(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        out = await repo.persist_groups("tenant-A", [])
        assert out == []
        assert es.index_calls == []


# ---------------------------------------------------------------------------
# Repository: list
# ---------------------------------------------------------------------------


class TestRepositoryList:
    async def test_list_filters_to_requesting_tenant_only(
        self, repo: CombinableGroupRepository
    ):
        g1 = _build_group(tenant_id="tenant-A")
        g2 = _build_group(tenant_id="tenant-B")
        g2.group_id = "group_b"
        g2.members[0].destination_id = "s_b1"
        g2.members[1].destination_id = "s_b2"

        await repo.persist_groups("tenant-A", [g1])
        await repo.persist_groups("tenant-B", [g2])

        got = await repo.list_for_tenant("tenant-A")
        assert [g.group_id for g in got] == ["group_001"]

    async def test_list_filters_by_run_id(
        self, repo: CombinableGroupRepository
    ):
        g1 = _build_group(run_id="run-A")
        g2 = _build_group(run_id="run-B")
        g2.group_id = "group_other"
        await repo.persist_groups("tenant-A", [g1, g2])

        got = await repo.list_for_tenant("tenant-A", run_id="run-A")
        assert [g.group_id for g in got] == ["group_001"]

    async def test_list_filters_by_fuel_grade_with_alias_canonicalization(
        self, repo: CombinableGroupRepository
    ):
        g_diesel = _build_group()
        g_propane = _build_group()
        g_propane.group_id = "group_propane"
        for m in g_propane.members:
            m.fuel_grade = "PROPANE"
            m.product_code = "PROPANE"
        g_propane.fuel_grades = ["PROPANE"]

        await repo.persist_groups("tenant-A", [g_diesel, g_propane])

        # Legacy alias LPG → PROPANE should match the propane group.
        got = await repo.list_for_tenant("tenant-A", fuel_grade="LPG")
        assert [g.group_id for g in got] == ["group_propane"]

    async def test_list_filters_by_min_members(
        self, repo: CombinableGroupRepository
    ):
        g_small = _build_group()
        # Bigger group: append a third member.
        g_big = _build_group()
        g_big.group_id = "group_big"
        g_big.members = list(g_big.members) + [
            CombinableGroupMember(
                destination_type="station",
                destination_id="s3",
                station_id="s3",
                fuel_grade="DIESEL_2",
                product_code="DIESEL_2",
                estimated_gallons=50.0,
                location={"lat": 41.7610, "lon": -72.6710},
            )
        ]
        await repo.persist_groups("tenant-A", [g_small, g_big])

        got = await repo.list_for_tenant("tenant-A", min_members=3)
        assert [g.group_id for g in got] == ["group_big"]

    async def test_list_unknown_fuel_grade_returns_empty_without_hitting_es(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        await repo.persist_groups("tenant-A", [_build_group()])
        search_count_before = len(es.search_calls)
        got = await repo.list_for_tenant("tenant-A", fuel_grade="UNOBTAINIUM")
        assert got == []
        # Short-circuit: no new ES search was issued.
        assert len(es.search_calls) == search_count_before

    async def test_list_rejects_non_positive_size(
        self, repo: CombinableGroupRepository
    ):
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", size=0)
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", size=-1)

    async def test_list_rejects_negative_min_members(
        self, repo: CombinableGroupRepository
    ):
        with pytest.raises(ValueError):
            await repo.list_for_tenant("tenant-A", min_members=-1)

    async def test_list_drops_corrupt_records(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        await repo.persist_groups("tenant-A", [_build_group()])
        # Inject a corrupt doc directly into the mock.
        es.docs["bad"] = {
            "group_id": "bad",
            "tenant_id": "tenant-A",
            # Missing required fields — will fail validation.
        }
        got = await repo.list_for_tenant("tenant-A")
        assert [g.group_id for g in got] == ["group_001"]


# ---------------------------------------------------------------------------
# Repository: get + delete
# ---------------------------------------------------------------------------


class TestRepositoryGetAndDelete:
    async def test_get_returns_model_for_owned_group(
        self, repo: CombinableGroupRepository
    ):
        await repo.persist_groups("tenant-A", [_build_group()])
        got = await repo.get("tenant-A", "group_001")
        assert got is not None
        assert got.group_id == "group_001"
        assert got.tenant_id == "tenant-A"

    async def test_get_returns_none_for_missing_group(
        self, repo: CombinableGroupRepository
    ):
        got = await repo.get("tenant-A", "does-not-exist")
        assert got is None

    async def test_get_returns_none_for_cross_tenant_group(
        self, repo: CombinableGroupRepository
    ):
        await repo.persist_groups(
            "tenant-B", [_build_group(tenant_id="tenant-B")]
        )
        got = await repo.get("tenant-A", "group_001")
        assert got is None

    async def test_delete_owned_group_returns_true(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        await repo.persist_groups("tenant-A", [_build_group()])
        result = await repo.delete("tenant-A", "group_001")
        assert result is True
        assert "group_001" in es.delete_calls
        assert "group_001" not in es.docs

    async def test_delete_missing_group_returns_false(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        result = await repo.delete("tenant-A", "missing")
        assert result is False
        assert es.delete_calls == []

    async def test_delete_cross_tenant_raises(
        self, repo: CombinableGroupRepository, es: _FakeESService
    ):
        await repo.persist_groups(
            "tenant-B", [_build_group(tenant_id="tenant-B")]
        )
        with pytest.raises(CrossTenantAccessError):
            await repo.delete("tenant-A", "group_001")
        assert "group_001" in es.docs

    async def test_get_rejects_empty_group_id(
        self, repo: CombinableGroupRepository
    ):
        with pytest.raises(ValueError):
            await repo.get("tenant-A", "")

    async def test_delete_rejects_empty_ids(
        self, repo: CombinableGroupRepository
    ):
        with pytest.raises(ValueError):
            await repo.delete("", "group_001")
        with pytest.raises(ValueError):
            await repo.delete("tenant-A", "")


# ---------------------------------------------------------------------------
# Repository: construction
# ---------------------------------------------------------------------------


class TestRepositoryConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError):
            CombinableGroupRepository(es_service=None)  # type: ignore[arg-type]

    def test_rejects_empty_index_name(self, es: _FakeESService):
        with pytest.raises(ValueError):
            CombinableGroupRepository(es_service=es, index_name="")

    async def test_defaults_to_canonical_index(self, es: _FakeESService):
        repo = CombinableGroupRepository(es_service=es)
        await repo.list_for_tenant("tenant-A")
        assert es.search_calls[-1]["index"] == MVP_COMBINABLE_GROUPS_INDEX
