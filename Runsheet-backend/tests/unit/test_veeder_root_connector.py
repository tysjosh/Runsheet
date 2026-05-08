"""
Unit tests for :mod:`integrations.veeder_root`.

Covers Capability 5 / Task 9.5 / Requirements 5.3.1–5.3.6 of the fuel-ops
hardening spec:

* ``connect`` persists the credential envelope into the
  Tenant_Credentials_Vault and validates the per-mode config shape
  (Req 5.3.1, 5.3.2, 5.1.8).
* ``sync_pull`` retrieves volume / water / temperature readings via
  both the api-token HTTPS transport and the TLS-401 TCP transport,
  updates the matching :class:`CustomerTank` (or ``fuel_stations``
  record), and persists every reading to ``atg_readings``
  (Req 5.3.3, 5.3.4).
* High water-level readings publish a ``water_contamination``
  RiskSignal on the SignalBus with ``Severity.HIGH`` and respect the
  tenant-configurable Redis threshold (Req 5.3.6).
* ``sync_push`` is a no-op (tank monitors are read-only).
* The provider catalog entry is registered on demand and matches the
  expected shape.

External dependencies (httpx, asyncio sockets, Redis, the vault, ES,
the Customer_Tank repository, and the SignalBus) are all replaced with
in-memory fakes so the tests are hermetic and run without any network
surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import AsyncMock

import httpx
import pytest

from Agents.overlay.data_contracts import RiskSignal, Severity
from integrations.connector_base import ConnectionResult, SyncRun
from integrations.provider_catalog import clear_registry, get_provider
from integrations.veeder_root import (
    DEFAULT_SCHEDULE_CRON,
    DEFAULT_WATER_THRESHOLD_IN,
    MODE_API_TOKEN,
    MODE_TLS_401_TCP,
    VAULT_CREDENTIAL_KEY,
    WATER_CONTAMINATION_SIGNAL_TYPE,
    WATER_THRESHOLD_REDIS_KEY_TEMPLATE,
    VeederRootConnector,
    _build_tls_401_request,
    _parse_api_token_response,
    _parse_tls_401_response,
    build_catalog_entry,
    register_catalog_entry,
)


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


class _RecordingSignalBus:
    """Captures every :class:`RiskSignal` published by the connector."""

    def __init__(self) -> None:
        self.signals: List[RiskSignal] = []

    async def publish(self, signal: Any) -> int:
        self.signals.append(signal)
        return 1


class _FakeRedis:
    """Minimal async Redis stub supporting get only."""

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self.values: Dict[str, Any] = dict(values or {})
        self.get_calls: List[str] = []

    async def get(self, key: str) -> Optional[Any]:
        self.get_calls.append(key)
        return self.values.get(key)


class _StubResponse:
    """Minimal httpx.Response look-alike."""

    def __init__(self, status_code: int, body: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> Dict[str, Any]:
        return dict(self._body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://example.invalid"),
                response=httpx.Response(self.status_code),
            )


class _ScriptedHTTPClient:
    """Async HTTP client returning scripted responses for ``get``."""

    def __init__(self, responses: List[_StubResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> _StubResponse:
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            }
        )
        if not self._responses:
            raise AssertionError(f"unexpected GET {url}: no scripted response")
        return self._responses.pop(0)

    async def aclose(self) -> None:
        return None


class _FakeStreamReader:
    """asyncio.StreamReader look-alike returning a canned buffer."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._consumed = False

    async def readuntil(self, separator: bytes) -> bytes:
        if self._consumed:
            raise EOFError("already consumed")
        self._consumed = True
        idx = self._payload.find(separator)
        if idx == -1:
            # Mirror asyncio's IncompleteReadError behaviour.
            import asyncio

            raise asyncio.IncompleteReadError(self._payload, None)
        return self._payload[: idx + len(separator)]


class _FakeStreamWriter:
    """asyncio.StreamWriter look-alike recording ``write`` calls."""

    def __init__(self) -> None:
        self.written: List[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _make_tcp_connector(payload: bytes, writer: Optional[_FakeStreamWriter] = None):
    """Return a :data:`TCPConnectorFactory` yielding the supplied payload."""

    async def _connect(host: str, port: int):
        reader = _FakeStreamReader(payload)
        return reader, writer or _FakeStreamWriter()

    _connect.host_port_calls = []  # type: ignore[attr-defined]

    async def _wrapped(host: str, port: int):
        _connect.host_port_calls.append((host, port))  # type: ignore[attr-defined]
        return await _connect(host, port)

    return _wrapped


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_stores_envelope_for_api_token_mode(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_API_TOKEN,
                "endpoint_url": "https://insite360.example.com",
            },
            credentials_vault=vault,
        )
        result = await connector.connect({"api_token": "secret-token"})
        assert isinstance(result, ConnectionResult)
        assert result.status == "connected"
        assert result.credentials_ref is not None
        assert result.metadata == {"mode": MODE_API_TOKEN}
        # Vault call shape.
        assert len(vault.put_calls) == 1
        put = vault.put_calls[0]
        assert put["tenant_id"] == "tenant-a"
        assert put["key"] == VAULT_CREDENTIAL_KEY
        assert put["provider_name"] == "veeder_root"
        assert put["plaintext"] == {
            "mode": MODE_API_TOKEN,
            "api_token": "secret-token",
        }

    @pytest.mark.asyncio
    async def test_stores_envelope_for_tls_mode_with_security_code(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_TLS_401_TCP,
                "host": "console.example.com",
                "port": 10002,
            },
            credentials_vault=vault,
        )
        result = await connector.connect({"security_code": "123456"})
        assert result.status == "connected"
        assert vault.put_calls[0]["plaintext"] == {
            "mode": MODE_TLS_401_TCP,
            "security_code": "123456",
        }

    @pytest.mark.asyncio
    async def test_tls_mode_without_security_code_is_allowed(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_TLS_401_TCP,
                "host": "console.example.com",
            },
            credentials_vault=vault,
        )
        # No security_code in the credentials dict — still connects.
        result = await connector.connect({})
        assert result.status == "connected"
        # Envelope carries only the mode.
        assert vault.put_calls[0]["plaintext"] == {"mode": MODE_TLS_401_TCP}

    @pytest.mark.asyncio
    async def test_rejects_unsupported_mode(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={"mode": "carrier_pigeon"},
            credentials_vault=vault,
        )
        result = await connector.connect({"api_token": "x"})
        assert result.status == "error"
        assert "unsupported mode" in (result.message or "")
        assert vault.put_calls == []

    @pytest.mark.asyncio
    async def test_rejects_api_mode_without_token(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_API_TOKEN,
                "endpoint_url": "https://insite360.example.com",
            },
            credentials_vault=vault,
        )
        result = await connector.connect({})
        assert result.status == "error"
        assert "api_token" in (result.message or "")
        assert vault.put_calls == []

    @pytest.mark.asyncio
    async def test_rejects_api_mode_without_endpoint_url(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={"mode": MODE_API_TOKEN},
            credentials_vault=vault,
        )
        result = await connector.connect({"api_token": "secret"})
        assert result.status == "error"
        assert "endpoint_url" in (result.message or "")
        assert vault.put_calls == []

    @pytest.mark.asyncio
    async def test_rejects_tls_mode_without_host(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={"mode": MODE_TLS_401_TCP},
            credentials_vault=vault,
        )
        result = await connector.connect({"security_code": "000000"})
        assert result.status == "error"
        assert "host" in (result.message or "")
        assert vault.put_calls == []


# ---------------------------------------------------------------------------
# sync_pull — api_token mode
# ---------------------------------------------------------------------------


class TestSyncPullApiTokenMode:
    @pytest.mark.asyncio
    async def test_fetches_and_persists_readings_from_cloud_api(self):
        # Seed vault with a ready-to-use envelope so we can skip connect().
        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {
                        "mode": MODE_API_TOKEN,
                        "api_token": "secret-token",
                    },
                }
            }
        )
        http = _ScriptedHTTPClient(
            [
                _StubResponse(
                    200,
                    {
                        "tanks": [
                            {
                                "tank_id": 1,
                                "volume_gallons": 4200.5,
                                "water_level_in": 0.25,
                                "temperature_f": 58.9,
                                "reading_at": "2025-01-15T12:00:00Z",
                            },
                            {
                                "tank_id": 2,
                                "volume_gallons": 1800.0,
                                "water_level_in": 3.5,
                                "temperature_f": 60.1,
                                "reading_at": "2025-01-15T12:00:00Z",
                            },
                        ]
                    },
                )
            ]
        )
        es = _RecordingES()
        bus = _RecordingSignalBus()
        customer_tank_repo = AsyncMock()
        customer_tank_repo.update.return_value = object()  # truthy
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_API_TOKEN,
                "endpoint_url": "https://insite360.example.com",
                "tank_map": {
                    "1": {"target": "customer_tank", "id": "tank-A"},
                    "2": {"target": "customer_tank", "id": "tank-B"},
                },
            },
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
            es_service=es,
            customer_tank_repository=customer_tank_repo,
            signal_bus=bus,
            http_client=http,
        )

        since = datetime(2025, 1, 15, 11, 0, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)

        assert isinstance(run, SyncRun)
        assert run.operation == "pull"
        assert run.status == "success"
        assert run.record_counts["readings_fetched"] == 2
        assert run.record_counts["readings_persisted"] == 2
        assert run.record_counts["customer_tanks_updated"] == 2
        assert run.record_counts["water_contamination_signals"] == 1
        # HTTP call carried the bearer token and updated_since param.
        assert len(http.calls) == 1
        call = http.calls[0]
        assert call["headers"]["Authorization"] == "Bearer secret-token"
        assert call["params"]["updated_since"].startswith("2025-01-15T11:00:00")
        assert call["url"].startswith("https://insite360.example.com")
        # Two atg_readings writes, one per tank.
        atg = [w for w in es.indexed if w["index"] == "atg_readings"]
        assert len(atg) == 2
        # Each reading carries tenant + instance + customer_tank mapping.
        for write in atg:
            doc = write["document"]
            assert doc["tenant_id"] == "tenant-a"
            assert doc["instance_id"] == "inst-1"
            assert doc["customer_tank_id"] in {"tank-A", "tank-B"}
            assert doc["station_id"] is None
        # Customer_Tank repository received exactly two updates with the
        # right volumes.
        assert customer_tank_repo.update.await_count == 2
        calls_by_tank = {
            call.args[1]: call.args[2]
            for call in customer_tank_repo.update.await_args_list
        }
        assert calls_by_tank["tank-A"]["current_level_gallons"] == pytest.approx(
            4200.5
        )
        assert calls_by_tank["tank-B"]["current_level_gallons"] == pytest.approx(
            1800.0
        )
        # Exactly one contamination signal: tank 2 exceeded the default 2.0 threshold.
        assert len(bus.signals) == 1
        sig = bus.signals[0]
        assert isinstance(sig, RiskSignal)
        assert sig.severity == Severity.HIGH
        assert sig.tenant_id == "tenant-a"
        assert sig.entity_id == "tank-B"
        assert sig.context["signal_type"] == WATER_CONTAMINATION_SIGNAL_TYPE
        assert sig.context["water_level_in"] == pytest.approx(3.5)
        assert sig.context["threshold_in"] == DEFAULT_WATER_THRESHOLD_IN

    @pytest.mark.asyncio
    async def test_unmapped_readings_still_persist_but_do_not_update(self):
        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {
                        "mode": MODE_API_TOKEN,
                        "api_token": "tok",
                    },
                }
            }
        )
        http = _ScriptedHTTPClient(
            [
                _StubResponse(
                    200,
                    {
                        "tanks": [
                            {
                                "tank_id": 7,
                                "volume_gallons": 1000.0,
                                "water_level_in": 0.1,
                                "temperature_f": 52.0,
                            }
                        ]
                    },
                )
            ]
        )
        es = _RecordingES()
        customer_tank_repo = AsyncMock()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_API_TOKEN,
                "endpoint_url": "https://insite360.example.com",
                # Tank 7 intentionally unmapped.
                "tank_map": {},
            },
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
            es_service=es,
            customer_tank_repository=customer_tank_repo,
            http_client=http,
        )
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)
        assert run.status == "success"
        assert run.record_counts["readings_fetched"] == 1
        assert run.record_counts["readings_persisted"] == 1
        assert run.record_counts["customer_tanks_updated"] == 0
        assert run.record_counts["skipped_unmapped"] == 1
        customer_tank_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_override_raises_threshold(self):
        """A Redis override of 5.0 suppresses alerts below that level."""

        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {"mode": MODE_API_TOKEN, "api_token": "tok"},
                }
            }
        )
        http = _ScriptedHTTPClient(
            [
                _StubResponse(
                    200,
                    {
                        "tanks": [
                            {
                                "tank_id": 1,
                                "volume_gallons": 100.0,
                                "water_level_in": 3.0,
                                "temperature_f": 60.0,
                            }
                        ]
                    },
                )
            ]
        )
        es = _RecordingES()
        bus = _RecordingSignalBus()
        redis = _FakeRedis(
            values={
                WATER_THRESHOLD_REDIS_KEY_TEMPLATE.format(tenant_id="tenant-a"): "5.0"
            }
        )
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_API_TOKEN,
                "endpoint_url": "https://insite360.example.com",
                "tank_map": {"1": {"target": "customer_tank", "id": "tank-A"}},
            },
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
            es_service=es,
            customer_tank_repository=AsyncMock(update=AsyncMock(return_value=object())),
            signal_bus=bus,
            http_client=http,
            redis_client=redis,
        )
        run = await connector.sync_pull(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert run.record_counts["water_contamination_signals"] == 0
        assert bus.signals == []

    @pytest.mark.asyncio
    async def test_http_error_produces_terminal_error_syncrun(self):
        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {"mode": MODE_API_TOKEN, "api_token": "tok"},
                }
            }
        )
        http = _ScriptedHTTPClient([_StubResponse(503, {})])
        es = _RecordingES()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_API_TOKEN,
                "endpoint_url": "https://insite360.example.com",
            },
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
            es_service=es,
            http_client=http,
        )
        run = await connector.sync_pull(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert run.status == "error"
        # No readings persisted when the HTTP call failed.
        assert run.record_counts["readings_persisted"] == 0
        assert es.indexed == []


# ---------------------------------------------------------------------------
# sync_pull — tls_401_tcp mode
# ---------------------------------------------------------------------------


class TestSyncPullTlsMode:
    @pytest.mark.asyncio
    async def test_opens_socket_sends_i20100_and_parses_response(self):
        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {
                        "mode": MODE_TLS_401_TCP,
                        "security_code": "123456",
                    },
                }
            }
        )
        # Response carries two tanks with 11+ fields each. Field layout:
        # tank_id, volume, tc_volume, ullage, height, water, temp, ...
        body = (
            b"\x01I20100HEADER|"
            b"01,4200.5,4100.0,800.0,65.2,0.25,58.9,0,0,0,0|"
            b"02,1800.0,1750.0,1200.0,45.0,3.5,60.1,0,0,0,0"
            b"\x03"
        )
        writer = _FakeStreamWriter()
        factory = _make_tcp_connector(body, writer=writer)

        es = _RecordingES()
        bus = _RecordingSignalBus()
        repo = AsyncMock()
        repo.update.return_value = object()

        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_TLS_401_TCP,
                "host": "console.example.com",
                "port": 10002,
                "tank_map": {
                    "1": {"target": "customer_tank", "id": "tank-A"},
                    "2": {"target": "fuel_station", "id": "FS-100"},
                },
            },
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
            es_service=es,
            customer_tank_repository=repo,
            signal_bus=bus,
            tcp_connector=factory,
        )

        run = await connector.sync_pull(
            datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        assert run.status == "success"
        assert run.record_counts["readings_fetched"] == 2
        assert run.record_counts["customer_tanks_updated"] == 1
        assert run.record_counts["fuel_stations_updated"] == 1
        assert run.record_counts["water_contamination_signals"] == 1
        # Request bytes carried the SOH + security code + I20100 + ETX.
        assert len(writer.written) == 1
        req = writer.written[0]
        assert req == _build_tls_401_request("123456")
        # fuel_stations update went through the ES path with liters.
        station_updates = [
            u for u in es.updates if u["index"] == "fuel_stations"
        ]
        assert len(station_updates) == 1
        partial = station_updates[0]["partial"]
        # 1800 gal * 3.785411784 ≈ 6813.74 liters
        assert partial["current_stock_liters"] == pytest.approx(
            1800.0 * 3.785411784, rel=1e-6
        )
        # Customer_Tank repo received one call for tank-A.
        repo.update.assert_awaited_once()
        assert repo.update.await_args.args[1] == "tank-A"
        # Contamination signal fired for tank 2 (mapped to fuel_station FS-100).
        assert len(bus.signals) == 1
        sig = bus.signals[0]
        assert sig.entity_id == "FS-100"
        assert sig.entity_type == "fuel_station"
        assert sig.context["water_level_in"] == pytest.approx(3.5)

    @pytest.mark.asyncio
    async def test_malformed_response_raises_protocol_error(self):
        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {"mode": MODE_TLS_401_TCP},
                }
            }
        )
        # Response body has no pipe-delimited records, only a header.
        body = b"\x01I20100HEADER_ONLY\x03"
        factory = _make_tcp_connector(body)
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={
                "mode": MODE_TLS_401_TCP,
                "host": "console.example.com",
            },
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
            es_service=_RecordingES(),
            tcp_connector=factory,
        )
        run = await connector.sync_pull(
            datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        assert run.status == "error"
        assert "veeder_root_protocol_error" in (run.error_details or "")


# ---------------------------------------------------------------------------
# sync_push — read-only no-op
# ---------------------------------------------------------------------------


class TestSyncPush:
    @pytest.mark.asyncio
    async def test_sync_push_is_noop_success(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={"mode": MODE_API_TOKEN},
            credentials_vault=vault,
        )
        run = await connector.sync_push({"anything": "ignored"})
        assert isinstance(run, SyncRun)
        assert run.operation == "push"
        assert run.status == "success"
        assert run.record_counts == {"skipped_noop": 1}


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_deletes_vault_envelope(self):
        vault = _FakeVault(
            seed={
                "cred:tenant-a:veeder_root_creds:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {"mode": MODE_API_TOKEN, "api_token": "tok"},
                }
            }
        )
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={"mode": MODE_API_TOKEN},
            credentials_vault=vault,
            credentials_ref="cred:tenant-a:veeder_root_creds:seed",
        )
        await connector.disconnect()
        assert "cred:tenant-a:veeder_root_creds:seed" in vault.delete_calls

    @pytest.mark.asyncio
    async def test_disconnect_without_ref_is_noop(self):
        vault = _FakeVault()
        connector = VeederRootConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            instance_config={"mode": MODE_API_TOKEN},
            credentials_vault=vault,
        )
        await connector.disconnect()
        assert vault.delete_calls == []


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


class TestParsers:
    def test_parse_api_token_response_accepts_tanks_wrapper(self):
        readings = _parse_api_token_response(
            {
                "tanks": [
                    {
                        "tank_id": 3,
                        "volume_gallons": 500.0,
                        "water_level_in": 0.1,
                        "temperature_f": 55.5,
                    }
                ]
            }
        )
        assert len(readings) == 1
        r = readings[0]
        assert r["tank_id"] == 3
        assert r["volume_gallons"] == 500.0

    def test_parse_api_token_response_accepts_bare_list(self):
        readings = _parse_api_token_response(
            [
                {
                    "tank_id": "7",
                    "volume_gallons": "100.25",
                    "water_level_in": 0.2,
                    "temperature_f": 58.0,
                }
            ]
        )
        assert len(readings) == 1
        assert readings[0]["tank_id"] == 7
        assert readings[0]["volume_gallons"] == pytest.approx(100.25)

    def test_parse_api_token_response_drops_invalid_entries(self):
        readings = _parse_api_token_response(
            {
                "tanks": [
                    {"tank_id": 1, "volume_gallons": "abc", "water_level_in": 0.0, "temperature_f": 50.0},
                    {"tank_id": 2, "volume_gallons": 50.0, "water_level_in": 0.0, "temperature_f": 50.0},
                ]
            }
        )
        assert len(readings) == 1
        assert readings[0]["tank_id"] == 2

    def test_parse_tls_401_response_extracts_multiple_tanks(self):
        body = (
            b"\x01I20100HEADER|"
            b"01,2500.0,2400.0,500.0,60.0,0.1,55.0,0,0,0,0|"
            b"02,1000.0,950.0,1000.0,30.0,0.0,58.0,0,0,0,0"
            b"\x03"
        )
        readings = _parse_tls_401_response(body)
        assert len(readings) == 2
        assert readings[0]["tank_id"] == 1
        assert readings[0]["volume_gallons"] == 2500.0
        assert readings[0]["water_level_in"] == 0.1
        assert readings[0]["temperature_f"] == 55.0

    def test_parse_tls_401_response_skips_garbage_fields(self):
        # ``****`` is how the Veeder console prints offline probes.
        body = (
            b"\x01I20100HEADER|"
            b"01,****,****,****,****,****,****,0,0,0,0|"
            b"02,500.0,490.0,500.0,25.0,0.5,57.0,0,0,0,0"
            b"\x03"
        )
        readings = _parse_tls_401_response(body)
        assert len(readings) == 1
        assert readings[0]["tank_id"] == 2

    def test_parse_tls_401_response_handles_empty_payload(self):
        assert _parse_tls_401_response(b"") == []

    def test_build_tls_401_request_includes_security_code(self):
        req = _build_tls_401_request("654321")
        # Must start with SOH, then code, then I20100, end with ETX.
        assert req.startswith(b"\x01654321I20100")
        assert req.endswith(b"\x03")

    def test_build_tls_401_request_omits_blank_code(self):
        req = _build_tls_401_request("")
        assert req == b"\x01I20100\x03"
        req_none = _build_tls_401_request(None)
        assert req_none == b"\x01I20100\x03"


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------


class TestCatalogEntry:
    def setup_method(self) -> None:
        clear_registry()

    def teardown_method(self) -> None:
        clear_registry()

    def test_build_catalog_entry_matches_required_fields(self):
        entry = build_catalog_entry()
        assert entry.provider_name == "veeder_root"
        assert entry.category == "tank_monitor"
        assert "api_token" in entry.required_credential_fields
        assert "security_code" in entry.required_credential_fields
        # Marketplace visibility flag defaults to
        # overlay.integration.{provider_name} via
        # effective_feature_flag_key (Req 5.6.6).
        assert entry.feature_flag_key is None
        assert entry.effective_feature_flag_key() == (
            "overlay.integration.veeder_root"
        )
        # Description mentions the 15-minute default schedule so the
        # Marketplace UI can render it verbatim.
        assert "15" in entry.description

    def test_register_catalog_entry_adds_to_registry(self):
        assert get_provider("veeder_root") is None
        register_catalog_entry()
        entry = get_provider("veeder_root")
        assert entry is not None
        assert entry.category == "tank_monitor"


# ---------------------------------------------------------------------------
# Module-level constants sanity
# ---------------------------------------------------------------------------


def test_default_schedule_cron_is_every_15_minutes():
    assert DEFAULT_SCHEDULE_CRON == "*/15 * * * *"


def test_default_water_threshold_is_two_inches():
    assert DEFAULT_WATER_THRESHOLD_IN == 2.0
