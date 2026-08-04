"""
Property-based tests for Surface B order lookup, status/ETA, and delivery
history read endpoints.

# Feature: dinee-voice-integration, Property 17: Order lookup by phone is
# tenant-scoped and most-recent-first
# Feature: dinee-voice-integration, Property 18: Order status and ETA response
# shape
# Feature: dinee-voice-integration, Property 19: Delivery history honors limit

These properties exercise the ``GET /orders/lookup``, ``GET /orders/{id}/status``,
``GET /orders/{id}/eta`` and ``GET /customers/{id}/deliveries`` handlers
implemented in ``fuel/voice/voice_read_driver_router.py`` (task 8.4).

The handlers are driven directly (task 8.4 wires the repository via
``configure_voice_read_driver_router``); a recording in-memory fake
``FuelOrderRepository`` enforces tenant scoping / filtering / sorting the same
way the real ES-backed repository does, so no live Elasticsearch is required.

Property 17 (**Validates: Requirements 16.1, 16.2, 16.3**):

    * order lookup is restricted to the credential-bound tenant — an order in
      another tenant sharing the queried phone is never returned (Req 16.1,
      11.4);
    * matching orders are returned most-recent-first (``created_at`` desc,
      Req 16.2);
    * a blank/absent phone, or a phone matching no orders, yields
      ``{"orders": []}`` with HTTP 200 (Req 16.3).

Property 18 (**Validates: Requirements 17.1, 17.2**):

    * ``status`` responses always carry ``status`` and include ``updatedAt`` /
      ``note`` only when the underlying values are present (Req 17.1);
    * ``eta`` responses always carry ``status`` and include ``etaWindow`` /
      ``etaAt`` only when the delivery window is present (Req 17.2);
    * a cross-tenant / unknown order degrades to a uniform HTTP 404 on both
      endpoints (Req 17.3).

Property 19 (**Validates: Requirements 18.1, 18.2**):

    * delivery history returns only ``delivered`` orders, most-recent-first
      (Req 18.1);
    * the ``limit`` query parameter caps the returned history (Req 18.2).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import from_regex

from errors.exceptions import AppException
from fuel.order_models import FuelOrder
from fuel.voice import voice_read_driver_router as vrouter
from fuel.voice.voice_auth import VoiceTenantContext
from fuel.voice.voice_read_driver_router import (
    _iso,
    configure_voice_read_driver_router,
    customer_deliveries,
    order_eta,
    order_status,
    orders_lookup,
)

_BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# The order statuses drawn for generated orders. ``delivered`` is the terminal
# status the delivery-history endpoint filters on (Req 18.1).
_ORDER_STATUSES = (
    "placed", "confirmed", "scheduled", "dispatched",
    "in_transit", "delivered", "failed", "cancelled",
)

# Call types whose delivery window is optional at intake, so we can generate
# orders both with and without a window (Req 17.2 optional fields).
_WINDOWLESS_CALL_TYPES = ("will_call", "keep_full", "auto_fill")


# ---------------------------------------------------------------------------
# Recording in-memory fake — FuelOrderRepository (search / get)
# ---------------------------------------------------------------------------
class FakeFuelOrderRepository:
    """Tenant-scoped fake mirroring ``FuelOrderRepository.search`` / ``get``.

    Holds a per-tenant list of :class:`FuelOrder` models and only ever returns
    the orders owned by the ``tenant_id`` it is called with, so cross-tenant
    leakage is observable: another tenant's order is simply absent (``search``)
    or ``None`` (``get``). ``search`` applies the same structured filters and
    ``field:order`` sort semantics the real ES-backed repository does.
    """

    def __init__(self, orders_by_tenant: dict[str, list[FuelOrder]]) -> None:
        self.orders_by_tenant = orders_by_tenant
        self.search_calls: list[dict] = []
        self.get_calls: list[tuple[str, str]] = []

    @staticmethod
    def _sorted(orders: list[FuelOrder], sort) -> list[FuelOrder]:
        if not sort:
            return list(orders)
        field, _, order = sort.partition(":")
        reverse = order != "asc"
        return sorted(orders, key=lambda o: getattr(o, field), reverse=reverse)

    async def search(
        self,
        tenant_id: str,
        *,
        customer_phone=None,
        customer_id=None,
        driver_id=None,
        status=None,
        sort=None,
        size=None,
        **kwargs,
    ) -> dict:
        self.search_calls.append(
            {
                "tenant_id": tenant_id,
                "customer_phone": customer_phone,
                "customer_id": customer_id,
                "driver_id": driver_id,
                "sort": sort,
                "size": size,
            }
        )
        orders = list(self.orders_by_tenant.get(tenant_id, []))
        if customer_phone is not None:
            orders = [o for o in orders if o.customer_phone == customer_phone]
        if customer_id is not None:
            orders = [o for o in orders if o.customer_id == customer_id]
        if driver_id is not None:
            orders = [o for o in orders if o.assigned_driver_id == driver_id]
        if status is not None:
            orders = [o for o in orders if o.status == status]
        orders = self._sorted(orders, sort)
        if size is not None:
            orders = orders[:size]
        return {
            "orders": orders,
            "total": len(orders),
            "page": 1,
            "size": size if size is not None else len(orders),
        }

    async def get(self, tenant_id: str, order_id: str):
        self.get_calls.append((tenant_id, order_id))
        for order in self.orders_by_tenant.get(tenant_id, []):
            if order.order_id == order_id:
                return order
        return None


def _run(coro):
    return asyncio.run(coro)


def _ctx(tenant_id: str) -> VoiceTenantContext:
    return VoiceTenantContext(tenant_id=tenant_id, channel_id=f"chan-{tenant_id}")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_tenant_ids = from_regex(r"tenant-[a-z0-9]{6,12}", fullmatch=True)
_customer_ids = from_regex(r"cust-[a-z0-9]{6,12}", fullmatch=True)
_notes = from_regex(r"[A-Za-z][A-Za-z0-9 ]{1,30}", fullmatch=True)
_PHONE_POOL = ("+15550000001", "+15550000002", "+15550000003")


def _build_order(
    *,
    order_id: str,
    tenant_id: str,
    customer_id: str,
    customer_phone,
    status: str,
    created_at: datetime,
    special_instructions,
    include_window: bool,
    call_type: str,
) -> FuelOrder:
    """Construct a valid :class:`FuelOrder` for the fake repository."""
    kwargs: dict = {
        "order_id": order_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "customer_name": "Test Customer",
        "customer_phone": customer_phone,
        "ship_to_address": "123 Main St",
        "ship_to_lat": 30.0,
        "ship_to_lon": -90.0,
        "product_code": "DIESEL_2",
        "gallons_requested": 500.0,
        "fill_to_full": False,
        "call_type": call_type,
        "special_instructions": special_instructions,
        "intake_channel": "dispatcher",
        "intake_channel_id": "dispatcher-default",
        "status": status,
        "source_schema_version": "1.0",
        "trace_id": "trace-001",
        "created_at": created_at,
        "updated_at": created_at,
        "last_event_timestamp": created_at,
    }
    if include_window:
        kwargs["delivery_window_start"] = created_at + timedelta(hours=1)
        kwargs["delivery_window_end"] = created_at + timedelta(hours=3)
    if status == "on_hold":  # never generated, but keep the model valid
        kwargs["hold_reason"] = "voice_review_required"
    return FuelOrder(**kwargs)


@st.composite
def _orders(draw, tenant_id: str, *, customer_id=None, min_size=1, max_size=6):
    """Generate a list of distinct-``created_at`` orders for one tenant.

    Each order gets a deterministic, tenant-unique ``order_id`` and a distinct
    ``created_at`` (so most-recent-first ordering is unambiguous). ``phone``,
    ``status``, ``special_instructions`` and the delivery window are drawn so
    the projection's "include when present" behaviour is exercised.
    """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    minutes = draw(
        st.lists(st.integers(min_value=0, max_value=1_000_000),
                 min_size=n, max_size=n, unique=True)
    )
    orders: list[FuelOrder] = []
    for i in range(n):
        cust = customer_id if customer_id is not None else draw(_customer_ids)
        orders.append(
            _build_order(
                order_id=f"ord-{tenant_id}-{i}",
                tenant_id=tenant_id,
                customer_id=cust,
                customer_phone=draw(st.one_of(st.none(), st.sampled_from(_PHONE_POOL))),
                status=draw(st.sampled_from(_ORDER_STATUSES)),
                created_at=_BASE + timedelta(minutes=minutes[i]),
                special_instructions=draw(st.one_of(st.none(), _notes)),
                include_window=draw(st.booleans()),
                call_type=draw(st.sampled_from(_WINDOWLESS_CALL_TYPES)),
            )
        )
    return orders


# ===========================================================================
# Property 17 — Order lookup by phone is tenant-scoped and most-recent-first
# ===========================================================================
class TestOrderLookupByPhone:
    """# Feature: dinee-voice-integration, Property 17: Order lookup by phone
    is tenant-scoped and most-recent-first

    **Validates: Requirements 16.1, 16.2, 16.3**
    """

    @given(
        bound_tenant=_tenant_ids,
        other_tenant=_tenant_ids,
        orders=st.deferred(lambda: _orders_any_tenant()),
        query_phone=st.sampled_from(_PHONE_POOL),
    )
    @settings(max_examples=100)
    def test_lookup_is_tenant_scoped_and_recent_first(
        self, bound_tenant, other_tenant, orders, query_phone
    ):
        assume(other_tenant != bound_tenant)

        async def scenario():
            bound_orders = [
                _rebind(o, bound_tenant, f"bound-{i}")
                for i, o in enumerate(orders)
            ]
            # A cross-tenant order sharing the queried phone must never surface.
            poison = _build_order(
                order_id="ord-poison",
                tenant_id=other_tenant,
                customer_id="cust-other",
                customer_phone=query_phone,
                status="placed",
                created_at=_BASE + timedelta(minutes=9_000_000),
                special_instructions=None,
                include_window=False,
                call_type="will_call",
            )
            repo = FakeFuelOrderRepository(
                {bound_tenant: bound_orders, other_tenant: [poison]}
            )
            configure_voice_read_driver_router(fuel_order_repository=repo)

            resp = await orders_lookup(phone=query_phone, voice=_ctx(bound_tenant))
            returned_ids = [o.id for o in resp.orders]

            expected = [
                o for o in bound_orders if o.customer_phone == query_phone
            ]
            expected_ids = [
                o.order_id
                for o in sorted(expected, key=lambda o: o.created_at, reverse=True)
            ]
            # Exactly the bound-tenant matches, most-recent-first (Req 16.1/16.2).
            assert returned_ids == expected_ids
            # The cross-tenant order is never leaked (Req 16.1, 11.4).
            assert "ord-poison" not in returned_ids
            # The repository was queried with the credential-bound tenant and a
            # descending created_at sort (Req 16.2).
            assert repo.search_calls[-1]["tenant_id"] == bound_tenant
            assert repo.search_calls[-1]["sort"] == "created_at:desc"

        _run(scenario())

    @given(
        bound_tenant=_tenant_ids,
        orders=st.deferred(lambda: _orders_any_tenant()),
        blank_phone=st.sampled_from([None, "", "   "]),
    )
    @settings(max_examples=100)
    def test_blank_phone_returns_empty_without_query(
        self, bound_tenant, orders, blank_phone
    ):
        async def scenario():
            bound_orders = [
                _rebind(o, bound_tenant, f"bound-{i}") for i, o in enumerate(orders)
            ]
            repo = FakeFuelOrderRepository({bound_tenant: bound_orders})
            configure_voice_read_driver_router(fuel_order_repository=repo)

            resp = await orders_lookup(phone=blank_phone, voice=_ctx(bound_tenant))
            assert resp.model_dump() == {"orders": []}
            # A blank phone short-circuits before ever touching the repository.
            assert repo.search_calls == []

        _run(scenario())


# ===========================================================================
# Property 18 — Order status and ETA response shape
# ===========================================================================
class TestOrderStatusAndEtaShape:
    """# Feature: dinee-voice-integration, Property 18: Order status and ETA
    response shape

    **Validates: Requirements 17.1, 17.2**
    """

    @given(
        bound_tenant=_tenant_ids,
        other_tenant=_tenant_ids,
        orders=st.deferred(lambda: _orders_any_tenant()),
    )
    @settings(max_examples=100)
    def test_status_and_eta_shape_and_cross_tenant_404(
        self, bound_tenant, other_tenant, orders
    ):
        assume(other_tenant != bound_tenant)

        async def scenario():
            bound_orders = [
                _rebind(o, bound_tenant, f"bound-{i}") for i, o in enumerate(orders)
            ]
            other_orders = [
                _rebind(o, other_tenant, f"other-{i}") for i, o in enumerate(orders)
            ]
            repo = FakeFuelOrderRepository(
                {bound_tenant: bound_orders, other_tenant: other_orders}
            )
            configure_voice_read_driver_router(fuel_order_repository=repo)

            for order in bound_orders:
                status_resp = await order_status(
                    order_id=order.order_id, voice=_ctx(bound_tenant)
                )
                # status always present (Req 17.1).
                assert status_resp["status"] == order.status
                # updatedAt present when set (updated_at is always set here).
                assert status_resp["updatedAt"] == _iso(order.updated_at)
                # note only when special_instructions is present (Req 17.1).
                if order.special_instructions:
                    assert status_resp["note"] == order.special_instructions
                else:
                    assert "note" not in status_resp

                eta_resp = await order_eta(
                    order_id=order.order_id, voice=_ctx(bound_tenant)
                )
                # status always present (Req 17.2).
                assert eta_resp["status"] == order.status
                start = _iso(order.delivery_window_start)
                end = _iso(order.delivery_window_end)
                if start and end:
                    assert eta_resp["etaWindow"] == f"{start}/{end}"
                else:
                    assert "etaWindow" not in eta_resp
                if start:
                    assert eta_resp["etaAt"] == start
                else:
                    assert "etaAt" not in eta_resp

            # Cross-tenant / unknown orders degrade to a uniform 404 (Req 17.3).
            for order in other_orders:
                with pytest.raises(AppException) as status_exc:
                    await order_status(
                        order_id=order.order_id, voice=_ctx(bound_tenant)
                    )
                assert status_exc.value.status_code == 404
                with pytest.raises(AppException) as eta_exc:
                    await order_eta(
                        order_id=order.order_id, voice=_ctx(bound_tenant)
                    )
                assert eta_exc.value.status_code == 404

        _run(scenario())


# ===========================================================================
# Property 19 — Delivery history honors limit
# ===========================================================================
class TestDeliveryHistoryHonorsLimit:
    """# Feature: dinee-voice-integration, Property 19: Delivery history honors
    limit

    **Validates: Requirements 18.1, 18.2**
    """

    @given(
        bound_tenant=_tenant_ids,
        customer_id=_customer_ids,
        orders=st.deferred(lambda: _orders_for_customer()),
        limit=st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
    )
    @settings(max_examples=100)
    def test_deliveries_are_delivered_only_recent_first_and_capped(
        self, bound_tenant, customer_id, orders, limit
    ):
        async def scenario():
            # Rebind every generated order to this tenant + customer so the
            # endpoint has at least one order (avoiding the unknown-customer 404
            # path, which is Property 14's concern) and can filter by status.
            bound_orders = [
                _rebind(o, bound_tenant, f"bound-{i}", customer_id=customer_id)
                for i, o in enumerate(orders)
            ]
            repo = FakeFuelOrderRepository({bound_tenant: bound_orders})
            configure_voice_read_driver_router(fuel_order_repository=repo)

            resp = await customer_deliveries(
                customer_id=customer_id, limit=limit, voice=_ctx(bound_tenant)
            )
            returned = resp.deliveries
            returned_ids = [d["id"] for d in returned]

            delivered_sorted = [
                o.order_id
                for o in sorted(
                    (o for o in bound_orders if o.status == "delivered"),
                    key=lambda o: o.created_at,
                    reverse=True,
                )
            ]
            expected = (
                delivered_sorted if limit is None else delivered_sorted[:limit]
            )
            # Only delivered orders, most-recent-first, capped at limit
            # (Req 18.1/18.2).
            assert returned_ids == expected
            assert all(d["status"] == "delivered" for d in returned)
            if limit is not None:
                assert len(returned) <= limit

        _run(scenario())


# ---------------------------------------------------------------------------
# Helpers for rebinding generated orders onto specific tenants/customers
# ---------------------------------------------------------------------------
@st.composite
def _orders_any_tenant(draw):
    """Generate orders under a throwaway tenant, later rebound per-test."""
    return draw(_orders("gen"))


@st.composite
def _orders_for_customer(draw):
    """Generate orders that all share one throwaway customer id."""
    return draw(_orders("gen", customer_id="cust-shared0"))


def _rebind(order: FuelOrder, tenant_id: str, suffix: str, *, customer_id=None) -> FuelOrder:
    """Return a copy of ``order`` bound to a new tenant / order id / customer."""
    data = order.model_dump()
    data["tenant_id"] = tenant_id
    data["order_id"] = f"ord-{tenant_id}-{suffix}"
    if customer_id is not None:
        data["customer_id"] = customer_id
    return FuelOrder(**data)
