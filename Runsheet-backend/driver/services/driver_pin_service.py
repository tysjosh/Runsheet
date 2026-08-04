"""
Driver_PIN_Service — the human-session surface over
:class:`~fuel.voice.driver_pin.DriverPinVault`.

The PIN is a **Dinee voice-agent identity factor**, not an app sign-in
credential: ``GET /voice/drivers/verify`` reads it behind the machine-to-machine
voice bearer key, and nothing else does. Until this service existed the vault had
no human-facing writer at all, so a driver could not set or change their own PIN
without calling the office (R2 provenance).

Every rule about what a PIN *is* lives here rather than in the router, so a
direct service call and an HTTP request reject exactly the same values with
exactly the same error:

* 4 to 8 characters, every one of them a decimal digit — anything else is 422
  ``INVALID_PIN_FORMAT`` (R2.2, R2.3). No stripping, no Unicode digit folding,
  no coercion from a JSON number: a PIN carrying a space, a ``+``, or an Arabic
  numeral is a *different secret* from the one the driver thinks they typed, and
  silently normalizing it would let a PIN be enrolled that can never be entered
  again.
* Not a single repeated digit, and not a strictly ascending or strictly
  descending digit sequence — that is 422 ``WEAK_PIN`` (R2.4). "Strictly
  ascending" is read literally as *each digit greater than the one before*, so
  ``1357`` is refused along with ``1234``; the consecutive-run reading is a
  subset of this one.
* A rotation verifies the current PIN through ``DriverPinVault.verify_pin``
  **before** the new hash is written, and a failed verification is 403
  ``PIN_VERIFICATION_FAILED`` (R2.5, R2.6). ``verify_pin`` answers ``False``
  for "no PIN on file" as well as for "wrong PIN", so a driver who was never
  enrolled and a driver who mistyped are indistinguishable to the caller — that
  is deliberate and is preserved here.

**Secret hygiene (R2.7).** The PIN, the hash, the salt, and the iteration count
appear in no return value, in no exception ``details``, and in no log record
this module emits. The exception details carry the *rule* (``min_length`` /
``max_length``) and never the value, not even its length. Every log line is
keyed on ``(tenant_id, driver_id)`` and an action name. The vault is the only
thing that ever sees hash material, and :meth:`DriverPinVault.has_pin` returns a
boolean rather than the record so even the enrollment-state read cannot carry it.

**The R2.8 lockout** is :class:`PinAttemptLimiter`, wrapping the single
``verify_pin`` call in :meth:`DriverPinService.rotate`: five consecutive failures
inside 15 minutes lock that ``(tenant_id, driver_id)`` for 15 minutes with 429
``PIN_ATTEMPTS_EXCEEDED``. "Consecutive" is literal — a verification that
succeeds deletes the counter — and the lock is *not* extended by attempts made
while it is in force, so it always lifts 15 minutes after the fifth failure
rather than 15 minutes after the last person to knock. See the class docstring
for the Redis-unavailable posture, which is **fail open**.

Collaborators arrive through :func:`configure_pin_endpoints`, the module-global
wiring convention this surface uses everywhere: no DI container, no ``Depends``
for collaborators, and the router reads the built service back through
:func:`get_pin_service` so the two can never hold different instances.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Bootstrap Wiring
(``Driver_PIN_Service``, Phase 2) and §Error Codes.

Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from errors.exceptions import (
    invalid_pin_format,
    pin_attempts_exceeded,
    pin_verification_failed,
    weak_pin,
)
from ops.middleware.tenant_guard import TenantContext

logger = logging.getLogger(__name__)

#: Inclusive PIN length bounds (R2.2).
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8

#: The only characters a PIN may contain (R2.2). Deliberately this literal and
#: not ``str.isdigit``, which is ``True`` for Arabic-Indic and other Unicode
#: decimal digits that the voice agent's DTMF keypad cannot produce.
_DECIMAL_DIGITS = frozenset("0123456789")

#: Consecutive failed verifications that trip the lockout (R2.8).
PIN_MAX_ATTEMPTS = 5

#: Both the counting window and the lockout duration, in seconds (R2.8). R2.8
#: gives the same 15 minutes to each, and one constant is what keeps them equal.
PIN_ATTEMPT_WINDOW_SECONDS = 15 * 60

#: Redis key holding the consecutive-failure count for one driver. Tenant-scoped
#: because ``driver_id`` is only unique inside a tenant — an unscoped key would
#: let one tenant's failures lock another tenant's driver out.
PIN_ATTEMPT_KEY_TEMPLATE = "driver_pin_attempts:{tenant_id}:{driver_id}"

__all__ = [
    "PIN_MIN_LENGTH",
    "PIN_MAX_LENGTH",
    "PIN_MAX_ATTEMPTS",
    "PIN_ATTEMPT_WINDOW_SECONDS",
    "PIN_ATTEMPT_KEY_TEMPLATE",
    "PinAttemptLimiter",
    "DriverPinService",
    "validate_pin",
    "is_weak_pin",
    "configure_pin_endpoints",
    "get_pin_service",
]


# ---------------------------------------------------------------------------
# PIN rules
# ---------------------------------------------------------------------------


def is_weak_pin(pin: str) -> bool:
    """Return ``True`` for a PIN R2.4 refuses.

    Three shapes are refused: one digit repeated (``0000``), a strictly
    ascending digit sequence (``1234``, and also ``1357`` — each digit greater
    than the one before), and a strictly descending one (``4321``, ``9630``).

    Args:
        pin: A PIN that has already passed :func:`validate_pin`'s format check,
            so every character is a decimal digit.

    Returns:
        ``True`` when the PIN is refused as guessable.

    Validates: Requirements 2.4
    """
    digits = [ord(char) - 48 for char in pin]
    if len(set(digits)) == 1:
        return True
    pairs = list(zip(digits, digits[1:]))
    if all(later > earlier for earlier, later in pairs):
        return True
    if all(later < earlier for earlier, later in pairs):
        return True
    return False


def validate_pin(value: Any, *, field: str = "pin") -> str:
    """Return ``value`` as an acceptable PIN, or raise.

    The single gate every write path goes through. It rejects in two steps so
    the caller learns *which* rule it broke: format first (R2.3), then
    guessability (R2.4).

    ``value`` is typed ``Any`` because the request models hand this function
    whatever the client sent. A non-string — a JSON number, a list, ``None`` —
    is a format rejection rather than a coercion: ``1234`` and ``"1234"`` would
    hash identically only by accident, and ``0123`` as a JSON number is not
    even representable.

    Args:
        value: The submitted PIN, unvalidated and of any type.
        field: Which body field is being validated, for the rejection details.
            Names the field only — never its value.

    Returns:
        The PIN, unchanged.

    Raises:
        AppException: 422 ``INVALID_PIN_FORMAT`` when the value is not a string
            of 4 to 8 decimal digits; 422 ``WEAK_PIN`` when it is one of the
            guessable shapes.

    Validates: Requirements 2.2, 2.3, 2.4, 2.7
    """
    # ``details`` states the rule and nothing about the submitted value — not
    # its content and not its length (R2.7).
    rule: Dict[str, Any] = {
        "field": field,
        "min_length": PIN_MIN_LENGTH,
        "max_length": PIN_MAX_LENGTH,
        "allowed_characters": "decimal digits",
    }

    if not isinstance(value, str):
        raise invalid_pin_format(details=rule)
    if not PIN_MIN_LENGTH <= len(value) <= PIN_MAX_LENGTH:
        raise invalid_pin_format(details=rule)
    if any(char not in _DECIMAL_DIGITS for char in value):
        raise invalid_pin_format(details=rule)
    if is_weak_pin(value):
        raise weak_pin(
            details={
                "field": field,
                "refused_shapes": [
                    "a single repeated digit",
                    "a strictly ascending digit sequence",
                    "a strictly descending digit sequence",
                ],
            }
        )
    return value


# ---------------------------------------------------------------------------
# Attempt lockout (R2.8)
# ---------------------------------------------------------------------------


class PinAttemptLimiter:
    """Consecutive-failure counter and lockout for PIN verification (R2.8).

    One Redis key per ``(tenant_id, driver_id)`` —
    ``driver_pin_attempts:{tenant_id}:{driver_id}`` — carrying an integer count
    and a TTL. Three operations, in the order the caller uses them:

    * :meth:`check` before ``verify_pin``. A count at or above
      :data:`PIN_MAX_ATTEMPTS` is the lockout, and raises 429.
    * :meth:`record_failure` on a ``False``. ``INCR`` creates the key at 1 when
      it does not exist; the TTL is (re)applied on the first failure, which opens
      the 15-minute window, and again on the fifth, which restarts it as the
      15-minute lockout.
    * :meth:`clear` on a ``True``, which deletes the key. This is what makes the
      threshold *consecutive* rather than cumulative: a correct PIN resets the
      count to zero even at four failures.

    One key rather than a separate counter and lock marker, because a single
    integer with a TTL already expresses both states and the happy path costs one
    ``GET``. The 429 path deliberately does **not** increment, so an attacker
    hammering a locked driver cannot hold the lock open indefinitely: it lifts 15
    minutes after the fifth failure, which is what R2.8 says.

    **When Redis is unavailable this fails open** — the verification proceeds and
    no lockout is enforced. That is the posture the surrounding design implies,
    for three reasons:

    1. The residual control is already mounted. Every PIN route carries
       ``@limiter.limit(..., key_func=driver_rate_key)``, so a brute force is
       bounded per driver per minute even with no counter at all, and each
       attempt still pays ``DriverPinVault``'s PBKDF2-HMAC-SHA256 at 200,000
       iterations.
    2. Failing closed would answer 429 to *every* rotation, including the ones
       presenting the correct PIN. A Redis outage would take PIN rotation away
       from the whole fleet while protecting nothing that (1) does not already
       bound.
    3. It matches how this surface treats every other Redis-backed counter and
       cache: ``work_service``'s bundle cache is "a permanent miss, never an
       error", and the POD chain lock degrades to a process-local one. Fail-
       closed is reserved for the overlay feature flags, where the safe default
       ("off") costs a tenant a feature rather than an operation.

    An unwired client is reported once at boot by ``bootstrap/driver.py`` rather
    than once per request. A wired client that *errors* is logged per occurrence,
    because that is a live fault rather than a known deployment shape.
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Any] = None,
        max_attempts: int = PIN_MAX_ATTEMPTS,
        window_seconds: int = PIN_ATTEMPT_WINDOW_SECONDS,
    ) -> None:
        """
        Args:
            redis_client: An ``async`` Redis client exposing ``get`` / ``incr``
                / ``expire`` / ``ttl`` / ``delete``. ``None`` disables the
                lockout — see the fail-open note in the class docstring.
            max_attempts: Consecutive failures that trip the lockout.
            window_seconds: The counting window and the lockout duration.
        """
        self._redis = redis_client
        self._max_attempts = max(1, int(max_attempts))
        self._window = max(1, int(window_seconds))

    @property
    def enabled(self) -> bool:
        """``True`` when a Redis client is wired and the lockout is in force."""
        return self._redis is not None

    def _key(self, tenant_id: str, driver_id: str) -> str:
        return PIN_ATTEMPT_KEY_TEMPLATE.format(
            tenant_id=tenant_id, driver_id=driver_id
        )

    async def check(self, tenant_id: str, driver_id: str) -> None:
        """Raise 429 when ``driver_id`` is locked out, else return.

        Raises:
            AppException: 429 ``PIN_ATTEMPTS_EXCEEDED``. ``details`` carry the
                rule and the remaining wait — never the PIN, and never anything
                about the PIN (R2.7).

        Validates: Requirements 2.7, 2.8
        """
        if self._redis is None:
            return

        key = self._key(tenant_id, driver_id)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:  # noqa: BLE001 — fail open, see class docstring
            logger.warning(
                "Driver PIN attempt counter read failed — lockout not enforced "
                "for this attempt: %s",
                exc,
                extra={
                    "extra_data": {
                        "tenant_id": tenant_id,
                        "driver_id": driver_id,
                        "action": "pin_attempt_check",
                        "outcome": "counter_unavailable",
                    }
                },
            )
            return

        if _as_int(raw) < self._max_attempts:
            return

        logger.warning(
            "Driver PIN verification refused — attempt lockout in force",
            extra={
                "extra_data": {
                    "tenant_id": tenant_id,
                    "driver_id": driver_id,
                    "action": "pin_attempt_check",
                    "outcome": "locked_out",
                }
            },
        )
        raise pin_attempts_exceeded(
            details={
                "max_attempts": self._max_attempts,
                "lockout_seconds": self._window,
                "retry_after_seconds": await self._retry_after(key),
            }
        )

    async def record_failure(self, tenant_id: str, driver_id: str) -> int:
        """Count one failed verification and return the new count.

        Returns ``0`` when there is no counter to write to, which the caller
        treats as "not counted" rather than "first failure".

        Validates: Requirements 2.8
        """
        if self._redis is None:
            return 0

        key = self._key(tenant_id, driver_id)
        try:
            attempts = _as_int(await self._redis.incr(key))
            # First failure opens the window; the one that trips the threshold
            # restarts it as the lockout, so the lock always runs 15 minutes
            # from the fifth failure. Any other value leaves the existing TTL
            # alone, which is what keeps the window from sliding forward on
            # every attempt.
            if attempts <= 1 or attempts == self._max_attempts:
                await self._redis.expire(key, self._window)
        except Exception as exc:  # noqa: BLE001 — fail open, see class docstring
            logger.warning(
                "Driver PIN attempt counter write failed — this failure is not "
                "counted toward the lockout: %s",
                exc,
                extra={
                    "extra_data": {
                        "tenant_id": tenant_id,
                        "driver_id": driver_id,
                        "action": "pin_attempt_record",
                        "outcome": "counter_unavailable",
                    }
                },
            )
            return 0

        if attempts >= self._max_attempts:
            logger.warning(
                "Driver PIN attempt lockout engaged",
                extra={
                    "extra_data": {
                        "tenant_id": tenant_id,
                        "driver_id": driver_id,
                        "action": "pin_attempt_record",
                        "outcome": "locked_out",
                        "lockout_seconds": self._window,
                    }
                },
            )
        return attempts

    async def clear(self, tenant_id: str, driver_id: str) -> None:
        """Drop the counter after a successful verification.

        The whole of "consecutive" lives in this one call.

        Validates: Requirements 2.8
        """
        if self._redis is None:
            return
        try:
            await self._redis.delete(self._key(tenant_id, driver_id))
        except Exception as exc:  # noqa: BLE001 — a stale counter expires anyway
            logger.warning(
                "Driver PIN attempt counter reset failed — the count expires "
                "with its TTL: %s",
                exc,
                extra={
                    "extra_data": {
                        "tenant_id": tenant_id,
                        "driver_id": driver_id,
                        "action": "pin_attempt_clear",
                        "outcome": "counter_unavailable",
                    }
                },
            )

    async def _retry_after(self, key: str) -> int:
        """Return the seconds left on the lockout, best effort.

        Falls back to the full window when the client cannot answer or reports
        no TTL: over-stating the wait is the safe direction for a client that
        schedules its retry off this number.
        """
        try:
            remaining = _as_int(await self._redis.ttl(key))
        except Exception:  # noqa: BLE001 — the number is advisory
            return self._window
        return remaining if remaining > 0 else self._window


def _as_int(value: Any) -> int:
    """Coerce a Redis reply to ``int``, treating anything unreadable as ``0``.

    A Redis client may answer ``bytes``, ``str``, ``int``, or ``None`` for the
    same ``GET`` depending on how it was constructed (``bootstrap/agents.py``
    builds the shared client with ``decode_responses=False``). A value that
    cannot be read as a count is treated as no count, which keeps
    :meth:`PinAttemptLimiter.check` fail-open on a corrupt key.
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        if isinstance(value, (bytes, bytearray)):
            return int(value.decode("utf-8", "ignore") or 0)
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DriverPinService:
    """Enrollment, rotation, revocation, and the enrollment-state read.

    Holds no state of its own: the vault is the store, and the ``driver_id``
    every method acts on is supplied by the caller from a verified session
    claim, never read off a request body.
    """

    def __init__(
        self,
        *,
        pin_vault: Any,
        telemetry_service: Optional[Any] = None,
        attempt_limiter: Optional[PinAttemptLimiter] = None,
    ) -> None:
        """
        Args:
            pin_vault: A :class:`~fuel.voice.driver_pin.DriverPinVault`-shaped
                object exposing ``set_pin`` / ``verify_pin`` / ``has_pin`` /
                ``delete_pin``. Required — without a store there is nothing to
                enroll into.
            telemetry_service: Optional audit sink exposing ``log_audit_event``.
                Revocations are audited with or without it: absent the sink the
                event is still written to the application log with
                ``audit_event: True`` (R2.10).
            attempt_limiter: The R2.8 lockout. Omitted, a client-less
                :class:`PinAttemptLimiter` stands in, so ``rotate`` calls the
                same three methods whether or not the lockout is in force and
                there is no second code path to keep correct.
        """
        if pin_vault is None:
            raise ValueError("pin_vault is required")
        self._vault = pin_vault
        self._telemetry = telemetry_service
        self._attempts = attempt_limiter or PinAttemptLimiter()

    # -- enrollment ---------------------------------------------------------

    async def enroll(
        self, tenant_id: str, driver_id: str, pin: Any
    ) -> Dict[str, Any]:
        """Store the hash of ``pin`` for ``(tenant_id, driver_id)``.

        The vault ref is ``driver_pin:{tenant_id}:{driver_id}``, derived by
        :class:`DriverPinVault` from these two arguments, so a driver can only
        ever enroll against their own identity and their own tenant (R2.1).
        Re-enrolling replaces the prior hash in place — enrollment is an upsert,
        which is what makes a driver who forgot their PIN able to set a new one
        without an administrator first revoking the old.

        Args:
            tenant_id: The caller's tenant.
            driver_id: The caller's canonical driver identifier, from
                ``TenantContext.driver_id``.
            pin: The submitted PIN, unvalidated.

        Returns:
            ``{"pin_enrolled": True}`` — no hash, no salt, no iteration count
            (R2.7).

        Raises:
            AppException: 422 ``INVALID_PIN_FORMAT`` or ``WEAK_PIN``.

        Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.7
        """
        accepted = validate_pin(pin, field="pin")
        await self._vault.set_pin(tenant_id, driver_id, accepted)
        logger.info(
            "Driver PIN enrolled",
            extra={
                "extra_data": {
                    "tenant_id": tenant_id,
                    "driver_id": driver_id,
                    "action": "pin_enroll",
                }
            },
        )
        return {"pin_enrolled": True}

    # -- rotation -----------------------------------------------------------

    async def rotate(
        self,
        tenant_id: str,
        driver_id: str,
        current_pin: Any,
        new_pin: Any,
    ) -> Dict[str, Any]:
        """Replace the stored PIN after verifying the current one.

        Order matters and is fixed: the current PIN is verified **first**, and
        only then is the new PIN's format and strength judged. Validating the
        new PIN first would turn this endpoint into an oracle that answers
        "your replacement is weak" for a caller who does not know the current
        PIN at all.

        The R2.8 lockout brackets the verification: the counter is checked
        before it, incremented on a ``False``, and cleared on a ``True``. The
        check runs ahead of the vault call so a locked-out caller does not get
        200,000 PBKDF2 iterations of work done on their behalf, and the 429
        pre-empts the 403 — a locked driver learns they are locked rather than
        that their PIN was wrong.

        Args:
            tenant_id: The caller's tenant.
            driver_id: The caller's canonical driver identifier.
            current_pin: The PIN currently on file, as submitted.
            new_pin: The replacement PIN, unvalidated.

        Returns:
            ``{"pin_enrolled": True}``.

        Raises:
            AppException: 429 ``PIN_ATTEMPTS_EXCEEDED`` when five consecutive
                verifications have already failed inside 15 minutes; 403
                ``PIN_VERIFICATION_FAILED`` when the current PIN does not verify
                — including when no PIN is on file, which ``verify_pin`` reports
                identically; 422 ``INVALID_PIN_FORMAT`` or ``WEAK_PIN`` for the
                replacement.

        Validates: Requirements 2.5, 2.6, 2.7, 2.8
        """
        await self._attempts.check(tenant_id, driver_id)

        presented = current_pin if isinstance(current_pin, str) else ""
        verified = await self._vault.verify_pin(tenant_id, driver_id, presented)
        if not verified:
            failures = await self._attempts.record_failure(tenant_id, driver_id)
            logger.warning(
                "Driver PIN rotation refused — current PIN did not verify",
                extra={
                    "extra_data": {
                        "tenant_id": tenant_id,
                        "driver_id": driver_id,
                        "action": "pin_rotate",
                        "outcome": "verification_failed",
                        # A count, not a value: how many times in a row, never
                        # what was submitted (R2.7).
                        "consecutive_failures": failures,
                    }
                },
            )
            raise pin_verification_failed()

        # The verification succeeded, so the failure run is over — cleared
        # before the replacement is judged, because a driver who proved the
        # current PIN and then offered a weak new one has not failed a
        # verification (R2.8).
        await self._attempts.clear(tenant_id, driver_id)

        accepted = validate_pin(new_pin, field="new_pin")
        await self._vault.set_pin(tenant_id, driver_id, accepted)
        logger.info(
            "Driver PIN rotated",
            extra={
                "extra_data": {
                    "tenant_id": tenant_id,
                    "driver_id": driver_id,
                    "action": "pin_rotate",
                    "outcome": "rotated",
                }
            },
        )
        return {"pin_enrolled": True}

    # -- enrollment state ---------------------------------------------------

    async def enrollment_state(
        self, tenant_id: str, driver_id: str
    ) -> Dict[str, Any]:
        """Return whether the driver has a PIN on file, as one boolean.

        The body is exactly one field. There is no ``enrolled_at``, no
        ``algorithm``, and no ``iterations``: R2.9 asks for the state and
        nothing else, and every additional field would be one more thing to
        keep hash material out of.

        Validates: Requirements 2.7, 2.9
        """
        enrolled = await self._vault.has_pin(tenant_id, driver_id)
        return {"pin_enrolled": bool(enrolled)}

    # -- revocation ---------------------------------------------------------

    async def revoke(
        self, tenant: TenantContext, driver_id: str
    ) -> Dict[str, Any]:
        """Delete a driver's stored PIN on an administrator's behalf.

        The subject is the ``driver_id`` the administrator named, not the
        caller, which is why this method takes the whole
        :class:`TenantContext`: the audit event has to carry the acting user
        alongside the affected driver (R2.10). Deleting a PIN that does not
        exist is not an error — the post-condition "this driver has no PIN" is
        what was asked for, and it holds either way. The audit event records
        which of the two happened.

        Args:
            tenant: The verified Auth_Context of the acting administrator. Role
                gating happens in the router, before this is called.
            driver_id: The driver whose PIN is being revoked.

        Returns:
            ``{"driver_id": ..., "pin_enrolled": False, "pin_existed": bool}``.

        Validates: Requirements 2.7, 2.10
        """
        existed = bool(await self._vault.delete_pin(tenant.tenant_id, driver_id))
        self._audit(
            tenant=tenant,
            driver_id=driver_id,
            outcome="deleted" if existed else "no_pin_on_file",
        )
        return {
            "driver_id": driver_id,
            "pin_enrolled": False,
            "pin_existed": existed,
        }

    # -- audit --------------------------------------------------------------

    def _audit(
        self, *, tenant: TenantContext, driver_id: str, outcome: str
    ) -> None:
        """Record a PIN revocation, its actor, its subject, and its outcome.

        Mirrors the App_Access_Service audit helper
        (``fuel/api/driver_endpoints.py``): the telemetry sink first when one is
        wired, then the application log unconditionally, so the event survives a
        boot without telemetry. A failing sink is swallowed — an audit write must
        never turn a completed revocation into a 500.

        The payload carries the acting user and the affected driver, and no PIN
        material of any kind (R2.7, R2.10).
        """
        payload = {
            "acting_user_id": tenant.user_id,
            "tenant_id": tenant.tenant_id,
            "driver_id": driver_id,
            "outcome": outcome,
        }
        telemetry = self._telemetry
        if telemetry is not None and hasattr(telemetry, "log_audit_event"):
            try:
                telemetry.log_audit_event(
                    event_type="driver_pin_revoke",
                    user_id=tenant.user_id,
                    resource_type="driver_pin",
                    resource_id=driver_id,
                    action="revoke",
                    details=payload,
                )
            except Exception as exc:  # noqa: BLE001 — audit must never 500
                logger.warning("Driver PIN audit sink failed: %s", exc)
        logger.info(
            "Audit: driver_pin_revoke %s for driver %s",
            outcome,
            driver_id,
            extra={"extra_data": {"audit_event": True, **payload}},
        )


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------

# Module-level collaborators, wired via configure_pin_endpoints(). The router in
# ``driver/api/pin_endpoints.py`` has no ``configure_*`` of its own and reads the
# built service back through get_pin_service(), so there is exactly one
# DriverPinService per process.
_pin_vault: Optional[Any] = None
_telemetry_service: Optional[Any] = None
_redis_client: Optional[Any] = None
_pin_service: Optional[DriverPinService] = None


def configure_pin_endpoints(
    *,
    pin_vault: Any = None,
    telemetry_service: Any = None,
    redis_client: Any = None,
) -> None:
    """Wire the Driver_PIN_Service collaborators and build the service.

    Called once during startup from ``bootstrap/driver.py``. Like every other
    ``configure_*`` on this surface it assigns each module global
    unconditionally, so an argument a caller omits is reset to ``None`` — every
    call site must pass its full argument set.

    ``pin_vault`` is the store. Absent it no service is built and every handler
    fails closed with 500 rather than accepting a PIN that nothing persists —
    the vault needs a ``TenantCredentialsVault``, which needs KMS, so a boot
    without it is a real deployment state rather than a hypothetical one.

    ``redis_client`` is the opposite case: absent it the service is still built
    and rotation still works, with the R2.8 lockout not enforced. A PIN rotation
    is then bounded only by the per-driver route rate limit — see
    :class:`PinAttemptLimiter` for why that is the right trade and
    ``bootstrap/driver.py`` for where the absence is reported.

    Args:
        pin_vault: The :class:`~fuel.voice.driver_pin.DriverPinVault`.
        telemetry_service: Optional audit sink for revocations (R2.10).
        redis_client: Optional ``async`` Redis client backing the R2.8 attempt
            lockout.
    """
    global _pin_vault, _telemetry_service, _redis_client, _pin_service

    _pin_vault = pin_vault
    _telemetry_service = telemetry_service
    _redis_client = redis_client
    _pin_service = (
        DriverPinService(
            pin_vault=pin_vault,
            telemetry_service=telemetry_service,
            attempt_limiter=PinAttemptLimiter(redis_client=redis_client),
        )
        if pin_vault is not None
        else None
    )


def get_pin_service() -> Optional[DriverPinService]:
    """Return the service :func:`configure_pin_endpoints` built, or ``None``."""
    return _pin_service
