"""
Unit tests for the Drift Detector order-mode extension.

Covers:
- Channel with no drift adapter → ``drift_api_unavailable`` and other channels continue
- Channel with an adapter + no divergences → clean result
- Channel with all three divergence shapes → three DivergentRecord entries
- Threshold breach emits the ``orders_drift_alert_total`` metric and a WARN log
- Shipment/rider comparison preserved during deprecation window with separate
  entity_type="driver" and entity_type="order" buckets in the output

Validates: Requirements 7.1.1, 7.1.2, 7.1.3, 7.1.4, 7.1.5, 10.2.1
"""

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Prevent the real ElasticsearchService from connecting on import
_mock_es_module = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.services.drift_detector import (  # noqa: E402
    DriftDetector,
    DriftResult,
    DriftSourceAdapter,
    DriftSourceRegistry,
    DivergentRecord,
    orders_drift_alert_total,
)
from ops.services.ops_es_service import OpsElasticsearchService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    """Return a mock Settings object with Dinee API fields."""
    s = MagicMock()
    s.dinee_api_base_url = overrides.get("dinee_api_base_url", "https://api.dinee.test")
    s.dinee_api_key = overrides.get("dinee_api_key", "test-key-123")
    return s


def _make_ops_es(shipment_hits=None, rider_hits=None, order_hits=None):
    """Return a mock OpsElasticsearchService with canned search results."""
    mock_es = MagicMock(spec=OpsElasticsearchService)
    mock_client = MagicMock()

    def _search_side_effect(index, body, scroll="2m"):
        if index == OpsElasticsearchService.SHIPMENTS_CURRENT:
            hits = shipment_hits or []
        elif index == OpsElasticsearchService.RIDERS_CURRENT:
            hits = rider_hits or []
        elif index == "fuel_orders_current":
            hits = order_hits or []
        else:
            hits = []
        return {
            "_scroll_id": "scroll_1",
            "hits": {
                "hits": [
                    {
                        "_id": h.get("shipment_id", h.get("rider_id", h.get("order_id", "x"))),
                        "_source": h,
                    }
                    for h in hits
                ],
            },
        }

    mock_client.search.side_effect = _search_side_effect
    mock_client.scroll.return_value = {"hits": {"hits": []}}
    mock_client.clear_scroll.return_value = {}
    mock_es.client = mock_client
    return mock_es


class FakeChannel:
    """Minimal fake intake channel for testing."""

    def __init__(self, channel_id: str, channel_type: str):
        self.channel_id = channel_id
        self.channel_type = channel_type
        self.tenant_id = "tenant-1"
        self.enabled = True


class FakeDriftSourceAdapter:
    """A fake drift source adapter that returns canned upstream orders."""

    def __init__(self, orders: List[Dict[str, Any]]):
        self._orders = orders

    async def fetch_upstream_orders(
        self,
        channel: Any,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        return self._orders


class FailingDriftSourceAdapter:
    """A drift source adapter that raises on fetch."""

    async def fetch_upstream_orders(
        self,
        channel: Any,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        raise ConnectionError("upstream unavailable")


def _make_intake_channel_repo(channels: List[FakeChannel]):
    """Return a mock intake channel repository."""
    repo = AsyncMock()
    repo.list_for_tenant = AsyncMock(return_value=channels)
    return repo


# ---------------------------------------------------------------------------
# Tests: DriftSourceRegistry
# ---------------------------------------------------------------------------


class TestDriftSourceRegistry:
    """Unit tests for the DriftSourceRegistry."""

    def test_get_or_none_returns_none_for_unregistered(self):
        registry = DriftSourceRegistry()
        assert registry.get_or_none("voice") is None

    def test_register_and_get(self):
        registry = DriftSourceRegistry()
        adapter = FakeDriftSourceAdapter([])
        registry.register("edi", adapter)
        assert registry.get_or_none("edi") is adapter

    def test_registered_types(self):
        registry = DriftSourceRegistry()
        registry.register("edi", FakeDriftSourceAdapter([]))
        registry.register("csv", FakeDriftSourceAdapter([]))
        assert sorted(registry.registered_types) == ["csv", "edi"]


# ---------------------------------------------------------------------------
# Tests: Channel with no drift adapter → drift_api_unavailable
# ---------------------------------------------------------------------------


class TestChannelNoDriftAdapter:
    """Req 7.1.1 — channels without a registered adapter emit
    drift_api_unavailable and other channels continue."""

    @pytest.mark.asyncio
    async def test_no_adapter_emits_drift_api_unavailable(self):
        """Channel with no registered adapter → drift_api_unavailable."""
        channels = [
            FakeChannel("ch-voice-1", "voice"),
            FakeChannel("ch-edi-1", "edi"),
        ]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()
        # Register adapter only for edi, not voice
        registry.register("edi", FakeDriftSourceAdapter([]))

        detector = DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        # Voice channel has no adapter → drift_api_unavailable
        assert result.channel_statuses["ch-voice-1"] == "drift_api_unavailable"
        # EDI channel has adapter → ok (no divergences)
        assert result.channel_statuses["ch-edi-1"] == "ok"

    @pytest.mark.asyncio
    async def test_other_channels_continue_after_unavailable(self):
        """Even when one channel has no adapter, other channels are processed."""
        channels = [
            FakeChannel("ch-no-adapter", "voice"),
            FakeChannel("ch-with-adapter", "edi"),
        ]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()

        # EDI adapter returns one order that matches ES
        edi_adapter = FakeDriftSourceAdapter([
            {"order_id": "ord_1", "status": "placed"},
        ])
        registry.register("edi", edi_adapter)

        ops_es = _make_ops_es(order_hits=[
            {"order_id": "ord_1", "status": "placed", "intake_channel_id": "ch-with-adapter"},
        ])

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        assert result.channel_statuses["ch-no-adapter"] == "drift_api_unavailable"
        assert result.channel_statuses["ch-with-adapter"] == "ok"
        assert len(result.divergent_orders) == 0

    @pytest.mark.asyncio
    async def test_adapter_fetch_failure_emits_unavailable(self):
        """When adapter.fetch_upstream_orders raises, treat as unavailable."""
        channels = [FakeChannel("ch-failing", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()
        registry.register("edi", FailingDriftSourceAdapter())

        detector = DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        assert result.channel_statuses["ch-failing"] == "drift_api_unavailable"


# ---------------------------------------------------------------------------
# Tests: Channel with adapter + no divergences → clean result
# ---------------------------------------------------------------------------


class TestChannelCleanResult:
    """Req 7.1.2 — channel with adapter and no divergences → clean result."""

    @pytest.mark.asyncio
    async def test_no_divergences_clean_result(self):
        """When upstream and ES match perfectly, result is clean."""
        channels = [FakeChannel("ch-edi-1", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()

        upstream_orders = [
            {"order_id": "ord_1", "status": "placed"},
            {"order_id": "ord_2", "status": "delivered"},
        ]
        registry.register("edi", FakeDriftSourceAdapter(upstream_orders))

        es_orders = [
            {"order_id": "ord_1", "status": "placed", "intake_channel_id": "ch-edi-1"},
            {"order_id": "ord_2", "status": "delivered", "intake_channel_id": "ch-edi-1"},
        ]
        ops_es = _make_ops_es(order_hits=es_orders)

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        assert result.channel_statuses["ch-edi-1"] == "ok"
        assert len(result.divergent_orders) == 0


# ---------------------------------------------------------------------------
# Tests: All three divergence shapes
# ---------------------------------------------------------------------------


class TestThreeDivergenceShapes:
    """Req 7.1.2, 7.1.3 — three divergence shapes produce three
    DivergentRecord entries."""

    @pytest.mark.asyncio
    async def test_all_three_divergence_shapes(self):
        """missing-upstream, missing-runsheet, status-mismatch all detected."""
        channels = [FakeChannel("ch-edi-1", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()

        # Upstream has ord_1 (status mismatch) and ord_2 (missing in ES)
        upstream_orders = [
            {"order_id": "ord_1", "status": "in_transit"},
            {"order_id": "ord_2", "status": "placed"},
        ]
        registry.register("edi", FakeDriftSourceAdapter(upstream_orders))

        # ES has ord_1 (status mismatch) and ord_3 (missing upstream)
        es_orders = [
            {"order_id": "ord_1", "status": "delivered", "intake_channel_id": "ch-edi-1"},
            {"order_id": "ord_3", "status": "scheduled", "intake_channel_id": "ch-edi-1"},
        ]
        ops_es = _make_ops_es(order_hits=es_orders)

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        assert len(result.divergent_orders) == 3

        # Verify each divergence shape
        by_id = {r["entity_id"]: r for r in result.divergent_orders}

        # Status mismatch: ord_1
        assert by_id["ord_1"]["entity_type"] == "order"
        assert by_id["ord_1"]["field"] == "status"
        assert by_id["ord_1"]["expected"] == "in_transit"
        assert by_id["ord_1"]["actual"] == "delivered"

        # Missing in Runsheet: ord_2
        assert by_id["ord_2"]["entity_type"] == "order"
        assert by_id["ord_2"]["field"] == "presence"
        assert by_id["ord_2"]["expected"] == "exists"
        assert by_id["ord_2"]["actual"] == "missing"

        # Missing upstream: ord_3
        assert by_id["ord_3"]["entity_type"] == "order"
        assert by_id["ord_3"]["field"] == "presence"
        assert by_id["ord_3"]["expected"] == "missing"
        assert by_id["ord_3"]["actual"] == "exists"


# ---------------------------------------------------------------------------
# Tests: Threshold breach emits alert metric and WARN log
# ---------------------------------------------------------------------------


class TestThresholdBreachAlert:
    """Req 7.1.4 — threshold breach emits orders_drift_alert_total and WARN."""

    @pytest.mark.asyncio
    async def test_threshold_breach_emits_metric_and_warn(self, caplog):
        """When drift_percentage > threshold, emit metric + WARN log."""
        channels = [FakeChannel("ch-edi-1", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()

        # Upstream has 2 orders, ES has none → 100% drift
        upstream_orders = [
            {"order_id": "ord_1", "status": "placed"},
            {"order_id": "ord_2", "status": "placed"},
        ]
        registry.register("edi", FakeDriftSourceAdapter(upstream_orders))

        # ES has no orders for this channel
        ops_es = _make_ops_es(order_hits=[])

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            threshold_pct=1.0,  # 1% threshold
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        # Capture the metric value before
        before_value = orders_drift_alert_total.labels(
            tenant_id="tenant-1", channel_id="ch-edi-1"
        )._value.get()

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            with caplog.at_level(logging.WARNING):
                result = await detector.run_detection("tenant-1")

        # Metric incremented
        after_value = orders_drift_alert_total.labels(
            tenant_id="tenant-1", channel_id="ch-edi-1"
        )._value.get()
        assert after_value > before_value

        # WARN log emitted
        assert any(
            "ORDER DRIFT THRESHOLD EXCEEDED" in r.message
            for r in caplog.records
        )

        # Channel status reflects the alert
        assert result.channel_statuses["ch-edi-1"] == "drift_alert"

    @pytest.mark.asyncio
    async def test_no_alert_when_below_threshold(self):
        """When drift is 0%, no alert is emitted."""
        channels = [FakeChannel("ch-edi-1", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()

        upstream_orders = [{"order_id": "ord_1", "status": "placed"}]
        registry.register("edi", FakeDriftSourceAdapter(upstream_orders))

        es_orders = [
            {"order_id": "ord_1", "status": "placed", "intake_channel_id": "ch-edi-1"},
        ]
        ops_es = _make_ops_es(order_hits=es_orders)

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            threshold_pct=1.0,
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        assert result.channel_statuses["ch-edi-1"] == "ok"
        assert len(result.divergent_orders) == 0


# ---------------------------------------------------------------------------
# Tests: Preserved shipment/rider comparison + entity_type buckets
# ---------------------------------------------------------------------------


class TestPreservedLegacyComparison:
    """Req 7.1.5 — shipment/rider comparison preserved during deprecation
    window; separate entity_type="driver" and entity_type="order" buckets."""

    @pytest.mark.asyncio
    async def test_shipment_and_rider_comparison_preserved(self):
        """Legacy shipment + rider drift still runs alongside order drift."""
        channels = [FakeChannel("ch-edi-1", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()
        registry.register("edi", FakeDriftSourceAdapter([
            {"order_id": "ord_1", "status": "placed"},
        ]))

        # ES has the order
        ops_es = _make_ops_es(
            shipment_hits=[{"shipment_id": "S1", "status": "delivered"}],
            rider_hits=[{"rider_id": "R1", "status": "active"}],
            order_hits=[{"order_id": "ord_1", "status": "placed", "intake_channel_id": "ch-edi-1"}],
        )

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            # Dinee has a shipment with status mismatch
            mock_ds.return_value = [{"shipment_id": "S1", "status": "in_transit"}]
            # Dinee has a rider with status mismatch
            mock_dr.return_value = [{"rider_id": "R1", "status": "idle"}]

            result = await detector.run_detection("tenant-1")

        # Shipment divergence preserved (entity_type="shipment")
        assert len(result.divergent_shipments) == 1
        assert result.divergent_shipments[0]["entity_type"] == "shipment"
        assert result.divergent_shipments[0]["entity_id"] == "S1"

        # Rider divergence preserved (entity_type="rider")
        assert len(result.divergent_riders) == 1
        assert result.divergent_riders[0]["entity_type"] == "rider"
        assert result.divergent_riders[0]["entity_id"] == "R1"

        # Order drift runs separately (entity_type="order")
        assert len(result.divergent_orders) == 0  # no divergence for orders
        assert result.channel_statuses["ch-edi-1"] == "ok"

    @pytest.mark.asyncio
    async def test_entity_type_buckets_separate(self):
        """Divergent records use separate entity_type values for each bucket."""
        channels = [FakeChannel("ch-edi-1", "edi")]
        repo = _make_intake_channel_repo(channels)
        registry = DriftSourceRegistry()

        # Upstream has an order missing from ES
        registry.register("edi", FakeDriftSourceAdapter([
            {"order_id": "ord_missing", "status": "placed"},
        ]))

        ops_es = _make_ops_es(
            shipment_hits=[],
            rider_hits=[],
            order_hits=[],
        )

        detector = DriftDetector(
            ops_es=ops_es,
            settings=_make_settings(),
            intake_channel_repo=repo,
            drift_source_registry=registry,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            # Dinee has a shipment missing from ES
            mock_ds.return_value = [{"shipment_id": "S_missing", "status": "pending"}]
            # Dinee has a rider missing from ES
            mock_dr.return_value = [{"rider_id": "R_missing", "status": "active"}]

            result = await detector.run_detection("tenant-1")

        # Verify separate entity_type buckets
        shipment_types = {r["entity_type"] for r in result.divergent_shipments}
        rider_types = {r["entity_type"] for r in result.divergent_riders}
        order_types = {r["entity_type"] for r in result.divergent_orders}

        assert shipment_types == {"shipment"}
        assert rider_types == {"rider"}
        assert order_types == {"order"}

    @pytest.mark.asyncio
    async def test_no_intake_channel_repo_skips_order_drift(self):
        """When intake_channel_repo is None, order drift is skipped gracefully."""
        detector = DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(),
            intake_channel_repo=None,
            drift_source_registry=None,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = []
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        # No order drift results
        assert result.channel_statuses == {}
        assert result.divergent_orders == []


# ---------------------------------------------------------------------------
# Tests: _compare_orders pure logic
# ---------------------------------------------------------------------------


class TestCompareOrdersPure:
    """Unit tests for _compare_orders comparison logic."""

    def _detector(self):
        return DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(),
        )

    def test_no_divergence_when_identical(self):
        source = [{"order_id": "O1", "status": "placed"}]
        es = [{"order_id": "O1", "status": "placed"}]
        result = self._detector()._compare_orders(source, es, "ch-1")
        assert result == []

    def test_missing_runsheet(self):
        source = [{"order_id": "O1", "status": "placed"}]
        es = []
        result = self._detector()._compare_orders(source, es, "ch-1")
        assert len(result) == 1
        assert result[0]["entity_id"] == "O1"
        assert result[0]["field"] == "presence"
        assert result[0]["expected"] == "exists"
        assert result[0]["actual"] == "missing"

    def test_missing_upstream(self):
        source = []
        es = [{"order_id": "O1", "status": "placed"}]
        result = self._detector()._compare_orders(source, es, "ch-1")
        assert len(result) == 1
        assert result[0]["entity_id"] == "O1"
        assert result[0]["field"] == "presence"
        assert result[0]["expected"] == "missing"
        assert result[0]["actual"] == "exists"

    def test_status_mismatch(self):
        source = [{"order_id": "O1", "status": "in_transit"}]
        es = [{"order_id": "O1", "status": "delivered"}]
        result = self._detector()._compare_orders(source, es, "ch-1")
        assert len(result) == 1
        assert result[0]["entity_id"] == "O1"
        assert result[0]["field"] == "status"
        assert result[0]["expected"] == "in_transit"
        assert result[0]["actual"] == "delivered"

    def test_all_three_shapes_combined(self):
        source = [
            {"order_id": "O1", "status": "in_transit"},  # status mismatch
            {"order_id": "O2", "status": "placed"},       # missing in ES
        ]
        es = [
            {"order_id": "O1", "status": "delivered"},    # status mismatch
            {"order_id": "O3", "status": "scheduled"},    # missing upstream
        ]
        result = self._detector()._compare_orders(source, es, "ch-1")
        assert len(result) == 3

    def test_empty_both_sides(self):
        result = self._detector()._compare_orders([], [], "ch-1")
        assert result == []

    def test_entity_type_is_order(self):
        source = [{"order_id": "O1", "status": "placed"}]
        es = []
        result = self._detector()._compare_orders(source, es, "ch-1")
        assert all(r["entity_type"] == "order" for r in result)
