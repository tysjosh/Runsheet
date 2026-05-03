"""
Unit tests for the CustomerPromise overlay agent.

Tests cover:
- Constructor and agent_id configuration
- Signal subscription setup (sla_guardian_agent, delay_response_agent)
- Module-level constants (CONFIDENCE_THRESHOLD, DEFAULT_DELIVERY_COOLDOWN_MINUTES)
- evaluate() with empty signals
- evaluate() filters signals below confidence threshold (Req 7.2)
- evaluate() generates breach communication proposals (Req 7.3)
- evaluate() respects per-delivery cooldown (Req 7.4)
- evaluate() detects recovery conditions for flagged deliveries (Req 7.8)
- _compute_priority() scoring (Req 7.7)
- _select_channel() channel selection by severity
- _cleanup_flagged() removes old entries
- SLA-tier personalization (Req 16.1, 16.2, 16.3)
  - _get_sla_tier() lookup from signal context
  - _select_channel_for_tier() override for premium/enterprise
  - _select_template_for_tier() tier-based template selection
  - _select_recovery_template_for_tier() tier-based recovery template
  - _get_recovery_apology() tier-appropriate apology messages
  - evaluate() includes sla_tier in proposal parameters
  - evaluate() overrides channel for premium/enterprise tiers
  - evaluate() generates recovery with updated ETA and apology

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 16.1, 16.2, 16.3
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.overlay.data_contracts import (
    InterventionProposal,
    RiskClass,
    RiskSignal,
    Severity,
)
from Agents.overlay.customer_promise import (
    CONFIDENCE_THRESHOLD,
    DEFAULT_DELIVERY_COOLDOWN_MINUTES,
    CustomerPromise,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    entity_id="delivery-1",
    severity=Severity.HIGH,
    confidence=0.9,
    tenant_id="tenant-1",
    source_agent="sla_guardian_agent",
    context=None,
):
    return RiskSignal(
        source_agent=source_agent,
        entity_id=entity_id,
        entity_type="delivery",
        severity=severity,
        confidence=confidence,
        ttl_seconds=300,
        tenant_id=tenant_id,
        context=context or {},
    )


def _make_deps():
    """Create mocked dependencies for the CustomerPromise."""
    signal_bus = MagicMock()
    signal_bus.subscribe = AsyncMock()
    signal_bus.unsubscribe = AsyncMock()
    signal_bus.publish = AsyncMock(return_value=1)

    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": []}}
    )
    es_service.index_document = AsyncMock()

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id")
    activity_log.log = AsyncMock()

    ws_manager = MagicMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock()

    autonomy_config = MagicMock()
    feature_flags = MagicMock()
    feature_flags.is_enabled = AsyncMock(return_value=True)

    return {
        "signal_bus": signal_bus,
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "autonomy_config_service": autonomy_config,
        "feature_flag_service": feature_flags,
    }


def _make_agent(**overrides):
    deps = _make_deps()
    deps.update(overrides)
    return CustomerPromise(**deps), deps


# ---------------------------------------------------------------------------
# Tests: Module constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_confidence_threshold(self):
        assert CONFIDENCE_THRESHOLD == 0.7

    def test_default_delivery_cooldown_minutes(self):
        assert DEFAULT_DELIVERY_COOLDOWN_MINUTES == 30


# ---------------------------------------------------------------------------
# Tests: Constructor
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_agent_id(self):
        agent, _ = _make_agent()
        assert agent.agent_id == "customer_promise"

    def test_subscription_filters(self):
        agent, _ = _make_agent()
        assert len(agent._subscription_specs) == 1
        spec = agent._subscription_specs[0]
        assert spec["message_type"] is RiskSignal
        assert spec["filters"]["source_agent"] == [
            "sla_guardian_agent",
            "delay_response_agent",
        ]

    def test_default_poll_interval(self):
        agent, _ = _make_agent()
        assert agent.poll_interval == 45

    def test_custom_poll_interval(self):
        agent, _ = _make_agent(poll_interval=90)
        assert agent.poll_interval == 90

    def test_default_cooldown(self):
        agent, _ = _make_agent()
        assert agent.cooldown_minutes == DEFAULT_DELIVERY_COOLDOWN_MINUTES

    def test_custom_cooldown(self):
        agent, _ = _make_agent(delivery_cooldown_minutes=60)
        assert agent.cooldown_minutes == 60

    def test_flagged_deliveries_initially_empty(self):
        agent, _ = _make_agent()
        assert agent._flagged_deliveries == {}


# ---------------------------------------------------------------------------
# Tests: evaluate()
# ---------------------------------------------------------------------------


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_empty_signals_returns_empty(self):
        agent, _ = _make_agent()
        result = await agent.evaluate([])
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_low_confidence_signals(self):
        """Req 7.2: Only signals with confidence >= 0.7 generate proposals."""
        agent, _ = _make_agent()
        signal = _make_signal(confidence=0.5, severity=Severity.HIGH)
        result = await agent.evaluate([signal])
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_breach_proposal(self):
        """Req 7.3: Breach proposal includes delivery_id, channel, template, sla_tier."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-42",
            severity=Severity.HIGH,
            confidence=0.85,
        )
        result = await agent.evaluate([signal])

        assert len(result) == 1
        proposal = result[0]
        assert isinstance(proposal, InterventionProposal)
        assert proposal.source_agent == "customer_promise"
        assert proposal.tenant_id == "tenant-1"
        assert proposal.risk_class == RiskClass.MEDIUM
        assert len(proposal.actions) == 1

        action = proposal.actions[0]
        assert action["parameters"]["delivery_id"] == "delivery-42"
        assert action["parameters"]["notification_type"] == "eta_breach_warning"
        assert action["parameters"]["channel"] == "sms"
        assert action["parameters"]["message_template"] == "eta_delay_notification"
        assert action["parameters"]["sla_tier"] == "standard"

    @pytest.mark.asyncio
    async def test_respects_cooldown(self):
        """Req 7.4: No duplicate communications within cooldown period."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="delivery-1", confidence=0.9)

        # First call should produce a proposal
        result1 = await agent.evaluate([signal])
        assert len(result1) == 1

        # Second call should be suppressed by cooldown
        result2 = await agent.evaluate([signal])
        assert len(result2) == 0

    @pytest.mark.asyncio
    async def test_tracks_flagged_deliveries(self):
        """Req 7.8: Breach signals track delivery as flagged."""
        agent, _ = _make_agent()
        signal = _make_signal(entity_id="delivery-99", confidence=0.8)
        await agent.evaluate([signal])

        assert "delivery-99" in agent._flagged_deliveries
        info = agent._flagged_deliveries["delivery-99"]
        assert "flagged_at" in info
        assert info["original_severity"] == "high"

    @pytest.mark.asyncio
    async def test_recovery_detection(self):
        """Req 7.8: Previously flagged delivery with low severity triggers recovery."""
        agent, _ = _make_agent()

        # First: flag the delivery with a high-severity breach
        breach_signal = _make_signal(
            entity_id="delivery-50",
            severity=Severity.HIGH,
            confidence=0.9,
        )
        await agent.evaluate([breach_signal])
        assert "delivery-50" in agent._flagged_deliveries

        # Clear cooldown so recovery can be generated
        agent._cooldown_tracker.clear()

        # Second: send a low-severity signal for the same delivery
        recovery_signal = _make_signal(
            entity_id="delivery-50",
            severity=Severity.LOW,
            confidence=0.8,
        )
        result = await agent.evaluate([recovery_signal])

        assert len(result) == 1
        proposal = result[0]
        params = proposal.actions[0]["parameters"]
        assert params["notification_type"] == "eta_recovery"
        # Standard tier recovery uses severity-based channel (low → push)
        assert params["channel"] == "push"
        assert params["sla_tier"] == "standard"
        assert params["message_template"] == "eta_recovery_notification"
        assert "apology_message" in params
        assert "updated_eta" in params
        assert proposal.risk_class == RiskClass.LOW
        # Delivery should be removed from flagged
        assert "delivery-50" not in agent._flagged_deliveries

    @pytest.mark.asyncio
    async def test_recovery_respects_cooldown(self):
        """Recovery notifications also respect per-delivery cooldown."""
        agent, _ = _make_agent()

        # Flag the delivery
        breach_signal = _make_signal(
            entity_id="delivery-60",
            severity=Severity.HIGH,
            confidence=0.9,
        )
        await agent.evaluate([breach_signal])

        # Don't clear cooldown — recovery should be suppressed
        recovery_signal = _make_signal(
            entity_id="delivery-60",
            severity=Severity.LOW,
            confidence=0.8,
        )
        result = await agent.evaluate([recovery_signal])
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_multiple_signals_different_deliveries(self):
        """Multiple breach signals for different deliveries produce multiple proposals."""
        agent, _ = _make_agent()
        signals = [
            _make_signal(entity_id="d-1", confidence=0.9, severity=Severity.HIGH),
            _make_signal(entity_id="d-2", confidence=0.8, severity=Severity.CRITICAL),
        ]
        result = await agent.evaluate(signals)
        assert len(result) == 2

        delivery_ids = {
            p.actions[0]["parameters"]["delivery_id"] for p in result
        }
        assert delivery_ids == {"d-1", "d-2"}

    @pytest.mark.asyncio
    async def test_proposal_confidence_matches_signal(self):
        agent, _ = _make_agent()
        signal = _make_signal(confidence=0.85)
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert result[0].confidence == 0.85

    @pytest.mark.asyncio
    async def test_boundary_confidence_exactly_threshold(self):
        """Signal with confidence exactly 0.7 should generate a proposal."""
        agent, _ = _make_agent()
        signal = _make_signal(confidence=0.7, severity=Severity.MEDIUM)
        result = await agent.evaluate([signal])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_boundary_confidence_just_below_threshold(self):
        """Signal with confidence just below 0.7 should not generate a proposal."""
        agent, _ = _make_agent()
        signal = _make_signal(confidence=0.6999, severity=Severity.MEDIUM)
        result = await agent.evaluate([signal])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Tests: _compute_priority()
# ---------------------------------------------------------------------------


class TestComputePriority:
    def test_default_values(self):
        """Default tier_weight=1, delivery_value=1.0, severity medium=2."""
        agent, _ = _make_agent()
        signal = _make_signal(severity=Severity.MEDIUM)
        assert agent._compute_priority(signal) == 2  # 1 * 1.0 * 2

    def test_severity_weights(self):
        agent, _ = _make_agent()
        expected = {
            Severity.LOW: 1,
            Severity.MEDIUM: 2,
            Severity.HIGH: 3,
            Severity.CRITICAL: 4,
        }
        for sev, weight in expected.items():
            signal = _make_signal(severity=sev)
            assert agent._compute_priority(signal) == weight

    def test_with_customer_tier_weight(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            severity=Severity.HIGH,
            context={"customer_tier_weight": 3},
        )
        # 3 * 1.0 * 3 = 9
        assert agent._compute_priority(signal) == 9

    def test_with_delivery_value(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            severity=Severity.MEDIUM,
            context={"delivery_value": 5.0},
        )
        # 1 * 5.0 * 2 = 10
        assert agent._compute_priority(signal) == 10

    def test_with_all_context_values(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            severity=Severity.CRITICAL,
            context={"customer_tier_weight": 2, "delivery_value": 10.0},
        )
        # 2 * 10.0 * 4 = 80
        assert agent._compute_priority(signal) == 80

    def test_result_is_int(self):
        agent, _ = _make_agent()
        signal = _make_signal(
            severity=Severity.HIGH,
            context={"customer_tier_weight": 2, "delivery_value": 3.5},
        )
        result = agent._compute_priority(signal)
        assert isinstance(result, int)
        # 2 * 3.5 * 3 = 21
        assert result == 21


# ---------------------------------------------------------------------------
# Tests: _select_channel()
# ---------------------------------------------------------------------------


class TestSelectChannel:
    def test_critical_returns_sms(self):
        assert CustomerPromise._select_channel(Severity.CRITICAL) == "sms"

    def test_high_returns_sms(self):
        assert CustomerPromise._select_channel(Severity.HIGH) == "sms"

    def test_medium_returns_email(self):
        assert CustomerPromise._select_channel(Severity.MEDIUM) == "email"

    def test_low_returns_push(self):
        assert CustomerPromise._select_channel(Severity.LOW) == "push"


# ---------------------------------------------------------------------------
# Tests: _cleanup_flagged()
# ---------------------------------------------------------------------------


class TestCleanupFlagged:
    def test_removes_old_entries(self):
        agent, _ = _make_agent()
        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        agent._flagged_deliveries["old-delivery"] = {
            "flagged_at": old_time,
            "original_severity": "high",
        }
        agent._flagged_deliveries["recent-delivery"] = {
            "flagged_at": datetime.now(timezone.utc),
            "original_severity": "medium",
        }

        agent._cleanup_flagged()

        assert "old-delivery" not in agent._flagged_deliveries
        assert "recent-delivery" in agent._flagged_deliveries

    def test_keeps_entries_within_24_hours(self):
        agent, _ = _make_agent()
        recent_time = datetime.now(timezone.utc) - timedelta(hours=23)
        agent._flagged_deliveries["recent"] = {
            "flagged_at": recent_time,
            "original_severity": "low",
        }

        agent._cleanup_flagged()

        assert "recent" in agent._flagged_deliveries

    def test_empty_flagged_no_error(self):
        agent, _ = _make_agent()
        agent._cleanup_flagged()  # Should not raise
        assert agent._flagged_deliveries == {}


# ---------------------------------------------------------------------------
# Tests: _get_sla_tier() (Req 16.1)
# ---------------------------------------------------------------------------


class TestGetSlaTier:
    def test_returns_tier_from_context(self):
        signal = _make_signal(context={"sla_tier": "premium"})
        assert CustomerPromise._get_sla_tier(signal) == "premium"

    def test_returns_enterprise_from_context(self):
        signal = _make_signal(context={"sla_tier": "enterprise"})
        assert CustomerPromise._get_sla_tier(signal) == "enterprise"

    def test_defaults_to_standard_when_missing(self):
        signal = _make_signal(context={})
        assert CustomerPromise._get_sla_tier(signal) == "standard"

    def test_defaults_to_standard_when_no_context(self):
        signal = _make_signal()
        assert CustomerPromise._get_sla_tier(signal) == "standard"


# ---------------------------------------------------------------------------
# Tests: _select_channel_for_tier() (Req 16.3)
# ---------------------------------------------------------------------------


class TestSelectChannelForTier:
    """Req 16.3: Premium/enterprise override channel regardless of severity."""

    def test_enterprise_always_whatsapp_critical(self):
        assert CustomerPromise._select_channel_for_tier(Severity.CRITICAL, "enterprise") == "whatsapp"

    def test_enterprise_always_whatsapp_high(self):
        assert CustomerPromise._select_channel_for_tier(Severity.HIGH, "enterprise") == "whatsapp"

    def test_enterprise_always_whatsapp_medium(self):
        assert CustomerPromise._select_channel_for_tier(Severity.MEDIUM, "enterprise") == "whatsapp"

    def test_enterprise_always_whatsapp_low(self):
        assert CustomerPromise._select_channel_for_tier(Severity.LOW, "enterprise") == "whatsapp"

    def test_premium_always_sms_critical(self):
        assert CustomerPromise._select_channel_for_tier(Severity.CRITICAL, "premium") == "sms"

    def test_premium_always_sms_high(self):
        assert CustomerPromise._select_channel_for_tier(Severity.HIGH, "premium") == "sms"

    def test_premium_always_sms_medium(self):
        assert CustomerPromise._select_channel_for_tier(Severity.MEDIUM, "premium") == "sms"

    def test_premium_always_sms_low(self):
        assert CustomerPromise._select_channel_for_tier(Severity.LOW, "premium") == "sms"

    def test_standard_critical_returns_sms(self):
        assert CustomerPromise._select_channel_for_tier(Severity.CRITICAL, "standard") == "sms"

    def test_standard_high_returns_sms(self):
        assert CustomerPromise._select_channel_for_tier(Severity.HIGH, "standard") == "sms"

    def test_standard_medium_returns_email(self):
        assert CustomerPromise._select_channel_for_tier(Severity.MEDIUM, "standard") == "email"

    def test_standard_low_returns_push(self):
        assert CustomerPromise._select_channel_for_tier(Severity.LOW, "standard") == "push"


# ---------------------------------------------------------------------------
# Tests: _select_template_for_tier() (Req 16.1)
# ---------------------------------------------------------------------------


class TestSelectTemplateForTier:
    def test_enterprise_template(self):
        assert CustomerPromise._select_template_for_tier("enterprise") == "eta_delay_enterprise"

    def test_premium_template(self):
        assert CustomerPromise._select_template_for_tier("premium") == "eta_delay_premium"

    def test_standard_template(self):
        assert CustomerPromise._select_template_for_tier("standard") == "eta_delay_notification"

    def test_unknown_tier_defaults_to_standard(self):
        assert CustomerPromise._select_template_for_tier("unknown") == "eta_delay_notification"


# ---------------------------------------------------------------------------
# Tests: _select_recovery_template_for_tier() (Req 16.2)
# ---------------------------------------------------------------------------


class TestSelectRecoveryTemplateForTier:
    def test_enterprise_recovery_template(self):
        assert CustomerPromise._select_recovery_template_for_tier("enterprise") == "eta_recovery_enterprise"

    def test_premium_recovery_template(self):
        assert CustomerPromise._select_recovery_template_for_tier("premium") == "eta_recovery_premium"

    def test_standard_recovery_template(self):
        assert CustomerPromise._select_recovery_template_for_tier("standard") == "eta_recovery_notification"

    def test_unknown_tier_defaults_to_standard(self):
        assert CustomerPromise._select_recovery_template_for_tier("unknown") == "eta_recovery_notification"


# ---------------------------------------------------------------------------
# Tests: _get_recovery_apology() (Req 16.2)
# ---------------------------------------------------------------------------


class TestGetRecoveryApology:
    def test_enterprise_apology(self):
        apology = CustomerPromise._get_recovery_apology("enterprise")
        assert "enterprise partner" in apology
        assert "dedicated account team" in apology

    def test_premium_apology(self):
        apology = CustomerPromise._get_recovery_apology("premium")
        assert "premium customer" in apology
        assert "prioritized" in apology

    def test_standard_apology(self):
        apology = CustomerPromise._get_recovery_apology("standard")
        assert "back on schedule" in apology

    def test_unknown_tier_gets_standard_apology(self):
        apology = CustomerPromise._get_recovery_apology("unknown")
        assert "back on schedule" in apology


# ---------------------------------------------------------------------------
# Tests: evaluate() SLA-tier integration (Req 16.1, 16.2, 16.3)
# ---------------------------------------------------------------------------


class TestEvaluateSlaTier:
    """Integration tests for SLA-tier personalization in evaluate()."""

    @pytest.mark.asyncio
    async def test_premium_breach_uses_sms_regardless_of_severity(self):
        """Req 16.3: Premium tier always uses SMS even for low severity."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-p1",
            severity=Severity.MEDIUM,
            confidence=0.9,
            context={"sla_tier": "premium"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert params["channel"] == "sms"
        assert params["sla_tier"] == "premium"
        assert params["message_template"] == "eta_delay_premium"

    @pytest.mark.asyncio
    async def test_enterprise_breach_uses_whatsapp_regardless_of_severity(self):
        """Req 16.3: Enterprise tier always uses WhatsApp even for low severity."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-e1",
            severity=Severity.LOW,
            confidence=0.8,
            context={"sla_tier": "enterprise"},
        )
        # LOW severity for non-flagged delivery is a breach signal
        result = await agent.evaluate([signal])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert params["channel"] == "whatsapp"
        assert params["sla_tier"] == "enterprise"
        assert params["message_template"] == "eta_delay_enterprise"

    @pytest.mark.asyncio
    async def test_standard_breach_uses_severity_based_channel(self):
        """Req 16.3: Standard tier uses severity-based channel selection."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-s1",
            severity=Severity.MEDIUM,
            confidence=0.9,
            context={"sla_tier": "standard"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert params["channel"] == "email"
        assert params["sla_tier"] == "standard"
        assert params["message_template"] == "eta_delay_notification"

    @pytest.mark.asyncio
    async def test_sla_tier_included_in_breach_proposal_params(self):
        """Req 16.1: sla_tier is included in proposal parameters."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-t1",
            severity=Severity.HIGH,
            confidence=0.9,
            context={"sla_tier": "premium"},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert result[0].actions[0]["parameters"]["sla_tier"] == "premium"

    @pytest.mark.asyncio
    async def test_sla_tier_stored_in_flagged_deliveries(self):
        """Flagged deliveries track the SLA tier for recovery use."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-f1",
            severity=Severity.HIGH,
            confidence=0.9,
            context={"sla_tier": "enterprise"},
        )
        await agent.evaluate([signal])
        assert agent._flagged_deliveries["delivery-f1"]["sla_tier"] == "enterprise"

    @pytest.mark.asyncio
    async def test_premium_recovery_uses_sms_channel(self):
        """Req 16.2, 16.3: Premium recovery uses SMS channel."""
        agent, _ = _make_agent()

        # Flag with premium tier
        breach = _make_signal(
            entity_id="delivery-pr1",
            severity=Severity.HIGH,
            confidence=0.9,
            context={"sla_tier": "premium"},
        )
        await agent.evaluate([breach])
        agent._cooldown_tracker.clear()

        # Recovery signal
        recovery = _make_signal(
            entity_id="delivery-pr1",
            severity=Severity.LOW,
            confidence=0.8,
            context={"sla_tier": "premium", "updated_eta": "2025-01-15T14:00:00Z"},
        )
        result = await agent.evaluate([recovery])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert params["channel"] == "sms"
        assert params["sla_tier"] == "premium"
        assert params["message_template"] == "eta_recovery_premium"
        assert "premium customer" in params["apology_message"]
        assert params["updated_eta"] == "2025-01-15T14:00:00Z"

    @pytest.mark.asyncio
    async def test_enterprise_recovery_uses_whatsapp_channel(self):
        """Req 16.2, 16.3: Enterprise recovery uses WhatsApp channel."""
        agent, _ = _make_agent()

        # Flag with enterprise tier
        breach = _make_signal(
            entity_id="delivery-er1",
            severity=Severity.CRITICAL,
            confidence=0.95,
            context={"sla_tier": "enterprise"},
        )
        await agent.evaluate([breach])
        agent._cooldown_tracker.clear()

        # Recovery signal
        recovery = _make_signal(
            entity_id="delivery-er1",
            severity=Severity.LOW,
            confidence=0.85,
            context={"sla_tier": "enterprise", "updated_eta": "2025-01-15T16:00:00Z"},
        )
        result = await agent.evaluate([recovery])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert params["channel"] == "whatsapp"
        assert params["sla_tier"] == "enterprise"
        assert params["message_template"] == "eta_recovery_enterprise"
        assert "enterprise partner" in params["apology_message"]
        assert params["updated_eta"] == "2025-01-15T16:00:00Z"

    @pytest.mark.asyncio
    async def test_standard_recovery_uses_push_channel(self):
        """Req 16.2: Standard recovery uses severity-based channel (push for low)."""
        agent, _ = _make_agent()

        # Flag with standard tier (default)
        breach = _make_signal(
            entity_id="delivery-sr1",
            severity=Severity.HIGH,
            confidence=0.9,
        )
        await agent.evaluate([breach])
        agent._cooldown_tracker.clear()

        # Recovery signal
        recovery = _make_signal(
            entity_id="delivery-sr1",
            severity=Severity.LOW,
            confidence=0.8,
            context={"updated_eta": "2025-01-15T12:00:00Z"},
        )
        result = await agent.evaluate([recovery])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        assert params["channel"] == "push"
        assert params["sla_tier"] == "standard"
        assert params["message_template"] == "eta_recovery_notification"
        assert "back on schedule" in params["apology_message"]
        assert params["updated_eta"] == "2025-01-15T12:00:00Z"

    @pytest.mark.asyncio
    async def test_recovery_includes_updated_eta_from_context(self):
        """Req 16.2: Recovery notification includes updated ETA."""
        agent, _ = _make_agent()

        breach = _make_signal(
            entity_id="delivery-eta1",
            severity=Severity.HIGH,
            confidence=0.9,
        )
        await agent.evaluate([breach])
        agent._cooldown_tracker.clear()

        recovery = _make_signal(
            entity_id="delivery-eta1",
            severity=Severity.LOW,
            confidence=0.8,
            context={"updated_eta": "2025-06-15T10:30:00Z"},
        )
        result = await agent.evaluate([recovery])
        assert len(result) == 1
        assert result[0].actions[0]["parameters"]["updated_eta"] == "2025-06-15T10:30:00Z"

    @pytest.mark.asyncio
    async def test_recovery_updated_eta_none_when_not_in_context(self):
        """Recovery notification has updated_eta=None when not in signal context."""
        agent, _ = _make_agent()

        breach = _make_signal(
            entity_id="delivery-noeta",
            severity=Severity.HIGH,
            confidence=0.9,
        )
        await agent.evaluate([breach])
        agent._cooldown_tracker.clear()

        recovery = _make_signal(
            entity_id="delivery-noeta",
            severity=Severity.LOW,
            confidence=0.8,
        )
        result = await agent.evaluate([recovery])
        assert len(result) == 1
        assert result[0].actions[0]["parameters"]["updated_eta"] is None

    @pytest.mark.asyncio
    async def test_recovery_uses_flagged_tier_when_signal_has_no_tier(self):
        """Recovery uses the SLA tier stored at breach time if signal context lacks it."""
        agent, _ = _make_agent()

        # Flag with enterprise tier
        breach = _make_signal(
            entity_id="delivery-ft1",
            severity=Severity.HIGH,
            confidence=0.9,
            context={"sla_tier": "enterprise"},
        )
        await agent.evaluate([breach])
        agent._cooldown_tracker.clear()

        # Recovery signal without sla_tier in context
        recovery = _make_signal(
            entity_id="delivery-ft1",
            severity=Severity.LOW,
            confidence=0.8,
            context={},
        )
        result = await agent.evaluate([recovery])
        assert len(result) == 1
        params = result[0].actions[0]["parameters"]
        # Should use the enterprise tier from flagged data
        assert params["sla_tier"] == "enterprise"
        assert params["channel"] == "whatsapp"
        assert params["message_template"] == "eta_recovery_enterprise"

    @pytest.mark.asyncio
    async def test_default_sla_tier_when_not_in_context(self):
        """When no sla_tier in context, defaults to standard."""
        agent, _ = _make_agent()
        signal = _make_signal(
            entity_id="delivery-def1",
            severity=Severity.HIGH,
            confidence=0.9,
            context={},
        )
        result = await agent.evaluate([signal])
        assert len(result) == 1
        assert result[0].actions[0]["parameters"]["sla_tier"] == "standard"
