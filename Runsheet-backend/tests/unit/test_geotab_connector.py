"""
Unit tests for :mod:`integrations.geotab`.

Covers Capability 5 / Task 9.6 / Requirements 5.4.1–5.4.5 of the
fuel-ops hardening spec:

* ``connect`` validates the credential shape, performs
  ``Authenticate`` via the injected SDK call, and persists the
  envelope (including ``session_id`` + ``session_expires_at``) to the
  Tenant_Credentials_Vault (Req 5.4.1, 5.1.8).
* ``sync_pull`` fetches ``DeviceStatusInfo`` + ``DutyStatusLog``
  records, normalizes them into telemetry rows with location,
  speed_kph, engine_on, odometer_km, and hos_status, and persists
  each to the ``truck_telemetry`` ES index (Req 5.4.2, 5.4.3).
* ``trucks.current_location`` is updated only when the telemetry
  reading age is < 300 seconds (Req 5.4.4).
* Session-token renewal: an ``InvalidUserException`` (or HTTP 403 with
  that vendor code) triggers exactly one re-authenticate + retry
  (Req 5.4.5). A second failure surfaces as
  ``status="error"``/``error_details="session_expired: ..."``.
* Unmapped devices are persisted to ``truck_telemetry`` with
  ``truck_id=None`` so they remain visible in diagnostic dashboards.
* ``sync_push`` is a no-op; ``disconnect`` is idempotent.
* The provider catalog entry is registered on demand and matches the
  expected shape (Task 9.10 dependency).

External dependencies (``mygeotab``, ``httpx``, Redis, the vault, ES)
are all replaced with in-memory fakes so the tests are hermetic and
run without any network surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

import pytest

from integrations.connector_base import ConnectionResult, SyncRun
from integrations.geotab import (
    DEFAULT_FRESHNESS_SECONDS,
    DEFAULT_SCHEDULE_CRON,
    DEFAULT_SERVER,
    GeotabConnector,
    SESSION_EXPIRED_REASON,
    VAULT_CREDENTIAL_KEY,
    build_catalog_entry,
    register_catalog_entry,
)
from integrations.provider_catalog import clear_registry, get_provider


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeVault:
    """In-memory stand-in for :class:`TenantCredentialsVault`."""

    def __init__(self, seed: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._store: Dict[str, Dict[str, Any]] = dict(seed or {})
        self._seq = 0
        self.put_calls: List[Dict[str, Any]] = []
        self.get_calls: List[str] = []
        self.delete_calls: List[str] = []

    async def put(
        self,
        *,
        tenant_id: str,
        key: str,
        plaintext: Dict[str, Any],
        provider_name: Optional[str] = None,
    ) -> str:
        self._seq += 1
        ref = f"cred:{tenant_id}:{key}:{self._seq}"
        self._store[ref] = {"tenant_id": tenant_id, "plaintext": dict(plaintext)}
        self.put_calls.append(
            {
                "ref": ref,
                "tenant_id": tenant_id,
                "key": key,
                "plaintext": dict(plaintext),
                "provider_name": provider_name,
            }
        )
        return ref

    async def get(self, tenant_id: str, ref: str) -> Dict[str, Any]:
        self.get_calls.append(ref)
        entry = self._store.get(ref)
        if entry is None:
            raise KeyError(ref)
        if entry["tenant_id"] != tenant_id:
            raise PermissionError("cross_tenant")
        return dict(entry["plaintext"])

    async def delete(self, tenant_id: str, ref: str) -> bool:
        self.delete_calls.append(ref)
        return self._store.pop(ref, None) is not None


class _RecordingES:
    """ES fake recording every write so tests can assert on them."""

    def __init__(self) -> None:
        self.indexed: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.indexed.append(
            {"index": index, "doc_id": doc_id, "document": dict(document)}
        )

    async def update_document(
        self, index: str, doc_id: str, partial_doc: Dict[str, Any]
    ) -> None:
        self.updates.append(
            {"index": index, "doc_id": doc_id, "partial": dict(partial_doc)}
        )


class _ScriptedSDK:
    """Callable stand-in for the injected ``sdk_call`` parameter.

    Uses a per-method queue of response callables so tests can
    schedule heterogeneous responses (success, InvalidUserException,
    then success again after re-auth).
    """

    def __init__(self) -> None:
        self._scripts: Dict[str, List[Any]] = {}
        self.calls: List[Dict[str, Any]] = []

    def enqueue(self, method: str, response: Any) -> None:
        self._scripts.setdefault(method, []).append(response)

    def __call__(
        self,
        *,
        method: str,
        params: Mapping[str, Any],
        server: Optional[str] = None,
        session_id: Optional[str] = None,
        database: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "params": dict(params),
                "server": server,
                "session_id": session_id,
                "database": database,
                "username": username,
            }
        )
        queue = self._scripts.get(method) or []
        if not queue:
            raise AssertionError(
                f"unexpected SDK call {method}: no scripted response"
            )
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params)
        return response


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault() -> _FakeVault:
    return _FakeVault()


@pytest.fixture
def es() -> _RecordingES:
    return _RecordingES()


@pytest.fixture
def sdk() -> _ScriptedSDK:
    return _ScriptedSDK()


@pytest.fixture
def tenant_id() -> str:
    return "tenant-a"


@pytest.fixture
def instance_id() -> str:
    return "inst-geotab-1"


@pytest.fixture
def base_config() -> Dict[str, Any]:
    return {
        "device_map": {
            "device-1": "truck-1",
            "device-2": "truck-2",
        },
        "server": DEFAULT_SERVER,
    }


def _make_connector(
    *,
    tenant_id: str,
    instance_id: str,
    base_config: Dict[str, Any],
    vault: _FakeVault,
    es: _RecordingES,
    sdk: _ScriptedSDK,
    credentials_ref: Optional[str] = None,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
    clock: Optional[Any] = None,
) -> GeotabConnector:
    return GeotabConnector(
        tenant_id=tenant_id,
        instance_id=instance_id,
        instance_config=base_config,
        credentials_vault=vault,
        credentials_ref=credentials_ref,
        es_service=es,
        sdk_call=sdk,
        freshness_seconds=freshness_seconds,
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )


# ---------------------------------------------------------------------------
# Catalog registration
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_build_catalog_entry_shape(self):
        entry = build_catalog_entry()
        assert entry.provider_name == "geotab"
        assert entry.category == "gps_eld"
        assert set(entry.required_credential_fields) == {
            "username",
            "password",
            "database",
            "server",
        }
        # Marketplace visibility flag defaults to
        # overlay.integration.{provider_name} via
        # effective_feature_flag_key (Req 5.6.6).
        assert entry.feature_flag_key is None
        assert entry.effective_feature_flag_key() == "overlay.integration.geotab"

    def test_register_catalog_entry_registers_once(self):
        clear_registry()
        try:
            entry = register_catalog_entry()
            assert entry.provider_name == "geotab"
            assert get_provider("geotab") is not None
        finally:
            clear_registry()


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_authenticates_and_persists_envelope(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        sdk.enqueue(
            "Authenticate",
            {
                "result": {
                    "credentials": {
                        "sessionId": "sess-abc",
                        "database": "example_co",
                        "userName": "ops@example.com",
                    },
                    "path": "my17.geotab.com",
                }
            },
        )
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
        )
        result = await connector.connect(
            {
                "username": "ops@example.com",
                "password": "hunter2",
                "database": "example_co",
                "server": "my.geotab.com",
            }
        )
        assert isinstance(result, ConnectionResult)
        assert result.status == "connected"
        assert result.credentials_ref is not None
        assert result.metadata == {
            "database": "example_co",
            "server": "my17.geotab.com",
        }
        # Vault call shape.
        assert len(vault.put_calls) == 1
        put = vault.put_calls[0]
        assert put["tenant_id"] == tenant_id
        assert put["key"] == VAULT_CREDENTIAL_KEY
        assert put["provider_name"] == "geotab"
        env = put["plaintext"]
        assert env["username"] == "ops@example.com"
        assert env["password"] == "hunter2"
        assert env["database"] == "example_co"
        assert env["server"] == "my17.geotab.com"
        assert env["session_id"] == "sess-abc"
        assert "session_expires_at" in env
        # SDK was asked to Authenticate with the plaintext password.
        sdk_call = sdk.calls[0]
        assert sdk_call["method"] == "Authenticate"
        assert sdk_call["params"]["userName"] == "ops@example.com"
        assert sdk_call["params"]["password"] == "hunter2"
        assert sdk_call["params"]["database"] == "example_co"

    @pytest.mark.asyncio
    async def test_rejects_missing_fields(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
        )
        result = await connector.connect({"username": "u"})
        assert result.status == "error"
        assert "password" in (result.message or "") or "missing" in (
            result.message or ""
        )
        assert vault.put_calls == []

    @pytest.mark.asyncio
    async def test_surfaces_authenticate_failure_as_error(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        sdk.enqueue(
            "Authenticate",
            {
                "error": {
                    "errors": [
                        {"name": "InvalidUserException", "message": "bad creds"}
                    ]
                }
            },
        )
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
        )
        result = await connector.connect(
            {
                "username": "u",
                "password": "p",
                "database": "d",
                "server": "s",
            }
        )
        assert result.status == "error"
        assert vault.put_calls == []


# ---------------------------------------------------------------------------
# sync_pull — happy path
# ---------------------------------------------------------------------------


class TestSyncPullHappyPath:
    @pytest.mark.asyncio
    async def test_fetches_persists_and_updates_trucks(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Seed the vault with a ready envelope.
        ref = f"cred:{tenant_id}:{VAULT_CREDENTIAL_KEY}:seed"
        vault._store[ref] = {
            "tenant_id": tenant_id,
            "plaintext": {
                "username": "ops@example.com",
                "password": "hunter2",
                "database": "example_co",
                "server": "my17.geotab.com",
                "session_id": "sess-abc",
                "session_expires_at": recent,
            },
        }
        sdk.enqueue(
            "Get",
            {
                "result": [
                    {
                        "device": {"id": "device-1", "name": "Truck 1"},
                        "latitude": 40.75,
                        "longitude": -73.99,
                        "speed": 48.5,
                        "isDeviceCommunicating": True,
                        "odometer": 123456.7,
                        "driver": {"id": "driver-x"},
                        "dateTime": recent,
                    },
                    {
                        "device": {"id": "device-2", "name": "Truck 2"},
                        "latitude": 40.72,
                        "longitude": -74.00,
                        "speed": 0.0,
                        "isDeviceCommunicating": False,
                        "odometer": 98765.1,
                        "driver": "UnknownDriverId",
                        "dateTime": recent,
                    },
                ]
            },
        )
        sdk.enqueue(
            "Get",
            {
                "result": [
                    {"device": {"id": "device-1"}, "status": "D"},
                ]
            },
        )
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
            credentials_ref=ref,
            clock=lambda: now,
        )
        since = now - timedelta(minutes=5)
        run = await connector.sync_pull(since)
        assert isinstance(run, SyncRun)
        assert run.operation == "pull"
        assert run.status == "success"
        assert run.record_counts["readings_fetched"] == 2
        assert run.record_counts["readings_persisted"] == 2
        assert run.record_counts["trucks_updated"] == 2
        assert run.record_counts["skipped_unmapped"] == 0
        # truck_telemetry writes.
        telem = [w for w in es.indexed if w["index"] == "truck_telemetry"]
        assert len(telem) == 2
        by_truck = {w["document"]["truck_id"]: w["document"] for w in telem}
        truck1 = by_truck["truck-1"]
        assert truck1["tenant_id"] == tenant_id
        assert truck1["speed_kph"] == pytest.approx(48.5)
        assert truck1["engine_on"] is True
        assert truck1["odometer_km"] == pytest.approx(123456.7)
        assert truck1["hos_status"] == "D"
        assert truck1["driver_id"] == "driver-x"
        assert truck1["location"] == {"lat": 40.75, "lon": -73.99}
        truck2 = by_truck["truck-2"]
        assert truck2["engine_on"] is False
        # UnknownDriverId should be normalized out.
        assert truck2["driver_id"] is None
        # hos lookup missed device-2 → hos_status is None.
        assert truck2["hos_status"] is None
        # trucks updates carried coordinates + recorded_at.
        updates_by_id = {u["doc_id"]: u for u in es.updates if u["index"] == "trucks"}
        assert set(updates_by_id) == {"truck-1", "truck-2"}
        for upd in updates_by_id.values():
            loc = upd["partial"]["current_location"]["coordinates"]
            assert -180 <= loc["lon"] <= 180
            assert -90 <= loc["lat"] <= 90
            assert "current_location_at" in upd["partial"]

    @pytest.mark.asyncio
    async def test_unmapped_device_persists_telemetry_with_null_truck(
        self, vault, es, sdk, tenant_id, instance_id
    ):
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ref = f"cred:{tenant_id}:{VAULT_CREDENTIAL_KEY}:seed"
        vault._store[ref] = {
            "tenant_id": tenant_id,
            "plaintext": {
                "username": "u",
                "password": "p",
                "database": "d",
                "server": "s",
                "session_id": "sess-abc",
                "session_expires_at": recent,
            },
        }
        # device-999 is not in the device_map.
        sdk.enqueue(
            "Get",
            {
                "result": [
                    {
                        "device": {"id": "device-999"},
                        "latitude": 10.0,
                        "longitude": 20.0,
                        "speed": 5.0,
                        "dateTime": recent,
                    }
                ]
            },
        )
        sdk.enqueue("Get", {"result": []})
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config={"device_map": {"device-1": "truck-1"}},
            vault=vault,
            es=es,
            sdk=sdk,
            credentials_ref=ref,
            clock=lambda: now,
        )
        run = await connector.sync_pull(now - timedelta(minutes=5))
        assert run.status == "success"
        assert run.record_counts["readings_persisted"] == 1
        assert run.record_counts["skipped_unmapped"] == 1
        assert run.record_counts["trucks_updated"] == 0
        telem = [w for w in es.indexed if w["index"] == "truck_telemetry"]
        assert len(telem) == 1
        assert telem[0]["document"]["truck_id"] is None
        # Trucks index untouched.
        assert [u for u in es.updates if u["index"] == "trucks"] == []


# ---------------------------------------------------------------------------
# Freshness gate (Req 5.4.4)
# ---------------------------------------------------------------------------


class TestFreshnessGate:
    @pytest.mark.asyncio
    async def test_stale_reading_persists_but_does_not_update_truck(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        # Reading age is 600 seconds — older than the 300-second gate.
        stale = (now - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ref = f"cred:{tenant_id}:{VAULT_CREDENTIAL_KEY}:seed"
        vault._store[ref] = {
            "tenant_id": tenant_id,
            "plaintext": {
                "username": "u",
                "password": "p",
                "database": "d",
                "server": "s",
                "session_id": "sess-abc",
                "session_expires_at": stale,
            },
        }
        sdk.enqueue(
            "Get",
            {
                "result": [
                    {
                        "device": {"id": "device-1"},
                        "latitude": 40.75,
                        "longitude": -73.99,
                        "speed": 0.0,
                        "dateTime": stale,
                    }
                ]
            },
        )
        sdk.enqueue("Get", {"result": []})
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
            credentials_ref=ref,
            clock=lambda: now,
        )
        run = await connector.sync_pull(now - timedelta(hours=1))
        assert run.status == "success"
        assert run.record_counts["readings_persisted"] == 1
        assert run.record_counts["trucks_updated"] == 0
        assert run.record_counts["skipped_stale"] == 1
        # Telemetry was still persisted.
        assert [w for w in es.indexed if w["index"] == "truck_telemetry"]
        # No trucks index update.
        assert [u for u in es.updates if u["index"] == "trucks"] == []


# ---------------------------------------------------------------------------
# Session renewal (Req 5.4.5)
# ---------------------------------------------------------------------------


class TestSessionRenewal:
    @pytest.mark.asyncio
    async def test_reauth_once_and_retry_succeeds(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ref = f"cred:{tenant_id}:{VAULT_CREDENTIAL_KEY}:seed"
        vault._store[ref] = {
            "tenant_id": tenant_id,
            "plaintext": {
                "username": "ops@example.com",
                "password": "hunter2",
                "database": "example_co",
                "server": "my.geotab.com",
                "session_id": "expired-session",
                "session_expires_at": recent,
            },
        }
        # First Get → InvalidUserException; re-auth; second Get succeeds.
        sdk.enqueue(
            "Get",
            {
                "error": {
                    "errors": [
                        {
                            "name": "InvalidUserException",
                            "message": "session expired",
                        }
                    ]
                }
            },
        )
        sdk.enqueue(
            "Authenticate",
            {
                "result": {
                    "credentials": {
                        "sessionId": "fresh-session",
                        "database": "example_co",
                        "userName": "ops@example.com",
                    },
                    "path": "my.geotab.com",
                }
            },
        )
        sdk.enqueue(
            "Get",
            {
                "result": [
                    {
                        "device": {"id": "device-1"},
                        "latitude": 40.75,
                        "longitude": -73.99,
                        "speed": 10.0,
                        "dateTime": recent,
                    }
                ]
            },
        )
        # Second method Get request is for DutyStatusLog.
        sdk.enqueue("Get", {"result": []})
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
            credentials_ref=ref,
            clock=lambda: now,
        )
        run = await connector.sync_pull(now - timedelta(minutes=5))
        assert run.status == "success"
        assert run.record_counts["readings_persisted"] == 1
        # Re-auth happened exactly once.
        auth_calls = [c for c in sdk.calls if c["method"] == "Authenticate"]
        assert len(auth_calls) == 1
        # Rotated session was persisted back to the vault.
        assert any(
            put["plaintext"].get("session_id") == "fresh-session"
            for put in vault.put_calls
        )

    @pytest.mark.asyncio
    async def test_second_failure_surfaces_session_expired(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ref = f"cred:{tenant_id}:{VAULT_CREDENTIAL_KEY}:seed"
        vault._store[ref] = {
            "tenant_id": tenant_id,
            "plaintext": {
                "username": "ops@example.com",
                "password": "hunter2",
                "database": "example_co",
                "server": "my.geotab.com",
                "session_id": "expired-session",
                "session_expires_at": recent,
            },
        }
        # First Get → InvalidUserException; re-auth succeeds; retry
        # still returns InvalidUserException.
        invalid_user = {
            "error": {
                "errors": [
                    {
                        "name": "InvalidUserException",
                        "message": "session expired",
                    }
                ]
            }
        }
        sdk.enqueue("Get", invalid_user)
        sdk.enqueue(
            "Authenticate",
            {
                "result": {
                    "credentials": {
                        "sessionId": "new-but-still-broken",
                        "database": "example_co",
                        "userName": "ops@example.com",
                    },
                    "path": "my.geotab.com",
                }
            },
        )
        sdk.enqueue("Get", invalid_user)
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
            credentials_ref=ref,
            clock=lambda: now,
        )
        run = await connector.sync_pull(now - timedelta(minutes=5))
        assert run.status == "error"
        assert run.error_details and SESSION_EXPIRED_REASON in run.error_details
        # Only one re-auth attempt.
        auth_calls = [c for c in sdk.calls if c["method"] == "Authenticate"]
        assert len(auth_calls) == 1


# ---------------------------------------------------------------------------
# sync_push + disconnect
# ---------------------------------------------------------------------------


class TestSyncPushAndDisconnect:
    @pytest.mark.asyncio
    async def test_sync_push_is_noop(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
        )
        run = await connector.sync_push({"anything": 1})
        assert run.operation == "push"
        assert run.status == "success"
        assert run.record_counts == {"skipped_noop": 1}
        # No SDK calls and no ES writes.
        assert sdk.calls == []
        assert es.indexed == []
        assert es.updates == []

    @pytest.mark.asyncio
    async def test_disconnect_deletes_vault_envelope(
        self, vault, es, sdk, tenant_id, instance_id, base_config
    ):
        ref = f"cred:{tenant_id}:{VAULT_CREDENTIAL_KEY}:seed"
        vault._store[ref] = {
            "tenant_id": tenant_id,
            "plaintext": {"session_id": "s"},
        }
        connector = _make_connector(
            tenant_id=tenant_id,
            instance_id=instance_id,
            base_config=base_config,
            vault=vault,
            es=es,
            sdk=sdk,
            credentials_ref=ref,
        )
        await connector.disconnect()
        assert vault.delete_calls == [ref]
        # Second disconnect is a no-op.
        await connector.disconnect()
        assert vault.delete_calls == [ref]


# ---------------------------------------------------------------------------
# Module-level constants sanity
# ---------------------------------------------------------------------------


def test_default_schedule_is_every_minute():
    assert DEFAULT_SCHEDULE_CRON == "* * * * *"


def test_default_freshness_is_300_seconds():
    assert DEFAULT_FRESHNESS_SECONDS == 300
