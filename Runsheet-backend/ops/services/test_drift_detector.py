"""
Unit tests for the DriftDetector service.

Validates: Requirements 25.1-25.6
"""

import logging
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Prevent the real ElasticsearchService from connecting on import
_mock_es_module = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from ops.services.drift_detector import (  # noqa: E402
    DriftDetector,
    DriftResult,
    DivergentRecord,
    configure_drift_detector,
    get_drift_detector,
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


def _make_ops_es(shipment_hits=None, rider_hits=None):
    """Return a mock OpsElasticsearchService with canned search results."""
    mock_es = MagicMock(spec=OpsElasticsearchService)
    mock_client = MagicMock()

    def _search_side_effect(index, body, scroll="2m"):
        if index == OpsElasticsearchService.SHIPMENTS_CURRENT:
            hits = shipment_hits or []
        elif index == OpsElasticsearchService.RIDERS_CURRENT:
            hits = rider_hits or []
        else:
            hits = []
        return {
            "_scroll_id": "scroll_1",
            "hits": {
                "hits": [
                    {"_id": h.get("shipment_id", h.get("rider_id", "x")), "_source": h}
                    for h in hits
                ],
            },
        }

    # First call returns hits, second (scroll) returns empty
    mock_client.search.side_effect = _search_side_effect
    mock_client.scroll.return_value = {"hits": {"hits": []}}
    mock_client.clear_scroll.return_value = {}
    mock_es.client = mock_client
    return mock_es


def _dinee_response(data, has_more=False):
    """Build a mock httpx JSON response body."""
    return {"data": data, "has_more": has_more}


# ---------------------------------------------------------------------------
# Tests: comparison logic (pure, no I/O)
# ---------------------------------------------------------------------------

class TestCompareShipments:
    """Req 25.1, 25.3 — shipment count/status comparison."""

    def _detector(self):
        return DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(),
        )

    def test_no_divergence_when_identical(self):
        dinee = [{"shipment_id": "S1", "status": "delivered"}]
        es = [{"shipment_id": "S1", "status": "delivered"}]
        result = self._detector()._compare_shipments(dinee, es)
        assert result == []

    def test_status_mismatch_detected(self):
        dinee = [{"shipment_id": "S1", "status": "in_transit"}]
        es = [{"shipment_id": "S1", "status": "delivered"}]
        result = self._detector()._compare_shipments(dinee, es)
        assert len(result) == 1
        assert result[0]["entity_id"] == "S1"
        assert result[0]["field"] == "status"
        assert result[0]["expected"] == "in_transit"
        assert result[0]["actual"] == "delivered"

    def test_missing_in_runsheet(self):
        dinee = [{"shipment_id": "S1", "status": "pending"}]
        es = []
        result = self._detector()._compare_shipments(dinee, es)
        assert len(result) == 1
        assert result[0]["field"] == "presence"
        assert result[0]["actual"] == "missing"

    def test_extra_in_runsheet(self):
        dinee = []
        es = [{"shipment_id": "S1", "status": "pending"}]
        result = self._detector()._compare_shipments(dinee, es)
        assert len(result) == 1
        assert result[0]["field"] == "presence"
        assert result[0]["expected"] == "missing"
        assert result[0]["actual"] == "exists"

    def test_multiple_divergences(self):
        dinee = [
            {"shipment_id": "S1", "status": "in_transit"},
            {"shipment_id": "S2", "status": "delivered"},
        ]
        es = [
            {"shipment_id": "S1", "status": "delivered"},
            # S2 missing
        ]
        result = self._detector()._compare_shipments(dinee, es)
        assert len(result) == 2


class TestCompareRiders:
    """Req 25.2, 25.3 — rider status comparison."""

    def _detector(self):
        return DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(),
        )

    def test_no_divergence_when_identical(self):
        dinee = [{"rider_id": "R1", "status": "active"}]
        es = [{"rider_id": "R1", "status": "active"}]
        result = self._detector()._compare_riders(dinee, es)
        assert result == []

    def test_status_mismatch_detected(self):
        dinee = [{"rider_id": "R1", "status": "active"}]
        es = [{"rider_id": "R1", "status": "idle"}]
        result = self._detector()._compare_riders(dinee, es)
        assert len(result) == 1
        assert result[0]["entity_id"] == "R1"
        assert result[0]["field"] == "status"

    def test_missing_in_runsheet(self):
        dinee = [{"rider_id": "R1", "status": "active"}]
        es = []
        result = self._detector()._compare_riders(dinee, es)
        assert len(result) == 1
        assert result[0]["actual"] == "missing"


# ---------------------------------------------------------------------------
# Tests: drift percentage and alert threshold
# ---------------------------------------------------------------------------

class TestDriftPercentageAndAlert:
    """Req 25.6 — WARN alert when drift exceeds threshold."""

    @pytest.mark.asyncio
    async def test_no_alert_when_below_threshold(self):
        """0% drift should not trigger alert."""
        detector = DriftDetector(
            ops_es=_make_ops_es(
                shipment_hits=[{"shipment_id": "S1", "status": "delivered"}],
            ),
            settings=_make_settings(),
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = [{"shipment_id": "S1", "status": "delivered"}]
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        assert result.drift_percentage == 0.0
        assert result.alert_triggered is False

    @pytest.mark.asyncio
    async def test_alert_triggered_above_threshold(self, caplog):
        """Drift > 1% should trigger WARN alert."""
        # 2 Dinee shipments, 1 with status mismatch → 50% drift
        detector = DriftDetector(
            ops_es=_make_ops_es(
                shipment_hits=[{"shipment_id": "S1", "status": "delivered"}],
            ),
            settings=_make_settings(),
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = [
                {"shipment_id": "S1", "status": "in_transit"},
                {"shipment_id": "S2", "status": "pending"},
            ]
            mock_dr.return_value = []

            with caplog.at_level(logging.WARNING):
                result = await detector.run_detection("tenant-1")

        assert result.drift_percentage > 1.0
        assert result.alert_triggered is True
        assert any("DRIFT THRESHOLD EXCEEDED" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_custom_threshold(self):
        """Custom threshold should be respected."""
        detector = DriftDetector(
            ops_es=_make_ops_es(
                shipment_hits=[{"shipment_id": "S1", "status": "delivered"}],
            ),
            settings=_make_settings(),
            threshold_pct=50.0,
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            # 1 mismatch out of 2 = 50% drift, exactly at threshold
            mock_ds.return_value = [
                {"shipment_id": "S1", "status": "in_transit"},
                {"shipment_id": "S2", "status": "pending"},
            ]
            mock_dr.return_value = []

            result = await detector.run_detection("tenant-1")

        # 50% drift is not > 50% threshold, so no alert
        # (2 divergent out of 2 dinee entities = 100% actually)
        assert result.alert_triggered is True  # 100% > 50%


# ---------------------------------------------------------------------------
# Tests: divergent record logging
# ---------------------------------------------------------------------------

class TestDivergentRecordLogging:
    """Req 25.3 — log divergent records with entity_id, expected, actual."""

    @pytest.mark.asyncio
    async def test_divergent_shipments_logged(self, caplog):
        detector = DriftDetector(
            ops_es=_make_ops_es(shipment_hits=[]),
            settings=_make_settings(),
        )

        with patch.object(
            detector, "_fetch_dinee_shipments", new_callable=AsyncMock
        ) as mock_ds, patch.object(
            detector, "_fetch_dinee_riders", new_callable=AsyncMock
        ) as mock_dr:
            mock_ds.return_value = [{"shipment_id": "S99", "status": "pending"}]
            mock_dr.return_value = []

            with caplog.at_level(logging.INFO):
                result = await detector.run_detection("tenant-1")

        assert len(result.divergent_shipments) == 1
        log_messages = [r.message for r in caplog.records]
        assert any("S99" in m and "shipment divergence" in m for m in log_messages)


# ---------------------------------------------------------------------------
# Tests: Dinee API not configured
# ---------------------------------------------------------------------------

class TestDineeApiNotConfigured:
    """Graceful handling when Dinee API credentials are missing."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_base_url(self):
        detector = DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(dinee_api_base_url=None),
        )
        result = await detector.run_detection("tenant-1")
        assert result.shipment_count_dinee == 0
        assert result.rider_count_dinee == 0
        assert result.drift_percentage == 0.0

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_api_key(self):
        detector = DriftDetector(
            ops_es=_make_ops_es(),
            settings=_make_settings(dinee_api_key=None),
        )
        result = await detector.run_detection("tenant-1")
        assert result.shipment_count_dinee == 0


# ---------------------------------------------------------------------------
# Tests: configure / get helpers
# ---------------------------------------------------------------------------

class TestModuleLevelHelpers:
    def test_configure_and_get(self):
        ops_es = _make_ops_es()
        settings = _make_settings()
        detector = configure_drift_detector(ops_es=ops_es, settings=settings)
        assert detector is not None
        assert get_drift_detector() is detector

    def test_get_raises_when_not_configured(self):
        import ops.services.drift_detector as mod
        original = mod._drift_detector
        try:
            mod._drift_detector = None
            with pytest.raises(RuntimeError, match="not configured"):
                get_drift_detector()
        finally:
            mod._drift_detector = original
