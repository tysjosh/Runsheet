"""Stripe key-mode WARN checks (customer portal review R4).

Keys are built by concatenation so secret scanners don't match them.
Every check is advisory: the connector logs at WARN and keeps working.
"""

from __future__ import annotations

import logging

import pytest

from integrations.stripe_connector import (
    StripeConnector,
    stripe_key_mode,
    stripe_key_mode_warnings,
)
from tests.unit.test_stripe_connector import (
    _FakeStripeSDK,
    _FakeVault,
    _FakeWebhookAPI,
)

_SK_LIVE = "sk" + "_live_" + "abc"
_SK_TEST = "sk" + "_test_" + "abc"
_RK_LIVE = "rk" + "_live_" + "abc"
_PK_LIVE = "pk" + "_live_" + "abc"
_PK_TEST = "pk" + "_test_" + "abc"
_LOGGER = "integrations.stripe_connector"


def _envelope(secret: str, publishable: str) -> dict:
    return {
        "secret_key": secret,
        "publishable_key": publishable,
        "webhook_secret": "fake_whsec_1",
    }


def _connector(vault, *, live_keys_expected, ref=None, stripe_module=None):
    return StripeConnector(
        tenant_id="tenant-a",
        instance_id="inst-1",
        credentials_vault=vault,
        credentials_ref=ref,
        stripe_module=stripe_module,
        live_keys_expected=live_keys_expected,
    )


def _seeded(envelope: dict) -> tuple[_FakeVault, str]:
    ref = "cred:tenant-a:stripe:seed"
    return _FakeVault(seed={ref: {"tenant_id": "tenant-a", "plaintext": envelope}}), ref


def _key_mode_records(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "key mode check" in r.getMessage()
    ]


class TestStripeKeyMode:
    @pytest.mark.parametrize(
        "key,mode",
        [
            (_SK_LIVE, "live"),
            (_RK_LIVE, "live"),
            (_PK_LIVE, "live"),
            (_SK_TEST, "test"),
            (_PK_TEST, "test"),
            ("fake_sk__1", None),
            (None, None),
        ],
    )
    def test_prefix(self, key, mode):
        assert stripe_key_mode(key) == mode

    def test_consistent_test_keys_off_production_are_clean(self):
        assert stripe_key_mode_warnings(
            _envelope(_SK_TEST, _PK_TEST), live_keys_expected=False
        ) == []

    def test_live_keys_in_production_are_clean(self):
        assert stripe_key_mode_warnings(
            _envelope(_SK_LIVE, _PK_LIVE), live_keys_expected=True
        ) == []

    def test_mode_mismatch(self):
        (msg,) = stripe_key_mode_warnings(
            _envelope(_SK_TEST, _PK_LIVE), live_keys_expected=True
        )
        assert "secret key is test mode but publishable key is live mode" == msg

    def test_live_secret_off_production(self):
        assert stripe_key_mode_warnings(
            _envelope(_SK_LIVE, _PK_LIVE), live_keys_expected=False
        ) == ["live-mode secret key in a non-production environment"]

    def test_unknown_environment_skips_environment_check(self):
        assert stripe_key_mode_warnings(
            _envelope(_SK_LIVE, _PK_LIVE), live_keys_expected=None
        ) == []


class TestConnectorWarnings:
    @pytest.mark.asyncio
    async def test_connect_warns_and_still_connects(self, caplog):
        connector = _connector(_FakeVault(), live_keys_expected=False)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            result = await connector.connect(_envelope(_SK_LIVE, _PK_TEST))
        assert result.status == "connected"
        messages = _key_mode_records(caplog)
        assert len(messages) == 2
        # Never log the keys themselves.
        assert not any(_SK_LIVE in m or _PK_TEST in m for m in messages)

    @pytest.mark.asyncio
    async def test_envelope_load_warns_once_per_connector(self, caplog):
        vault, ref = _seeded(_envelope(_SK_LIVE, _PK_LIVE))
        connector = _connector(vault, live_keys_expected=False, ref=ref)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert await connector.get_publishable_key() == _PK_LIVE
            await connector.get_publishable_key()
        assert len(_key_mode_records(caplog)) == 1

    @pytest.mark.asyncio
    async def test_no_warning_for_consistent_test_keys(self, caplog):
        vault, ref = _seeded(_envelope(_SK_TEST, _PK_TEST))
        connector = _connector(vault, live_keys_expected=False, ref=ref)
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            await connector.get_publishable_key()
        assert _key_mode_records(caplog) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "live_keys_expected,livemode,warned",
        [(False, True, True), (False, False, False), (True, True, False), (None, True, False)],
    )
    async def test_webhook_livemode(self, caplog, live_keys_expected, livemode, warned):
        vault, ref = _seeded(_envelope(_SK_TEST, _PK_TEST))
        sdk = _FakeStripeSDK(
            webhook_api=_FakeWebhookAPI(
                construct_return={"id": "evt_1", "type": "payment_intent.succeeded", "livemode": livemode}
            )
        )
        connector = _connector(
            vault, live_keys_expected=live_keys_expected, ref=ref, stripe_module=sdk
        )
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            event = await connector.verify_webhook_signature(b"{}", "t=1,v1=x")
        assert event["id"] == "evt_1"
        hits = [r for r in caplog.records if "live-mode webhook event" in r.getMessage()]
        assert bool(hits) is warned
