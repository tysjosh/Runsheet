"""
Unit tests for driver proof of delivery (POD) submission endpoints.

Tests POD storage, geotag distance validation, OTP validation,
job timeline event appending, and WebSocket broadcasting.

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

# ---------------------------------------------------------------------------
# Patch ElasticsearchService singleton BEFORE any scheduling imports
# ---------------------------------------------------------------------------
_mock_es_module = MagicMock()
_mock_es_module.ElasticsearchService = MagicMock
_mock_es_module.elasticsearch_service = MagicMock()
sys.modules.setdefault("services.elasticsearch_service", _mock_es_module)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from driver.api.pod_endpoints import (
    router as pod_router,
    configure_pod_endpoints,
    _validate_geotag,
)
from driver.services.geo_utils import haversine_distance_meters

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JWT_SECRET = "test-jwt-secret"
JWT_ALGORITHM = "HS256"
TENANT_ID = "t1"

_SETTINGS_PATCH = patch(
    "ops.middleware.tenant_guard.get_settings",
    return_value=MagicMock(jwt_secret=JWT_SECRET, jwt_algorithm=JWT_ALGORITHM),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(tenant_id: str = TENANT_ID, sub: str = "driver-1") -> str:
    return jwt.encode(
        {"tenant_id": tenant_id, "sub": sub}, JWT_SECRET, algorithm=JWT_ALGORITHM
    )


def _auth_headers(tenant_id: str = TENANT_ID) -> dict:
    return {"Authorization": f"Bearer {_make_token(tenant_id)}"}


def _make_es_service(tenant_policies: dict = None) -> MagicMock:
    """Create a mock ElasticsearchService.

    If tenant_policies is provided, search_documents will return it
    when querying tenant_job_policies.
    """
    es = MagicMock()
    es.index_document = AsyncMock(return_value={"result": "created"})

    if tenant_policies is not None:
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": tenant_policies}],
                    "total": {"value": 1},
                }
            }
        )
    else:
        # No tenant policies found — defaults apply
        es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )

    return es


def _make_job_service(destination_location: dict = None) -> MagicMock:
    """Create a mock JobService with _append_event and _get_job_doc."""
    svc = MagicMock()
    svc._append_event = AsyncMock(return_value="evt-123")

    job_doc = {
        "job_id": "JOB_1",
        "status": "in_progress",
        "tenant_id": TENANT_ID,
    }
    if destination_location:
        job_doc["destination_location"] = destination_location
    svc._get_job_doc = AsyncMock(return_value=job_doc)

    return svc


def _make_app(
    es_service=None,
    job_service=None,
    scheduling_ws=None,
    driver_ws=None,
) -> FastAPI:
    """Create a test FastAPI app with the POD router."""
    from errors.handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(pod_router)

    configure_pod_endpoints(
        es_service=es_service or _make_es_service(),
        job_service=job_service,
        scheduling_ws_manager=scheduling_ws,
        driver_ws_manager=driver_ws,
    )
    return app


def _pod_payload(
    recipient_name: str = "John Doe",
    signature_url: str = "https://example.com/sig.png",
    photo_urls: list = None,
    geotag: dict = None,
    timestamp: str = "2024-01-15T10:30:00Z",
    otp: str = None,
) -> dict:
    """Build a valid POD request payload."""
    payload = {
        "recipient_name": recipient_name,
        "signature_url": signature_url,
        "photo_urls": photo_urls or ["https://example.com/photo1.jpg"],
        "geotag": geotag or {"lat": -33.8688, "lng": 151.2093},
        "timestamp": timestamp,
    }
    if otp is not None:
        payload["otp"] = otp
    return payload


# ---------------------------------------------------------------------------
# Test: _validate_geotag (pure function)
# ---------------------------------------------------------------------------


class TestValidateGeotag:
    """Tests for the _validate_geotag helper function."""

    def test_within_radius_returns_true(self):
        """Geotag within radius returns True (no mismatch). Validates: Req 8.3"""
        # Same point — distance is 0
        assert _validate_geotag(0.0, 0.0, 0.0, 0.0, 500) is True

    def test_outside_radius_returns_false(self):
        """Geotag outside radius returns False (mismatch). Validates: Req 8.3"""
        # Sydney to Melbourne is ~714 km — well outside 500m
        assert _validate_geotag(-33.8688, 151.2093, -37.8136, 144.9631, 500) is False

    def test_exactly_at_radius_boundary(self):
        """Geotag at exactly the radius boundary returns True. Validates: Req 8.3"""
        # Use a known distance: ~111 km per degree of latitude
        # 500m ≈ 0.0045 degrees of latitude
        assert _validate_geotag(0.0, 0.0, 0.004, 0.0, 500) is True

    def test_just_outside_radius_boundary(self):
        """Geotag just outside the radius boundary returns False. Validates: Req 8.3"""
        # 0.005 degrees ≈ ~556m — outside 500m
        assert _validate_geotag(0.0, 0.0, 0.005, 0.0, 500) is False

    def test_custom_radius(self):
        """Custom radius is respected. Validates: Req 8.3"""
        # ~1.1 km apart — within 2000m radius
        assert _validate_geotag(0.0, 0.0, 0.01, 0.0, 2000) is True
        # Same distance — outside 500m radius
        assert _validate_geotag(0.0, 0.0, 0.01, 0.0, 500) is False


# ---------------------------------------------------------------------------
# Test: submit_pod endpoint — storage and timeline
# ---------------------------------------------------------------------------


class TestSubmitPod:
    """Tests for the POST /jobs/{job_id}/pod endpoint."""

    def test_stores_pod_in_es(self):
        """POD is stored in proof_of_delivery index. Validates: Req 8.1"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        es.index_document.assert_called_once()
        call_args = es.index_document.call_args
        assert call_args.args[0] == "proof_of_delivery"
        doc = call_args.args[2]
        assert doc["job_id"] == "JOB_1"
        assert doc["recipient_name"] == "John Doe"
        assert doc["signature_url"] == "https://example.com/sig.png"
        assert doc["status"] == "submitted"
        assert doc["tenant_id"] == TENANT_ID

    def test_appends_pod_submitted_event(self):
        """Appends pod_submitted event to job timeline. Validates: Req 8.1"""
        job_svc = _make_job_service()
        app = _make_app(es_service=_make_es_service(), job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        job_svc._append_event.assert_called_once()
        call_kwargs = job_svc._append_event.call_args.kwargs
        assert call_kwargs["event_type"] == "pod_submitted"
        assert call_kwargs["job_id"] == "JOB_1"
        assert call_kwargs["tenant_id"] == TENANT_ID
        assert "pod_id" in call_kwargs["payload"]

    def test_returns_pod_data(self):
        """Response contains the stored POD document. Validates: Req 8.1"""
        app = _make_app(
            es_service=_make_es_service(), job_service=_make_job_service()
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["job_id"] == "JOB_1"
        assert data["recipient_name"] == "John Doe"
        assert data["status"] == "submitted"
        assert "pod_id" in data
        assert "timestamp" in data

    def test_stores_photo_urls(self):
        """Photo URLs are stored correctly. Validates: Req 8.1"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        photos = ["https://example.com/p1.jpg", "https://example.com/p2.jpg"]
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(photo_urls=photos),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["photo_urls"] == photos

    def test_missing_required_fields_returns_422(self):
        """Missing required fields return 422. Validates: Req 8.1"""
        app = _make_app(
            es_service=_make_es_service(), job_service=_make_job_service()
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json={"recipient_name": "John"},
                headers=_auth_headers(),
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test: Geotag distance validation
# ---------------------------------------------------------------------------


class TestGeotagValidation:
    """Tests for geotag distance validation in POD submission."""

    def test_within_radius_no_mismatch(self):
        """POD within radius has location_mismatch=False. Validates: Req 8.3"""
        # Job destination at same location as geotag
        job_svc = _make_job_service(
            destination_location={"lat": -33.8688, "lon": 151.2093}
        )
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(geotag={"lat": -33.8688, "lng": 151.2093}),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["location_mismatch"] is False

    def test_outside_radius_flags_mismatch(self):
        """POD outside radius has location_mismatch=True. Validates: Req 8.3"""
        # Job destination in Sydney, geotag in Melbourne (~714 km away)
        job_svc = _make_job_service(
            destination_location={"lat": -33.8688, "lon": 151.2093}
        )
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(geotag={"lat": -37.8136, "lng": 144.9631}),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["location_mismatch"] is True

    def test_custom_radius_from_tenant_policies(self):
        """Tenant-configured radius is used for validation. Validates: Req 8.3"""
        # Tenant has a 2000m radius — geotag ~1.1 km away should pass
        es = _make_es_service(
            tenant_policies={
                "tenant_id": TENANT_ID,
                "pod_required": True,
                "pod_radius_meters": 2000,
                "otp_required": False,
            }
        )
        job_svc = _make_job_service(
            destination_location={"lat": 0.0, "lon": 0.0}
        )
        app = _make_app(es_service=es, job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(geotag={"lat": 0.01, "lng": 0.0}),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["location_mismatch"] is False

    def test_no_destination_skips_geotag_validation(self):
        """No destination location skips geotag validation. Validates: Req 8.3"""
        job_svc = _make_job_service(destination_location=None)
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["location_mismatch"] is False


# ---------------------------------------------------------------------------
# Test: OTP validation
# ---------------------------------------------------------------------------


class TestOtpValidation:
    """Tests for OTP validation in POD submission."""

    def test_otp_required_but_missing_returns_error(self):
        """OTP required but not provided returns error. Validates: Req 8.5"""
        es = _make_es_service(
            tenant_policies={
                "tenant_id": TENANT_ID,
                "pod_required": True,
                "pod_radius_meters": 500,
                "otp_required": True,
            }
        )
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),  # No OTP
                headers=_auth_headers(),
            )

        assert resp.status_code == 200  # Returns error in body
        body = resp.json()
        assert body.get("error_code") == "OTP_REQUIRED"

    def test_otp_required_and_provided_succeeds(self):
        """OTP required and provided stores POD with otp_verified=True. Validates: Req 8.5"""
        es = _make_es_service(
            tenant_policies={
                "tenant_id": TENANT_ID,
                "pod_required": True,
                "pod_radius_meters": 500,
                "otp_required": True,
            }
        )
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(otp="123456"),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["otp_verified"] is True

    def test_otp_not_required_skips_validation(self):
        """OTP not required skips OTP validation. Validates: Req 8.5"""
        es = _make_es_service()  # No tenant policies — defaults (otp_required=False)
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["otp_verified"] is False


# ---------------------------------------------------------------------------
# Test: WebSocket broadcasting
# ---------------------------------------------------------------------------


class TestPodBroadcast:
    """Tests for POD event WebSocket broadcasting."""

    def test_broadcasts_pod_event_through_scheduling_ws(self):
        """POD event is broadcast through scheduling WS. Validates: Req 8.4"""
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
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        ws_manager.broadcast.assert_called_once()
        call_args = ws_manager.broadcast.call_args
        assert call_args.args[0] == "pod_submitted"
        assert call_args.args[1]["job_id"] == "JOB_1"
        assert call_args.args[1]["status"] == "submitted"

    def test_broadcasts_pod_event_through_driver_ws(self):
        """POD event is broadcast through driver WS. Validates: Req 8.4"""
        driver_ws = MagicMock()
        driver_ws.send_to_driver = AsyncMock()

        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            driver_ws=driver_ws,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        driver_ws.send_to_driver.assert_called_once()

    def test_ws_broadcast_failure_does_not_break_endpoint(self):
        """WS broadcast failure does not propagate. Validates: Req 8.4"""
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
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200

    def test_no_ws_configured_still_succeeds(self):
        """Endpoint works without WS managers configured. Validates: Req 8.4"""
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            scheduling_ws=None,
            driver_ws=None,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test: Job service failure resilience
# ---------------------------------------------------------------------------


class TestJobServiceResilience:
    """Tests for resilience when job service operations fail."""

    def test_append_event_failure_does_not_break_endpoint(self):
        """Job timeline append failure does not propagate. Validates: Req 8.1"""
        job_svc = _make_job_service()
        job_svc._append_event = AsyncMock(side_effect=Exception("ES down"))
        app = _make_app(es_service=_make_es_service(), job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200

    def test_no_job_service_configured_still_succeeds(self):
        """Endpoint works without a JobService configured. Validates: Req 8.1"""
        app = _make_app(
            es_service=_make_es_service(),
            job_service=None,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200

    def test_get_job_doc_failure_skips_geotag_validation(self):
        """Job doc fetch failure skips geotag validation. Validates: Req 8.3"""
        job_svc = _make_job_service()
        job_svc._get_job_doc = AsyncMock(side_effect=Exception("Not found"))
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=job_svc)

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["location_mismatch"] is False
