"""
Unit tests for the GET
``/api/fuel/mvp/compartments/{compartment_id}/load-eligibility`` endpoint
added by Task 6.7 of the fuel-ops-hardening spec.

Validates: Requirement 7.2.5.

The tests exercise the full wiring (
:func:`configure_fuel_ops_endpoints` → :func:`check_compatibility` →
:class:`CompartmentStateRepository`) with an injected fake repository so
the suite is decoupled from Elasticsearch while still covering:

* The happy ``allowed`` path for compatible products.
* ``blocked`` with the ``cross_contamination_blocked`` reason and the
  ``blocked`` governing rule.
* ``requires_cleaning`` with the ``cleaning_required`` reason on a
  compartment that has not been cleaned since its last load.
* Downgrade from ``requires_cleaning`` to ``allowed`` when a fresh
  Cleaning_Event exists (Req 7.2.4), with the governing rule preserved
  so callers can see *why* the decision was downgraded.
* Legacy NG product-code alias canonicalization (AGO → DIESEL_2, PMS →
  GASOLINE_REG, LPG → PROPANE) on both the previous product (stamped in
  state) and the proposed product (query parameter).
* Empty / freshly-cleaned compartment short-circuits to ``allowed``.
* 404 on missing compartment.
* 404 on cross-tenant compartment (existence never leaked).
* 422 on unknown product code.
* 400 on blank path / query parameters.
* Tenant overrides via :func:`load_tenant_compatibility_rules` are
  applied when a ``tenant_config`` handle is wired.
* Tenant override config outage degrades gracefully to the default rule
  table (graceful-degradation contract with the Compartment_Loading_Agent).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    mvp_router,
    router,
)
from fuel.compartment_state_models import CompartmentState
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStateRepository:
    """Minimal stand-in for :class:`CompartmentStateRepository` exposing
    just the :meth:`get` method the endpoint relies on."""

    def __init__(self) -> None:
        self.states: Dict[str, CompartmentState] = {}
        self.get_calls: List[tuple[str, str]] = []

    def seed(self, state: CompartmentState) -> None:
        self.states[state.compartment_id] = state

    async def get(
        self, tenant_id: str, compartment_doc_id: str
    ) -> Optional[CompartmentState]:
        self.get_calls.append((tenant_id, compartment_doc_id))
        state = self.states.get(compartment_doc_id)
        if state is None:
            return None
        # Mirror the real repository: cross-tenant reads are suppressed.
        if state.tenant_id != tenant_id:
            return None
        return state


class _FakeTenantConfig:
    """Async Redis-handle stand-in; matches the agent-side contract."""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.data: Dict[str, Any] = dict(data or {})
        self.calls: list[str] = []

    async def get(self, key: str) -> Any:
        self.calls.append(key)
        return self.data.get(key)


class _RaisingTenantConfig:
    async def get(self, key: str) -> Any:
        raise RuntimeError("redis down")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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


def _build_app(
    *,
    tenant_id: str = "tenant-1",
    seed_states: Optional[List[CompartmentState]] = None,
    state_repo: Optional[_FakeStateRepository] = None,
    tenant_config: Any = None,
):
    state_repo = state_repo or _FakeStateRepository()
    for state in seed_states or []:
        state_repo.seed(state)

    es_stub = mock.MagicMock()
    configure_fuel_ops_endpoints(
        es_service=es_stub,
        destination_service=mock.MagicMock(
            list=mock.AsyncMock(return_value=[])
        ),
        customer_tank_repository=mock.MagicMock(),
        depot_repository=mock.MagicMock(),
        terminal_repository=mock.MagicMock(),
        compartment_state_repository=state_repo,
        cleaning_event_service=mock.MagicMock(),
        tenant_config=tenant_config,
    )

    app = FastAPI()
    app.include_router(router)
    app.include_router(mvp_router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, state_repo


def _compartment_state(
    *,
    compartment_id: str = "truck-1_c1",
    truck_id: str = "truck-1",
    tenant_id: str = "tenant-1",
    state: str = "loaded",
    last_loaded_product: Optional[str] = "DIESEL_2",
    last_loaded_at: Optional[datetime] = None,
    last_cleaned_at: Optional[datetime] = None,
) -> CompartmentState:
    if last_loaded_at is None and last_loaded_product is not None:
        last_loaded_at = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    return CompartmentState(
        compartment_id=compartment_id,
        truck_id=truck_id,
        tenant_id=tenant_id,
        state=state,  # type: ignore[arg-type]
        last_loaded_product=last_loaded_product,
        last_loaded_at=last_loaded_at,
        last_cleaned_at=last_cleaned_at,
    )


def _url(compartment_id: str) -> str:
    return f"/api/fuel/mvp/compartments/{compartment_id}/load-eligibility"


# ---------------------------------------------------------------------------
# Happy path — each decision branch
# ---------------------------------------------------------------------------


class TestLoadEligibilityAllowed:
    def test_same_product_is_allowed(self):
        state = _compartment_state(last_loaded_product="DIESEL_2")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["reason"] is None
        assert body["governing_rule"] == "allowed"
        assert body["compartment_id"] == state.compartment_id
        assert body["previous_product"] == "DIESEL_2"
        assert body["proposed_product"] == "DIESEL_2"
        # Compartment state block echoes the inputs that drove the decision.
        cs = body["compartment_state"]
        assert cs["compartment_id"] == state.compartment_id
        assert cs["truck_id"] == state.truck_id
        assert cs["state"] == state.state
        assert cs["last_loaded_product"] == "DIESEL_2"
        assert cs["last_loaded_at"] is not None

    def test_gasoline_reg_to_prem_allowed(self):
        state = _compartment_state(last_loaded_product="GASOLINE_REG")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "GASOLINE_PREM"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["governing_rule"] == "allowed"

    def test_empty_compartment_allows_any_product(self):
        state = _compartment_state(
            last_loaded_product=None,
            last_loaded_at=None,
            state="clean",
        )
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "PROPANE"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["governing_rule"] == "allowed"
        assert body["previous_product"] is None


class TestLoadEligibilityBlocked:
    def test_heating_oil_to_gasoline_reg_blocked(self):
        # Requirements 7.2.1: HEATING_OIL → GASOLINE_* is blocked.
        state = _compartment_state(last_loaded_product="HEATING_OIL")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "GASOLINE_REG"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "blocked"
        assert body["reason"] == "cross_contamination_blocked"
        assert body["governing_rule"] == "blocked"
        assert body["previous_product"] == "HEATING_OIL"
        assert body["proposed_product"] == "GASOLINE_REG"

    def test_def_to_diesel_blocked(self):
        # DEF with any non-DEF is strictly blocked (Req 7.2.1).
        state = _compartment_state(last_loaded_product="DEF")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "blocked"
        assert body["reason"] == "cross_contamination_blocked"
        assert body["governing_rule"] == "blocked"


class TestLoadEligibilityRequiresCleaning:
    def test_gasoline_to_diesel_requires_cleaning_without_fresh_clean(self):
        state = _compartment_state(
            last_loaded_product="GASOLINE_REG",
            last_loaded_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            last_cleaned_at=None,
        )
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "requires_cleaning"
        assert body["reason"] == "cleaning_required"
        assert body["governing_rule"] == "requires_cleaning"

    def test_gasoline_to_diesel_allowed_after_fresh_cleaning(self):
        # Req 7.2.4: ``requires_cleaning`` downgrades to ``allowed`` when
        # last_cleaned_at > last_loaded_at. The governing rule stays
        # ``requires_cleaning`` so the caller sees *why* the decision
        # was downgraded.
        loaded = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state = _compartment_state(
            last_loaded_product="GASOLINE_REG",
            last_loaded_at=loaded,
            last_cleaned_at=loaded + timedelta(hours=2),
        )
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["reason"] is None
        # Governing rule preserved so callers see the matrix cell
        # that drove the downgrade decision (Req 7.2.5 — surface the
        # *governing* rule, not just the decision).
        assert body["governing_rule"] == "requires_cleaning"

    def test_cleaning_older_than_load_requires_cleaning(self):
        loaded = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state = _compartment_state(
            last_loaded_product="GASOLINE_REG",
            last_loaded_at=loaded,
            last_cleaned_at=loaded - timedelta(hours=2),
        )
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "requires_cleaning"
        assert body["reason"] == "cleaning_required"
        assert body["governing_rule"] == "requires_cleaning"


# ---------------------------------------------------------------------------
# Legacy NG alias canonicalization (Req 6.1.4)
# ---------------------------------------------------------------------------


class TestLegacyAliasCanonicalization:
    def test_query_param_alias_is_canonicalized_in_response(self):
        state = _compartment_state(last_loaded_product="DIESEL_2")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        # AGO is the legacy NG alias for DIESEL_2. The endpoint should
        # canonicalize it and return DIESEL_2 in ``proposed_product``.
        resp = client.get(
            _url(state.compartment_id), params={"product_code": "AGO"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["proposed_product"] == "DIESEL_2"

    def test_previous_product_is_returned_in_canonical_form(self):
        # LPG → PROPANE canonicalization on persistence means the state
        # already carries PROPANE; the response echoes that canonical
        # form even when the proposed product uses the legacy alias.
        state = _compartment_state(last_loaded_product="LPG")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "PROPANE"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["previous_product"] == "PROPANE"


# ---------------------------------------------------------------------------
# Error modes
# ---------------------------------------------------------------------------


class TestErrorModes:
    def test_missing_compartment_returns_404(self):
        app, _ = _build_app(seed_states=[])
        client = TestClient(app)

        resp = client.get(
            _url("does-not-exist"), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error_code"] == "compartment_not_found"
        assert detail["compartment_id"] == "does-not-exist"

    def test_cross_tenant_compartment_returns_404_not_403(self):
        # Existence is never leaked across tenants.
        state = _compartment_state(tenant_id="other-tenant")
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert detail["error_code"] == "compartment_not_found"

    def test_unknown_product_code_returns_422(self):
        state = _compartment_state()
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id),
            params={"product_code": "NOT_A_REAL_FUEL"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error_code"] == "unknown_product_code"
        assert detail["product_code"] == "NOT_A_REAL_FUEL"

    def test_missing_product_code_query_returns_422(self):
        # FastAPI returns 422 for missing required query params.
        state = _compartment_state()
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(_url(state.compartment_id))
        assert resp.status_code == 422

    def test_blank_product_code_returns_422(self):
        # ``min_length=1`` catches empty string at validation time.
        state = _compartment_state()
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": ""}
        )
        assert resp.status_code == 422

    def test_whitespace_only_product_code_returns_400(self):
        # A whitespace-only value slips past ``min_length`` but is
        # caught by the explicit blank-check in the handler.
        state = _compartment_state()
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "   "}
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["error_code"] == "invalid_product_code"


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


class TestTenantScoping:
    def test_state_repository_lookup_uses_jwt_tenant(self):
        state = _compartment_state(tenant_id="tenant-a")
        app, state_repo = _build_app(
            tenant_id="tenant-a", seed_states=[state]
        )
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200
        # The repository was queried with the JWT-derived tenant — never
        # a tenant supplied by the caller via header / query / body.
        assert state_repo.get_calls[0] == ("tenant-a", state.compartment_id)


# ---------------------------------------------------------------------------
# Tenant override integration
# ---------------------------------------------------------------------------


class TestTenantOverrides:
    def test_tenant_override_can_block_a_defaultly_allowed_pair(self):
        # By default KEROSENE → GASOLINE_REG is allowed; tenant override
        # flips it to blocked.
        state = _compartment_state(last_loaded_product="KEROSENE")
        override_payload = '{"KEROSENE->GASOLINE_REG": "blocked"}'
        tc = _FakeTenantConfig(
            {"compatibility_matrix_config:tenant-1": override_payload}
        )
        app, _ = _build_app(seed_states=[state], tenant_config=tc)
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "GASOLINE_REG"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "blocked"
        assert body["reason"] == "cross_contamination_blocked"
        assert body["governing_rule"] == "blocked"
        # The endpoint consulted the tenant-specific key.
        assert tc.calls == ["compatibility_matrix_config:tenant-1"]

    def test_tenant_override_can_relax_a_requires_cleaning_rule(self):
        # Default GASOLINE_REG → DIESEL_2 = requires_cleaning; override
        # relaxes to allowed.
        state = _compartment_state(last_loaded_product="GASOLINE_REG")
        override_payload = '{"GASOLINE_REG->DIESEL_2": "allowed"}'
        tc = _FakeTenantConfig(
            {"compatibility_matrix_config:tenant-1": override_payload}
        )
        app, _ = _build_app(seed_states=[state], tenant_config=tc)
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "DIESEL_2"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "allowed"
        assert body["governing_rule"] == "allowed"

    def test_tenant_config_outage_degrades_to_default_rules(self):
        # A Redis outage must not block the eligibility check — the
        # endpoint falls back to the default rule table.
        state = _compartment_state(last_loaded_product="HEATING_OIL")
        app, _ = _build_app(
            seed_states=[state], tenant_config=_RaisingTenantConfig()
        )
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "GASOLINE_REG"}
        )
        assert resp.status_code == 200
        body = resp.json()
        # Default HEATING_OIL → GASOLINE_REG = blocked surfaces even
        # when the override fetch raises.
        assert body["decision"] == "blocked"
        assert body["governing_rule"] == "blocked"

    def test_missing_tenant_config_uses_default_rules(self):
        # No tenant_config wired at all: endpoint uses seed rules.
        state = _compartment_state(last_loaded_product="HEATING_OIL")
        app, _ = _build_app(seed_states=[state], tenant_config=None)
        client = TestClient(app)

        resp = client.get(
            _url(state.compartment_id), params={"product_code": "GASOLINE_REG"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "blocked"
        assert body["governing_rule"] == "blocked"


# ---------------------------------------------------------------------------
# Property-based tests — Req 7.2.7 (determinism)
# ---------------------------------------------------------------------------


class TestLoadEligibilityProperties:
    """Property-based tests verifying the determinism property mandated
    by Requirement 7.2.7: two consecutive calls with identical inputs
    return the same decision.

    Validates: Requirement 7.2.7.
    """

    @settings(max_examples=25, deadline=None)
    @given(
        previous=st.sampled_from(
            [
                "DIESEL_2",
                "HEATING_OIL",
                "GASOLINE_REG",
                "GASOLINE_PREM",
                "OFF_ROAD_DIESEL",
                "KEROSENE",
                "PROPANE",
                "DEF",
            ]
        ),
        proposed=st.sampled_from(
            [
                "DIESEL_2",
                "HEATING_OIL",
                "GASOLINE_REG",
                "GASOLINE_PREM",
                "OFF_ROAD_DIESEL",
                "KEROSENE",
                "PROPANE",
                "DEF",
            ]
        ),
        clean_hours_after_load=st.one_of(
            st.none(),
            st.integers(min_value=-48, max_value=48),
        ),
    )
    def test_consecutive_calls_are_deterministic(
        self,
        previous: str,
        proposed: str,
        clean_hours_after_load: Optional[int],
    ) -> None:
        loaded = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        cleaned = (
            None
            if clean_hours_after_load is None
            else loaded + timedelta(hours=clean_hours_after_load)
        )
        state = _compartment_state(
            last_loaded_product=previous,
            last_loaded_at=loaded,
            last_cleaned_at=cleaned,
        )
        app, _ = _build_app(seed_states=[state])
        client = TestClient(app)

        first = client.get(
            _url(state.compartment_id), params={"product_code": proposed}
        )
        second = client.get(
            _url(state.compartment_id), params={"product_code": proposed}
        )
        assert first.status_code == 200
        assert second.status_code == 200
        # Compare the decision-bearing keys; the compartment_state
        # block is also deterministic because the repository is keyed
        # and the state is not mutated by the endpoint.
        first_body = first.json()
        second_body = second.json()
        for key in ("decision", "reason", "governing_rule", "previous_product", "proposed_product"):
            assert first_body[key] == second_body[key], (
                f"determinism violated for key {key!r} "
                f"(previous={previous}, proposed={proposed}, "
                f"clean_hours_after_load={clean_hours_after_load})"
            )
