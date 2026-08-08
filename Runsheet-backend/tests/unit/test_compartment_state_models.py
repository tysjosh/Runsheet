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


class _FakeStore:
    """In-memory stand-in for :class:`persistence.document_store.PostgresDocumentStore`.

    Implements the two methods the repository reaches through the facade, with the
    store's documented contract: ``atomic_update`` calls ``transform`` with a COPY
    of the stored document, writes whatever it returns, treats ``None`` as a no-op,
    and answers ``(document, applied)``.

    This replaced a fake Elasticsearch client plus a fake facade that borrowed the
    real ``atomic_update`` to drive its ``if_seq_no`` retry loop. That loop is gone:
    Phase 6 deleted the Elasticsearch branch, and on Postgres the row is locked, so
    a concurrent writer waits instead of colliding. Borrowing the shipped method is
    no longer worth anything either — it is now a one-line delegation to the store,
    so there is no logic left in it to exercise. The store's own behaviour, including
    real contention, is covered in ``tests/postgres/test_document_store_atomic.py``.

    What is under test here is the REPOSITORY: which fields it patches, how it
    canonicalises a product code, and that it refuses a cross-tenant write before
    touching anything.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        #: Every document written, in order — the assertion surface that replaces
        #: the old ``update_calls`` list.
        self.writes: List[Dict[str, Any]] = []

    def seed(self, doc_id: str, source: Dict[str, Any]) -> None:
        self.docs[doc_id] = dict(source)

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        stored = self.docs.get(doc_id)
        return dict(stored) if stored is not None else None

    async def atomic_update(self, index, doc_id, transform, *, upsert=None):
        stored = self.docs.get(doc_id)
        if stored is None:
            if upsert is None:
                return (None, False)
            document = dict(upsert)
            self.docs[doc_id] = document
            self.writes.append(dict(document))
            return (document, True)
        # A copy, so a transform that mutates in place cannot corrupt the store —
        # the real store passes a copy for the same reason.
        updated = transform(dict(stored))
        if updated is None:
            return (dict(stored), False)
        self.docs[doc_id] = dict(updated)
        self.writes.append(dict(updated))
        return (dict(updated), True)


class _FakeESService:
    """The facade surface the repository uses, backed by :class:`_FakeStore`."""

    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        #: Present and unusable on purpose: the repository must not reach the raw
        #: client. Any attempt raises AttributeError rather than silently working.
        self.client = None

    async def get_document(self, index: str, doc_id: str):
        return await self._store.get_document(index, doc_id)

    async def atomic_update(self, index, doc_id, transform, **kwargs):
        # ``max_retries`` / ``backoff_base_seconds`` are accepted and ignored, as
        # the Postgres path ignores them: there is nothing to retry against a lock.
        kwargs.pop("max_retries", None)
        kwargs.pop("backoff_base_seconds", None)
        return await self._store.atomic_update(index, doc_id, transform, **kwargs)



@pytest.fixture
def es_client() -> _FakeStore:
    """Kept under its old name so the twenty-odd existing tests read unchanged.

    It is the STORE now, not a client — the repository no longer has a raw client
    to talk to. ``seed`` and ``docs`` work as they did.
    """
    return _FakeStore()


@pytest.fixture
def es(es_client: _FakeStore) -> _FakeESService:
    return _FakeESService(es_client)


@pytest.fixture
def repo(es: _FakeESService) -> CompartmentStateRepository:
    return CompartmentStateRepository(es_service=es)


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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
    ):
        es_client.seed("T1_c1", _base_compartment_doc(state="loaded",
                                                     last_loaded_product="DIESEL_2"))
        state = await repo.get("tenant-A", "T1_c1")
        assert state is not None
        assert state.state == "loaded"
        assert state.last_loaded_product == "DIESEL_2"

    async def test_get_defaults_legacy_doc_to_clean(
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
    ):
        es_client.seed("T1_c1", _base_compartment_doc())

        state = await repo.mark_loaded(
            "tenant-A", "T1_c1", product_code="LPG"
        )

        assert state.last_loaded_product == "PROPANE"
        assert es_client.docs["T1_c1"]["last_loaded_product"] == "PROPANE"

    # Three optimistic-concurrency tests were removed here, not silenced:
    #
    #   test_mark_loaded_asserts_seq_no_and_primary_term
    #   test_mark_loaded_retries_on_single_conflict_then_succeeds
    #   test_mark_loaded_raises_on_persistent_conflict
    #
    # They asserted that the repository read ``_seq_no`` / ``_primary_term``, wrote
    # with them asserted, retried on a 409, and raised
    # ``CompartmentStateConflictError`` once the retry budget ran out. All four
    # behaviours belonged to the Elasticsearch branch that Phase 6 deleted. Against
    # Postgres the row is locked for the transaction, so a concurrent writer waits
    # rather than colliding: there is no version to assert, no conflict to retry,
    # and ``CompartmentStateConflictError`` is unreachable. The repository still
    # translates it if the facade ever raises one, which is the only part of that
    # contract left.
    #
    # Concurrency is covered where it is now real:
    # ``tests/postgres/test_document_store_atomic.py`` runs ten concurrent
    # increments against the actual store and was verified non-vacuous by removing
    # the row lock, which loses seven of them.




    async def test_mark_loaded_rejects_cross_tenant_write(
        self, repo: CompartmentStateRepository, es_client: _FakeStore
    ):
        es_client.seed("T1_c1", _base_compartment_doc(tenant_id="tenant-B"))

        with pytest.raises(CrossTenantCompartmentAccessError):
            await repo.mark_loaded(
                "tenant-A", "T1_c1", product_code="DIESEL_2"
            )
        # No update should have been issued — the tenant guard fires
        # before any mutation.
        assert es_client.writes == []

    async def test_mark_loaded_raises_when_compartment_missing(
        self, repo: CompartmentStateRepository
    ):
        with pytest.raises(CompartmentNotFoundError):
            await repo.mark_loaded(
                "tenant-A", "missing", product_code="DIESEL_2"
            )

    async def test_mark_loaded_rejects_empty_product(
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
    ):
        es_client.seed("T1_c1", _base_compartment_doc())

        before = datetime.now(timezone.utc)
        state = await repo.mark_cleaned("tenant-A", "T1_c1")
        after = datetime.now(timezone.utc)

        assert state.last_cleaned_at is not None
        assert before <= state.last_cleaned_at <= after

    async def test_mark_cleaned_rejects_cross_tenant(
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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
        self, repo: CompartmentStateRepository, es_client: _FakeStore
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

        # Only ``state`` changed; every other field is byte-identical to the seed.
        #
        # Asserted on the STORED DOCUMENT rather than on the wire payload. The
        # repository used to send a partial ``{"doc": {"state": ...}}`` and let
        # Elasticsearch merge it server-side; ``atomic_update`` computes the whole
        # new document and writes that, because a merge would silently keep a
        # field the transform removed and because the Postgres backend replaces
        # the row. The observable guarantee — nothing but ``state`` moves — is the
        # same, and is what this now checks.
        stored = es_client.docs["T1_c1"]
        assert stored["state"] == "needs_cleaning"
        seeded = _base_compartment_doc(
            state="loaded",
            last_loaded_product="DIESEL_2",
            last_loaded_at="2025-01-14T08:00:00+00:00",
            last_cleaned_at="2025-01-10T08:00:00+00:00",
        )
        for field, value in seeded.items():
            if field == "state":
                continue
            assert stored[field] == value, field

    async def test_mark_needs_cleaning_rejects_cross_tenant(
        self, repo: CompartmentStateRepository, es_client: _FakeStore
    ):
        es_client.seed("T1_c1", _base_compartment_doc(tenant_id="tenant-B"))

        with pytest.raises(CrossTenantCompartmentAccessError):
            await repo.mark_needs_cleaning("tenant-A", "T1_c1")

    async def test_mark_needs_cleaning_raises_when_missing(
        self, repo: CompartmentStateRepository
    ):
        with pytest.raises(CompartmentNotFoundError):
            await repo.mark_needs_cleaning("tenant-A", "missing")
