"""
Unit tests for the billing resolver reads (cross-module-entity-linkage task 11).

Exercises the additive ``?expand=...`` behaviour on:

* ``GET /api/commerce/invoices/{id}?expand=order,account,customer``
* ``GET /api/commerce/accounts/{id}?expand=customer``

Both reads resolve their references via the shared ``RefResolver`` into a
``links`` object where each reference is either a resolved summary or an
explicit ``unresolved``/``empty`` marker — never silently dropped (Req 5.4 /
Property 4). Non-expanded reads stay byte-compatible with the prior contract
(additive/backward-compatible, Req 6.3). All resolution is tenant-scoped; the
loaders never cross tenants (Req 5.3).

The feature-flag gate dependencies (``require_invoicing_enabled`` /
``require_accounts_enabled``) are overridden so the suite stays decoupled from
tenant feature flags and Elasticsearch.

Validates: Requirements 12.1, 12.4, 5.3, 5.4, 6.3.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import AppException, resource_not_found
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from services.ref_resolver import RefResolver

import commerce.api.invoice_endpoints as invoice_endpoints
import commerce.api.account_endpoints as account_endpoints


@pytest.fixture(autouse=True)
def _reset_module_level_resolvers():
    """Reset the endpoint module-level resolver/service overrides after each test.

    ``configure_invoice_api`` / ``configure_account_api`` mutate process-wide
    module state (``_ref_resolver`` / ``_invoice_service`` / ``_account_service``).
    Without cleanup that state leaks into later tests in a full-suite run (e.g.
    the resolution-totality property test, which relies on the process-wide
    resolver rather than a module-level override). Restoring the originals keeps
    the suite order-independent.
    """
    originals = {
        (invoice_endpoints, "_ref_resolver"): invoice_endpoints._ref_resolver,
        (invoice_endpoints, "_invoice_service"): invoice_endpoints._invoice_service,
        (account_endpoints, "_ref_resolver"): account_endpoints._ref_resolver,
        (account_endpoints, "_account_service"): account_endpoints._account_service,
        (account_endpoints, "_credit_service"): account_endpoints._credit_service,
    }
    try:
        yield
    finally:
        for (module, attr), value in originals.items():
            setattr(module, attr, value)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeInvoiceService:
    """In-memory fake of InvoiceService.get keyed by (tenant_id, invoice_id)."""

    def __init__(self) -> None:
        self._invoices: Dict[str, Dict[str, Any]] = {}

    def seed(self, tenant_id: str, invoice: Dict[str, Any]) -> None:
        self._invoices[f"{tenant_id}::{invoice['invoice_id']}"] = invoice

    async def get(self, *, tenant_id: str, invoice_id: str) -> Dict[str, Any]:
        inv = self._invoices.get(f"{tenant_id}::{invoice_id}")
        if inv is None:
            raise resource_not_found(
                message=f"Invoice '{invoice_id}' not found",
                details={"invoice_id": invoice_id},
            )
        return inv


class FakeAccountService:
    """In-memory fake of AccountService.get keyed by (tenant_id, account_id)."""

    def __init__(self) -> None:
        self._accounts: Dict[str, Dict[str, Any]] = {}

    def seed(self, tenant_id: str, account: Dict[str, Any]) -> None:
        self._accounts[f"{tenant_id}::{account['account_id']}"] = account

    async def get(self, tenant_id: str, account_id: str) -> Dict[str, Any]:
        acct = self._accounts.get(f"{tenant_id}::{account_id}")
        if acct is None:
            raise resource_not_found(
                message=f"Account '{account_id}' not found",
                details={"account_id": account_id},
            )
        return acct


def _tenant_ctx(tenant_id: str = "tenant-A") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="user-1",
        has_pii_access=True,
        roles=["admin"],
    )


def _attach_exception_handler(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.to_dict()},
        )


# ---------------------------------------------------------------------------
# Invoice resolver read
# ---------------------------------------------------------------------------


def _build_invoice_app(
    *,
    service: FakeInvoiceService,
    resolver: Optional[RefResolver] = None,
    tenant_id: str = "tenant-A",
) -> TestClient:
    invoice_endpoints.configure_invoice_api(
        invoice_service=service, ref_resolver=resolver
    )
    app = FastAPI()
    app.include_router(invoice_endpoints.router)
    _attach_exception_handler(app)
    ctx = _tenant_ctx(tenant_id)
    # Bypass the feature-flag gate so the suite is decoupled from flags.
    app.dependency_overrides[invoice_endpoints.require_invoicing_enabled] = lambda: ctx
    app.dependency_overrides[get_tenant_context] = lambda: ctx
    return TestClient(app)


def _make_invoice(**overrides: Any) -> Dict[str, Any]:
    inv = {
        "invoice_id": "inv_1",
        "tenant_id": "tenant-A",
        "customer_id": "cust-1",
        "account_id": "acct-1",
        "order_id": "ord-1",
        "status": "open",
        "total_cents": 5000,
    }
    inv.update(overrides)
    return inv


class TestInvoiceResolverRead:
    """``GET /api/commerce/invoices/{id}?expand=order,account,customer``."""

    def test_non_expanded_read_has_no_links(self):
        """Without ``expand`` the response carries no ``links`` key (Req 6.3)."""
        svc = FakeInvoiceService()
        svc.seed("tenant-A", _make_invoice())
        client = _build_invoice_app(service=svc, resolver=RefResolver())

        resp = client.get("/api/commerce/invoices/inv_1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["invoice_id"] == "inv_1"
        assert "links" not in body

    def test_expand_resolves_order_account_customer(self):
        """Resolvable references come back as ``resolved`` summaries (Req 12.1)."""
        svc = FakeInvoiceService()
        svc.seed("tenant-A", _make_invoice())

        resolver = RefResolver()

        async def _order(tenant_id, entity_id):
            return {"order_id": entity_id, "status": "delivered"}

        async def _account(tenant_id, entity_id):
            return {"account_id": entity_id, "display_name": "Acme Billing"}

        async def _customer(tenant_id, entity_id):
            return {"customer_id": entity_id, "display_name": "Acme Fuel"}

        resolver.register("order", _order)
        resolver.register("account", _account)
        resolver.register("customer", _customer)

        client = _build_invoice_app(service=svc, resolver=resolver)

        resp = client.get(
            "/api/commerce/invoices/inv_1?expand=order,account,customer"
        )
        assert resp.status_code == 200
        links = resp.json()["links"]
        assert links["order"]["status"] == "resolved"
        assert links["order"]["summary"]["status"] == "delivered"
        assert links["account"]["status"] == "resolved"
        assert links["account"]["summary"]["display_name"] == "Acme Billing"
        assert links["customer"]["status"] == "resolved"
        assert links["customer"]["summary"]["display_name"] == "Acme Fuel"

    def test_expand_unresolved_reference_marked_not_dropped(self):
        """A dangling reference returns an explicit ``unresolved`` marker (Req 5.4)."""
        svc = FakeInvoiceService()
        svc.seed("tenant-A", _make_invoice(order_id="GHOST"))

        resolver = RefResolver()

        async def _none(tenant_id, entity_id):
            return None

        resolver.register("order", _none)

        client = _build_invoice_app(service=svc, resolver=resolver)

        resp = client.get("/api/commerce/invoices/inv_1?expand=order")
        assert resp.status_code == 200
        link = resp.json()["links"]["order"]
        assert link["status"] == "unresolved"
        assert link["id"] == "GHOST"
        assert "summary" not in link

    def test_expand_absent_reference_marked_empty(self):
        """A null reference id resolves to ``empty`` (absent, not dangling)."""
        svc = FakeInvoiceService()
        svc.seed("tenant-A", _make_invoice(order_id=None))
        client = _build_invoice_app(service=svc, resolver=RefResolver())

        resp = client.get("/api/commerce/invoices/inv_1?expand=order")
        assert resp.status_code == 200
        link = resp.json()["links"]["order"]
        assert link["status"] == "empty"
        assert link["id"] is None

    def test_unknown_expand_token_ignored(self):
        """Unknown ``expand`` tokens are ignored (forward-compatible)."""
        svc = FakeInvoiceService()
        svc.seed("tenant-A", _make_invoice())
        client = _build_invoice_app(service=svc, resolver=RefResolver())

        resp = client.get("/api/commerce/invoices/inv_1?expand=bogus")
        assert resp.status_code == 200
        # No known tokens → additive path skipped, no ``links`` key (Req 6.3).
        assert "links" not in resp.json()

    def test_cross_tenant_invoice_returns_404(self):
        """An invoice owned by another tenant is not readable (Req 5.3)."""
        svc = FakeInvoiceService()
        svc.seed("tenant-B", _make_invoice(tenant_id="tenant-B"))
        client = _build_invoice_app(
            service=svc, resolver=RefResolver(), tenant_id="tenant-A"
        )

        resp = client.get("/api/commerce/invoices/inv_1?expand=customer")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Account resolver read
# ---------------------------------------------------------------------------


def _build_account_app(
    *,
    service: FakeAccountService,
    resolver: Optional[RefResolver] = None,
    tenant_id: str = "tenant-A",
) -> TestClient:
    account_endpoints.configure_account_api(
        account_service=service, credit_service=object(), ref_resolver=resolver
    )
    app = FastAPI()
    app.include_router(account_endpoints.router)
    _attach_exception_handler(app)
    ctx = _tenant_ctx(tenant_id)
    app.dependency_overrides[account_endpoints.require_accounts_enabled] = lambda: ctx
    app.dependency_overrides[get_tenant_context] = lambda: ctx
    return TestClient(app)


def _make_account(**overrides: Any) -> Dict[str, Any]:
    acct = {
        "account_id": "acct-1",
        "tenant_id": "tenant-A",
        "customer_id": "cust-1",
        "display_name": "Acme Billing",
        "status": "active",
    }
    acct.update(overrides)
    return acct


class TestAccountResolverRead:
    """``GET /api/commerce/accounts/{id}?expand=customer``."""

    def test_non_expanded_read_has_no_links(self):
        """Without ``expand`` the response carries no ``links`` key (Req 6.3)."""
        svc = FakeAccountService()
        svc.seed("tenant-A", _make_account())
        client = _build_account_app(service=svc, resolver=RefResolver())

        resp = client.get("/api/commerce/accounts/acct-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["account_id"] == "acct-1"
        assert "links" not in body

    def test_expand_resolves_owning_customer(self):
        """A resolvable owning customer comes back as a summary (Req 12.4)."""
        svc = FakeAccountService()
        svc.seed("tenant-A", _make_account())

        resolver = RefResolver()

        async def _customer(tenant_id, entity_id):
            return {"customer_id": entity_id, "display_name": "Acme Fuel"}

        resolver.register("customer", _customer)

        client = _build_account_app(service=svc, resolver=resolver)

        resp = client.get("/api/commerce/accounts/acct-1?expand=customer")
        assert resp.status_code == 200
        link = resp.json()["links"]["customer"]
        assert link["status"] == "resolved"
        assert link["summary"]["display_name"] == "Acme Fuel"

    def test_expand_unresolved_customer_marked_not_dropped(self):
        """A dangling owning-customer ref returns ``unresolved`` (Req 5.4)."""
        svc = FakeAccountService()
        svc.seed("tenant-A", _make_account(customer_id="GHOST"))

        resolver = RefResolver()

        async def _none(tenant_id, entity_id):
            return None

        resolver.register("customer", _none)

        client = _build_account_app(service=svc, resolver=resolver)

        resp = client.get("/api/commerce/accounts/acct-1?expand=customer")
        assert resp.status_code == 200
        link = resp.json()["links"]["customer"]
        assert link["status"] == "unresolved"
        assert link["id"] == "GHOST"
        assert "summary" not in link

    def test_cross_tenant_account_returns_404(self):
        """An account owned by another tenant is not readable (Req 5.3)."""
        svc = FakeAccountService()
        svc.seed("tenant-B", _make_account(tenant_id="tenant-B"))
        client = _build_account_app(
            service=svc, resolver=RefResolver(), tenant_id="tenant-A"
        )

        resp = client.get("/api/commerce/accounts/acct-1?expand=customer")
        assert resp.status_code == 404
