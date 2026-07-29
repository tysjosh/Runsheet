"""
Unit tests for driver exception reporting endpoints.

Tests exception storage, RiskSignal conversion, SignalBus publishing,
job timeline event appending, severity-based escalation broadcasting,
and ExceptionType enum validation.

Validates: Requirements 7.1, 7.2, 7.3, 7.4
"""

import sys
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.auth_seam import auth_headers, install_test_auth

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any scheduling imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.exception_endpoints import (
    router as exception_router,
    configure_exception_endpoints,
)

# ``_build_risk_signal`` moved into the extracted ExceptionReportService when
# the exception rule left the handler (task 5.5). The shim keeps the existing
# RiskSignal tests expressed in terms of a job id.
from driver.services.exception_service import ExceptionReportService
from driver.services.work_ref import WorkRef
from driver.models import ExceptionRequest, ExceptionType, GeoPoint
from Agents.overlay.data_contracts import RiskSignal, Severity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = "t1"

# Endpoints authenticate via the Test_Auth_Path dependency-override seam
# (installed per-app in ``_make_app``); no legacy-JWT settings patch needed.
_SETTINGS_PATCH = nullcontext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers(tenant_id: str = TENANT_ID) -> dict:
    """A driver-scoped Test_Auth_Path header set.

    ``/api/driver`` resolution runs ``require_driver_identity``, so the context
    must hold the exact ``driver`` role and a canonical ``driver_id`` (Req 1.5,
    1.6).
    """
    return auth_headers(
        tenant_id, sub="driver-1", roles=["driver"], driver_id="driver-1"
    )


def _make_es_service() -> MagicMock:
    """Create a mock ElasticsearchService."""
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})
    return es


def _make_job_service(asset_assigned: str = "driver-1") -> MagicMock:
    """Create a mock JobService with _append_event and _get_job_doc."""
    svc = MagicMock()
    svc._append_event = AsyncMock(return_value="evt-123")
    svc._get_job_doc = AsyncMock(
        return_value={
            "job_id": "JOB_1",
            "status": "assigned",
            "tenant_id": TENANT_ID,
            "asset_assigned": asset_assigned,
        }
    )
    return svc


def _build_risk_signal(
    exception_id: str, job_id: str, body: ExceptionRequest, tenant_id: str
) -> RiskSignal:
    """Build a RiskSignal through the extracted service (was a module helper)."""
    service = ExceptionReportService(es_service=_make_es_service())
    ref = WorkRef(
        tenant_id=tenant_id,
        driver_id="driver-1",
        kind="job",
        work_id=job_id,
    )
    return service.build_risk_signal(ref, body, exception_id)


def _make_signal_bus() -> MagicMock:
    """Create a mock SignalBus."""
    bus = MagicMock()
    bus.publish = AsyncMock(return_value=1)
    return bus


def _make_order_repository(
    tenant_id: str = TENANT_ID,
    assigned_driver_id: str = "driver-1",
    order_id: str = "ORD_1",
) -> MagicMock:
    """A fake order repository returning one assigned order document.

    ``WorkRefResolver.resolve_order`` accepts either a ``FuelOrder`` or a raw
    document, so a plain dict is enough here (Req 7.21).
    """
    repo = MagicMock()
    repo.get = AsyncMock(
        return_value={
            "order_id": order_id,
            "tenant_id": tenant_id,
            "assigned_driver_id": assigned_driver_id,
            "status": "in_transit",
        }
    )
    return repo


def _make_app(
    es_service=None,
    job_service=None,
    signal_bus=None,
    scheduling_ws=None,
    driver_ws=None,
    order_repository=None,
) -> FastAPI:
    """Create a test FastAPI app with the exception router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(exception_router)

    configure_exception_endpoints(
        es_service=es_service or _make_es_service(),
        job_service=job_service,
        order_repository=order_repository,
        signal_bus=signal_bus,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    install_test_auth(app)
    return app


def _exception_payload(
    exception_type: str = "road_closure",
    severity: str = "medium",
    note: str = "Road blocked due to construction",
    location: dict = None,
    media_refs: list = None,
) -> dict:
    """Build a valid exception request payload."""
    payload = {
        "exception_type": exception_type,
        "severity": severity,
        "note": note,
    }
    if location is not None:
        payload["location"] = location
    if media_refs is not None:
        payload["media_refs"] = media_refs
    return payload


# ---------------------------------------------------------------------------
# Test: _build_risk_signal (pure function)
# ---------------------------------------------------------------------------


class TestBuildRiskSignal:
    """Tests for the _build_risk_signal helper function."""

    def test_maps_exception_type_to_entity_type(self):
        """RiskSignal entity_type matches exception_type value. Validates: Req 7.2"""
        body = ExceptionRequest(
            exception_type=ExceptionType.VEHICLE_BREAKDOWN,
            severity=Severity.HIGH,
            note="Engine failure",
        )
        signal = _build_risk_signal("exc-1", "JOB_1", body, "t1")
        assert signal.entity_type == "vehicle_breakdown"

    def test_maps_severity_correctly(self):
        """RiskSignal severity matches exception severity. Validates: Req 7.2"""
        body = ExceptionRequest(
            exception_type=ExceptionType.ROAD_CLOSURE,
            severity=Severity.CRITICAL,
            note="Major road closure",
        )
        signal = _build_risk_signal("exc-2", "JOB_2", body, "t1")
        assert signal.severity == Severity.CRITICAL

    def test_sets_entity_id_to_job_id(self):
        """RiskSignal entity_id is the job_id. Validates: Req 7.2"""
        body = ExceptionRequest(
            exception_type=ExceptionType.WEATHER,
            severity=Severity.MEDIUM,
            note="Heavy rain",
        )
        signal = _build_risk_signal("exc-3", "JOB_42", body, "t1")
        assert signal.entity_id == "JOB_42"

    def test_includes_exception_context(self):
        """RiskSignal context includes exception details. Validates: Req 7.2"""
        body = ExceptionRequest(
            exception_type=ExceptionType.CARGO_DAMAGE,
            severity=Severity.HIGH,
            note="Container damaged",
            location=GeoPoint(lat=-33.8688, lng=151.2093),
            media_refs=["photo1.jpg", "photo2.jpg"],
        )
        signal = _build_risk_signal("exc-4", "JOB_5", body, "t1")
        assert signal.context["exception_id"] == "exc-4"
        assert signal.context["note"] == "Container damaged"
        assert signal.context["location"] == {"lat": -33.8688, "lng": 151.2093}
        assert signal.context["media_refs"] == ["photo1.jpg", "photo2.jpg"]

    def test_returns_valid_risk_signal(self):
        """Returned object is a valid RiskSignal instance. Validates: Req 7.2"""
        body = ExceptionRequest(
            exception_type=ExceptionType.OTHER,
            severity=Severity.LOW,
            note="Minor issue",
        )
        signal = _build_risk_signal("exc-5", "JOB_6", body, "t1")
        assert isinstance(signal, RiskSignal)
        assert signal.source_agent == "driver_exception_reporter"
        assert signal.tenant_id == "t1"
        assert signal.confidence == 0.9
        assert signal.ttl_seconds == 3600


# ---------------------------------------------------------------------------
# Test: report_exception endpoint — storage and timeline
# ---------------------------------------------------------------------------


class TestReportException:
    """Tests for the POST /jobs/{job_id}/exceptions endpoint."""

    def test_stores_exception_in_es(self):
        """Exception is stored in driver_exceptions index. Validates: Req 7.1"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args.args[0] == "driver_exceptions"
        doc = call_args.args[2]
        assert doc["job_id"] == "JOB_1"
        assert doc["exception_type"] == "road_closure"
        assert doc["severity"] == "medium"
        assert doc["tenant_id"] == TENANT_ID

    def test_appends_exception_reported_event(self):
        """Appends exception_reported event to job timeline. Validates: Req 7.1"""
        job_svc = _make_job_service()
        app = _make_app(es_service=_make_es_service(), job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        job_svc._append_event.assert_called_once()
        call_kwargs = job_svc._append_event.call_args.kwargs
        assert call_kwargs["event_type"] == "exception_reported"
        assert call_kwargs["job_id"] == "JOB_1"
        assert call_kwargs["tenant_id"] == TENANT_ID
        assert call_kwargs["payload"]["exception_type"] == "road_closure"

    def test_returns_exception_data(self):
        """Response contains the stored exception document. Validates: Req 7.1"""
        app = _make_app(
            es_service=_make_es_service(), job_service=_make_job_service()
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(
                    exception_type="customer_unavailable",
                    severity="low",
                    note="Customer not home",
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["job_id"] == "JOB_1"
        assert data["exception_type"] == "customer_unavailable"
        assert data["severity"] == "low"
        assert data["note"] == "Customer not home"
        assert "exception_id" in data
        assert "timestamp" in data

    def test_stores_location_when_provided(self):
        """Location is stored when provided. Validates: Req 7.1"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(
                    location={"lat": -33.8688, "lng": 151.2093}
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["location"] == {"lat": -33.8688, "lng": 151.2093}

    def test_stores_media_refs_when_provided(self):
        """Media refs are stored when provided. Validates: Req 7.1"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(media_refs=["photo1.jpg"]),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["media_refs"] == ["photo1.jpg"]


# ---------------------------------------------------------------------------
# Test: RiskSignal publishing
# ---------------------------------------------------------------------------


class TestRiskSignalPublishing:
    """Tests for RiskSignal conversion and SignalBus publishing."""

    def test_publishes_risk_signal_to_signal_bus(self):
        """Exception is converted to RiskSignal and published. Validates: Req 7.2"""
        signal_bus = _make_signal_bus()
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            signal_bus=signal_bus,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(
                    exception_type="vehicle_breakdown",
                    severity="high",
                ),
                headers=_auth_headers(),
            )

        signal_bus.publish.assert_called_once()
        published_signal = signal_bus.publish.call_args.args[0]
        assert isinstance(published_signal, RiskSignal)
        assert published_signal.entity_id == "JOB_1"
        assert published_signal.entity_type == "vehicle_breakdown"
        assert published_signal.severity == Severity.HIGH

    def test_signal_bus_failure_does_not_break_endpoint(self):
        """SignalBus failure does not propagate to the response. Validates: Req 7.2"""
        signal_bus = _make_signal_bus()
        signal_bus.publish = AsyncMock(side_effect=Exception("Bus down"))
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            signal_bus=signal_bus,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200

    def test_no_signal_bus_configured_still_succeeds(self):
        """Endpoint works without a SignalBus configured. Validates: Req 7.2"""
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            signal_bus=None,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: ExceptionType enum validation
# ---------------------------------------------------------------------------


class TestExceptionTypeValidation:
    """Tests for ExceptionType enum validation via Pydantic."""

    def test_valid_exception_types_accepted(self):
        """All valid exception types are accepted. Validates: Req 7.3"""
        valid_types = [
            "road_closure",
            "vehicle_breakdown",
            "customer_unavailable",
            "access_denied",
            "weather",
            "cargo_damage",
            "other",
        ]
        app = _make_app(
            es_service=_make_es_service(), job_service=_make_job_service()
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            for exc_type in valid_types:
                resp = client.post(
                    "/api/driver/jobs/JOB_1/exceptions",
                    json=_exception_payload(exception_type=exc_type),
                    headers=_auth_headers(),
                )
                assert resp.status_code == 200, (
                    f"Expected 200 for exception_type={exc_type}, "
                    f"got {resp.status_code}"
                )

    def test_invalid_exception_type_returns_422(self):
        """Invalid exception_type returns 422 (Pydantic validation). Validates: Req 7.3"""
        app = _make_app(
            es_service=_make_es_service(), job_service=_make_job_service()
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(exception_type="alien_invasion"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 422

    def test_missing_exception_type_returns_422(self):
        """Missing exception_type returns 422. Validates: Req 7.3"""
        app = _make_app(
            es_service=_make_es_service(), job_service=_make_job_service()
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json={"severity": "low", "note": "test"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test: Severity-based escalation broadcast
# ---------------------------------------------------------------------------


class TestEscalationBroadcast:
    """Tests for severity-based escalation broadcasting."""

    def test_high_severity_broadcasts_escalation(self):
        """High severity triggers escalation broadcast. Validates: Req 7.4"""
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            scheduling_ws=ws_manager,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(severity="high"),
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "exception_escalation"
        assert call_args.args[1]["severity"] == "high"
        assert call_args.args[1]["job_id"] == "JOB_1"

    def test_critical_severity_broadcasts_escalation(self):
        """Critical severity triggers escalation broadcast. Validates: Req 7.4"""
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            scheduling_ws=ws_manager,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(severity="critical"),
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "exception_escalation"
        assert call_args.args[1]["severity"] == "critical"

    def test_low_severity_does_not_broadcast_escalation(self):
        """Low severity does NOT trigger escalation broadcast. Validates: Req 7.4"""
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            scheduling_ws=ws_manager,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(severity="low"),
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_not_called()

    def test_medium_severity_does_not_broadcast_escalation(self):
        """Medium severity does NOT trigger escalation broadcast. Validates: Req 7.4"""
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()

        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            scheduling_ws=ws_manager,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(severity="medium"),
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_not_called()

    def test_ws_broadcast_failure_does_not_break_endpoint(self):
        """WS broadcast failure does not propagate. Validates: Req 7.4"""
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock(side_effect=Exception("WS down"))

        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            scheduling_ws=ws_manager,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/exceptions",
                json=_exception_payload(severity="critical"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: the order-keyed sibling
# ---------------------------------------------------------------------------


class TestReportExceptionForOrder:
    """Tests for POST /api/driver/orders/{order_id}/exceptions.

    Validates: Requirements 7.13, 7.16, 7.19, 7.21
    """

    def test_stores_exception_keyed_on_order(self):
        """The sibling route exists and persists with order_id. Validates: Req 7.13"""
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            order_repository=_make_order_repository(),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["order_id"] == "ORD_1"
        assert doc["job_id"] is None
        assert doc["driver_id"] == "driver-1"
        assert doc["tenant_id"] == TENANT_ID

    def test_no_job_timeline_event_on_order_path(self):
        """The order-keyed path has no job timeline to append to. Validates: Req 7.16"""
        job_svc = _make_job_service()
        app = _make_app(
            es_service=_make_es_service(),
            job_service=job_svc,
            order_repository=_make_order_repository(),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        job_svc._append_event.assert_not_called()

    def test_publishes_risk_signal_keyed_on_order(self):
        """Same service, so the RiskSignal is published here too. Validates: Req 7.16"""
        signal_bus = _make_signal_bus()
        app = _make_app(
            es_service=_make_es_service(),
            signal_bus=signal_bus,
            order_repository=_make_order_repository(),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(
                    exception_type="vehicle_breakdown", severity="high"
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        published = signal_bus.publish.call_args.args[0]
        assert published.entity_id == "ORD_1"
        assert published.entity_type == "vehicle_breakdown"
        assert published.severity == Severity.HIGH

    def test_high_severity_broadcasts_escalation(self):
        """Escalation fires from the order path unchanged. Validates: Req 7.16"""
        ws_manager = MagicMock()
        ws_manager.broadcast = AsyncMock()
        app = _make_app(
            es_service=_make_es_service(),
            scheduling_ws=ws_manager,
            order_repository=_make_order_repository(),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(severity="critical"),
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        assert ws_manager.broadcast.call_args.args[0] == "exception_escalation"
        assert ws_manager.broadcast.call_args.args[1]["order_id"] == "ORD_1"

    def test_unknown_order_returns_404(self):
        """A missing order is 404 RESOURCE_NOT_FOUND. Validates: Req 7.21"""
        repo = _make_order_repository()
        repo.get = AsyncMock(return_value=None)
        app = _make_app(es_service=_make_es_service(), order_repository=repo)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_MISSING/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    def test_cross_tenant_order_returns_404(self):
        """An order in another tenant is indistinguishable from absent. Validates: Req 7.21"""
        app = _make_app(
            es_service=_make_es_service(),
            order_repository=_make_order_repository(tenant_id="other-tenant"),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 404
        assert resp.json()["error_code"] == "RESOURCE_NOT_FOUND"

    def test_order_assigned_to_another_driver_returns_403(self):
        """No dual acceptance: a mismatch is 403 FORBIDDEN. Validates: Req 7.21"""
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            order_repository=_make_order_repository(
                assigned_driver_id="driver-2"
            ),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "FORBIDDEN"
        es.index_document.assert_not_called()

    def test_missing_driver_identity_returns_403(self):
        """A caller without a driver_id claim is rejected. Validates: Req 7.21"""
        app = _make_app(
            es_service=_make_es_service(),
            order_repository=_make_order_repository(),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(),
                headers=auth_headers(
                    TENANT_ID, sub="driver-1", roles=["driver"]
                ),
            )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"

    def test_invalid_exception_type_returns_422(self):
        """Identical validation to the job-keyed sibling. Validates: Req 7.19"""
        app = _make_app(
            es_service=_make_es_service(),
            order_repository=_make_order_repository(),
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/orders/ORD_1/exceptions",
                json=_exception_payload(exception_type="alien_invasion"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 422
