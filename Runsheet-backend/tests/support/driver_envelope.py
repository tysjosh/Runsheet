"""
Shared error-envelope assertions for the driver surface.

Every rejection on a ``/api/driver`` (or ``/auth/driver``) route is an
``errors.exceptions.AppException`` rendered by
``errors.handlers.handle_app_exception`` into the ``schemas.common.ErrorResponse``
envelope: ``error_code``, ``message``, ``details``, ``request_id`` (Req 15.10).
Two other error shapes exist in the codebase and MUST NOT appear on a driver
route:

* FastAPI's own ``{"detail": "..."}`` / ``{"detail": [ ... ]}`` — what a raw
  ``HTTPException`` or an unconverted request-validation failure produces.
* The legacy nested ``{"detail": {"error_code": ...}}`` shape some pre-existing
  fuel-ops handlers still emit.

``assert_error_envelope`` fails on both with a message that names the shape it
found, so a regression reads as "this handler raised a raw HTTPException"
rather than as an opaque ``KeyError``.

The second half of this module enforces Req 15.14: an authorization rejection
must not echo the roles the caller holds, nor the identity of the driver the
resource is assigned to. ``assert_no_identity_echo`` walks the envelope and
fails when a held role turns up as a *value* or when a driver identifier turns
up anywhere.

Role matching is deliberately value-based rather than substring-based. The
fixed rejection messages legitimately contain the word "driver" ("This
operation requires a driver identity on the session."), so a substring scan
would flag every driver-role caller. What Req 15.14 forbids is the response
*carrying* the caller's role list back — i.e. the role appearing as a JSON
value or as a ``roles``-flavored key — and that is what is checked here.

Usage::

    from tests.support.auth_seam import auth_headers
    from tests.support.driver_envelope import assert_authorization_rejection

    resp = client.get("/api/driver/work", headers=auth_headers("t1", roles=["driver"]))
    assert_authorization_rejection(
        resp,
        expected_status=403,
        expected_code="DRIVER_IDENTITY_MISSING",
        roles=["driver"],
    )

Test-only module: it makes assertions, it never relaxes one.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional, Sequence

#: The four envelope fields, in ``ErrorResponse`` declaration order.
ENVELOPE_FIELDS: tuple[str, ...] = ("error_code", "message", "details", "request_id")

#: ``handle_app_exception`` serializes with ``exclude_none=True``, so a rejection
#: carrying no context omits ``details`` on the wire rather than sending
#: ``null``. Both are accepted and normalized to ``None``; nothing else is.
_OPTIONAL_FIELDS: frozenset[str] = frozenset({"details"})

#: Error codes that are authorization rejections — the set Req 15.14 governs.
AUTHORIZATION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "UNAUTHORIZED",
        "FORBIDDEN",
        "INSUFFICIENT_ROLE",
        "SESSION_EXPIRED",
        "DRIVER_IDENTITY_MISSING",
        "DRIVER_RECORD_NOT_PROVISIONED",
        "PIN_VERIFICATION_FAILED",
        "PIN_ATTEMPTS_EXCEEDED",
        "OTP_VERIFICATION_FAILED",
        "SENDER_IDENTITY_MISMATCH",
    }
)

#: HTTP statuses an authorization rejection may carry. ``PIN_ATTEMPTS_EXCEEDED``
#: is the lockout, so 429 belongs here alongside 401/403.
AUTHORIZATION_STATUS_CODES: frozenset[int] = frozenset({401, 403, 429})

#: Envelope codes that intentionally ride a non-error status. R13.18: the duty
#: status event is durable and only its projection lags, so the response is a
#: 202 that still carries an ``error_code``.
_NON_ERROR_ENVELOPE_CODES: frozenset[str] = frozenset(
    {"DUTY_STATUS_PROJECTION_PENDING"}
)

#: Keys that would carry the caller's held roles. Flagged even when empty,
#: because the key itself is the disclosure surface.
_ROLE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "roles",
        "role",
        "caller_roles",
        "held_roles",
        "roles_held",
        "user_roles",
        "session_roles",
        "granted_roles",
        "actor_roles",
    }
)

#: Below this length an identifier is matched by equality rather than by
#: substring — a two-character id would otherwise collide with the random
#: ``request_id``.
_MIN_SUBSTRING_ID_LENGTH = 3


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def _body(response: Any) -> Any:
    """Return the decoded JSON body, or fail with the raw text."""
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - any decode failure is the same verdict
        text = getattr(response, "text", "<no body>")
        raise AssertionError(
            "Driver-surface error response body is not JSON. The structured "
            f"envelope is required (Req 15.10). Got: {text!r}"
        ) from None


def _describe_foreign_shape(payload: Mapping[str, Any]) -> Optional[str]:
    """Name the non-envelope error shape in ``payload``, if it is one."""
    if "detail" not in payload:
        return None
    detail = payload["detail"]
    if isinstance(detail, list):
        return (
            "FastAPI request-validation shape {'detail': [...]}. A driver "
            "endpoint must validate through the structured envelope (e.g. "
            "errors.exceptions.invalid_request / validation_error) instead of "
            "letting the framework default render the rejection"
        )
    if isinstance(detail, Mapping) and "error_code" in detail:
        return (
            "legacy nested shape {'detail': {'error_code': ...}}. Raise an "
            "AppException so the code lands at the top level of the envelope"
        )
    return (
        "raw HTTPException shape {'detail': <str>}. Req 15.10 forbids a raw "
        "HTTPException on any module added by this feature"
    )


def parse_error_envelope(response: Any) -> dict[str, Any]:
    """Validate the envelope shape of ``response`` and return it normalized.

    The returned dict always has all four :data:`ENVELOPE_FIELDS`, with
    ``details`` filled in as ``None`` when the wire body omitted it.
    """
    payload = _body(response)

    if not isinstance(payload, Mapping):
        raise AssertionError(
            "Driver-surface error body must be a JSON object carrying "
            f"{list(ENVELOPE_FIELDS)} (Req 15.10). Got {type(payload).__name__}: "
            f"{payload!r}"
        )

    foreign = _describe_foreign_shape(payload)
    if foreign is not None:
        raise AssertionError(
            f"Driver-surface error response uses the {foreign}. Body: {dict(payload)!r}"
        )

    missing = [
        field
        for field in ENVELOPE_FIELDS
        if field not in payload and field not in _OPTIONAL_FIELDS
    ]
    if missing:
        raise AssertionError(
            f"Driver-surface error envelope is missing {missing} (Req 15.10). "
            f"Body: {dict(payload)!r}"
        )

    unknown = sorted(set(payload) - set(ENVELOPE_FIELDS))
    if unknown:
        raise AssertionError(
            f"Driver-surface error envelope carries unexpected top-level keys "
            f"{unknown}. Only {list(ENVELOPE_FIELDS)} may appear — extra keys "
            f"leak handler internals. Body: {dict(payload)!r}"
        )

    for field in ("error_code", "message", "request_id"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise AssertionError(
                f"Driver-surface error envelope field {field!r} must be a "
                f"non-empty string (Req 15.10). Got {value!r}"
            )

    details = payload.get("details")
    if details is not None and not isinstance(details, Mapping):
        raise AssertionError(
            "Driver-surface error envelope field 'details' must be an object "
            f"or absent. Got {type(details).__name__}: {details!r}"
        )

    return {
        "error_code": payload["error_code"],
        "message": payload["message"],
        "details": dict(details) if isinstance(details, Mapping) else None,
        "request_id": payload["request_id"],
    }


def assert_error_envelope(
    response: Any,
    *,
    expected_status: Optional[int] = None,
    expected_code: Optional[str] = None,
) -> dict[str, Any]:
    """Assert ``response`` is a conforming driver-surface error envelope.

    Args:
        response: A ``TestClient`` response (anything with ``status_code`` and
            ``json()``).
        expected_status: Asserted when given.
        expected_code: Asserted against ``error_code`` when given.

    Returns:
        The normalized envelope, so callers can go on to assert on ``details``.
    """
    envelope = parse_error_envelope(response)
    status = response.status_code

    if expected_status is not None:
        assert status == expected_status, (
            f"Expected HTTP {expected_status}, got {status} with envelope "
            f"{envelope!r}"
        )
    elif (
        status < 400
        and envelope["error_code"] not in _NON_ERROR_ENVELOPE_CODES
    ):
        raise AssertionError(
            f"HTTP {status} carries error_code {envelope['error_code']!r}. A "
            "rejection must ride a 4xx/5xx status — the only 2xx envelope on "
            f"the driver surface is {sorted(_NON_ERROR_ENVELOPE_CODES)}"
        )

    if expected_code is not None:
        assert envelope["error_code"] == expected_code, (
            f"Expected error_code {expected_code!r}, got "
            f"{envelope['error_code']!r} (HTTP {status})"
        )

    return envelope


# ---------------------------------------------------------------------------
# Req 15.14 — no role or driver-identity echo
# ---------------------------------------------------------------------------


def is_authorization_rejection(status_code: int, error_code: str) -> bool:
    """Whether a rejection falls under Req 15.14's non-disclosure rule."""
    return (
        error_code in AUTHORIZATION_ERROR_CODES
        or status_code in AUTHORIZATION_STATUS_CODES
    )


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Yield ``(json_path, leaf)`` for every key and scalar leaf in ``node``."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield f"{child}<key>", str(key)
            yield from _walk(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")
    else:
        yield path or "<root>", node


def _identifier_hit(leaf: Any, identifier: str) -> bool:
    """Whether ``leaf`` discloses ``identifier``."""
    text = leaf if isinstance(leaf, str) else str(leaf)
    if len(identifier) < _MIN_SUBSTRING_ID_LENGTH:
        return text.strip().casefold() == identifier.casefold()
    return identifier.casefold() in text.casefold()


def assert_no_identity_echo(
    payload: Any,
    *,
    roles: Sequence[str] = (),
    driver_id: Optional[str] = None,
    assigned_driver_id: Optional[str] = None,
    also_forbidden: Sequence[str] = (),
    allow_values: Sequence[str] = (),
) -> None:
    """Assert an authorization rejection discloses neither roles nor driver ids.

    Args:
        payload: A response object or an already-parsed envelope / mapping.
        roles: The roles the caller held. None may appear as a value in the
            body, and no ``roles``-flavored key may appear at all (Req 15.14).
        driver_id: The caller's ``drivers_current.driver_id``.
        assigned_driver_id: The ``assigned_driver_id`` of the resource the
            caller was rejected from — the "assigned driver's identity" half of
            Req 15.14, which is often a *different* driver than the caller.
        also_forbidden: Extra values that must not appear (a driver name, a
            phone number, an auth user id).
        allow_values: Leaf values exempted from the role-value check, for the
            rare rejection whose fixed ``details`` legitimately equals a role
            name.
    """
    body = payload if isinstance(payload, Mapping) else _body(payload)
    if not isinstance(body, Mapping):
        raise AssertionError(
            f"Cannot inspect a non-object error body for identity echo: {body!r}"
        )

    leaves = list(_walk(body))
    exempt = {value.casefold() for value in allow_values}
    violations: list[str] = []

    role_values = {role.strip().casefold() for role in roles if role and role.strip()}
    for path, leaf in leaves:
        if path.endswith("<key>"):
            if str(leaf).casefold() in _ROLE_KEY_NAMES:
                violations.append(
                    f"key {path[: -len('<key>')]!r} names the caller's roles"
                )
            continue
        if not isinstance(leaf, str):
            continue
        normalized = leaf.strip().casefold()
        if normalized in role_values and normalized not in exempt:
            violations.append(f"{path} echoes the held role {leaf!r}")

    identifiers = {
        "caller driver_id": driver_id,
        "assigned driver_id": assigned_driver_id,
    }
    identifiers.update({f"forbidden value {v!r}": v for v in also_forbidden})
    for label, identifier in identifiers.items():
        if not identifier:
            continue
        for path, leaf in leaves:
            if _identifier_hit(leaf, identifier):
                violations.append(f"{path} discloses the {label} ({identifier!r})")

    assert not violations, (
        "Authorization rejection leaks caller/driver identity (Req 15.14):\n  - "
        + "\n  - ".join(violations)
        + f"\nBody: {json.dumps(body, default=str)}"
    )


def assert_driver_error(
    response: Any,
    *,
    expected_status: Optional[int] = None,
    expected_code: Optional[str] = None,
    roles: Sequence[str] = (),
    driver_id: Optional[str] = None,
    assigned_driver_id: Optional[str] = None,
    also_forbidden: Sequence[str] = (),
    allow_values: Sequence[str] = (),
) -> dict[str, Any]:
    """Assert the envelope shape, plus Req 15.14 when this is an authz rejection.

    The single entry point most driver tests want: it checks the four envelope
    fields on every error, and additionally runs
    :func:`assert_no_identity_echo` when the rejection is an authorization one
    (by code or by status).
    """
    envelope = assert_error_envelope(
        response, expected_status=expected_status, expected_code=expected_code
    )

    if is_authorization_rejection(response.status_code, envelope["error_code"]):
        assert_no_identity_echo(
            envelope,
            roles=roles,
            driver_id=driver_id,
            assigned_driver_id=assigned_driver_id,
            also_forbidden=also_forbidden,
            allow_values=allow_values,
        )
    return envelope


def assert_authorization_rejection(
    response: Any,
    *,
    expected_status: Optional[int] = None,
    expected_code: Optional[str] = None,
    roles: Sequence[str] = (),
    driver_id: Optional[str] = None,
    assigned_driver_id: Optional[str] = None,
    also_forbidden: Sequence[str] = (),
    allow_values: Sequence[str] = (),
) -> dict[str, Any]:
    """Assert ``response`` is an authorization rejection that discloses nothing.

    Stricter than :func:`assert_driver_error`: the response must actually *be*
    an authorization rejection, so a handler that quietly downgrades a 403 to a
    404-with-a-different-code fails here instead of skipping the Req 15.14
    checks.
    """
    envelope = assert_error_envelope(
        response, expected_status=expected_status, expected_code=expected_code
    )

    assert is_authorization_rejection(response.status_code, envelope["error_code"]), (
        f"HTTP {response.status_code} / {envelope['error_code']!r} is not an "
        "authorization rejection. Expected one of "
        f"{sorted(AUTHORIZATION_STATUS_CODES)} or a code in "
        f"{sorted(AUTHORIZATION_ERROR_CODES)}"
    )

    assert_no_identity_echo(
        envelope,
        roles=roles,
        driver_id=driver_id,
        assigned_driver_id=assigned_driver_id,
        also_forbidden=also_forbidden,
        allow_values=allow_values,
    )
    return envelope
