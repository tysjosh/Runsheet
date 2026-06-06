"""
Production loaders for the cross-module :class:`RefResolver`.

The resolver itself (``services.ref_resolver``) is a pure registry: it delegates
all I/O to per-entity-type *loaders* with the shape
``async (tenant_id, entity_id) -> summary | None``. This module builds those
loaders for the canonical entity types the order/job resolver reads need
(``customer`` / ``asset`` / ``driver``) and registers them on a resolver
instance.

Each loader is tenant-scoped two ways for defense-in-depth, mirroring the
repository posture:

1. The Elasticsearch query is wrapped with
   :func:`ops.middleware.tenant_guard.inject_tenant_filter`, and
2. the returned source is re-validated against the caller's ``tenant_id``
   before any summary crosses the boundary,

so a reference to an id owned by another tenant resolves to ``None`` (surfaced
as ``unresolved`` on read — never leaked across tenants, Req 5.3 / Property 2).

Design: ``.kiro/specs/cross-module-entity-linkage/design.md`` §Referential
Integrity Strategy.

Validates: Requirements 1.1, 5.1, 5.3, 5.4.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

EntityLoader = Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]]

# Canonical ES indices the loaders read from.
CUSTOMERS_INDEX = "customers_current"
ASSETS_INDEX = "assets"  # alias onto the migrated ``trucks`` index
DRIVERS_INDEX = "drivers_current"
# Commerce billing projections (cross-module-entity-linkage Phase G, Req 12).
INVOICES_INDEX = "invoices_current"
ACCOUNTS_INDEX = "accounts_current"
PAYMENTS_INDEX = "payments_current"
DEPOTS_INDEX = "depots"


def _first_source(resp: Any) -> Optional[Dict[str, Any]]:
    """Extract the first ``_source`` mapping from an ES search response."""
    if not resp:
        return None
    hits = (resp.get("hits") or {}).get("hits") or []
    if not hits:
        return None
    source = hits[0].get("_source")
    return source if isinstance(source, dict) else None


def _pick(source: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Return only the present keys from ``source`` (drop missing/None-only)."""
    return {k: source[k] for k in keys if source.get(k) is not None}


def make_customer_loader(es_service: Any) -> EntityLoader:
    """Loader resolving a ``customer_id`` to a small display summary.

    Reads the tenant's ``customers_current`` projection. Returns
    ``{display_name, status}`` (Req 1.1, 1.4 — the commerce record is the
    source of truth for the display name).
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"term": {"customer_id": entity_id}}}, tenant_id
        )
        query["size"] = 1
        resp = await es_service.search_documents(CUSTOMERS_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "customer_id": entity_id,
            **_pick(source, "display_name", "legal_name", "status"),
        }

    return _load


def make_asset_loader(es_service: Any) -> EntityLoader:
    """Loader resolving an ``asset_id`` (a.k.a. ``truck_id``) to a summary.

    The ``assets`` alias points at the ``trucks`` index whose documents key on
    either ``asset_id`` or ``truck_id`` depending on vintage, so the lookup
    matches both. Returns ``{name, asset_type, asset_subtype, status}``.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        inner = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"asset_id": entity_id}},
                        {"term": {"truck_id": entity_id}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        }
        query = inject_tenant_filter(inner, tenant_id)
        query["size"] = 1
        resp = await es_service.search_documents(ASSETS_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "asset_id": entity_id,
            **_pick(source, "name", "asset_type", "asset_subtype", "status"),
        }

    return _load


def make_driver_loader_from_repo(driver_repository: Any) -> EntityLoader:
    """Loader resolving a ``driver_id`` via the tenant-scoped driver repository.

    The repository already enforces tenant isolation and returns ``None`` for a
    cross-tenant / missing driver, so we surface ``{driver_name, status}``.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        driver = await driver_repository.get(tenant_id, entity_id)
        if driver is None:
            return None
        summary: Dict[str, Any] = {"driver_id": entity_id}
        for attr in ("driver_name", "status", "availability"):
            value = getattr(driver, attr, None)
            if value is not None:
                summary[attr] = value
        return summary

    return _load


def make_driver_loader_from_es(es_service: Any) -> EntityLoader:
    """Loader resolving a ``driver_id`` from the ``drivers_current`` projection.

    Fallback used when no driver repository is wired.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"term": {"driver_id": entity_id}}}, tenant_id
        )
        query["size"] = 1
        resp = await es_service.search_documents(DRIVERS_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "driver_id": entity_id,
            **_pick(source, "driver_name", "status", "availability"),
        }

    return _load


def make_order_loader_from_repo(order_repository: Any) -> EntityLoader:
    """Loader resolving an ``order_id`` via the tenant-scoped order repository.

    Powers the job resolver read's ``expand=order`` link (cross-module-entity-
    linkage task 3.2, Req 5.2). The repository already enforces tenant isolation
    and returns ``None`` for a cross-tenant / missing order, so a reference that
    crosses tenants surfaces as ``unresolved`` (Req 5.3 / Property 2). Returns a
    small ``{order_id, status, customer_id, customer_name}`` summary.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        order = await order_repository.get(tenant_id, entity_id)
        if order is None:
            return None
        summary: Dict[str, Any] = {"order_id": entity_id}
        for attr in ("status", "customer_id", "customer_name"):
            value = getattr(order, attr, None)
            if value is not None:
                summary[attr] = value
        return summary

    return _load


def make_depot_loader_from_repo(depot_repository: Any) -> EntityLoader:
    """Loader resolving a ``depot_id`` via the tenant-scoped depot repository.

    Powers the resolution of an asset's ``assigned_depot_id`` and the tenant's
    ``default_depot_id`` to a depot record (cross-module-entity-linkage task 9,
    Req 10.1). The repository already enforces tenant isolation (a cross-tenant
    ``get`` returns ``None``), so a reference that crosses tenants surfaces as
    ``unresolved`` (Req 5.3 / Property 2). The summary round-trips ``is_default``
    so callers can render the default-depot affordance without UI-side inference
    (Req 10.3).
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        depot = await depot_repository.get(tenant_id, entity_id)
        if depot is None:
            return None
        summary: Dict[str, Any] = {"depot_id": entity_id}
        for attr in ("name", "status", "address", "is_default"):
            value = getattr(depot, attr, None)
            if value is not None:
                summary[attr] = value
        return summary

    return _load


def make_depot_loader_from_es(es_service: Any) -> EntityLoader:
    """Loader resolving a ``depot_id`` from the ``depots`` projection.

    Fallback used when no depot repository is wired. Returns a small
    ``{depot_id, name, status, is_default}`` summary, re-validating the source's
    ``tenant_id`` before any summary crosses the boundary (Req 5.3).
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"term": {"depot_id": entity_id}}}, tenant_id
        )
        query["size"] = 1
        resp = await es_service.search_documents(DEPOTS_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "depot_id": entity_id,
            **_pick(source, "name", "status", "is_default"),
        }

    return _load


def register_depot_loader(
    resolver: Any,
    *,
    depot_repository: Any = None,
    es_service: Any = None,
) -> None:
    """Register a ``depot`` loader on ``resolver`` (idempotent).

    Prefers the tenant-scoped :class:`fuel.depot_models.DepotRepository` when
    available, falling back to the ``depots`` ES projection otherwise. When
    neither dependency is supplied no loader is registered and ``depot``
    references degrade to ``unresolved`` rather than failing — mirroring the
    :func:`register_order_link_loaders` posture.

    Validates: Requirements 10.1, 5.3, 5.4.
    """
    if depot_repository is not None:
        resolver.register("depot", make_depot_loader_from_repo(depot_repository))
    elif es_service is not None:
        resolver.register("depot", make_depot_loader_from_es(es_service))


def make_invoice_loader(es_service: Any) -> EntityLoader:
    """Loader resolving an ``invoice_id`` to a small display summary.

    Reads the tenant's ``invoices_current`` projection. Powers the payment
    resolver read's ``expand=invoice`` link (cross-module-entity-linkage
    task 12, Req 12.3). Returns ``{invoice_id, status, total_cents,
    customer_id, account_id}``.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"term": {"invoice_id": entity_id}}}, tenant_id
        )
        query["size"] = 1
        resp = await es_service.search_documents(INVOICES_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "invoice_id": entity_id,
            **_pick(
                source,
                "status",
                "total_cents",
                "remaining_cents",
                "customer_id",
                "account_id",
            ),
        }

    return _load


def make_account_loader(es_service: Any) -> EntityLoader:
    """Loader resolving an ``account_id`` to a small display summary.

    Reads the tenant's ``accounts_current`` projection. Powers the payment
    resolver read's ``expand=account`` link (cross-module-entity-linkage
    task 12, Req 12.3). Returns ``{account_id, display_name, status,
    customer_id}``.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"term": {"account_id": entity_id}}}, tenant_id
        )
        query["size"] = 1
        resp = await es_service.search_documents(ACCOUNTS_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "account_id": entity_id,
            **_pick(source, "display_name", "status", "customer_id"),
        }

    return _load


def make_payment_loader(es_service: Any) -> EntityLoader:
    """Loader resolving a canonical ``payment_id`` to a small summary.

    Reads the tenant's ``payments_current`` projection so a reconciliation /
    Stripe-mapping surface can resolve a canonical ``payment_id`` to a
    display summary (cross-module-entity-linkage task 12, Req 12.3). Returns
    ``{payment_id, status, amount_cents, source, invoice_id, account_id}``.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        query = inject_tenant_filter(
            {"query": {"term": {"payment_id": entity_id}}}, tenant_id
        )
        query["size"] = 1
        resp = await es_service.search_documents(PAYMENTS_INDEX, query, 1)
        source = _first_source(resp)
        if source is None or source.get("tenant_id") not in (None, tenant_id):
            return None
        return {
            "payment_id": entity_id,
            **_pick(
                source,
                "status",
                "amount_cents",
                "source",
                "invoice_id",
                "account_id",
            ),
        }

    return _load


def register_billing_link_loaders(
    resolver: Any,
    *,
    es_service: Any = None,
) -> None:
    """Register invoice / account / payment loaders on ``resolver``.

    Powers the billing resolver reads (cross-module-entity-linkage Phase G):
    a payment's ``invoice_id`` / ``account_id`` become resolvable references
    and a canonical ``payment_id`` resolves to a summary (Req 12.1, 12.3,
    12.4). Registration is idempotent — re-registering replaces the prior
    loader — and only happens when ``es_service`` is available, so a
    partially-wired environment degrades to ``unresolved`` rather than
    failing.
    """
    if es_service is not None:
        resolver.register("invoice", make_invoice_loader(es_service))
        resolver.register("account", make_account_loader(es_service))
        resolver.register("payment", make_payment_loader(es_service))


def make_customer_tank_loader_from_repo(customer_tank_repository: Any) -> EntityLoader:
    """Loader resolving a ``customer_tank_id`` via the tenant-scoped repository.

    Powers ``<EntityLink type="tank">`` and any resolver read that expands a
    tank reference (cross-module-entity-linkage task 6, Req 7.3 / 13.1). The
    :class:`fuel.customer_tank_models.CustomerTankRepository` already enforces
    tenant isolation and returns ``None`` for a cross-tenant / missing tank, so
    a reference that crosses tenants surfaces as ``unresolved`` (Req 5.3 /
    Property 2). Returns a small ``{customer_tank_id, customer_id, status,
    fuel_product_code, last_refill_order_id}`` summary.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        tank = await customer_tank_repository.get(tenant_id, entity_id)
        if tank is None:
            return None
        summary: Dict[str, Any] = {"customer_tank_id": entity_id}
        for attr in (
            "customer_id",
            "status",
            "fuel_product_code",
            "last_refill_order_id",
        ):
            value = getattr(tank, attr, None)
            if value is not None:
                summary[attr] = value
        return summary

    return _load


def register_customer_tank_link_loader(
    resolver: Any,
    *,
    customer_tank_repository: Any = None,
) -> None:
    """Register the ``tank`` loader on ``resolver`` when a repo is available.

    Idempotent — re-registering replaces the prior loader. Wired by the
    fuel-ops bootstrap so a tank reference resolves to a summary instead of
    dangling as ``unresolved`` (cross-module-entity-linkage task 6).
    """
    if customer_tank_repository is not None:
        resolver.register(
            "tank", make_customer_tank_loader_from_repo(customer_tank_repository)
        )


def make_terminal_loader_from_repo(terminal_repository: Any) -> EntityLoader:
    """Loader resolving a ``terminal_id`` via the tenant-scoped repository.

    Powers ``<EntityLink type="terminal">`` and any resolver read that expands
    a terminal reference carried by a sourcing recommendation, terminal BOL, or
    wait report (cross-module-entity-linkage task 8, Req 9.1/9.2/13.1). The
    :class:`fuel.terminal_models.TerminalRepository` already enforces tenant
    isolation and returns ``None`` for a cross-tenant / missing terminal, so a
    reference that crosses tenants surfaces as ``unresolved`` (Req 5.3 /
    Property 2). Returns a small ``{terminal_id, name, operator, status}``
    summary — the canonical terminal record is the source of truth for the
    display name (Req 9.2), superseding any free-text ``terminal_name`` snapshot.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        terminal = await terminal_repository.get(tenant_id, entity_id)
        if terminal is None:
            return None
        summary: Dict[str, Any] = {"terminal_id": entity_id}
        for attr in ("name", "operator", "status", "branded", "supplier_brand"):
            value = getattr(terminal, attr, None)
            if value is not None:
                summary[attr] = value
        return summary

    return _load


def make_contract_loader_from_repo(supplier_contract_repository: Any) -> EntityLoader:
    """Loader resolving a ``contract_id`` via the tenant-scoped repository.

    Powers resolution of the ``contract_id`` a sourcing recommendation candidate
    carries (cross-module-entity-linkage task 8, Req 9.1/9.2). The
    :class:`fuel.terminal_models.SupplierContractRepository` already enforces
    tenant isolation and returns ``None`` for a cross-tenant / missing contract,
    so a reference that crosses tenants surfaces as ``unresolved`` (Req 5.3 /
    Property 2). Returns a small ``{contract_id, supplier_name, product_code,
    status}`` summary.
    """

    async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
        contract = await supplier_contract_repository.get(tenant_id, entity_id)
        if contract is None:
            return None
        summary: Dict[str, Any] = {"contract_id": entity_id}
        for attr in ("supplier_name", "product_code", "status"):
            value = getattr(contract, attr, None)
            if value is not None:
                summary[attr] = value
        return summary

    return _load


def register_terminal_link_loaders(
    resolver: Any,
    *,
    terminal_repository: Any = None,
    supplier_contract_repository: Any = None,
) -> None:
    """Register ``terminal`` / ``contract`` loaders on ``resolver``.

    Powers the canonical-terminal / supplier-contract references introduced by
    cross-module-entity-linkage task 8 (Req 9): a sourcing recommendation,
    terminal BOL, or wait report's ``terminal_id`` resolves to a canonical
    terminal summary and a recommendation candidate's ``contract_id`` resolves
    to a supplier-contract summary, instead of dangling as ``unresolved``.

    Only registers a loader when its backing repository is available, so a
    partially-wired environment degrades to ``unresolved`` rather than failing
    (an unregistered type already resolves to ``unresolved``). Registration is
    idempotent — re-registering replaces the prior loader.

    Validates: Requirements 9.1, 9.2, 5.3, 5.4.
    """
    if terminal_repository is not None:
        resolver.register(
            "terminal", make_terminal_loader_from_repo(terminal_repository)
        )
    if supplier_contract_repository is not None:
        resolver.register(
            "contract", make_contract_loader_from_repo(supplier_contract_repository)
        )


def register_order_link_loaders(
    resolver: Any,
    *,
    es_service: Any = None,
    driver_repository: Any = None,
    order_repository: Any = None,
) -> None:
    """Register customer / asset / driver / order loaders on ``resolver``.

    Only registers a loader when the backing dependency is available, so a
    partially-wired environment degrades to ``unresolved`` rather than failing
    (an unregistered type already resolves to ``unresolved``). Registration is
    idempotent — re-registering replaces the prior loader.
    """
    if es_service is not None:
        resolver.register("customer", make_customer_loader(es_service))
        resolver.register("asset", make_asset_loader(es_service))

    if driver_repository is not None:
        resolver.register("driver", make_driver_loader_from_repo(driver_repository))
    elif es_service is not None:
        resolver.register("driver", make_driver_loader_from_es(es_service))

    if order_repository is not None:
        resolver.register("order", make_order_loader_from_repo(order_repository))


__all__ = [
    "make_customer_loader",
    "make_asset_loader",
    "make_driver_loader_from_repo",
    "make_driver_loader_from_es",
    "make_order_loader_from_repo",
    "make_depot_loader_from_repo",
    "make_depot_loader_from_es",
    "make_invoice_loader",
    "make_account_loader",
    "make_payment_loader",
    "make_customer_tank_loader_from_repo",
    "make_terminal_loader_from_repo",
    "make_contract_loader_from_repo",
    "register_depot_loader",
    "register_billing_link_loaders",
    "register_customer_tank_link_loader",
    "register_terminal_link_loaders",
    "register_order_link_loaders",
]
