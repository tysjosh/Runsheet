"""
Self-tests for ``tests/support/driver_envelope.py``.

The helper is an assertion surface every later driver-surface test leans on, so
it needs its own guard: an assertion helper that silently passes everything is
worse than no helper at all. These tests drive it against a real FastAPI app
wired with ``errors.handlers.register_exception_handlers``, so the accepted
shape is the *actual* ``ErrorResponse`` serialization (``exclude_none=True``)
rather than a hand-written approximation.

Covers both halves of the helper:

* Req 15.10 — the envelope carries ``error_code``, ``message``, ``details``,
  ``request_id``, and the two foreign error shapes (raw ``HTTPException``'s
  ``{"detail": str}`` and the legacy nested ``{"detail": {"error_code": ...}}``)
  are rejected.
* Req 15.14 — an authorization rejection that echoes the caller's held roles or
  the assigned driver's identity fails.

Validates: Requirements 15.10, 15.14
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from errors.exceptions import (
    driver_identity_missing,
    forbidden,
    resource_not_found,
)
from errors.handlers import register_exception_handlers
from tests.support.driver_envelope import (
    ENVELOPE_FIELDS,
    assert_authorization_rejection,
    assert_driver_error,
    assert_error_envelope,
    assert_no_identity_echo,
    is_authorization_rejection,
)


# ---------------------------------------------------------------------------
# App under test — one route per error shape the helper must judge
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/clean-authz")
    def clean_authz():
        # The canonical driver-surface authorization rejection: fixed message,
        # no caller context. Note the message legitimately contains the word
        # "driver" — a substring role scan would false-positive here.
        raise driver_identity_missing()

    @app.get("/leaky-authz")
    def leaky_authz():
        # What pod_endpoints.py used to do: both driver ids in ``details``.
        raise forbidden(
            message="Not your job",
            details={
                "requesting_driver": "drv_caller",
                "assigned_driver": "drv_other",
                "roles": ["driver"],
            },
        )

    @app.get("/not-found")
    def not_found():
        raise resource_not_found(message="Job not found", details={"job_id": "job_1"})

    @app.get("/raw-detail")
    def raw_detail():
        # Byte-identical to what a raw HTTPException renders. Built with a
        # JSONResponse on purpose so this file stays at zero raw-exception
        # call sites under tests/unit/test_http_exception_ceiling.py.
        return JSONResponse(status_code=403, content={"detail": "Not permitted"})

    @app.get("/nested-detail")
    def nested_detail():
        return JSONResponse(
            status_code=403,
            content={"detail": {"error_code": "FORBIDDEN", "message": "nope"}},
        )

    @app.get("/validation-detail")
    def validation_detail():
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": ["body", "gallons"], "msg": "required"}]},
        )

    @app.get("/projection-pending")
    def projection_pending():
        # R13.18 — the one 2xx that legitimately carries an error_code.
        return JSONResponse(
            status_code=202,
            content={
                "error_code": "DUTY_STATUS_PROJECTION_PENDING",
                "message": "Duty status recorded; projection is catching up.",
                "request_id": "req-1",
            },
        )

    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# Req 15.10 — envelope shape
# ---------------------------------------------------------------------------


def test_conforming_envelope_is_accepted_and_normalized(client: TestClient) -> None:
    envelope = assert_error_envelope(
        client.get("/not-found"),
        expected_status=404,
        expected_code="RESOURCE_NOT_FOUND",
    )
    assert set(envelope) == set(ENVELOPE_FIELDS)
    assert envelope["details"] == {"job_id": "job_1"}
    assert envelope["request_id"]


def test_absent_details_normalizes_to_none(client: TestClient) -> None:
    """``exclude_none=True`` drops ``details``; the helper fills it back in."""
    response = client.get("/clean-authz")
    assert "details" not in response.json()

    envelope = assert_error_envelope(response, expected_status=403)
    assert envelope["details"] is None


def test_raw_http_exception_shape_is_rejected(client: TestClient) -> None:
    with pytest.raises(AssertionError, match="raw HTTPException shape"):
        assert_error_envelope(client.get("/raw-detail"), expected_status=403)


def test_legacy_nested_shape_is_rejected(client: TestClient) -> None:
    with pytest.raises(AssertionError, match="legacy nested shape"):
        assert_error_envelope(client.get("/nested-detail"), expected_status=403)


def test_framework_validation_shape_is_rejected(client: TestClient) -> None:
    with pytest.raises(AssertionError, match="request-validation shape"):
        assert_error_envelope(client.get("/validation-detail"), expected_status=422)


def test_unexpected_status_or_code_fails(client: TestClient) -> None:
    with pytest.raises(AssertionError, match="Expected HTTP 403"):
        assert_error_envelope(client.get("/not-found"), expected_status=403)
    with pytest.raises(AssertionError, match="Expected error_code"):
        assert_error_envelope(client.get("/not-found"), expected_code="FORBIDDEN")


def test_projection_pending_is_the_only_tolerated_2xx_envelope(
    client: TestClient,
) -> None:
    envelope = assert_error_envelope(client.get("/projection-pending"))
    assert envelope["error_code"] == "DUTY_STATUS_PROJECTION_PENDING"

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "error_code": "FORBIDDEN",
                "message": "nope",
                "request_id": "req-2",
            }

    with pytest.raises(AssertionError, match="must ride a 4xx/5xx status"):
        assert_error_envelope(_FakeResponse())


# ---------------------------------------------------------------------------
# Req 15.14 — no role or driver-identity echo
# ---------------------------------------------------------------------------


def test_clean_rejection_passes_even_when_message_mentions_driver(
    client: TestClient,
) -> None:
    """A fixed message containing the word "driver" is not a role echo."""
    assert_authorization_rejection(
        client.get("/clean-authz"),
        expected_status=403,
        expected_code="DRIVER_IDENTITY_MISSING",
        roles=["driver"],
        driver_id="drv_caller",
        assigned_driver_id="drv_other",
    )


def test_leaked_driver_ids_and_role_key_fail(client: TestClient) -> None:
    response = client.get("/leaky-authz")
    with pytest.raises(AssertionError) as excinfo:
        assert_authorization_rejection(
            response,
            expected_status=403,
            roles=["driver"],
            driver_id="drv_caller",
            assigned_driver_id="drv_other",
        )
    message = str(excinfo.value)
    assert "caller driver_id" in message
    assert "assigned driver_id" in message
    assert "names the caller's roles" in message


def test_assigned_driver_echo_alone_fails() -> None:
    """The assigned driver is a *different* identity than the caller."""
    envelope = {
        "error_code": "FORBIDDEN",
        "message": "This job is assigned to drv_other",
        "details": None,
        "request_id": "req-3",
    }
    with pytest.raises(AssertionError, match="assigned driver_id"):
        assert_no_identity_echo(envelope, assigned_driver_id="drv_other")


def test_extra_forbidden_values_are_checked() -> None:
    envelope = {
        "error_code": "FORBIDDEN",
        "message": "Insufficient permissions",
        "details": {"contact": "+15551234567"},
        "request_id": "req-4",
    }
    with pytest.raises(AssertionError, match="forbidden value"):
        assert_no_identity_echo(envelope, also_forbidden=["+15551234567"])


def test_non_authorization_rejection_skips_identity_checks(
    client: TestClient,
) -> None:
    """``assert_driver_error`` only applies Req 15.14 to authz rejections."""
    assert not is_authorization_rejection(404, "RESOURCE_NOT_FOUND")
    envelope = assert_driver_error(
        client.get("/not-found"),
        expected_status=404,
        roles=["driver"],
    )
    assert envelope["error_code"] == "RESOURCE_NOT_FOUND"


def test_strict_helper_rejects_a_downgraded_authorization_failure(
    client: TestClient,
) -> None:
    with pytest.raises(AssertionError, match="is not an authorization rejection"):
        assert_authorization_rejection(client.get("/not-found"), expected_status=404)
