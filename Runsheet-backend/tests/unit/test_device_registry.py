"""
Unit tests for the Device_Registry: ``driver/services/device_registry.py`` and
the ``PUT`` / ``DELETE /api/driver/devices/{device_id}`` router.

The router is wired to a **real** :class:`DeviceRegistry` over a fake
Elasticsearch, so the assertions cover the whole path: the composite document id
that makes a re-registration a replacement rather than a duplicate, the token
stored verbatim, and the delete that can only ever reach the caller's own
record.

The sharp assertions are about *scope*. A body-supplied ``driver_id`` changes
nothing, and one driver's sign-out cannot remove another driver's — or another
tenant's — registration for the same ``device_id``, because neither can address
the id.

Validates: Requirements 9.1, 9.2, 9.3, 9.18
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.device_endpoints import (
    configure_device_endpoints,
    router as device_router,
)
from driver.services.device_registry import (
    DeviceRegistry,
    driver_device_doc_id,
)
from driver.services.driver_es_mappings import (
    DRIVER_DEVICES_INDEX,
    DRIVER_DEVICES_MAPPING,
)
from errors.codes import ErrorCode
from errors.exceptions import AppException
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"
OTHER_TENANT = "t2"
DRIVER = "drv_1"
OTHER_DRIVER = "drv_2"
DEVICE = "device-abc"
#: Deliberately padded and mixed-case: the registry stores it byte-for-byte.
TOKEN = "  OpaqueToken[AbC-123_xyz]  "
NEW_TOKEN = "OpaqueToken[rotated-999]"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeES:
    """In-memory ``driver_devices`` store keyed by document id."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.indexed: List[tuple] = []
        self.deleted: List[tuple] = []

    async def index_document(self, index, doc_id, document):
        self.indexed.append((index, doc_id, dict(document)))
        self.docs[doc_id] = dict(document)
        return {"result": "created"}

    async def get_document(self, index, doc_id):
        record = self.docs.get(doc_id)
        return dict(record) if record is not None else None

    async def delete_document(self, index, doc_id):
        self.deleted.append((index, doc_id))
        return self.docs.pop(doc_id, None) is not None


def _make_app(es_service: Any) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(device_router)
    configure_device_endpoints(es_service=es_service)
    install_test_auth(app)
    return app


def _driver_headers(
    driver_id: str = DRIVER, tenant_id: str = TENANT, **kwargs
) -> dict:
    kwargs.setdefault("roles", ["driver"])
    return auth_headers(tenant_id, sub="user-1", driver_id=driver_id, **kwargs)


def _register_body(**overrides) -> dict:
    body = {"push_token": TOKEN, "platform": "ios", "app_version": "1.0.0"}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# PUT /api/driver/devices/{device_id}
# ---------------------------------------------------------------------------


class TestRegisterDevice:
    """Registration and re-registration."""

    def test_persists_the_required_fields_under_the_composite_id(self):
        """Validates: Requirements 9.1"""
        es = FakeES()
        client = TestClient(_make_app(es))

        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["tenant_id"] == TENANT
        assert data["driver_id"] == DRIVER
        assert data["device_id"] == DEVICE
        assert data["platform"] == "ios"
        assert data["registered_at"]
        assert data["replaced"] is False
        # The token is never handed back on a response.
        assert "push_token" not in data

        assert len(es.indexed) == 1
        index, doc_id, doc = es.indexed[0]
        assert index == DRIVER_DEVICES_INDEX
        assert doc_id == f"{TENANT}:{DRIVER}:{DEVICE}"
        assert doc["device_registration_id"] == doc_id

    def test_reregistration_replaces_rather_than_duplicates(self):
        """R9.2: a rotated token replaces the record; no second row appears."""
        es = FakeES()
        client = TestClient(_make_app(es))

        first = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )
        second = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(push_token=NEW_TOKEN),
            headers=_driver_headers(),
        )

        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["data"]["replaced"] is True

        # Two writes, one document, one id.
        assert len(es.indexed) == 2
        assert {doc_id for _, doc_id, _ in es.indexed} == {
            f"{TENANT}:{DRIVER}:{DEVICE}"
        }
        assert len(es.docs) == 1
        assert es.docs[f"{TENANT}:{DRIVER}:{DEVICE}"]["push_token"] == NEW_TOKEN

    def test_reregistration_refreshes_last_seen_at_and_keeps_registered_at(self):
        """The app's 24-hour refresh is a liveness signal, not a new record."""
        es = FakeES()
        client = TestClient(_make_app(es))

        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )

        # Simulate the record as it stands when the app refreshes 24 hours on.
        stale = "2026-05-01T00:00:00+00:00"
        record = es.docs[f"{TENANT}:{DRIVER}:{DEVICE}"]
        record["registered_at"] = stale
        record["last_seen_at"] = stale

        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )

        data = resp.json()["data"]
        # ``registered_at`` survives the replacement; ``last_seen_at`` moves.
        assert data["registered_at"] == stale
        assert data["last_seen_at"] != stale
        assert es.docs[f"{TENANT}:{DRIVER}:{DEVICE}"]["last_seen_at"] == (
            data["last_seen_at"]
        )

    def test_push_token_is_stored_verbatim(self):
        """R9.18: not trimmed, not normalized, not format-checked."""
        es = FakeES()
        client = TestClient(_make_app(es))

        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(push_token=TOKEN),
            headers=_driver_headers(),
        )

        assert es.docs[f"{TENANT}:{DRIVER}:{DEVICE}"]["push_token"] == TOKEN

    def test_opaque_token_from_another_provider_is_accepted(self):
        """R9.18: a provider change needs no registry change."""
        es = FakeES()
        client = TestClient(_make_app(es))

        opaque = "fcm:abcdef0123456789/not-an-expo-shape"
        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(push_token=opaque),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert es.docs[f"{TENANT}:{DRIVER}:{DEVICE}"]["push_token"] == opaque

    def test_body_supplied_driver_id_is_ignored(self):
        """The subject is the session's driver, never a body value."""
        es = FakeES()
        client = TestClient(_make_app(es))

        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(driver_id=OTHER_DRIVER),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["driver_id"] == DRIVER
        assert list(es.docs) == [f"{TENANT}:{DRIVER}:{DEVICE}"]
        assert es.docs[f"{TENANT}:{DRIVER}:{DEVICE}"]["driver_id"] == DRIVER

    def test_unknown_platform_is_rejected(self):
        es = FakeES()
        client = TestClient(_make_app(es))

        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(platform="blackberry"),
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert es.docs == {}

    def test_session_without_a_driver_identity_is_403(self):
        """R1.6: a driver surface needs a driver identity."""
        es = FakeES()
        client = TestClient(_make_app(es))

        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=auth_headers(TENANT, roles=["driver"]),
        )

        assert resp.status_code == 403
        assert es.docs == {}


# ---------------------------------------------------------------------------
# DELETE /api/driver/devices/{device_id}
# ---------------------------------------------------------------------------


class TestUnregisterDevice:
    """Sign-out."""

    def test_delete_removes_the_record(self):
        """Validates: Requirements 9.3"""
        es = FakeES()
        client = TestClient(_make_app(es))

        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )
        resp = client.delete(
            f"/api/driver/devices/{DEVICE}", headers=_driver_headers()
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True
        assert es.docs == {}
        assert es.deleted == [(DRIVER_DEVICES_INDEX, f"{TENANT}:{DRIVER}:{DEVICE}")]

    def test_delete_of_an_absent_record_is_not_a_failure(self):
        """A repeated sign-out must not fail a driver who has already gone."""
        es = FakeES()
        client = TestClient(_make_app(es))

        resp = client.delete(
            f"/api/driver/devices/{DEVICE}", headers=_driver_headers()
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is False

    def test_another_driver_cannot_delete_this_drivers_registration(self):
        """Cross-driver: the id carries the driver, so the record is unreachable."""
        es = FakeES()
        client = TestClient(_make_app(es))

        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )
        resp = client.delete(
            f"/api/driver/devices/{DEVICE}",
            headers=_driver_headers(driver_id=OTHER_DRIVER),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is False
        assert f"{TENANT}:{DRIVER}:{DEVICE}" in es.docs

    def test_another_tenant_cannot_delete_this_tenants_registration(self):
        """Cross-tenant: same device_id, same driver_id, different tenant."""
        es = FakeES()
        client = TestClient(_make_app(es))

        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )
        resp = client.delete(
            f"/api/driver/devices/{DEVICE}",
            headers=_driver_headers(tenant_id=OTHER_TENANT),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is False
        assert f"{TENANT}:{DRIVER}:{DEVICE}" in es.docs

    def test_two_drivers_sharing_a_device_id_hold_separate_records(self):
        """The composite id keeps the two registrations independent."""
        es = FakeES()
        client = TestClient(_make_app(es))

        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )
        client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(push_token=NEW_TOKEN),
            headers=_driver_headers(driver_id=OTHER_DRIVER),
        )

        assert set(es.docs) == {
            f"{TENANT}:{DRIVER}:{DEVICE}",
            f"{TENANT}:{OTHER_DRIVER}:{DEVICE}",
        }


# ---------------------------------------------------------------------------
# Service-level and wiring properties
# ---------------------------------------------------------------------------


class TestRegistryService:
    """Direct assertions on :class:`DeviceRegistry`."""

    @pytest.mark.asyncio
    async def test_get_ignores_a_record_from_another_scope(self):
        es = FakeES()
        registry = DeviceRegistry(es_service=es)

        await registry.register(
            TENANT, DRIVER, DEVICE, push_token=TOKEN, platform="android"
        )

        assert await registry.get(TENANT, DRIVER, DEVICE) is not None
        assert await registry.get(TENANT, OTHER_DRIVER, DEVICE) is None
        assert await registry.get(OTHER_TENANT, DRIVER, DEVICE) is None

    @pytest.mark.asyncio
    async def test_blank_token_is_rejected(self):
        registry = DeviceRegistry(es_service=FakeES())

        with pytest.raises(AppException) as excinfo:
            await registry.register(
                TENANT, DRIVER, DEVICE, push_token="   ", platform="ios"
            )

        assert excinfo.value.error_code == ErrorCode.INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_registry_without_a_store_fails_closed(self):
        registry = DeviceRegistry(es_service=None)

        with pytest.raises(AppException) as excinfo:
            await registry.register(
                TENANT, DRIVER, DEVICE, push_token=TOKEN, platform="ios"
            )

        assert excinfo.value.error_code == ErrorCode.INTERNAL_ERROR

    def test_doc_id_shape_matches_the_dispatcher_prune_key(self):
        """The registry and the push dispatcher address one document."""
        assert driver_device_doc_id(TENANT, DRIVER, DEVICE) == (
            f"{TENANT}:{DRIVER}:{DEVICE}"
        )

    def test_push_token_is_declared_unindexed(self):
        """R9.18: retrievable, never queryable."""
        properties = DRIVER_DEVICES_MAPPING["mappings"]["properties"]

        assert properties["push_token"]["index"] is False

    @pytest.mark.asyncio
    async def test_registry_writes_only_declared_fields(self):
        """``driver_devices`` is ``dynamic: strict`` — an extra field is a 400."""
        declared = set(DRIVER_DEVICES_MAPPING["mappings"]["properties"])
        es = FakeES()
        registry = DeviceRegistry(es_service=es)

        await registry.register(
            TENANT, DRIVER, DEVICE, push_token=TOKEN, platform="ios"
        )

        _index, _doc_id, document = es.indexed[0]
        assert set(document).issubset(declared)


class TestWiring:
    """``configure_device_endpoints`` fails closed when unwired."""

    def test_unconfigured_router_returns_a_structured_error(self):
        client = TestClient(_make_app(None))

        resp = client.put(
            f"/api/driver/devices/{DEVICE}",
            json=_register_body(),
            headers=_driver_headers(),
        )

        assert resp.status_code == 500
        assert "device" in str(resp.json()).lower()

    def test_registry_module_names_no_push_provider(self):
        """R9.15: only the dispatcher module may name the provider."""
        from pathlib import Path

        import driver.services.device_registry as registry_module
        import driver.api.device_endpoints as endpoints_module

        for module in (registry_module, endpoints_module):
            source = Path(module.__file__).read_text().lower()
            assert "expo" not in source
            assert "exp.host" not in source
