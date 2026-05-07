"""
Unit tests for :mod:`fuel.compartment_state_models`.

Covers Capability 7 / Requirement 7.1.1 of the fuel-ops hardening spec:

* Extension of the ``truck_compartments`` ES mapping with the four
  additive state fields (``last_loaded_product``, ``last_loaded_at``,
  ``last_cleaned_at``, ``state``) while preserving every existing field.
* :class:`CompartmentState` model validation — including canonicalization
  of ``last_loaded_product`` so legacy NG aliases (``LPG``) land as US
  codes (``PROPANE``).
* :class:`CompartmentStateRepository` atomic updates:

    - ``get`` returns ``None`` for missing or cross-tenant docs (no
      existence leak).
    - ``mark_loaded`` canonicalizes the product, stamps
      ``last_loaded_at``, and transitions ``state`` to ``loaded``,
      asserting ``_seq_no`` / ``_primary_term`` via OCC.
    - ``mark_cleaned`` clears ``last_loaded_product``, stamps
      ``last_cleaned_at``, and transitions ``state`` to ``clean``.
    - ``mark_needs_cleaning`` transitions ``state`` without mutating
      timestamps.
    - Cross-tenant writes raise :class:`CrossTenantCompartmentAccessError`.
    - Missing compartments raise :class:`CompartmentNotFoundError`.
    - Repeated ``version_conflict`` surfaces as
      :class:`CompartmentStateConflictError` after the bounded retry
      budget is exhausted.
    - A successful retry after a single conflict returns a refreshed
      model.

Validates: Requirements 7.1.1, 7.1.2, 7.1.3.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from Agents.support.mvp_es_mappings import (
    TRUCK_COMPARTMENTS_INDEX,
    TRUCK_COMPARTMENTS_MAPPING,
)
from fuel.compartment_state_models import (
    CompartmentNotFoundError,
    CompartmentState,
    CompartmentStateConflictError,
    CompartmentStateRepository,
    CrossTenantCompartmentAccessError,
)


# ---------------------------------------------------------------------------
# ES mapping shape
# ---------------------------------------------------------------------------


class TestTruckCompartmentsMapping:
    """The additive Task 6.1 fields are present without breaking existing fields."""

    def test_adds_state_tracking_fields(self):
        props = TRUCK_COMPARTMENTS_MAPPING["mappings"]["properties"]
        assert props["last_loaded_product"] == {"type": "keyword"}
        assert props["last_loaded_at"] == {"type": "date"}
        assert props["last_cleaned_at"] == {"type": "date"}
        assert props["state"] == {"type": "keyword"}

    def test_preserves_existing_fields(self):
        props = TRUCK_COMPARTMENTS_MAPPING["mappings"]["properties"]
        # Every field that existed before Task 6.1 must still be present.
        for field in (
            "compartment_id",
            "truck_id",
            "capacity_liters",
            "allowed_grades",
            "position_index",
            "depot_city",
            "depot_location",
            "latitude",
            "longitude",
            "tenant_id",
            "updated_at",
            "created_at",
        ):
            assert field in props, f"{field!r} missing from mapping"

    def test_mapping_is_strict(self):
        assert TRUCK_COMPARTMENTS_MAPPING["mappings"]["dynamic"] == "strict"


# ---------------------------------------------------------------------------
# CompartmentState model
# ---------------------------------------------------------------------------


class TestCompartmentStateModel:
    def test_defaults_to_clean(self):
        state = CompartmentState(
            compartment_id="c1",
            truck_id="T1",
            tenant_id="tenant-A",
        )
        assert state.state == "clean"
        assert state.last_loaded_product is None
        assert state.last_loaded_at is None
        assert state.last_cleaned_at is None

    def test_canonicalizes_legacy_alias(self):
        # LPG is a known alias for PROPANE in the default catalog.
        state = CompartmentState(
            compartment_id="c1",
            truck_id="T1",
            tenant_id="tenant-A",
            last_loaded_product="LPG",
            state="loaded",
        )
        assert state.last_loaded_product == "PROPANE"

    def test_accepts_empty_string_as_null_product(self):
        state = CompartmentState(
            compartment_id="c1",
            truck_id="T1",
            tenant_id="tenant-A",
            last_loaded_product="",
        )
        assert state.last_loaded_product is None

    def test_rejects_unknown_state(self):
        with pytest.raises(ValidationError):
            CompartmentState(
                compartment_id="c1",
                truck_id="T1",
                tenant_id="tenant-A",
                state="exploding",  # type: ignore[arg-type]
            )

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            CompartmentState(
                compartment_id="   ",
                truck_id="T1",
                tenant_id="tenant-A",
            )


# ---------------------------------------------------------------------------
# Fake ES client plumbing
# ---------------------------------------------------------------------------


class _FakeESClient:
    """Synchronous fake mirroring the subset of the ES client we use.

    Stores a single document per ``doc_id`` plus its ``_seq_no`` and
    ``_primary_term``. ``update`` bumps ``_seq_no`` on every successful
    write so OCC is observable in tests.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.versions: Dict[str, Dict[str, int]] = {}
        self.update_calls: List[Dict[str, Any]] = []
        #: ``doc_id -> remaining conflict count``. Each conflict consumes
        #: one unit before the next call goes through.
        self._forced_conflicts: Dict[str, int] = {}
        self._forced_conflicts_total: Dict[str, int] = {}

    # -- configuration helpers ------------------------------------------------

    def force_conflicts(self, doc_id: str, n: int) -> None:
        self._forced_conflicts[doc_id] = n
        self._forced_conflicts_total[doc_id] = n

    def seed(
        self,
        doc_id: str,
        source: Dict[str, Any],
        *,
        seq_no: int = 0,
        primary_term: int = 1,
    ) -> None:
        self.docs[doc_id] = dict(source)
        self.versions[doc_id] = {"_seq_no": seq_no, "_primary_term": primary_term}

    # -- ES-compatible API ---------------------------------------------------

    def get(self, *, index: str, id: str) -> Dict[str, Any]:
        if id not in self.docs:
            raise _FakeNotFoundError(f"doc {id!r} not found")
        version = self.versions[id]
        return {
            "_source": dict(self.docs[id]),
            "_seq_no": version["_seq_no"],
            "_primary_term": version["_primary_term"],
        }

    def update(
        self,
        *,
        index: str,
        id: str,
        body: Dict[str, Any],
        if_seq_no: Optional[int] = None,
        if_primary_term: Optional[int] = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        self.update_calls.append(
            {
                "index": index,
                "id": id,
                "body": dict(body),
                "if_seq_no": if_seq_no,
                "if_primary_term": if_primary_term,
                "refresh": refresh,
            }
        )
        if self._forced_conflicts.get(id, 0) > 0:
            self._forced_conflicts[id] -= 1
            raise _FakeConflictError("version_conflict")

        if id not in self.docs:
            raise _FakeNotFoundError(f"doc {id!r} not found")

        version = self.versions[id]
        # Simulate OCC: assert supplied versions match the stored ones.
        if if_seq_no is not None and if_seq_no != version["_seq_no"]:
            raise _FakeConflictError("version_conflict")
        if (
            if_primary_term is not None
            and if_primary_term != version["_primary_term"]
        ):
            raise _FakeConflictError("version_conflict")

        # Merge the ``doc`` payload into the stored source. Explicit
        # ``None`` values clear fields, matching ES's own semantics.
        patch = body.get("doc", {})
        merged = {**self.docs[id], **patch}
        self.docs[id] = merged
        version["_seq_no"] += 1
        return {"_id": id, "result": "updated"}


class _FakeNotFoundError(Exception):
    status_code = 404


class _FakeConflictError(Exception):
    status_code = 409


class _FakeESService:
    def __init__(self, client: _FakeESClient) -> None:
        self.client = client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def es_client() -> _FakeESClient:
    return _FakeESClient()


@pytest.fixture
def es(es_client: _FakeESClient) -> _FakeESService:
    return _FakeESService(es_client)


@pytest.fixture
def repo(es: _FakeESService) -> CompartmentStateRepository:
    # Zero backoff keeps test runtime tight while still exercising the
    # retry loop.
    repo = CompartmentStateRepository(es_service=es)
    repo.OCC_BACKOFF_BASE_SECONDS = 0.0  # type: ignore[assignment]
    return repo


def _base_compartment_doc(**overrides: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "compartment_id": "c1",
        "truck_id": "T1",
        "capacity_liters": 5000.0,
        "allowed_grades": ["DIESEL_2"],
        "position_index": 0,
        "tenant_id": "tenant-A",
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestRepositoryConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError):
            CompartmentStateRepository(es_service=None)  # type: ignore[arg-type]

    def test_rejects_empty_index_name(self, es: _FakeESService):
        with pytest.raises(ValueError):
            CompartmentStateRepository(es_service=es, index_name="")

    def test_defaults_to_truck_compartments_index(self, es: _FakeESService):
        r = CompartmentStateRepository(es_service=es)
        assert r._index == TRUCK_COMPARTMENTS_INDEX


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRepositoryGet:
    async def test_get_returns_state_for_owned_doc(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc(state="loaded",
                                                     last_loaded_product="DIESEL_2"))
        state = await repo.get("tenant-A", "T1_c1")
        assert state is not None
        assert state.state == "loaded"
        assert state.last_loaded_product == "DIESEL_2"

    async def test_get_defaults_legacy_doc_to_clean(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        # Legacy compartments written before Task 6.1 will have no
        # ``state`` field — the repo must coerce them to clean rather
        # than raising.
        es_client.seed("T1_c1", _base_compartment_doc())
        state = await repo.get("tenant-A", "T1_c1")
        assert state is not None
        assert state.state == "clean"

    async def test_get_returns_none_for_missing_doc(
        self, repo: CompartmentStateRepository
    ):
        assert await repo.get("tenant-A", "missing") is None

    async def test_get_returns_none_for_cross_tenant_doc(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed(
            "T1_c1",
            _base_compartment_doc(tenant_id="tenant-B", state="loaded"),
        )
        assert await repo.get("tenant-A", "T1_c1") is None

    async def test_get_rejects_empty_tenant(
        self, repo: CompartmentStateRepository
    ):
        with pytest.raises(ValueError):
            await repo.get("", "T1_c1")

    async def test_get_rejects_empty_doc_id(
        self, repo: CompartmentStateRepository
    ):
        with pytest.raises(ValueError):
            await repo.get("tenant-A", "")


# ---------------------------------------------------------------------------
# mark_loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMarkLoaded:
    async def test_mark_loaded_sets_state_and_timestamp(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc())

        when = datetime(2025, 1, 15, 9, 30, tzinfo=timezone.utc)
        state = await repo.mark_loaded(
            "tenant-A", "T1_c1", product_code="DIESEL_2", loaded_at=when
        )

        assert state.state == "loaded"
        assert state.last_loaded_product == "DIESEL_2"
        assert state.last_loaded_at == when

        stored = es_client.docs["T1_c1"]
        assert stored["state"] == "loaded"
        assert stored["last_loaded_product"] == "DIESEL_2"
        assert stored["last_loaded_at"] == when.isoformat()

    async def test_mark_loaded_canonicalizes_alias(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc())

        state = await repo.mark_loaded(
            "tenant-A", "T1_c1", product_code="LPG"
        )

        assert state.last_loaded_product == "PROPANE"
        assert es_client.docs["T1_c1"]["last_loaded_product"] == "PROPANE"

    async def test_mark_loaded_asserts_seq_no_and_primary_term(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc(), seq_no=7, primary_term=2)

        await repo.mark_loaded(
            "tenant-A", "T1_c1", product_code="DIESEL_2"
        )

        # The update call must carry the seq_no + primary_term pulled
        # from the prior ``get``.
        assert len(es_client.update_calls) == 1
        call = es_client.update_calls[0]
        assert call["if_seq_no"] == 7
        assert call["if_primary_term"] == 2
        assert call["refresh"] is True

    async def test_mark_loaded_retries_on_single_conflict_then_succeeds(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc())
        es_client.force_conflicts("T1_c1", 1)

        state = await repo.mark_loaded(
            "tenant-A", "T1_c1", product_code="DIESEL_2"
        )

        assert state.state == "loaded"
        # One conflict + one successful write == two update attempts.
        assert len(es_client.update_calls) == 2

    async def test_mark_loaded_raises_on_persistent_conflict(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc())
        # Force more conflicts than the retry budget so the repo gives up.
        es_client.force_conflicts("T1_c1", repo.MAX_OCC_RETRIES + 1)

        with pytest.raises(CompartmentStateConflictError):
            await repo.mark_loaded(
                "tenant-A", "T1_c1", product_code="DIESEL_2"
            )
        assert len(es_client.update_calls) == repo.MAX_OCC_RETRIES

    async def test_mark_loaded_rejects_cross_tenant_write(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc(tenant_id="tenant-B"))

        with pytest.raises(CrossTenantCompartmentAccessError):
            await repo.mark_loaded(
                "tenant-A", "T1_c1", product_code="DIESEL_2"
            )
        # No update should have been issued — the tenant guard fires
        # before any mutation.
        assert es_client.update_calls == []

    async def test_mark_loaded_raises_when_compartment_missing(
        self, repo: CompartmentStateRepository
    ):
        with pytest.raises(CompartmentNotFoundError):
            await repo.mark_loaded(
                "tenant-A", "missing", product_code="DIESEL_2"
            )

    async def test_mark_loaded_rejects_empty_product(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc())
        with pytest.raises(ValueError):
            await repo.mark_loaded("tenant-A", "T1_c1", product_code="")


# ---------------------------------------------------------------------------
# mark_cleaned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMarkCleaned:
    async def test_mark_cleaned_resets_state_and_clears_product(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed(
            "T1_c1",
            _base_compartment_doc(
                state="needs_cleaning",
                last_loaded_product="GASOLINE_REG",
                last_loaded_at="2025-01-14T08:00:00+00:00",
            ),
        )

        when = datetime(2025, 1, 15, 14, 0, tzinfo=timezone.utc)
        state = await repo.mark_cleaned("tenant-A", "T1_c1", cleaned_at=when)

        assert state.state == "clean"
        assert state.last_loaded_product is None
        assert state.last_cleaned_at == when

        stored = es_client.docs["T1_c1"]
        assert stored["state"] == "clean"
        assert stored["last_cleaned_at"] == when.isoformat()
        assert stored["last_loaded_product"] is None
        # last_loaded_at is preserved so the compatibility engine can
        # compare cleaning vs load timestamps.
        assert stored["last_loaded_at"] == "2025-01-14T08:00:00+00:00"

    async def test_mark_cleaned_defaults_to_now_when_no_timestamp(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc())

        before = datetime.now(timezone.utc)
        state = await repo.mark_cleaned("tenant-A", "T1_c1")
        after = datetime.now(timezone.utc)

        assert state.last_cleaned_at is not None
        assert before <= state.last_cleaned_at <= after

    async def test_mark_cleaned_rejects_cross_tenant(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc(tenant_id="tenant-B"))

        with pytest.raises(CrossTenantCompartmentAccessError):
            await repo.mark_cleaned("tenant-A", "T1_c1")


# ---------------------------------------------------------------------------
# mark_needs_cleaning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMarkNeedsCleaning:
    async def test_mark_needs_cleaning_preserves_timestamps(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed(
            "T1_c1",
            _base_compartment_doc(
                state="loaded",
                last_loaded_product="DIESEL_2",
                last_loaded_at="2025-01-14T08:00:00+00:00",
                last_cleaned_at="2025-01-10T08:00:00+00:00",
            ),
        )

        state = await repo.mark_needs_cleaning("tenant-A", "T1_c1")

        assert state.state == "needs_cleaning"
        # Timestamps and product must be preserved — only the flag
        # changes.
        assert state.last_loaded_product == "DIESEL_2"
        assert state.last_loaded_at == datetime(
            2025, 1, 14, 8, 0, tzinfo=timezone.utc
        )
        assert state.last_cleaned_at == datetime(
            2025, 1, 10, 8, 0, tzinfo=timezone.utc
        )

        # Only ``state`` is in the persisted patch.
        call = es_client.update_calls[-1]
        assert call["body"]["doc"] == {"state": "needs_cleaning"}

    async def test_mark_needs_cleaning_rejects_cross_tenant(
        self, repo: CompartmentStateRepository, es_client: _FakeESClient
    ):
        es_client.seed("T1_c1", _base_compartment_doc(tenant_id="tenant-B"))

        with pytest.raises(CrossTenantCompartmentAccessError):
            await repo.mark_needs_cleaning("tenant-A", "T1_c1")

    async def test_mark_needs_cleaning_raises_when_missing(
        self, repo: CompartmentStateRepository
    ):
        with pytest.raises(CompartmentNotFoundError):
            await repo.mark_needs_cleaning("tenant-A", "missing")
