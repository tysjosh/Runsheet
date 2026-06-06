"""
Unit tests for :class:`fuel.compartment_state_models.CleaningEventService`.

Covers Capability 7 / Requirement 7.1.3 of the fuel-ops hardening spec:

* :class:`CleaningEvent` model validation — required strings, allowed
  ``method`` values, timezone normalization of ``cleaned_at``, dedup and
  blank rejection for ``evidence_refs``, and normalization of empty
  ``notes``.
* :class:`CleaningEventService.record` end-to-end behaviour:

    - Pre-flight check rejects missing / cross-tenant compartments
      **before** any cleaning-event document is written, so no orphan
      records are left in ``compartment_cleaning_events``.
    - On a happy path the service writes to
      ``compartment_cleaning_events`` with the canonical ES fields,
      mints a ``ce_`` id when one is not supplied, then calls
      :meth:`CompartmentStateRepository.mark_cleaned` with the exact
      ``cleaned_at`` timestamp persisted on the event.
    - Evidence refs are validated against the tenant via the injected
      File_Storage_Service when one is present; cross-tenant refs raise
      ``PermissionError`` **before** the event is indexed.
    - A failure in the downstream ``mark_cleaned`` call after the event
      has been persisted surfaces as a
      :class:`CleaningEventPersistenceError` carrying the original
      exception so callers can retry the reset step idempotently.

Validates: Requirement 7.1.3.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from fuel.compartment_state_models import (
    CleaningEvent,
    CleaningEventPersistenceError,
    CleaningEventService,
    CompartmentNotFoundError,
    CompartmentState,
    CompartmentStateConflictError,
    CompartmentStateRepository,
    CrossTenantCompartmentAccessError,
)
from fuel.services.fuel_ops_es_mappings import COMPARTMENT_CLEANING_EVENTS_INDEX


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeESService:
    """Async recording mock covering the subset the service uses.

    Only ``index_document`` is implemented; the :class:`CleaningEventService`
    does not read from ES (reads flow through the state repo mock), so the
    rest of the ElasticsearchService surface is intentionally left out.
    """

    def __init__(self) -> None:
        self.index_calls: List[Dict[str, Any]] = []
        #: If non-None, every ``index_document`` call raises this exception
        #: once before resetting.
        self.raise_on_index: Optional[BaseException] = None

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self.raise_on_index is not None:
            exc, self.raise_on_index = self.raise_on_index, None
            raise exc
        self.index_calls.append(
            {"index": index, "id": doc_id, "doc": dict(document)}
        )
        return {"_id": doc_id, "result": "created"}


class _FakeStateRepository:
    """Stand-in for :class:`CompartmentStateRepository`.

    Recorded calls let tests assert that (a) ``get`` runs before any write
    (pre-flight tenant/existence check) and (b) ``mark_cleaned`` is invoked
    with the same ``cleaned_at`` that the event row persisted.
    """

    def __init__(self) -> None:
        self.states: Dict[str, CompartmentState] = {}
        self.get_calls: List[Dict[str, str]] = []
        self.mark_cleaned_calls: List[Dict[str, Any]] = []
        #: Optional override exceptions keyed by operation name.
        self.raise_on_get: Optional[BaseException] = None
        self.raise_on_mark_cleaned: Optional[BaseException] = None

    def seed(self, compartment_doc_id: str, state: CompartmentState) -> None:
        self.states[compartment_doc_id] = state

    async def get(
        self, tenant_id: str, compartment_doc_id: str
    ) -> Optional[CompartmentState]:
        self.get_calls.append(
            {"tenant_id": tenant_id, "compartment_doc_id": compartment_doc_id}
        )
        if self.raise_on_get is not None:
            exc, self.raise_on_get = self.raise_on_get, None
            raise exc
        cached = self.states.get(compartment_doc_id)
        if cached is None:
            return None
        # Repo semantics: a cross-tenant request returns None.
        if cached.tenant_id != tenant_id:
            return None
        return cached

    async def mark_cleaned(
        self,
        tenant_id: str,
        compartment_doc_id: str,
        *,
        cleaned_at: Optional[datetime] = None,
    ) -> CompartmentState:
        self.mark_cleaned_calls.append(
            {
                "tenant_id": tenant_id,
                "compartment_doc_id": compartment_doc_id,
                "cleaned_at": cleaned_at,
            }
        )
        if self.raise_on_mark_cleaned is not None:
            exc, self.raise_on_mark_cleaned = self.raise_on_mark_cleaned, None
            raise exc
        current = self.states[compartment_doc_id]
        refreshed = current.model_copy(
            update={
                "state": "clean",
                "last_cleaned_at": cleaned_at,
                "last_loaded_product": None,
            }
        )
        self.states[compartment_doc_id] = refreshed
        return refreshed


class _FakeFileStorage:
    """Records ``validate_ref`` calls and raises for flagged cross-tenant refs."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        #: Refs that must raise PermissionError on validate_ref.
        self.forbidden_refs: set[str] = set()

    def validate_ref(
        self, tenant_id: str, file_ref: str, actor: Optional[str] = None
    ) -> bool:
        self.calls.append(
            {"tenant_id": tenant_id, "file_ref": file_ref, "actor": actor}
        )
        if file_ref in self.forbidden_refs:
            raise PermissionError("cross_tenant_file_ref")
        return True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def state_repo() -> _FakeStateRepository:
    return _FakeStateRepository()


@pytest.fixture
def file_storage() -> _FakeFileStorage:
    return _FakeFileStorage()


@pytest.fixture
def service(
    es: _FakeESService,
    state_repo: _FakeStateRepository,
    file_storage: _FakeFileStorage,
) -> CleaningEventService:
    return CleaningEventService(
        es_service=es,
        state_repository=state_repo,
        file_storage=file_storage,
    )


def _seed_needs_cleaning(
    state_repo: _FakeStateRepository,
    *,
    compartment_doc_id: str = "T1_c1",
    tenant_id: str = "tenant-A",
    last_loaded_product: str = "GASOLINE_REG",
) -> None:
    state_repo.seed(
        compartment_doc_id,
        CompartmentState(
            compartment_id="c1",
            truck_id="T1",
            tenant_id=tenant_id,
            state="needs_cleaning",
            last_loaded_product=last_loaded_product,
            last_loaded_at=datetime(2025, 1, 14, 8, 0, tzinfo=timezone.utc),
        ),
    )


# ---------------------------------------------------------------------------
# CleaningEvent model
# ---------------------------------------------------------------------------


class TestCleaningEventModel:
    def _base_kwargs(self, **overrides: Any) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "cleaning_event_id": "ce_001",
            "tenant_id": "tenant-A",
            "compartment_id": "T1_c1",
            "truck_id": "T1",
            "method": "sanitize",
            "actor_id": "user_42",
            "cleaned_at": datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
        }
        kwargs.update(overrides)
        return kwargs

    def test_roundtrips_with_minimum_fields(self):
        event = CleaningEvent(**self._base_kwargs())
        assert event.method == "sanitize"
        assert event.notes is None
        assert event.evidence_refs == []
        assert event.cleaned_at.tzinfo is not None

    def test_rejects_unknown_method(self):
        with pytest.raises(ValidationError):
            CleaningEvent(**self._base_kwargs(method="scrub"))

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            CleaningEvent(**self._base_kwargs(actor_id="   "))
        with pytest.raises(ValidationError):
            CleaningEvent(**self._base_kwargs(compartment_id=""))

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            CleaningEvent(**self._base_kwargs(secret="boom"))

    def test_coerces_naive_cleaned_at_to_utc(self):
        naive = datetime(2025, 1, 15, 12, 0)
        event = CleaningEvent(**self._base_kwargs(cleaned_at=naive))
        assert event.cleaned_at.tzinfo is timezone.utc

    def test_normalizes_blank_notes_to_none(self):
        event = CleaningEvent(**self._base_kwargs(notes="   "))
        assert event.notes is None

    def test_dedupes_evidence_refs_preserving_order(self):
        refs = [
            "tenants/tenant-A/photo/2025/01/15/a.jpg",
            "tenants/tenant-A/photo/2025/01/15/b.jpg",
            "tenants/tenant-A/photo/2025/01/15/a.jpg",
        ]
        event = CleaningEvent(**self._base_kwargs(evidence_refs=refs))
        assert event.evidence_refs == [
            "tenants/tenant-A/photo/2025/01/15/a.jpg",
            "tenants/tenant-A/photo/2025/01/15/b.jpg",
        ]

    def test_rejects_blank_evidence_ref(self):
        with pytest.raises(ValidationError):
            CleaningEvent(**self._base_kwargs(evidence_refs=["   "]))

    def test_driver_id_roundtrips_and_strips(self):
        event = CleaningEvent(**self._base_kwargs(driver_id="  DRV-9  "))
        assert event.driver_id == "DRV-9"

    def test_blank_driver_id_normalizes_to_none(self):
        assert CleaningEvent(**self._base_kwargs(driver_id="   ")).driver_id is None
        assert CleaningEvent(**self._base_kwargs()).driver_id is None


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------


class TestServiceConstruction:
    def test_rejects_missing_es(self, state_repo: _FakeStateRepository):
        with pytest.raises(ValueError):
            CleaningEventService(
                es_service=None,  # type: ignore[arg-type]
                state_repository=state_repo,
            )

    def test_rejects_missing_state_repository(self, es: _FakeESService):
        with pytest.raises(ValueError):
            CleaningEventService(
                es_service=es,
                state_repository=None,  # type: ignore[arg-type]
            )

    def test_rejects_empty_index_name(
        self, es: _FakeESService, state_repo: _FakeStateRepository
    ):
        with pytest.raises(ValueError):
            CleaningEventService(
                es_service=es,
                state_repository=state_repo,
                index_name="",
            )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecordHappyPath:
    async def test_persists_event_then_resets_state(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)

        when = datetime(2025, 1, 15, 14, 0, tzinfo=timezone.utc)
        event = await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="sanitize",
            actor_id="user_42",
            cleaned_at=when,
            notes="Post-heating-oil purge",
        )

        assert event.method == "sanitize"
        assert event.cleaned_at == when
        assert event.cleaning_event_id.startswith("ce_")
        assert event.notes == "Post-heating-oil purge"

        # Event was written to the canonical index with the minted id.
        assert len(es.index_calls) == 1
        call = es.index_calls[0]
        assert call["index"] == COMPARTMENT_CLEANING_EVENTS_INDEX
        assert call["id"] == event.cleaning_event_id
        assert call["doc"]["tenant_id"] == "tenant-A"
        assert call["doc"]["compartment_id"] == "T1_c1"
        assert call["doc"]["truck_id"] == "T1"
        assert call["doc"]["method"] == "sanitize"
        # The persisted ``cleaned_at`` round-trips back to the exact
        # datetime the caller supplied (Pydantic serializes UTC as
        # ``...Z`` in ``mode="json"``, so compare semantically).
        assert datetime.fromisoformat(
            call["doc"]["cleaned_at"].replace("Z", "+00:00")
        ) == when

        # State repository saw a pre-flight read and then a mark_cleaned
        # with the same ``cleaned_at`` we persisted on the event.
        assert state_repo.get_calls == [
            {"tenant_id": "tenant-A", "compartment_doc_id": "T1_c1"}
        ]
        assert state_repo.mark_cleaned_calls == [
            {
                "tenant_id": "tenant-A",
                "compartment_doc_id": "T1_c1",
                "cleaned_at": when,
            }
        ]
        refreshed = state_repo.states["T1_c1"]
        assert refreshed.state == "clean"
        assert refreshed.last_loaded_product is None
        assert refreshed.last_cleaned_at == when

    async def test_persists_canonical_driver_id_when_supplied(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        # cross-module-entity-linkage Req 8.2: the optional canonical
        # ``driver_id`` is persisted alongside the deprecated free-text
        # ``actor_id`` alias.
        _seed_needs_cleaning(state_repo)

        event = await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="flush",
            actor_id="user_42",
            driver_id="DRV-7",
        )

        assert event.driver_id == "DRV-7"
        assert event.actor_id == "user_42"
        assert es.index_calls[0]["doc"]["driver_id"] == "DRV-7"

    async def test_driver_id_defaults_to_none_when_omitted(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        # Additive/backward-compatible: a cleaning event without a
        # ``driver_id`` persists ``None`` so legacy events stay valid.
        _seed_needs_cleaning(state_repo)

        event = await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="flush",
            actor_id="user_42",
        )

        assert event.driver_id is None
        assert es.index_calls[0]["doc"]["driver_id"] is None

    async def test_defaults_cleaned_at_when_omitted(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)

        before = datetime.now(timezone.utc)
        event = await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="flush",
            actor_id="user_42",
        )
        after = datetime.now(timezone.utc)

        assert before <= event.cleaned_at <= after
        # Event and state-reset share the same timestamp.
        mark_call = state_repo.mark_cleaned_calls[-1]
        assert mark_call["cleaned_at"] == event.cleaned_at

    async def test_uses_supplied_event_id_for_idempotent_retries(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)

        event = await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="flush",
            actor_id="user_42",
            cleaning_event_id="ce_explicit_001",
        )
        assert event.cleaning_event_id == "ce_explicit_001"
        assert es.index_calls[0]["id"] == "ce_explicit_001"

    async def test_validates_evidence_refs_when_file_storage_injected(
        self,
        service: CleaningEventService,
        state_repo: _FakeStateRepository,
        file_storage: _FakeFileStorage,
    ):
        _seed_needs_cleaning(state_repo)

        refs = [
            "tenants/tenant-A/photo/2025/01/15/a.jpg",
            "tenants/tenant-A/photo/2025/01/15/b.jpg",
        ]
        await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="sanitize",
            actor_id="user_42",
            evidence_refs=refs,
        )

        validated = [call["file_ref"] for call in file_storage.calls]
        assert validated == refs
        # Actor is propagated into the audit trail.
        assert all(call["actor"] == "user_42" for call in file_storage.calls)

    async def test_skips_evidence_validation_without_file_storage(
        self,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)
        service = CleaningEventService(
            es_service=es,
            state_repository=state_repo,
            file_storage=None,
        )

        event = await service.record(
            "tenant-A",
            "T1_c1",
            truck_id="T1",
            method="sanitize",
            actor_id="user_42",
            evidence_refs=["tenants/tenant-A/photo/2025/01/15/a.jpg"],
        )
        assert event.evidence_refs == [
            "tenants/tenant-A/photo/2025/01/15/a.jpg"
        ]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecordFailureModes:
    async def test_rejects_missing_compartment_before_writing_event(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        # Nothing seeded — state repo's ``get`` returns None.
        with pytest.raises(CompartmentNotFoundError):
            await service.record(
                "tenant-A",
                "T1_missing",
                truck_id="T1",
                method="flush",
                actor_id="user_42",
            )

        # No index call occurred — the guard fires before any write.
        assert es.index_calls == []
        assert state_repo.mark_cleaned_calls == []

    async def test_rejects_cross_tenant_compartment_before_writing_event(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo, tenant_id="tenant-B")

        with pytest.raises(CompartmentNotFoundError):
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="flush",
                actor_id="user_42",
            )
        assert es.index_calls == []
        assert state_repo.mark_cleaned_calls == []

    async def test_rejects_cross_tenant_evidence_ref_before_writing_event(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
        file_storage: _FakeFileStorage,
    ):
        _seed_needs_cleaning(state_repo)
        bad_ref = "tenants/tenant-B/photo/2025/01/15/a.jpg"
        file_storage.forbidden_refs.add(bad_ref)

        with pytest.raises(PermissionError):
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="sanitize",
                actor_id="user_42",
                evidence_refs=[bad_ref],
            )

        # File-storage validation runs before index_document, so no event
        # was persisted and no state reset was attempted.
        assert es.index_calls == []
        assert state_repo.mark_cleaned_calls == []

    async def test_surfaces_persistence_error_when_state_reset_fails(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)
        state_repo.raise_on_mark_cleaned = CompartmentStateConflictError(
            tenant_id="tenant-A", compartment_doc_id="T1_c1", attempts=3
        )

        with pytest.raises(CleaningEventPersistenceError) as exc_info:
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="flush",
                actor_id="user_42",
            )

        # The event was persisted before the failure so retry is
        # idempotent if the caller reuses the event id.
        assert len(es.index_calls) == 1
        assert exc_info.value.cleaning_event_id == es.index_calls[0]["id"]
        assert exc_info.value.tenant_id == "tenant-A"
        assert exc_info.value.compartment_id == "T1_c1"
        # The underlying cause is attached for downstream diagnostics.
        assert isinstance(exc_info.value.__cause__, CompartmentStateConflictError)

    async def test_propagates_cross_tenant_error_from_state_reset(
        self,
        service: CleaningEventService,
        es: _FakeESService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)
        # Simulate a race where the doc existed at get() time but the
        # subsequent mark_cleaned call observes a tenant-id flip.
        state_repo.raise_on_mark_cleaned = CrossTenantCompartmentAccessError(
            tenant_id="tenant-A",
            compartment_doc_id="T1_c1",
            owning_tenant_id="tenant-B",
        )

        with pytest.raises(CleaningEventPersistenceError) as exc_info:
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="flush",
                actor_id="user_42",
            )
        assert isinstance(
            exc_info.value.__cause__, CrossTenantCompartmentAccessError
        )

    async def test_rejects_blank_required_arguments(
        self,
        service: CleaningEventService,
        state_repo: _FakeStateRepository,
    ):
        _seed_needs_cleaning(state_repo)
        with pytest.raises(ValueError):
            await service.record(
                "",
                "T1_c1",
                truck_id="T1",
                method="flush",
                actor_id="user_42",
            )
        with pytest.raises(ValueError):
            await service.record(
                "tenant-A",
                "   ",
                truck_id="T1",
                method="flush",
                actor_id="user_42",
            )
        with pytest.raises(ValueError):
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="",
                method="flush",
                actor_id="user_42",
            )
        with pytest.raises(ValueError):
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="flush",
                actor_id="",
            )
        with pytest.raises(ValueError):
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="   ",
                actor_id="user_42",
            )

    async def test_rejects_unknown_method_via_model_validation(
        self,
        service: CleaningEventService,
        state_repo: _FakeStateRepository,
        es: _FakeESService,
    ):
        _seed_needs_cleaning(state_repo)
        with pytest.raises(ValidationError):
            await service.record(
                "tenant-A",
                "T1_c1",
                truck_id="T1",
                method="scrub",
                actor_id="user_42",
            )
        # No write should have happened.
        assert es.index_calls == []
        assert state_repo.mark_cleaned_calls == []
