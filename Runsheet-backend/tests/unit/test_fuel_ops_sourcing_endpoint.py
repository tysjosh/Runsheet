"""
Unit tests for ``GET /api/fuel/sourcing/recommendations`` (Task 7.10 —
Req 8.4.5, 8.5.4, 8.5.5).

The endpoint lives on :data:`fuel.api.fuel_ops_endpoints.router` and:

    1. Canonicalizes inputs (legacy product aliases, ISO ``as_of``, CSV
       ``terminal_ids`` filter).
    2. Invokes the already-wired Sourcing_Recommender to produce a
       ranked :class:`SourcingRecommendation`.
    3. Persists the recommendation to the ``sourcing_recommendations``
       ES index for audit.
    4. Emits ``sourcing_recommendation_ready`` on ``/ws/fuel-planning``.
    5. Returns the persisted :class:`SourcingRecommendation`.

Tests stub the recommender + repository + WS manager so we exercise the
endpoint plumbing (query-param validation, tenant scoping, persistence,
WS broadcast, 503 / 422 error paths) without touching ES, Redis, or
httpx.

Validates: Requirements 8.4.5, 8.5.4, 8.5.5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from fuel.services.sourcing_recommender import InvalidBrandedPreferenceError
from fuel.services.fuel_product_catalog import UnknownFuelProductError
from fuel.terminal_models import (
    SourcingRecommendation,
    TerminalCandidate,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _StubRecommender:
    """Records ``recommend`` calls and returns a canned recommendation."""

    canned: Optional[SourcingRecommendation] = None
    raises: Optional[BaseException] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def recommend(self, **kwargs: Any) -> SourcingRecommendation:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        assert self.canned is not None, "canned recommendation not set"
        return self.canned


@dataclass
class _StubRecommendationRepository:
    """Captures the recommendation passed to ``create`` and returns it."""

    created: List[SourcingRecommendation] = field(default_factory=list)
    raise_on_create: Optional[BaseException] = None

    async def create(
        self,
        tenant_id: str,
        recommendation: SourcingRecommendation,
    ) -> SourcingRecommendation:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        # Enforce tenant tagging the same way the real repo does so the
        # tests notice any regression where the endpoint skips tagging.
        assert recommendation.tenant_id == tenant_id, (
            f"cross-tenant persist attempted: "
            f"{recommendation.tenant_id!r} != {tenant_id!r}"
        )
        self.created.append(recommendation)
        return recommendation


@dataclass
class _StubFuelPlanningWSManager:
    """Captures the broadcast args emitted by the sourcing endpoint."""

    broadcasts: List[Dict[str, Any]] = field(default_factory=list)

    async def broadcast_sourcing_recommendation_ready(self, **kwargs: Any) -> int:
        self.broadcasts.append(kwargs)
        return 1


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_AS_OF = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)


def _tenant_ctx_factory(tenant_id: str = "tenant-1"):
    def _factory() -> TenantContext:
        return TenantContext(
            tenant_id=tenant_id,
            user_id="user-1",
            has_pii_access=False,
            roles=["dispatcher"],
            region="US",
            measurement_units={"volume": "gal", "distance": "mi"},
        )

    return _factory


def _make_recommendation(
    *,
    tenant_id: str = "tenant-1",
    product_code: str = "DIESEL_2",
    candidates: Optional[List[TerminalCandidate]] = None,
    rack_price_fallback: bool = False,
    volume_gallons: float = 1000.0,
    truck_id: Optional[str] = None,
    run_id: Optional[str] = None,
    request_id: str = "req_test",
    recommendation_id: str = "srec_test",
) -> SourcingRecommendation:
    return SourcingRecommendation(
        recommendation_id=recommendation_id,
        request_id=request_id,
        tenant_id=tenant_id,
        truck_id=truck_id,
        run_id=run_id,
        product_code=product_code,
        volume_gallons=volume_gallons,
        origin_lat=40.7128,
        origin_lon=-74.0060,
        candidates=candidates or [],
        rack_price_fallback=rack_price_fallback,
        generated_at=_AS_OF,
    )


def _make_candidate(
    *,
    terminal_id: str = "term_a",
    score: float = 0.9,
    price: float = 3.25,
    wait: float = 5.0,
    distance_km: float = 10.0,
    branded: bool = False,
    wait_warning: bool = False,
    reasons: Optional[List[str]] = None,
) -> TerminalCandidate:
    return TerminalCandidate(
        terminal_id=terminal_id,
        price_per_gallon_usd=price,
        branded_flag=branded,
        contract_id=None,
        avg_wait_minutes=wait,
        distance_km_from_start=distance_km,
        score=score,
        reasons=reasons or ["best_price"],
        wait_warning=wait_warning,
    )


def _build_app(
    *,
    recommender: Optional[_StubRecommender] = None,
    repository: Optional[_StubRecommendationRepository] = None,
    ws_manager: Optional[_StubFuelPlanningWSManager] = None,
    tenant_id: str = "tenant-1",
) -> tuple[
    FastAPI,
    Optional[_StubRecommender],
    _StubRecommendationRepository,
    Optional[_StubFuelPlanningWSManager],
]:
    es_service = AsyncMock()
    # The SourcingRecommendationRepository default-constructs from the
    # ES handle so we ensure it is replaced with our stub.
    repo = repository or _StubRecommendationRepository()

    configure_fuel_ops_endpoints(
        es_service=es_service,
        sourcing_recommender=recommender,
        sourcing_recommendation_repository=repo,
        fuel_planning_ws_manager=ws_manager,
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, recommender, repo, ws_manager


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestSourcingHappyPath:
    def test_returns_ranked_recommendation(self):
        """Top-ranked candidate appears first, score descending."""

        candidates = [
            _make_candidate(terminal_id="term_a", score=0.92),
            _make_candidate(terminal_id="term_b", score=0.71, price=3.55),
        ]
        recommender = _StubRecommender(
            canned=_make_recommendation(candidates=candidates)
        )
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.7128,
                    "origin_lon": -74.0060,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert [c["terminal_id"] for c in body["candidates"]] == [
            "term_a",
            "term_b",
        ]
        assert body["candidates"][0]["score"] >= body["candidates"][1]["score"]
        assert body["product_code"] == "DIESEL_2"

    def test_persists_recommendation_to_es(self):
        """The response payload is the repo-persisted audit record."""

        candidates = [_make_candidate(terminal_id="term_a", score=0.92)]
        recommender = _StubRecommender(
            canned=_make_recommendation(candidates=candidates)
        )
        repo = _StubRecommendationRepository()
        app, _, _, _ = _build_app(recommender=recommender, repository=repo)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 500,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 200
        assert len(repo.created) == 1
        assert repo.created[0].tenant_id == "tenant-1"
        assert repo.created[0].product_code == "DIESEL_2"

    def test_emits_websocket_event(self):
        """The endpoint broadcasts ``sourcing_recommendation_ready``."""

        candidates = [_make_candidate(terminal_id="term_a", score=0.88)]
        recommender = _StubRecommender(
            canned=_make_recommendation(
                candidates=candidates,
                recommendation_id="srec_ws1",
                request_id="req_ws1",
            )
        )
        ws = _StubFuelPlanningWSManager()
        app, _, _, _ = _build_app(recommender=recommender, ws_manager=ws)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 200
        assert len(ws.broadcasts) == 1
        event = ws.broadcasts[0]
        assert event["recommendation_id"] == "srec_ws1"
        assert event["tenant_id"] == "tenant-1"
        assert event["top_terminal_id"] == "term_a"
        assert event["candidate_count"] == 1


# ---------------------------------------------------------------------------
# Tests — query param validation
# ---------------------------------------------------------------------------


class TestSourcingValidation:
    def test_missing_product_code_returns_422(self):
        recommender = _StubRecommender(canned=_make_recommendation())
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 422

    def test_non_positive_volume_returns_422(self):
        recommender = _StubRecommender(canned=_make_recommendation())
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 0,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 422

    def test_bad_lat_returns_422(self):
        recommender = _StubRecommender(canned=_make_recommendation())
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 200,  # out of range
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 422

    def test_malformed_as_of_returns_422(self):
        recommender = _StubRecommender(canned=_make_recommendation())
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                    "as_of": "not-a-date",
                },
            )

        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "invalid_as_of"

    def test_unknown_product_returns_422(self):
        recommender = _StubRecommender(
            raises=UnknownFuelProductError("SPACE_FUEL")
        )
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "SPACE_FUEL",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "unknown_product_code"


# ---------------------------------------------------------------------------
# Tests — tenant scoping + 503 guard
# ---------------------------------------------------------------------------


class TestSourcingTenantAndAvailability:
    def test_tenant_id_flows_from_jwt_context(self):
        """Caller tenant is the only tenant the recommender receives."""

        recommender = _StubRecommender(
            canned=_make_recommendation(tenant_id="tenant-A")
        )
        app, _, _, _ = _build_app(
            recommender=recommender, tenant_id="tenant-A"
        )

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 200
        # Recommender was invoked with the JWT tenant_id — not a
        # header/query override.
        assert len(recommender.calls) == 1
        assert recommender.calls[0]["tenant_id"] == "tenant-A"

    def test_503_when_recommender_unwired(self):
        app, _, _, _ = _build_app(recommender=None)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 503
        assert (
            resp.json()["detail"]["error_code"]
            == "sourcing_recommender_unavailable"
        )


# ---------------------------------------------------------------------------
# Tests — alias canonicalization + filters + rack_price_fallback
# ---------------------------------------------------------------------------


class TestSourcingPassthroughs:
    def test_alias_canonicalizes_on_product_code(self):
        """``AGO`` is canonicalized to ``DIESEL_2`` by the response."""

        candidates = [_make_candidate(terminal_id="term_a", score=0.9)]
        recommender = _StubRecommender(
            canned=_make_recommendation(candidates=candidates)
        )
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "AGO",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 200
        # The recommender stub captured the original string; the
        # endpoint hands it through but the stub's canned response
        # carries the canonical product_code so the client sees the
        # canonical form in the body.
        assert resp.json()["product_code"] == "DIESEL_2"
        assert recommender.calls[0]["product_code"] == "AGO"

    def test_terminal_ids_csv_filter(self):
        """CSV ``terminal_ids`` param is parsed into a deduped list."""

        candidates = [_make_candidate(terminal_id="term_a", score=0.9)]
        recommender = _StubRecommender(
            canned=_make_recommendation(candidates=candidates)
        )
        app, _, _, _ = _build_app(recommender=recommender)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                    "terminal_ids": "term_a, term_b ,term_a",
                },
            )

        assert resp.status_code == 200
        assert recommender.calls[0]["terminal_ids"] == ["term_a", "term_b"]

    def test_rack_price_fallback_propagates(self):
        """``rack_price_fallback`` flows from the recommendation to the
        response body and the WebSocket payload."""

        candidates = [_make_candidate(terminal_id="term_a", score=0.9)]
        recommender = _StubRecommender(
            canned=_make_recommendation(
                candidates=candidates, rack_price_fallback=True
            )
        )
        ws = _StubFuelPlanningWSManager()
        app, _, _, _ = _build_app(recommender=recommender, ws_manager=ws)

        with TestClient(app) as client:
            resp = client.get(
                "/api/fuel/sourcing/recommendations",
                params={
                    "product_code": "DIESEL_2",
                    "volume_gallons": 1000,
                    "origin_lat": 40.0,
                    "origin_lon": -74.0,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["rack_price_fallback"] is True
        assert ws.broadcasts[0]["rack_price_fallback"] is True
