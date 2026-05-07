"""
Unit tests for :mod:`fuel.services.traffic_provider`.

Covers Capability 2 / Requirements 2.1.1, 2.1.2, 2.1.4, 2.1.7 of the fuel-ops
hardening spec:

* :class:`TravelMatrix` Pydantic validation and shape enforcement.
* :class:`TrafficProvider` base-class plumbing:
    - Per-pair Redis cache lookup + populate keyed by
      ``traffic:{provider}:{lat1}:{lon1}:{lat2}:{lon2}:{bucket_15min}`` with
      a 900-second TTL (Req 2.1.4).
    - Per-tenant monthly budget check + increment on success
      (``traffic_budget:{tenant_id}:{YYYY-MM}``, Req 2.1.7).
    - 10-second timeout enforced via ``asyncio.wait_for`` (timeout budget
      documented in Req 2.1.5 but the *reachability* of the timeout is what
      this module validates).
    - Full-cache short-circuit — no HTTP or budget increment when every
      pair is warm.
* :class:`MapboxTrafficProvider`, :class:`HERETrafficProvider`, and
  :class:`GoogleDirectionsTrafficProvider` correctly call their upstream APIs
  through an injected ``httpx.AsyncClient`` backed by :class:`httpx.MockTransport`
  — no real network is touched.
* :func:`build_traffic_provider` factory resolves by short name and rejects
  unknowns.

Validates: Requirements 2.1.1, 2.1.2, 2.1.4, 2.1.7.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from fuel.services.traffic_provider import (
    BUCKET_SECONDS,
    BUDGET_COUNTER_KEY_TEMPLATE,
    BUDGET_COUNTER_TTL_SECONDS,
    BUDGET_LIMIT_KEY_TEMPLATE,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    GoogleDirectionsTrafficProvider,
    HERETrafficProvider,
    LatLon,
    MapboxTrafficProvider,
    TrafficBudgetExceeded,
    TrafficProvider,
    TravelMatrix,
    build_cache_key,
    build_traffic_provider,
    compute_bucket_15min,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRedis:
    """In-memory async Redis stub exposing ``get``, ``setex``, ``incr``, ``expire``."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.get_calls: List[str] = []
        self.setex_calls: List[Dict[str, Any]] = []
        self.incr_calls: List[str] = []
        self.expire_calls: List[Tuple[str, int]] = []
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

    async def set(self, key: str, value: str) -> bool:
        self.store[key] = value
        return True

    async def incr(self, key: str) -> int:
        self.incr_calls.append(key)
        current = int(self.store.get(key, "0")) + 1
        self.store[key] = str(current)
        return current

    async def expire(self, key: str, ttl: int) -> bool:
        self.expire_calls.append((key, ttl))
        self.ttls[key] = ttl
        return True


class _StubProvider(TrafficProvider):
    """Concrete provider whose ``_fetch_raw`` is configurable per test."""

    name = "stub"  # type: ignore[assignment]

    def __init__(
        self,
        result_or_exc: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._result_or_exc = result_or_exc
        self.fetch_raw_calls: List[Dict[str, Any]] = []

    async def _fetch_raw(
        self,
        *,
        origins: List[LatLon],
        destinations: List[LatLon],
        depart_at: datetime,
    ) -> Tuple[List[List[float]], List[List[float]]]:
        self.fetch_raw_calls.append(
            {
                "origins": origins,
                "destinations": destinations,
                "depart_at": depart_at,
            }
        )
        if callable(self._result_or_exc):
            return await self._result_or_exc(
                origins=origins, destinations=destinations, depart_at=depart_at
            )
        if isinstance(self._result_or_exc, Exception):
            raise self._result_or_exc
        return self._result_or_exc  # tuple of (distance_km, duration_minutes)


def _depart() -> datetime:
    return datetime(2024, 3, 15, 14, 7, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestTravelMatrixModel:
    def test_valid_matrix(self):
        m = TravelMatrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8), (42.1, -72.9)],
            distance_km=[[1.5, 3.0]],
            duration_minutes=[[2.0, 5.0]],
            provider="mapbox",
        )
        assert m.provider == "mapbox"
        assert m.distance_km == [[1.5, 3.0]]

    @pytest.mark.parametrize(
        "kwargs",
        [
            # Wrong distance row count
            dict(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                distance_km=[[1.0], [2.0]],
                duration_minutes=[[5.0]],
                provider="mapbox",
            ),
            # Wrong distance col count
            dict(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8), (42.1, -72.9)],
                distance_km=[[1.0]],
                duration_minutes=[[5.0, 6.0]],
                provider="mapbox",
            ),
            # Wrong duration shape
            dict(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                distance_km=[[1.0]],
                duration_minutes=[[5.0, 6.0]],
                provider="mapbox",
            ),
        ],
    )
    def test_shape_mismatch_rejected(self, kwargs: Dict[str, Any]):
        with pytest.raises(ValidationError):
            TravelMatrix(**kwargs)

    def test_negative_values_rejected(self):
        with pytest.raises(ValidationError):
            TravelMatrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                distance_km=[[-0.1]],
                duration_minutes=[[5.0]],
                provider="mapbox",
            )
        with pytest.raises(ValidationError):
            TravelMatrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                distance_km=[[1.0]],
                duration_minutes=[[-5.0]],
                provider="mapbox",
            )

    def test_out_of_range_coordinates_rejected(self):
        with pytest.raises(ValidationError):
            TravelMatrix(
                origins=[(91.0, 0.0)],
                destinations=[(42.0, -72.8)],
                distance_km=[[1.0]],
                duration_minutes=[[2.0]],
                provider="mapbox",
            )
        with pytest.raises(ValidationError):
            TravelMatrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -181.0)],
                distance_km=[[1.0]],
                duration_minutes=[[2.0]],
                provider="mapbox",
            )

    def test_rejects_empty_lists(self):
        with pytest.raises(ValidationError):
            TravelMatrix(
                origins=[],
                destinations=[(42.0, -72.8)],
                distance_km=[],
                duration_minutes=[],
                provider="mapbox",
            )

    def test_rejects_blank_provider(self):
        with pytest.raises(ValidationError):
            TravelMatrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                distance_km=[[1.0]],
                duration_minutes=[[2.0]],
                provider="  ",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_compute_bucket_15min_is_stable(self):
        # Two timestamps inside the same 15-minute window map to the same bucket.
        t1 = datetime(2024, 3, 15, 14, 7, 30, tzinfo=timezone.utc)
        t2 = datetime(2024, 3, 15, 14, 14, 59, tzinfo=timezone.utc)
        t3 = datetime(2024, 3, 15, 14, 15, 0, tzinfo=timezone.utc)
        assert compute_bucket_15min(t1) == compute_bucket_15min(t2)
        assert compute_bucket_15min(t3) == compute_bucket_15min(t1) + 1

    def test_compute_bucket_requires_tz_aware(self):
        with pytest.raises(ValueError):
            compute_bucket_15min(datetime(2024, 3, 15, 14, 0))
        with pytest.raises(TypeError):
            compute_bucket_15min("2024-03-15")  # type: ignore[arg-type]

    def test_build_cache_key_format(self):
        key = build_cache_key(
            "mapbox", (41.9, -72.7), (42.000005, -72.8), bucket_15min=1_000
        )
        # Coordinates are rounded to 5 decimals so jitter produces a stable key.
        assert key == "traffic:mapbox:41.9:-72.7:42.00001:-72.8:1000"


# ---------------------------------------------------------------------------
# Base class — cache, budget, timeout
# ---------------------------------------------------------------------------


class TestTrafficProviderBase:
    async def test_fetch_populates_matrix_cache_and_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Freeze the current month so budget-counter key assertions are stable.
        monkeypatch.setattr(
            "fuel.services.traffic_provider._current_month_key",
            lambda now=None: "2024-03",
        )
        redis = _FakeRedis()
        provider = _StubProvider(
            result_or_exc=([[1.5, 3.0]], [[2.0, 5.0]]),
            redis_client=redis,
        )

        matrix = await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8), (42.1, -72.9)],
            depart_at=_depart(),
            tenant_id="t-1",
        )

        assert matrix.provider == "stub"
        assert matrix.distance_km == [[1.5, 3.0]]
        assert matrix.duration_minutes == [[2.0, 5.0]]
        # One upstream call.
        assert len(provider.fetch_raw_calls) == 1
        # Budget incremented exactly once with the right key.
        month_key = BUDGET_COUNTER_KEY_TEMPLATE.format(
            tenant_id="t-1", month="2024-03"
        )
        assert redis.incr_calls == [month_key]
        # Counter refreshed with 32-day TTL so it naturally rolls off.
        assert (month_key, BUDGET_COUNTER_TTL_SECONDS) in redis.expire_calls
        # Cache populated for every pair with 900s TTL.
        assert len(redis.setex_calls) == 2
        for call in redis.setex_calls:
            assert call["ttl"] == DEFAULT_CACHE_TTL_SECONDS
            assert call["key"].startswith("traffic:stub:")
            payload = json.loads(call["value"])
            assert "distance_km" in payload
            assert "duration_minutes" in payload

    async def test_full_cache_hit_skips_http_and_budget(self):
        redis = _FakeRedis()
        provider = _StubProvider(
            result_or_exc=([[1.5]], [[2.0]]),
            redis_client=redis,
        )

        # Warm the cache.
        await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8)],
            depart_at=_depart(),
            tenant_id="t-1",
        )
        provider.fetch_raw_calls.clear()
        redis.incr_calls.clear()

        matrix = await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8)],
            depart_at=_depart(),
            tenant_id="t-1",
        )

        assert matrix.distance_km == [[1.5]]
        assert matrix.duration_minutes == [[2.0]]
        # Second call was a pure cache hit — no HTTP, no budget increment.
        assert provider.fetch_raw_calls == []
        assert redis.incr_calls == []

    async def test_partial_cache_still_calls_provider(self):
        redis = _FakeRedis()
        provider = _StubProvider(
            result_or_exc=([[1.5, 3.0]], [[2.0, 5.0]]),
            redis_client=redis,
        )

        # Seed one of the two pairs. The second pair forces a re-fetch.
        warm_key = build_cache_key(
            "stub",
            (41.9, -72.7),
            (42.0, -72.8),
            bucket_15min=compute_bucket_15min(_depart()),
        )
        redis.store[warm_key] = json.dumps(
            {"distance_km": 9.9, "duration_minutes": 9.9}
        )

        await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8), (42.1, -72.9)],
            depart_at=_depart(),
            tenant_id="t-1",
        )

        assert len(provider.fetch_raw_calls) == 1
        # Budget was incremented because we hit the network.
        assert len(redis.incr_calls) == 1

    async def test_budget_exceeded_raises_before_http(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "fuel.services.traffic_provider._current_month_key",
            lambda now=None: "2024-03",
        )
        redis = _FakeRedis()
        # Limit of 3, counter already at 3.
        redis.store[BUDGET_LIMIT_KEY_TEMPLATE.format(tenant_id="t-1")] = "3"
        redis.store[
            BUDGET_COUNTER_KEY_TEMPLATE.format(tenant_id="t-1", month="2024-03")
        ] = "3"
        provider = _StubProvider(
            result_or_exc=([[1.5]], [[2.0]]),
            redis_client=redis,
        )

        with pytest.raises(TrafficBudgetExceeded) as exc:
            await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )

        assert exc.value.tenant_id == "t-1"
        assert exc.value.current == 3
        assert exc.value.limit == 3
        assert exc.value.month == "2024-03"
        # No HTTP call was issued, no increment performed.
        assert provider.fetch_raw_calls == []
        assert redis.incr_calls == []

    async def test_no_budget_configured_is_treated_as_unlimited(self):
        redis = _FakeRedis()  # no limit key set
        provider = _StubProvider(
            result_or_exc=([[1.5]], [[2.0]]),
            redis_client=redis,
        )
        await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8)],
            depart_at=_depart(),
            tenant_id="t-1",
        )
        # Call succeeded, counter was incremented.
        assert redis.incr_calls and redis.incr_calls[0].startswith(
            "traffic_budget:t-1:"
        )

    async def test_provider_exception_propagates_for_caller_fallback(self):
        """Network errors must propagate so the Route_Planning_Agent can fall
        back to Haversine + DEFAULT_SPEED_KMH with ``traffic_fallback: true``
        per Requirement 2.1.5."""

        provider = _StubProvider(
            result_or_exc=httpx.ConnectError("connection refused"),
            redis_client=_FakeRedis(),
        )

        with pytest.raises(httpx.HTTPError):
            await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )

    async def test_timeout_propagates(self):
        async def slow(**_: Any):
            await asyncio.sleep(0.5)
            return ([[1.5]], [[2.0]])

        provider = _StubProvider(
            result_or_exc=slow,
            timeout_seconds=0.05,
        )

        with pytest.raises(asyncio.TimeoutError):
            await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )

    async def test_rejects_invalid_args(self):
        provider = _StubProvider(result_or_exc=([[1.0]], [[1.0]]))
        d = _depart()

        with pytest.raises(ValueError):
            await provider.get_matrix(
                origins=[],
                destinations=[(0.0, 0.0)],
                depart_at=d,
                tenant_id="t-1",
            )
        with pytest.raises(ValueError):
            await provider.get_matrix(
                origins=[(0.0, 0.0)],
                destinations=[],
                depart_at=d,
                tenant_id="t-1",
            )
        with pytest.raises(ValueError):
            await provider.get_matrix(
                origins=[(0.0, 0.0)],
                destinations=[(0.0, 0.0)],
                depart_at=d,
                tenant_id="",
            )
        with pytest.raises(ValueError):
            # Naive datetime rejected.
            await provider.get_matrix(
                origins=[(0.0, 0.0)],
                destinations=[(0.0, 0.0)],
                depart_at=datetime(2024, 1, 1),
                tenant_id="t-1",
            )
        with pytest.raises(ValueError):
            await provider.get_matrix(
                origins=[(91.0, 0.0)],
                destinations=[(0.0, 0.0)],
                depart_at=d,
                tenant_id="t-1",
            )
        with pytest.raises(ValueError):
            await provider.get_matrix(
                origins=[(0.0, 0.0)],
                destinations=[(0.0, 181.0)],
                depart_at=d,
                tenant_id="t-1",
            )
        with pytest.raises(TypeError):
            await provider.get_matrix(
                origins=[(0.0,)],  # type: ignore[list-item]
                destinations=[(0.0, 0.0)],
                depart_at=d,
                tenant_id="t-1",
            )

    async def test_bad_raw_shape_raises(self):
        # Provider returns wrong shape — base class should flag it.
        provider = _StubProvider(
            result_or_exc=([[1.0]], [[1.0, 2.0]]),  # 1x1 vs 1x2 mismatch
            redis_client=_FakeRedis(),
        )
        with pytest.raises(ValueError):
            await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )

    async def test_cache_failure_does_not_mask_rows(self):
        redis = _FakeRedis()
        redis.raise_on_setex = RuntimeError("redis down")
        provider = _StubProvider(
            result_or_exc=([[1.5]], [[2.0]]),
            redis_client=redis,
        )
        matrix = await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8)],
            depart_at=_depart(),
            tenant_id="t-1",
        )
        assert matrix.distance_km == [[1.5]]

    async def test_no_redis_skips_cache_and_budget(self):
        provider = _StubProvider(
            result_or_exc=([[1.5]], [[2.0]]),
            redis_client=None,
        )
        matrix = await provider.get_matrix(
            origins=[(41.9, -72.7)],
            destinations=[(42.0, -72.8)],
            depart_at=_depart(),
            tenant_id="t-1",
        )
        assert matrix.distance_km == [[1.5]]

    async def test_invalid_init_args(self):
        with pytest.raises(ValueError):
            _StubProvider(result_or_exc=([[1.0]], [[1.0]]), timeout_seconds=0)
        with pytest.raises(ValueError):
            _StubProvider(result_or_exc=([[1.0]], [[1.0]]), cache_ttl_seconds=0)


# ---------------------------------------------------------------------------
# Mapbox adapter
# ---------------------------------------------------------------------------


def _mapbox_response(
    *,
    distances: List[List[Any]],
    durations: List[List[Any]],
    code: str = "Ok",
) -> httpx.Response:
    return httpx.Response(
        200, json={"code": code, "distances": distances, "durations": durations}
    )


class TestMapboxTrafficProvider:
    async def test_happy_path(self):
        # Mapbox returns meters & seconds; we convert to km & minutes.
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _mapbox_response(
                distances=[[0, 1500]],  # 0km, 1.5km
                durations=[[0, 120]],  # 0min, 2min
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = MapboxTrafficProvider(
                access_token="tok", http_client=client
            )
            matrix = await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(41.9, -72.7), (42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert matrix.provider == "mapbox"
        assert matrix.distance_km == [[0.0, 1.5]]
        assert matrix.duration_minutes == [[0.0, 2.0]]
        # Verify URL + params.
        assert len(captured) == 1
        req = captured[0]
        assert req.url.path.startswith("/directions-matrix/v1/mapbox/driving-traffic/")
        params = dict(req.url.params)
        assert params["access_token"] == "tok"
        assert params["annotations"] == "distance,duration"
        assert params["sources"] == "0"
        assert params["destinations"] == "1;2"

    async def test_http_500_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = MapboxTrafficProvider(
                access_token="tok", http_client=client
            )
            with pytest.raises(httpx.HTTPError):
                await provider.get_matrix(
                    origins=[(41.9, -72.7)],
                    destinations=[(42.0, -72.8)],
                    depart_at=_depart(),
                    tenant_id="t-1",
                )
        finally:
            await client.aclose()

    async def test_non_ok_code_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _mapbox_response(distances=[], durations=[], code="ProfileNotFound")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = MapboxTrafficProvider(
                access_token="tok", http_client=client
            )
            with pytest.raises(httpx.HTTPError):
                await provider.get_matrix(
                    origins=[(41.9, -72.7)],
                    destinations=[(42.0, -72.8)],
                    depart_at=_depart(),
                    tenant_id="t-1",
                )
        finally:
            await client.aclose()

    async def test_null_cells_coerced_to_zero(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _mapbox_response(
                distances=[[None, 500]],
                durations=[[None, 30]],
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = MapboxTrafficProvider(
                access_token="tok", http_client=client
            )
            matrix = await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(41.9, -72.7), (42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert matrix.distance_km == [[0.0, 0.5]]
        assert matrix.duration_minutes == [[0.0, 0.5]]

    async def test_missing_token_raises(self):
        # No token anywhere should surface a clear config error (not a silent
        # empty response) so the Route_Planning_Agent can deactivate.
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("MAPBOX_ACCESS_TOKEN", None)
            provider = MapboxTrafficProvider(access_token=None)
            with pytest.raises(RuntimeError):
                await provider.get_matrix(
                    origins=[(41.9, -72.7)],
                    destinations=[(42.0, -72.8)],
                    depart_at=_depart(),
                    tenant_id="t-1",
                )


# ---------------------------------------------------------------------------
# HERE adapter
# ---------------------------------------------------------------------------


class TestHERETrafficProvider:
    async def test_happy_path(self):
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            # 2x2 flat row-major, meters and seconds.
            return httpx.Response(
                200,
                json={
                    "matrix": {
                        "distances": [0, 1500, 2000, 0],
                        "travelTimes": [0, 120, 180, 0],
                    }
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = HERETrafficProvider(api_key="k", http_client=client)
            matrix = await provider.get_matrix(
                origins=[(41.9, -72.7), (42.0, -72.8)],
                destinations=[(41.9, -72.7), (42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        assert matrix.distance_km == [[0.0, 1.5], [2.0, 0.0]]
        assert matrix.duration_minutes == [[0.0, 2.0], [3.0, 0.0]]
        assert len(captured) == 1
        req = captured[0]
        assert req.url.host == "matrix.router.hereapi.com"
        # JSON body has the expected shape.
        body = json.loads(req.content.decode("utf-8"))
        assert body["transportMode"] == "truck"
        assert body["matrixAttributes"] == ["travelTimes", "distances"]
        assert len(body["origins"]) == 2
        assert len(body["destinations"]) == 2

    async def test_wrong_matrix_size_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "matrix": {
                        "distances": [0, 1],  # expected 4 (2x2)
                        "travelTimes": [0, 1, 2, 3],
                    }
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = HERETrafficProvider(api_key="k", http_client=client)
            with pytest.raises(httpx.HTTPError):
                await provider.get_matrix(
                    origins=[(41.9, -72.7), (42.0, -72.8)],
                    destinations=[(41.9, -72.7), (42.0, -72.8)],
                    depart_at=_depart(),
                    tenant_id="t-1",
                )
        finally:
            await client.aclose()

    async def test_missing_key_raises(self):
        import os

        os.environ.pop("HERE_API_KEY", None)
        provider = HERETrafficProvider(api_key=None)
        with pytest.raises(RuntimeError):
            await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )


# ---------------------------------------------------------------------------
# Google Directions adapter
# ---------------------------------------------------------------------------


class TestGoogleDirectionsTrafficProvider:
    async def test_happy_path_prefers_duration_in_traffic(self):
        captured: List[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "rows": [
                        {
                            "elements": [
                                {
                                    "status": "OK",
                                    "distance": {"value": 1500},
                                    "duration": {"value": 60},
                                    "duration_in_traffic": {"value": 180},
                                },
                                {
                                    "status": "OK",
                                    "distance": {"value": 3000},
                                    "duration": {"value": 120},
                                },
                            ]
                        }
                    ],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = GoogleDirectionsTrafficProvider(
                api_key="k", http_client=client
            )
            matrix = await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8), (42.1, -72.9)],
                depart_at=_depart(),
                tenant_id="t-1",
            )
        finally:
            await client.aclose()

        # First element: uses duration_in_traffic (180s = 3 min) over duration.
        # Second element: duration_in_traffic missing → falls back to duration.
        assert matrix.distance_km == [[1.5, 3.0]]
        assert matrix.duration_minutes == [[3.0, 2.0]]
        assert matrix.provider == "google"
        req = captured[0]
        params = dict(req.url.params)
        assert params["mode"] == "driving"
        assert params["key"] == "k"
        assert params["traffic_model"] == "best_guess"
        # Verify the departure_time is the Unix epoch seconds.
        assert int(params["departure_time"]) == int(_depart().timestamp())

    async def test_api_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "INVALID_REQUEST",
                    "error_message": "missing origin",
                    "rows": [],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = GoogleDirectionsTrafficProvider(
                api_key="k", http_client=client
            )
            with pytest.raises(httpx.HTTPError):
                await provider.get_matrix(
                    origins=[(41.9, -72.7)],
                    destinations=[(42.0, -72.8)],
                    depart_at=_depart(),
                    tenant_id="t-1",
                )
        finally:
            await client.aclose()

    async def test_element_not_ok_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "rows": [
                        {
                            "elements": [
                                {"status": "ZERO_RESULTS"},
                            ]
                        }
                    ],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            provider = GoogleDirectionsTrafficProvider(
                api_key="k", http_client=client
            )
            with pytest.raises(httpx.HTTPError):
                await provider.get_matrix(
                    origins=[(41.9, -72.7)],
                    destinations=[(42.0, -72.8)],
                    depart_at=_depart(),
                    tenant_id="t-1",
                )
        finally:
            await client.aclose()

    async def test_missing_key_raises(self):
        import os

        os.environ.pop("GOOGLE_MAPS_API_KEY", None)
        provider = GoogleDirectionsTrafficProvider(api_key=None)
        with pytest.raises(RuntimeError):
            await provider.get_matrix(
                origins=[(41.9, -72.7)],
                destinations=[(42.0, -72.8)],
                depart_at=_depart(),
                tenant_id="t-1",
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_build_mapbox(self):
        provider = build_traffic_provider("mapbox", access_token="t")
        assert isinstance(provider, MapboxTrafficProvider)

    def test_build_here_case_insensitive(self):
        provider = build_traffic_provider("HERE", api_key="k")
        assert isinstance(provider, HERETrafficProvider)

    def test_build_google(self):
        provider = build_traffic_provider("google", api_key="k")
        assert isinstance(provider, GoogleDirectionsTrafficProvider)

    def test_build_google_directions_alias(self):
        provider = build_traffic_provider("google_directions", api_key="k")
        assert isinstance(provider, GoogleDirectionsTrafficProvider)

    def test_rejects_unknown(self):
        with pytest.raises(ValueError):
            build_traffic_provider("waze")

    def test_rejects_blank_name(self):
        with pytest.raises(ValueError):
            build_traffic_provider("   ")


# ---------------------------------------------------------------------------
# Module constants surface area
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_defaults_match_spec(self):
        # Requirement 2.1.5 — 10-second timeout.
        assert DEFAULT_HTTP_TIMEOUT_SECONDS == 10.0
        # Requirement 2.1.4 — 900-second TTL + 15-minute bucket.
        assert DEFAULT_CACHE_TTL_SECONDS == 900
        assert BUCKET_SECONDS == 15 * 60
