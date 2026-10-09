"""SMTP email dispatcher (Mailtrap on staging).

Covers: configured vs unconfigured selection (bootstrap and the portal email
path), STARTTLS before login with username ``api``, implicit TLS, a hung
relay not blocking the event loop, no secret in logs or failure reasons, the
invite email's wording over SMTP, order emails as text plus HTML, and
SuperTokens auth email honouring port 587 STARTTLS (``smtp_secure=False``).

``smtplib`` and ``aiosmtplib`` are mocked; nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import threading
import time
from unittest.mock import patch

import pytest

from notifications.services import smtp_email_dispatcher as sed

FAKE_TOKEN = "mt-fake-token-for-tests-only-7f3a"

SMTP_ENV = {
    "SMTP_HOST": "live.smtp.mailtrap.io",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "api",
    "SMTP_PASSWORD": FAKE_TOKEN,
    "SMTP_FROM_EMAIL": "no-reply@runsheetops.com",
    "SMTP_FROM_NAME": "Runsheet",
    "SMTP_SECURE": "false",
}

_EMAIL_ENV_NAMES = list(SMTP_ENV) + ["SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"]


@pytest.fixture
def no_email_env(monkeypatch):
    for name in _EMAIL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def smtp_env(no_email_env, monkeypatch):
    for name, value in SMTP_ENV.items():
        monkeypatch.setenv(name, value)


class FakeSMTP:
    """Records the SMTP session; behaviour tweakable per test."""

    instances: list = []
    starttls_supported = True
    login_error: Exception | None = None
    block: threading.Event | None = None

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.block is not None:
            FakeSMTP.block.wait(5)
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list = []
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        if not FakeSMTP.starttls_supported:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error

    def send_message(self, msg):
        self.calls.append("send")
        self.sent.append(msg)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.starttls_supported = True
    FakeSMTP.login_error = None
    FakeSMTP.block = None
    monkeypatch.setattr(sed.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(sed.smtplib, "SMTP_SSL", FakeSMTPSSL)
    yield FakeSMTP
    if FakeSMTP.block is not None:
        FakeSMTP.block.set()


def _parts(msg):
    return {part.get_content_type(): part.get_content() for part in msg.walk() if not part.is_multipart()}


# ---------------------------------------------------------------------------
# Selection: configured vs unconfigured
# ---------------------------------------------------------------------------


def _email_dispatcher_from_bootstrap():
    from bootstrap.notifications import _create_dispatchers

    (email,) = [d for d in _create_dispatchers() if d.channel_name == "email"]
    return email


def test_bootstrap_selects_smtp_when_host_from_and_password_are_set():
    with patch.dict(os.environ, SMTP_ENV, clear=True):
        assert isinstance(_email_dispatcher_from_bootstrap(), sed.SmtpEmailDispatcher)


def test_bootstrap_prefers_smtp_over_sendgrid():
    env = dict(SMTP_ENV, SENDGRID_API_KEY="SG.x", SENDGRID_FROM_EMAIL="a@example.test")
    with patch.dict(os.environ, env, clear=True):
        assert isinstance(_email_dispatcher_from_bootstrap(), sed.SmtpEmailDispatcher)


@pytest.mark.parametrize("missing", ["SMTP_HOST", "SMTP_FROM_EMAIL", "SMTP_PASSWORD"])
def test_bootstrap_falls_back_to_stub_when_smtp_is_incomplete(missing):
    from notifications.services.channel_dispatchers import StubEmailDispatcher

    env = {k: v for k, v in SMTP_ENV.items() if k != missing}
    with patch.dict(os.environ, env, clear=True):
        assert isinstance(_email_dispatcher_from_bootstrap(), StubEmailDispatcher)


def test_bootstrap_stub_when_nothing_is_configured():
    from notifications.services.channel_dispatchers import StubEmailDispatcher

    with patch.dict(os.environ, {}, clear=True):
        assert isinstance(_email_dispatcher_from_bootstrap(), StubEmailDispatcher)


def test_init_names_missing_settings_without_values(no_email_env, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "live.smtp.mailtrap.io")
    with pytest.raises(ValueError) as exc_info:
        sed.SmtpEmailDispatcher()
    assert "SMTP_PASSWORD" in str(exc_info.value) and "SMTP_FROM_EMAIL" in str(exc_info.value)


async def test_portal_email_unconfigured_sends_nothing(no_email_env, fake_smtp):
    from portal.services import portal_email as pe

    assert pe.email_channel_configured() is False
    assert await pe.send_portal_email("a@example.test", pe.PortalEmail("s", "t", "<p>t</p>")) is False
    assert fake_smtp.instances == []


# ---------------------------------------------------------------------------
# Transport: STARTTLS, login as "api", implicit TLS, credential-free failures
# ---------------------------------------------------------------------------


async def test_starttls_before_login_with_username_api(smtp_env, fake_smtp, caplog):
    caplog.set_level(logging.DEBUG)
    notification = {"recipient_reference": "buyer@example.test", "subject": "Hi", "message_body": "Body"}
    assert await sed.SmtpEmailDispatcher().dispatch(notification) == "sent"

    (server,) = fake_smtp.instances
    assert (server.host, server.port) == ("live.smtp.mailtrap.io", 587)
    assert server.timeout == sed.SEND_TIMEOUT_SECONDS
    assert server.calls == ["ehlo", "starttls", "ehlo", ("login", "api", FAKE_TOKEN), "send", "quit"]
    (msg,) = server.sent
    assert msg["From"] == "Runsheet <no-reply@runsheetops.com>"
    assert msg["To"] == "buyer@example.test"
    assert notification["provider_message_id"] == msg["Message-ID"]
    assert msg["Message-ID"].endswith("@runsheetops.com>")
    assert FAKE_TOKEN not in caplog.text


async def test_no_starttls_fails_without_sending_the_password(smtp_env, fake_smtp):
    fake_smtp.starttls_supported = False
    notification = {"recipient_reference": "b@example.test", "subject": "s", "message_body": "t"}
    assert await sed.SmtpEmailDispatcher().dispatch(notification) == "failed"
    (server,) = fake_smtp.instances
    assert not any(isinstance(c, tuple) and c[0] == "login" for c in server.calls)
    assert "SMTPNotSupportedError" in notification["failure_reason"]


async def test_secure_true_uses_implicit_tls(smtp_env, fake_smtp, monkeypatch):
    monkeypatch.setenv("SMTP_SECURE", "true")
    monkeypatch.setenv("SMTP_PORT", "465")
    assert await sed.SmtpEmailDispatcher().dispatch(
        {"recipient_reference": "b@example.test", "subject": "s", "message_body": "t"}
    ) == "sent"
    (server,) = fake_smtp.instances
    assert isinstance(server, FakeSMTPSSL) and server.port == 465
    assert "starttls" not in server.calls


async def test_auth_failure_never_logs_the_token(smtp_env, fake_smtp, caplog):
    caplog.set_level(logging.DEBUG)
    # A server reply that echoes the credential must not reach the logs.
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, f"bad credentials {FAKE_TOKEN}".encode())
    notification = {"recipient_reference": "b@example.test", "subject": "s", "message_body": "t"}
    assert await sed.SmtpEmailDispatcher().dispatch(notification) == "failed"
    assert notification["failure_reason"] == "SMTP authentication failed (SMTPAuthenticationError 535)"
    assert FAKE_TOKEN not in notification["failure_reason"]
    assert FAKE_TOKEN not in caplog.text
    assert "quit" in fake_smtp.instances[0].calls


# ---------------------------------------------------------------------------
# A hung relay never stalls the event loop
# ---------------------------------------------------------------------------


async def test_hung_smtp_times_out_without_blocking_the_event_loop(smtp_env, fake_smtp, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(sed, "SEND_TIMEOUT_SECONDS", 0.3)
    fake_smtp.block = threading.Event()  # the connect hangs until released

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    notification = {"recipient_reference": "b@example.test", "subject": "s", "message_body": "t"}
    started = time.monotonic()
    try:
        outcome = await sed.SmtpEmailDispatcher().dispatch(notification)
    finally:
        task.cancel()
        fake_smtp.block.set()
    elapsed = time.monotonic() - started
    assert outcome == "failed"
    assert notification["failure_reason"] == "SMTP send timed out after 0.3s"
    assert elapsed < 2
    assert ticks >= 5  # the loop kept running while the send hung
    assert FAKE_TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# Portal emails over SMTP: invite wording, order emails as text + HTML
# ---------------------------------------------------------------------------


async def test_invite_email_over_smtp_uses_invite_wording(smtp_env, fake_smtp, caplog):
    from portal.services.invite_email import render_invite_email, send_invite_email

    caplog.set_level(logging.DEBUG)
    content = render_invite_email(
        supplier_name="Demo Fuels",
        customer_name="Acme Farms",
        link="https://app.example.test/auth/reset-password?token=t&invite=1",
    )
    assert await send_invite_email("buyer@example.test", content) is True
    (msg,) = fake_smtp.instances[0].sent
    assert msg["Subject"] == "Demo Fuels invited you to their customer portal"
    parts = _parts(msg)
    assert "set up a customer portal account for Acme Farms" in parts["text/plain"]
    assert "Choose your password" in parts["text/html"]
    for body in parts.values():
        assert "reset your password" not in body.lower()
    assert FAKE_TOKEN not in caplog.text


@pytest.mark.parametrize(
    "event_type, subject",
    [
        ("portal_request_received", "We received your delivery request"),
        ("portal_request_confirmed", "Your delivery request is confirmed"),
        ("portal_request_declined", "Your delivery request was declined"),
    ],
)
async def test_order_emails_send_text_and_html_parts(smtp_env, fake_smtp, event_type, subject):
    from portal.services import portal_email as pe

    mail = pe.render_default(
        event_type,
        {
            "supplier_name": "Demo Fuels",
            "tank_name": "Yard tank",
            "product_name": "Diesel",
            "quantity": "500 gal",
            "delivery_window": "Fri 9 Oct",
            "po_line": "PO: PO-42",
            "portal_link": "https://app.example.test/portal/orders",
        },
    )
    assert await pe.send_portal_email("buyer@example.test", mail) is True
    (msg,) = fake_smtp.instances[0].sent
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Subject"] == subject
    parts = _parts(msg)
    assert set(parts) == {"text/plain", "text/html"}
    assert parts["text/plain"].strip() == mail.text.strip()
    assert '<html lang="en">' in parts["text/html"] and "Yard tank" in parts["text/html"]


# ---------------------------------------------------------------------------
# SuperTokens auth email (password reset + invite fallback) on 587 STARTTLS
# ---------------------------------------------------------------------------


def _mailtrap_settings():
    from config.settings import Settings

    return Settings(
        smtp_host="live.smtp.mailtrap.io",
        smtp_port=587,
        smtp_username="api",
        smtp_password=FAKE_TOKEN,
        smtp_from_email="no-reply@runsheetops.com",
        smtp_from_name="Runsheet",
        smtp_secure=False,
    )


async def test_auth_email_uses_starttls_on_587_and_logs_no_token(caplog, monkeypatch):
    from auth.supertokens_init import _build_email_delivery
    from supertokens_python.ingredients.emaildelivery.services import smtp as st_smtp

    caplog.set_level(logging.DEBUG)
    config = _build_email_delivery(_mailtrap_settings())
    assert config is not None
    transporter = config.service.service_implementation.transporter
    settings = transporter.smtp_settings
    assert (settings.host, settings.port, settings.secure) == ("live.smtp.mailtrap.io", 587, False)
    assert settings.username == "api" and settings.from_.email == "no-reply@runsheetops.com"

    seen = {}

    class FakeAioSMTP:
        def __init__(self, hostname, port, use_tls, tls_context=None):
            seen.update(hostname=hostname, port=port, use_tls=use_tls, calls=[])

        async def connect(self):
            seen["calls"].append("connect")

        async def starttls(self, tls_context=None):
            seen["calls"].append("starttls")

        async def login(self, user, password):
            seen["calls"].append(("login", user, password))

    monkeypatch.setattr(st_smtp.aiosmtplib, "SMTP", FakeAioSMTP)
    await transporter._connect()
    assert seen["use_tls"] is False and seen["port"] == 587
    assert seen["calls"] == ["connect", "starttls", ("login", "api", FAKE_TOKEN)]
    assert FAKE_TOKEN not in caplog.text
