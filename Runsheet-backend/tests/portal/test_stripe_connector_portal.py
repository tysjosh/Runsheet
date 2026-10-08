"""The portal StripeConnector methods use a per-request key (FREEZE F7; PAY-12).

The local venv has no ``stripe`` package, so a fake module is injected via
``stripe_module=``. Its ``PaymentIntent`` methods record the ``api_key``
kwarg and sleep 50 ms in the worker thread, so concurrent calls overlap.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

from integrations.stripe_connector import StripeConnector

SENTINEL = "module-level-key-must-never-change"
KEYS = {"demo-tenant": "sk_test_tenant_one_SECRET", "qa-tenant-b": "sk_test_tenant_two_SECRET"}


class _FakeStripeModule:
    def __init__(self) -> None:
        self.api_key = SENTINEL
        self.calls = []
        self._lock = threading.Lock()
        module = self

        class _PaymentIntent:
            @staticmethod
            def create(**kwargs):
                time.sleep(0.05)
                with module._lock:
                    module.calls.append(("create", kwargs.get("api_key"), kwargs))
                return {"id": f"pi_{kwargs['metadata']['tenant']}", "client_secret": "cs_x",
                        "status": "requires_payment_method", "amount": kwargs["amount"]}

            @staticmethod
            def cancel(pi_id, **kwargs):
                time.sleep(0.05)
                with module._lock:
                    module.calls.append(("cancel", kwargs.get("api_key"), {"id": pi_id}))
                return {"id": pi_id, "status": "canceled"}

            @staticmethod
            def retrieve(pi_id, **kwargs):
                time.sleep(0.05)
                with module._lock:
                    module.calls.append(("retrieve", kwargs.get("api_key"), {"id": pi_id}))
                return SimpleNamespace(to_dict=lambda: {"id": pi_id, "status": "processing",
                                                        "client_secret": "cs_r"})

        self.PaymentIntent = _PaymentIntent


class _Vault:
    async def get(self, tenant_id, ref):
        return {"secret_key": KEYS[tenant_id], "publishable_key": f"pk_{tenant_id}",
                "webhook_secret": "whsec_x"}


def _connector(tenant_id, module):
    return StripeConnector(tenant_id=tenant_id, instance_id=f"inst-{tenant_id}",
                           credentials_vault=_Vault(), credentials_ref="ref-1",
                           stripe_module=module)


async def test_concurrent_tenants_use_own_key(caplog):
    caplog.set_level(logging.DEBUG)
    module = _FakeStripeModule()
    conns = {t: _connector(t, module) for t in KEYS}

    async def run(tenant_id):
        c = conns[tenant_id]
        created = await c.create_portal_ach_intent(
            1234, idempotency_key=f"portal_pa_{tenant_id}", metadata={"tenant": tenant_id},
            description="Invoice INV-1",
        )
        canceled = await c.cancel_intent(f"pi_{tenant_id}")
        retrieved = await c.retrieve_intent(f"pi_{tenant_id}")
        return created, canceled, retrieved

    started = time.monotonic()
    results = await asyncio.gather(*(run(t) for t in KEYS for _ in range(3)))
    assert time.monotonic() - started < 6 * 3 * 0.05  # the calls overlapped

    assert module.api_key == SENTINEL
    by_pi = {}
    for name, key, kwargs in module.calls:
        pi = kwargs.get("id") or f"pi_{kwargs['metadata']['tenant']}"
        by_pi.setdefault(pi, set()).add(key)
    assert by_pi == {f"pi_{t}": {k} for t, k in KEYS.items()}
    assert len(module.calls) == 18

    created, canceled, retrieved = results[0]
    assert created == {"id": "pi_demo-tenant", "client_secret": "cs_x",
                       "status": "requires_payment_method"}
    assert canceled == {"id": "pi_demo-tenant", "status": "canceled"}
    assert retrieved == {"id": "pi_demo-tenant", "client_secret": "cs_r", "status": "processing"}
    for key in KEYS.values():
        assert key not in caplog.text


async def test_create_body_is_us_bank_account_with_request_options():
    module = _FakeStripeModule()
    await _connector("demo-tenant", module).create_portal_ach_intent(
        30660, idempotency_key="portal_pa_ppa_1",
        metadata={"tenant": "demo-tenant", "source": "runsheet_portal"},
        description="Invoice INV-7",
    )
    (_name, key, kwargs), = module.calls
    assert key == KEYS["demo-tenant"]
    assert kwargs["idempotency_key"] == "portal_pa_ppa_1"
    assert kwargs["amount"] == 30660 and kwargs["currency"] == "usd"
    assert kwargs["payment_method_types"] == ["us_bank_account"]
    assert kwargs["payment_method_options"] == {
        "us_bank_account": {"verification_method": "automatic"}
    }
    assert kwargs["description"] == "Invoice INV-7"
    assert kwargs["metadata"]["source"] == "runsheet_portal"
    assert module.api_key == SENTINEL
