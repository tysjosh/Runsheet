"""
Unit tests for :mod:`Agents.autonomous.weather_alert_ingester`.

Covers Task 10.2 of the fuel-ops hardening spec:

* Default poll interval / cooldown / agent id (Requirement 9.1.1).
* Customer-tank aggregation builds the tenant → ZIP footprint and is
  injectable for tests.
* NWS fetch is wired through ``httpx.MockTransport`` — no real network.
* Raw NWS feature → :class:`WeatherAlert` conversion covers severity
  normalization, timestamp parsing, ``activation_status`` derivation, and
  the affected-ZIP intersection.
* End-to-end cycle persists new alerts to the ``weather_alerts`` ES index
  and publishes them on the injected SignalBus.
* Duplicate-alert detection (same ``alert_id``) is idempotent
  (Requirement 9.1.2 and Task 10.2 contract).
* Error paths (bad NWS payload, ES index failure, missing loader result)
  degrade gracefully to an empty action log.

Validates: Requirements 9.1.1, 9.1.2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from Agents.autonomous.weather_alert_ingester import (
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    NWS_ACTIVE_ALERTS_URL,
    WeatherAlertIngester,
    zip_to_state,
)
from fuel.services.fuel_ops_es_mappings import (
    CUSTOMER_TANKS_INDEX,
    WEATHER_ALERTS_INDEX,
)
from fuel.storm_mode_models import WeatherAlert


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_deps() -> Dict[str, Any]:
    """Return mocked base-class dependencies for the ingester."""
    es_service = MagicMock()
    es_service.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"tenants": {"buckets": []}},
        }
    )
    es_service.get_document = AsyncMock(return_value=None)
    es_service.index_document = AsyncMock(return_value=None)

    activity_log = MagicMock()
    activity_log.log_monitoring_cycle = AsyncMock(return_value="log-1")

    ws_manager = MagicMock()
    ws_manager.broadcast_event = AsyncMock()

    confirmation_protocol = MagicMock()

    signal_bus = MagicMock()
    signal_bus.publish = AsyncMock(return_value=1)

    feature_flag_service = MagicMock()
    feature_flag_service.is_enabled = AsyncMock(return_value=True)

    return {
        "es_service": es_service,
        "activity_log_service": activity_log,
        "ws_manager": ws_manager,
        "confirmation_protocol": confirmation_protocol,
        "signal_bus": signal_bus,
        "feature_flag_service": feature_flag_service,
    }


def _build_agent(
    *,
    http_client: Optional[httpx.AsyncClient] = None,
    tenant_footprint_loader=None,
    **overrides: Any,
) -> WeatherAlertIngester:
    deps = _make_deps()
    deps.update(overrides)
    return WeatherAlertIngester(
        es_service=deps["es_service"],
        activity_log_service=deps["activity_log_service"],
        ws_manager=deps["ws_manager"],
        confirmation_protocol=deps["confirmation_protocol"],
        signal_bus=deps["signal_bus"],
        feature_flag_service=deps["feature_flag_service"],
        http_client=http_client,
        tenant_footprint_loader=tenant_footprint_loader,
    )


def _nws_feature(
    *,
    alert_id: str = "urn:oid:2.49.0.1.840.0.abc123",
    event: str = "Winter Storm Warning",
    severity: str = "Severe",
    onset_offset_minutes: int = 0,
    expires_offset_minutes: int = 360,
    status: str = "Actual",
    affected_zips: Optional[List[str]] = None,
    area_desc: str = "Erie County, NY",
    headline: str = "Winter Storm Warning in effect",
    description: str = "Heavy snowfall expected.",
    sender: str = "NWS Buffalo NY",
) -> Dict[str, Any]:
    """Build a realistic NWS GeoJSON feature for tests."""
    onset = _now() + timedelta(minutes=onset_offset_minutes)
    expires = _now() + timedelta(minutes=expires_offset_minutes)
    props: Dict[str, Any] = {
        "id": alert_id,
        "event": event,
        "severity": severity,
        "onset": onset.isoformat(),
        "effective": onset.isoformat(),
        "ends": expires.isoformat(),
        "expires": expires.isoformat(),
        "status": status,
        "headline": headline,
        "description": description,
        "areaDesc": area_desc,
        "senderName": sender,
    }
    if affected_zips is not None:
        props["parameters"] = {"ZIPS": affected_zips}
    return {
        "id": f"https://api.weather.gov/alerts/{alert_id}",
        "type": "Feature",
        "properties": props,
    }


def _mock_transport(features: List[Dict[str, Any]]) -> httpx.MockTransport:
    """Return a MockTransport that serves a GeoJSON FeatureCollection."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/alerts/active"
        return httpx.Response(
            200,
            json={"type": "FeatureCollection", "features": features},
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# ZIP -> state derivation + NWS area query (regression for the HTTP 400 bug
# where ZIP codes were sent as the ``zone`` param, which NWS rejects)
# ---------------------------------------------------------------------------


class TestZipToState:
    """zip_to_state maps US ZIPs to USPS 2-letter states via prefix ranges."""

    def test_maps_known_zips(self):
        assert zip_to_state("75247") == "TX"   # Dallas
        assert zip_to_state("30303") == "GA"   # Atlanta
        assert zip_to_state("77002") == "TX"   # Houston
        assert zip_to_state("14202") == "NY"   # Buffalo
        assert zip_to_state("06001") == "CT"   # Avon
        assert zip_to_state("99501") == "AK"   # Anchorage
        assert zip_to_state("90001") == "CA"   # Los Angeles

    def test_handles_zip_plus_four(self):
        assert zip_to_state("75247-1234") == "TX"

    def test_returns_none_for_malformed(self):
        assert zip_to_state("abcde") is None
        assert zip_to_state("") is None
        assert zip_to_state("ab") is None

    def test_returns_none_for_non_string(self):
        assert zip_to_state(None) is None  # type: ignore[arg-type]
        assert zip_to_state(75247) is None  # type: ignore[arg-type]


class TestNWSAreaQuery:
    """The fetch must query NWS by ``area`` (state) derived from the ZIP
    footprint — never by ``zone=<zip>`` which NWS rejects with HTTP 400."""

    @pytest.mark.asyncio
    async def test_fetch_uses_area_state_not_zone_zip(self):
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": []}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            # Dallas + Atlanta + Houston -> {TX, GA}
            await agent._fetch_nws_alerts("tenant-A", ["75247", "30303", "77002"])

        assert "zone" not in captured["params"], "must not send ZIPs as zone"
        assert "area" in captured["params"], "must filter by area (state)"
        states = set(captured["params"]["area"].split(","))
        assert states == {"TX", "GA"}

    @pytest.mark.asyncio
    async def test_fetch_dedupes_states(self):
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": []}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            # Three TX ZIPs collapse to a single area=TX.
            await agent._fetch_nws_alerts("tenant-A", ["75247", "77002", "75201"])

        assert captured["params"].get("area") == "TX"

    @pytest.mark.asyncio
    async def test_fetch_falls_back_to_unscoped_when_no_state(self):
        """Un-mappable ZIPs must NOT produce a zone/area filter — an unscoped
        active-alerts pull is used and filtered client-side instead."""
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": []}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            await agent._fetch_nws_alerts("tenant-A", ["abcde", "zzzzz"])

        assert "zone" not in captured["params"]
        assert "area" not in captured["params"]


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------


class TestConstructorDefaults:
    """Requirement 9.1.1 — 5-minute poll, stable agent id."""

    def test_agent_id_is_weather_alert_ingester(self):
        agent = _build_agent()
        assert agent.agent_id == "weather_alert_ingester"

    def test_default_poll_interval_is_five_minutes(self):
        agent = _build_agent()
        assert agent.poll_interval == DEFAULT_POLL_INTERVAL_SECONDS == 300

    def test_default_cooldown_is_tracked(self):
        agent = _build_agent()
        assert agent.cooldown_minutes == DEFAULT_COOLDOWN_MINUTES == 5

    def test_es_service_is_required(self):
        with pytest.raises(ValueError):
            WeatherAlertIngester(
                es_service=None,
                activity_log_service=MagicMock(),
                ws_manager=MagicMock(),
                confirmation_protocol=MagicMock(),
            )

    def test_signal_bus_is_optional(self):
        agent = _build_agent(signal_bus=None)
        assert agent._signal_bus is None


# ---------------------------------------------------------------------------
# Tenant footprint loader
# ---------------------------------------------------------------------------


class TestTenantFootprint:
    """Customer_tanks aggregation → {tenant_id: [zip_codes]}."""

    @pytest.mark.asyncio
    async def test_footprint_aggregates_customer_tanks(self):
        deps = _make_deps()
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "aggregations": {
                    "tenants": {
                        "buckets": [
                            {
                                "key": "tenant-A",
                                "doc_count": 3,
                                "zips": {
                                    "buckets": [
                                        {"key": "14202", "doc_count": 2},
                                        {"key": "14203", "doc_count": 1},
                                    ]
                                },
                            },
                            {
                                "key": "tenant-B",
                                "doc_count": 1,
                                "zips": {
                                    "buckets": [
                                        {"key": "02139", "doc_count": 1}
                                    ]
                                },
                            },
                        ]
                    }
                }
            }
        )
        agent = WeatherAlertIngester(
            es_service=deps["es_service"],
            activity_log_service=deps["activity_log_service"],
            ws_manager=deps["ws_manager"],
            confirmation_protocol=deps["confirmation_protocol"],
            signal_bus=deps["signal_bus"],
        )

        footprint = await agent._load_footprint_from_es()

        assert footprint == {
            "tenant-A": ["14202", "14203"],
            "tenant-B": ["02139"],
        }
        deps["es_service"].search_documents.assert_awaited_once()
        call_index = deps["es_service"].search_documents.await_args.args[0]
        assert call_index == CUSTOMER_TANKS_INDEX

    @pytest.mark.asyncio
    async def test_footprint_ignores_tenants_without_zips(self):
        deps = _make_deps()
        deps["es_service"].search_documents = AsyncMock(
            return_value={
                "aggregations": {
                    "tenants": {
                        "buckets": [
                            {
                                "key": "tenant-empty",
                                "doc_count": 0,
                                "zips": {"buckets": []},
                            },
                            {
                                "key": "tenant-C",
                                "doc_count": 2,
                                "zips": {
                                    "buckets": [{"key": "10001", "doc_count": 2}]
                                },
                            },
                        ]
                    }
                }
            }
        )
        agent = _build_agent(
            es_service=deps["es_service"], signal_bus=deps["signal_bus"]
        )

        footprint = await agent._load_footprint_from_es()

        assert "tenant-empty" not in footprint
        assert footprint == {"tenant-C": ["10001"]}

    @pytest.mark.asyncio
    async def test_footprint_falls_back_to_empty_on_es_error(self):
        deps = _make_deps()
        deps["es_service"].search_documents = AsyncMock(
            side_effect=RuntimeError("ES down")
        )
        agent = _build_agent(
            es_service=deps["es_service"], signal_bus=deps["signal_bus"]
        )

        footprint = await agent._load_footprint_from_es()

        assert footprint == {}

    @pytest.mark.asyncio
    async def test_custom_loader_is_used(self):
        async def loader():
            return {"tenant-X": ["99501"]}

        agent = _build_agent(tenant_footprint_loader=loader)
        result = await agent._safe_load_footprint()
        assert result == {"tenant-X": ["99501"]}

    def test_normalize_footprint_rejects_non_dict(self):
        assert WeatherAlertIngester._normalize_footprint([1, 2, 3]) == {}

    def test_normalize_footprint_dedupes_and_strips(self):
        raw = {
            "tenant-A": ["14202", " 14202 ", "", "14203"],
            "  ": ["12345"],  # blank tenant id rejected
            "tenant-B": "12345",  # strings are not iterable as zip lists
        }
        assert WeatherAlertIngester._normalize_footprint(raw) == {
            "tenant-A": ["14202", "14203"],
        }


# ---------------------------------------------------------------------------
# Raw NWS feature → WeatherAlert conversion
# ---------------------------------------------------------------------------


class TestBuildWeatherAlert:
    """Structural mapping of NWS fields to WeatherAlert fields."""

    def test_happy_path_populates_all_core_fields(self):
        agent = _build_agent()
        feature = _nws_feature(
            alert_id="alert-001",
            event="Winter Storm Warning",
            severity="Severe",
        )

        alert = agent._build_weather_alert(
            feature, tenant_id="tenant-A", zip_footprint=["14202"]
        )

        assert isinstance(alert, WeatherAlert)
        assert alert.alert_id == "alert-001"
        assert alert.tenant_id == "tenant-A"
        assert alert.alert_type == "Winter Storm Warning"
        assert alert.severity == "severe"
        assert alert.source == "nws"
        assert alert.headline == "Winter Storm Warning in effect"

    def test_severity_normalization(self):
        agent = _build_agent()
        cases = {
            "Severe": "severe",
            "extreme": "extreme",
            "Moderate": "moderate",
            "MINOR": "minor",
            "unknown_label": "moderate",  # fallback
            None: "moderate",
        }
        for nws_value, expected in cases.items():
            feature = _nws_feature(alert_id=f"alert-{nws_value}", severity=nws_value or "")
            if nws_value is None:
                feature["properties"]["severity"] = None
            alert = agent._build_weather_alert(
                feature, tenant_id="t", zip_footprint=["00001"]
            )
            assert alert.severity == expected

    def test_active_status_when_onset_in_past_and_end_in_future(self):
        agent = _build_agent()
        feature = _nws_feature(
            alert_id="alert-active",
            onset_offset_minutes=-10,
            expires_offset_minutes=120,
        )

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["00001"]
        )

        assert alert.activation_status == "active"

    def test_forecast_status_when_onset_in_future(self):
        agent = _build_agent()
        feature = _nws_feature(
            alert_id="alert-forecast",
            onset_offset_minutes=120,
            expires_offset_minutes=240,
        )

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["00001"]
        )

        assert alert.activation_status == "forecast"

    def test_cleared_status_when_end_in_past(self):
        agent = _build_agent()
        feature = _nws_feature(
            alert_id="alert-cleared",
            onset_offset_minutes=-240,
            expires_offset_minutes=-60,
        )

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["00001"]
        )

        assert alert.activation_status == "cleared"

    def test_cancelled_status_when_upstream_status_cancel(self):
        agent = _build_agent()
        feature = _nws_feature(
            alert_id="alert-cancel",
            onset_offset_minutes=-10,
            expires_offset_minutes=120,
            status="Cancel",
        )

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["00001"]
        )

        assert alert.activation_status == "cancelled"

    def test_affected_zips_intersect_upstream_when_available(self):
        agent = _build_agent()
        feature = _nws_feature(
            alert_id="alert-zips",
            affected_zips=["14202", "99999"],
        )

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["14202", "14203"]
        )

        assert alert.affected_zip_codes == ["14202"]

    def test_affected_zips_falls_back_to_footprint_when_no_upstream(self):
        agent = _build_agent()
        feature = _nws_feature(alert_id="alert-no-zips", affected_zips=None)

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["14202", "14203"]
        )

        assert sorted(alert.affected_zip_codes) == ["14202", "14203"]

    def test_missing_alert_id_raises(self):
        agent = _build_agent()
        feature = {"properties": {"event": "x", "severity": "severe"}}

        with pytest.raises(ValueError, match="alert_id"):
            agent._build_weather_alert(
                feature, tenant_id="t", zip_footprint=["00001"]
            )

    def test_missing_onset_raises(self):
        agent = _build_agent()
        feature = _nws_feature(alert_id="alert-no-onset")
        feature["properties"]["onset"] = None
        feature["properties"]["effective"] = None
        feature["properties"]["sent"] = None

        with pytest.raises(ValueError, match="expected_start_at"):
            agent._build_weather_alert(
                feature, tenant_id="t", zip_footprint=["00001"]
            )

    def test_ingested_at_is_utc_aware(self):
        agent = _build_agent()
        feature = _nws_feature(alert_id="alert-tz")

        alert = agent._build_weather_alert(
            feature, tenant_id="t", zip_footprint=["00001"]
        )

        assert alert.ingested_at.tzinfo is not None


# ---------------------------------------------------------------------------
# NWS fetch (httpx.MockTransport)
# ---------------------------------------------------------------------------


class TestNWSFetch:
    """HTTP plumbing against api.weather.gov."""

    @pytest.mark.asyncio
    async def test_successful_fetch_returns_feature_list(self):
        feature = _nws_feature(alert_id="alert-http-1")
        transport = _mock_transport([feature])
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            result = await agent._fetch_nws_alerts("tenant-A", ["14202"])

        assert len(result) == 1
        assert result[0]["properties"]["id"] == "alert-http-1"

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_http_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            result = await agent._fetch_nws_alerts("tenant-A", ["14202"])

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_non_json_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            result = await agent._fetch_nws_alerts("tenant-A", ["14202"])

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_network_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            result = await agent._fetch_nws_alerts("tenant-A", ["14202"])

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_sends_user_agent_header(self):
        captured: Dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["ua"] = request.headers.get("user-agent")
            captured["accept"] = request.headers.get("accept")
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": []}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            await agent._fetch_nws_alerts("tenant-A", ["14202"])

        assert captured["ua"]
        assert "geo+json" in captured["accept"]

    @pytest.mark.asyncio
    async def test_fetch_hits_configured_endpoint(self):
        called_urls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called_urls.append(str(request.url))
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": []}
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agent = _build_agent(http_client=client)
            await agent._fetch_nws_alerts("tenant-A", ["14202"])

        assert called_urls, "expected at least one HTTP request"
        assert NWS_ACTIVE_ALERTS_URL in called_urls[0]


# ---------------------------------------------------------------------------
# End-to-end monitor_cycle
# ---------------------------------------------------------------------------


class TestMonitorCycle:
    """Full ingestion: footprint → fetch → persist → publish."""

    @pytest.mark.asyncio
    async def test_new_alert_is_persisted_and_published(self):
        feature = _nws_feature(alert_id="alert-new-1")
        transport = _mock_transport([feature])

        async def loader():
            return {"tenant-A": ["14202"]}

        async with httpx.AsyncClient(transport=transport) as client:
            deps = _make_deps()
            agent = WeatherAlertIngester(
                es_service=deps["es_service"],
                activity_log_service=deps["activity_log_service"],
                ws_manager=deps["ws_manager"],
                confirmation_protocol=deps["confirmation_protocol"],
                signal_bus=deps["signal_bus"],
                http_client=client,
                tenant_footprint_loader=loader,
            )

            detections, actions = await agent.monitor_cycle()

        assert detections == ["alert-new-1"]
        assert len(actions) == 1
        assert actions[0]["action"] == "ingested"
        assert actions[0]["published"] is True

        # Persisted to the weather_alerts index with alert_id as the doc id.
        deps["es_service"].index_document.assert_awaited_once()
        call = deps["es_service"].index_document.await_args
        assert call.args[0] == WEATHER_ALERTS_INDEX
        assert call.args[1] == "alert-new-1"
        assert call.args[2]["tenant_id"] == "tenant-A"
        assert call.args[2]["alert_id"] == "alert-new-1"
        assert call.args[2]["source"] == "nws"

        # Published a WeatherAlert to the SignalBus.
        deps["signal_bus"].publish.assert_awaited_once()
        published = deps["signal_bus"].publish.await_args.args[0]
        assert isinstance(published, WeatherAlert)
        assert published.alert_id == "alert-new-1"

    @pytest.mark.asyncio
    async def test_empty_footprint_returns_no_detections_or_actions(self):
        async def loader():
            return {}

        agent = _build_agent(tenant_footprint_loader=loader)

        detections, actions = await agent.monitor_cycle()

        assert detections == []
        assert actions == []

    @pytest.mark.asyncio
    async def test_malformed_alert_is_skipped_without_crashing(self):
        # Missing onset / effective / sent → _build_weather_alert raises
        # ValueError for "missing expected_start_at" and the alert is
        # dropped from the action log.
        malformed_feature = _nws_feature(alert_id="alert-malformed-1")
        malformed_feature["properties"]["onset"] = None
        malformed_feature["properties"]["effective"] = None
        malformed_feature["properties"]["sent"] = None
        good_feature = _nws_feature(alert_id="alert-good-1")
        transport = _mock_transport([malformed_feature, good_feature])

        async def loader():
            return {"tenant-A": ["14202"]}

        async with httpx.AsyncClient(transport=transport) as client:
            deps = _make_deps()
            agent = WeatherAlertIngester(
                es_service=deps["es_service"],
                activity_log_service=deps["activity_log_service"],
                ws_manager=deps["ws_manager"],
                confirmation_protocol=deps["confirmation_protocol"],
                signal_bus=deps["signal_bus"],
                http_client=client,
                tenant_footprint_loader=loader,
            )

            detections, actions = await agent.monitor_cycle()

        # Both alerts show up in detections (they both had a valid id on
        # ingress), but only the good alert makes it through the build
        # step and into the action log.
        assert "alert-good-1" in detections
        assert "alert-malformed-1" in detections
        assert len(actions) == 1
        assert actions[0]["alert_id"] == "alert-good-1"

    @pytest.mark.asyncio
    async def test_persist_failure_is_recorded_but_does_not_raise(self):
        feature = _nws_feature(alert_id="alert-persist-fail")
        transport = _mock_transport([feature])

        async def loader():
            return {"tenant-A": ["14202"]}

        async with httpx.AsyncClient(transport=transport) as client:
            deps = _make_deps()
            deps["es_service"].index_document = AsyncMock(
                side_effect=RuntimeError("ES write down")
            )
            agent = WeatherAlertIngester(
                es_service=deps["es_service"],
                activity_log_service=deps["activity_log_service"],
                ws_manager=deps["ws_manager"],
                confirmation_protocol=deps["confirmation_protocol"],
                signal_bus=deps["signal_bus"],
                http_client=client,
                tenant_footprint_loader=loader,
            )

            detections, actions = await agent.monitor_cycle()

        assert detections == ["alert-persist-fail"]
        assert len(actions) == 1
        assert actions[0]["action"] == "persist_failed"
        # A persist failure must suppress the SignalBus publish so we do
        # not emit phantom alerts.
        deps["signal_bus"].publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_signal_bus_is_optional(self):
        feature = _nws_feature(alert_id="alert-no-bus")
        transport = _mock_transport([feature])

        async def loader():
            return {"tenant-A": ["14202"]}

        async with httpx.AsyncClient(transport=transport) as client:
            deps = _make_deps()
            agent = WeatherAlertIngester(
                es_service=deps["es_service"],
                activity_log_service=deps["activity_log_service"],
                ws_manager=deps["ws_manager"],
                confirmation_protocol=deps["confirmation_protocol"],
                signal_bus=None,
                http_client=client,
                tenant_footprint_loader=loader,
            )

            _, actions = await agent.monitor_cycle()

        # Persisted even though there's no bus; published flag is False.
        assert len(actions) == 1
        assert actions[0]["action"] == "ingested"
        assert actions[0]["published"] is False


# ---------------------------------------------------------------------------
# Duplicate-alert detection (idempotence contract)
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Same ``alert_id`` must not double-persist or double-publish."""

    @pytest.mark.asyncio
    async def test_duplicate_alert_is_skipped(self):
        existing_payload = {
            "alert_id": "dup-1",
            "tenant_id": "tenant-A",
        }
        feature = _nws_feature(alert_id="dup-1")
        transport = _mock_transport([feature])

        async def loader():
            return {"tenant-A": ["14202"]}

        async with httpx.AsyncClient(transport=transport) as client:
            deps = _make_deps()
            deps["es_service"].get_document = AsyncMock(
                return_value=existing_payload
            )
            agent = WeatherAlertIngester(
                es_service=deps["es_service"],
                activity_log_service=deps["activity_log_service"],
                ws_manager=deps["ws_manager"],
                confirmation_protocol=deps["confirmation_protocol"],
                signal_bus=deps["signal_bus"],
                http_client=client,
                tenant_footprint_loader=loader,
            )

            detections, actions = await agent.monitor_cycle()

        # The duplicate still shows up in detections (the ingester saw it
        # from NWS) but is recorded as a skip in actions.
        assert detections == ["dup-1"]
        assert len(actions) == 1
        assert actions[0]["action"] == "skipped_duplicate"
        deps["es_service"].index_document.assert_not_called()
        deps["signal_bus"].publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_two_cycles_with_same_alert_only_ingest_once(self):
        feature = _nws_feature(alert_id="dup-2")
        transport = _mock_transport([feature])

        async def loader():
            return {"tenant-A": ["14202"]}

        # Shared ES mock that remembers what was indexed.
        persisted_ids: List[str] = []

        async def index_document(index, doc_id, doc):
            persisted_ids.append(doc_id)

        async def get_document(index, doc_id):
            return {"alert_id": doc_id} if doc_id in persisted_ids else None

        async with httpx.AsyncClient(transport=transport) as client:
            deps = _make_deps()
            deps["es_service"].index_document = AsyncMock(side_effect=index_document)
            deps["es_service"].get_document = AsyncMock(side_effect=get_document)
            agent = WeatherAlertIngester(
                es_service=deps["es_service"],
                activity_log_service=deps["activity_log_service"],
                ws_manager=deps["ws_manager"],
                confirmation_protocol=deps["confirmation_protocol"],
                signal_bus=deps["signal_bus"],
                http_client=client,
                tenant_footprint_loader=loader,
            )

            # First cycle — ingests.
            _, first_actions = await agent.monitor_cycle()
            # Second cycle — should no-op.
            _, second_actions = await agent.monitor_cycle()

        assert first_actions[0]["action"] == "ingested"
        assert second_actions[0]["action"] == "skipped_duplicate"
        # Only one persist call + one publish across the two cycles.
        assert persisted_ids == ["dup-2"]
        assert deps["signal_bus"].publish.await_count == 1
