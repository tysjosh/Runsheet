"""Portal fixes 2026-10-09: online ordering decoupled from the intake rollout
flag, the tenant portal ordering setting, request notifications, the portal
email templates and SendGrid delivery (portal-fixes B2, C1-C3), the supplier
display-name seed (A1).

[real-auth] for the HTTP tests (``main.app`` with fake session verifiers and
the in-memory :class:`OrderHarness`); the rest are unit tests.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from fuel.order_models import PORTAL_REVIEW_HOLD_REASON
from tests.portal.conftest import CUSTOMER_A, T1, T2, call
from tests.portal.test_order_release_hold_portal import _release, staff_orders  # noqa: F401
from tests.portal.test_portal_orders import TANK, _SettingsRedis, order_body, portal_ordering  # noqa: F401

FAKE_KEY = "SG.fake-key-for-tests-only"


# ---------------------------------------------------------------------------
# Tenant settings: portal ordering switch and display-name seed
# ---------------------------------------------------------------------------


async def test_portal_ordering_defaults_on_and_is_per_tenant():
    from services.tenant_settings import TenantSettingsService

    redis = _SettingsRedis()
    service = TenantSettingsService(redis_client=redis)
    assert await service.get_portal_ordering_enabled(T1) is True
    await service.set_portal_ordering_enabled(T1, False)
    assert redis.data == {f"tenant:{T1}:portal_ordering": "disabled"}
    assert await service.get_portal_ordering_enabled(T1) is False
    assert await service.get_portal_ordering_enabled(T2) is True
    await service.set_portal_ordering_enabled(T1, True)
    assert redis.data == {}
    assert await service.get_portal_ordering_enabled(T1) is True
    with pytest.raises(ValueError):
        await service.set_portal_ordering_enabled(T1, "no")  # type: ignore[arg-type]


async def test_portal_ordering_read_failure_fails_closed_and_no_store_is_default_on():
    from services.tenant_settings import TenantSettingsService

    class Broken(_SettingsRedis):
        async def get(self, key):
            raise ConnectionError("redis down")

    assert await TenantSettingsService(redis_client=Broken()).get_portal_ordering_enabled(T1) is False
    assert await TenantSettingsService(redis_client=None).get_portal_ordering_enabled(T1) is True


async def test_display_name_seed_never_overwrites():
    from services.tenant_settings import TenantSettingsService

    redis = _SettingsRedis()
    service = TenantSettingsService(redis_client=redis)
    assert await service.seed_display_name(T1, "  Demo   Fuels ") is True
    assert await service.get_display_name(T1) == "Demo Fuels"
    await service.set_display_name(T1, "Operator Name")
    assert await service.seed_display_name(T1, "Demo Fuels") is False
    assert await service.get_display_name(T1) == "Operator Name"
    assert await service.seed_display_name(T1, "   ") is False


# ---------------------------------------------------------------------------
# Staff portal settings API
# ---------------------------------------------------------------------------


def test_portal_settings_api_admin_sets_dispatcher_reads(client, sessions, portal_on, portal_ordering, cA):
    admin = sessions.staff("admin")
    dispatcher = sessions.staff("dispatcher")
    resp = call(client, "GET", "/api/commerce/portal-settings", dispatcher)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ordering_enabled": True}

    denied = call(client, "PUT", "/api/commerce/portal-settings", dispatcher,
                  json={"ordering_enabled": False})
    assert denied.status_code == 403

    resp = call(client, "PUT", "/api/commerce/portal-settings", admin, json={"ordering_enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ordering_enabled": False}
    assert call(client, "GET", "/api/commerce/portal-settings", dispatcher).json() == {
        "ordering_enabled": False
    }
    bad = call(client, "PUT", "/api/commerce/portal-settings", admin,
               json={"ordering_enabled": False, "tenant_id": T2})
    assert bad.status_code == 422


def test_portal_settings_api_refuses_customers_and_needs_the_portal(
    client, sessions, set_flags, portal_ordering, cA
):
    set_flags()
    assert call(client, "GET", "/api/commerce/portal-settings", cA).status_code in (403, 404)
    assert call(client, "PUT", "/api/commerce/portal-settings", cA,
                json={"ordering_enabled": False}).status_code in (403, 404)
    set_flags(portal=False)
    resp = call(client, "GET", "/api/commerce/portal-settings", sessions.staff("admin"))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Notifications: dispatchers and the requester
# ---------------------------------------------------------------------------


class FakeActivity:
    def __init__(self):
        self.entries = []

    async def log(self, entry):
        self.entries.append(dict(entry))
        return "log-1"


class Outbox:
    def __init__(self):
        self.sent = []

    async def __call__(self, recipient, mail):
        self.sent.append((recipient, mail))
        return True


@pytest.fixture
def notified(portal_on, staff_orders, cA, monkeypatch):  # noqa: F811
    """A configured notifier with a fake activity log and outbox; SendGrid
    'configured' with a fake key (nothing leaves the process)."""
    from portal.services import order_notifications as on

    staff_orders.add_tank(T1, CUSTOMER_A, TANK)
    monkeypatch.setenv("SENDGRID_API_KEY", FAKE_KEY)
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "no-reply@example.test")
    activity, outbox = FakeActivity(), Outbox()
    emails = {cA.user_id: "buyer-a@example.test"}

    async def email_for(user_id):
        return emails.get(user_id, "")

    notifier = on.PortalOrderNotifier(
        activity_log=activity,
        order_repository=staff_orders.order_repo,
        tank_repository=None,
        email_for_user=email_for,
        sender=outbox,
    )
    saved = on.get_portal_order_notifier()
    on.configure_portal_order_notifier(notifier)
    try:
        yield SimpleNamespace(h=staff_orders, activity=activity, outbox=outbox)
    finally:
        on.configure_portal_order_notifier(saved)


def _submit(client, session, **kw):
    resp = call(client, "POST", "/api/portal/orders", session, json=order_body(**kw))
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["order_id"]


def test_new_request_notifies_dispatchers_and_emails_the_requester(client, notified, cA):
    oid = _submit(client, cA, po_number="PO-42")
    (entry,) = notified.activity.entries
    assert entry["tenant_id"] == T1
    assert entry["action_type"] == "New delivery request"
    assert "Customer A" in entry["tool_name"] and "100 gal" in entry["tool_name"]
    assert entry["details"]["order_id"] == oid

    ((to, mail),) = notified.outbox.sent
    assert to == "buyer-a@example.test"
    assert mail.subject == "We received your delivery request"
    assert "PO: PO-42" in mail.text and "100 gal" in mail.text
    assert '<html lang="en">' in mail.html and "PO: PO-42" in mail.html
    for leaked in (oid, "price", "cost", "margin", "cents", CUSTOMER_A, T1):
        assert leaked not in mail.text
    for leaked in (oid, "price", "cents", CUSTOMER_A, T1):
        assert leaked not in mail.html


def test_replayed_submit_notifies_once(client, notified, cA):
    body = order_body()
    assert call(client, "POST", "/api/portal/orders", cA, json=body).status_code == 201
    assert call(client, "POST", "/api/portal/orders", cA, json=body).status_code == 200
    assert len(notified.activity.entries) == 1
    assert len(notified.outbox.sent) == 1


def test_dispatcher_confirm_emails_the_requester(client, sessions, notified, cA):
    oid = _submit(client, cA)
    notified.outbox.sent.clear()
    resp = _release(client, sessions, oid)
    assert resp.status_code == 200, resp.text
    ((to, mail),) = notified.outbox.sent
    assert to == "buyer-a@example.test"
    assert mail.subject == "Your delivery request is confirmed"
    portal = call(client, "GET", f"/api/portal/orders/{oid}", cA).json()["data"]
    assert portal["status_code"] == "confirmed" and portal["cancellable"] is False


def test_dispatcher_decline_emails_the_requester(client, sessions, notified, cA):
    oid = _submit(client, cA)
    notified.outbox.sent.clear()
    resp = call(client, "POST", f"/api/orders/{oid}/cancel", sessions.staff("dispatcher"),
                json={"reason": "no_capacity"})
    assert resp.status_code == 200, resp.text
    ((to, mail),) = notified.outbox.sent
    assert mail.subject == "Your delivery request was declined"
    assert "no_capacity" not in mail.text
    portal = call(client, "GET", f"/api/portal/orders/{oid}", cA).json()["data"]
    assert portal["status_code"] == "cancelled"


def test_customer_cancel_sends_no_decline_email(client, notified, cA):
    oid = _submit(client, cA)
    notified.outbox.sent.clear()
    assert call(client, "POST", f"/api/portal/orders/{oid}/cancel", cA, json={}).status_code == 200
    assert notified.outbox.sent == []


def test_staff_actions_on_other_orders_send_nothing(client, sessions, notified, cA):
    notified.h.add_order(T1, CUSTOMER_A, "QA-ORD-DISPATCH-1", status="on_hold",
                         hold_reason="credit check")
    notified.h.add_order(T1, CUSTOMER_A, "QA-ORD-DISPATCH-2", status="placed")
    _release(client, sessions, "QA-ORD-DISPATCH-1")
    call(client, "POST", "/api/orders/QA-ORD-DISPATCH-2/cancel", sessions.staff("dispatcher"),
         json={"reason": "duplicate"})
    assert notified.outbox.sent == []


def test_without_sendgrid_no_email_but_dispatchers_still_hear(client, notified, cA, monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY")
    _submit(client, cA)
    assert len(notified.activity.entries) == 1
    assert notified.outbox.sent == []


def test_notification_failures_never_fail_the_request(client, notified, cA):
    async def boom(*_a, **_k):
        raise RuntimeError("provider down")

    notified.activity.log = boom
    from portal.services.order_notifications import get_portal_order_notifier

    get_portal_order_notifier()._send = boom
    _submit(client, cA)


# ---------------------------------------------------------------------------
# Templates and rendering
# ---------------------------------------------------------------------------


def test_portal_templates_are_in_the_notification_template_defaults():
    from notifications.services.template_renderer import DEFAULT_TEMPLATES

    keys = {(t["event_type"], t["channel"]) for t in DEFAULT_TEMPLATES}
    for event in ("portal_invite", "portal_request_received", "portal_request_confirmed",
                  "portal_request_declined"):
        assert (event, "email") in keys
    assert len(keys) == len(DEFAULT_TEMPLATES)  # no duplicate pairs


def test_invite_uses_invite_wording_in_text_and_html():
    from portal.services.invite_email import render_invite_email

    mail = render_invite_email(supplier_name="Demo Fuels", customer_name="Acme <Farms>",
                               link="https://app.example.test/auth/reset-password?token=t&invite=1")
    assert mail.subject == "Demo Fuels invited you to their customer portal"
    assert "reset" not in mail.subject.lower()
    assert "reset your password" not in mail.text.lower()
    assert "reset your password" not in mail.html.lower()
    assert "Choose your password" in mail.html
    assert "Acme &lt;Farms&gt;" in mail.html and "<Farms>" not in mail.html
    assert 'href="https://app.example.test/auth/reset-password?token=t&amp;invite=1"' in mail.html


async def test_tenant_edited_template_wins():
    from portal.services import portal_email as pe

    class Renderer:
        async def list_templates(self, tenant_id, event_type=None, channel=None):
            if tenant_id == T1 and event_type == pe.REQUEST_CONFIRMED:
                return [{"subject_template": "Booked: {tank_name}", "body_template": "See {portal_link}"}]
            return []

    pe.configure_portal_email_templates(Renderer())
    try:
        data = {"tank_name": "Yard tank", "portal_link": "https://app.example.test/portal/orders"}
        mine = await pe.render_portal_email(pe.REQUEST_CONFIRMED, data, tenant_id=T1)
        other = await pe.render_portal_email(pe.REQUEST_CONFIRMED, data, tenant_id=T2)
    finally:
        pe.configure_portal_email_templates(None)
    assert mine.subject == "Booked: Yard tank"
    assert other.subject == "Your delivery request is confirmed"


def test_window_and_quantity_formats():
    from portal.services.order_notifications import format_quantity, format_window

    assert format_window("2026-10-09T14:00:00Z", "2026-10-09T18:00:00Z", "America/Chicago") == (
        "Fri 9 Oct, 9:00 AM – 1:00 PM CDT"
    )
    assert format_window("2026-10-09T05:00:00Z", "2026-10-10T05:00:00Z", "America/Chicago") == "Fri 9 Oct"
    assert format_quantity(275.5, False) == "276 gal"
    assert format_quantity(None, True) == "Fill to full"


# ---------------------------------------------------------------------------
# SendGrid: configured / unconfigured, HTML alternative, no key in logs
# ---------------------------------------------------------------------------


async def test_send_portal_email_unconfigured_sends_nothing(monkeypatch):
    from portal.services import portal_email as pe

    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    monkeypatch.delenv("SENDGRID_FROM_EMAIL", raising=False)
    assert pe.email_channel_configured() is False
    assert await pe.send_portal_email("a@example.test", pe.PortalEmail("s", "t", "<p>t</p>")) is False


async def test_send_portal_email_through_sendgrid_with_html_and_no_key_in_logs(monkeypatch, caplog):
    import sys

    from portal.services import portal_email as pe

    calls = []

    class Mail:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    class Client:
        def __init__(self, key):
            self.key = key

        def send(self, mail):
            return SimpleNamespace(status_code=202, headers={"X-Message-Id": "msg-1"})

    sendgrid = SimpleNamespace(SendGridAPIClient=Client)
    helpers_mail = SimpleNamespace(Mail=Mail)
    monkeypatch.setitem(sys.modules, "sendgrid", sendgrid)
    monkeypatch.setitem(sys.modules, "sendgrid.helpers", SimpleNamespace(mail=helpers_mail))
    monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", helpers_mail)
    monkeypatch.setenv("SENDGRID_API_KEY", FAKE_KEY)
    monkeypatch.setenv("SENDGRID_FROM_EMAIL", "no-reply@example.test")
    caplog.set_level(logging.DEBUG)

    mail = pe.render_default(pe.REQUEST_RECEIVED, {"supplier_name": "Demo Fuels"})
    assert await pe.send_portal_email("buyer@example.test", mail) is True
    (kwargs,) = calls
    assert kwargs["plain_text_content"] == mail.text
    assert kwargs["html_content"] == mail.html
    assert kwargs["from_email"] == "no-reply@example.test"

    class Failing(Client):
        def send(self, mail):
            raise RuntimeError("401 Unauthorized")

    sendgrid.SendGridAPIClient = Failing
    assert await pe.send_portal_email("buyer@example.test", mail) is False
    assert FAKE_KEY not in caplog.text


def test_auth_email_routes_through_sendgrid_smtp_without_logging_the_key(caplog):
    from auth.supertokens_init import _build_email_delivery
    from config.settings import Settings

    caplog.set_level(logging.DEBUG)
    settings = Settings(
        smtp_host="smtp.sendgrid.net",
        smtp_port=587,
        smtp_username="apikey",
        smtp_password=FAKE_KEY,
        smtp_from_email="no-reply@example.test",
        smtp_secure=False,
    )
    config = _build_email_delivery(settings)
    assert config is not None
    smtp = config.service.service_implementation.transporter.smtp_settings
    assert smtp.host == "smtp.sendgrid.net" and smtp.port == 587 and smtp.secure is False
    assert smtp.username == "apikey" and smtp.from_.email == "no-reply@example.test"
    assert FAKE_KEY not in caplog.text

    assert _build_email_delivery(Settings(smtp_host="", smtp_from_email="")) is None


def test_staff_resolution_ignores_non_portal_and_non_awaiting_orders():
    import asyncio

    from portal.services import order_notifications as on

    seen = []

    class Spy(on.PortalOrderNotifier):
        async def request_resolved(self, tenant_id, order, *, confirmed):
            seen.append((tenant_id, confirmed))

    saved = on.get_portal_order_notifier()
    on.configure_portal_order_notifier(Spy())
    try:
        held = {"intake_channel": "web_portal", "status": "on_hold",
                "hold_reason": PORTAL_REVIEW_HOLD_REASON, "order_id": "o1"}
        asyncio.run(on.notify_staff_resolution(T1, {**held, "intake_channel": "dispatcher"}, None, confirmed=True))
        asyncio.run(on.notify_staff_resolution(T1, {**held, "hold_reason": "credit"}, None, confirmed=True))
        asyncio.run(on.notify_staff_resolution(T1, {**held, "status": "placed"}, None, confirmed=False))
        asyncio.run(on.notify_staff_resolution(T1, held, None, confirmed=False))
    finally:
        on.configure_portal_order_notifier(saved)
    assert seen == [(T1, False)]
