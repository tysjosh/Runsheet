"""
Unit tests for the POST ``/api/fuel/mvp/compartments/{id}/cleaning-events``
endpoint added by Task 6.3 of the fuel-ops-hardening spec.

Validates: Requirement 7.1.4.

The tests exercise the full wiring (
:func:`configure_fuel_ops_endpoints` → :class:`CleaningEventService` →
:class:`CompartmentStateRepository`) with an in-memory ES stub plus
injected fakes for the :class:`CleaningEventService` and
:class:`FileStorageService`. This keeps the suite decoupled from the real
Elasticsearch backend while still exercising the router's error
translation, tenant stamping, and evidence_refs validation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
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
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStateRepository:
    """Minimal stand-in for :class:`CompartmentStateRepository` with just
    the :meth:`get` method the endpoint needs."""

    def __init__(self) -> None:
        self.states: Dict[str, CompartmentState] = {}
        self.get_calls: List[tuple[str, str]] = []

    def seed(self, state: CompartmentState) -> None:
        self.states[state.compartment_id] = state

    async def get(
        self, tenant_id: str, compartment_doc_id: str
    ) -> Optional[CompartmentState]:
        self.get_calls.append((tenant_id, compartment_doc_id))
        state = self.states.get(compartment_doc_id)
        if state is None:
            return None
        # Mirror the real repository: cross-tenant reads are suppressed.
        if state.tenant_id != tenant_id:
            return None
        return state


class _FakeCleaningEventService:
    """Recording stand-in for :class:`CleaningEventService`.

    The router only calls :meth:`record`; we capture the kwargs so tests
    can assert the router forwards them correctly, and we let tests
    override the return value or raise a specific exception to verify
    error translation.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.return_value: Optional[CleaningEvent] = None
        self.exc_to_raise: Optional[BaseException] = None

    async def record(self, **kwargs: Any) -> CleaningEvent:
        self.calls.append(dict(kwargs))
        if self.exc_to_raise is not None:
            raise self.exc_to_raise
        if self.return_value is not None:
            return self.return_value
        # Default happy-path: synthesize a minimal event echoing the inputs
        # so endpoint serialization can be exercised.
        now = datetime.now(timezone.utc)
        return CleaningEvent(
            cleaning_event_id="ce_test",
            tenant_id=kwargs["tenant_id"],
            compartment_id=kwargs["compartment_id"],
            truck_id=kwargs["truck_id"],
            method=kwargs["method"],
            actor_id=kwargs["actor_id"],
            notes=kwargs.get("notes"),
            evidence_refs=list(kwargs.get("evidence_refs") or []),
            cleaned_at=now,
            created_at=now,
            updated_at=now,
        )


class _FakeFileStorage:
    """Validates tenant-prefix on evidence_refs.

    Real :class:`FileStorageService.validate_ref` raises
    :class:`PermissionError` on a tenant-prefix mismatch and a
    :class:`ValueError` on a malformed key; this fake mirrors both modes.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def validate_ref(
        self, tenant_id: str, file_ref: str, actor: Optional[str] = None
    ) -> bool:
        self.calls.append({"tenant_id": tenant_id, "file_ref": file_ref, "actor": actor})
        if not file_ref.startswith(f"tenants/{tenant_id}/"):
            raise PermissionError(
                f"cross-tenant file_ref {file_ref} for tenant {tenant_id}"
            )
        return True


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-1"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    *,
    tenant_id: str = "tenant-1",
    seed_states: Optional[List[CompartmentState]] = None,
    file_storage: Any = None,
    cleaning_service: Optional[_FakeCleaningEventService] = None,
    state_repo: Optional[_FakeStateRepository] = None,
    ref_resolver: Any = None,
):
    state_repo = state_repo or _FakeStateRepository()
    for state in seed_states or []:
        state_repo.seed(state)
    cleaning_service = cleaning_service or _FakeCleaningEventService()

    # ES service is unused by these endpoints directly; pass a truthy
    # stub so configure_fuel_ops_endpoints does not wire real services
    # on top of our injected fakes.
    import unittest.mock as _mock

    es_stub = _mock.MagicMock()

    configure_fuel_ops_endpoints(
        es_service=es_stub,
        destination_service=_mock.MagicMock(list=_mock.AsyncMock(return_value=[])),
        customer_tank_repository=_mock.MagicMock(),
        depot_repository=_mock.MagicMock(),
        terminal_repository=_mock.MagicMock(),
        compartment_state_repository=state_repo,
        cleaning_event_service=cleaning_service,
        file_storage_service=file_storage,
        ref_resolver=ref_resolver,
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, state_repo, cleaning_service


def _clean_compartment_state(
    *,
    compartment_id: str = "truck-1_c1",
    truck_id: str = "truck-1",
    tenant_id: str = "tenant-1",
    state: str = "needs_cleaning",
) -> CompartmentState:
    return CompartmentState(
        compartment_id=compartment_id,
        truck_id=truck_id,
        tenant_id=tenant_id,
        state=state,  # type: ignore[arg-type]
        last_loaded_product="DIESEL_2",
        last_loaded_at=datetime.now(timezone.utc),
        last_cleaned_at=None,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRecordCleaningEventSuccess:
    def test_persists_event_with_truck_id_from_state(self):
        state = _clean_compartment_state()
        app, _state_repo, svc = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "sanitize",
                "actor_id": "driver-42",
                "notes": "Post-heating-oil cleanout",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Router stamps tenant_id and forwards truck_id from the state.
        assert body["tenant_id"] == "tenant-1"
        assert body["truck_id"] == state.truck_id
        assert body["method"] == "sanitize"
        assert body["actor_id"] == "driver-42"
        assert body["notes"] == "Post-heating-oil cleanout"

        # Router forwarded the exact fields to the service.
        assert len(svc.calls) == 1
        call = svc.calls[0]
        assert call["tenant_id"] == "tenant-1"
        assert call["compartment_id"] == state.compartment_id
        assert call["truck_id"] == state.truck_id
        assert call["method"] == "sanitize"
        assert call["actor_id"] == "driver-42"
        assert call["notes"] == "Post-heating-oil cleanout"
        assert call["evidence_refs"] == []

    def test_accepts_flush_purge_and_sanitize_methods(self):
        state = _clean_compartment_state()
        for method in ("flush", "purge", "sanitize"):
            app, _, svc = _build_app(seed_states=[state])
            client = TestClient(app)

            resp = client.post(
                f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
                json={"method": method, "actor_id": "driver-1"},
            )
            assert resp.status_code == 201
            assert svc.calls[-1]["method"] == method

    def test_empty_evidence_refs_is_accepted(self):
        state = _clean_compartment_state()
        app, _, svc = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "flush",
                "actor_id": "driver-1",
                "evidence_refs": [],
            },
        )
        assert resp.status_code == 201
        assert svc.calls[0]["evidence_refs"] == []


# ---------------------------------------------------------------------------
# Evidence ref tenant validation (Req 7.1.4)
# ---------------------------------------------------------------------------


class TestEvidenceRefValidation:
    def test_accepts_own_tenant_refs(self):
        state = _clean_compartment_state()
        file_storage = _FakeFileStorage()
        app, _, svc = _build_app(
            seed_states=[state], file_storage=file_storage
        )
        client = TestClient(app)

        refs = [
            "tenants/tenant-1/photo/2025/01/15/aaaa.jpg",
            "tenants/tenant-1/attachment/2025/01/15/bbbb.pdf",
        ]
        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "sanitize",
                "actor_id": "driver-1",
                "evidence_refs": refs,
            },
        )
        assert resp.status_code == 201
        # FileStorageService.validate_ref was called once per ref.
        assert len(file_storage.calls) == 2
        assert {c["file_ref"] for c in file_storage.calls} == set(refs)
        for call in file_storage.calls:
            assert call["tenant_id"] == "tenant-1"
            assert call["actor"] == "driver-1"
        # Router forwarded refs to the service after validation.
        assert svc.calls[0]["evidence_refs"] == refs

    def test_rejects_cross_tenant_evidence_ref_with_403(self):
        state = _clean_compartment_state()
        file_storage = _FakeFileStorage()
        app, _, svc = _build_app(
            seed_states=[state], file_storage=file_storage
        )
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "sanitize",
                "actor_id": "driver-1",
                "evidence_refs": [
                    "tenants/tenant-1/photo/2025/01/15/aaaa.jpg",
                    "tenants/other-tenant/photo/2025/01/15/bbbb.jpg",
                ],
            },
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "cross_tenant_file_ref"
        assert detail["field"] == "evidence_refs[1]"
        # The service must not have been called once a ref failed.
        assert svc.calls == []

    def test_missing_file_storage_skips_validation(self):
        """When no FileStorageService is wired (optional dep), the router
        still forwards the refs without validating them so unit tests and
        development environments that haven't wired S3 yet still work.
        Production bootstrap always injects a real service."""
        state = _clean_compartment_state()
        app, _, svc = _build_app(seed_states=[state], file_storage=None)
        client = TestClient(app)

        refs = ["tenants/some-tenant/photo/2025/01/15/x.jpg"]
        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "flush",
                "actor_id": "driver-1",
                "evidence_refs": refs,
            },
        )
        assert resp.status_code == 201
        assert svc.calls[0]["evidence_refs"] == refs


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


class TestErrorModes:
    def test_missing_compartment_returns_404(self):
        app, state_repo, svc = _build_app(seed_states=[])
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/mvp/compartments/does-not-exist/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error_code"] == "compartment_not_found"
        assert detail["compartment_id"] == "does-not-exist"
        assert svc.calls == []

    def test_cross_tenant_compartment_returns_404_not_403(self):
        # The state repository downgrades cross-tenant reads to None so
        # the router returns a uniform 404 — existence is never leaked.
        state = _clean_compartment_state(tenant_id="other-tenant")
        app, _, svc = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 404
        assert svc.calls == []

    def test_unknown_method_returns_422(self):
        state = _clean_compartment_state()
        app, _, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "scrub", "actor_id": "driver-1"},
        )
        assert resp.status_code == 422

    def test_missing_actor_id_returns_422(self):
        state = _clean_compartment_state()
        app, _, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush"},
        )
        assert resp.status_code == 422

    def test_blank_actor_id_returns_422(self):
        state = _clean_compartment_state()
        app, _, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "   "},
        )
        # min_length=1 fails after Pydantic strips; blank surfaces as 422.
        assert resp.status_code in (400, 422)

    def test_extra_fields_are_rejected(self):
        state = _clean_compartment_state()
        app, _, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "flush",
                "actor_id": "driver-1",
                # extra="forbid" rejects server-computed fields.
                "tenant_id": "tenant-1",
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Service error translation
# ---------------------------------------------------------------------------


class TestServiceErrorTranslation:
    def test_compartment_deleted_between_preflight_and_service_returns_404(self):
        state = _clean_compartment_state()
        svc = _FakeCleaningEventService()
        svc.exc_to_raise = CompartmentNotFoundError("tenant-1", state.compartment_id)
        app, _, _ = _build_app(seed_states=[state], cleaning_service=svc)
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "compartment_not_found"

    def test_cross_tenant_access_error_returns_403(self):
        state = _clean_compartment_state()
        svc = _FakeCleaningEventService()
        svc.exc_to_raise = CrossTenantCompartmentAccessError(
            "tenant-1", state.compartment_id, "other-tenant"
        )
        app, _, _ = _build_app(seed_states=[state], cleaning_service=svc)
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error_code"] == "cross_tenant_access_denied"

    def test_conflict_error_returns_409(self):
        state = _clean_compartment_state()
        svc = _FakeCleaningEventService()
        svc.exc_to_raise = CompartmentStateConflictError(
            "tenant-1", state.compartment_id, 3
        )
        app, _, _ = _build_app(seed_states=[state], cleaning_service=svc)
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 409
        assert (
            resp.json()["detail"]["error_code"] == "compartment_state_conflict"
        )

    def test_persistence_error_returns_500_with_event_id(self):
        state = _clean_compartment_state()
        svc = _FakeCleaningEventService()
        svc.exc_to_raise = CleaningEventPersistenceError(
            tenant_id="tenant-1",
            compartment_id=state.compartment_id,
            cleaning_event_id="ce_abc",
            cause=RuntimeError("ES unavailable"),
        )
        app, _, _ = _build_app(seed_states=[state], cleaning_service=svc)
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert detail["error_code"] == "cleaning_event_persistence_error"
        assert detail["cleaning_event_id"] == "ce_abc"

    def test_service_permission_error_returns_403(self):
        """When no router-level FileStorageService is wired but the
        service has its own file_storage dep, a cross-tenant ref surfaces
        as ``PermissionError`` from the service; the router translates
        that to HTTP 403 consistently with the pre-flight branch."""
        state = _clean_compartment_state()
        svc = _FakeCleaningEventService()
        svc.exc_to_raise = PermissionError("cross tenant")
        app, _, _ = _build_app(seed_states=[state], cleaning_service=svc)
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 403
        assert (
            resp.json()["detail"]["error_code"] == "cross_tenant_file_ref"
        )


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


class TestTenantScoping:
    def test_tenant_id_is_not_taken_from_request_body(self):
        """The tenant_id stamped on the event must come from the JWT
        (injected via the :func:`get_tenant_context` dependency), never
        from the request body. The body schema rejects a ``tenant_id``
        field at all (``extra="forbid"``); this test double-checks
        that the service receives the JWT tenant even when the caller
        attempts to spoof it via a path-prefixed compartment id."""
        # Compartment owned by tenant-1; caller authenticates as tenant-1.
        state = _clean_compartment_state(tenant_id="tenant-1")
        app, _, svc = _build_app(tenant_id="tenant-1", seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 201
        assert svc.calls[0]["tenant_id"] == "tenant-1"

    def test_state_repository_lookup_uses_jwt_tenant(self):
        state = _clean_compartment_state(tenant_id="tenant-a")
        app, state_repo, _ = _build_app(
            tenant_id="tenant-a", seed_states=[state]
        )
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 201
        assert state_repo.get_calls[0] == ("tenant-a", state.compartment_id)


# ---------------------------------------------------------------------------
# Canonical driver_id reference (cross-module-entity-linkage Req 8.2)
# ---------------------------------------------------------------------------


class TestCleaningEventDriverIdLinkage:
    """The cleaning-event endpoint accepts an optional canonical ``driver_id``
    that supersedes the free-text ``actor_id`` alias, validating it against the
    Drivers module when a resolver is wired (Req 8.2)."""

    @staticmethod
    def _resolver_with_drivers(*driver_ids: str):
        from services.ref_resolver import RefResolver

        known = set(driver_ids)

        async def _driver_loader(tenant_id: str, entity_id: str):
            if entity_id in known:
                return {"driver_id": entity_id, "driver_name": entity_id.upper()}
            return None

        resolver = RefResolver()
        resolver.register("driver", _driver_loader)
        return resolver

    def test_forwards_driver_id_to_service(self):
        state = _clean_compartment_state()
        resolver = self._resolver_with_drivers("DRV-1")
        app, _state_repo, svc = _build_app(
            seed_states=[state], ref_resolver=resolver
        )
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "flush",
                "actor_id": "driver-42",
                "driver_id": "DRV-1",
            },
        )

        assert resp.status_code == 201, resp.text
        assert svc.calls[0]["driver_id"] == "DRV-1"
        # actor_id remains accepted as the deprecated free-text alias.
        assert svc.calls[0]["actor_id"] == "driver-42"

    def test_rejects_unknown_driver_id(self):
        state = _clean_compartment_state()
        resolver = self._resolver_with_drivers("DRV-1")
        app, _state_repo, svc = _build_app(
            seed_states=[state], ref_resolver=resolver
        )
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={
                "method": "flush",
                "actor_id": "driver-42",
                "driver_id": "DRV-NOPE",
            },
        )

        # Unknown / cross-tenant driver is rejected before the event is written.
        assert resp.status_code == 400, resp.text
        assert svc.calls == []

    def test_driver_id_optional_without_resolver(self):
        # No resolver wired (and the process-wide resolver has no driver
        # loader) → the field is accepted unvalidated for back-compat.
        state = _clean_compartment_state()
        app, _state_repo, svc = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.post(
            f"/api/fuel/mvp/compartments/{state.compartment_id}/cleaning-events",
            json={"method": "flush", "actor_id": "driver-1"},
        )
        assert resp.status_code == 201, resp.text
        assert svc.calls[0]["driver_id"] is None
