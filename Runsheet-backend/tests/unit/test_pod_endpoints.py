"""
Unit tests for driver proof of delivery (POD) submission endpoints.

Tests POD storage, geotag distance validation, OTP validation,
job timeline event appending, and WebSocket broadcasting.

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5
"""

import sys
from typing import Optional
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

# File_refs in the tenant-prefixed layout
# ``tenants/{tenant_id}/{category}/{yyyy}/{mm}/{dd}/{uuid}.{ext}``.
_SIGNATURE_REF = (
    f"tenants/{TENANT_ID}/signature/2024/01/15/"
    "11111111-1111-1111-1111-111111111111.png"
)
_PHOTO_REF_1 = (
    f"tenants/{TENANT_ID}/photo/2024/01/15/"
    "22222222-2222-2222-2222-222222222222.jpg"
)
_PHOTO_REF_2 = (
    f"tenants/{TENANT_ID}/photo/2024/01/15/"
    "33333333-3333-3333-3333-333333333333.jpg"
)
_METER_TICKET_REF = (
    f"tenants/{TENANT_ID}/meter_ticket/2024/01/15/"
    "44444444-4444-4444-4444-444444444444.jpg"
)
_CROSS_TENANT_SIGNATURE_REF = (
    "tenants/other-tenant/signature/2024/01/15/"
    "55555555-5555-5555-5555-555555555555.png"
)

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


def _make_file_storage() -> MagicMock:
    """Create a mock FileStorageService that validates tenant-prefixed refs.

    Mirrors the real ``FileStorageService.validate_ref`` contract used by
    the submit_pod handler: refs whose prefix matches
    ``tenants/{tenant_id}/`` return True; mismatches raise PermissionError.
    """

    def _validate_ref(tenant_id: str, file_ref: str, actor=None) -> bool:
        if not file_ref or not file_ref.startswith(f"tenants/{tenant_id}/"):
            raise PermissionError("cross_tenant_file_ref")
        return True

    svc = MagicMock()
    svc.validate_ref = MagicMock(side_effect=_validate_ref)
    return svc


def _make_app(
    es_service=None,
    job_service=None,
    scheduling_ws=None,
    driver_ws=None,
    file_storage=None,
    ocr_service=None,
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
        file_storage_service=file_storage or _make_file_storage(),
        ocr_service=ocr_service,
    )
    return app


def _pod_payload(
    recipient_name: str = "John Doe",
    signature_ref: Optional[str] = _SIGNATURE_REF,
    photo_refs: Optional[list] = None,
    meter_ticket_ref: Optional[str] = None,
    geotag: dict = None,
    timestamp: str = "2024-01-15T10:30:00Z",
    otp: str = None,
    delivered_gallons: Optional[float] = None,
    # Deprecated raw-URL fields kept for backward-compat coverage.
    signature_url: Optional[str] = None,
    photo_urls: Optional[list] = None,
) -> dict:
    """Build a POD request payload exercising the file_ref path by default.

    When ``signature_ref``/``photo_refs`` are ``None`` (and the deprecated
    ``signature_url``/``photo_urls`` are not supplied) the corresponding
    keys are omitted so validators can run against partial payloads.
    """
    payload: dict = {
        "recipient_name": recipient_name,
        "geotag": geotag or {"lat": -33.8688, "lng": 151.2093},
        "timestamp": timestamp,
    }
    if signature_ref is not None:
        payload["signature_ref"] = signature_ref
    if photo_refs is None and signature_ref is not None and signature_url is None:
        payload["photo_refs"] = [_PHOTO_REF_1]
    elif photo_refs is not None:
        payload["photo_refs"] = photo_refs
    if meter_ticket_ref is not None:
        payload["meter_ticket_ref"] = meter_ticket_ref
    if signature_url is not None:
        payload["signature_url"] = signature_url
    if photo_urls is not None:
        payload["photo_urls"] = photo_urls
    if otp is not None:
        payload["otp"] = otp
    if delivered_gallons is not None:
        payload["delivered_gallons"] = delivered_gallons
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
        """POD is stored in proof_of_delivery index. Validates: Requirements 8.1, 4.1.4"""
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
        # file_ref path is now preferred; the legacy signature_url echoes empty.
        assert doc["signature_ref"] == _SIGNATURE_REF
        assert doc["photo_refs"] == [_PHOTO_REF_1]
        assert doc["signature_url"] == ""
        assert doc["photo_urls"] == []
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

    def test_submit_pod_persists_hash_chain_fields(self):
        """Submit POD writes pod_hash + previous_pod_hash atomically. Validates: Req 4.5.2."""
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
        doc = es.index_document.call_args.args[2]
        # First POD for this tenant — previous_pod_hash MUST be the zero-hash.
        assert doc["previous_pod_hash"] == "0" * 64
        # pod_hash is a 64-char lowercase SHA-256 hex digest.
        pod_hash = doc["pod_hash"]
        assert isinstance(pod_hash, str)
        assert len(pod_hash) == 64
        assert all(c in "0123456789abcdef" for c in pod_hash)
        assert pod_hash != doc["previous_pod_hash"]
        assert doc["chain_sequence"] == 1
        # Response surfaces the hash-chain fields.
        data = resp.json()["data"]
        assert data["pod_hash"] == pod_hash
        assert data["previous_pod_hash"] == "0" * 64
        assert data["chain_sequence"] == 1

    def test_submit_pod_links_to_prior_pod_in_chain(self):
        """Second POD's previous_pod_hash equals the first POD's pod_hash. Validates: Req 4.5.2."""
        es = _make_es_service()
        # Simulate the presence of a prior POD in the tenant's chain.
        prior_hash = "a1b2c3" + "0" * 58
        es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "pod_id": "prior",
                                "tenant_id": TENANT_ID,
                                "pod_hash": prior_hash,
                                "chain_sequence": 7,
                            }
                        }
                    ],
                    "total": {"value": 1},
                }
            }
        )
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["previous_pod_hash"] == prior_hash
        assert doc["chain_sequence"] == 8


    def test_stores_photo_refs(self):
        """Photo file_refs are stored correctly. Validates: Requirements 8.1, 4.1.4"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        photos = [_PHOTO_REF_1, _PHOTO_REF_2]
        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(photo_refs=photos),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["photo_refs"] == photos
        # Deprecated photo_urls is blanked when file_refs win.
        assert doc["photo_urls"] == []

    def test_stores_meter_ticket_ref(self):
        """Optional meter_ticket_ref is persisted when provided. Validates: Requirement 4.1.4"""
        es = _make_es_service()
        app = _make_app(es_service=es, job_service=_make_job_service())

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["meter_ticket_ref"] == _METER_TICKET_REF

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
# Test: file_ref validation (tenant-prefix enforcement)
# ---------------------------------------------------------------------------


class TestPodFileRefs:
    """Tests for the file_ref path on the POD endpoint.

    Validates: Requirements 4.1.4, 4.1.6.
    """

    def test_file_refs_validated_via_file_storage_service(self):
        """Each supplied file_ref is validated against the tenant prefix.

        Validates: Requirement 4.1.4
        """
        fs = _make_file_storage()
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            file_storage=fs,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(
                    signature_ref=_SIGNATURE_REF,
                    photo_refs=[_PHOTO_REF_1, _PHOTO_REF_2],
                    meter_ticket_ref=_METER_TICKET_REF,
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # signature + 2 photos + meter_ticket = 4 validation calls.
        assert fs.validate_ref.call_count == 4
        validated_refs = [
            call.kwargs.get("file_ref") or call.args[1]
            for call in fs.validate_ref.call_args_list
        ]
        assert _SIGNATURE_REF in validated_refs
        assert _PHOTO_REF_1 in validated_refs
        assert _PHOTO_REF_2 in validated_refs
        assert _METER_TICKET_REF in validated_refs

    def test_cross_tenant_signature_ref_returns_403(self):
        """A signature_ref from another tenant is rejected with HTTP 403.

        Validates: Requirements 4.1.4, 4.1.6
        """
        fs = _make_file_storage()
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            file_storage=fs,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(signature_ref=_CROSS_TENANT_SIGNATURE_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        body = resp.json()
        # Drill into the standardized error payload regardless of wrapper shape.
        details = body.get("details") or body.get("error", {}).get("details") or {}
        assert details.get("reason") == "cross_tenant_file_ref"
        assert details.get("field") == "signature_ref"

    def test_cross_tenant_photo_ref_returns_403(self):
        """A single cross-tenant photo_ref rejects the whole submission with 403.

        Validates: Requirements 4.1.4, 4.1.6
        """
        fs = _make_file_storage()
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            file_storage=fs,
        )
        cross_photo = (
            "tenants/other-tenant/photo/2024/01/15/"
            "66666666-6666-6666-6666-666666666666.jpg"
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(photo_refs=[_PHOTO_REF_1, cross_photo]),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        body = resp.json()
        details = body.get("details") or body.get("error", {}).get("details") or {}
        assert details.get("reason") == "cross_tenant_file_ref"
        assert "photo_refs" in (details.get("field") or "")

    def test_cross_tenant_meter_ticket_ref_returns_403(self):
        """A cross-tenant meter_ticket_ref is rejected with HTTP 403.

        Validates: Requirements 4.1.4, 4.1.6
        """
        fs = _make_file_storage()
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            file_storage=fs,
        )
        cross_meter = (
            "tenants/other-tenant/meter_ticket/2024/01/15/"
            "77777777-7777-7777-7777-777777777777.jpg"
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=cross_meter),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        body = resp.json()
        details = body.get("details") or body.get("error", {}).get("details") or {}
        assert details.get("reason") == "cross_tenant_file_ref"
        assert details.get("field") == "meter_ticket_ref"

    def test_legacy_url_path_still_accepted(self):
        """Submissions using only legacy signature_url/photo_urls still succeed.

        Validates: Requirement 4.1.4 — backward compatibility for the
        deprecated URL fields while the file_ref rollout completes.
        """
        fs = _make_file_storage()
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            file_storage=fs,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(
                    signature_ref=None,
                    photo_refs=None,
                    signature_url="https://example.com/sig.png",
                    photo_urls=["https://example.com/p1.jpg"],
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        # No file_refs provided, so FileStorageService is never consulted.
        assert fs.validate_ref.call_count == 0
        doc = es.index_document.call_args.args[2]
        assert doc["signature_ref"] is None
        assert doc["photo_refs"] == []
        assert doc["signature_url"] == "https://example.com/sig.png"
        assert doc["photo_urls"] == ["https://example.com/p1.jpg"]

    def test_file_ref_preferred_when_both_supplied(self):
        """When both file_refs and legacy URLs are supplied, file_refs win.

        Validates: Requirement 4.1.4 — handler prefers ``*_ref`` over the
        deprecated ``*_url`` fields.
        """
        fs = _make_file_storage()
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            file_storage=fs,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(
                    signature_ref=_SIGNATURE_REF,
                    photo_refs=[_PHOTO_REF_1],
                    signature_url="https://example.com/sig.png",
                    photo_urls=["https://example.com/p1.jpg"],
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["signature_ref"] == _SIGNATURE_REF
        assert doc["photo_refs"] == [_PHOTO_REF_1]
        # Legacy fields are suppressed when file_refs are present.
        assert doc["signature_url"] == ""
        assert doc["photo_urls"] == []


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


# ---------------------------------------------------------------------------
# Test: Meter-ticket OCR integration (Task 8.4)
# ---------------------------------------------------------------------------


def _make_ocr_service(
    *,
    extracted_gallons: Optional[float] = None,
    confidence: float = 0.0,
    requires_manual_review: bool = True,
    error_details: Optional[str] = None,
    side_effect=None,
) -> MagicMock:
    """Build a mock MeterTicketOCRService returning a configurable OCRResult.

    The mock honors the duck-typed contract the POD handler reads against
    (``ocr_result_id``, ``confidence``, ``extracted_gallons``,
    ``requires_manual_review``, ``error_details``). Pass ``side_effect`` to
    exercise timeout / provider failure paths.
    """
    svc = MagicMock()
    if side_effect is not None:
        svc.extract = AsyncMock(side_effect=side_effect)
        return svc

    result = MagicMock()
    result.ocr_result_id = "ocr-result-1"
    result.confidence = confidence
    result.extracted_gallons = extracted_gallons
    result.requires_manual_review = requires_manual_review
    result.error_details = error_details
    svc.extract = AsyncMock(return_value=result)
    return svc


class TestSubmitPodOcrIntegration:
    """Tests for MeterTicketOCRService wiring into POD submission.

    Validates: Requirements 4.2.4, 4.2.5, 4.2.6 (Task 8.4).
    """

    def test_ocr_fills_delivered_gallons_when_driver_value_absent(self):
        """High-confidence OCR populates delivered_gallons with source=ocr.

        Validates: Requirement 4.2.4.
        """
        ocr = _make_ocr_service(
            extracted_gallons=812.5,
            confidence=0.92,
            requires_manual_review=False,
        )
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        ocr.extract.assert_called_once()
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] == pytest.approx(812.5)
        assert doc["delivered_gallons_source"] == "ocr"
        assert doc["ocr_result_id"] == "ocr-result-1"
        assert doc["ocr_confidence"] == pytest.approx(0.92)
        assert doc["ocr_requires_manual_review"] is False
        assert doc["ocr_error"] is None

    def test_driver_entered_gallons_suppresses_ocr(self):
        """Driver-supplied delivered_gallons wins and OCR is skipped.

        Validates: Requirement 4.2.4 (inverse — OCR only runs when the
        driver did not hand-type a value).
        """
        ocr = _make_ocr_service(extracted_gallons=1.0, confidence=0.99)
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(
                    meter_ticket_ref=_METER_TICKET_REF,
                    delivered_gallons=750.0,
                ),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        ocr.extract.assert_not_called()
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] == pytest.approx(750.0)
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_error"] is None

    def test_requires_manual_review_falls_through_to_manual(self):
        """``requires_manual_review=True`` forces manual confirmation.

        Validates: Requirement 4.2.5.
        """
        ocr = _make_ocr_service(
            extracted_gallons=500.0,
            confidence=0.52,
            requires_manual_review=True,
        )
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] is None
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_requires_manual_review"] is True
        assert doc["ocr_error"] == "requires_manual_review"

    def test_ocr_provider_error_records_error_and_falls_back(self):
        """OCR service returning an ``error_details`` falls through to manual.

        Validates: Requirement 4.2.5 — any provider error must route the
        POD through manual entry with the error recorded.
        """
        ocr = _make_ocr_service(
            extracted_gallons=None,
            confidence=0.0,
            requires_manual_review=True,
            error_details="textract_error:ThrottlingException",
        )
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] is None
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_error"] == "textract_error:ThrottlingException"

    def test_ocr_timeout_records_timeout_error(self):
        """An OCR timeout beyond 15s falls through to manual with a logged error.

        Validates: Requirement 4.2.6.
        """
        import asyncio

        ocr = _make_ocr_service(side_effect=asyncio.TimeoutError())
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] is None
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_error"] == "textract_timeout"

    def test_ocr_raises_unexpected_exception_falls_back_to_manual(self):
        """Unexpected OCR exceptions are caught and translated to manual entry.

        Validates: Requirement 4.2.5 — the POD flow must never hard-fail
        because of a misbehaving OCR provider.
        """
        ocr = _make_ocr_service(side_effect=RuntimeError("boom"))
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] is None
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_error"] == "ocr_error:RuntimeError"

    def test_ocr_cross_tenant_permission_error_returns_403(self):
        """``PermissionError`` from the OCR service maps to HTTP 403.

        Validates: Requirements 4.1.4, 4.1.6 — cross-tenant meter_ticket
        refs must be rejected at every layer.
        """
        ocr = _make_ocr_service(side_effect=PermissionError("cross-tenant"))
        # Use a permissive file_storage mock so the validate_ref step passes
        # and we exercise the OCR-layer tenant guard directly.
        fs = MagicMock()
        fs.validate_ref = MagicMock(return_value=True)
        app = _make_app(
            es_service=_make_es_service(),
            job_service=_make_job_service(),
            file_storage=fs,
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 403
        body = resp.json()
        details = body.get("details") or body.get("error", {}).get("details") or {}
        assert details.get("reason") == "cross_tenant_file_ref"
        assert details.get("field") == "meter_ticket_ref"

    def test_no_meter_ticket_ref_skips_ocr_entirely(self):
        """POD with no meter_ticket_ref skips OCR and defaults to manual=None.

        Validates: Requirement 4.2.4 — OCR is only invoked when a
        meter_ticket_ref is supplied.
        """
        ocr = _make_ocr_service(extracted_gallons=999.0, confidence=0.99)
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=ocr,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        ocr.extract.assert_not_called()
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] is None
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_error"] is None
        assert doc["ocr_result_id"] is None

    def test_no_ocr_service_configured_skips_extraction(self):
        """When ocr_service is not wired the handler falls through gracefully.

        Validates: Requirement 4.2.5 — an unprovisioned OCR backend must
        not break POD submission; the POD simply records manual entry.
        """
        es = _make_es_service()
        app = _make_app(
            es_service=es,
            job_service=_make_job_service(),
            ocr_service=None,
        )

        with _SETTINGS_PATCH:
            client = TestClient(app)
            resp = client.post(
                "/api/driver/jobs/JOB_1/pod",
                json=_pod_payload(meter_ticket_ref=_METER_TICKET_REF),
                headers=_auth_headers(),
            )

        assert resp.status_code == 200
        doc = es.index_document.call_args.args[2]
        assert doc["delivered_gallons"] is None
        assert doc["delivered_gallons_source"] == "manual"
        assert doc["ocr_error"] is None
