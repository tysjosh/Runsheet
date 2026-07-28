"""
Unit tests for the Capability 1 filter extensions on
``GET /api/fuel/mvp/forecasts``.

The endpoint itself has served the fuel-distribution MVP since day one
and is tested generally in :mod:`tests.unit.test_mvp_endpoints`. Task 3.6
of the fuel-ops-hardening spec extends it with four new optional query
parameters — ``customer_tank_id``, ``customer_id``, ``customer_type``,
and ``fuel_type`` — plus alias-aware ``fuel_grade`` filtering.

These tests assert that each new query parameter surfaces as the correct
``term`` clause on the Elasticsearch query body so the router does not
silently drop them on its way to ES.

Validates: Requirements 1.1.4, 1.6.1.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from Agents.support.mvp_endpoints import configure_mvp_endpoints, router
from tests.support.auth_seam import auth_headers, install_test_auth


def _build_app() -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    es = MagicMock()
    es.search_documents = AsyncMock(
        return_value={
            "hits": {"hits": [], "total": {"value": 0}},
        }
    )
    configure_mvp_endpoints(pipeline=MagicMock(), es_service=es)
    # These endpoints depend on ``get_tenant_context``; without the
    # Test_Auth_Path override the dependency reaches the real SuperTokens
    # verifier and raises "Initialisation not done".
    install_test_auth(app)
    return app, es


def _client(app: FastAPI) -> TestClient:
    """A TestClient that carries an authenticated tenant scope on every call."""
    return TestClient(app, headers=auth_headers("t1"))


def _must_clauses_from_last_query(es: MagicMock) -> List[Dict[str, Any]]:
    assert es.search_documents.await_count >= 1
    call = es.search_documents.await_args_list[-1]
    # ``call.args`` is the positional tuple passed to search_documents.
    # ``search_documents(index, query, size)`` — so query is at index 1.
    args = call.args
    query = args[1] if len(args) > 1 else call.kwargs.get("query", {})
    return query.get("query", {}).get("bool", {}).get("must", [])


def _term_value(must: List[Dict[str, Any]], field: str) -> Any:
    for clause in must:
        if "term" in clause and field in clause["term"]:
            return clause["term"][field]
    return None


class TestForecastFilterExtensions:
    def test_customer_tank_id_lands_in_query(self):
        app, es = _build_app()
        client = _client(app)

        resp = client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "t1", "customer_tank_id": "ct_123"},
        )
        assert resp.status_code == 200
        assert _term_value(_must_clauses_from_last_query(es), "customer_tank_id") == "ct_123"

    def test_customer_id_lands_in_query(self):
        app, es = _build_app()
        client = _client(app)

        client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "t1", "customer_id": "cust-9"},
        )
        assert _term_value(_must_clauses_from_last_query(es), "customer_id") == "cust-9"

    def test_customer_type_lands_in_query(self):
        app, es = _build_app()
        client = _client(app)

        client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "t1", "customer_type": "residential"},
        )
        assert _term_value(_must_clauses_from_last_query(es), "customer_type") == "residential"

    def test_fuel_type_lands_in_query(self):
        app, es = _build_app()
        client = _client(app)

        client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "t1", "fuel_type": "propane"},
        )
        assert _term_value(_must_clauses_from_last_query(es), "fuel_type") == "propane"

    def test_fuel_grade_alias_is_canonicalized(self):
        app, es = _build_app()
        client = _client(app)

        client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "t1", "fuel_grade": "LPG"},
        )
        assert _term_value(_must_clauses_from_last_query(es), "fuel_grade") == "PROPANE"

    def test_unknown_fuel_grade_falls_back_to_raw(self):
        app, es = _build_app()
        client = _client(app)

        client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "t1", "fuel_grade": "UNOBTAINIUM"},
        )
        # Unknown grade passes through unchanged → empty result set.
        assert _term_value(_must_clauses_from_last_query(es), "fuel_grade") == "UNOBTAINIUM"

    def test_multiple_filters_are_combined(self):
        app, es = _build_app()
        client = _client(app)

        client.get(
            "/api/fuel/mvp/forecasts",
            params={
                "tenant_id": "t1",
                "customer_id": "cust-9",
                "customer_type": "commercial",
                "fuel_type": "diesel",
            },
        )
        must = _must_clauses_from_last_query(es)
        assert _term_value(must, "tenant_id") == "t1"
        assert _term_value(must, "customer_id") == "cust-9"
        assert _term_value(must, "customer_type") == "commercial"
        assert _term_value(must, "fuel_type") == "diesel"

    def test_tenant_scoping_always_present(self):
        """The query is always scoped to the SESSION tenant.

        A client-supplied ``tenant_id`` query parameter must never influence
        scoping (Req 5.1/5.2): the endpoint derives the tenant solely from the
        verified context, so passing a foreign ``tenant_id`` is ignored rather
        than honored.
        """
        app, es = _build_app()
        client = _client(app)  # authenticated as tenant "t1"

        client.get(
            "/api/fuel/mvp/forecasts",
            params={"tenant_id": "tenant-xyz"},
        )
        # Scoped to the credential-bound tenant, NOT the query parameter.
        assert _term_value(_must_clauses_from_last_query(es), "tenant_id") == "t1"
