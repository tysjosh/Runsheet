"""
Unit tests for :mod:`integrations.connector_base`.

Covers Capability 5 / Requirements 5.1.1, 5.1.2, 5.1.4 of the fuel-ops
hardening spec:

* :class:`IntegrationConnector` ABC — enforces both ClassVar overrides,
  cannot be instantiated without implementing every abstract method,
  accepts intermediate abstract bases that still declare abstract
  methods of their own, and a conforming concrete subclass can be
  instantiated and called.
* :class:`IntegrationInstance` model — field shapes, tenant/provider
  validation, enum bounds for ``status`` and ``category``, default
  values (``enabled=False``, ``status="pending"``, ``retry_count=0``),
  whitespace/blank rejection on required strings, and extra-field
  rejection.
* :class:`SyncRun` model — required fields, operation and status
  enums, record_counts validation (reject negative values and
  booleans-as-ints), and optional duration_ms lower bound.
* :class:`ConnectionResult` model — defaults to ``connected``, rejects
  unknown status values, normalizes blank optional strings to ``None``.
* :class:`IntegrationInstanceRepository` async CRUD, all tenant-scoped:
    - create → writes to ES, stamps ``updated_at`` / ``created_at``,
      mints a uuid when id is omitted, rejects cross-tenant payloads.
    - get → returns the model, ``None`` when missing, ``None`` when
      owned by another tenant (no existence leak).
    - list_for_tenant → filters by provider_name / category / enabled
      / status, drops mis-labelled records with a warning, never
      returns another tenant's data.
    - update → tenant-scoped, strips immutable fields, raises
      CrossTenantAccessError on cross-tenant writes, returns None for
      missing.
    - delete → returns True on success, False when missing, raises
      CrossTenantAccessError when owned by a different tenant.

The ElasticsearchService dependency is replaced with a recording async
mock so tests never touch a real cluster.

Validates: Requirements 5.1.1, 5.1.2, 5.1.4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from fuel.services.fuel_ops_es_mappings import INTEGRATION_INSTANCES_INDEX
from integrations.connector_base import (
    ConnectionResult,
    CrossTenantAccessError,
    IntegrationConnector,
    IntegrationInstance,
    IntegrationInstanceRepository,
    SyncRun,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeESService:
    """In-memory recording mock for ElasticsearchService.

    Stores indexed documents keyed by ``doc_id`` and provides the
    subset of the ``ElasticsearchService`` async API used by
    :class:`IntegrationInstanceRepository`:

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
    if not must and "term" in inner:
        must = [inner]
    for clause in must:
        if "term" in clause:
            for field, expected in clause["term"].items():
                actual = doc.get(field)
                if actual != expected:
                    return False
    return True


def _base_instance_kwargs(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "instance_id": "integration_001",
        "tenant_id": "tenant-A",
        "provider_name": "quickbooks_online",
        "category": "accounting",
        "enabled": True,
        "status": "connected",
        "credentials_ref": "cred:tenant-A:qbo:abc",
        "schedule_cron": "0 */6 * * *",
        "config": {"realm_id": "1234567890"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def es() -> _FakeESService:
    return _FakeESService()


@pytest.fixture
def repo(es: _FakeESService) -> IntegrationInstanceRepository:
    return IntegrationInstanceRepository(es_service=es)


# ---------------------------------------------------------------------------
# IntegrationConnector ABC
# ---------------------------------------------------------------------------


class TestIntegrationConnectorABC:
    def test_abstract_connector_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            IntegrationConnector()  # type: ignore[abstract]

    def test_subclass_missing_category_fails_at_class_definition(self):
        with pytest.raises(TypeError):

            class BadConnector(IntegrationConnector):
                # category intentionally left as empty default.
                provider_name = "bad"

                async def connect(self, credentials):  # pragma: no cover
                    return ConnectionResult()

                async def sync_pull(self, since):  # pragma: no cover
                    return _valid_sync_run()

                async def sync_push(self, payload):  # pragma: no cover
                    return _valid_sync_run()

                async def disconnect(self):  # pragma: no cover
                    return None

    def test_subclass_missing_provider_name_fails_at_class_definition(self):
        with pytest.raises(TypeError):

            class BadConnector(IntegrationConnector):
                category = "accounting"
                # provider_name intentionally left as empty default.

                async def connect(self, credentials):  # pragma: no cover
                    return ConnectionResult()

                async def sync_pull(self, since):  # pragma: no cover
                    return _valid_sync_run()

                async def sync_push(self, payload):  # pragma: no cover
                    return _valid_sync_run()

                async def disconnect(self):  # pragma: no cover
                    return None

    def test_subclass_missing_abstract_method_cannot_be_instantiated(self):
        class HalfConnector(IntegrationConnector):
            category = "accounting"
            provider_name = "halfbaked"

            async def connect(self, credentials):  # pragma: no cover
                return ConnectionResult()

            async def sync_pull(self, since):  # pragma: no cover
                return _valid_sync_run()

            async def sync_push(self, payload):  # pragma: no cover
                return _valid_sync_run()

            # disconnect intentionally omitted — still abstract.

        with pytest.raises(TypeError):
            HalfConnector()  # type: ignore[abstract]

    def test_intermediate_abstract_base_is_allowed(self):
        # An intermediate class that adds its OWN abstract method should
        # not trip the ClassVar override check, because it's still
        # abstract and not yet a concrete provider.
        from abc import abstractmethod

        class OAuthConnectorBase(IntegrationConnector):
            @abstractmethod
            async def refresh_token(self):
                ...

        # No error raised — importing / defining the class works fine.
        assert OAuthConnectorBase.__abstractmethods__

    @pytest.mark.asyncio
    async def test_conforming_subclass_instantiates_and_runs(self):
        connector = _GoodConnector()
        assert connector.category == "accounting"
        assert connector.provider_name == "good_provider"

        result = await connector.connect({"api_key": "secret"})
        assert isinstance(result, ConnectionResult)
        assert result.status == "connected"

        pull = await connector.sync_pull(datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert isinstance(pull, SyncRun)
        assert pull.operation == "pull"
        assert pull.status == "success"

        push = await connector.sync_push({"invoice_id": "inv_1"})
        assert isinstance(push, SyncRun)
        assert push.operation == "push"

        # Disconnect is fire-and-forget; any return value is ignored.
        await connector.disconnect()


def _valid_sync_run(**overrides: Any) -> SyncRun:
    payload: Dict[str, Any] = {
        "run_id": "run_001",
        "tenant_id": "tenant-A",
        "instance_id": "integration_001",
        "provider_name": "good_provider",
        "operation": "pull",
        "started_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "finished_at": datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        "status": "success",
        "record_counts": {"invoices": 0},
        "duration_ms": 1000,
    }
    payload.update(overrides)
    return SyncRun(**payload)


class _GoodConnector(IntegrationConnector):
    """A minimal conforming concrete connector used in tests."""

    category = "accounting"
    provider_name = "good_provider"

    async def connect(self, credentials: Dict[str, Any]) -> ConnectionResult:
        return ConnectionResult(status="connected", credentials_ref="cred:ok")

    async def sync_pull(self, since: datetime) -> SyncRun:
        return _valid_sync_run(operation="pull")

    async def sync_push(self, payload: Dict[str, Any]) -> SyncRun:
        return _valid_sync_run(operation="push")

    async def disconnect(self) -> None:
        return None


# ---------------------------------------------------------------------------
# IntegrationInstance model
# ---------------------------------------------------------------------------


class TestIntegrationInstanceModel:
    def test_valid_payload_round_trips(self):
        inst = IntegrationInstance(**_base_instance_kwargs())
        assert inst.instance_id == "integration_001"
        assert inst.tenant_id == "tenant-A"
        assert inst.provider_name == "quickbooks_online"
        assert inst.category == "accounting"
        assert inst.enabled is True
        assert inst.status == "connected"
        assert inst.config == {"realm_id": "1234567890"}

    def test_defaults_are_conservative(self):
        # Omit fields that should default to safe values.
        kwargs = _base_instance_kwargs()
        kwargs.pop("enabled")
        kwargs.pop("status")
        kwargs.pop("schedule_cron")
        kwargs.pop("credentials_ref")
        kwargs.pop("config")
        inst = IntegrationInstance(**kwargs)
        assert inst.enabled is False
        assert inst.status == "pending"
        assert inst.credentials_ref is None
        assert inst.schedule_cron is None
        assert inst.config == {}
        assert inst.retry_count == 0

    def test_rejects_blank_required_strings(self):
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(tenant_id="  "))
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(instance_id=""))
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(provider_name="  "))

    def test_rejects_unknown_category(self):
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(category="unknown"))

    def test_accepts_every_catalogued_category(self):
        for category in [
            "accounting",
            "tank_monitor",
            "gps_eld",
            "payment",
            "tms",
            "terminal_pricing",
        ]:
            IntegrationInstance(**_base_instance_kwargs(category=category))

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(status="broken"))

    def test_rejects_negative_retry_count(self):
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(retry_count=-1))

    def test_blank_optional_strings_normalize_to_none(self):
        inst = IntegrationInstance(
            **_base_instance_kwargs(credentials_ref="   ", schedule_cron="")
        )
        assert inst.credentials_ref is None
        assert inst.schedule_cron is None

    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            IntegrationInstance(**_base_instance_kwargs(unknown_field="x"))


# ---------------------------------------------------------------------------
# SyncRun model
# ---------------------------------------------------------------------------


class TestSyncRunModel:
    def test_valid_payload_round_trips(self):
        run = _valid_sync_run()
        assert run.run_id == "run_001"
        assert run.operation == "pull"
        assert run.status == "success"
        assert run.record_counts == {"invoices": 0}
        assert run.duration_ms == 1000

    def test_defaults(self):
        # Only the strictly required fields supplied.
        run = SyncRun(
            run_id="r1",
            tenant_id="tenant-A",
            instance_id="i1",
            provider_name="p",
            operation="pull",
            started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        assert run.status == "running"
        assert run.finished_at is None
        assert run.record_counts == {}
        assert run.error_details is None
        assert run.duration_ms is None

    def test_rejects_unknown_operation(self):
        with pytest.raises(ValidationError):
            _valid_sync_run(operation="sync")

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            _valid_sync_run(status="broken")

    def test_rejects_negative_record_counts(self):
        with pytest.raises(ValidationError):
            _valid_sync_run(record_counts={"invoices": -1})

    def test_rejects_boolean_record_counts(self):
        # ``bool`` is a subclass of ``int`` in Python — the validator
        # must explicitly reject it to keep the counts sane.
        with pytest.raises(ValidationError):
            _valid_sync_run(record_counts={"invoices": True})

    def test_rejects_negative_duration_ms(self):
        with pytest.raises(ValidationError):
            _valid_sync_run(duration_ms=-5)

    def test_blank_error_details_normalize_to_none(self):
        run = _valid_sync_run(error_details="   ")
        assert run.error_details is None

    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            _valid_sync_run(unknown_field=1)


# ---------------------------------------------------------------------------
# ConnectionResult model
# ---------------------------------------------------------------------------


class TestConnectionResultModel:
    def test_default_status_is_connected(self):
        result = ConnectionResult()
        assert result.status == "connected"
        assert result.credentials_ref is None
        assert result.metadata == {}
        assert result.message is None

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            ConnectionResult(status="broken")

    def test_blank_optional_strings_normalize_to_none(self):
        result = ConnectionResult(credentials_ref="  ", message="   ")
        assert result.credentials_ref is None
        assert result.message is None


# ---------------------------------------------------------------------------
# Repository: construction
# ---------------------------------------------------------------------------


class TestRepositoryConstruction:
    def test_rejects_none_es_service(self):
        with pytest.raises(ValueError):
            IntegrationInstanceRepository(es_service=None)  # type: ignore[arg-type]

    def test_rejects_empty_index_name(self, es: _FakeESService):
        with pytest.raises(ValueError):
            IntegrationInstanceRepository(es_service=es, index_name="")

    @pytest.mark.asyncio
    async def test_defaults_to_canonical_index(self, es: _FakeESService):
        repo = IntegrationInstanceRepository(es_service=es)
        await repo.list_for_tenant("tenant-A")
        assert es.search_calls[-1]["index"] == INTEGRATION_INSTANCES_INDEX


# ---------------------------------------------------------------------------
# Repository: create
# ---------------------------------------------------------------------------


class TestRepositoryCreate:
    @pytest.mark.asyncio
    async def test_create_persists_payload(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        inst = IntegrationInstance(**_base_instance_kwargs())
        result = await repo.create("tenant-A", inst)
        assert result.instance_id == "integration_001"
        assert len(es.index_calls) == 1
        call = es.index_calls[0]
        assert call["index"] == INTEGRATION_INSTANCES_INDEX
        assert call["id"] == "integration_001"
        assert call["doc"]["tenant_id"] == "tenant-A"
        assert call["doc"]["created_at"]
        assert call["doc"]["updated_at"]

    @pytest.mark.asyncio
    async def test_create_from_dict_coerces_to_model(
        self, repo: IntegrationInstanceRepository
    ):
        result = await repo.create("tenant-A", _base_instance_kwargs())
        assert isinstance(result, IntegrationInstance)
        assert result.provider_name == "quickbooks_online"

    @pytest.mark.asyncio
    async def test_create_stamps_tenant_when_omitted(
        self, repo: IntegrationInstanceRepository
    ):
        payload = _base_instance_kwargs()
        payload.pop("tenant_id")
        result = await repo.create("tenant-A", payload)
        assert result.tenant_id == "tenant-A"

    @pytest.mark.asyncio
    async def test_create_mints_id_when_omitted(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        payload = _base_instance_kwargs()
        payload.pop("instance_id")
        result = await repo.create("tenant-A", payload)
        assert result.instance_id.startswith("integration_")
        assert es.index_calls[0]["id"] == result.instance_id

    @pytest.mark.asyncio
    async def test_create_rejects_cross_tenant_payload(
        self, repo: IntegrationInstanceRepository
    ):
        payload = _base_instance_kwargs(tenant_id="tenant-B")
        with pytest.raises(CrossTenantAccessError):
            await repo.create("tenant-A", payload)

    @pytest.mark.asyncio
    async def test_create_rejects_empty_tenant_id(
        self, repo: IntegrationInstanceRepository
    ):
        with pytest.raises(ValueError):
            await repo.create("  ", _base_instance_kwargs())

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_type(
        self, repo: IntegrationInstanceRepository
    ):
        with pytest.raises(TypeError):
            await repo.create("tenant-A", 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository: get
# ---------------------------------------------------------------------------


class TestRepositoryGet:
    @pytest.mark.asyncio
    async def test_get_returns_model_for_owned_instance(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        got = await repo.get("tenant-A", "integration_001")
        assert got is not None
        assert got.instance_id == "integration_001"
        assert got.tenant_id == "tenant-A"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_instance(
        self, repo: IntegrationInstanceRepository
    ):
        got = await repo.get("tenant-A", "does-not-exist")
        assert got is None

    @pytest.mark.asyncio
    async def test_get_returns_none_for_cross_tenant_instance(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-B", _base_instance_kwargs(tenant_id="tenant-B")
        )
        # Tenant A sees nothing — no existence leak.
        got = await repo.get("tenant-A", "integration_001")
        assert got is None

    @pytest.mark.asyncio
    async def test_get_rejects_empty_instance_id(
        self, repo: IntegrationInstanceRepository
    ):
        with pytest.raises(ValueError):
            await repo.get("tenant-A", "")

    @pytest.mark.asyncio
    async def test_get_rejects_empty_tenant_id(
        self, repo: IntegrationInstanceRepository
    ):
        with pytest.raises(ValueError):
            await repo.get("", "integration_001")


# ---------------------------------------------------------------------------
# Repository: list_for_tenant
# ---------------------------------------------------------------------------


class TestRepositoryList:
    @pytest.mark.asyncio
    async def test_list_filters_to_requesting_tenant_only(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-A", _base_instance_kwargs(instance_id="a1")
        )
        await repo.create(
            "tenant-A", _base_instance_kwargs(instance_id="a2")
        )
        await repo.create(
            "tenant-B",
            _base_instance_kwargs(instance_id="b1", tenant_id="tenant-B"),
        )

        got = await repo.list_for_tenant("tenant-A")
        ids = sorted(i.instance_id for i in got)
        assert ids == ["a1", "a2"]

    @pytest.mark.asyncio
    async def test_list_filters_by_provider_name(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(
                instance_id="qbo", provider_name="quickbooks_online"
            ),
        )
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(
                instance_id="vr",
                provider_name="veeder_root",
                category="tank_monitor",
            ),
        )
        got = await repo.list_for_tenant(
            "tenant-A", provider_name="veeder_root"
        )
        assert [i.instance_id for i in got] == ["vr"]

    @pytest.mark.asyncio
    async def test_list_filters_by_category(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-A", _base_instance_kwargs(instance_id="acct")
        )
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(
                instance_id="pay",
                provider_name="stripe",
                category="payment",
            ),
        )
        got = await repo.list_for_tenant("tenant-A", category="payment")
        assert [i.instance_id for i in got] == ["pay"]

    @pytest.mark.asyncio
    async def test_list_filters_by_enabled(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(instance_id="on", enabled=True),
        )
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(instance_id="off", enabled=False),
        )
        got = await repo.list_for_tenant("tenant-A", enabled=False)
        assert [i.instance_id for i in got] == ["off"]

    @pytest.mark.asyncio
    async def test_list_filters_by_status(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(instance_id="ok", status="connected"),
        )
        await repo.create(
            "tenant-A",
            _base_instance_kwargs(instance_id="bad", status="error"),
        )
        got = await repo.list_for_tenant("tenant-A", status="error")
        assert [i.instance_id for i in got] == ["bad"]

    @pytest.mark.asyncio
    async def test_list_drops_corrupt_records_without_raising(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        await repo.create(
            "tenant-A", _base_instance_kwargs(instance_id="good")
        )
        es.docs["bad"] = {
            "instance_id": "bad",
            "tenant_id": "tenant-A",
            # Missing a raft of required fields.
        }
        got = await repo.list_for_tenant("tenant-A")
        assert [i.instance_id for i in got] == ["good"]

    @pytest.mark.asyncio
    async def test_list_rejects_non_positive_size(
        self, repo: IntegrationInstanceRepository
    ):
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
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        updated = await repo.update(
            "tenant-A",
            "integration_001",
            {"enabled": False, "status": "disconnected"},
        )
        assert updated is not None
        assert updated.enabled is False
        assert updated.status == "disconnected"
        partial = es.update_calls[-1]["partial"]
        assert "enabled" in partial
        assert "status" in partial
        assert "updated_at" in partial
        # Immutable fields never leak into the partial update.
        assert "tenant_id" not in partial
        assert "instance_id" not in partial
        assert "provider_name" not in partial

    @pytest.mark.asyncio
    async def test_update_rejects_cross_tenant_write(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create(
            "tenant-B", _base_instance_kwargs(tenant_id="tenant-B")
        )
        with pytest.raises(CrossTenantAccessError):
            await repo.update(
                "tenant-A", "integration_001", {"enabled": False}
            )

    @pytest.mark.asyncio
    async def test_update_returns_none_for_missing_instance(
        self, repo: IntegrationInstanceRepository
    ):
        got = await repo.update(
            "tenant-A", "missing", {"enabled": False}
        )
        assert got is None

    @pytest.mark.asyncio
    async def test_update_strips_immutable_fields_silently(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        patch = {
            "tenant_id": "tenant-X",  # ignored
            "instance_id": "integration_999",  # ignored
            "provider_name": "other_provider",  # ignored
            "category": "payment",  # ignored
            "created_at": "1970-01-01T00:00:00+00:00",  # ignored
            "enabled": False,  # applied
        }
        updated = await repo.update(
            "tenant-A", "integration_001", patch
        )
        assert updated is not None
        assert updated.tenant_id == "tenant-A"
        assert updated.instance_id == "integration_001"
        assert updated.provider_name == "quickbooks_online"
        assert updated.category == "accounting"
        assert updated.enabled is False
        partial = es.update_calls[-1]["partial"]
        assert partial.keys() == {"enabled", "updated_at"}

    @pytest.mark.asyncio
    async def test_update_no_op_patch_returns_current_model(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        before_updates = len(es.update_calls)
        updated = await repo.update(
            "tenant-A",
            "integration_001",
            {"tenant_id": "tenant-X", "instance_id": "integration_999"},
        )
        assert updated is not None
        assert updated.instance_id == "integration_001"
        assert len(es.update_calls) == before_updates

    @pytest.mark.asyncio
    async def test_update_rejects_non_dict_patch(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        with pytest.raises(TypeError):
            await repo.update(
                "tenant-A", "integration_001", "not a dict"  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_status(
        self, repo: IntegrationInstanceRepository
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        with pytest.raises(ValidationError):
            await repo.update(
                "tenant-A", "integration_001", {"status": "broken"}
            )


# ---------------------------------------------------------------------------
# Repository: delete
# ---------------------------------------------------------------------------


class TestRepositoryDelete:
    @pytest.mark.asyncio
    async def test_delete_owned_instance_returns_true(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        await repo.create("tenant-A", _base_instance_kwargs())
        result = await repo.delete("tenant-A", "integration_001")
        assert result is True
        assert "integration_001" in es.delete_calls
        assert "integration_001" not in es.docs

    @pytest.mark.asyncio
    async def test_delete_missing_instance_returns_false(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        result = await repo.delete("tenant-A", "missing")
        assert result is False
        assert es.delete_calls == []

    @pytest.mark.asyncio
    async def test_delete_cross_tenant_raises(
        self, repo: IntegrationInstanceRepository, es: _FakeESService
    ):
        await repo.create(
            "tenant-B", _base_instance_kwargs(tenant_id="tenant-B")
        )
        with pytest.raises(CrossTenantAccessError):
            await repo.delete("tenant-A", "integration_001")
        # Instance still exists — delete was blocked.
        assert "integration_001" in es.docs

    @pytest.mark.asyncio
    async def test_delete_rejects_empty_ids(
        self, repo: IntegrationInstanceRepository
    ):
        with pytest.raises(ValueError):
            await repo.delete("", "integration_001")
        with pytest.raises(ValueError):
            await repo.delete("tenant-A", "")
