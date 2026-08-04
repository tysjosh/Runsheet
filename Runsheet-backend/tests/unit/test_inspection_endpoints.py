"""
Unit tests for inspection intake — the router
(``driver/api/inspection_endpoints.py``) over a real
:class:`~driver.services.inspection_service.InspectionService`.

The service is wired to fakes rather than mocked, so the assertions cover the
whole path: the composite document id ``{tenant_id}:{inspection_id}``, the
defect vocabulary, the tenant-prefix check on every photo ``file_ref``, and the
``X-Idempotency-Key`` replay.

Two of these are scope properties rather than behaviour checks. Pre-trip intake
reads **no** feature flag, so a tenant with
``driver.pretrip_inspection_required`` disabled — the default — still records
reports (R8.13). And a ``file_ref`` carrying another tenant's prefix is refused
*before* the write, so a rejected report leaves nothing behind (R15.8).

``post_trip`` is the one conditional value: refused where the flag is disabled or
unreadable, accepted with the pre-trip field set where the tenant has enabled the
workflow (R8.8).

Validates: Requirements 8.3, 8.4, 8.8, 8.10, 8.12, 8.13, 15.8
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import driver.middleware.idempotency as idempotency_module
from driver.api.inspection_endpoints import (
    configure_inspection_endpoints,
    router as inspection_router,
)
from driver.middleware.idempotency import configure_idempotency_middleware
from driver.services.driver_es_mappings import (
    IDEMPOTENCY_KEYS_INDEX,
    VEHICLE_INSPECTIONS_INDEX,
)
from driver.services.inspection_service import (
    DEFECT_SEVERITIES,
    INSPECTION_COMPONENTS,
)
from errors.handlers import register_exception_handlers
from tests.support.auth_seam import auth_headers, install_test_auth

TENANT = "t1"
OTHER_TENANT = "t2"
DRIVER = "drv_1"
ASSET = "truck_7"

TIMESTAMP = "2026-05-01T06:15:00+00:00"
LOCAL_DATE = "2026-05-01"

OWN_REF = f"tenants/{TENANT}/inspection/2026/05/01/photo-1.jpg"
FOREIGN_REF = f"tenants/{OTHER_TENANT}/inspection/2026/05/01/photo-1.jpg"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeES:
    """Records every ``vehicle_inspections`` write."""

    def __init__(self) -> None:
        self.indexed: List[tuple] = []

    async def index_document(self, index, doc_id, document):
        self.indexed.append((index, doc_id, dict(document)))
        return {"result": "created"}


class FakeFileStorage:
    """``validate_ref`` with the real tenant-prefix rule and nothing else."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def validate_ref(self, *, tenant_id: str, file_ref: str, actor=None) -> bool:
        self.calls.append(file_ref)
        if not file_ref.startswith(f"tenants/{tenant_id}/"):
            raise PermissionError("cross-tenant file_ref")
        return True


class FakeFeatureFlags:
    """Overlay flag source that records any read and answers ``disabled``."""

    def __init__(self, state: str = "disabled") -> None:
        self._state = state
        self.reads: List[tuple] = []

    async def get_overlay_state(self, flag_key: str, tenant_id: str) -> str:
        self.reads.append((flag_key, tenant_id))
        return self._state


class FakeIdempotencyES:
    """In-memory stand-in for the ``idempotency_keys`` index."""

    def __init__(self) -> None:
        self.docs: Dict[str, dict] = {}

    async def index_document(self, index: str, doc_id: str, document: dict) -> None:
        assert index == IDEMPOTENCY_KEYS_INDEX
        self.docs[doc_id] = document

    async def get_document(self, index: str, doc_id: str):
        assert index == IDEMPOTENCY_KEYS_INDEX
        return self.docs.get(doc_id)


@pytest.fixture
def idempotency_store():
    previous = idempotency_module.get_idempotency_middleware()
    store = FakeIdempotencyES()
    configure_idempotency_middleware(es_service=store)
    try:
        yield store
    finally:
        idempotency_module._idempotency_middleware = previous


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(
    *,
    es_service: Any = None,
    file_storage_service: Any = None,
    feature_flag_service: Any = None,
    scheduling_ws_manager: Any = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(inspection_router)
    # The full argument set, always: ``configure_inspection_endpoints`` assigns
    # every module global unconditionally, so an omitted argument is a reset.
    configure_inspection_endpoints(
        es_service=es_service,
        file_storage_service=file_storage_service,
        feature_flag_service=feature_flag_service,
        scheduling_ws_manager=scheduling_ws_manager,
    )
    install_test_auth(app)
    return app


def _driver_headers(driver_id: str = DRIVER, **kwargs) -> dict:
    kwargs.setdefault("roles", ["driver"])
    return auth_headers(TENANT, sub="user-1", driver_id=driver_id, **kwargs)


def _body(**overrides) -> dict:
    body = {
        "asset_id": ASSET,
        "odometer_miles": 128450.5,
        "inspection_timestamp": TIMESTAMP,
        "inspection_local_date": LOCAL_DATE,
        "defects": [],
    }
    body.update(overrides)
    return body


def _defect(
    *,
    component: str = "service_brakes",
    severity: str = "minor",
    note: str = "left front pad thin",
    photo_refs: Optional[List[str]] = None,
) -> dict:
    return {
        "component": component,
        "severity": severity,
        "note": note,
        "photo_refs": photo_refs or [],
    }


# ---------------------------------------------------------------------------
# Accepted submissions
# ---------------------------------------------------------------------------


class TestAcceptedSubmission:
    """The R8.3 / R8.4 field set, and the composite document id."""

    def test_accepts_a_report_and_writes_the_composite_document_id(self):
        """Validates: Requirements 8.3, 8.4"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect()]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["driver_id"] == DRIVER
        assert data["tenant_id"] == TENANT
        assert data["asset_id"] == ASSET
        assert data["inspection_type"] == "pre_trip"
        assert data["odometer_miles"] == 128450.5
        assert data["inspection_timestamp"] == TIMESTAMP
        assert data["inspection_local_date"] == LOCAL_DATE
        assert data["server_received_at"]
        assert data["defects"] == [
            {
                "component": "service_brakes",
                "severity": "minor",
                "note": "left front pad thin",
                "photo_refs": [],
            }
        ]

        # Exactly one write, on ``vehicle_inspections``, under
        # ``{tenant_id}:{inspection_id}``.
        assert len(es.indexed) == 1
        index, doc_id, doc = es.indexed[0]
        assert index == VEHICLE_INSPECTIONS_INDEX
        assert doc_id == f"{TENANT}:{doc['inspection_id']}"

    def test_a_report_with_no_defects_is_accepted(self):
        """A clean walk-around is a report, not an empty submission.

        Validates: Requirements 8.3
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections", json=_body(), headers=_driver_headers()
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["defects"] == []
        assert len(es.indexed) == 1

    def test_local_date_is_derived_when_the_client_sends_none(self):
        """Validates: Requirements 8.3"""
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(inspection_local_date=None),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["inspection_local_date"] == LOCAL_DATE

    def test_an_out_of_service_defect_sets_the_denormalized_flag(self):
        """Validates: Requirements 8.4"""
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(
                defects=[_defect(component="tires", severity="out_of_service")]
            ),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["has_out_of_service_defect"] is True

    def test_a_minor_defect_leaves_the_flag_false(self):
        """Validates: Requirements 8.4"""
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect()]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["data"]["has_out_of_service_defect"] is False


# ---------------------------------------------------------------------------
# The flag is not consulted (R8.12, R8.13)
# ---------------------------------------------------------------------------


class TestFlagIndependence:
    """Intake is in force in every tenant, flag or no flag."""

    def test_accepts_while_the_pretrip_flag_is_disabled(self):
        """The flag defaults to disabled and intake never reads it.

        Validates: Requirements 8.12, 8.13
        """
        es = FakeES()
        flags = FakeFeatureFlags(state="disabled")
        client = TestClient(
            _make_app(es_service=es, feature_flag_service=flags)
        )

        resp = client.post(
            "/api/driver/inspections",
            json=_body(
                defects=[_defect(component="tires", severity="out_of_service")]
            ),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert len(es.indexed) == 1
        assert resp.json()["data"]["has_out_of_service_defect"] is True
        # R8.11 — no flag read happens anywhere on the intake path.
        assert flags.reads == []

    def test_accepts_with_no_feature_flag_service_wired_at_all(self):
        """Validates: Requirements 8.13"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es, feature_flag_service=None))

        resp = client.post(
            "/api/driver/inspections", json=_body(), headers=_driver_headers()
        )

        assert resp.status_code == 200
        assert len(es.indexed) == 1


# ---------------------------------------------------------------------------
# Defect validation (R8.4)
# ---------------------------------------------------------------------------


class TestDefectValidation:
    """The component list and the two severities are closed vocabularies."""

    def test_unknown_component_is_rejected_and_nothing_persists(self):
        """Validates: Requirements 8.4"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect(component="flux_capacitor")]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.indexed == []

    def test_unknown_severity_is_rejected_and_nothing_persists(self):
        """Validates: Requirements 8.4"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect(severity="catastrophic")]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.indexed == []

    def test_every_declared_component_and_severity_is_accepted(self):
        """The vocabularies the service publishes are the ones it accepts.

        Validates: Requirements 8.4
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        for severity in DEFECT_SEVERITIES:
            resp = client.post(
                "/api/driver/inspections",
                json=_body(
                    defects=[
                        _defect(component=component, severity=severity)
                        for component in INSPECTION_COMPONENTS
                    ]
                ),
                headers=_driver_headers(),
            )
            assert resp.status_code == 200, resp.text
            assert len(resp.json()["data"]["defects"]) == len(
                INSPECTION_COMPONENTS
            )

    def test_a_negative_odometer_reading_is_rejected(self):
        """Validates: Requirements 8.3"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(odometer_miles=-1),
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert resp.json()["error_code"] == "INVALID_REQUEST"
        assert es.indexed == []

    def test_a_malformed_timestamp_is_rejected(self):
        """Validates: Requirements 8.3"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(inspection_timestamp="last tuesday"),
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert es.indexed == []

    def test_post_trip_intake_is_refused_where_the_flag_is_disabled(self):
        """Post-trip intake is closed until the tenant enables the workflow.

        No ``feature_flag_service`` is wired, which is the fail-closed case: the
        flag reads as disabled and the submission is refused rather than stored.

        Validates: Requirements 8.3, 8.8, 8.12
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(inspection_type="post_trip"),
            headers=_driver_headers(),
        )

        assert resp.status_code == 400
        assert resp.json()["details"]["reason"] == "post_trip_intake_not_enabled"
        assert es.indexed == []

    def test_post_trip_intake_is_accepted_where_the_flag_is_enabled(self):
        """The same body is recorded once the tenant enables the workflow.

        The field set is the pre-trip field set — only ``inspection_type``
        distinguishes the two — and the retention stamp still lands (R8.9).

        Validates: Requirements 8.8, 8.9
        """
        es = FakeES()
        client = TestClient(
            _make_app(
                es_service=es,
                feature_flag_service=FakeFeatureFlags(state="active_gated"),
            )
        )

        resp = client.post(
            "/api/driver/inspections",
            json=_body(inspection_type="post_trip"),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["inspection_type"] == "post_trip"
        assert data["asset_id"] == ASSET
        assert data["driver_id"] == DRIVER
        assert data["odometer_miles"] == 128450.5
        assert data["expires_at"]
        assert len(es.indexed) == 1


# ---------------------------------------------------------------------------
# Photo file_ref tenant prefix (R15.8)
# ---------------------------------------------------------------------------


class TestPhotoRefs:
    """Every submitted ref is validated against the caller's tenant prefix."""

    def test_own_tenant_ref_is_accepted_and_validated(self):
        """Validates: Requirements 8.4, 15.8"""
        es = FakeES()
        storage = FakeFileStorage()
        client = TestClient(
            _make_app(es_service=es, file_storage_service=storage)
        )

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect(photo_refs=[OWN_REF])]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 200
        assert storage.calls == [OWN_REF]
        assert resp.json()["data"]["defects"][0]["photo_refs"] == [OWN_REF]

    def test_foreign_tenant_ref_is_forbidden_and_nothing_persists(self):
        """Validates: Requirements 15.8"""
        es = FakeES()
        storage = FakeFileStorage()
        client = TestClient(
            _make_app(es_service=es, file_storage_service=storage)
        )

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect(photo_refs=[FOREIGN_REF])]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "FORBIDDEN"
        # The rejection names the field, never the other tenant.
        assert OTHER_TENANT not in resp.text
        assert es.indexed == []

    def test_one_foreign_ref_rejects_the_whole_report(self):
        """Validation completes before the single write.

        Validates: Requirements 15.8
        """
        es = FakeES()
        client = TestClient(
            _make_app(es_service=es, file_storage_service=FakeFileStorage())
        )

        resp = client.post(
            "/api/driver/inspections",
            json=_body(
                defects=[
                    _defect(photo_refs=[OWN_REF]),
                    _defect(component="tires", photo_refs=[FOREIGN_REF]),
                ]
            ),
            headers=_driver_headers(),
        )

        assert resp.status_code == 403
        assert es.indexed == []

    def test_refs_with_no_file_storage_wired_fail_closed(self):
        """Validates: Requirements 15.8"""
        es = FakeES()
        client = TestClient(
            _make_app(es_service=es, file_storage_service=None),
            raise_server_exceptions=False,
        )

        resp = client.post(
            "/api/driver/inspections",
            json=_body(defects=[_defect(photo_refs=[OWN_REF])]),
            headers=_driver_headers(),
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"
        assert es.indexed == []


# ---------------------------------------------------------------------------
# Idempotency (R8.10)
# ---------------------------------------------------------------------------


class TestIdempotency:
    """A seen key replays; an unseen key is a first-time submission."""

    def test_repeated_key_replays_the_stored_response(self, idempotency_store):
        """Validates: Requirements 8.10"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))
        headers = {**_driver_headers(), "X-Idempotency-Key": "key-1"}

        first = client.post(
            "/api/driver/inspections", json=_body(), headers=headers
        )
        second = client.post(
            "/api/driver/inspections", json=_body(), headers=headers
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        assert second.headers.get("X-Idempotent-Replayed") == "true"
        # The replay wrote nothing: one report, not two.
        assert len(es.indexed) == 1

    def test_an_unseen_key_is_processed_as_a_first_time_submission(
        self, idempotency_store
    ):
        """Validates: Requirements 8.10"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        first = client.post(
            "/api/driver/inspections",
            json=_body(),
            headers={**_driver_headers(), "X-Idempotency-Key": "key-1"},
        )
        second = client.post(
            "/api/driver/inspections",
            json=_body(),
            headers={**_driver_headers(), "X-Idempotency-Key": "key-2"},
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert "X-Idempotent-Replayed" not in second.headers
        assert len(es.indexed) == 2
        assert (
            first.json()["data"]["inspection_id"]
            != second.json()["data"]["inspection_id"]
        )

    def test_no_header_means_no_deduplication(self, idempotency_store):
        """Validates: Requirements 8.10"""
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        client.post(
            "/api/driver/inspections", json=_body(), headers=_driver_headers()
        )
        client.post(
            "/api/driver/inspections", json=_body(), headers=_driver_headers()
        )

        assert len(es.indexed) == 2


# ---------------------------------------------------------------------------
# Identity and wiring
# ---------------------------------------------------------------------------


class TestIdentityAndWiring:
    """The subject is the session's driver, and an unwired router fails closed."""

    def test_body_driver_id_is_rejected_outright(self):
        """The submission surface carries no ``driver_id`` at all.

        Validates: Requirements 8.3
        """
        es = FakeES()
        client = TestClient(_make_app(es_service=es))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(driver_id="drv_2"),
            headers=_driver_headers(),
        )

        assert resp.status_code == 422
        assert es.indexed == []

    def test_non_driver_role_cannot_submit(self):
        """Validates: Requirements 8.3"""
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(),
            headers=auth_headers(TENANT, roles=["dispatcher"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "INSUFFICIENT_ROLE"

    def test_missing_driver_identity_is_rejected(self):
        """Validates: Requirements 8.3"""
        client = TestClient(_make_app(es_service=FakeES()))

        resp = client.post(
            "/api/driver/inspections",
            json=_body(),
            headers=auth_headers(TENANT, roles=["driver"]),
        )

        assert resp.status_code == 403
        assert resp.json()["error_code"] == "DRIVER_IDENTITY_MISSING"

    def test_unconfigured_router_returns_a_structured_error(self):
        client = TestClient(
            _make_app(es_service=None), raise_server_exceptions=False
        )

        resp = client.post(
            "/api/driver/inspections", json=_body(), headers=_driver_headers()
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "INTERNAL_ERROR"

    def test_the_submission_route_names_no_driver_id(self):
        """R8.3 as a surface property: the write cannot name another driver."""
        for route in inspection_router.routes:
            if "POST" not in getattr(route, "methods", set()):
                continue
            assert "driver_id" not in route.path
            params = {p.name for p in getattr(route, "dependant").query_params}
            assert "driver_id" not in params
