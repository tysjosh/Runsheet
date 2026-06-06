"""
Unit + property tests for the cross-module RefResolver (Phase A).

Covers task 1 (resolve / unresolved marker / batch) and task 1.1 (write-time
validation helper), plus the optional property test 1.2 (validation soundness +
tenant containment).

Feature: cross-module-entity-linkage
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from errors.exceptions import AppException
from services.ref_resolver import RefResolver


# ---------------------------------------------------------------------------
# In-memory fake store + loader factory
# ---------------------------------------------------------------------------


class FakeStore:
    """A tiny tenant-scoped store: {tenant_id: {entity_id: summary}}."""

    def __init__(self) -> None:
        self.data: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def put(self, tenant_id: str, entity_id: str, summary: Dict[str, Any]) -> None:
        self.data.setdefault(tenant_id, {})[entity_id] = summary

    def loader(self):
        async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
            return self.data.get(tenant_id, {}).get(entity_id)

        return _load


def _resolver_with_customer(tenant: str = "t1") -> tuple[RefResolver, FakeStore]:
    store = FakeStore()
    store.put(tenant, "CUST-1", {"display_name": "Acme Fuel", "status": "active"})
    resolver = RefResolver()
    resolver.register("customer", store.loader())
    return resolver, store


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_returns_summary_for_existing_ref():
    resolver, _ = _resolver_with_customer()
    ref = asyncio.run(resolver.resolve("t1", "customer", "CUST-1"))
    assert ref.is_resolved
    assert ref.status == "resolved"
    assert ref.summary == {"display_name": "Acme Fuel", "status": "active"}
    assert ref.to_dict() == {
        "status": "resolved",
        "id": "CUST-1",
        "summary": {"display_name": "Acme Fuel", "status": "active"},
    }


def test_resolve_unknown_id_is_unresolved():
    resolver, _ = _resolver_with_customer()
    ref = asyncio.run(resolver.resolve("t1", "customer", "CUST-NOPE"))
    assert not ref.is_resolved
    assert ref.status == "unresolved"
    assert ref.to_dict() == {"status": "unresolved", "id": "CUST-NOPE"}


def test_resolve_absent_id_is_empty():
    resolver, _ = _resolver_with_customer()
    ref = asyncio.run(resolver.resolve("t1", "customer", None))
    assert ref.status == "empty"
    assert ref.to_dict() == {"status": "empty", "id": None}


def test_resolve_unregistered_type_is_unresolved():
    resolver, _ = _resolver_with_customer()
    ref = asyncio.run(resolver.resolve("t1", "asset", "AST-1"))
    assert ref.status == "unresolved"


def test_loader_exception_is_treated_as_unresolved():
    resolver = RefResolver()

    async def _boom(_t: str, _i: str):
        raise RuntimeError("backend down")

    resolver.register("order", _boom)
    ref = asyncio.run(resolver.resolve("t1", "order", "ORD-1"))
    assert ref.status == "unresolved"


def test_resolve_many_labels_batch():
    resolver, store = _resolver_with_customer()
    store.put("t1", "ORD-1", {"status": "placed"})
    resolver.register("order", store.loader())

    out = asyncio.run(
        resolver.resolve_many(
            "t1",
            {
                "customer": ("customer", "CUST-1"),
                "order": ("order", "ORD-1"),
                "asset": ("asset", "AST-x"),  # unregistered -> unresolved
            },
        )
    )
    assert out["customer"].is_resolved
    assert out["order"].is_resolved
    assert out["asset"].status == "unresolved"


# ---------------------------------------------------------------------------
# validate_ref() — task 1.1
# ---------------------------------------------------------------------------


def test_validate_ref_passes_for_existing_same_tenant():
    resolver, _ = _resolver_with_customer()
    # Should not raise
    asyncio.run(resolver.validate_ref("t1", "customer", "CUST-1"))


def test_validate_ref_rejects_unknown_id():
    resolver, _ = _resolver_with_customer()
    with pytest.raises(AppException) as exc:
        asyncio.run(resolver.validate_ref("t1", "customer", "CUST-NOPE"))
    assert exc.value.details["reason"] == "customer_not_found"
    assert exc.value.status_code == 400


def test_validate_ref_rejects_cross_tenant_id():
    resolver, store = _resolver_with_customer(tenant="t1")
    # Same id exists for t1 but caller is t2 -> must not resolve (tenant containment).
    with pytest.raises(AppException) as exc:
        asyncio.run(resolver.validate_ref("t2", "customer", "CUST-1"))
    assert exc.value.details["reason"] == "customer_not_found"


def test_validate_ref_required_missing_raises():
    resolver, _ = _resolver_with_customer()
    with pytest.raises(AppException) as exc:
        asyncio.run(resolver.validate_ref("t1", "customer", None))
    assert exc.value.details["reason"] == "customer_required"


def test_validate_ref_optional_missing_ok():
    resolver, _ = _resolver_with_customer()
    # Should not raise when not required.
    asyncio.run(resolver.validate_ref("t1", "customer", None, required=False))


# ---------------------------------------------------------------------------
# Property 2 (tenant containment) + Property 5 (validation soundness)
# Feature: cross-module-entity-linkage, Property 2 & 5
# ---------------------------------------------------------------------------

_ids = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-", min_size=1, max_size=12
)
_tenants = st.sampled_from(["tenant-a", "tenant-b", "tenant-c"])


@settings(max_examples=100)
@given(
    owner=_tenants,
    caller=_tenants,
    entity_id=_ids,
)
def test_property_tenant_containment_and_validation_soundness(owner, caller, entity_id):
    """A ref validates iff caller tenant == owner tenant; resolve never leaks
    an entity across tenants."""
    store = FakeStore()
    store.put(owner, entity_id, {"display_name": "X"})
    resolver = RefResolver()
    resolver.register("customer", store.loader())

    ref = asyncio.run(resolver.resolve(caller, "customer", entity_id))

    if caller == owner:
        assert ref.is_resolved  # Property 5: soundness — exists in tenant
    else:
        # Property 2: containment — another tenant's id never resolves
        assert not ref.is_resolved
        with pytest.raises(AppException):
            asyncio.run(resolver.validate_ref(caller, "customer", entity_id))


# ---------------------------------------------------------------------------
# Production order loader (cross-module-entity-linkage task 3.2)
# Powers the job resolver read's expand=order link.
# ---------------------------------------------------------------------------


class _FakeOrder:
    def __init__(self, status, customer_id, customer_name):
        self.status = status
        self.customer_id = customer_id
        self.customer_name = customer_name


class _FakeOrderRepo:
    """Tenant-scoped order repo: get(tenant_id, order_id) -> order | None."""

    def __init__(self, table):
        self._table = table  # {(tenant_id, order_id): order}

    async def get(self, tenant_id, order_id):
        return self._table.get((tenant_id, order_id))


def test_order_loader_resolves_same_tenant_order():
    from services.ref_loaders import make_order_loader_from_repo

    repo = _FakeOrderRepo({("t1", "ORD-1"): _FakeOrder("placed", "CUST-1", "Acme")})
    resolver = RefResolver()
    resolver.register("order", make_order_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t1", "order", "ORD-1"))
    assert ref.is_resolved
    assert ref.summary == {
        "order_id": "ORD-1",
        "status": "placed",
        "customer_id": "CUST-1",
        "customer_name": "Acme",
    }


def test_order_loader_cross_tenant_is_unresolved():
    from services.ref_loaders import make_order_loader_from_repo

    repo = _FakeOrderRepo({("t1", "ORD-1"): _FakeOrder("placed", "CUST-1", "Acme")})
    resolver = RefResolver()
    resolver.register("order", make_order_loader_from_repo(repo))

    # Caller in another tenant must not resolve t1's order (Property 2).
    ref = asyncio.run(resolver.resolve("t2", "order", "ORD-1"))
    assert not ref.is_resolved


def test_register_order_link_loaders_registers_order_when_repo_present():
    from services.ref_loaders import register_order_link_loaders

    repo = _FakeOrderRepo({})
    resolver = RefResolver()
    register_order_link_loaders(resolver, order_repository=repo)
    assert "order" in resolver.registered_types()


# ---------------------------------------------------------------------------
# Production depot loader (cross-module-entity-linkage task 9)
# Resolves an asset's assigned_depot_id / tenant's default_depot_id to a
# depot record, round-tripping is_default (Req 10.1, 10.3).
# ---------------------------------------------------------------------------


class _FakeDepot:
    def __init__(self, name, status, address, is_default):
        self.name = name
        self.status = status
        self.address = address
        self.is_default = is_default


class _FakeDepotRepo:
    """Tenant-scoped depot repo: get(tenant_id, depot_id) -> depot | None."""

    def __init__(self, table):
        self._table = table  # {(tenant_id, depot_id): depot}

    async def get(self, tenant_id, depot_id):
        return self._table.get((tenant_id, depot_id))


def test_depot_loader_resolves_same_tenant_depot_with_is_default():
    from services.ref_loaders import make_depot_loader_from_repo

    repo = _FakeDepotRepo(
        {("t1", "depot_1"): _FakeDepot("Newark Rack", "active", "1 Fuel Ln", True)}
    )
    resolver = RefResolver()
    resolver.register("depot", make_depot_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t1", "depot", "depot_1"))
    assert ref.is_resolved
    # is_default must round-trip through the summary (Req 10.3).
    assert ref.summary == {
        "depot_id": "depot_1",
        "name": "Newark Rack",
        "status": "active",
        "address": "1 Fuel Ln",
        "is_default": True,
    }


def test_depot_loader_round_trips_non_default_flag():
    from services.ref_loaders import make_depot_loader_from_repo

    repo = _FakeDepotRepo(
        {("t1", "depot_2"): _FakeDepot("Dallas Yard", "active", "9 Rack Rd", False)}
    )
    resolver = RefResolver()
    resolver.register("depot", make_depot_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t1", "depot", "depot_2"))
    assert ref.is_resolved
    # A False flag is still surfaced (not dropped), so the UI never infers it.
    assert ref.summary["is_default"] is False


def test_depot_loader_cross_tenant_is_unresolved():
    from services.ref_loaders import make_depot_loader_from_repo

    repo = _FakeDepotRepo(
        {("t1", "depot_1"): _FakeDepot("Newark Rack", "active", "1 Fuel Ln", True)}
    )
    resolver = RefResolver()
    resolver.register("depot", make_depot_loader_from_repo(repo))

    # Caller in another tenant must not resolve t1's depot (Property 2).
    ref = asyncio.run(resolver.resolve("t2", "depot", "depot_1"))
    assert not ref.is_resolved


def test_register_depot_loader_registers_depot_when_repo_present():
    from services.ref_loaders import register_depot_loader

    repo = _FakeDepotRepo({})
    resolver = RefResolver()
    register_depot_loader(resolver, depot_repository=repo)
    assert "depot" in resolver.registered_types()


def test_register_depot_loader_noop_without_dependency():
    from services.ref_loaders import register_depot_loader

    resolver = RefResolver()
    register_depot_loader(resolver)
    # No backing dependency → no loader registered; references degrade to
    # unresolved rather than erroring.
    assert "depot" not in resolver.registered_types()
    ref = asyncio.run(resolver.resolve("t1", "depot", "depot_1"))
    assert not ref.is_resolved


# ---------------------------------------------------------------------------
# Production billing loaders (cross-module-entity-linkage task 12)
# Resolve a payment's invoice_id/account_id and a canonical payment_id from
# the commerce ES projections (Req 12.1, 12.3, 12.4).
# ---------------------------------------------------------------------------


class _FakeBillingES:
    """Minimal ES stub keyed by index → list of source docs.

    Mirrors the ``search_documents(index, query, size)`` contract the billing
    loaders use; matches the index's id field term within the tenant-scoped
    filter that ``inject_tenant_filter`` adds.
    """

    _ID_FIELD = {
        "invoices_current": "invoice_id",
        "accounts_current": "account_id",
        "payments_current": "payment_id",
    }

    def __init__(self, table):
        self._table = table  # {index: [source, ...]}

    async def search_documents(self, index, query, size=1):
        docs = self._table.get(index, [])
        q = str(query)
        id_field = self._ID_FIELD.get(index)
        hits = []
        for doc in docs:
            ident = doc.get(id_field) if id_field else None
            # term match on the id field AND tenant-scoped filter match.
            if ident and str(ident) in q and str(doc.get("tenant_id")) in q:
                hits.append({"_source": doc})
        return {"hits": {"hits": hits[:size]}}


def test_invoice_loader_resolves_same_tenant_invoice():
    from services.ref_loaders import make_invoice_loader

    es = _FakeBillingES(
        {
            "invoices_current": [
                {
                    "invoice_id": "inv_1",
                    "tenant_id": "t1",
                    "status": "open",
                    "total_cents": 5000,
                    "remaining_cents": 5000,
                    "customer_id": "cust_1",
                    "account_id": "acct_1",
                }
            ]
        }
    )
    resolver = RefResolver()
    resolver.register("invoice", make_invoice_loader(es))

    ref = asyncio.run(resolver.resolve("t1", "invoice", "inv_1"))
    assert ref.is_resolved
    assert ref.summary["invoice_id"] == "inv_1"
    assert ref.summary["account_id"] == "acct_1"
    assert ref.summary["customer_id"] == "cust_1"


def test_account_loader_resolves_same_tenant_account():
    from services.ref_loaders import make_account_loader

    es = _FakeBillingES(
        {
            "accounts_current": [
                {
                    "account_id": "acct_1",
                    "tenant_id": "t1",
                    "display_name": "Acme Fuel",
                    "status": "active",
                    "customer_id": "cust_1",
                }
            ]
        }
    )
    resolver = RefResolver()
    resolver.register("account", make_account_loader(es))

    ref = asyncio.run(resolver.resolve("t1", "account", "acct_1"))
    assert ref.is_resolved
    assert ref.summary["display_name"] == "Acme Fuel"
    assert ref.summary["customer_id"] == "cust_1"


def test_payment_loader_resolves_same_tenant_payment():
    from services.ref_loaders import make_payment_loader

    es = _FakeBillingES(
        {
            "payments_current": [
                {
                    "payment_id": "pay_1",
                    "tenant_id": "t1",
                    "status": "applied",
                    "amount_cents": 5000,
                    "source": "stripe",
                    "invoice_id": "inv_1",
                    "account_id": "acct_1",
                }
            ]
        }
    )
    resolver = RefResolver()
    resolver.register("payment", make_payment_loader(es))

    ref = asyncio.run(resolver.resolve("t1", "payment", "pay_1"))
    assert ref.is_resolved
    assert ref.summary["payment_id"] == "pay_1"
    assert ref.summary["source"] == "stripe"


def test_billing_loader_cross_tenant_is_unresolved():
    from services.ref_loaders import make_invoice_loader

    es = _FakeBillingES(
        {
            "invoices_current": [
                {"invoice_id": "inv_1", "tenant_id": "t1", "status": "open"}
            ]
        }
    )
    resolver = RefResolver()
    resolver.register("invoice", make_invoice_loader(es))

    # Caller in another tenant must not resolve t1's invoice (Property 2).
    ref = asyncio.run(resolver.resolve("t2", "invoice", "inv_1"))
    assert not ref.is_resolved


def test_register_billing_link_loaders_registers_all_when_es_present():
    from services.ref_loaders import register_billing_link_loaders

    resolver = RefResolver()
    register_billing_link_loaders(resolver, es_service=_FakeBillingES({}))
    types = resolver.registered_types()
    assert "invoice" in types
    assert "account" in types
    assert "payment" in types


def test_register_billing_link_loaders_noop_without_es():
    from services.ref_loaders import register_billing_link_loaders

    resolver = RefResolver()
    register_billing_link_loaders(resolver)
    # No ES → no loaders registered; references degrade to unresolved.
    assert "invoice" not in resolver.registered_types()


# ---------------------------------------------------------------------------
# Production customer-tank loader (cross-module-entity-linkage task 6)
# Resolves a customer_tank_id to a summary via the tenant-scoped repository
# (Req 7.3, 13.1).
# ---------------------------------------------------------------------------


class _FakeTank:
    def __init__(self, customer_id, status, fuel_product_code, last_refill_order_id):
        self.customer_id = customer_id
        self.status = status
        self.fuel_product_code = fuel_product_code
        self.last_refill_order_id = last_refill_order_id


class _FakeTankRepo:
    """Tenant-scoped tank repo: get(tenant_id, customer_tank_id) -> tank | None."""

    def __init__(self, table):
        self._table = table  # {(tenant_id, tank_id): tank}

    async def get(self, tenant_id, tank_id):
        return self._table.get((tenant_id, tank_id))


def test_customer_tank_loader_resolves_same_tenant_tank():
    from services.ref_loaders import make_customer_tank_loader_from_repo

    repo = _FakeTankRepo(
        {("t1", "tank_1"): _FakeTank("cust_1", "active", "PROPANE", "ORD-9")}
    )
    resolver = RefResolver()
    resolver.register("tank", make_customer_tank_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t1", "tank", "tank_1"))
    assert ref.is_resolved
    assert ref.summary == {
        "customer_tank_id": "tank_1",
        "customer_id": "cust_1",
        "status": "active",
        "fuel_product_code": "PROPANE",
        "last_refill_order_id": "ORD-9",
    }


def test_customer_tank_loader_cross_tenant_is_unresolved():
    from services.ref_loaders import make_customer_tank_loader_from_repo

    repo = _FakeTankRepo(
        {("t1", "tank_1"): _FakeTank("cust_1", "active", "PROPANE", None)}
    )
    resolver = RefResolver()
    resolver.register("tank", make_customer_tank_loader_from_repo(repo))

    # Caller in another tenant must not resolve t1's tank (Property 2).
    ref = asyncio.run(resolver.resolve("t2", "tank", "tank_1"))
    assert not ref.is_resolved


def test_register_customer_tank_link_loader_registers_when_repo_present():
    from services.ref_loaders import register_customer_tank_link_loader

    repo = _FakeTankRepo({})
    resolver = RefResolver()
    register_customer_tank_link_loader(resolver, customer_tank_repository=repo)
    assert "tank" in resolver.registered_types()


def test_register_customer_tank_link_loader_noop_without_dependency():
    from services.ref_loaders import register_customer_tank_link_loader

    resolver = RefResolver()
    register_customer_tank_link_loader(resolver)
    assert "tank" not in resolver.registered_types()


# ---------------------------------------------------------------------------
# Production terminal / contract loaders (cross-module-entity-linkage task 8)
# Resolve a terminal_id / contract_id to a summary via the tenant-scoped
# repositories so sourcing recommendations, terminal BOLs, and wait reports
# reference canonical records instead of free text (Req 9.1, 9.2, 13.1).
# ---------------------------------------------------------------------------


class _FakeTerminal:
    def __init__(self, name, operator, status, branded=False, supplier_brand=None):
        self.name = name
        self.operator = operator
        self.status = status
        self.branded = branded
        self.supplier_brand = supplier_brand


class _FakeContract:
    def __init__(self, supplier_name, product_code, status):
        self.supplier_name = supplier_name
        self.product_code = product_code
        self.status = status


class _FakeTerminalRepo:
    """Tenant-scoped terminal repo: get(tenant_id, terminal_id) -> terminal | None."""

    def __init__(self, table):
        self._table = table  # {(tenant_id, terminal_id): terminal}

    async def get(self, tenant_id, terminal_id):
        return self._table.get((tenant_id, terminal_id))


class _FakeContractRepo:
    """Tenant-scoped contract repo: get(tenant_id, contract_id) -> contract | None."""

    def __init__(self, table):
        self._table = table  # {(tenant_id, contract_id): contract}

    async def get(self, tenant_id, contract_id):
        return self._table.get((tenant_id, contract_id))


def test_terminal_loader_resolves_same_tenant_terminal():
    from services.ref_loaders import make_terminal_loader_from_repo

    repo = _FakeTerminalRepo(
        {
            ("t1", "term_1"): _FakeTerminal(
                "Buckeye Newark", "Buckeye", "active", branded=False
            )
        }
    )
    resolver = RefResolver()
    resolver.register("terminal", make_terminal_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t1", "terminal", "term_1"))
    assert ref.is_resolved
    assert ref.summary == {
        "terminal_id": "term_1",
        "name": "Buckeye Newark",
        "operator": "Buckeye",
        "status": "active",
        "branded": False,
    }


def test_terminal_loader_cross_tenant_is_unresolved():
    from services.ref_loaders import make_terminal_loader_from_repo

    repo = _FakeTerminalRepo(
        {("t1", "term_1"): _FakeTerminal("Buckeye Newark", "Buckeye", "active")}
    )
    resolver = RefResolver()
    resolver.register("terminal", make_terminal_loader_from_repo(repo))

    # Caller in another tenant must not resolve t1's terminal (Property 2).
    ref = asyncio.run(resolver.resolve("t2", "terminal", "term_1"))
    assert not ref.is_resolved


def test_contract_loader_resolves_same_tenant_contract():
    from services.ref_loaders import make_contract_loader_from_repo

    repo = _FakeContractRepo(
        {("t1", "sc_1"): _FakeContract("Acme Supply", "DIESEL_2", "active")}
    )
    resolver = RefResolver()
    resolver.register("contract", make_contract_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t1", "contract", "sc_1"))
    assert ref.is_resolved
    assert ref.summary == {
        "contract_id": "sc_1",
        "supplier_name": "Acme Supply",
        "product_code": "DIESEL_2",
        "status": "active",
    }


def test_contract_loader_cross_tenant_is_unresolved():
    from services.ref_loaders import make_contract_loader_from_repo

    repo = _FakeContractRepo(
        {("t1", "sc_1"): _FakeContract("Acme Supply", "DIESEL_2", "active")}
    )
    resolver = RefResolver()
    resolver.register("contract", make_contract_loader_from_repo(repo))

    ref = asyncio.run(resolver.resolve("t2", "contract", "sc_1"))
    assert not ref.is_resolved


def test_register_terminal_link_loaders_registers_when_repos_present():
    from services.ref_loaders import register_terminal_link_loaders

    resolver = RefResolver()
    register_terminal_link_loaders(
        resolver,
        terminal_repository=_FakeTerminalRepo({}),
        supplier_contract_repository=_FakeContractRepo({}),
    )
    types = resolver.registered_types()
    assert "terminal" in types
    assert "contract" in types


def test_register_terminal_link_loaders_noop_without_dependency():
    from services.ref_loaders import register_terminal_link_loaders

    resolver = RefResolver()
    register_terminal_link_loaders(resolver)
    # No backing repositories → no loaders registered; references degrade to
    # unresolved rather than erroring.
    assert "terminal" not in resolver.registered_types()
    assert "contract" not in resolver.registered_types()
