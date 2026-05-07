"""
Unit tests for the Task 7.7 Terminal_Wait endpoints.

Covers the two endpoints mounted on
:data:`fuel.api.fuel_ops_endpoints.router`:

* ``POST /api/fuel/terminals/{terminal_id}/wait-reports`` — accepts a
  driver / dispatcher wait-time submission and persists a
  :class:`TerminalWaitReport` to the ``terminal_wait_reports`` index.
  Req 8.4.2.

* ``GET /api/fuel/terminals/{terminal_id}/wait-summary`` — returns the
  rolling 2-hour mean wait time for the terminal and caches it at
  ``terminal_wait:{tenant_id}:{terminal_id}`` in Redis so the
  Sourcing_Recommender (Task 7.9) can consume it without re-scanning ES.
  Req 8.4.4.

The tests use a minimal in-memory Redis stub and a
:class:`_FakeESService` that implements the subset of methods the
repositories rely on. Pattern matches the sibling
``test_fuel_ops_depot_endpoints`` / ``test_fuel_ops_customer_tank_endpoints``
suites so the assertions are consistent with the rest of the fuel-ops
endpoint coverage.

Validates: Requirements 8.4.2, 8.4.4.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    TERMINAL_WAIT_CACHE_KEY_TEMPLATE,
    TERMINAL_WAIT_CACHE_TTL_SECONDS,
    WAIT_SUMMARY_WINDOW,
    configure_fuel_ops_endpoints,
    router,
)
from fuel.terminal_models import (
    SupplierContractRepository,
    TerminalRepository,
    TerminalWaitReportRepository,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


TENANT_ID = "tenant-1"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeESService:
    """Minimal async ES stub covering the subset used by the repositories.

    Implements ``index_document``, ``search_documents``,
    ``update_document``, and ``delete_document`` over an in-memory dict
    keyed by ``doc_id``. The repositories drive all lookups through
    ``search_documents`` with a ``term`` clause on ``tenant_id`` plus
    optional filters (``terminal_id``, ``source``, ``observed_at``
    range), so this stub honours those shapes without pulling in a real
    ES client.
    """

    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.index_calls: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []

    async def index_document(
        self, index: str, doc_id: str, document: Dict[str, Any]
    ) -> None:
        self.docs[doc_id] = dict(document)
        self.index_calls.append(
            {"index": index, "doc_id": doc_id, "doc": dict(document)}
        )

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        self.search_calls.append({"index": index, "query": dict(query)})

        must = query.get("query", {}).get("bool", {}).get("must", [])
        tenant_id: Optional[str] = None
        terminal_id: Optional[str] = None
        report_id: Optional[str] = None
        wait_id: Optional[str] = None
        source_filter: Optional[str] = None
        id_lookup: Optional[str] = None
        observed_since: Optional[str] = None

        for clause in must:
            term = clause.get("term") if isinstance(clause, dict) else None
            if term:
                for field, value in term.items():
                    if field == "tenant_id":
                        tenant_id = value
                    elif field == "terminal_id":
                        terminal_id = value
                    elif field == "source":
                        source_filter = value
                    elif field == "report_id":
                        id_lookup = value
                    elif field in ("contract_id",):
                        id_lookup = value
                continue
            rng = clause.get("range") if isinstance(clause, dict) else None
            if rng and "observed_at" in rng:
                observed_since = rng["observed_at"].get("gte")

        if id_lookup is not None:
            doc = self.docs.get(id_lookup)
            if doc is None:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            return {
                "hits": {
                    "hits": [{"_source": dict(doc)}],
                    "total": {"value": 1},
                }
            }

        matches: List[Dict[str, Any]] = []
        for doc in self.docs.values():
            if index == "terminals" and "terminal_id" not in doc:
                continue
            if index == "terminal_wait_reports" and "report_id" not in doc:
                continue
            if tenant_id is not None and doc.get("tenant_id") != tenant_id:
                continue
            if terminal_id is not None and doc.get("terminal_id") != terminal_id:
                continue
            if source_filter is not None and doc.get("source") != source_filter:
                continue
            if observed_since is not None:
                observed_at = doc.get("observed_at")
                if observed_at is None or str(observed_at) < observed_since:
                    continue
            matches.append({"_source": dict(doc)})

        matches = matches[:size]
        return {
            "hits": {"hits": matches, "total": {"value": len(matches)}}
        }

    async def update_document(
        self, index: str, doc_id: str, partial: Dict[str, Any]
    ) -> None:
        existing = self.docs.get(doc_id)
        if existing is None:
            raise RuntimeError(f"update_document called for missing {doc_id}")
        existing.update(partial)

    async def delete_document(self, index: str, doc_id: str) -> bool:
        return self.docs.pop(doc_id, None) is not None


class _FakeRedis:
    """Minimal async Redis stub: ``get`` / ``setex`` / ``delete``.

    The wait-summary endpoint only uses these three operations, so we
    keep the stub small. ``setex`` stores (value, ttl) tuples so tests
    can assert on the TTL when needed.
    """

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.delete_calls: List[str] = []

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value
        self.ttls[key] = int(ttl)

    async def delete(self, key: str) -> int:
        self.delete_calls.append(key)
        return 1 if self.store.pop(key, None) is not None else 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(
    tenant_id: str = TENANT_ID, user_id: str = "user-1"
):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id=user_id,
            has_pii_access=False,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _seed_terminal(
    es: _FakeESService,
    *,
    terminal_id: str = "term_001",
    tenant_id: str = TENANT_ID,
    branded: bool = False,
    supplier_brand: Optional[str] = None,
) -> None:
    """Seed a minimal Terminal document into the fake ES store."""

    es.docs[terminal_id] = {
        "terminal_id": terminal_id,
        "tenant_id": tenant_id,
        "name": "Newark Rack",
        "operator": "Buckeye",
        "location_lat": 40.73,
        "location_lon": -74.17,
        "address": "1 Fuel Lane, Newark NJ",
        "timezone": "America/New_York",
        "operating_hours": [
            {"day_of_week": "mon", "open": "06:00", "close": "20:00"}
        ],
        "supported_products": ["DIESEL_2"],
        "branded": branded,
        "supplier_brand": supplier_brand,
        "status": "active",
    }


def _build_app(
    *,
    tenant_id: str = TENANT_ID,
    redis_client: Optional[_FakeRedis] = None,
) -> tuple[FastAPI, _FakeESService, Optional[_FakeRedis]]:
    es = _FakeESService()
    configure_fuel_ops_endpoints(
        es_service=es,
        terminal_repository=TerminalRepository(es_service=es),
        supplier_contract_repository=SupplierContractRepository(es_service=es),
        terminal_wait_report_repository=TerminalWaitReportRepository(
            es_service=es
        ),
        redis_client=redis_client,
    )

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es, redis_client


# ---------------------------------------------------------------------------
# POST /api/fuel/terminals/{terminal_id}/wait-reports
# ---------------------------------------------------------------------------


class TestSubmitTerminalWaitReport:
    def test_driver_report_persists_and_returns_201(self):
        app, es, redis = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        client = TestClient(app)

        observed = (
            datetime.now(timezone.utc) - timedelta(minutes=30)
        ).isoformat()
        resp = client.post(
            "/api/fuel/terminals/term_001/wait-reports",
            json={
                "wait_minutes": 45.0,
                "source": "driver_report",
                "reporter_id": "driver_017",
                "observed_at": observed,
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["tenant_id"] == TENANT_ID
        assert body["terminal_id"] == "term_001"
        assert body["wait_minutes"] == 45.0
        assert body["source"] == "driver_report"
        assert body["reporter_id"] == "driver_017"
        # ``retrieved_at`` stamped by the repository must be >=
        # ``observed_at`` per the Pydantic validator.
        assert body["retrieved_at"] >= body["observed_at"]

    def test_reporter_id_defaults_to_jwt_user_id(self):
        """When the caller omits ``reporter_id`` for a ``driver_report`` we
        stamp it from the JWT ``user_id`` so the report is always
        attributable."""

        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/term_001/wait-reports",
            json={"wait_minutes": 12.5},
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["reporter_id"] == "user-1"  # from _tenant_ctx_factory
        assert body["source"] == "driver_report"  # default

    def test_missing_terminal_returns_404(self):
        app, _, _ = _build_app(redis_client=_FakeRedis())
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/term_missing/wait-reports",
            json={"wait_minutes": 15.0, "reporter_id": "driver_001"},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_cross_tenant_terminal_returns_404(self):
        """A terminal owned by another tenant must surface as 404 — the
        same response as 'missing' so existence isn't leaked."""

        app, es, _ = _build_app(tenant_id="tenant-a")
        _seed_terminal(es, tenant_id="tenant-b")
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/term_001/wait-reports",
            json={"wait_minutes": 10.0, "reporter_id": "driver_001"},
        )

        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_negative_wait_minutes_rejected(self):
        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/term_001/wait-reports",
            json={"wait_minutes": -1.0, "reporter_id": "driver_001"},
        )

        # FastAPI surfaces Pydantic ``ge=0`` failures as 422.
        assert resp.status_code == 422

    def test_eld_geofence_report_allowed_without_reporter(self):
        """Automatic ELD-derived reports don't have a reporter_id — the
        Pydantic validator on :class:`TerminalWaitReport` allows this
        case."""

        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/term_001/wait-reports",
            json={
                "wait_minutes": 22.0,
                "source": "eld_geofence",
                "reporter_id": None,
                "truck_id": "truck-17",
            },
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["source"] == "eld_geofence"
        assert resp.json()["truck_id"] == "truck-17"

    def test_submission_invalidates_summary_cache(self):
        """A successful wait-report submission must delete any cached
        summary at ``terminal_wait:{tenant_id}:{terminal_id}`` so the
        next summary read picks up the new observation immediately."""

        redis = _FakeRedis()
        cached_key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id="term_001"
        )
        redis.store[cached_key] = json.dumps(
            {
                "tenant_id": TENANT_ID,
                "terminal_id": "term_001",
                "avg_wait_minutes": 3.14,
                "sample_count": 1,
                "max_wait_minutes": 3.14,
                "window_minutes": 120,
                "window_start": "2024-01-01T00:00:00+00:00",
                "window_end": "2024-01-01T02:00:00+00:00",
                "generated_at": "2024-01-01T02:00:00+00:00",
                "source": "cache",
            }
        )
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.post(
            "/api/fuel/terminals/term_001/wait-reports",
            json={"wait_minutes": 99.0, "reporter_id": "driver_017"},
        )

        assert resp.status_code == 201, resp.text
        # Cache key must have been dropped so the next summary read
        # re-aggregates from ES.
        assert cached_key not in redis.store
        assert cached_key in redis.delete_calls


# ---------------------------------------------------------------------------
# GET /api/fuel/terminals/{terminal_id}/wait-summary
# ---------------------------------------------------------------------------


def _seed_wait_report(
    es: _FakeESService,
    *,
    report_id: str,
    terminal_id: str = "term_001",
    tenant_id: str = TENANT_ID,
    wait_minutes: float,
    observed_at: datetime,
    source: str = "driver_report",
    reporter_id: str = "driver_001",
) -> None:
    retrieved_at = observed_at + timedelta(seconds=1)
    es.docs[report_id] = {
        "report_id": report_id,
        "tenant_id": tenant_id,
        "terminal_id": terminal_id,
        "wait_minutes": wait_minutes,
        "source": source,
        "reporter_id": reporter_id,
        "truck_id": None,
        "observed_at": observed_at.isoformat(),
        "retrieved_at": retrieved_at.isoformat(),
    }


class TestGetTerminalWaitSummary:
    def test_empty_summary_when_no_reports(self):
        app, es, redis = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["terminal_id"] == "term_001"
        assert body["tenant_id"] == TENANT_ID
        assert body["sample_count"] == 0
        assert body["avg_wait_minutes"] == 0.0
        assert body["max_wait_minutes"] is None
        assert body["window_minutes"] == 120
        assert body["source"] == "computed"

    def test_rolling_average_over_2_hour_window(self):
        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)

        now = datetime.now(timezone.utc)
        _seed_wait_report(
            es,
            report_id="twr_1",
            wait_minutes=30.0,
            observed_at=now - timedelta(minutes=15),
        )
        _seed_wait_report(
            es,
            report_id="twr_2",
            wait_minutes=60.0,
            observed_at=now - timedelta(minutes=45),
        )
        _seed_wait_report(
            es,
            report_id="twr_3",
            wait_minutes=90.0,
            observed_at=now - timedelta(minutes=90),
        )
        # Outside the 2-hour window → excluded.
        _seed_wait_report(
            es,
            report_id="twr_old",
            wait_minutes=500.0,
            observed_at=now - timedelta(hours=3),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sample_count"] == 3
        assert body["avg_wait_minutes"] == pytest.approx(60.0, abs=1e-6)
        assert body["max_wait_minutes"] == 90.0

    def test_cross_tenant_wait_reports_excluded(self):
        """Reports written under another tenant (legitimately or via
        mis-seeding) must never appear in this tenant's summary."""

        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        now = datetime.now(timezone.utc)
        _seed_wait_report(
            es,
            report_id="twr_mine",
            wait_minutes=10.0,
            observed_at=now - timedelta(minutes=5),
        )
        _seed_wait_report(
            es,
            report_id="twr_other",
            tenant_id="tenant-other",
            wait_minutes=999.0,
            observed_at=now - timedelta(minutes=5),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sample_count"] == 1
        assert body["avg_wait_minutes"] == 10.0

    def test_missing_terminal_returns_404(self):
        app, _, _ = _build_app(redis_client=_FakeRedis())
        client = TestClient(app)

        resp = client.get("/api/fuel/terminals/term_missing/wait-summary")

        assert resp.status_code == 404
        assert resp.json()["detail"]["error_code"] == "terminal_not_found"

    def test_summary_cached_to_redis_after_compute(self):
        redis = _FakeRedis()
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)

        now = datetime.now(timezone.utc)
        _seed_wait_report(
            es,
            report_id="twr_1",
            wait_minutes=42.0,
            observed_at=now - timedelta(minutes=10),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        cached_key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id="term_001"
        )
        assert cached_key in redis.store
        cached_payload = json.loads(redis.store[cached_key])
        assert cached_payload["tenant_id"] == TENANT_ID
        assert cached_payload["terminal_id"] == "term_001"
        assert cached_payload["avg_wait_minutes"] == 42.0
        # TTL is set to the module-level constant (15 minutes).
        assert redis.ttls[cached_key] == TERMINAL_WAIT_CACHE_TTL_SECONDS

    def test_cache_hit_short_circuits_aggregation(self):
        """A valid cached payload must be returned without re-querying
        the ``terminal_wait_reports`` index. We assert this by pre-
        populating the cache with distinctive values and verifying the
        endpoint does not touch ES for wait reports."""

        redis = _FakeRedis()
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)

        cached_key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id="term_001"
        )
        now = datetime.now(timezone.utc)
        cached_payload = {
            "tenant_id": TENANT_ID,
            "terminal_id": "term_001",
            "window_minutes": 120,
            "avg_wait_minutes": 13.37,
            "sample_count": 4,
            "max_wait_minutes": 50.0,
            "window_start": (now - WAIT_SUMMARY_WINDOW).isoformat(),
            "window_end": now.isoformat(),
            "generated_at": now.isoformat(),
            "source": "cache",
        }
        redis.store[cached_key] = json.dumps(cached_payload)

        client = TestClient(app)
        # Reset the ES search-call log so the terminal lookup earlier
        # doesn't count.
        pre_call_count = len(es.search_calls)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")
        assert resp.status_code == 200, resp.text

        body = resp.json()
        assert body["source"] == "cache"
        assert body["avg_wait_minutes"] == 13.37
        assert body["sample_count"] == 4
        # The endpoint must have queried ES only for the terminal-
        # ownership check, not for the wait-reports aggregation. The
        # terminal lookup is a single search; the wait-reports query
        # would be a second one.
        post_call_count = len(es.search_calls)
        wait_searches = [
            c for c in es.search_calls[pre_call_count:post_call_count]
            if c["index"] == "terminal_wait_reports"
        ]
        assert wait_searches == []

    def test_cache_with_mismatched_identity_is_ignored(self):
        """A cache entry tagged with a different ``tenant_id`` or
        ``terminal_id`` is dropped on the way in so coincident-key bugs
        never leak cross-tenant wait data."""

        redis = _FakeRedis()
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)

        cached_key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id="term_001"
        )
        redis.store[cached_key] = json.dumps(
            {
                "tenant_id": "tenant-other",
                "terminal_id": "term_001",
                "avg_wait_minutes": 999.0,
                "sample_count": 99,
                "max_wait_minutes": 999.0,
                "window_minutes": 120,
                "window_start": "2024-01-01T00:00:00+00:00",
                "window_end": "2024-01-01T02:00:00+00:00",
                "generated_at": "2024-01-01T02:00:00+00:00",
                "source": "cache",
            }
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The poisoned cache was discarded; the re-compute found no
        # samples and returned zeros with ``source=computed``.
        assert body["source"] == "computed"
        assert body["sample_count"] == 0

    def test_no_redis_client_still_returns_summary(self):
        """When Redis is not wired (bootstrap ordering, tests) the
        endpoint must still compute and return the summary — just
        without the caching benefit."""

        app, es, _ = _build_app(redis_client=None)
        _seed_terminal(es)

        now = datetime.now(timezone.utc)
        _seed_wait_report(
            es,
            report_id="twr_1",
            wait_minutes=77.0,
            observed_at=now - timedelta(minutes=5),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["sample_count"] == 1
        assert body["avg_wait_minutes"] == 77.0
        assert body["source"] == "computed"


# ---------------------------------------------------------------------------
# Redis key contract
# ---------------------------------------------------------------------------


class TestRedisKeyContract:
    def test_cache_key_template_matches_spec(self):
        """Task 7.7 mandates ``terminal_wait:{tenant_id}:{terminal_id}``
        as the Redis key so the Sourcing_Recommender (Task 7.9) can
        read it. Lock the format down with a dedicated test so future
        refactors do not silently break that contract."""

        key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id="tenant-1", terminal_id="term_001"
        )
        assert key == "terminal_wait:tenant-1:term_001"


# ---------------------------------------------------------------------------
# Task 7.7 response enrichments (most_recent_report_at + wait_warning_*)
# ---------------------------------------------------------------------------


class TestSummaryEnrichments:
    """Lock down the Task 7.7 extras the Sourcing_Recommender and UI
    depend on:

    * ``most_recent_report_at`` — the newest ``observed_at`` in the
      window, or null when empty.
    * ``wait_warning_threshold_minutes`` — tenant-configured threshold
      read from ``terminal_wait_warning_minutes:{tenant_id}`` with the
      shipped 60-minute default.
    * ``wait_warning_exceeded`` — True iff ``avg_wait_minutes`` >
      threshold and ``sample_count > 0``.
    """

    def test_most_recent_report_at_returned(self):
        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        now = datetime.now(timezone.utc)
        # Newest observation is ``twr_new`` at now-5m.
        _seed_wait_report(
            es,
            report_id="twr_old",
            wait_minutes=10.0,
            observed_at=now - timedelta(minutes=60),
        )
        _seed_wait_report(
            es,
            report_id="twr_new",
            wait_minutes=30.0,
            observed_at=now - timedelta(minutes=5),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["most_recent_report_at"] is not None
        # The newest report's timestamp must round-trip. Compare as
        # datetimes to paper over "Z" vs "+00:00" serialization
        # differences between Pydantic and the ES repository.
        expected = datetime.fromisoformat(
            es.docs["twr_new"]["observed_at"]
        )
        actual = datetime.fromisoformat(
            body["most_recent_report_at"].replace("Z", "+00:00")
        )
        assert actual == expected

    def test_empty_window_returns_null_most_recent(self):
        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["most_recent_report_at"] is None
        assert body["sample_count"] == 0

    def test_default_threshold_is_60_minutes(self):
        """When the tenant has no ``terminal_wait_warning_minutes:{tid}``
        Redis key, the threshold defaults to 60."""

        app, es, _ = _build_app(redis_client=_FakeRedis())
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["wait_warning_threshold_minutes"] == 60.0
        # Empty window never trips the warning.
        assert body["wait_warning_exceeded"] is False

    def test_warning_exceeded_true_when_avg_above_threshold(self):
        redis = _FakeRedis()
        # Configure a tenant threshold of 30 minutes so our 45-minute
        # observation trips the warning.
        redis.store["terminal_wait_warning_minutes:" + TENANT_ID] = "30"
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)

        now = datetime.now(timezone.utc)
        _seed_wait_report(
            es,
            report_id="twr_1",
            wait_minutes=45.0,
            observed_at=now - timedelta(minutes=10),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["wait_warning_threshold_minutes"] == 30.0
        assert body["avg_wait_minutes"] == 45.0
        assert body["wait_warning_exceeded"] is True

    def test_warning_not_exceeded_when_equal_to_threshold(self):
        """Strict inequality: ``avg == threshold`` does not trip the
        warning. Avoids flapping when the mean sits right on the
        boundary."""

        redis = _FakeRedis()
        redis.store["terminal_wait_warning_minutes:" + TENANT_ID] = "45"
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)

        now = datetime.now(timezone.utc)
        _seed_wait_report(
            es,
            report_id="twr_1",
            wait_minutes=45.0,
            observed_at=now - timedelta(minutes=10),
        )

        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["wait_warning_exceeded"] is False

    def test_malformed_threshold_falls_back_to_default(self):
        redis = _FakeRedis()
        redis.store["terminal_wait_warning_minutes:" + TENANT_ID] = "not-a-number"
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)
        client = TestClient(app)

        resp = client.get("/api/fuel/terminals/term_001/wait-summary")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["wait_warning_threshold_minutes"] == 60.0

    def test_threshold_recomputed_on_cache_hit(self):
        """Threshold changes must be reflected on the next read even
        when the summary itself is served from cache — we never cache
        the warning booleans."""

        redis = _FakeRedis()
        app, es, _ = _build_app(redis_client=redis)
        _seed_terminal(es)

        # Seed a cached summary with avg = 45.
        cached_key = TERMINAL_WAIT_CACHE_KEY_TEMPLATE.format(
            tenant_id=TENANT_ID, terminal_id="term_001"
        )
        now = datetime.now(timezone.utc)
        cached_payload = {
            "tenant_id": TENANT_ID,
            "terminal_id": "term_001",
            "window_minutes": 120,
            "avg_wait_minutes": 45.0,
            "sample_count": 2,
            "max_wait_minutes": 60.0,
            "most_recent_report_at": (now - timedelta(minutes=5)).isoformat(),
            "window_start": (now - WAIT_SUMMARY_WINDOW).isoformat(),
            "window_end": now.isoformat(),
            "generated_at": now.isoformat(),
            "source": "cache",
        }
        redis.store[cached_key] = json.dumps(cached_payload)

        # With threshold 60 the warning is not tripped.
        redis.store["terminal_wait_warning_minutes:" + TENANT_ID] = "60"
        client = TestClient(app)
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "cache"
        assert resp.json()["wait_warning_exceeded"] is False

        # Lower the threshold; the same cached summary must now trip
        # the warning on the very next read.
        redis.store["terminal_wait_warning_minutes:" + TENANT_ID] = "30"
        resp = client.get("/api/fuel/terminals/term_001/wait-summary")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "cache"
        assert body["wait_warning_threshold_minutes"] == 30.0
        assert body["wait_warning_exceeded"] is True
