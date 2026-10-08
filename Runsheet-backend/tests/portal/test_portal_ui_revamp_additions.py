"""Portal API additions for the UI revamp (escalations PE1–PE5, PE7).

Each addition keeps the per-customer and per-tenant scoping exactly as it
was: customer A never sees customer B's (same tenant) or customer C's (other
tenant) data through any new field or filter, and no new field carries cost,
margin, driver data or internal ids.

[real-auth] ``main.app`` with the in-memory harnesses from the portal conftest.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.portal.conftest import (
    CUSTOMER_A,
    CUSTOMER_B,
    CUSTOMER_C,
    T1,
    T2,
    FakeCustomerService,
    FakePortalDB,
    FakeSuperTokens,
    FakeTelemetry,
    call,
    make_access_service,
)


class FakeRedis:
    """The two calls ``TenantSettingsService`` makes."""

    def __init__(self) -> None:
        self.data: dict = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):  # noqa: ARG002
        self.data[key] = value


@pytest.fixture
def tenant_names(cA):  # noqa: ARG001 — after the session fixture resets the guard
    """A tenant settings service where T1 has a display name and T2 doesn't."""
    from ops.middleware import tenant_guard
    from services.tenant_settings import TenantSettingsService

    redis = FakeRedis()
    service = TenantSettingsService(redis_client=redis)
    redis.data[f"tenant:{T1}:settings"] = json.dumps(
        {"region": "US", "measurement_units": {"volume": "gal", "distance": "mi"},
         "display_name": "QA Demo Fuels"}
    )
    tenant_guard.configure_tenant_guard(service)
    try:
        yield service
    finally:
        tenant_guard.configure_tenant_guard(None)


# ---------------------------------------------------------------------------
# PE1: supplier display name
# ---------------------------------------------------------------------------


def test_me_supplier_name_is_the_tenant_display_name(client, portal_on, tenant_names, cA):
    resp = call(client, "GET", "/api/portal/me", cA)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["supplier_name"] == "QA Demo Fuels"


def test_me_supplier_name_falls_back_to_the_tenant_id(client, portal_on, tenant_names, cC):
    resp = call(client, "GET", "/api/portal/me", cC)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["supplier_name"] == T2


async def test_tenant_settings_display_name_round_trip_and_survives_other_writes():
    from services.tenant_settings import TenantSettingsService

    service = TenantSettingsService(redis_client=FakeRedis())
    assert await service.get_display_name(T1) is None
    await service.set_display_name(T1, "  QA   Demo Fuels ")
    assert await service.get_display_name(T1) == "QA Demo Fuels"
    await service.set_region(T1, "US")
    await service.set_default_depot_id(T1, "QA-DEPOT-1")
    assert await service.get_display_name(T1) == "QA Demo Fuels"
    await service.set_display_name(T1, None)
    assert await service.get_display_name(T1) is None
    # Unset names stay out of the stored payload (existing shape unchanged).
    assert "display_name" not in (await service.get(T1)).to_dict()


# ---------------------------------------------------------------------------
# PE4: open balance in /me
# ---------------------------------------------------------------------------


def test_me_open_balance_counts_only_this_customer(client, portal_on, portal_fakes, cA, cB):
    h = portal_fakes.invoices
    # A: two open-ish invoices (one overdue) and one paid; B and C get more.
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A1", status="open", remaining_cents=10_000)
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A2", status="overdue", remaining_cents=2_500)
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A3", status="paid", remaining_cents=0)
    h.add_invoice(T1, CUSTOMER_A, "QA-INV-A4", status="draft", remaining_cents=99_999)
    h.add_invoice(T1, CUSTOMER_B, "QA-INV-B1", status="open", remaining_cents=777_777)
    h.add_invoice(T2, CUSTOMER_C, "QA-INV-C1", status="overdue", remaining_cents=555_555)

    a = call(client, "GET", "/api/portal/me", cA).json()["data"]
    assert a["open_balance_cents"] == 12_500
    assert a["open_invoice_count"] == 2
    assert a["overdue_count"] == 1

    b = call(client, "GET", "/api/portal/me", cB).json()["data"]
    assert b["open_balance_cents"] == 777_777
    assert b["open_invoice_count"] == 1
    assert b["overdue_count"] == 0


def test_me_open_balance_is_null_while_invoicing_is_off(client, portal_on, portal_fakes, cA):
    import portal.api.me_endpoints as me

    me.configure_portal_me(invoice_service=None)
    data = call(client, "GET", "/api/portal/me", cA).json()["data"]
    assert data["invoices_available"] is False
    assert data["open_balance_cents"] is None
    assert data["open_invoice_count"] is None
    assert data["overdue_count"] is None


# ---------------------------------------------------------------------------
# PE7: status_group on the orders list
# ---------------------------------------------------------------------------


def _seed_orders(h):
    h.add_tank(T1, CUSTOMER_A, "QA-TANK-A1")
    h.add_tank(T1, CUSTOMER_B, "QA-TANK-B1")
    statuses = ["placed", "confirmed", "in_transit", "delivered", "failed", "cancelled"]
    for i, status in enumerate(statuses):
        h.add_order(T1, CUSTOMER_A, f"QA-ORD-A-{status}", customer_tank_id="QA-TANK-A1",
                    status=status, created_at=h.now - timedelta(hours=i + 1))
        h.add_order(T1, CUSTOMER_B, f"QA-ORD-B-{status}", customer_tank_id="QA-TANK-B1",
                    status=status, created_at=h.now - timedelta(hours=i + 1))


def test_orders_status_group_active_and_past(client, portal_on, portal_orders, cA):
    _seed_orders(portal_orders)
    active = call(client, "GET", "/api/portal/orders?status_group=active", cA)
    assert active.status_code == 200, active.text
    ids = {o["order_id"] for o in active.json()["data"]}
    assert ids == {"QA-ORD-A-placed", "QA-ORD-A-confirmed", "QA-ORD-A-in_transit"}

    past = call(client, "GET", "/api/portal/orders?status_group=past", cA).json()["data"]
    assert {o["order_id"] for o in past} == {
        "QA-ORD-A-delivered", "QA-ORD-A-failed", "QA-ORD-A-cancelled",
    }
    assert {o["status_code"] for o in past} == {"delivered", "not_delivered", "cancelled"}

    everything = call(client, "GET", "/api/portal/orders", cA).json()["data"]
    assert len(everything) == 6
    assert all(o["order_id"].startswith("QA-ORD-A-") for o in everything)


def test_orders_status_group_never_crosses_customers(client, portal_on, portal_orders, cA, cB):
    _seed_orders(portal_orders)
    for session, own in ((cA, "QA-ORD-A-"), (cB, "QA-ORD-B-")):
        for group in ("active", "past"):
            data = call(client, "GET", f"/api/portal/orders?status_group={group}", session)
            assert data.status_code == 200
            assert all(o["order_id"].startswith(own) for o in data.json()["data"])


def test_orders_status_group_rejects_unknown_values(client, portal_on, portal_orders, cA):
    resp = call(client, "GET", "/api/portal/orders?status_group=everything", cA)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PE2: tank display name and service address
# ---------------------------------------------------------------------------


def test_tank_display_name_and_service_address(client, portal_on, portal_orders, cA, cB):
    h = portal_orders
    doc = h.add_tank(T1, CUSTOMER_A, "QA-TANK-A1")
    doc.update(display_name="  Farm   off-road ", service_address="12 QA Farm Lane, Springfield")
    h.store.seed("customer_tanks", "QA-TANK-A1", doc)
    other = h.add_tank(T1, CUSTOMER_B, "QA-TANK-B1")
    other.update(display_name="QA B secret tank", service_address="9 Secret Way")
    h.store.seed("customer_tanks", "QA-TANK-B1", other)
    h.add_order(T1, CUSTOMER_A, "QA-ORD-A1", customer_tank_id="QA-TANK-A1", status="confirmed")

    tanks = call(client, "GET", "/api/portal/tanks", cA)
    assert tanks.status_code == 200, tanks.text
    (tank,) = tanks.json()["data"]
    assert tank["display_name"] == "Farm off-road"
    assert tank["label"] == "Farm off-road"
    assert tank["service_address"] == "12 QA Farm Lane, Springfield"
    assert tank["next_delivery"]["status_code"] == "confirmed"
    assert "QA B secret tank" not in tanks.text and "9 Secret Way" not in tanks.text

    detail = call(client, "GET", "/api/portal/tanks/QA-TANK-A1", cA).json()["data"]
    assert detail["service_address"] == "12 QA Farm Lane, Springfield"
    # The order's tank reference uses the same name.
    order = call(client, "GET", "/api/portal/orders/QA-ORD-A1", cA).json()["data"]
    assert order["tank"]["label"] == "Farm off-road"

    # B's tank stays invisible to A, like any other id.
    assert call(client, "GET", "/api/portal/tanks/QA-TANK-B1", cA).status_code == 404


def test_tank_without_a_name_keeps_the_existing_label(client, portal_on, portal_orders, cA):
    portal_orders.add_tank(T1, CUSTOMER_A, "QA-TANK-A1", external_tank_id="North yard")
    (tank,) = call(client, "GET", "/api/portal/tanks", cA).json()["data"]
    assert tank["label"] == "North yard"
    assert tank["display_name"] is None and tank["service_address"] is None


def test_staff_tank_models_accept_and_bound_the_new_fields():
    from pydantic import ValidationError

    from fuel.api.fuel_ops_endpoints import CustomerTankUpdateRequest

    ok = CustomerTankUpdateRequest(display_name="Farm off-road", service_address="12 QA Lane")
    assert ok.model_dump(exclude_none=True) == {
        "display_name": "Farm off-road", "service_address": "12 QA Lane",
    }
    with pytest.raises(ValidationError):
        CustomerTankUpdateRequest(display_name="x" * 81)
    with pytest.raises(ValidationError):
        CustomerTankUpdateRequest(service_address="x" * 201)


# ---------------------------------------------------------------------------
# PE3: unit price at stored precision
# ---------------------------------------------------------------------------


def test_line_item_unit_price_at_stored_precision(client, portal_on, portal_fakes, cA):
    portal_fakes.invoices.add_invoice(T1, CUSTOMER_A, "QA-INV-P1")
    data = call(client, "GET", "/api/portal/invoices/QA-INV-P1", cA).json()["data"]
    (line,) = data["line_items"]
    # 100 gal × $2.966 = $296.60, which the cents value ($2.97) can't show.
    assert line["unit_price_dollars"] == "2.966"
    assert line["unit_price_cents"] == 297
    assert "unit_price_micros" not in line


@pytest.mark.parametrize(
    ("micros", "text"),
    [(2_966_000, "2.966"), (2_910_000, "2.91"), (3_000_000, "3.00"), (2_919_345, "2.919345")],
)
def test_unit_price_dollars_text(micros, text):
    from portal.services.projection import unit_price_dollars_text

    assert unit_price_dollars_text(micros) == text


# ---------------------------------------------------------------------------
# PE5: invite flag and invite email
# ---------------------------------------------------------------------------


def test_with_invite_flag_is_idempotent_and_keeps_the_token():
    from portal.services.invite_email import with_invite_flag

    link = "https://app.example.test/auth/reset-password?token=abc&tenantId=public"
    once = with_invite_flag(link)
    assert once == "https://app.example.test/auth/reset-password?token=abc&tenantId=public&invite=1"
    assert with_invite_flag(once) == once


def test_invite_email_template_names_supplier_and_link_only():
    from portal.services.invite_email import render_invite_email

    mail = render_invite_email(
        supplier_name="QA Demo Fuels", customer_name="QA-PORTAL Customer A",
        link="https://app.example.test/auth/reset-password?token=t&invite=1",
    )
    assert mail.subject == "QA Demo Fuels invited you to their customer portal"
    assert "QA-PORTAL Customer A" in mail.text
    assert "https://app.example.test/auth/reset-password?token=t&invite=1" in mail.text
    assert "reset" not in mail.subject.lower()


class InviteEnv:
    def __init__(self, sender) -> None:
        from auth.test_auth import issue_test_context
        from portal.services.portal_access_service import PortalAccessService

        self.customers = FakeCustomerService()
        for tenant_id, customer_id in ((T1, CUSTOMER_A), (T1, CUSTOMER_B), (T2, CUSTOMER_C)):
            self.customers.add(tenant_id, customer_id)
        self.db = FakePortalDB()
        self.st = FakeSuperTokens(self.db)
        base = make_access_service(self.customers, self.db, self.st, FakeTelemetry())
        self.service = PortalAccessService(
            customer_service=self.customers,
            uow_factory=self.db.uow,
            supertokens_admin=self.st,
            session_revoker=self.st.revoke_sessions,
            user_deleter=self.st.delete_user,
            role_creator=self.st.create_role,
            link_minter=self.st.mint_link,
            reset_emailer=self.st.send_reset_email,
            telemetry_service=base._telemetry,
            invite_sender=sender,
        )
        self.admin = issue_test_context(T1, roles=("admin",), user_id="admin-t1")


async def test_invite_sends_the_invite_template_instead_of_the_reset_email():
    sent = []

    async def sender(email, content):
        sent.append((email, content))
        return True

    env = InviteEnv(sender)
    result = await env.service.invite(env.admin, CUSTOMER_A, "buyer@example.test")
    assert result.email_sent is True
    assert result.password_set_link.endswith("?invite=1")
    ((email, content),) = sent
    assert email == "buyer@example.test"
    assert result.password_set_link in content.text
    assert not any(c[0] == "send_reset_email" for c in env.st.calls)


async def test_invite_falls_back_to_the_reset_email_without_an_email_channel():
    async def sender(email, content):  # noqa: ARG001
        return False

    env = InviteEnv(sender)
    result = await env.service.invite(env.admin, CUSTOMER_A, "buyer@example.test")
    uid = env.db.user("buyer@example.test")["st_user_id"]
    assert result.email_sent is True
    assert ("send_reset_email", uid) in env.st.calls
    assert result.password_set_link.endswith("?invite=1")


async def test_resend_to_a_user_who_has_signed_in_is_a_plain_reset():
    sent = []

    async def sender(email, content):
        sent.append(email)
        return True

    env = InviteEnv(sender)
    grant = await env.service.invite(env.admin, CUSTOMER_A, "buyer@example.test")
    sent.clear()
    await env.db.mark_first_seen(grant_id=grant.grant_id)
    again = await env.service.resend(env.admin, CUSTOMER_A, grant.grant_id)
    assert "invite=1" not in (again.password_set_link or "")
    assert sent == []


async def test_default_sender_sends_nothing_without_email_credentials(monkeypatch):
    from portal.services.invite_email import InviteEmail, send_invite_email

    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    assert await send_invite_email("qa@example.test", InviteEmail("s", "t")) is False


def test_new_fields_carry_no_restricted_data(client, portal_on, portal_orders, portal_fakes, cA):
    """No PE field reintroduces cost, margin, driver data or internal ids."""
    portal_orders.add_tank(T1, CUSTOMER_A, "QA-TANK-A1")
    portal_fakes.invoices.add_invoice(T1, CUSTOMER_A, "QA-INV-R1")
    banned = {"cost", "margin", "cost_cents", "margin_cents", "driver_id",
              "assigned_driver_id", "unit_price_micros", "customer_id", "tenant_id"}

    def keys(node, out):
        if isinstance(node, dict):
            for k, v in node.items():
                out.add(k)
                keys(v, out)
        elif isinstance(node, list):
            for v in node:
                keys(v, out)
        return out

    for path in ("/api/portal/me", "/api/portal/tanks", "/api/portal/orders?status_group=active",
                 "/api/portal/invoices/QA-INV-R1"):
        resp = call(client, "GET", path, cA)
        assert resp.status_code == 200, (path, resp.text)
        assert keys(resp.json(), set()) & banned == set(), path
