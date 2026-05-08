"""
Unit tests for :mod:`integrations.api.stripe_endpoints`.

Task 9.8 of the fuel-ops-hardening spec exposes two Stripe routes —
``GET /api/integrations/stripe/public-config`` (tenant-scoped) and
``POST /webhooks/stripe/{tenant_id}`` (unauthenticated, signed
webhooks). These tests exercise both surfaces with an injected
``connector_factory`` that returns a :class:`StripeConnector` backed
by the same in-memory fakes the connector suite uses.

Covers:

* Public-config returns only the publishable_key, never the secret
  key or webhook secret (Req 5.5.1, 5.5.2, 5.1.8).
* Public-config 404 when no Stripe integration is configured.
* Webhook happy path: signature verified, handler dispatched,
  reconciliation updated (Req 5.5.4).
* Webhook 400 on invalid signature.
* Webhook 400 on missing ``Stripe-Signature`` header.
* Webhook 404 when no integration instance exists for the tenant.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.api.stripe_endpoints import (
    configure_stripe_endpoints,
    router,
    webhook_router,
)
from integrations.stripe_connector import (
    StripeConnector,
    StripeSignatureVerificationError,
    VAULT_CREDENTIAL_KEY,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes (kept local — mirrors the ones in test_stripe_connector.py)
# ---------------------------------------------------------------------------


class _FakeVault:
    def __init__(self, seed: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._store: Dict[str, Dict[str, Any]] = dict(seed or {})
        self._seq = 0
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
        return ref

    async def get(self, tenant_id: str, ref: str) -> Dict[str, Any]:
        entry = self._store.get(ref)
        if entry is None:
            raise KeyError(ref)
        if entry["tenant_id"] != tenant_id:
            raise PermissionError("cross_tenant")
        return dict(entry["plaintext"])

    async def delete(self, tenant_id: str, ref: str) -> bool:
        self.delete_calls.append(ref)
        return self._store.pop(ref, None) is not None


class _FakePaymentIntentAPI:
    def __init__(self) -> None:
        self.create_calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        self.create_calls.append(dict(kwargs))
        return {"id": "pi_test"}

    def list(self, **kwargs: Any) -> Dict[str, Any]:
        return {"data": []}


class _FakeWebhookAPI:
    def __init__(
        self,
        *,
        construct_return: Optional[Dict[str, Any]] = None,
        construct_raises: Optional[Exception] = None,
    ) -> None:
        self._return = construct_return or {
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": "pi_test",
                    "metadata": {"reconciliation_id": "rec-1"},
                }
            },
        }
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
# Helpers
# ---------------------------------------------------------------------------


_TENANT = "tenant-A"
_CRED_REF = f"cred:{_TENANT}:{VAULT_CREDENTIAL_KEY}:seed"


def _seeded_vault() -> _FakeVault:
    return _FakeVault(
        seed={
            _CRED_REF: {
                "tenant_id": _TENANT,
                "plaintext": {
                    "secret_key": "fake_sk__secret",
                    "publishable_key": "fake_pk__public",
                    "webhook_secret": "fake_whsec__",
                },
            }
        }
    )


def _tenant_ctx_factory(tenant_id: str = _TENANT):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["admin"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _build_app(
    *,
    connector: Optional[StripeConnector] = None,
    raise_on_factory: bool = False,
) -> tuple[FastAPI, "List[str]"]:
    """Wire the routers with an injected connector factory.

    Returns the FastAPI app and the list of tenant_ids the factory
    was called with so tests can assert resolution.
    """

    calls: List[str] = []

    async def _factory(tenant_id: str) -> Optional[StripeConnector]:
        calls.append(tenant_id)
        if raise_on_factory:
            raise RuntimeError("factory raised")
        return connector

    configure_stripe_endpoints(connector_factory=_factory)

    app = FastAPI()
    app.include_router(router)
    app.include_router(webhook_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory()
    return app, calls


def _build_connector(
    *,
    stripe_module: Optional[Any] = None,
    recon: Any = None,
) -> StripeConnector:
    return StripeConnector(
        tenant_id=_TENANT,
        instance_id="inst-stripe-1",
        credentials_vault=_seeded_vault(),
        credentials_ref=_CRED_REF,
        reconciliation_service=recon,
        stripe_module=stripe_module,
    )


# ---------------------------------------------------------------------------
# GET /api/integrations/stripe/public-config
# ---------------------------------------------------------------------------


class TestPublicConfigEndpoint:
    def test_returns_publishable_key_only(self):
        connector = _build_connector()
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.get("/api/integrations/stripe/public-config")
        assert resp.status_code == 200
        body = resp.json()
        # Only the publishable key is returned — secret and webhook
        # secret are NEVER exposed (Req 5.1.8, 5.5.1).
        assert body == {"publishable_key": "fake_pk__public"}
        assert "secret_key" not in body
        assert "webhook_secret" not in body

    def test_returns_404_when_no_integration(self):
        app, calls = _build_app(connector=None)
        with TestClient(app) as client:
            resp = client.get("/api/integrations/stripe/public-config")
        assert resp.status_code == 404
        assert calls == [_TENANT]
        assert (
            resp.json()["detail"]["error_code"]
            == "stripe_integration_not_configured"
        )


# ---------------------------------------------------------------------------
# POST /webhooks/stripe/{tenant_id}
# ---------------------------------------------------------------------------


class TestWebhookEndpoint:
    def test_valid_signature_dispatches_to_handler(self):
        recon = AsyncMock()
        webhook_api = _FakeWebhookAPI(
            construct_return={
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_wh_1",
                        "metadata": {"reconciliation_id": "rec-1"},
                    }
                },
            }
        )
        stripe_sdk = _FakeStripeSDK(webhook_api=webhook_api)
        connector = _build_connector(stripe_module=stripe_sdk, recon=recon)
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.post(
                f"/webhooks/stripe/{_TENANT}",
                content=b'{"id":"evt_1"}',
                headers={"Stripe-Signature": "t=1,v1=abc"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["received"] is True
        assert body["handled"] is True
        assert body["event_type"] == "payment_intent.succeeded"
        recon.update_payment_status.assert_awaited_once()
        kwargs = recon.update_payment_status.await_args.kwargs
        assert kwargs["reconciliation_id"] == "rec-1"
        assert kwargs["payment_status"] == "paid"
        assert kwargs["payment_intent_id"] == "pi_wh_1"
        # Signature verification was called with the raw body bytes,
        # not a re-serialized JSON object.
        assert webhook_api.calls[0]["payload"] == b'{"id":"evt_1"}'

    def test_invalid_signature_returns_400(self):
        webhook_api = _FakeWebhookAPI(
            construct_raises=ValueError("invalid sig")
        )
        stripe_sdk = _FakeStripeSDK(webhook_api=webhook_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.post(
                f"/webhooks/stripe/{_TENANT}",
                content=b'{"id":"evt_2"}',
                headers={"Stripe-Signature": "bad-sig"},
            )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "invalid_signature"

    def test_missing_signature_header_returns_400(self):
        connector = _build_connector(stripe_module=_FakeStripeSDK())
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.post(
                f"/webhooks/stripe/{_TENANT}",
                content=b'{"id":"evt_3"}',
            )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "missing_stripe_signature"

    def test_webhook_returns_404_when_no_integration(self):
        app, calls = _build_app(connector=None)
        with TestClient(app) as client:
            resp = client.post(
                f"/webhooks/stripe/{_TENANT}",
                content=b'{"id":"evt_4"}',
                headers={"Stripe-Signature": "t=1,v1=abc"},
            )
        assert resp.status_code == 404
        # Factory resolved the tenant_id from the URL path.
        assert calls == [_TENANT]

    def test_webhook_does_not_require_jwt(self):
        """The webhook router has no tenant_guard dependency.

        We override ``get_tenant_context`` to raise so any accidental
        tenant-guard wiring on the webhook route would surface as
        a 500 here. The route MUST succeed (200) because Stripe's
        signature IS the authentication (Req 5.5.4).
        """

        def _raise_on_tenant_guard() -> TenantContext:  # pragma: no cover
            raise AssertionError(
                "webhook should not invoke tenant_guard — auth is via "
                "Stripe-Signature verification"
            )

        webhook_api = _FakeWebhookAPI(
            construct_return={
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_wh_5",
                        "metadata": {},  # no reconciliation_id — still 200
                    }
                },
            }
        )
        stripe_sdk = _FakeStripeSDK(webhook_api=webhook_api)
        connector = _build_connector(stripe_module=stripe_sdk)

        calls: List[str] = []

        async def _factory(tenant_id: str) -> Optional[StripeConnector]:
            calls.append(tenant_id)
            return connector

        configure_stripe_endpoints(connector_factory=_factory)
        app = FastAPI()
        app.include_router(router)
        app.include_router(webhook_router)
        # Override tenant_guard to RAISE if it's ever called.
        app.dependency_overrides[get_tenant_context] = _raise_on_tenant_guard
        with TestClient(app) as client:
            resp = client.post(
                f"/webhooks/stripe/{_TENANT}",
                content=b'{"id":"evt_6"}',
                headers={"Stripe-Signature": "t=1,v1=abc"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["received"] is True
        # No reconciliation_id metadata — handler flags it but returns 200.
        assert body["handled"] is False
        assert body["reason"] == "missing_reconciliation_id"
        assert calls == [_TENANT]


# ---------------------------------------------------------------------------
# GET /api/integrations/stripe/payments — Req 5.5.6, 5.1.8
# ---------------------------------------------------------------------------


class _FakeListingPaymentIntentAPI(_FakePaymentIntentAPI):
    """PaymentIntent.list stub that returns a realistic raw response."""

    def __init__(
        self,
        *,
        raw_items: Optional[List[Dict[str, Any]]] = None,
        has_more: bool = False,
        raises: Optional[Exception] = None,
    ) -> None:
        super().__init__()
        self._raw_items = raw_items or []
        self._has_more = has_more
        self._raises = raises
        self.list_calls: List[Dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Dict[str, Any]:
        self.list_calls.append(dict(kwargs))
        if self._raises is not None:
            raise self._raises
        return {
            "data": [dict(item) for item in self._raw_items],
            "has_more": self._has_more,
        }


def _sensitive_payment_intent(intent_id: str = "pi_1") -> Dict[str, Any]:
    """A PaymentIntent carrying fields that MUST NOT leak through the API."""

    return {
        "id": intent_id,
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 5000,
        "currency": "usd",
        "created": 1_700_000_000,
        "customer": "cus_xyz",
        "description": "delivery",
        "metadata": {"reconciliation_id": "rec-1"},
        "client_secret": "pi_secret_must_not_leak",
        "receipt_email": "op@example.com",
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
                    "billing_details": {"email": "customer@example.com"},
                    "payment_method_details": {
                        "card": {"last4": "4242", "brand": "visa"}
                    },
                }
            ]
        },
    }


class TestListPaymentsEndpoint:
    def test_happy_path_returns_redacted_items_and_cursor(self):
        raw = _sensitive_payment_intent("pi_a")
        pi_api = _FakeListingPaymentIntentAPI(
            raw_items=[raw], has_more=True
        )
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, factory_calls = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.get(
                "/api/integrations/stripe/payments?limit=5"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is True
        assert body["next_starting_after"] == "pi_a"
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["id"] == "pi_a"
        assert item["amount"] == 5000
        assert item["metadata"] == {"reconciliation_id": "rec-1"}
        # Factory was called with the tenant_id from the JWT context.
        assert factory_calls == [_TENANT]
        # Stripe SDK was invoked with the forwarded limit.
        assert pi_api.list_calls[0]["limit"] == 5

    def test_tenant_scoping_forwards_jwt_tenant_id(self):
        """The connector factory MUST receive tenant.tenant_id verbatim.

        A different tenant on the JWT must resolve a different
        connector — we confirm the factory is handed the JWT value
        rather than a URL path parameter.
        """

        raw = _sensitive_payment_intent("pi_b")
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[raw])
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, factory_calls = _build_app(connector=connector)
        # Override the JWT context to a different tenant.
        app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
            "tenant-B"
        )
        with TestClient(app) as client:
            resp = client.get("/api/integrations/stripe/payments")
        assert resp.status_code == 200
        assert factory_calls == ["tenant-B"]

    def test_returns_404_when_no_integration(self):
        app, _ = _build_app(connector=None)
        with TestClient(app) as client:
            resp = client.get("/api/integrations/stripe/payments")
        assert resp.status_code == 404
        assert (
            resp.json()["detail"]["error_code"]
            == "stripe_integration_not_configured"
        )

    def test_limit_above_100_is_capped_to_100(self):
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[])
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.get(
                "/api/integrations/stripe/payments?limit=500"
            )
        assert resp.status_code == 200
        # Stripe SDK MUST NOT be asked for more than 100.
        assert pi_api.list_calls[0]["limit"] == 100

    def test_sensitive_fields_never_leak_through_response(self):
        raw = _sensitive_payment_intent("pi_c")
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[raw])
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.get("/api/integrations/stripe/payments")
        assert resp.status_code == 200
        body_text = resp.text
        # Top-level raw-PII markers MUST NOT appear anywhere in the body.
        banned_markers = (
            "4242424242424242",  # card number
            "pi_secret_must_not_leak",  # client_secret
            "op@example.com",  # receipt_email
            "customer@example.com",  # billing_details.email
            "payment_method_data",
            "client_secret",
            "receipt_email",
            "billing_details",
            "charges",
        )
        for marker in banned_markers:
            assert marker not in body_text, (
                f"/payments response leaked {marker!r}"
            )

    def test_invalid_created_timestamp_returns_400(self):
        pi_api = _FakeListingPaymentIntentAPI(raw_items=[])
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.get(
                "/api/integrations/stripe/payments?created.gte=not-a-date"
            )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "invalid_timestamp"

    def test_connector_error_returns_503(self):
        pi_api = _FakeListingPaymentIntentAPI(
            raises=RuntimeError("stripe down")
        )
        stripe_sdk = _FakeStripeSDK(payment_intent_api=pi_api)
        connector = _build_connector(stripe_module=stripe_sdk)
        app, _ = _build_app(connector=connector)
        with TestClient(app) as client:
            resp = client.get("/api/integrations/stripe/payments")
        assert resp.status_code == 503
        assert (
            resp.json()["detail"]["error_code"]
            == "stripe_list_payments_failed"
        )
