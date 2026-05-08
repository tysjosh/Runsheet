"""
Unit tests for :mod:`integrations.quickbooks_online`.

Covers Capability 5 / Task 9.4 / Requirements 5.2.1–5.2.6 of the fuel-ops
hardening spec:

* ``connect`` persists the OAuth envelope into the Tenant_Credentials_Vault
  and returns a redacted :class:`ConnectionResult` (Req 5.2.1, 5.2.2,
  5.1.8).
* ``sync_push`` creates a QBO Invoice only when the
  ``overlay.qbo_invoice_push`` feature flag is in an active overlay state
  (Req 5.2.3) and shapes the body from the supplied POD payload.
* ``sync_pull`` imports Payments + Invoice status changes and routes
  them into :meth:`ReconciliationService.update_invoice_fields`
  (Req 5.2.4).
* A 401 response triggers exactly one refresh-token exchange; when the
  refresh succeeds the original request is retried and the rotated
  refresh_token is written back to the vault (Req 5.2.5). A failed
  refresh surfaces a terminal SyncRun with
  ``error_details`` containing the canonical
  ``credentials_expired`` reason.
* The per-tenant 500 req/min throttle is enforced via the
  ``qbo_ratelimit:{tenant_id}:{minute_bucket}`` Redis counter
  (Req 5.2.6).

The ``intuit-oauth`` library, httpx, ES, Redis, vault, feature flag, and
reconciliation service are all replaced with in-memory fakes so the
tests are hermetic and run without any network / AWS surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import httpx
import pytest

from integrations.connector_base import ConnectionResult, SyncRun
from integrations.provider_catalog import clear_registry, get_provider
from integrations.quickbooks_online import (
    CREDENTIALS_EXPIRED_REASON,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    QBO_INVOICE_PUSH_FLAG_KEY,
    QuickBooksCredentialsExpired,
    QuickBooksOnlineConnector,
    QuickBooksRateLimitExceeded,
    VAULT_CREDENTIAL_KEY,
    _build_invoice_body,
    _extract_invoice_gallons,
    _extract_payment_status,
    _minute_bucket,
    _rate_limit_key,
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
        self.rotate_calls: List[str] = []
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
            {"ref": ref, "tenant_id": tenant_id, "key": key, "plaintext": dict(plaintext)}
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

    async def rotate(self, tenant_id: str, ref: str) -> str:
        self.rotate_calls.append(ref)
        return ref

    async def delete(self, tenant_id: str, ref: str) -> bool:
        self.delete_calls.append(ref)
        return self._store.pop(ref, None) is not None


class _FakeFeatureFlags:
    """Exposes the ``get_overlay_state`` API the connector uses."""

    def __init__(self, state: str = "disabled") -> None:
        self._state = state
        self.calls: List[Dict[str, str]] = []

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        self.calls.append({"flag_key": flag_key, "tenant_id": tenant_id})
        return self._state


class _FakeRedis:
    """Minimal async Redis stub supporting incr / expire / get."""

    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self.ttls: Dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def get(self, key: str) -> Optional[str]:
        value = self.counters.get(key)
        return str(value) if value is not None else None

    async def set(self, key: str, value: Any) -> bool:
        self.counters[key] = int(value)
        return True


class _RecordingES:
    """ES fake that can seed reconciliation rows and record search calls."""

    def __init__(self, seed: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.docs: Dict[str, Dict[str, Any]] = dict(seed or {})
        self.search_calls: List[Dict[str, Any]] = []
        self.get_calls: List[str] = []

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int = 1
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": query, "size": size})
        must = query.get("query", {}).get("bool", {}).get("must") or []
        matched: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            ok = True
            for clause in must:
                for field, expected in (clause.get("term") or {}).items():
                    if doc.get(field) != expected:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                matched.append(doc)
        return {"hits": {"hits": [{"_source": dict(d)} for d in matched[:size]]}}

    async def get_document(self, index: str, doc_id: str) -> Optional[Dict[str, Any]]:
        self.get_calls.append(doc_id)
        doc = self.docs.get(doc_id)
        return dict(doc) if doc else None


class _StubResponse:
    """httpx-style response stand-in."""

    def __init__(
        self,
        status_code: int,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
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
    """Scripted async HTTP client returning canned responses per call."""

    def __init__(self, responses: List[_StubResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> _StubResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "json": dict(json) if isinstance(json, dict) else json,
                "headers": dict(headers or {}),
            }
        )
        if not self._responses:
            raise AssertionError(
                f"unexpected HTTP call: {method} {url} (no scripted response)"
            )
        return self._responses.pop(0)

    async def post(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> _StubResponse:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": dict(headers or {}),
                "data": dict(data or {}),
                "json": dict(json) if isinstance(json, dict) else json,
            }
        )
        if not self._responses:
            raise AssertionError(
                f"unexpected POST call: {url} (no scripted response)"
            )
        return self._responses.pop(0)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _seeded_connector(
    *,
    vault: Optional[_FakeVault] = None,
    feature_flags: Optional[_FakeFeatureFlags] = None,
    redis: Optional[_FakeRedis] = None,
    http: Optional[_ScriptedHTTPClient] = None,
    es: Optional[_RecordingES] = None,
    recon: Any = None,
    credentials_ref: str = "cred:tenant-a:qbo_oauth:seed",
) -> QuickBooksOnlineConnector:
    """Return a connector whose vault is pre-seeded with a known envelope."""

    v = vault or _FakeVault(
        seed={
            credentials_ref: {
                "tenant_id": "tenant-a",
                "plaintext": {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "refresh_token": "rt-old",
                    "realm_id": "realm-1",
                    "access_token": "at-old",
                    "token_expires_at": None,
                },
            }
        }
    )
    return QuickBooksOnlineConnector(
        tenant_id="tenant-a",
        instance_id="inst-1",
        credentials_vault=v,
        credentials_ref=credentials_ref,
        reconciliation_service=recon,
        feature_flag_service=feature_flags,
        redis_client=redis,
        http_client=http,
        es_service=es,
    )


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_stores_envelope_in_vault(self):
        vault = _FakeVault()
        connector = QuickBooksOnlineConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            credentials_vault=vault,
        )
        result = await connector.connect(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rt",
                "realm_id": "realm-1",
            }
        )
        assert isinstance(result, ConnectionResult)
        assert result.status == "connected"
        assert result.credentials_ref is not None
        assert result.metadata == {"realm_id": "realm-1"}
        assert len(vault.put_calls) == 1
        put = vault.put_calls[0]
        assert put["tenant_id"] == "tenant-a"
        assert put["key"] == VAULT_CREDENTIAL_KEY
        assert put["plaintext"]["refresh_token"] == "rt"

    @pytest.mark.asyncio
    async def test_returns_error_when_required_fields_missing(self):
        vault = _FakeVault()
        connector = QuickBooksOnlineConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            credentials_vault=vault,
        )
        result = await connector.connect(
            {"client_id": "cid", "client_secret": "csecret"}
        )
        assert result.status == "error"
        assert "refresh_token" in (result.message or "")
        # Nothing was persisted.
        assert vault.put_calls == []


# ---------------------------------------------------------------------------
# sync_push() — feature-flag gating + body shaping (Req 5.2.3)
# ---------------------------------------------------------------------------


class TestSyncPush:
    @pytest.mark.asyncio
    async def test_skips_when_feature_flag_disabled(self):
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="disabled"),
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 100.0,
                "unit_price_usd": 3.0,
            }
        )
        assert isinstance(run, SyncRun)
        assert run.operation == "push"
        assert run.status == "success"
        assert run.record_counts["invoices_pushed"] == 0
        assert run.record_counts["skipped_disabled"] == 1

    @pytest.mark.asyncio
    async def test_skips_when_feature_flag_is_shadow(self):
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="shadow"),
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 100.0,
                "unit_price_usd": 3.0,
            }
        )
        assert run.record_counts["skipped_disabled"] == 1

    @pytest.mark.asyncio
    async def test_creates_invoice_when_enabled(self):
        http = _ScriptedHTTPClient(
            [_StubResponse(200, {"Invoice": {"Id": "INV-42"}})]
        )
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "product_code": "DIESEL_2",
                "delivery_date": "2025-01-14",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.289,
            }
        )
        assert run.status == "success"
        assert run.record_counts["invoices_pushed"] == 1
        assert len(http.calls) == 1
        call = http.calls[0]
        assert call["method"] == "POST"
        assert "/v3/company/realm-1/invoice" in call["url"]
        assert call["json"]["CustomerRef"] == {"value": "cust-1"}
        line = call["json"]["Line"][0]
        assert line["SalesItemLineDetail"]["Qty"] == 480.0
        assert line["SalesItemLineDetail"]["UnitPrice"] == 3.289
        # Bearer auth header is attached.
        assert call["headers"]["Authorization"].startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_reports_error_when_http_fails(self):
        http = _ScriptedHTTPClient([_StubResponse(500, {})])
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.0,
            }
        )
        assert run.status == "error"
        assert run.record_counts["failed"] == 1


# ---------------------------------------------------------------------------
# sync_pull() — folds Invoices + Payments into ReconciliationService (Req 5.2.4)
# ---------------------------------------------------------------------------


class TestSyncPull:
    @pytest.mark.asyncio
    async def test_updates_reconciliation_from_invoice(self):
        invoice_payload = {
            "QueryResponse": {
                "Invoice": [
                    {
                        "Id": "INV-42",
                        "TotalAmt": 100.0,
                        "Balance": 0.0,
                        "Line": [
                            {
                                "DetailType": "SalesItemLineDetail",
                                "SalesItemLineDetail": {"Qty": 480.0},
                            }
                        ],
                    }
                ]
            }
        }
        empty_payments = {"QueryResponse": {}}
        http = _ScriptedHTTPClient(
            [
                _StubResponse(200, invoice_payload),
                _StubResponse(200, empty_payments),
            ]
        )
        recon = AsyncMock()
        es = _RecordingES(
            seed={
                "rec-1": {
                    "reconciliation_id": "rec-1",
                    "tenant_id": "tenant-a",
                    "invoice_id": "INV-42",
                    "invoiced_gallons": 480.0,
                }
            }
        )
        connector = _seeded_connector(http=http, recon=recon, es=es)
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)
        assert run.status == "success"
        assert run.record_counts["invoices_processed"] == 1
        assert run.record_counts["reconciliations_updated"] == 1
        recon.update_invoice_fields.assert_awaited_once()
        kwargs = recon.update_invoice_fields.await_args.kwargs
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["reconciliation_id"] == "rec-1"
        assert kwargs["invoice_id"] == "INV-42"
        assert kwargs["invoiced_gallons"] == 480.0
        assert kwargs["payment_status"] == "paid"

    @pytest.mark.asyncio
    async def test_skips_invoice_without_matching_reconciliation(self):
        invoice_payload = {
            "QueryResponse": {
                "Invoice": [
                    {
                        "Id": "INV-99",
                        "TotalAmt": 100.0,
                        "Balance": 100.0,
                        "Line": [
                            {
                                "DetailType": "SalesItemLineDetail",
                                "SalesItemLineDetail": {"Qty": 10.0},
                            }
                        ],
                    }
                ]
            }
        }
        http = _ScriptedHTTPClient(
            [
                _StubResponse(200, invoice_payload),
                _StubResponse(200, {"QueryResponse": {}}),
            ]
        )
        recon = AsyncMock()
        es = _RecordingES()  # empty — no reconciliation seeded
        connector = _seeded_connector(http=http, recon=recon, es=es)
        run = await connector.sync_pull(datetime(2025, 1, 1, tzinfo=timezone.utc))
        # No reconciliation updated → partial.
        assert run.record_counts["reconciliations_updated"] == 0
        assert run.record_counts["skipped_no_match"] == 1
        recon.update_invoice_fields.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_payment_linked_to_invoice(self):
        empty_invoices = {"QueryResponse": {}}
        payment_payload = {
            "QueryResponse": {
                "Payment": [
                    {
                        "Id": "PAY-1",
                        "Line": [
                            {
                                "LinkedTxn": [
                                    {"TxnId": "INV-42", "TxnType": "Invoice"}
                                ]
                            }
                        ],
                    }
                ]
            }
        }
        http = _ScriptedHTTPClient(
            [
                _StubResponse(200, empty_invoices),
                _StubResponse(200, payment_payload),
            ]
        )
        recon = AsyncMock()
        es = _RecordingES(
            seed={
                "rec-1": {
                    "reconciliation_id": "rec-1",
                    "tenant_id": "tenant-a",
                    "invoice_id": "INV-42",
                    "invoiced_gallons": 480.0,
                }
            }
        )
        connector = _seeded_connector(http=http, recon=recon, es=es)
        run = await connector.sync_pull(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert run.record_counts["payments_processed"] == 1
        assert run.record_counts["reconciliations_updated"] == 1
        recon.update_invoice_fields.assert_awaited_once()
        kwargs = recon.update_invoice_fields.await_args.kwargs
        assert kwargs["payment_status"] == "paid"
        assert kwargs["invoiced_gallons"] == 480.0


# ---------------------------------------------------------------------------
# 401 refresh flow (Req 5.2.5)
# ---------------------------------------------------------------------------


class TestRefreshOn401:
    @pytest.mark.asyncio
    async def test_single_refresh_then_retry(self, monkeypatch):
        """A 401 triggers exactly one refresh; the retry succeeds."""

        # Script: first QBO call returns 401; after refresh, retry returns 200.
        http = _ScriptedHTTPClient(
            [
                _StubResponse(401, {}),
                _StubResponse(200, {"Invoice": {"Id": "INV-NEW"}}),
            ]
        )
        vault = _FakeVault(
            seed={
                "cred:tenant-a:qbo_oauth:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {
                        "client_id": "cid",
                        "client_secret": "csecret",
                        "refresh_token": "rt-old",
                        "realm_id": "realm-1",
                        "access_token": "at-old",
                    },
                }
            }
        )
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
            vault=vault,
        )

        # Force the :func:`_refresh_access_token` helper to take the HTTP
        # fallback path so we don't actually import ``intuitlib``.
        async def _stub_refresh_via_http(
            self, *, client_id, client_secret, refresh_token
        ):
            return {
                "access_token": "at-new",
                "refresh_token": "rt-new",
                "expires_in": 3600,
            }

        monkeypatch.setattr(
            QuickBooksOnlineConnector,
            "_refresh_via_http",
            _stub_refresh_via_http,
            raising=True,
        )
        # Force ImportError on intuitlib so we always take the HTTP fallback.
        import sys

        sys.modules["intuitlib.client"] = None  # type: ignore[assignment]

        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.0,
            }
        )
        assert run.status == "success"
        assert run.record_counts["invoices_pushed"] == 1
        # Exactly two QBO HTTP calls: 401, then retry.
        qbo_calls = [c for c in http.calls if "quickbooks.api.intuit.com" in c["url"]]
        assert len(qbo_calls) == 2
        # Rotated refresh_token was persisted back to the vault.
        rotated_put = [
            p for p in vault.put_calls if p["plaintext"].get("refresh_token") == "rt-new"
        ]
        assert rotated_put, "rotated refresh_token was not persisted to vault"

    @pytest.mark.asyncio
    async def test_second_401_yields_credentials_expired(self, monkeypatch):
        http = _ScriptedHTTPClient(
            [
                _StubResponse(401, {}),
                _StubResponse(401, {}),
            ]
        )

        async def _stub_refresh_via_http(
            self, *, client_id, client_secret, refresh_token
        ):
            return {"access_token": "at-new", "refresh_token": "rt-new"}

        monkeypatch.setattr(
            QuickBooksOnlineConnector,
            "_refresh_via_http",
            _stub_refresh_via_http,
            raising=True,
        )
        import sys

        sys.modules["intuitlib.client"] = None  # type: ignore[assignment]

        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.0,
            }
        )
        assert run.status == "error"
        assert run.error_details is not None
        assert CREDENTIALS_EXPIRED_REASON in run.error_details

    @pytest.mark.asyncio
    async def test_refresh_failure_yields_credentials_expired(self, monkeypatch):
        http = _ScriptedHTTPClient([_StubResponse(401, {})])

        async def _stub_refresh_via_http(
            self, *, client_id, client_secret, refresh_token
        ):
            raise RuntimeError("refresh endpoint HTTP 400")

        monkeypatch.setattr(
            QuickBooksOnlineConnector,
            "_refresh_via_http",
            _stub_refresh_via_http,
            raising=True,
        )
        import sys

        sys.modules["intuitlib.client"] = None  # type: ignore[assignment]

        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.0,
            }
        )
        assert run.status == "error"
        assert CREDENTIALS_EXPIRED_REASON in (run.error_details or "")


# ---------------------------------------------------------------------------
# Rate limiting (Req 5.2.6)
# ---------------------------------------------------------------------------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_exceeding_ceiling_raises(self):
        redis = _FakeRedis()
        # Pre-populate the current minute's counter to the ceiling.
        bucket = _minute_bucket()
        key = _rate_limit_key("tenant-a", bucket)
        redis.counters[key] = DEFAULT_RATE_LIMIT_PER_MINUTE

        http = _ScriptedHTTPClient(
            [_StubResponse(200, {"Invoice": {"Id": "never"}})]
        )
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
            redis=redis,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.0,
            }
        )
        assert run.status == "error"
        assert "rate_limit_exceeded" in (run.error_details or "")
        # The HTTP call should never have fired.
        assert http.calls == []

    @pytest.mark.asyncio
    async def test_rate_limit_counter_key_format(self):
        redis = _FakeRedis()
        http = _ScriptedHTTPClient(
            [_StubResponse(200, {"Invoice": {"Id": "OK"}})]
        )
        connector = _seeded_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            http=http,
            redis=redis,
        )
        await connector.sync_push(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.0,
            }
        )
        # Exactly one counter key of the form qbo_ratelimit:{tenant}:{bucket}.
        keys = list(redis.counters.keys())
        assert len(keys) == 1
        assert keys[0].startswith("qbo_ratelimit:tenant-a:")
        assert redis.counters[keys[0]] == 1
        # TTL was refreshed.
        assert redis.ttls.get(keys[0]) is not None


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_deletes_vault_envelope(self):
        http = _ScriptedHTTPClient([_StubResponse(200, {})])
        vault = _FakeVault(
            seed={
                "cred:tenant-a:qbo_oauth:seed": {
                    "tenant_id": "tenant-a",
                    "plaintext": {
                        "client_id": "cid",
                        "client_secret": "csecret",
                        "refresh_token": "rt-old",
                        "realm_id": "realm-1",
                        "access_token": "at-old",
                    },
                }
            }
        )
        connector = _seeded_connector(vault=vault, http=http)
        await connector.disconnect()
        assert "cred:tenant-a:qbo_oauth:seed" in vault.delete_calls

    @pytest.mark.asyncio
    async def test_disconnect_without_ref_is_noop(self):
        vault = _FakeVault()
        connector = QuickBooksOnlineConnector(
            tenant_id="tenant-a",
            instance_id="inst-1",
            credentials_vault=vault,
        )
        # Should not raise, should not touch the vault.
        await connector.disconnect()
        assert vault.delete_calls == []


# ---------------------------------------------------------------------------
# Pure helpers (body shaping, extraction, catalog entry)
# ---------------------------------------------------------------------------


class TestBodyShaping:
    def test_build_invoice_body_computes_amount(self):
        body = _build_invoice_body(
            {
                "pod_id": "pod-1",
                "customer_id": "cust-1",
                "product_code": "DIESEL_2",
                "delivery_date": "2025-01-14",
                "delivered_gallons": 480.0,
                "unit_price_usd": 3.289,
                "memo": "Route 12 delivery",
                "invoice_doc_number": "R12-POD-1",
            }
        )
        assert body["CustomerRef"] == {"value": "cust-1"}
        line = body["Line"][0]
        # 480 * 3.289 = 1578.72
        assert line["Amount"] == pytest.approx(1578.72, abs=0.01)
        assert line["SalesItemLineDetail"]["Qty"] == 480.0
        assert body["TxnDate"] == "2025-01-14"
        assert body["CustomerMemo"] == {"value": "Route 12 delivery"}
        assert body["DocNumber"] == "R12-POD-1"

    def test_build_invoice_body_rejects_missing_required_fields(self):
        with pytest.raises(ValueError):
            _build_invoice_body({"customer_id": "cust-1", "delivered_gallons": 1.0})

    def test_build_invoice_body_rejects_nonpositive_gallons(self):
        with pytest.raises(ValueError):
            _build_invoice_body(
                {
                    "customer_id": "cust-1",
                    "delivered_gallons": 0,
                    "unit_price_usd": 3.0,
                }
            )

    def test_extract_invoice_gallons_reads_sales_item_detail(self):
        qty = _extract_invoice_gallons(
            {
                "Line": [
                    {
                        "DetailType": "SalesItemLineDetail",
                        "SalesItemLineDetail": {"Qty": 125.5},
                    }
                ]
            }
        )
        assert qty == 125.5

    def test_extract_invoice_gallons_returns_none_when_no_qty(self):
        qty = _extract_invoice_gallons(
            {"Line": [{"DetailType": "SubTotalLineDetail"}]}
        )
        assert qty is None

    def test_extract_payment_status_paid_when_balance_zero(self):
        assert (
            _extract_payment_status({"TotalAmt": 100.0, "Balance": 0.0}) == "paid"
        )

    def test_extract_payment_status_partial(self):
        assert (
            _extract_payment_status({"TotalAmt": 100.0, "Balance": 25.0})
            == "partial"
        )

    def test_extract_payment_status_unpaid(self):
        assert (
            _extract_payment_status({"TotalAmt": 100.0, "Balance": 100.0})
            == "unpaid"
        )

    def test_extract_payment_status_none_when_total_zero(self):
        assert _extract_payment_status({"TotalAmt": 0.0, "Balance": 0.0}) is None


class TestCatalogEntry:
    def setup_method(self) -> None:
        clear_registry()

    def teardown_method(self) -> None:
        clear_registry()

    def test_build_catalog_entry_matches_required_fields(self):
        entry = build_catalog_entry()
        assert entry.provider_name == "quickbooks_online"
        assert entry.category == "accounting"
        assert entry.auth_mode == "oauth2"
        assert "client_id" in entry.required_credential_fields
        assert "client_secret" in entry.required_credential_fields
        assert "refresh_token" in entry.required_credential_fields
        assert "realm_id" in entry.required_credential_fields
        # Marketplace-level visibility flag defaults to
        # overlay.integration.{provider_name} (Req 5.6.6); the
        # connector-specific overlay.qbo_invoice_push flag is a
        # separate, behaviour-level gate enforced inside sync_push.
        assert entry.feature_flag_key is None
        assert entry.effective_feature_flag_key() == (
            "overlay.integration.quickbooks_online"
        )
        assert QBO_INVOICE_PUSH_FLAG_KEY == "overlay.qbo_invoice_push"

    def test_register_catalog_entry_adds_to_registry(self):
        assert get_provider("quickbooks_online") is None
        register_catalog_entry()
        assert get_provider("quickbooks_online") is not None
