"""
Property-based test for cross-module-entity-linkage (task 13.1).

Implements the named correctness property from design.md:

* **Property 4 — Resolution totality** (Validates: Requirements 5.4, 1.2)
  Every declared reference id on a returned entity is either resolved to a
  summary or explicitly marked ``unresolved`` (or ``empty`` for an absent id) —
  NEVER silently dropped.

The property is asserted across *every* resolver read in the spec by exercising
the shared ``RefResolver.resolve_many`` / ``ResolvedRef.to_dict()`` contract via
each endpoint's ``_build_*_links`` helper (the single seam every resolver read
builds its ``links`` object from):

* orders        — ``fuel/api/order_endpoints._build_order_links``
* jobs          — ``scheduling/api/endpoints._build_job_links``
* customer-tanks— ``fuel/api/fuel_ops_endpoints._build_customer_tank_links``
* invoices      — ``commerce/api/invoice_endpoints._build_invoice_links``
* accounts      — ``commerce/api/account_endpoints._build_account_links``

plus the reconciliation row, whose cross-module reference ids
(``order_id`` → ``plan_id`` → ``pod_id`` → ``invoice_id`` chain and the
``customer_id`` / ``assigned_asset_id`` / ``assigned_driver_id`` pivots) are
rendered navigable on the client via ``<EntityLink>``; here we assert the
serialized row always carries every declared reference key (present id or an
explicit ``None`` → "unlinked"), never silently dropping one.

The generators sweep each reference id over the three input classes — present
(resolves), dangling (does not resolve in tenant), and absent (no id) — and an
arbitrary subset of ``expand`` tokens, asserting every requested expand key
appears in the output with ``status`` ∈ {resolved, unresolved, empty} and is
never missing.

These reuse the in-memory fake-loader patterns established in
``tests/unit/test_ref_resolver.py`` and the resolver-read patterns in
``tests/unit/test_billing_resolver_reads.py``.

Feature: cross-module-entity-linkage, Property 4

Validates: Requirements 5.4, 1.2
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from hypothesis import given, settings
from hypothesis import strategies as st

import commerce.api.account_endpoints as account_endpoints
import commerce.api.invoice_endpoints as invoice_endpoints
import fuel.api.fuel_ops_endpoints as fuel_ops_endpoints
import fuel.api.order_endpoints as order_endpoints
import scheduling.api.endpoints as scheduling_endpoints
from services.ref_resolver import RefResolver, configure_ref_resolver
from services.reconciliation_service import ReconciliationRecord

TENANT = "tenant-A"

#: The three allowed resolution outcomes. Property 4: a declared reference is
#: always reported in one of these states — never silently dropped.
_ALLOWED_STATUSES = {"resolved", "unresolved", "empty"}

_ID_TOKEN = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_", min_size=1, max_size=10
)
#: An "absent" id: either ``None`` or an empty string. Both must collapse to
#: the ``empty`` resolution state (the reference is simply not set).
_ABSENT = st.sampled_from([None, ""])


# --------------------------------------------------------------------------- #
# In-memory multi-type store + resolver (mirrors test_ref_resolver.py fakes)
# --------------------------------------------------------------------------- #


class _FakeMultiStore:
    """Tenant-scoped store across entity types: {(etype, tenant, id): summary}."""

    def __init__(self) -> None:
        self.data: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def put(self, etype: str, tenant_id: str, entity_id: str) -> None:
        self.data[(etype, tenant_id, entity_id)] = {
            "id": entity_id,
            "display_name": f"{etype}:{entity_id}",
        }

    def loader(self, etype: str):
        async def _load(tenant_id: str, entity_id: str) -> Optional[Dict[str, Any]]:
            return self.data.get((etype, tenant_id, entity_id))

        return _load


def _build_resolver(entity_types: Tuple[str, ...]) -> Tuple[RefResolver, _FakeMultiStore]:
    store = _FakeMultiStore()
    resolver = RefResolver()
    for etype in entity_types:
        resolver.register(etype, store.loader(etype))
    return resolver, store


# --------------------------------------------------------------------------- #
# Reference-plan strategy
# --------------------------------------------------------------------------- #
#
# A "read spec" lists, per expand token, the entity attribute/key carrying the
# id and the entity_type it resolves against:
#   token -> (source_key, entity_type)
#
# The plan strategy assigns each token one of three input classes and the id
# value to use, plus the expected resolution status. ``resolved`` ids are later
# seeded into the store; ``unresolved`` ids are deliberately left absent.


@st.composite
def _ref_plan(draw, read_spec: Dict[str, Tuple[str, str]]):
    """Generate a per-token ``(entity_type, source_key, id, expected_status)``."""
    plan: Dict[str, Tuple[str, str, Optional[str], str]] = {}
    for token, (source_key, etype) in read_spec.items():
        kind = draw(st.sampled_from(["resolved", "unresolved", "empty"]))
        if kind == "empty":
            value = draw(_ABSENT)
            plan[token] = (etype, source_key, value, "empty")
        else:
            # Suffix the token to keep ids unique per field so a "resolved" id
            # for one field can never accidentally satisfy an "unresolved" field
            # of the same entity type.
            value = f"{token}-{draw(_ID_TOKEN)}"
            plan[token] = (etype, source_key, value, kind)
    return plan


def _seed_and_build_entity(
    plan: Dict[str, Tuple[str, str, Optional[str], str]],
    store: _FakeMultiStore,
) -> Dict[str, Any]:
    """Seed ``resolved`` ids into the store; return {source_key: id} for the entity."""
    source_fields: Dict[str, Any] = {}
    for _token, (etype, source_key, value, status) in plan.items():
        source_fields[source_key] = value
        if status == "resolved":
            store.put(etype, TENANT, value)  # type: ignore[arg-type]
    return source_fields


def _assert_totality(
    links: Dict[str, Any],
    plan: Dict[str, Tuple[str, str, Optional[str], str]],
    expand: set,
) -> None:
    """Core Property-4 assertion over a built ``links`` object.

    * Every requested expand token appears as a key (none silently dropped).
    * No unrequested token leaks in.
    * Each link's ``status`` ∈ {resolved, unresolved, empty} and matches the
      input class (present → resolved, dangling → unresolved, absent → empty).
    * Resolved links carry a ``summary``; unresolved/empty carry only
      ``{status, id}``.
    """
    # Totality of keys: exactly the requested tokens, no additions, no drops.
    assert set(links.keys()) == expand

    for token in expand:
        etype, _source_key, value, expected_status = plan[token]
        link = links[token]
        assert link["status"] in _ALLOWED_STATUSES
        assert link["status"] == expected_status
        # The declared id is echoed back (never lost), even when unresolved/empty.
        assert link["id"] == value
        if expected_status == "resolved":
            assert "summary" in link and isinstance(link["summary"], dict)
        else:
            # Explicit marker only — no silently-substituted summary.
            assert "summary" not in link


def _run_build(build_fn, entity: Any, expand: set, resolver: RefResolver) -> Dict[str, Any]:
    """Invoke a ``_build_*_links`` helper with ``resolver`` as the process-wide one."""
    configure_ref_resolver(resolver)
    try:
        return asyncio.run(build_fn(TENANT, entity, expand))
    finally:
        configure_ref_resolver(None)


# --------------------------------------------------------------------------- #
# Per-read specs: token -> (entity source key, entity_type)
# --------------------------------------------------------------------------- #

_ORDER_SPEC = {
    "customer": ("customer_id", "customer"),
    "asset": ("assigned_asset_id", "asset"),
    "driver": ("assigned_driver_id", "driver"),
}
_JOB_SPEC = {
    "order": ("order_id", "order"),
    "customer": ("customer_id", "customer"),
    "asset": ("asset_assigned", "asset"),  # asset link resolves asset_assigned
    "driver": ("driver_id", "driver"),
}
_TANK_SPEC = {
    "customer": ("customer_id", "customer"),
    "last_refill_order": ("last_refill_order_id", "order"),
}
_INVOICE_SPEC = {
    "order": ("order_id", "order"),
    "account": ("account_id", "account"),
    "customer": ("customer_id", "customer"),
}
_ACCOUNT_SPEC = {
    "customer": ("customer_id", "customer"),
}


def _entity_types(spec: Dict[str, Tuple[str, str]]) -> Tuple[str, ...]:
    return tuple({etype for _src, etype in spec.values()})


def _expand_subset_strategy(spec: Dict[str, Tuple[str, str]]):
    return st.sets(st.sampled_from(list(spec.keys())))


# --------------------------------------------------------------------------- #
# Orders — fuel/api/order_endpoints._build_order_links
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(data=st.data())
def test_property_resolution_totality_order(data):
    """Order resolver read never drops a declared customer/asset/driver ref.

    **Validates: Requirements 5.4, 1.2** (Property 4)
    """
    plan = data.draw(_ref_plan(_ORDER_SPEC))
    expand = data.draw(_expand_subset_strategy(_ORDER_SPEC))

    resolver, store = _build_resolver(_entity_types(_ORDER_SPEC))
    fields = _seed_and_build_entity(plan, store)
    order = SimpleNamespace(**fields)

    links = _run_build(order_endpoints._build_order_links, order, expand, resolver)
    _assert_totality(links, plan, expand)


# --------------------------------------------------------------------------- #
# Jobs — scheduling/api/endpoints._build_job_links
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(data=st.data())
def test_property_resolution_totality_job(data):
    """Job resolver read never drops a declared order/customer/asset/driver ref.

    **Validates: Requirements 5.4, 1.2** (Property 4)
    """
    plan = data.draw(_ref_plan(_JOB_SPEC))
    expand = data.draw(_expand_subset_strategy(_JOB_SPEC))

    resolver, store = _build_resolver(_entity_types(_JOB_SPEC))
    fields = _seed_and_build_entity(plan, store)
    job = SimpleNamespace(**fields)

    links = _run_build(scheduling_endpoints._build_job_links, job, expand, resolver)
    _assert_totality(links, plan, expand)


# --------------------------------------------------------------------------- #
# Customer tanks — fuel/api/fuel_ops_endpoints._build_customer_tank_links
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(data=st.data())
def test_property_resolution_totality_customer_tank(data):
    """Customer-tank read never drops a declared customer/refill-order ref.

    **Validates: Requirements 5.4, 1.2** (Property 4)
    """
    plan = data.draw(_ref_plan(_TANK_SPEC))
    expand = data.draw(_expand_subset_strategy(_TANK_SPEC))

    resolver, store = _build_resolver(_entity_types(_TANK_SPEC))
    fields = _seed_and_build_entity(plan, store)
    tank = SimpleNamespace(**fields)

    links = _run_build(
        fuel_ops_endpoints._build_customer_tank_links, tank, expand, resolver
    )
    _assert_totality(links, plan, expand)


# --------------------------------------------------------------------------- #
# Invoices — commerce/api/invoice_endpoints._build_invoice_links
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(data=st.data())
def test_property_resolution_totality_invoice(data):
    """Invoice read never drops a declared order/account/customer ref.

    **Validates: Requirements 5.4, 1.2** (Property 4)
    """
    plan = data.draw(_ref_plan(_INVOICE_SPEC))
    expand = data.draw(_expand_subset_strategy(_INVOICE_SPEC))

    resolver, store = _build_resolver(_entity_types(_INVOICE_SPEC))
    fields = _seed_and_build_entity(plan, store)
    invoice: Dict[str, Any] = dict(fields)  # invoice helper uses dict.get

    links = _run_build(
        invoice_endpoints._build_invoice_links, invoice, expand, resolver
    )
    _assert_totality(links, plan, expand)


# --------------------------------------------------------------------------- #
# Accounts — commerce/api/account_endpoints._build_account_links
# --------------------------------------------------------------------------- #


@settings(max_examples=100)
@given(data=st.data())
def test_property_resolution_totality_account(data):
    """Account read never drops a declared customer ref.

    **Validates: Requirements 5.4, 1.2** (Property 4)
    """
    plan = data.draw(_ref_plan(_ACCOUNT_SPEC))
    expand = data.draw(_expand_subset_strategy(_ACCOUNT_SPEC))

    resolver, store = _build_resolver(_entity_types(_ACCOUNT_SPEC))
    fields = _seed_and_build_entity(plan, store)
    account: Dict[str, Any] = dict(fields)  # account helper uses dict.get

    links = _run_build(
        account_endpoints._build_account_links, account, expand, resolver
    )
    _assert_totality(links, plan, expand)


# --------------------------------------------------------------------------- #
# Reconciliation rows — services/reconciliation_service.ReconciliationRecord
# --------------------------------------------------------------------------- #
#
# Reconciliation rows expose the navigable chain (order_id → plan_id → pod_id →
# invoice_id) plus the order-derived pivots (customer_id / assigned_asset_id /
# assigned_driver_id). The client renders each via <EntityLink>; resolution
# totality here means the serialized row always carries every declared
# reference key — a present id or an explicit ``None`` ("unlinked") — and never
# silently drops one (Req 12.2 / Property 4).

#: The reference fields a reconciliation row declares. ``order_id`` / ``plan_id``
#: / ``pod_id`` are required (always present); the rest are nullable pivots/chain
#: links that must still be *present* in the serialized output when absent.
_RECON_REQUIRED_REFS = ("order_id", "plan_id", "pod_id")
_RECON_OPTIONAL_REFS = (
    "invoice_id",
    "customer_id",
    "assigned_asset_id",
    "assigned_driver_id",
)


@settings(max_examples=100)
@given(
    order_id=_ID_TOKEN,
    plan_id=_ID_TOKEN,
    pod_id=_ID_TOKEN,
    invoice_id=st.one_of(st.none(), _ID_TOKEN),
    customer_id=st.one_of(st.none(), _ID_TOKEN),
    assigned_asset_id=st.one_of(st.none(), _ID_TOKEN),
    assigned_driver_id=st.one_of(st.none(), _ID_TOKEN),
)
def test_property_resolution_totality_reconciliation_row(
    order_id,
    plan_id,
    pod_id,
    invoice_id,
    customer_id,
    assigned_asset_id,
    assigned_driver_id,
):
    """A reconciliation row never drops a declared chain/pivot reference.

    Every declared reference key is present in the serialized row; an absent
    optional reference serializes as ``None`` (rendered "unlinked") rather than
    being omitted (Req 12.2).

    **Validates: Requirements 5.4, 1.2** (Property 4)
    """
    record = ReconciliationRecord(
        reconciliation_id=f"rec-{order_id}",
        tenant_id=TENANT,
        order_id=order_id,
        plan_id=plan_id,
        pod_id=pod_id,
        invoice_id=invoice_id,
        customer_id=customer_id,
        assigned_asset_id=assigned_asset_id,
        assigned_driver_id=assigned_driver_id,
        ordered_gallons=100.0,
        loaded_gallons=100.0,
        delivered_gallons=100.0,
        variance_load_vs_order_pct=0.0,
        variance_delivered_vs_loaded_pct=0.0,
        generated_at=datetime(2026, 5, 11, 10, 0, 0, tzinfo=timezone.utc),
    )

    row = record.model_dump(mode="json")

    expected = {
        "order_id": order_id,
        "plan_id": plan_id,
        "pod_id": pod_id,
        "invoice_id": invoice_id,
        "customer_id": customer_id,
        "assigned_asset_id": assigned_asset_id,
        "assigned_driver_id": assigned_driver_id,
    }
    # Every declared reference key is present in the row (none silently dropped).
    for key in (*_RECON_REQUIRED_REFS, *_RECON_OPTIONAL_REFS):
        assert key in row, f"reconciliation row silently dropped reference {key!r}"
        assert row[key] == expected[key]

    # Required chain coordinates always carry a non-empty id.
    for key in _RECON_REQUIRED_REFS:
        assert isinstance(row[key], str) and row[key]
