"""
Unit tests for the SLA Guardian Agent.

Tests the SLAGuardianAgent autonomous agent including monitor_cycle,
cooldown enforcement, rider workload evaluation, reassignment proposals,
SLA breach escalation, WebSocket sla_breach broadcasts, and edge cases.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from Agents.autonomous.sla_guardian_agent import (
    SLAGuardianAgent,
    SHIPMENTS_CURRENT_INDEX,
    DEFAULT_SLA_THRESHOLD_MINUTES,
    DEFAULT_MAX_RIDER_SHIPMENTS,
)
from Agents.confirmation_protocol import MutationRequest, MutationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(feature_flags=False):
    """Create mocked dependencies for the agent."""
    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-id-1")

    ws_manager = MagicMock()
    ws_manager.broadcast_event = AsyncMock()
    ws_manager.broadcast_activity = AsyncMock()

    confirmation_protocol = MagicMock()
    confirmation_protocol.process_mutation = AsyncMock(
        return_value=MutationResult(
            executed=True,
            risk_level="high",
            result="Successfully executed reassign_rider",
            confirmation_method="immediate",
        )
    )

    feature_flag_service = None
    if feature_flags:
        feature_flag_service = MagicMock()
        feature_flag_service.is_enabled = AsyncMock(return_value=True)

    return es_service, activity_log, ws_manager, confirmation_protocol, feature_flag_service


def _make_agent(
    feature_flags=False,
    poll_interval=120,
    cooldown_minutes=10,
    sla_threshold_minutes=DEFAULT_SLA_THRESHOLD_MINUTES,
    max_rider_shipments=DEFAULT_MAX_RIDER_SHIPMENTS,
):
    """Create an SLAGuardianAgent with mocked dependencies."""
    es, al, ws, cp, ffs = _make_deps(feature_flags=feature_flags)
    agent = SLAGuardianAgent(
        es_service=es,
        activity_log_service=al,
        ws_manager=ws,
        confirmation_protocol=cp,
        feature_flag_service=ffs,
        poll_interval=poll_interval,
        cooldown_minutes=cooldown_minutes,
        sla_threshold_minutes=sla_threshold_minutes,
        max_rider_shipments=max_rider_shipments,
    )
    return agent


def _shipment(
    shipment_id="SHP-001",
    tenant_id="default",
    rider_id="RIDER-001",
    status="in_transit",
    estimated_delivery=None,
):
    """Create a sample shipment document approaching SLA breach."""
    if estimated_delivery is None:
        # Default: 15 minutes from now (within 30-min threshold)
        estimated_delivery = (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).isoformat()
    return {
        "shipment_id": shipment_id,
        "tenant_id": tenant_id,
        "rider_id": rider_id,
        "status": status,
        "estimated_delivery": estimated_delivery,
        "origin": "Warehouse A",
        "destination": "Customer B",
        "priority": "normal",
    }


def _breached_shipment(
    shipment_id="SHP-002",
    tenant_id="default",
    rider_id="RIDER-001",
):
    """Create a sample shipment that has already breached SLA."""
    return _shipment(
        shipment_id=shipment_id,
        tenant_id=tenant_id,
        rider_id=rider_id,
        estimated_delivery=(
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ).isoformat(),
    )


def _rider(rider_id="RIDER-002", active_shipment_count=1):
    """Create a sample rider document."""
    return {
        "rider_id": rider_id,
        "status": "active",
        "tenant_id": "default",
        "active_shipment_count": active_shipment_count,
    }


def _es_response(docs):
    """Wrap documents in an ES search response structure."""
    return {
        "hits": {
            "hits": [{"_source": doc} for doc in docs],
            "total": {"value": len(docs)},
        }
    }


def _es_count_response(count):
    """Create an ES response with a specific total count (for count queries)."""
    return {
        "hits": {
            "hits": [],
            "total": {"value": count},
        }
    }


# ---------------------------------------------------------------------------
# Tests: __init__
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for agent initialisation."""

    def test_agent_id(self):
        agent = _make_agent()
        assert agent.agent_id == "sla_guardian_agent"

    def test_default_poll_interval(self):
        es, al, ws, cp, ffs = _make_deps()
        agent = SLAGuardianAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent.poll_interval == 120

    def test_default_cooldown(self):
        es, al, ws, cp, ffs = _make_deps()
        agent = SLAGuardianAgent(
            es_service=es,
            activity_log_service=al,
            ws_manager=ws,
            confirmation_protocol=cp,
        )
        assert agent.cooldown_minutes == 10

    def test_default_sla_threshold(self):
        agent = _make_agent()
        assert agent._sla_threshold_minutes == DEFAULT_SLA_THRESHOLD_MINUTES

    def test_default_max_rider_shipments(self):
        agent = _make_agent()
        assert agent._max_rider_shipments == DEFAULT_MAX_RIDER_SHIPMENTS

    def test_custom_poll_interval(self):
        agent = _make_agent(poll_interval=60)
        assert agent.poll_interval == 60

    def test_custom_cooldown(self):
        agent = _make_agent(cooldown_minutes=20)
        assert agent.cooldown_minutes == 20

    def test_custom_sla_threshold(self):
        agent = _make_agent(sla_threshold_minutes=45)
        assert agent._sla_threshold_minutes == 45

    def test_custom_max_rider_shipments(self):
        agent = _make_agent(max_rider_shipments=5)
        assert agent._max_rider_shipments == 5

    def test_stores_es_service(self):
        agent = _make_agent()
        assert agent._es is not None

    def test_feature_flags_optional(self):
        agent = _make_agent(feature_flags=False)
        assert agent._feature_flags is None


# ---------------------------------------------------------------------------
# Tests: _is_breached
# ---------------------------------------------------------------------------


class TestIsBreached:
    """Tests for the _is_breached static method."""

    def test_past_delivery_is_breached(self):
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=10)).isoformat()
        assert SLAGuardianAgent._is_breached(past, now) is True

    def test_future_delivery_is_not_breached(self):
        now = datetime.now(timezone.utc)
        future = (now + timedelta(minutes=10)).isoformat()
        assert SLAGuardianAgent._is_breached(future, now) is False

    def test_exact_now_is_breached(self):
        now = datetime.now(timezone.utc)
        assert SLAGuardianAgent._is_breached(now.isoformat(), now) is True

    def test_none_delivery_is_not_breached(self):
        now = datetime.now(timezone.utc)
        assert SLAGuardianAgent._is_breached(None, now) is False

    def test_invalid_string_is_not_breached(self):
        now = datetime.now(timezone.utc)
        assert SLAGuardianAgent._is_breached("not-a-date", now) is False

    def test_z_suffix_handled(self):
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert SLAGuardianAgent._is_breached(past, now) is True


# ---------------------------------------------------------------------------
# Tests: _get_rider_active_shipment_count
# ---------------------------------------------------------------------------


class TestGetRiderActiveShipmentCount:
    """Tests for the _get_rider_active_shipment_count helper."""

    @pytest.mark.asyncio
    async def test_returns_count_from_es(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(
            return_value=_es_count_response(4)
        )

        count = await agent._get_rider_active_shipment_count("RIDER-001", "default")
        assert count == 4

    @pytest.mark.asyncio
    async def test_returns_zero_for_none_rider(self):
        agent = _make_agent()

        count = await agent._get_rider_active_shipment_count(None, "default")
        assert count == 0
        # Should not query ES
        agent._es.search_documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_queries_correct_index(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(
            return_value=_es_count_response(0)
        )

        await agent._get_rider_active_shipment_count("RIDER-001", "default")

        call_args = agent._es.search_documents.call_args
        assert call_args[0][0] == SHIPMENTS_CURRENT_INDEX

    @pytest.mark.asyncio
    async def test_filters_by_rider_and_status(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(
            return_value=_es_count_response(0)
        )

        await agent._get_rider_active_shipment_count("RIDER-X", "tenant-1")

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]

        rider_filter = next(f for f in filters if "rider_id" in f.get("term", {}))
        assert rider_filter["term"]["rider_id"] == "RIDER-X"

        status_filter = next(f for f in filters if "status" in f.get("term", {}))
        assert status_filter["term"]["status"] == "in_transit"

        tenant_filter = next(f for f in filters if "tenant_id" in f.get("term", {}))
        assert tenant_filter["term"]["tenant_id"] == "tenant-1"


# ---------------------------------------------------------------------------
# Tests: _find_less_loaded_rider
# ---------------------------------------------------------------------------


class TestFindLessLoadedRider:
    """Tests for the _find_less_loaded_rider helper."""

    @pytest.mark.asyncio
    async def test_returns_rider_when_found(self):
        agent = _make_agent()
        rider_doc = _rider(rider_id="RIDER-002")
        agent._es.search_documents = AsyncMock(
            return_value=_es_response([rider_doc])
        )

        result = await agent._find_less_loaded_rider("RIDER-001", "default")
        assert result is not None
        assert result["rider_id"] == "RIDER-002"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_rider(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        result = await agent._find_less_loaded_rider("RIDER-001", "default")
        assert result is None

    @pytest.mark.asyncio
    async def test_excludes_current_rider(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent._find_less_loaded_rider("RIDER-001", "default")

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        must_not = query["query"]["bool"]["must_not"]
        excluded = next(f for f in must_not if "rider_id" in f.get("term", {}))
        assert excluded["term"]["rider_id"] == "RIDER-001"

    @pytest.mark.asyncio
    async def test_sorts_by_active_shipment_count_asc(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent._find_less_loaded_rider("RIDER-001", "default")

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        sort = query["sort"]
        assert sort[0]["active_shipment_count"]["order"] == "asc"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — no at-risk shipments
# ---------------------------------------------------------------------------


class TestMonitorCycleNoShipments:
    """Tests for monitor_cycle when no at-risk shipments are found."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_at_risk_shipments(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        detections, actions = await agent.monitor_cycle()
        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_queries_shipments_current_index(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        assert call_args[0][0] == SHIPMENTS_CURRENT_INDEX

    @pytest.mark.asyncio
    async def test_queries_in_transit_status(self):
        agent = _make_agent()
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        status_filter = next(
            f for f in filters if "term" in f and "status" in f["term"]
        )
        assert status_filter["term"]["status"] == "in_transit"

    @pytest.mark.asyncio
    async def test_queries_estimated_delivery_within_threshold(self):
        agent = _make_agent(sla_threshold_minutes=30)
        agent._es.search_documents = AsyncMock(return_value=_es_response([]))

        await agent.monitor_cycle()

        call_args = agent._es.search_documents.call_args
        query = call_args[0][1]
        filters = query["query"]["bool"]["filter"]
        range_filter = next(f for f in filters if "range" in f)
        assert "estimated_delivery" in range_filter["range"]
        assert "lte" in range_filter["range"]["estimated_delivery"]


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — rider overloaded, reassignment proposed
# ---------------------------------------------------------------------------


class TestMonitorCycleReassignment:
    """Tests for monitor_cycle when rider is overloaded and reassignment is proposed."""

    @pytest.mark.asyncio
    async def test_detects_at_risk_shipment(self):
        agent = _make_agent()
        shp = _shipment()

        # Call 1: shipments query
        # Call 2: rider workload count (rider has 4 active → overloaded)
        # Call 3: find less loaded rider
        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(4),
                _es_response([_rider(rider_id="RIDER-002")]),
            ]
        )

        detections, actions = await agent.monitor_cycle()
        assert "SHP-001" in detections

    @pytest.mark.asyncio
    async def test_proposes_reassignment_when_rider_overloaded(self):
        agent = _make_agent(max_rider_shipments=3)
        shp = _shipment()  # future delivery, not breached

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(4),  # rider has 4 > 3
                _es_response([_rider(rider_id="RIDER-002")]),
            ]
        )

        detections, actions = await agent.monitor_cycle()
        reassignment_actions = [a for a in actions if a["action"] == "reassignment_proposed"]
        assert len(reassignment_actions) == 1
        assert reassignment_actions[0]["shipment_id"] == "SHP-001"
        assert reassignment_actions[0]["new_rider_id"] == "RIDER-002"

    @pytest.mark.asyncio
    async def test_calls_confirmation_protocol_for_reassignment(self):
        agent = _make_agent(max_rider_shipments=3)
        shp = _shipment()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(4),
                _es_response([_rider(rider_id="RIDER-002")]),
            ]
        )

        await agent.monitor_cycle()

        # At least one call should be for reassign_rider
        calls = agent._confirmation_protocol.process_mutation.call_args_list
        reassign_calls = [
            c for c in calls
            if c[0][0].tool_name == "reassign_rider"
        ]
        assert len(reassign_calls) == 1
        request = reassign_calls[0][0][0]
        assert isinstance(request, MutationRequest)
        assert request.parameters["shipment_id"] == "SHP-001"
        assert request.parameters["new_rider_id"] == "RIDER-002"
        assert request.agent_id == "sla_guardian_agent"

    @pytest.mark.asyncio
    async def test_no_reassignment_when_rider_not_overloaded(self):
        agent = _make_agent(max_rider_shipments=3)
        shp = _shipment()  # future delivery, not breached

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(2),  # rider has 2 <= 3
            ]
        )

        detections, actions = await agent.monitor_cycle()
        reassignment_actions = [a for a in actions if a["action"] == "reassignment_proposed"]
        assert len(reassignment_actions) == 0

    @pytest.mark.asyncio
    async def test_no_reassignment_when_no_less_loaded_rider(self):
        agent = _make_agent(max_rider_shipments=3)
        shp = _shipment()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(5),  # rider overloaded
                _es_response([]),  # no less loaded rider found
            ]
        )

        detections, actions = await agent.monitor_cycle()
        reassignment_actions = [a for a in actions if a["action"] == "reassignment_proposed"]
        assert len(reassignment_actions) == 0


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — SLA breach escalation
# ---------------------------------------------------------------------------


class TestMonitorCycleEscalation:
    """Tests for monitor_cycle when shipments have breached SLA."""

    @pytest.mark.asyncio
    async def test_escalates_breached_shipment(self):
        agent = _make_agent()
        shp = _breached_shipment()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),  # rider not overloaded
            ]
        )

        detections, actions = await agent.monitor_cycle()
        escalation_actions = [a for a in actions if a["action"] == "escalation"]
        assert len(escalation_actions) == 1
        assert escalation_actions[0]["shipment_id"] == "SHP-002"
        assert escalation_actions[0]["priority"] == "critical"

    @pytest.mark.asyncio
    async def test_calls_escalate_shipment_mutation(self):
        agent = _make_agent()
        shp = _breached_shipment()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        await agent.monitor_cycle()

        calls = agent._confirmation_protocol.process_mutation.call_args_list
        escalate_calls = [
            c for c in calls
            if c[0][0].tool_name == "escalate_shipment"
        ]
        assert len(escalate_calls) == 1
        request = escalate_calls[0][0][0]
        assert request.parameters["shipment_id"] == "SHP-002"
        assert request.parameters["priority"] == "critical"
        assert request.agent_id == "sla_guardian_agent"

    @pytest.mark.asyncio
    async def test_broadcasts_sla_breach_event(self):
        agent = _make_agent()
        shp = _breached_shipment()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        await agent.monitor_cycle()

        agent._ws.broadcast_event.assert_called_once()
        call_args = agent._ws.broadcast_event.call_args
        assert call_args[0][0] == "sla_breach"
        payload = call_args[0][1]
        assert payload["shipment_id"] == "SHP-002"
        assert payload["priority"] == "critical"
        assert payload["rider_id"] == "RIDER-001"
        assert payload["tenant_id"] == "default"

    @pytest.mark.asyncio
    async def test_no_escalation_for_non_breached_shipment(self):
        agent = _make_agent()
        shp = _shipment()  # future delivery, not breached

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),  # rider not overloaded
            ]
        )

        detections, actions = await agent.monitor_cycle()
        escalation_actions = [a for a in actions if a["action"] == "escalation"]
        assert len(escalation_actions) == 0
        agent._ws.broadcast_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_reassignment_and_escalation_for_breached_overloaded(self):
        """When a breached shipment has an overloaded rider, both actions occur."""
        agent = _make_agent(max_rider_shipments=3)
        shp = _breached_shipment()

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(5),  # rider overloaded
                _es_response([_rider(rider_id="RIDER-002")]),  # less loaded rider
            ]
        )

        detections, actions = await agent.monitor_cycle()
        action_types = [a["action"] for a in actions]
        assert "reassignment_proposed" in action_types
        assert "escalation" in action_types


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — cooldown enforcement
# ---------------------------------------------------------------------------


class TestMonitorCycleCooldown:
    """Tests for cooldown enforcement in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_skips_shipment_on_cooldown(self):
        agent = _make_agent()
        shp = _breached_shipment(shipment_id="SHP-002")

        # Pre-set cooldown for the shipment
        agent._set_cooldown("SHP-002")

        agent._es.search_documents = AsyncMock(
            return_value=_es_response([shp])
        )

        detections, actions = await agent.monitor_cycle()
        # Shipment is detected but no action taken
        assert "SHP-002" in detections
        assert len(actions) == 0

    @pytest.mark.asyncio
    async def test_does_not_query_rider_for_cooldown_shipment(self):
        agent = _make_agent()
        shp = _shipment()

        agent._set_cooldown("SHP-001")

        agent._es.search_documents = AsyncMock(
            return_value=_es_response([shp])
        )

        await agent.monitor_cycle()
        # Only the initial shipments query should have been made
        assert agent._es.search_documents.call_count == 1

    @pytest.mark.asyncio
    async def test_processes_shipment_after_cooldown_expires(self):
        agent = _make_agent(cooldown_minutes=10)
        shp = _breached_shipment(shipment_id="SHP-002")

        # Set cooldown in the past (expired)
        agent._cooldown_tracker["SHP-002"] = datetime.now(timezone.utc) - timedelta(
            minutes=15
        )

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),  # rider not overloaded
            ]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(actions) == 1
        assert actions[0]["action"] == "escalation"

    @pytest.mark.asyncio
    async def test_sets_cooldown_after_action(self):
        agent = _make_agent()
        shp = _breached_shipment(shipment_id="SHP-002")

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        await agent.monitor_cycle()
        assert agent._is_on_cooldown("SHP-002") is True

    @pytest.mark.asyncio
    async def test_mixed_cooldown_and_fresh_shipments(self):
        agent = _make_agent()
        shp_cooldown = _breached_shipment(shipment_id="SHP-001")
        shp_fresh = _breached_shipment(shipment_id="SHP-002")

        agent._set_cooldown("SHP-001")

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp_cooldown, shp_fresh]),
                _es_count_response(1),  # rider count for SHP-002
            ]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(detections) == 2
        assert len(actions) == 1
        assert actions[0]["shipment_id"] == "SHP-002"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — tenant_id handling
# ---------------------------------------------------------------------------


class TestMonitorCycleTenantId:
    """Tests for tenant_id handling in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_uses_shipment_tenant_id(self):
        agent = _make_agent()
        shp = _breached_shipment(tenant_id="tenant-42")

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        await agent.monitor_cycle()

        calls = agent._confirmation_protocol.process_mutation.call_args_list
        request = calls[0][0][0]
        assert request.tenant_id == "tenant-42"

    @pytest.mark.asyncio
    async def test_defaults_to_default_tenant(self):
        agent = _make_agent()
        shp = _breached_shipment()
        del shp["tenant_id"]  # Remove tenant_id to test default

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        await agent.monitor_cycle()

        calls = agent._confirmation_protocol.process_mutation.call_args_list
        request = calls[0][0][0]
        assert request.tenant_id == "default"


# ---------------------------------------------------------------------------
# Tests: monitor_cycle — edge cases
# ---------------------------------------------------------------------------


class TestMonitorCycleEdgeCases:
    """Tests for edge cases in monitor_cycle."""

    @pytest.mark.asyncio
    async def test_shipment_with_no_rider_id(self):
        """Shipment with no rider_id should not trigger reassignment check."""
        agent = _make_agent()
        shp = _breached_shipment()
        shp["rider_id"] = None

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
            ]
        )

        detections, actions = await agent.monitor_cycle()
        # Should still escalate (breached) but no reassignment
        escalation_actions = [a for a in actions if a["action"] == "escalation"]
        reassignment_actions = [a for a in actions if a["action"] == "reassignment_proposed"]
        assert len(escalation_actions) == 1
        assert len(reassignment_actions) == 0

    @pytest.mark.asyncio
    async def test_shipment_with_missing_rider_id_key(self):
        """Shipment missing rider_id key entirely should handle gracefully."""
        agent = _make_agent()
        shp = _breached_shipment()
        del shp["rider_id"]

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
            ]
        )

        detections, actions = await agent.monitor_cycle()
        escalation_actions = [a for a in actions if a["action"] == "escalation"]
        assert len(escalation_actions) == 1

    @pytest.mark.asyncio
    async def test_multiple_shipments_processed(self):
        agent = _make_agent()
        shp1 = _breached_shipment(shipment_id="SHP-001")
        shp2 = _breached_shipment(shipment_id="SHP-002")

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp1, shp2]),
                _es_count_response(1),  # rider count for SHP-001
                _es_count_response(1),  # rider count for SHP-002
            ]
        )

        detections, actions = await agent.monitor_cycle()
        assert len(detections) == 2
        escalation_actions = [a for a in actions if a["action"] == "escalation"]
        assert len(escalation_actions) == 2

    @pytest.mark.asyncio
    async def test_confirmation_result_stored_in_action(self):
        """Verify the MutationResult is stored in the action dict."""
        agent = _make_agent()
        shp = _breached_shipment()

        expected_result = MutationResult(
            executed=False,
            approval_id="approval-456",
            risk_level="medium",
            confirmation_method="approval_queue",
        )
        agent._confirmation_protocol.process_mutation = AsyncMock(
            return_value=expected_result
        )
        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        detections, actions = await agent.monitor_cycle()
        escalation_actions = [a for a in actions if a["action"] == "escalation"]
        assert escalation_actions[0]["result"] is expected_result

    @pytest.mark.asyncio
    async def test_sla_breach_payload_includes_estimated_delivery(self):
        """Verify the sla_breach WebSocket payload includes estimated_delivery."""
        agent = _make_agent()
        delivery_time = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        shp = _shipment(
            shipment_id="SHP-003",
            estimated_delivery=delivery_time,
        )

        agent._es.search_documents = AsyncMock(
            side_effect=[
                _es_response([shp]),
                _es_count_response(1),
            ]
        )

        await agent.monitor_cycle()

        payload = agent._ws.broadcast_event.call_args[0][1]
        assert payload["estimated_delivery"] == delivery_time