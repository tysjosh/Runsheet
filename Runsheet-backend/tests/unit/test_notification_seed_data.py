"""Unit tests for ``notifications.services.seed_data``.

Another module the changed-file coverage gate found at zero coverage the first
time it ran. It is never exercised by accident: ``bootstrap/notifications.py``
imports it lazily and only when ``SEED_TENANT_ID`` is set, so nothing reaches it
in a normal boot or a normal test run.

What makes it worth testing rather than excluding is the ``pod_otp`` rule and its
comment: "Without an enabled rule for this event type, ``notify_event`` resolves
none and returns without delivering". A missing seed there does not raise — the
dispatch-time proof-of-delivery code simply never reaches the customer, and the
driver is then blocked at delivery by a code nobody received. That is a silent
failure in a delivery-blocking path, which is exactly the shape worth a test.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from notifications.services.seed_data import (
    FUEL_NOTIFICATION_RULES,
    seed_default_data,
    seed_fuel_notification_rules,
)

TENANT = "tenant-seed-1"


def _es() -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock()
    es.search_documents = AsyncMock(return_value={"hits": {"hits": []}})
    es.update_document = AsyncMock()
    es.get_document = AsyncMock(return_value=None)
    return es


def _indexed_rules(es: MagicMock) -> List[Dict[str, Any]]:
    """The rule documents written through ``index_document``."""
    return [
        call.args[2]
        for call in es.index_document.call_args_list
        if len(call.args) >= 3 and isinstance(call.args[2], dict)
        and "event_type" in call.args[2]
    ]


class TestTenantIdIsRequired:
    """Seeding without a tenant would write cross-tenant rules."""

    @pytest.mark.asyncio
    async def test_seed_fuel_rules_rejects_an_empty_tenant(self):
        with pytest.raises(ValueError, match="tenant_id"):
            await seed_fuel_notification_rules(_es(), "")

    @pytest.mark.asyncio
    async def test_seed_default_data_rejects_an_empty_tenant(self):
        with pytest.raises(ValueError, match="tenant_id"):
            await seed_default_data(_es(), "")


class TestFuelRuleSeeding:
    @pytest.mark.asyncio
    async def test_seeds_every_declared_fuel_rule(self):
        es = _es()

        await seed_fuel_notification_rules(es, TENANT)

        written = {r["event_type"] for r in _indexed_rules(es)}
        declared = {r["event_type"] for r in FUEL_NOTIFICATION_RULES}
        assert written == declared

    @pytest.mark.asyncio
    async def test_seeds_the_pod_otp_rule(self):
        """Its absence silently prevents the POD code from being delivered."""
        es = _es()

        await seed_fuel_notification_rules(es, TENANT)

        pod = [r for r in _indexed_rules(es) if r["event_type"] == "pod_otp"]
        assert pod, "pod_otp rule was not seeded"
        (rule,) = pod
        assert rule["enabled"] is True, (
            "a disabled pod_otp rule resolves to no delivery, which is the "
            "same outcome as not seeding it at all"
        )
        assert "sms" in rule["default_channels"]

    @pytest.mark.asyncio
    async def test_every_rule_is_scoped_to_the_tenant(self):
        es = _es()

        await seed_fuel_notification_rules(es, TENANT)

        assert {r["tenant_id"] for r in _indexed_rules(es)} == {TENANT}

    @pytest.mark.asyncio
    async def test_is_idempotent_for_rules_that_already_exist(self):
        """Re-seeding must not duplicate; the function is called on every boot."""
        es = _es()
        existing = [
            {"event_type": r["event_type"]} for r in FUEL_NOTIFICATION_RULES
        ]

        import notifications.services.rule_engine as rule_engine_mod

        original = rule_engine_mod.RuleEngine
        try:
            fake = MagicMock()
            fake.list_rules = AsyncMock(return_value=existing)
            rule_engine_mod.RuleEngine = MagicMock(return_value=fake)  # type: ignore[misc]

            await seed_fuel_notification_rules(es, TENANT)
        finally:
            rule_engine_mod.RuleEngine = original  # type: ignore[misc]

        assert _indexed_rules(es) == [], (
            "rules were re-written despite already existing, so every boot "
            "would add a duplicate"
        )

    @pytest.mark.asyncio
    async def test_each_rule_carries_a_distinct_id(self):
        es = _es()

        await seed_fuel_notification_rules(es, TENANT)

        ids = [r["rule_id"] for r in _indexed_rules(es)]
        assert len(ids) == len(set(ids))


class TestSeedDefaultDataIsolatesFailures:
    """Each seeding step is independent; one failure must not skip the rest.

    Templates seed last. If a rule-engine error propagated, a tenant would end
    up with rules and no templates to render them, which fails at delivery time
    rather than at seed time.
    """

    @pytest.mark.asyncio
    async def test_a_failing_rule_engine_does_not_stop_template_seeding(self):
        es = _es()

        import notifications.services.rule_engine as rule_engine_mod
        import notifications.services.template_renderer as template_mod

        orig_engine = rule_engine_mod.RuleEngine
        orig_renderer = template_mod.TemplateRenderer
        try:
            broken = MagicMock()
            broken.initialize_default_rules = AsyncMock(
                side_effect=RuntimeError("ES unavailable")
            )
            broken.list_rules = AsyncMock(side_effect=RuntimeError("ES unavailable"))
            rule_engine_mod.RuleEngine = MagicMock(return_value=broken)  # type: ignore[misc]

            renderer = MagicMock()
            renderer.initialize_default_templates = AsyncMock()
            template_mod.TemplateRenderer = MagicMock(return_value=renderer)  # type: ignore[misc]

            await seed_default_data(es, TENANT)

            renderer.initialize_default_templates.assert_awaited_once_with(TENANT)
        finally:
            rule_engine_mod.RuleEngine = orig_engine  # type: ignore[misc]
            template_mod.TemplateRenderer = orig_renderer  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_a_failing_template_renderer_does_not_raise(self):
        es = _es()

        import notifications.services.template_renderer as template_mod

        orig = template_mod.TemplateRenderer
        try:
            broken = MagicMock()
            broken.initialize_default_templates = AsyncMock(
                side_effect=RuntimeError("ES unavailable")
            )
            template_mod.TemplateRenderer = MagicMock(return_value=broken)  # type: ignore[misc]

            # Must not propagate: seeding runs during startup.
            await seed_default_data(es, TENANT)
        finally:
            template_mod.TemplateRenderer = orig  # type: ignore[misc]
