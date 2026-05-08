"""
Unit tests for Task 10.4 of the fuel-ops-hardening spec:

* ``GET /api/fuel/storm-mode/status`` — return the current Storm_Mode
  state for the tenant, any active manual override, the triggering
  :class:`WeatherAlert`\\ s, and the activation window (Req 9.1.6,
  9.4.3).

The tests exercise the full router wiring (
:func:`configure_fuel_ops_endpoints` → :class:`StormModeEvaluator` →
``weather_alerts`` / ``storm_mode_overrides`` ES indices) with an
in-memory ES stub plus an in-memory Redis stub so transitions the
evaluator persisted on a prior tick are visible to the endpoint.

Validates: Requirements 9.1.6, 9.4.3.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuel.api.fuel_ops_endpoints import (
    configure_fuel_ops_endpoints,
    router,
)
from fuel.services.fuel_ops_es_mappings import (
    STORM_MODE_OVERRIDES_INDEX,
    WEATHER_ALERTS_INDEX,
)
from fuel.services.storm_mode_evaluator import (
    ACTIVE,
    DEFAULT_ACTIVATION_SEVERITY,
    DEFAULT_ACTIVATION_WINDOW_HOURS,
    INACTIVE,
    PersistedState,
    StormModeEvaluator,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hits(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "hits": {
            "hits": [{"_source": s} for s in sources],
            "total": {"value": len(sources)},
        }
    }


class _FakeES:
    """In-memory ES stub supporting the queries the endpoint issues.

    Stores rows keyed by index name. The only queries the endpoint
    runs are:

    * ``weather_alerts`` hydrate-by-ids — ``bool.filter`` with a
      ``term`` on ``tenant_id`` and a ``terms`` on ``alert_id``.
    * ``storm_mode_overrides`` most-recent — ``bool.filter`` +
      ``should`` + ``sort`` on ``created_at`` desc.

    The stub honours the tenant_id filter and the alert_id terms
    filter so cross-tenant defense-in-depth is actually exercised.
    """

    def __init__(self) -> None:
        self.rows: Dict[str, List[Dict[str, Any]]] = {}

    def seed(self, index: str, rows: List[Dict[str, Any]]) -> None:
        self.rows[index] = list(rows)

    async def search_documents(
        self, index: str, query: Dict[str, Any], size: int
    ) -> Dict[str, Any]:
        bucket = list(self.rows.get(index, []))
        q = query.get("query", {}).get("bool", {}) if isinstance(query, dict) else {}
        filters = q.get("filter") or []
        tenant_id: Optional[str] = None
        alert_ids: Optional[List[str]] = None
        for clause in filters:
            if not isinstance(clause, dict):
                continue
            if "term" in clause and isinstance(clause["term"], dict):
                for field, value in clause["term"].items():
                    if field == "tenant_id":
                        tenant_id = value
            if "terms" in clause and isinstance(clause["terms"], dict):
                for field, values in clause["terms"].items():
                    if field == "alert_id" and isinstance(values, list):
                        alert_ids = [str(v) for v in values]

        matches: List[Dict[str, Any]] = []
        for row in bucket:
            if tenant_id is not None and row.get("tenant_id") != tenant_id:
                continue
            if alert_ids is not None and row.get("alert_id") not in alert_ids:
                continue
            matches.append(row)

        sort = query.get("sort") or []
        if sort and matches:
            def _key(row: Dict[str, Any]) -> str:
                for spec in sort:
                    if not isinstance(spec, dict):
                        continue
                    for field in spec.keys():
                        return str(row.get(field) or "")
                return ""

            matches.sort(key=_key, reverse=True)

        return _hits(matches[: max(size, len(matches))])


class _FakeRedis:
    """Tiny async Redis stub backing the evaluator's persisted state."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def get(self, key: str) -> Optional[bytes]:
        raw = self.store.get(key)
        if raw is None:
            return None
        return raw.encode("utf-8")

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _tenant_ctx_factory(tenant_id: str = "tenant-A"):
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


def _alert_row(
    *,
    alert_id: str,
    tenant_id: str = "tenant-A",
    severity: str = "severe",
    alert_type: str = "winter_storm_warning",
    start_offset_hours: float = 1.0,
    end_offset_hours: Optional[float] = 10.0,
    status: str = "forecast",
    affected_zip_codes: Optional[List[str]] = None,
    source: str = "nws",
    headline: str = "Winter Storm Warning",
) -> Dict[str, Any]:
    start = _now() + timedelta(hours=start_offset_hours)
    payload: Dict[str, Any] = {
        "alert_id": alert_id,
        "tenant_id": tenant_id,
        "region_code": "NY",
        "alert_type": alert_type,
        "severity": severity,
        "headline": headline,
        "expected_start_at": start.isoformat(),
        "affected_zip_codes": affected_zip_codes or ["14202"],
        "source": source,
        "ingested_at": _now().isoformat(),
        "activation_status": status,
    }
    if end_offset_hours is not None:
        payload["expected_end_at"] = (
            _now() + timedelta(hours=end_offset_hours)
        ).isoformat()
    return payload


def _override_row(
    *,
    override_id: str,
    tenant_id: str = "tenant-A",
    action: str = "activate",
    reason: str = "manual call",
    actor_id: str = "ops-1",
    expires_offset_hours: Optional[float] = 4.0,
    created_offset_hours: float = -0.1,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "override_id": override_id,
        "tenant_id": tenant_id,
        "action": action,
        "reason": reason,
        "actor_id": actor_id,
        "created_at": (
            _now() + timedelta(hours=created_offset_hours)
        ).isoformat(),
        "updated_at": (
            _now() + timedelta(hours=created_offset_hours)
        ).isoformat(),
    }
    if expires_offset_hours is not None:
        payload["expires_at"] = (
            _now() + timedelta(hours=expires_offset_hours)
        ).isoformat()
    return payload


def _persist_state(
    redis: _FakeRedis,
    tenant_id: str,
    state: PersistedState,
) -> None:
    from fuel.services.storm_mode_evaluator import STATE_KEY_PATTERN

    redis.store[STATE_KEY_PATTERN.format(tenant_id=tenant_id)] = state.to_json()


def _build_app(
    *,
    tenant_id: str = "tenant-A",
    wire_evaluator: bool = True,
    alerts: Optional[List[Dict[str, Any]]] = None,
    overrides: Optional[List[Dict[str, Any]]] = None,
    persisted: Optional[PersistedState] = None,
) -> tuple[FastAPI, _FakeES, _FakeRedis]:
    es = _FakeES()
    redis = _FakeRedis()
    if alerts:
        es.seed(WEATHER_ALERTS_INDEX, alerts)
    if overrides:
        es.seed(STORM_MODE_OVERRIDES_INDEX, overrides)
    if persisted is not None:
        _persist_state(redis, tenant_id, persisted)

    evaluator: Optional[StormModeEvaluator] = None
    if wire_evaluator:
        evaluator = StormModeEvaluator(
            es_service=es,
            redis_client=redis,
        )

    configure_fuel_ops_endpoints(
        es_service=es,
        storm_mode_evaluator=evaluator,
    )

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_tenant_context] = _tenant_ctx_factory(
        tenant_id=tenant_id
    )
    return app, es, redis


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInactiveState:
    def test_returns_inactive_when_no_state_persisted(self):
        app, _, _ = _build_app()
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "tenant-A"
        assert body["state"] == INACTIVE
        assert body["computed_state"] == INACTIVE
        assert body["override_active"] is False
        assert body["override"] is None
        assert body["triggering_alerts"] == []
        window = body["activation_window"]
        assert window["lookahead_hours"] == DEFAULT_ACTIVATION_WINDOW_HOURS
        assert window["severity_threshold"] == DEFAULT_ACTIVATION_SEVERITY
        assert window["activated_at"] is None
        assert window["clears_at"] is None
        assert body["updated_at"] is None


class TestActiveState:
    def test_returns_active_with_hydrated_alert(self):
        alert = _alert_row(alert_id="alert-001")
        now = _now()
        end = now + timedelta(hours=10)
        persisted = PersistedState(
            state=ACTIVE,
            updated_at=now,
            triggering_alert_ids=["alert-001"],
            expected_end_at=end,
        )
        app, _, _ = _build_app(
            alerts=[alert],
            persisted=persisted,
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == ACTIVE
        assert body["computed_state"] == ACTIVE
        assert body["override_active"] is False
        assert len(body["triggering_alerts"]) == 1
        hit = body["triggering_alerts"][0]
        assert hit["alert_id"] == "alert-001"
        assert hit["alert_type"] == "winter_storm_warning"
        assert hit["severity"] == "severe"
        assert hit["affected_zip_codes"] == ["14202"]
        window = body["activation_window"]
        assert window["activated_at"] is not None
        assert window["clears_at"] is not None
        assert body["updated_at"] is not None

    def test_alerts_preserve_persisted_order(self):
        # Seed two alerts; persisted order is [alert-002, alert-001].
        alerts = [
            _alert_row(alert_id="alert-001", start_offset_hours=2.0),
            _alert_row(alert_id="alert-002", start_offset_hours=1.0),
        ]
        persisted = PersistedState(
            state=ACTIVE,
            updated_at=_now(),
            triggering_alert_ids=["alert-002", "alert-001"],
            expected_end_at=None,
        )
        app, _, _ = _build_app(alerts=alerts, persisted=persisted)
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        ids = [a["alert_id"] for a in resp.json()["triggering_alerts"]]
        assert ids == ["alert-002", "alert-001"]


class TestOverrides:
    def test_activate_override_forces_active_without_alerts(self):
        override = _override_row(override_id="ov-1", action="activate")
        app, _, _ = _build_app(overrides=[override])
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == ACTIVE
        assert body["computed_state"] == INACTIVE
        assert body["override_active"] is True
        assert body["override"]["override_id"] == "ov-1"
        assert body["override"]["action"] == "activate"
        assert body["override"]["reason"] == "manual call"
        assert body["override"]["actor_id"] == "ops-1"

    def test_deactivate_override_forces_inactive_despite_alerts(self):
        alert = _alert_row(alert_id="alert-001")
        override = _override_row(override_id="ov-2", action="deactivate")
        persisted = PersistedState(
            state=ACTIVE,
            updated_at=_now(),
            triggering_alert_ids=["alert-001"],
            expected_end_at=_now() + timedelta(hours=5),
        )
        app, _, _ = _build_app(
            alerts=[alert],
            overrides=[override],
            persisted=persisted,
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == INACTIVE
        assert body["computed_state"] == ACTIVE
        assert body["override_active"] is True
        assert body["override"]["action"] == "deactivate"

    def test_snooze_override_forces_inactive(self):
        override = _override_row(override_id="ov-3", action="snooze")
        persisted = PersistedState(
            state=ACTIVE,
            updated_at=_now(),
            triggering_alert_ids=[],
            expected_end_at=None,
        )
        app, _, _ = _build_app(overrides=[override], persisted=persisted)
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == INACTIVE
        assert body["override_active"] is True
        assert body["override"]["action"] == "snooze"

    def test_clear_override_falls_back_to_computed(self):
        override = _override_row(override_id="ov-4", action="clear")
        persisted = PersistedState(
            state=ACTIVE,
            updated_at=_now(),
            triggering_alert_ids=[],
            expected_end_at=None,
        )
        app, _, _ = _build_app(overrides=[override], persisted=persisted)
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == ACTIVE
        assert body["override_active"] is False
        assert body["override"] is None

    def test_expired_override_ignored(self):
        override = _override_row(
            override_id="ov-5",
            action="activate",
            expires_offset_hours=-1.0,  # expired one hour ago
        )
        app, _, _ = _build_app(overrides=[override])
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == INACTIVE
        assert body["override_active"] is False


class TestTenantIsolation:
    def test_cross_tenant_alert_never_leaks(self):
        alert = _alert_row(alert_id="alert-100", tenant_id="tenant-B")
        persisted = PersistedState(
            state=ACTIVE,
            updated_at=_now(),
            triggering_alert_ids=["alert-100"],
            expected_end_at=None,
        )
        app, _, _ = _build_app(
            tenant_id="tenant-A",
            alerts=[alert],
            persisted=persisted,
        )
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        # State says active but hydration drops the cross-tenant row.
        assert resp.json()["triggering_alerts"] == []

    def test_cross_tenant_override_never_leaks(self):
        override = _override_row(
            override_id="ov-x", tenant_id="tenant-B", action="activate"
        )
        app, _, _ = _build_app(tenant_id="tenant-A", overrides=[override])
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == INACTIVE
        assert body["override_active"] is False


class TestUnwired:
    def test_returns_503_when_evaluator_missing(self):
        app, _, _ = _build_app(wire_evaluator=False)
        client = TestClient(app)

        resp = client.get("/api/fuel/storm-mode/status")
        assert resp.status_code == 503
        assert (
            resp.json()["detail"]["error_code"]
            == "storm_mode_evaluator_unavailable"
        )
