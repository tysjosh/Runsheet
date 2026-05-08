"""
Unit tests for :mod:`integrations.stripe_connector`.

Covers Capability 5 / Task 9.8 / Requirements 5.5.1–5.5.5, 5.5.7 of the
fuel-ops hardening spec:

* ``connect`` persists the envelope in the Tenant_Credentials_Vault
  and returns a redacted :class:`ConnectionResult` whose ``metadata``
  exposes ONLY the ``publishable_key`` (Req 5.5.1, 5.5.2, 5.1.8).
* ``get_publishable_key`` returns the publishable key only — never
  the secret key or webhook secret (Req 5.5.2, 5.1.8).
* ``sync_push`` short-circuits when ``overlay.stripe_autocharge`` is
  disabled (Req 5.5.3).
* ``sync_push`` under the ceiling calls
  ``stripe.PaymentIntent.create`` with the configured metadata
  (Req 5.5.3).
* ``sync_push`` at or above the ceiling routes through the
  Confirmation_Protocol with risk HIGH rather than charging
  (Req 5.5.5).
* ``sync_pull`` folds PaymentIntents into
  :meth:`ReconciliationService.update_payment_status` when
  ``metadata.reconciliation_id`` is present (Req 5.5.4).
* ``verify_webhook_signature`` delegates to
  ``stripe.Webhook.construct_event`` and raises
  :class:`StripeSignatureVerificationError` on any failure (Req 5.5.4).
* ``handle_webhook_event`` dispatches on event type and updates
  reconciliation for ``payment_intent.succeeded``,
  ``payment_intent.payment_failed``, and
  ``payment_intent.processing`` (Req 5.5.4).
* ``disconnect`` deletes the vault envelope idempotently.
* Catalog registration via :func:`build_catalog_entry` /
  :func:`register_catalog_entry` (Req 5.5.7).

The Stripe SDK, vault, Redis, feature flag, confirmation protocol, and
reconciliation service are replaced with in-memory fakes so the tests
are hermetic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from integrations.connector_base import ConnectionResult, SyncRun
from integrations.provider_catalog import clear_registry, get_provider
from integrations.stripe_connector import (
    AUTOCHARGE_CEILING_REDIS_KEY_TEMPLATE,
    DEFAULT_AUTOCHARGE_CEILING_USD,
    HIGH_RISK_TOOL_NAME,
    STRIPE_AUTOCHARGE_FLAG_KEY,
    STRIPE_AGENT_ID,
    StripeConnector,
    StripeSignatureVerificationError,
    VAULT_CREDENTIAL_KEY,
    _build_payment_intent_body,
    _redact_payment_intent,
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
                "provider_name": provider_name,
                "plaintext": dict(plaintext),
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


class _FakeFeatureFlags:
    def __init__(self, state: str = "disabled") -> None:
        self._state = state
        self.calls: List[Dict[str, str]] = []

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        self.calls.append({"flag_key": flag_key, "tenant_id": tenant_id})
        return self._state


class _FakeRedis:
    def __init__(self, values: Optional[Dict[str, str]] = None) -> None:
        self._values = dict(values or {})

    async def get(self, key: str) -> Optional[str]:
        return self._values.get(key)


class _FakeConfirmationProtocol:
    def __init__(self) -> None:
        self.process_calls: List[Any] = []

    async def process_mutation(self, request: Any) -> Any:
        self.process_calls.append(request)

        class _Result:
            executed = False
            approval_id = "appr-1"
            risk_level = "high"
            confirmation_method = "approval_queue"

        return _Result()


class _FakePaymentIntentAPI:
    """In-memory Stripe PaymentIntent API stub."""

    def __init__(
        self,
        list_items: Optional[List[Dict[str, Any]]] = None,
        create_return: Optional[Dict[str, Any]] = None,
        create_raises: Optional[Exception] = None,
    ) -> None:
        self._list_items = list_items or []
        self._create_return = create_return or {"id": "pi_test_1"}
        self._create_raises = create_raises
        self.create_calls: List[Dict[str, Any]] = []
        self.list_calls: List[Dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Dict[str, Any]:
        self.list_calls.append(dict(kwargs))
        return {"data": list(self._list_items)}

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        self.create_calls.append(dict(kwargs))
        if self._create_raises is not None:
            raise self._create_raises
        return dict(self._create_return)


class _FakeWebhookAPI:
    def __init__(
        self,
        *,
        construct_return: Optional[Dict[str, Any]] = None,
        construct_raises: Optional[Exception] = None,
    ) -> None:
        self._return = construct_return or {"type": "payment_intent.succeeded"}
        self._raises = construct_raises
        self.calls: List[Dict[str, Any]] = []

    def construct_event(
        self, payload: Any, sig_header: Any, secret: Any
    ) -> Dict[str, Any]:
        self.calls.append(
            {"payload": payload, "sig_header": sig_header, "secret": secret}
        )
        if self._raises is not None:
            raise self._raises
        return dict(self._return)


class _FakeStripeSDK:
    """Stand-in for ``import stripe``."""

    def __init__(
        self,
        *,
        payment_intent_api: Optional[_FakePaymentIntentAPI] = None,
        webhook_api: Optional[_FakeWebhookAPI] = None,
    ) -> None:
        self.api_key: Optional[str] = None
        self.PaymentIntent = payment_intent_api or _FakePaymentIntentAPI()
        self.Webhook = webhook_api or _FakeWebhookAPI()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_TENANT = "tenant-a"
_INSTANCE = "inst-stripe-1"
_CRED_REF = f"cred:{_TENANT}:{VAULT_CREDENTIAL_KEY}:seed"


def _seeded_vault() -> _FakeVault:
    return _FakeVault(
        seed={
            _CRED_REF: {
                "tenant_id": _TENANT,
                "plaintext": {
                    "secret_key": "fake_sk__123",
                    "publishable_key": "fake_pk__123",
                    "webhook_secret": "fake_whsec___123",
                },
            }
        }
    )


def _build_connector(
    *,
    vault: Optional[_FakeVault] = None,
    feature_flags: Optional[_FakeFeatureFlags] = None,
    redis: Optional[_FakeRedis] = None,
    confirmation: Optional[Any] = None,
    recon: Any = None,
    stripe_module: Optional[Any] = None,
    credentials_ref: Optional[str] = _CRED_REF,
) -> StripeConnector:
    return StripeConnector(
        tenant_id=_TENANT,
        instance_id=_INSTANCE,
        credentials_vault=vault or _seeded_vault(),
        credentials_ref=credentials_ref,
        reconciliation_service=recon,
        feature_flag_service=feature_flags,
        confirmation_protocol=confirmation,
        redis_client=redis,
        stripe_module=stripe_module,
    )


# ---------------------------------------------------------------------------
# connect() — Req 5.5.1, 5.5.2, 5.1.8
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_stores_envelope_in_vault_and_returns_publishable_only(self):
        vault = _FakeVault()
        connector = StripeConnector(
            tenant_id=_TENANT,
            instance_id=_INSTANCE,
            credentials_vault=vault,
        )
        result = await connector.connect(
            {
                "secret_key": "fake_sk__xxx",
                "publishable_key": "fake_pk__xxx",
                "webhook_secret": "fake_whsec_xxx",
            }
        )
        assert isinstance(result, ConnectionResult)
        assert result.status == "connected"
        assert result.credentials_ref is not None
        # Metadata must expose ONLY the publishable_key.
        assert result.metadata == {"publishable_key": "fake_pk__xxx"}
        assert "secret_key" not in result.metadata
        assert "webhook_secret" not in result.metadata
        # Envelope was written to the vault with all three fields.
        assert len(vault.put_calls) == 1
        put = vault.put_calls[0]
        assert put["tenant_id"] == _TENANT
        assert put["key"] == VAULT_CREDENTIAL_KEY
        assert put["plaintext"]["secret_key"] == "fake_sk__xxx"
        assert put["plaintext"]["webhook_secret"] == "fake_whsec_xxx"

    @pytest.mark.asyncio
    async def test_returns_error_when_required_fields_missing(self):
        vault = _FakeVault()
        connector = StripeConnector(
            tenant_id=_TENANT,
            instance_id=_INSTANCE,
            credentials_vault=vault,
        )
        result = await connector.connect(
            {"secret_key": "fake_sk__xx", "publishable_key": "fake_pk__xx"}
        )
        assert result.status == "error"
        assert "webhook_secret" in (result.message or "")
        assert vault.put_calls == []


# ---------------------------------------------------------------------------
# get_publishable_key() — Req 5.5.2, 5.1.8
# ---------------------------------------------------------------------------


class TestGetPublishableKey:
    @pytest.mark.asyncio
    async def test_returns_publishable_key_only(self):
        connector = _build_connector()
        key = await connector.get_publishable_key()
        assert key == "fake_pk__123"

    @pytest.mark.asyncio
    async def test_raises_when_no_credentials_ref(self):
        vault = _FakeVault()
        connector = StripeConnector(
            tenant_id=_TENANT,
            instance_id=_INSTANCE,
            credentials_vault=vault,
        )
        with pytest.raises(RuntimeError):
            await connector.get_publishable_key()


# ---------------------------------------------------------------------------
# sync_push() — Req 5.5.3, 5.5.5
# ---------------------------------------------------------------------------


class TestSyncPush:
    @pytest.mark.asyncio
    async def test_skips_when_feature_flag_disabled(self):
        stripe_sdk = _FakeStripeSDK()
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="disabled"),
            stripe_module=stripe_sdk,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "customer_id": "cus_1",
                "amount_usd": 100.0,
            }
        )
        assert isinstance(run, SyncRun)
        assert run.status == "success"
        assert run.record_counts.get("skipped_disabled") == 1
        assert run.record_counts.get("payment_intents_created") == 0
        assert stripe_sdk.PaymentIntent.create_calls == []

    @pytest.mark.asyncio
    async def test_skips_when_feature_flag_shadow(self):
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="shadow"),
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "amount_usd": 50.0,
            }
        )
        assert run.record_counts.get("skipped_disabled") == 1

    @pytest.mark.asyncio
    async def test_creates_payment_intent_below_ceiling(self):
        pi_api = _FakePaymentIntentAPI(create_return={"id": "pi_abc"})
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            stripe_module=stripe_sdk,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "customer_id": "cus_1",
                "amount_usd": 123.45,
                "description": "route 12 delivery",
            }
        )
        assert run.status == "success"
        assert run.record_counts.get("payment_intents_created") == 1
        assert run.record_counts.get("escalated_to_confirmation") == 0
        assert len(pi_api.create_calls) == 1
        call = pi_api.create_calls[0]
        # Stripe expects cents — 123.45 → 12345.
        assert call["amount"] == 12345
        assert call["currency"] == "usd"
        assert call["customer"] == "cus_1"
        assert call["description"] == "route 12 delivery"
        # reconciliation_id propagated via metadata.
        assert call["metadata"]["reconciliation_id"] == "rec-1"
        assert call["metadata"]["pod_id"] == "pod-1"
        # The api_key was set from the vault envelope before calling.
        assert stripe_sdk.api_key == "fake_sk__123"

    @pytest.mark.asyncio
    async def test_escalates_above_default_ceiling(self):
        confirmation = _FakeConfirmationProtocol()
        pi_api = _FakePaymentIntentAPI()
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            confirmation=confirmation,
            stripe_module=stripe_sdk,
        )
        # 6000 > 5000 default ceiling.
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "customer_id": "cus_1",
                "amount_usd": 6000.0,
            }
        )
        assert run.status == "success"
        assert run.record_counts.get("escalated_to_confirmation") == 1
        assert run.record_counts.get("payment_intents_created") == 0
        # No Stripe call was issued — the ceiling guard short-circuited.
        assert pi_api.create_calls == []
        # Confirmation protocol saw exactly one HIGH-risk mutation.
        assert len(confirmation.process_calls) == 1
        request = confirmation.process_calls[0]
        assert request.tool_name == HIGH_RISK_TOOL_NAME
        assert request.tenant_id == _TENANT
        assert request.agent_id == STRIPE_AGENT_ID
        assert request.parameters["amount_usd"] == 6000.0
        assert request.parameters["ceiling_usd"] == DEFAULT_AUTOCHARGE_CEILING_USD
        assert request.parameters["reconciliation_id"] == "rec-1"

    @pytest.mark.asyncio
    async def test_escalates_at_exact_ceiling_boundary(self):
        """Requirement uses ``>=``: a payment at exactly the ceiling escalates."""

        confirmation = _FakeConfirmationProtocol()
        pi_api = _FakePaymentIntentAPI()
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            confirmation=confirmation,
            stripe_module=stripe_sdk,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "amount_usd": DEFAULT_AUTOCHARGE_CEILING_USD,
            }
        )
        assert run.record_counts.get("escalated_to_confirmation") == 1
        assert pi_api.create_calls == []

    @pytest.mark.asyncio
    async def test_honours_tenant_override_ceiling(self):
        confirmation = _FakeConfirmationProtocol()
        pi_api = _FakePaymentIntentAPI(create_return={"id": "pi_low"})
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        redis = _FakeRedis(
            {
                AUTOCHARGE_CEILING_REDIS_KEY_TEMPLATE.format(tenant_id=_TENANT): "10000"
            }
        )
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            confirmation=confirmation,
            redis=redis,
            stripe_module=stripe_sdk,
        )
        # 6000 is over the default but under the tenant's 10000 override.
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "amount_usd": 6000.0,
            }
        )
        assert run.record_counts.get("payment_intents_created") == 1
        assert confirmation.process_calls == []

    @pytest.mark.asyncio
    async def test_reports_error_when_stripe_raises(self):
        pi_api = _FakePaymentIntentAPI(
            create_raises=RuntimeError("stripe API down")
        )
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(
            feature_flags=_FakeFeatureFlags(state="active_auto"),
            stripe_module=stripe_sdk,
        )
        run = await connector.sync_push(
            {
                "pod_id": "pod-1",
                "reconciliation_id": "rec-1",
                "amount_usd": 100.0,
            }
        )
        assert run.status == "error"
        assert run.record_counts.get("failed") == 1
        assert "stripe API down" in (run.error_details or "")


# ---------------------------------------------------------------------------
# sync_pull() — Req 5.5.4
# ---------------------------------------------------------------------------


class TestSyncPull:
    @pytest.mark.asyncio
    async def test_updates_reconciliation_from_payment_intent(self):
        pi_api = _FakePaymentIntentAPI(
            list_items=[
                {
                    "id": "pi_1",
                    "status": "succeeded",
                    "metadata": {"reconciliation_id": "rec-1"},
                }
            ]
        )
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        recon = AsyncMock()
        connector = _build_connector(stripe_module=stripe_sdk, recon=recon)
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        run = await connector.sync_pull(since)
        assert run.status == "success"
        assert run.record_counts["payment_intents_processed"] == 1
        assert run.record_counts["reconciliations_updated"] == 1
        recon.update_payment_status.assert_awaited_once()
        kwargs = recon.update_payment_status.await_args.kwargs
        assert kwargs["tenant_id"] == _TENANT
        assert kwargs["reconciliation_id"] == "rec-1"
        assert kwargs["payment_status"] == "paid"
        assert kwargs["payment_intent_id"] == "pi_1"
        # api_key is set from the vault envelope on the SDK.
        assert stripe_sdk.api_key == "fake_sk__123"
        # list() was called with ``created={"gte": <since_ts>}``.
        list_call = pi_api.list_calls[0]
        assert list_call["created"] == {"gte": int(since.timestamp())}

    @pytest.mark.asyncio
    async def test_skips_payment_intents_without_reconciliation_metadata(self):
        pi_api = _FakePaymentIntentAPI(
            list_items=[
                {"id": "pi_1", "status": "succeeded", "metadata": {}},
                {"id": "pi_2", "status": "succeeded"},
            ]
        )
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        recon = AsyncMock()
        connector = _build_connector(stripe_module=stripe_sdk, recon=recon)
        run = await connector.sync_pull(
            datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        assert run.record_counts["skipped_no_metadata"] == 2
        assert run.record_counts["reconciliations_updated"] == 0
        recon.update_payment_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# verify_webhook_signature() — Req 5.5.4
# ---------------------------------------------------------------------------


class TestVerifyWebhookSignature:
    @pytest.mark.asyncio
    async def test_valid_signature_returns_event(self):
        webhook_api = _FakeWebhookAPI(
            construct_return={
                "type": "payment_intent.succeeded",
                "data": {"object": {"id": "pi_test", "metadata": {}}},
            }
        )
        stripe_sdk = _FakeStripeSDK(webhook_api=webhook_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        event = await connector.verify_webhook_signature(
            b'{"id":"evt_test"}', "t=123,v1=hex..."
        )
        assert event["type"] == "payment_intent.succeeded"
        assert len(webhook_api.calls) == 1
        call = webhook_api.calls[0]
        assert call["payload"] == b'{"id":"evt_test"}'
        assert call["sig_header"] == "t=123,v1=hex..."
        # The webhook_secret from the vault envelope was passed to the SDK.
        assert call["secret"] == "fake_whsec___123"

    @pytest.mark.asyncio
    async def test_invalid_signature_raises_structured_error(self):
        webhook_api = _FakeWebhookAPI(
            construct_raises=ValueError("bad signature")
        )
        stripe_sdk = _FakeStripeSDK(webhook_api=webhook_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        with pytest.raises(StripeSignatureVerificationError):
            await connector.verify_webhook_signature(
                b'{"id":"evt_test"}', "bad-sig"
            )


# ---------------------------------------------------------------------------
# handle_webhook_event() — Req 5.5.4
# ---------------------------------------------------------------------------


class TestHandleWebhookEvent:
    @pytest.mark.asyncio
    async def test_succeeded_event_sets_payment_paid(self):
        recon = AsyncMock()
        connector = _build_connector(recon=recon)
        event = {
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_1",
                    "metadata": {"reconciliation_id": "rec-1"},
                }
            },
        }
        summary = await connector.handle_webhook_event(event)
        assert summary["handled"] is True
        assert summary["payment_status"] == "paid"
        recon.update_payment_status.assert_awaited_once()
        kwargs = recon.update_payment_status.await_args.kwargs
        assert kwargs["payment_status"] == "paid"
        assert kwargs["reconciliation_id"] == "rec-1"
        assert kwargs["payment_intent_id"] == "pi_1"

    @pytest.mark.asyncio
    async def test_failed_event_sets_payment_failed(self):
        recon = AsyncMock()
        connector = _build_connector(recon=recon)
        event = {
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_2",
                    "metadata": {"reconciliation_id": "rec-2"},
                }
            },
        }
        summary = await connector.handle_webhook_event(event)
        assert summary["handled"] is True
        assert summary["payment_status"] == "failed"
        kwargs = recon.update_payment_status.await_args.kwargs
        assert kwargs["payment_status"] == "failed"

    @pytest.mark.asyncio
    async def test_processing_event_sets_payment_processing(self):
        recon = AsyncMock()
        connector = _build_connector(recon=recon)
        event = {
            "type": "payment_intent.processing",
            "data": {
                "object": {
                    "id": "pi_3",
                    "metadata": {"reconciliation_id": "rec-3"},
                }
            },
        }
        summary = await connector.handle_webhook_event(event)
        assert summary["handled"] is True
        assert summary["payment_status"] == "processing"

    @pytest.mark.asyncio
    async def test_unknown_event_type_is_ignored(self):
        recon = AsyncMock()
        connector = _build_connector(recon=recon)
        summary = await connector.handle_webhook_event(
            {"type": "customer.created", "data": {"object": {}}}
        )
        assert summary["handled"] is False
        assert summary["reason"] == "ignored_event_type"
        recon.update_payment_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_event_without_reconciliation_id_is_skipped(self):
        recon = AsyncMock()
        connector = _build_connector(recon=recon)
        summary = await connector.handle_webhook_event(
            {
                "type": "payment_intent.succeeded",
                "data": {"object": {"id": "pi_4", "metadata": {}}},
            }
        )
        assert summary["handled"] is False
        assert summary["reason"] == "missing_reconciliation_id"
        recon.update_payment_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_payments() — Req 5.5.6, 5.1.8
# ---------------------------------------------------------------------------


class _FakeListingPaymentIntentAPI(_FakePaymentIntentAPI):
    """PaymentIntent.list stub that returns a realistic raw response."""

    def __init__(
        self,
        *,
        raw_items: List[Dict[str, Any]],
        has_more: bool = False,
    ) -> None:
        super().__init__()
        self._raw_items = raw_items
        self._has_more = has_more

    def list(self, **kwargs: Any) -> Dict[str, Any]:
        self.list_calls.append(dict(kwargs))
        return {
            "data": [dict(item) for item in self._raw_items],
            "has_more": self._has_more,
        }


def _unsafe_payment_intent(intent_id: str = "pi_raw_1") -> Dict[str, Any]:
    """A PaymentIntent carrying fields that MUST NOT leak through redaction."""

    return {
        "id": intent_id,
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 12345,
        "currency": "usd",
        "created": 1_700_000_000,
        "customer": "cus_1",
        "description": "route 12 delivery",
        "metadata": {
            "reconciliation_id": "rec-1",
            "internal_note": "do-not-leak",
        },
        # --- sensitive / PII-adjacent fields the redactor MUST strip. ---
        "client_secret": "pi_secret_must_not_leak",
        "receipt_email": "operator@example.com",
        "payment_method": "pm_card_visa",
        "payment_method_data": {
            "type": "card",
            "card": {
                "number": "4242424242424242",
                "exp_month": 12,
                "exp_year": 2099,
                "cvc": "123",
            },
        },
        "charges": {
            "data": [
                {
                    "id": "ch_1",
                    "billing_details": {
                        "email": "customer@example.com",
                        "name": "Jane Doe",
                        "address": {"postal_code": "94103"},
                    },
                    "payment_method_details": {
                        "card": {
                            "last4": "4242",
                            "brand": "visa",
                            "fingerprint": "abcd1234",
                        },
                    },
                }
            ],
        },
        "last_payment_error": {"message": "do-not-leak"},
    }


class TestListPayments:
    @pytest.mark.asyncio
    async def test_returns_redacted_items_and_pagination_cursor(self):
        raw = _unsafe_payment_intent("pi_raw_1")
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[raw], has_more=True)
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        page = await connector.list_payments(limit=5)

        assert page["has_more"] is True
        assert page["next_starting_after"] == "pi_raw_1"
        assert stripe_sdk.api_key == "fake_sk__123"
        assert pi_api.list_calls[0]["limit"] == 5

        assert len(page["items"]) == 1
        item = page["items"][0]
        # Safe fields carried through.
        assert item["id"] == "pi_raw_1"
        assert item["status"] == "succeeded"
        assert item["amount"] == 12345
        assert item["currency"] == "usd"
        assert item["created"] == 1_700_000_000
        assert item["customer"] == "cus_1"
        assert item["description"] == "route 12 delivery"
        assert item["metadata"] == {"reconciliation_id": "rec-1"}
        # Sensitive fields dropped.
        for banned in (
            "client_secret",
            "receipt_email",
            "payment_method",
            "payment_method_data",
            "charges",
            "last_payment_error",
        ):
            assert banned not in item, (
                f"PaymentIntent redactor leaked {banned!r}"
            )
        # Unwanted metadata keys dropped.
        assert "internal_note" not in item["metadata"]

    @pytest.mark.asyncio
    async def test_clamps_limit_to_100(self):
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[])
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        page = await connector.list_payments(limit=1000)
        assert pi_api.list_calls[0]["limit"] == 100
        assert page["items"] == []
        assert page["has_more"] is False
        assert page["next_starting_after"] is None

    @pytest.mark.asyncio
    async def test_forwards_starting_after_and_created_bounds(self):
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[])
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        gte = datetime(2025, 1, 1, tzinfo=timezone.utc)
        lte = datetime(2025, 2, 1, tzinfo=timezone.utc)
        await connector.list_payments(
            limit=7,
            starting_after="pi_prev",
            created_gte=gte,
            created_lte=lte,
        )
        call = pi_api.list_calls[0]
        assert call["limit"] == 7
        assert call["starting_after"] == "pi_prev"
        assert call["created"] == {
            "gte": int(gte.timestamp()),
            "lte": int(lte.timestamp()),
        }

    @pytest.mark.asyncio
    async def test_no_cursor_when_no_more_pages(self):
        raw = _unsafe_payment_intent("pi_only")
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[raw], has_more=False)
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        page = await connector.list_payments()
        assert page["has_more"] is False
        assert page["next_starting_after"] is None
        assert len(page["items"]) == 1


class TestRedactPaymentIntent:
    def test_strips_all_sensitive_fields(self):
        raw = _unsafe_payment_intent("pi_1")
        redacted = _redact_payment_intent(raw)
        sensitive = (
            "client_secret",
            "receipt_email",
            "payment_method",
            "payment_method_data",
            "charges",
            "last_payment_error",
            "object",
        )
        for field_name in sensitive:
            assert field_name not in redacted

    def test_keeps_only_reconciliation_id_in_metadata(self):
        raw = {
            "id": "pi_2",
            "metadata": {
                "reconciliation_id": "rec-9",
                "tax_id": "SSN-000-00-0000",
                "note": "anything",
            },
        }
        redacted = _redact_payment_intent(raw)
        assert redacted["metadata"] == {"reconciliation_id": "rec-9"}

    def test_empty_metadata_when_no_reconciliation_id(self):
        raw = {"id": "pi_3", "metadata": {"note": "x"}}
        redacted = _redact_payment_intent(raw)
        assert redacted["metadata"] == {}


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_deletes_vault_envelope(self):
        vault = _seeded_vault()
        connector = _build_connector(vault=vault)
        await connector.disconnect()
        assert _CRED_REF in vault.delete_calls

    @pytest.mark.asyncio
    async def test_disconnect_without_ref_is_noop(self):
        vault = _FakeVault()
        connector = StripeConnector(
            tenant_id=_TENANT,
            instance_id=_INSTANCE,
            credentials_vault=vault,
        )
        await connector.disconnect()
        assert vault.delete_calls == []


# ---------------------------------------------------------------------------
# Pure helpers + catalog registration — Req 5.5.7
# ---------------------------------------------------------------------------


class TestPaymentIntentBody:
    def test_converts_dollars_to_cents(self):
        body = _build_payment_intent_body(
            {
                "reconciliation_id": "rec-1",
                "customer_id": "cus_1",
                "amount_usd": 123.45,
            },
            amount_usd=123.45,
        )
        assert body["amount"] == 12345
        assert body["currency"] == "usd"
        assert body["customer"] == "cus_1"
        assert body["metadata"]["reconciliation_id"] == "rec-1"

    def test_raises_on_nonpositive_amount(self):
        with pytest.raises(ValueError):
            _build_payment_intent_body({"amount_usd": 0}, amount_usd=0)

    def test_preserves_extra_metadata(self):
        body = _build_payment_intent_body(
            {
                "reconciliation_id": "rec-1",
                "amount_usd": 10.0,
                "metadata": {"route_id": "R-42"},
            },
            amount_usd=10.0,
        )
        assert body["metadata"]["route_id"] == "R-42"
        assert body["metadata"]["reconciliation_id"] == "rec-1"


class TestCatalogEntry:
    def setup_method(self) -> None:
        clear_registry()

    def teardown_method(self) -> None:
        clear_registry()

    def test_build_catalog_entry_matches_required_fields(self):
        entry = build_catalog_entry()
        assert entry.provider_name == "stripe"
        assert entry.category == "payment"
        assert entry.auth_mode == "api_key"
        assert "secret_key" in entry.required_credential_fields
        assert "publishable_key" in entry.required_credential_fields
        assert "webhook_secret" in entry.required_credential_fields
        # Marketplace-level visibility flag defaults to
        # overlay.integration.{provider_name} (Req 5.6.6); the
        # connector-specific overlay.stripe_autocharge flag is a
        # separate, behaviour-level gate enforced inside sync_push.
        assert entry.feature_flag_key is None
        assert entry.effective_feature_flag_key() == "overlay.integration.stripe"
        assert STRIPE_AUTOCHARGE_FLAG_KEY == "overlay.stripe_autocharge"

    def test_register_catalog_entry_adds_to_registry(self):
        assert get_provider("stripe") is None
        register_catalog_entry()
        assert get_provider("stripe") is not None
