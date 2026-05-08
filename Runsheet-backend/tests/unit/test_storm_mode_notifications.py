"""
Unit tests for the Storm_Mode-aware notification pipeline.

Covers:

* :class:`notifications.services.storm_mode_notifications.StormModeNotificationResolver`
  (eligibility decisions, alert-ref building, placeholder flattening,
  defensive fall-through).
* :class:`notifications.services.notification_service.NotificationService`
  storm-variant integration (event_type swap, weather_alert_ref
  stamping, placeholder merging, graceful fallback).

Validates: Requirement 9.2.6 / Task 10.9.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from notifications.models import DeliveryStatus, NotificationType
from notifications.services.notification_service import NotificationService
from notifications.services.storm_mode_notifications import (
    STORM_REASON_GENERATOR,
    STORM_REASON_KEEP_FULL,
    StormModeNotificationResolver,
    StormNotificationDecision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persisted_active_state(
    *,
    triggering_alert: dict | None = None,
    triggering_alert_ids: list[str] | None = None,
    expected_end_at: datetime | None = None,
) -> SimpleNamespace:
    """Return a PersistedState-shaped object reporting Storm_Mode ACTIVE."""
    return SimpleNamespace(
        state="active",
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        triggering_alert_ids=triggering_alert_ids or [],
        expected_end_at=expected_end_at,
        triggering_alert=triggering_alert,
    )


def _persisted_inactive_state() -> SimpleNamespace:
    return SimpleNamespace(
        state="inactive",
        updated_at=None,
        triggering_alert_ids=[],
        expected_end_at=None,
    )


def _make_profile(
    *,
    keep_full: bool = False,
    generator: bool = False,
) -> dict:
    """Return a minimal CustomerProfile-shaped dict."""
    return {
        "customer_id": "cust-1",
        "tenant_id": "tenant-1",
        "keep_full": {
            "keep_full_enabled": keep_full,
            "minimum_low_water_pct": 30.0,
            "keep_full_priority_boost": 0.25,
        },
        "is_generator_fuel": generator,
    }


def _make_state_provider(state: object) -> MagicMock:
    provider = MagicMock()
    provider.get_state = AsyncMock(return_value=state)
    return provider


def _make_profile_resolver(profile: object | None) -> MagicMock:
    resolver = MagicMock()
    resolver.__call__ = AsyncMock(return_value=profile)
    # ``StormModeNotificationResolver`` invokes the resolver directly so
    # we wrap it in an AsyncMock assigned at the attribute used by the
    # protocol signature.
    async def _call(tenant_id, customer_id):
        return profile

    return _call


# ---------------------------------------------------------------------------
# StormModeNotificationResolver — eligibility
# ---------------------------------------------------------------------------


class TestResolverEligibility:
    """Task 10.9 / Req 9.2.6 — only keep-full and generator customers flip."""

    async def test_returns_inactive_when_no_state_provider(self):
        resolver = StormModeNotificationResolver(state_provider=None)
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is False
        assert decision.storm_event_type is None
        assert decision.weather_alert_ref is None

    async def test_returns_inactive_when_state_inactive(self):
        state_provider = _make_state_provider(_persisted_inactive_state())
        resolver = StormModeNotificationResolver(
            state_provider=state_provider,
            profile_resolver=_make_profile_resolver(
                _make_profile(keep_full=True)
            ),
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is False

    async def test_returns_inactive_when_customer_not_eligible(self):
        state = _persisted_active_state(
            triggering_alert={
                "alert_id": "alert-1",
                "alert_type": "winter_storm_warning",
                "severity": "severe",
                "headline": "Winter Storm",
                "source": "noaa",
            }
        )
        resolver = StormModeNotificationResolver(
            state_provider=_make_state_provider(state),
            profile_resolver=_make_profile_resolver(_make_profile()),
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is False
        assert decision.storm_variant_reason is None

    async def test_flags_keep_full_customer_with_active_storm(self):
        state = _persisted_active_state(
            triggering_alert={
                "alert_id": "alert-1",
                "alert_type": "winter_storm_warning",
                "severity": "severe",
                "headline": "Winter Storm Warning in effect",
                "source": "noaa",
                "region_code": "MA",
                "expected_start_at": "2025-01-10T12:00:00+00:00",
                "expected_end_at": "2025-01-12T12:00:00+00:00",
                "affected_zip_codes": ["02139", "02140"],
            }
        )
        resolver = StormModeNotificationResolver(
            state_provider=_make_state_provider(state),
            profile_resolver=_make_profile_resolver(
                _make_profile(keep_full=True)
            ),
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is True
        assert decision.storm_event_type == "delay_alert_storm"
        assert decision.storm_variant_reason == STORM_REASON_KEEP_FULL

        assert decision.weather_alert_ref is not None
        assert decision.weather_alert_ref["alert_id"] == "alert-1"
        assert decision.weather_alert_ref["severity"] == "severe"
        assert (
            decision.weather_alert_ref["expected_end_at"]
            == "2025-01-12T12:00:00+00:00"
        )

        # Placeholder fields flattened for template rendering.
        assert (
            decision.placeholder_data["weather_alert_type"]
            == "winter_storm_warning"
        )
        assert (
            decision.placeholder_data["weather_alert_headline"]
            == "Winter Storm Warning in effect"
        )

    async def test_flags_generator_customer_with_active_storm(self):
        state = _persisted_active_state(
            triggering_alert={
                "alert_id": "alert-2",
                "alert_type": "hurricane_warning",
                "severity": "extreme",
                "headline": "Hurricane Warning",
                "source": "nws",
            }
        )
        resolver = StormModeNotificationResolver(
            state_provider=_make_state_provider(state),
            profile_resolver=_make_profile_resolver(
                _make_profile(generator=True)
            ),
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="eta_change"
        )
        assert decision.storm_mode_active is True
        assert decision.storm_event_type == "eta_change_storm"
        assert decision.storm_variant_reason == STORM_REASON_GENERATOR

    async def test_keep_full_takes_precedence_over_generator(self):
        state = _persisted_active_state(
            triggering_alert={"alert_id": "a", "alert_type": "ice_storm"}
        )
        resolver = StormModeNotificationResolver(
            state_provider=_make_state_provider(state),
            profile_resolver=_make_profile_resolver(
                _make_profile(keep_full=True, generator=True)
            ),
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is True
        assert decision.storm_variant_reason == STORM_REASON_KEEP_FULL


# ---------------------------------------------------------------------------
# StormModeNotificationResolver — defensive fall-through
# ---------------------------------------------------------------------------


class TestResolverFallThrough:
    """A broken Storm_Mode signal must never suppress a notification."""

    async def test_state_provider_exception_degrades_to_inactive(self):
        provider = MagicMock()
        provider.get_state = AsyncMock(side_effect=RuntimeError("boom"))
        resolver = StormModeNotificationResolver(state_provider=provider)
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is False

    async def test_profile_resolver_exception_degrades_to_inactive(self):
        async def _raising_resolver(tenant_id, customer_id):
            raise RuntimeError("boom")

        resolver = StormModeNotificationResolver(
            state_provider=_make_state_provider(_persisted_active_state()),
            profile_resolver=_raising_resolver,
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is False

    async def test_missing_tenant_id_short_circuits(self):
        state_provider = _make_state_provider(_persisted_active_state())
        resolver = StormModeNotificationResolver(
            state_provider=state_provider,
            profile_resolver=_make_profile_resolver(
                _make_profile(keep_full=True)
            ),
        )
        decision = await resolver.resolve(
            tenant_id="", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is False
        state_provider.get_state.assert_not_called()


# ---------------------------------------------------------------------------
# StormModeNotificationResolver — alert ref extraction
# ---------------------------------------------------------------------------


class TestAlertRefExtraction:
    """The weather_alert_ref payload must carry the triggering alert."""

    async def test_falls_back_to_triggering_alert_ids_when_no_alert_dict(self):
        state = _persisted_active_state(
            triggering_alert=None,
            triggering_alert_ids=["alert-xyz"],
            expected_end_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
        )
        resolver = StormModeNotificationResolver(
            state_provider=_make_state_provider(state),
            profile_resolver=_make_profile_resolver(
                _make_profile(keep_full=True)
            ),
        )
        decision = await resolver.resolve(
            tenant_id="t1", customer_id="c1", event_type="delay_alert"
        )
        assert decision.storm_mode_active is True
        assert decision.weather_alert_ref["alert_id"] == "alert-xyz"
        assert (
            decision.weather_alert_ref["expected_end_at"]
            == "2025-02-01T00:00:00+00:00"
        )


# ---------------------------------------------------------------------------
# NotificationService storm-variant integration
# ---------------------------------------------------------------------------


def _make_es_mock() -> MagicMock:
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    es.update_document = AsyncMock(return_value={"result": "updated"})
    es.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    return es


def _make_dispatcher_mock(status: str = "sent") -> MagicMock:
    dispatcher = MagicMock()
    dispatcher.channel_name = "sms"
    dispatcher.dispatch = AsyncMock(return_value=status)
    return dispatcher


def _rule(event_type: str = "delay_alert") -> dict:
    return {
        "rule_id": "rule-1",
        "tenant_id": "tenant-1",
        "event_type": event_type,
        "enabled": True,
        "default_channels": ["sms"],
        "template_id": None,
    }


def _storm_template(event_type: str = "delay_alert_storm") -> dict:
    return {
        "template_id": "tmpl-storm",
        "tenant_id": "tenant-1",
        "event_type": event_type,
        "channel": "sms",
        "subject_template": "Storm Delay — Order {order_id}",
        "body_template": (
            "{weather_alert_type} active. Order {order_id} delayed "
            "{delay_minutes} min."
        ),
        "placeholders": ["order_id", "delay_minutes", "weather_alert_type"],
    }


def _default_template() -> dict:
    return {
        "template_id": "tmpl-default",
        "tenant_id": "tenant-1",
        "event_type": "delay_alert",
        "channel": "sms",
        "subject_template": "Delay — {order_id}",
        "body_template": "Order {order_id} delayed {delay_minutes} min.",
        "placeholders": ["order_id", "delay_minutes"],
    }


class TestNotificationServiceStormIntegration:
    """NotificationService swaps templates + stamps metadata under Storm_Mode."""

    def _wire(
        self,
        *,
        decision: StormNotificationDecision,
        storm_template: dict | None = None,
        default_template: dict | None = None,
    ) -> tuple[NotificationService, list[dict]]:
        es = _make_es_mock()
        service = NotificationService(es)

        # Rule engine and preference resolver always return the same
        # trivial setup.
        service._rule_engine.evaluate_rule = AsyncMock(
            return_value=_rule("delay_alert")
        )
        service._preference_resolver.resolve_channels = AsyncMock(
            return_value=[]
        )

        # Template lookup: honour the event_type argument the service
        # passes so we can verify the storm event_type is preferred.
        async def _list_templates(tenant_id, event_type=None, channel=None):
            if event_type == "delay_alert_storm" and storm_template is not None:
                return [storm_template]
            if event_type == "delay_alert" and default_template is not None:
                return [default_template]
            return []

        service._template_renderer.list_templates = AsyncMock(
            side_effect=_list_templates
        )

        async def _render(template_id, event_data, tenant_id):
            if template_id == "tmpl-storm":
                return {
                    "subject": f"Storm Delay — Order {event_data.get('order_id','')}",
                    "body": (
                        f"{event_data.get('weather_alert_type','')} active. "
                        f"Order {event_data.get('order_id','')} delayed "
                        f"{event_data.get('delay_minutes','')} min."
                    ),
                }
            return {
                "subject": f"Delay — {event_data.get('order_id','')}",
                "body": (
                    f"Order {event_data.get('order_id','')} delayed "
                    f"{event_data.get('delay_minutes','')} min."
                ),
            }

        service._template_renderer.render = AsyncMock(side_effect=_render)

        dispatcher = _make_dispatcher_mock()
        service.register_dispatcher("sms", dispatcher)

        # Install a resolver that always returns the provided decision.
        mock_resolver = MagicMock()
        mock_resolver.resolve = AsyncMock(return_value=decision)
        service.set_storm_notification_resolver(mock_resolver)

        # Capture every indexed notification so the test can inspect
        # what landed in ES.
        indexed: list[dict] = []

        async def _capture_index(index, doc_id, doc):
            indexed.append(dict(doc))
            return {"result": "created"}

        es.index_document = AsyncMock(side_effect=_capture_index)

        return service, indexed

    async def test_storm_active_swaps_to_storm_variant_template(self):
        storm_decision = StormNotificationDecision(
            storm_mode_active=True,
            storm_event_type="delay_alert_storm",
            weather_alert_ref={
                "alert_id": "alert-1",
                "alert_type": "winter_storm_warning",
                "severity": "severe",
            },
            storm_variant_reason=STORM_REASON_KEEP_FULL,
            placeholder_data={
                "weather_alert_type": "winter_storm_warning",
                "weather_alert_headline": "Winter Storm Warning",
                "weather_alert_severity": "severe",
                "weather_alert_expected_end_at": "2025-01-12T12:00:00+00:00",
                "weather_alert_id": "alert-1",
            },
        )
        service, indexed = self._wire(
            decision=storm_decision,
            storm_template=_storm_template(),
            default_template=_default_template(),
        )

        result = await service.notify_event(
            "delay_alert",
            {
                "customer_id": "cust-1",
                "order_id": "ORD-1",
                "delay_minutes": 30,
            },
            "tenant-1",
        )

        assert len(result) == 1
        assert len(indexed) == 1
        doc = indexed[0]

        # Template swap shows up in the rendered message body.
        assert "winter_storm_warning active" in doc["message_body"]
        # Storm_Mode metadata stamped per Task 10.9 / Req 9.2.6.
        assert doc["storm_mode_active"] is True
        assert doc["storm_variant_reason"] == STORM_REASON_KEEP_FULL
        assert doc["weather_alert_ref"]["alert_id"] == "alert-1"
        assert doc["weather_alert_ref"]["severity"] == "severe"

    async def test_storm_inactive_keeps_default_template_and_no_metadata(self):
        service, indexed = self._wire(
            decision=StormNotificationDecision.inactive(),
            storm_template=_storm_template(),
            default_template=_default_template(),
        )

        result = await service.notify_event(
            "delay_alert",
            {
                "customer_id": "cust-1",
                "order_id": "ORD-1",
                "delay_minutes": 30,
            },
            "tenant-1",
        )

        assert len(result) == 1
        assert len(indexed) == 1
        doc = indexed[0]

        assert doc["message_body"].startswith("Order ORD-1 delayed")
        # No Storm_Mode metadata on default-flow notifications.
        assert "storm_mode_active" not in doc
        assert "weather_alert_ref" not in doc
        assert "storm_variant_reason" not in doc

    async def test_storm_active_falls_back_to_default_when_variant_missing(self):
        """When a tenant has no storm-variant template, the default renders.

        Verifies the graceful fallback path added alongside the
        severe-weather variants — the notification still ships even
        though the preferred template is missing.
        """
        storm_decision = StormNotificationDecision(
            storm_mode_active=True,
            storm_event_type="delay_alert_storm",
            weather_alert_ref={"alert_id": "alert-1"},
            storm_variant_reason=STORM_REASON_GENERATOR,
            placeholder_data={
                "weather_alert_type": "ice_storm_warning",
                "weather_alert_headline": "Ice Storm",
                "weather_alert_severity": "severe",
                "weather_alert_expected_end_at": "",
                "weather_alert_id": "alert-1",
            },
        )
        # Only the default template exists for this tenant.
        service, indexed = self._wire(
            decision=storm_decision,
            storm_template=None,
            default_template=_default_template(),
        )

        result = await service.notify_event(
            "delay_alert",
            {
                "customer_id": "cust-1",
                "order_id": "ORD-1",
                "delay_minutes": 30,
            },
            "tenant-1",
        )

        assert len(result) == 1
        doc = indexed[0]

        # Default template was rendered because no storm variant
        # exists.
        assert doc["message_body"].startswith("Order ORD-1 delayed")
        # Storm metadata is still stamped — the resolver flagged the
        # event as storm-eligible regardless of template availability.
        assert doc["storm_mode_active"] is True
        assert doc["weather_alert_ref"]["alert_id"] == "alert-1"
        assert doc["storm_variant_reason"] == STORM_REASON_GENERATOR

    async def test_storm_placeholder_data_merged_into_event_data(self):
        """Placeholder data is exposed to the default template too."""
        storm_decision = StormNotificationDecision(
            storm_mode_active=True,
            storm_event_type="delay_alert_storm",
            weather_alert_ref={"alert_id": "alert-1"},
            storm_variant_reason=STORM_REASON_KEEP_FULL,
            placeholder_data={
                "weather_alert_type": "winter_storm_warning",
                "weather_alert_headline": "Winter Storm",
                "weather_alert_severity": "severe",
                "weather_alert_expected_end_at": "2025-01-12T12:00:00+00:00",
                "weather_alert_id": "alert-1",
            },
        )
        service, indexed = self._wire(
            decision=storm_decision,
            storm_template=_storm_template(),
            default_template=_default_template(),
        )

        await service.notify_event(
            "delay_alert",
            {
                "customer_id": "cust-1",
                "order_id": "ORD-99",
                "delay_minutes": 45,
            },
            "tenant-1",
        )

        # The storm template references {weather_alert_type} so the
        # rendered body must carry the flattened placeholder.
        render_mock: AsyncMock = service._template_renderer.render  # type: ignore[assignment]
        assert render_mock.await_count == 1
        passed_event_data = render_mock.await_args.args[1]
        assert (
            passed_event_data["weather_alert_type"]
            == "winter_storm_warning"
        )
        # Original caller data is preserved.
        assert passed_event_data["order_id"] == "ORD-99"
        assert passed_event_data["delay_minutes"] == 45
