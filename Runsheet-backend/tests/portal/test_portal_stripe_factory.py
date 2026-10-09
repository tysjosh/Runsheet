"""The portal Stripe connector factory: enabled instances only (design §3.1; PAY-10).

Both factories come from ``bootstrap.agents._make_stripe_connector_factory``;
``enabled_only=False`` is the webhook factory, which still serves a disabled
instance so its webhooks verify.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from bootstrap.agents import _make_stripe_connector_factory


class _Repo:
    def __init__(self, instances=(), error=None):
        self.instances = list(instances)
        self.error = error
        self.calls = []

    async def list_for_tenant(self, *, tenant_id, provider_name, enabled):
        self.calls.append((tenant_id, provider_name, enabled))
        if self.error is not None:
            raise self.error
        return list(self.instances)


def _inst(instance_id, enabled):
    return SimpleNamespace(instance_id=instance_id, credentials_ref=f"ref-{instance_id}", enabled=enabled)


def _factories(repo, vault=object()):
    def build(tenant_id, instance):
        return ("connector", tenant_id, instance.instance_id)

    portal = _make_stripe_connector_factory(repository=repo, vault=vault, build_connector=build,
                                            enabled_only=True)
    webhook = _make_stripe_connector_factory(repository=repo, vault=vault, build_connector=build,
                                             enabled_only=False)
    return portal, webhook


async def test_only_disabled_instance_gives_none_but_webhook_still_resolves():
    repo = _Repo([_inst("stripe-off", False)])
    portal, webhook = _factories(repo)
    assert await portal("demo-tenant") is None
    assert await webhook("demo-tenant") == ("connector", "demo-tenant", "stripe-off")
    assert repo.calls[0] == ("demo-tenant", "stripe", None)


async def test_disabled_plus_enabled_gives_the_enabled_one():
    repo = _Repo([_inst("stripe-off", False), _inst("stripe-on", True)])
    portal, webhook = _factories(repo)
    assert await portal("demo-tenant") == ("connector", "demo-tenant", "stripe-on")
    assert await webhook("demo-tenant") == ("connector", "demo-tenant", "stripe-on")


async def test_no_instances_or_missing_wiring_gives_none():
    portal, _ = _factories(_Repo([]))
    assert await portal("demo-tenant") is None
    portal, _ = _factories(None)
    assert await portal("demo-tenant") is None
    portal, webhook = _factories(_Repo([_inst("stripe-on", True)]), vault=None)
    assert await portal("demo-tenant") is None
    assert await webhook("demo-tenant") is None


async def test_repository_error_gives_none_and_warn(caplog):
    caplog.set_level(logging.WARNING, logger="bootstrap.agents")
    portal, _ = _factories(_Repo(error=RuntimeError("ddb down")))
    assert await portal("demo-tenant") is None
    warns = [r for r in caplog.records if r.name == "bootstrap.agents" and r.levelno == logging.WARNING]
    assert len(warns) == 1 and "Portal Stripe connector factory" in warns[0].getMessage()
