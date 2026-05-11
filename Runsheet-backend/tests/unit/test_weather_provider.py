"""
Unit tests for :mod:`Agents.support.weather_provider`.

Covers Capability 1 / Requirements 1.2.1–1.2.6 of the fuel-ops hardening spec:

* :class:`DailyWeather` Pydantic validation and the canonical mapping shape.
* :class:`WeatherProvider` base-class plumbing:
    - Redis cache lookup + populate keyed by
      ``weather:{provider}:{zip}:{start}:{end}`` with 3600s TTL (Req 1.2.4).
    - ES persistence to ``weather_observations`` with tenant_id (Req 1.2.6).
    - 5-second timeout enforced via ``asyncio.wait_for`` (Req 1.2.5).
    - Graceful degradation to an empty list on network / parse failures
      (Req 1.2.5).
* :class:`NOAAWeatherProvider` and :class:`OpenWeatherProvider` correctly
  call their upstream APIs through an injected ``httpx.AsyncClient`` backed
  by :class:`httpx.MockTransport` — no real network is touched.
* :func:`build_weather_provider` factory resolves by short name and rejects
  unknowns.

Validates: Requirements 1.2.1, 1.2.2, 1.2.4, 1.2.5, 1.2.6.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest
from pydantic import ValidationError

from fuel.services.fuel_ops_es_mappings import WEATHER_OBSERVATIONS_INDEX
from fuel.services.weather_provider import (
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    DailyWeather,
    NOAAWeatherProvider,
    OpenWeatherProvider,
    WeatherProvider,
    build_weather_provider,
    compute_hdd,
)
from services.external_call_tracing import default_circuit_breaker


# ---------------------------------------------------------------------------
# Shared circuit-breaker isolation
#
# The weather provider uses the process-wide :data:`default_circuit_breaker`
# singleton keyed by ``(tenant_id, provider)``. Failure-path tests here and
# in the traffic/rack-price test modules share the ``("t-1", "stub")`` key
# so a breaker tripped in one module would short-circuit callers in another.
# The autouse fixture clears state between tests to keep them independent.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reset_default_circuit_breaker():
    """Reset the shared breaker between tests to avoid cross-test leaks."""
    await default_circuit_breaker.reset()
    yield
    await default_circuit_breaker.reset()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-memory async Redis stub exposing ``get`` and ``setex`` only."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.get_calls: List[str] = []
        self.setex_calls: List[Dict[str, Any]] = []
        self.raise_on_get: Optional[Exception] = None
        self.raise_on_setex: Optional[Exception] = None

    async def get(self, key: str) -> Optional[str]:
        self.get_calls.append(key)
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.setex_calls.append({"key": key, "ttl": ttl, "value": value})
        if self.raise_on_setex is not None:
            raise self.raise_on_setex
        self.store[key] = value
        self.ttls[key] = ttl
        return True


class _FakeES:
    """In-memory ES service with just the ``index_document`` coroutine used here."""

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.raise_on_index: Optional[Exception] = None

    async def index_document(self, index: str, doc_id: str, document: Dict[str, Any]):
        self.calls.append({"index": index, "doc_id": doc_id, "document": dict(document)})
        if self.raise_on_index is not None:
            raise self.raise_on_index
        self.docs[doc_id] = dict(document)
        return {"_id": doc_id, "result": "created"}


class _StubProvider(WeatherProvider):
    """Concrete provider whose ``_fetch_raw`` is configurable per test."""

    name = "stub"  # type: ignore[assignment]

    def __init__(self, rows_or_exc: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._rows_or_exc = rows_or_exc
        self.fetch_raw_calls: List[Dict[str, Any]] = []

    async def _fetch_raw(
        self,
        *,
        zip_code: str,
        start_date: date,
        end_date: date,
        tenant_id: str,
    ) -> List[DailyWeather]:
        self.fetch_raw_calls.append(
            {
                "zip_code": zip_code,
                "start_date": start_date,
                "end_date": end_date,
                "tenant_id": tenant_id,
            }
        )
        if callable(self._rows_or_exc):
            return await self._rows_or_exc(zip_code=zip_code, start_date=start_date, end_date=end_date, tenant_id=tenant_id)
        if isinstance(self._rows_or_exc, Exception):
            raise self._rows_or_exc
        return list(self._rows_or_exc)


def _row(day: date, *, tenant: str = "t-1", zip_code: str = "06001", temp: float = 30.0, provider: str = "stub") -> DailyWeather:
    return DailyWeather(
        date=day,
        zip_code=zip_code,
        tenant_id=tenant,
        avg_temp_f=temp,
        hdd=compute_hdd(temp),
        provider=provider,
        retrieved_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestDailyWeatherModel:
    def test_valid_row_round_trips(self):
        row = _row(date(2024, 1, 15))
        assert row.zip_code == "06001"
        assert row.provider == "stub"
        dumped = row.model_dump(mode="json")
        assert dumped["date"] == "2024-01-15"
        assert dumped["retrieved_at"].startswith("2024-01-01T12:00:00")

    def test_rejects_blank_required_strings(self):
        base = dict(
            date=date(2024, 1, 1),
            zip_code="06001",
            tenant_id="t-1",
            avg_temp_f=30.0,
            hdd=35.0,
            provider="stub",
            retrieved_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        for field in ("zip_code", "tenant_id", "provider"):
            payload = {**base, field: "   "}
            with pytest.raises(ValidationError):
                DailyWeather(**payload)

    def test_rejects_negative_hdd(self):
        # hdd is bounded >= 0 by the mapping. The model should refuse the row.
        with pytest.raises(ValidationError):
            DailyWeather(
                date=date(2024, 1, 1),
                zip_code="06001",
                tenant_id="t-1",
                avg_temp_f=30.0,
                hdd=-1.0,
                provider="stub",
                retrieved_at=datetime.now(timezone.utc),
            )

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            DailyWeather(
                date=date(2024, 1, 1),
                zip_code="06001",
                tenant_id="t-1",
                avg_temp_f=30.0,
                hdd=35.0,
                provider="stub",
                retrieved_at=datetime.now(timezone.utc),
                not_a_field=True,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        "avg_temp, expected",
        [
            (65.0, 0.0),
            (70.0, 0.0),
            (50.0, 15.0),
            (0.0, 65.0),
            (-10.0, 75.0),
        ],
    )
    def test_compute_hdd(self, avg_temp: float, expected: float):
        assert compute_hdd(avg_temp) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Base class — cache, ES, fallback, timeout
# ---------------------------------------------------------------------------


class TestWeatherProviderBase:
    async def test_lazily_created_http_client_is_reused_until_closed(self):
        provider = _StubProvider(rows_or_exc=[])

        first = await provider._get_http_client()
        second = await provider._get_http_client()

        assert first is second
        assert first.is_closed is False

        await provider.aclose()

        assert first.is_closed is True

    async def test_fetch_returns_rows_persists_es_and_caches(self):
        redis = _FakeRedis()
        es = _FakeES()
        rows = [_row(date(2024, 1, 1)), _row(date(2024, 1, 2))]
        provider = _StubProvider(
            rows_or_exc=rows,
            es_service=es,
            redis_client=redis,
        )

        out = await provider.fetch(
            "06001", date(2024, 1, 1), date(2024, 1, 2), tenant_id="t-1"
        )

        assert [r.date for r in out] == [date(2024, 1, 1), date(2024, 1, 2)]
        # ES received both rows under the correct index.
        assert len(es.calls) == 2
        for call in es.calls:
            assert call["index"] == WEATHER_OBSERVATIONS_INDEX
            assert call["document"]["tenant_id"] == "t-1"
            assert call["document"]["provider"] == "stub"
        # Cache populated with the canonical key + 3600s TTL.
        assert redis.setex_calls, "expected setex to have been called once"
        setex = redis.setex_calls[0]
        assert setex["ttl"] == DEFAULT_CACHE_TTL_SECONDS
        assert setex["key"] == "weather:stub:06001:2024-01-01:2024-01-02"
        payload = json.loads(setex["value"])
        assert len(payload) == 2
        assert payload[0]["date"] == "2024-01-01"

    async def test_fetch_returns_cached_rows_without_calling_provider(self):
        redis = _FakeRedis()
        es = _FakeES()
        rows = [_row(date(2024, 1, 1))]
        provider = _StubProvider(
            rows_or_exc=rows,
            es_service=es,
            redis_client=redis,
        )
        # Populate cache.
        await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        provider.fetch_raw_calls.clear()
        es.calls.clear()

        # Second call should be a pure cache hit.
        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")

        assert [r.date for r in out] == [date(2024, 1, 1)]
        assert provider.fetch_raw_calls == []
        assert es.calls == []

    async def test_cache_hit_restamps_tenant_id(self):
        redis = _FakeRedis()
        provider = _StubProvider(
            rows_or_exc=[_row(date(2024, 1, 1), tenant="t-1")],
            redis_client=redis,
        )
        await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        provider.fetch_raw_calls.clear()

        # A different tenant hitting the same cache entry should still get
        # rows tagged with their own tenant_id.
        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-2")

        assert len(out) == 1
        assert out[0].tenant_id == "t-2"
        assert provider.fetch_raw_calls == []

    async def test_fetch_returns_empty_on_raw_exception(self):
        provider = _StubProvider(
            rows_or_exc=RuntimeError("boom"),
            redis_client=_FakeRedis(),
            es_service=_FakeES(),
        )

        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        assert out == []

    async def test_fetch_returns_empty_on_http_error(self):
        provider = _StubProvider(
            rows_or_exc=httpx.ConnectError("connection refused"),
            redis_client=_FakeRedis(),
            es_service=_FakeES(),
        )

        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        assert out == []

    async def test_fetch_enforces_timeout(self):
        async def slow(**_: Any) -> List[DailyWeather]:
            await asyncio.sleep(0.5)
            return []

        provider = _StubProvider(
            rows_or_exc=slow,
            timeout_seconds=0.05,
        )

        out = await provider.fetch(
            "06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1"
        )
        assert out == []

    async def test_fetch_rejects_invalid_args(self):
        provider = _StubProvider(rows_or_exc=[])
        with pytest.raises(ValueError):
            await provider.fetch("", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        with pytest.raises(ValueError):
            await provider.fetch("06001", date(2024, 1, 2), date(2024, 1, 1), tenant_id="t-1")
        with pytest.raises(ValueError):
            await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="")

    async def test_fetch_without_cache_or_es_still_returns_rows(self):
        provider = _StubProvider(rows_or_exc=[_row(date(2024, 1, 1))])
        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        assert len(out) == 1

    async def test_persist_failure_does_not_mask_rows(self):
        es = _FakeES()
        es.raise_on_index = RuntimeError("es down")
        provider = _StubProvider(
            rows_or_exc=[_row(date(2024, 1, 1))],
            es_service=es,
        )
        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        # Rows still returned to the caller even though ES blew up.
        assert len(out) == 1

    async def test_cache_failure_does_not_mask_rows(self):
        redis = _FakeRedis()
        redis.raise_on_setex = RuntimeError("redis down")
        provider = _StubProvider(
            rows_or_exc=[_row(date(2024, 1, 1))],
            redis_client=redis,
        )
        out = await provider.fetch("06001", date(2024, 1, 1), date(2024, 1, 1), tenant_id="t-1")
        assert len(out) == 1

    async def test_invalid_init_args(self):
        with pytest.raises(ValueError):
            _StubProvider(rows_or_exc=[], timeout_seconds=0)
        with pytest.raises(ValueError):
            _StubProvider(rows_or_exc=[], cache_ttl_seconds=0)


# ---------------------------------------------------------------------------
# NOAA adapter
# ---------------------------------------------------------------------------


def _noaa_response(rows: List[Dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"results": rows})


class TestNOAAWeatherProvider:
    async def test_happy_path_parses_and_averages_stations(self):
        # Two stations for 2024-01-15, one for 2024-01-16.
        # CDO returns TAVG in tenths of °C.
        # Station A: -50 tenths = -5°C = 23°F
        # Station B: -30 tenths = -3°C = 26.6°F
        # Average: (23 + 26.6) / 2 = 24.8°F → hdd = 40.2
        # Next day: -10 tenths = -1°C = 30.2°F → hdd = 34.8
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _noaa_response(
                [
                    {"date": "2024-01-15T00:00:00", "value": -50, "station": "A"},
                    {"date": "2024-01-15T00:00:00", "value": -30, "station": "B"},
                    {"date": "2024-01-16T00:00:00", "value": -10, "station": "A"},
                ]
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = NOAAWeatherProvider(
                token="unit-test-token",
                http_client=client,
                es_service=_FakeES(),
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 16), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert [r.date for r in rows] == [date(2024, 1, 15), date(2024, 1, 16)]
        assert rows[0].avg_temp_f == pytest.approx(24.8, rel=1e-3)
        assert rows[0].hdd == pytest.approx(40.2, rel=1e-3)
        assert rows[0].tenant_id == "t-1"
        assert rows[0].provider == "noaa"
        # Verify we hit the right URL and included the token header.
        assert len(captured) == 1
        req = captured[0]
        assert req.url.path.endswith("/data")
        assert req.headers.get("token") == "unit-test-token"
        params = dict(req.url.params)
        assert params["datasetid"] == "GHCND"
        assert params["datatypeid"] == "TAVG"
        assert params["locationid"] == "ZIP:06001"
        assert params["startdate"] == "2024-01-15"
        assert params["enddate"] == "2024-01-16"

    async def test_missing_token_returns_empty_without_http(self):
        # No token anywhere — should short-circuit without a network call.
        calls: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _noaa_response([])

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = NOAAWeatherProvider(
                token=None,
                http_client=client,
            )
            # Ensure env var isn't leaking in.
            import os
            os.environ.pop("NOAA_CDO_TOKEN", None)
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert rows == []
        assert calls == []

    async def test_http_500_degrades_to_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = NOAAWeatherProvider(
                token="tok",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert rows == []

    async def test_malformed_rows_are_skipped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _noaa_response(
                [
                    {"date": None, "value": -10},
                    {"date": "2024-01-15T00:00:00", "value": None},
                    {"date": "2024-01-15T00:00:00", "value": "bogus"},
                    {"date": "2024-01-15T00:00:00", "value": -20},
                ]
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = NOAAWeatherProvider(
                token="tok",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert len(rows) == 1
        # -20 tenths = -2°C = 28.4°F → hdd ≈ 36.6
        assert rows[0].avg_temp_f == pytest.approx(28.4, rel=1e-3)
        assert rows[0].hdd == pytest.approx(36.6, rel=1e-3)


# ---------------------------------------------------------------------------
# OpenWeather adapter
# ---------------------------------------------------------------------------


class _GeoDayHandler:
    """Composable MockTransport handler for OpenWeather's geo + day_summary."""

    def __init__(
        self,
        lat: Optional[float],
        lon: Optional[float],
        day_temps: Dict[str, Dict[str, float]],
        *,
        geo_error: bool = False,
        day_error: bool = False,
    ) -> None:
        self.lat = lat
        self.lon = lon
        self.day_temps = day_temps
        self.geo_error = geo_error
        self.day_error = day_error
        self.geo_calls: List[httpx.Request] = []
        self.day_calls: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "/geo/" in str(request.url):
            self.geo_calls.append(request)
            if self.geo_error:
                return httpx.Response(502, json={"error": "geo"})
            if self.lat is None or self.lon is None:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json={"lat": self.lat, "lon": self.lon})
        if "day_summary" in str(request.url):
            self.day_calls.append(request)
            if self.day_error:
                return httpx.Response(502, json={"error": "day"})
            day = dict(request.url.params).get("date", "")
            temps = self.day_temps.get(day)
            if temps is None:
                return httpx.Response(404, json={"error": "no_data"})
            return httpx.Response(200, json={"temperature": temps})
        return httpx.Response(404, json={"error": "unknown_route"})


class TestOpenWeatherProvider:
    async def test_happy_path_end_to_end(self):
        handler = _GeoDayHandler(
            lat=41.9,
            lon=-72.7,
            day_temps={
                "2024-01-15": {"afternoon": 30.0, "min": 20.0, "max": 35.0},
                "2024-01-16": {"afternoon": 28.0, "min": 18.0, "max": 32.0},
            },
        )
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(
                api_key="unit-test-key",
                http_client=client,
                es_service=_FakeES(),
                redis_client=_FakeRedis(),
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 16), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert [r.date for r in rows] == [date(2024, 1, 15), date(2024, 1, 16)]
        assert rows[0].avg_temp_f == pytest.approx(30.0, rel=1e-3)
        assert rows[0].hdd == pytest.approx(35.0, rel=1e-3)
        assert rows[0].provider == "openweather"
        assert len(handler.geo_calls) == 1
        assert len(handler.day_calls) == 2
        geo_params = dict(handler.geo_calls[0].url.params)
        assert geo_params["zip"] == "06001,US"
        assert geo_params["appid"] == "unit-test-key"

    async def test_falls_back_to_min_max_mean_when_afternoon_missing(self):
        handler = _GeoDayHandler(
            lat=41.9,
            lon=-72.7,
            day_temps={"2024-01-15": {"min": 20.0, "max": 40.0}},
        )
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(
                api_key="unit-test-key",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert len(rows) == 1
        assert rows[0].avg_temp_f == pytest.approx(30.0, rel=1e-3)

    async def test_day_404_is_skipped_not_fatal(self):
        handler = _GeoDayHandler(
            lat=41.9,
            lon=-72.7,
            day_temps={"2024-01-15": {"afternoon": 25.0}},  # only one day present
        )
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(
                api_key="unit-test-key",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 16), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        # First day parsed, second day missing — still returns the partial list.
        assert [r.date for r in rows] == [date(2024, 1, 15)]

    async def test_geo_failure_returns_empty(self):
        handler = _GeoDayHandler(lat=None, lon=None, day_temps={}, geo_error=True)
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(
                api_key="unit-test-key",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert rows == []
        assert handler.day_calls == []

    async def test_vault_api_key_preferred_over_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENWEATHER_API_KEY", "env-key")

        class _FakeVault:
            async def get(self, tenant_id: str, ref: str) -> Dict[str, Any]:
                assert tenant_id == "t-1"
                assert ref == "cred:t-1:openweather:abc"
                return {"api_key": "vault-key"}

        handler = _GeoDayHandler(
            lat=41.9,
            lon=-72.7,
            day_temps={"2024-01-15": {"afternoon": 40.0}},
        )
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(
                credentials_vault=_FakeVault(),
                credentials_ref="cred:t-1:openweather:abc",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert len(rows) == 1
        assert dict(handler.geo_calls[0].url.params)["appid"] == "vault-key"

    async def test_vault_failure_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENWEATHER_API_KEY", "env-key")

        class _ExplodingVault:
            async def get(self, tenant_id: str, ref: str) -> Dict[str, Any]:
                raise RuntimeError("kms down")

        handler = _GeoDayHandler(
            lat=41.9,
            lon=-72.7,
            day_temps={"2024-01-15": {"afternoon": 40.0}},
        )
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(
                credentials_vault=_ExplodingVault(),
                credentials_ref="cred:t-1:openweather:abc",
                http_client=client,
            )
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert len(rows) == 1
        assert dict(handler.geo_calls[0].url.params)["appid"] == "env-key"

    async def test_missing_api_key_returns_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
        handler = _GeoDayHandler(lat=41.9, lon=-72.7, day_temps={})
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            provider = OpenWeatherProvider(http_client=client)
            rows = await provider.fetch(
                "06001", date(2024, 1, 15), date(2024, 1, 15), tenant_id="t-1"
            )
        finally:
            await client.aclose()

        assert rows == []
        # Geo endpoint should not have been called since the key check runs first.
        assert handler.geo_calls == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_build_noaa(self):
        provider = build_weather_provider("noaa", token="t")
        assert isinstance(provider, NOAAWeatherProvider)

    def test_build_openweather_case_insensitive(self):
        provider = build_weather_provider("OpenWeather")
        assert isinstance(provider, OpenWeatherProvider)

    def test_rejects_unknown(self):
        with pytest.raises(ValueError):
            build_weather_provider("weather_channel")

    def test_rejects_blank_name(self):
        with pytest.raises(ValueError):
            build_weather_provider("   ")
