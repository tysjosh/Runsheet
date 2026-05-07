"""
Unit tests for :mod:`fuel.services.terminal_wait_resolver`.

Covers the centralized :class:`TerminalWaitResolver` introduced in
Task 7.7 so the Sourcing_Recommender (Task 7.9) and any other
consumer can read the same Redis-backed rolling 2-hour wait average
written by ``GET /api/fuel/terminals/{terminal_id}/wait-summary``.

The tests exercise the three axes that matter:

* **Redis fast path** — cached payloads with matching identity return
  the embedded ``avg_wait_minutes``. Cross-tenant or malformed payloads
  are dropped.
* **ES fallback** — when Redis is missing, empty, or corrupt the
  resolver aggregates the trailing 2-hour window from the repository.
* **Degraded paths** — None return values propagate as "no observation"
  so the Sourcing_Recommender's default-to-zero behaviour kicks in.

Validates: Requirements 8.4.2, 8.4.4.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from fuel.services.terminal_wait_resolver import (
    TERMINAL_WAIT_CACHE_KEY_TEMPLATE,
    TerminalWaitResolver,
    build_wait_time_resolver,
)


TENANT_ID = "tenant-1"
TERMINAL_ID = "term_001"


class _FakeRedis:
    def __init__(self, store: Optional[Dict[str, str]] = None) -> None:
        self.store: Dict[str, str] = dict(store or {})
        self.get_calls: List[str] = []

    async def get(self, key: str) -> Optional[str]:
        self.get_calls.append(key)
        return self.store.get(key)


class _FakeReport:
    def __init__(self, wait_minutes: float, observed_at: datetime) -> None:
        self.wait_minutes = wait_minutes
        self.observed_at = observed_at


class _FakeRepo:
    def __init__(self, reports: Optional[List[_FakeReport]] = None) -> None:
        self.reports = reports or []
        self.calls: List[Dict[str, Any]] = []

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        terminal_id: Optional[str] = None,
        observed_since: Optional[datetime] = None,
        size: int = 500,
        **_: Any,
    ) -> List[_FakeReport]:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "terminal_id": terminal_id,
                "observed_since": observed_since,
                "size": size,
            }
        )
        if terminal_id is None:
            return list(self.reports)
        return list(self.reports)


class _ExplodingRedis:
    async def get(self, key: str) -> Optional[str]:
        raise RuntimeError("redis down")


class _ExplodingRepo:
    async def list_for_tenant(self, *_: Any, **__: Any) -> List[_FakeReport]:
        raise RuntimeError("es down")


def _cached_payload(
    *,
    tenant_id: str = TENANT_ID,
    terminal_id: str = TERMINAL_ID,
    avg: float = 42.0,
    samples: int = 3,
) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "terminal_id": terminal_id,
        "avg_wait_minutes": avg,
        "sample_count": samples,
        "max_wait_minutes": avg,
        "window_minutes": 120,
        "window_start": "2024-01-01T00:00:00+00:00",
        "window_end": "2024-01-01T02:00:00+00:00",
        "generated_at": "2024-01-01T02:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_at_least_one_backing_store(self):
        with pytest.raises(ValueError):
            TerminalWaitResolver(
                redis_client=None, wait_report_repository=None
            )

    def test_rejects_non_positive_window(self):
        with pytest.raises(ValueError):
            TerminalWaitResolver(
                redis_client=_FakeRedis(), window=timedelta(0)
            )


# ---------------------------------------------------------------------------
# Redis fast path
# ---------------------------------------------------------------------------


class TestRedisPath:
    async def test_returns_cached_avg_when_payload_matches(self):
        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id=TERMINAL_ID
        )
        redis = _FakeRedis({key: json.dumps(_cached_payload(avg=25.0))})
        resolver = TerminalWaitResolver(redis_client=redis)

        value = await resolver.resolve(TENANT_ID, TERMINAL_ID)

        assert value == 25.0
        assert key in redis.get_calls

    async def test_empty_window_returns_none(self):
        """A cached payload with ``sample_count == 0`` must surface as
        ``None`` — the consumer should treat that as missing telemetry,
        not as "0 wait"."""

        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id=TERMINAL_ID
        )
        redis = _FakeRedis(
            {key: json.dumps(_cached_payload(avg=0.0, samples=0))}
        )
        resolver = TerminalWaitResolver(redis_client=redis)

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) is None

    async def test_cross_tenant_cache_discarded(self):
        """A cached payload whose tenant_id doesn't match must be
        ignored — defense against a Redis with coincident keys across
        tenants."""

        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id=TERMINAL_ID
        )
        poisoned = _cached_payload(tenant_id="tenant-other", avg=999.0)
        redis = _FakeRedis({key: json.dumps(poisoned)})
        repo = _FakeRepo()  # empty → resolver falls back to None
        resolver = TerminalWaitResolver(
            redis_client=redis, wait_report_repository=repo
        )

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) is None

    async def test_malformed_payload_falls_back_to_es(self):
        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id=TERMINAL_ID
        )
        redis = _FakeRedis({key: "not-json"})
        now = datetime.now(timezone.utc)
        repo = _FakeRepo(
            [_FakeReport(33.0, now - timedelta(minutes=10))]
        )
        resolver = TerminalWaitResolver(
            redis_client=redis, wait_report_repository=repo
        )

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) == 33.0
        assert repo.calls  # ES was consulted


# ---------------------------------------------------------------------------
# ES fallback
# ---------------------------------------------------------------------------


class TestESFallback:
    async def test_aggregates_mean_over_window(self):
        now = datetime.now(timezone.utc)
        repo = _FakeRepo(
            [
                _FakeReport(20.0, now - timedelta(minutes=10)),
                _FakeReport(40.0, now - timedelta(minutes=30)),
                _FakeReport(60.0, now - timedelta(minutes=90)),
            ]
        )
        resolver = TerminalWaitResolver(wait_report_repository=repo)

        value = await resolver.resolve(TENANT_ID, TERMINAL_ID)

        assert value == pytest.approx(40.0, abs=1e-6)

    async def test_empty_window_returns_none(self):
        repo = _FakeRepo([])
        resolver = TerminalWaitResolver(wait_report_repository=repo)

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) is None

    async def test_future_observations_excluded(self):
        """Belt-and-braces: reports with observed_at > now are dropped
        so clock-skew / spoofed timestamps don't pollute the mean."""

        now = datetime.now(timezone.utc)
        repo = _FakeRepo(
            [
                _FakeReport(10.0, now - timedelta(minutes=5)),
                _FakeReport(999.0, now + timedelta(minutes=5)),  # bad
            ]
        )
        resolver = TerminalWaitResolver(wait_report_repository=repo)

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) == 10.0


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------


class TestDegradedPaths:
    async def test_redis_exception_falls_back_to_es(self):
        now = datetime.now(timezone.utc)
        repo = _FakeRepo([_FakeReport(12.0, now - timedelta(minutes=5))])
        resolver = TerminalWaitResolver(
            redis_client=_ExplodingRedis(), wait_report_repository=repo
        )

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) == 12.0

    async def test_es_exception_returns_none(self):
        resolver = TerminalWaitResolver(
            wait_report_repository=_ExplodingRepo()
        )

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) is None

    async def test_redis_only_no_cache_returns_none(self):
        """With only Redis wired and no cache entry the resolver must
        return ``None`` — the caller (Sourcing_Recommender) then
        defaults to 0 wait."""

        resolver = TerminalWaitResolver(redis_client=_FakeRedis())

        assert await resolver.resolve(TENANT_ID, TERMINAL_ID) is None


# ---------------------------------------------------------------------------
# build_wait_time_resolver
# ---------------------------------------------------------------------------


class TestBuildWaitTimeResolver:
    async def test_returns_awaitable_matching_sourcing_protocol(self):
        """The shipped factory must return an async callable the
        Sourcing_Recommender can plug in verbatim as
        ``wait_time_resolver``."""

        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id=TERMINAL_ID
        )
        redis = _FakeRedis({key: json.dumps(_cached_payload(avg=17.5))})

        resolver = build_wait_time_resolver(redis_client=redis)
        value = await resolver(TENANT_ID, TERMINAL_ID)

        assert value == 17.5


class TestInputValidation:
    async def test_blank_tenant_rejected(self):
        resolver = TerminalWaitResolver(redis_client=_FakeRedis())
        with pytest.raises(ValueError):
            await resolver.resolve("", TERMINAL_ID)

    async def test_blank_terminal_rejected(self):
        resolver = TerminalWaitResolver(redis_client=_FakeRedis())
        with pytest.raises(ValueError):
            await resolver.resolve(TENANT_ID, "  ")
