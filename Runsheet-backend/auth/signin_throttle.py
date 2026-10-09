"""
Sign-in and password-reset throttle (staging finding F5).

``POST /auth/signin``, ``POST /auth/driver/session`` and
``POST /auth/user/password/reset/token`` had no rate limit at all: the SDK
routes sit outside every ``@limiter.limit`` decorator. This module counts
attempts in Redis, so every replica shares the same counters, along two
dimensions:

* **Sign-in per client IP — all attempts** (default 20 per 60 s). Counted
  before the credential check, so it bounds core and hashing load and password
  spraying across many emails without needing the outcome. 20/min is well above
  human use, so an office behind one NAT is not affected.
* **Sign-in per email — failures only** (default 10 per 900 s). A user who
  signs in correctly never spends budget, and a success deletes the counter.
  The window opens at the first failure and checks made while blocked never
  extend it, so a victim's lockout lasts at most one window and is never
  permanent; the per-IP limit bounds how fast an attacker can re-lock it.
  Unknown and known emails count the same, so a 429 is not an enumeration
  oracle.
* **Password-reset token — all attempts** on both dimensions (default 10 per
  900 s per IP, 5 per 3600 s per email). The endpoint always answers OK, so
  there is no failure signal; the per-email cap stops reset-email bombing.

``/auth/signin`` and ``/auth/driver/session`` share the sign-in buckets: they
verify the same credential, so attempts cannot be split across them.

Every counter is a fixed window created in one MULTI: ``SET key 0 EX window NX``
then ``INCR``. A key therefore always carries a TTL, and no sequence of calls
can leave a permanent lock. Concurrent failures for one email can overshoot the
limit by the concurrency level; that is accepted.

Emails are normalized (``strip().lower()``) and hashed into the key, so no
plaintext address sits in Redis keys or log lines.

**When Redis is unavailable this fails open**: the attempt is allowed and a
WARNING is logged per occurrence. Failing closed would lock every user out of
sign-in for the duration of a Redis outage. This matches ``PinAttemptLimiter``.

The PIN routes keep their own ``PinAttemptLimiter``; this module does not touch
them.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_KEY_PREFIX = "auth_throttle"


def normalize_email(value: Any) -> str:
    """Return the email as it is keyed: trimmed and lowercased ('' if absent)."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _email_digest(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]


def _email_key(scope: str, email: str) -> str:
    """Key for a per-email counter. The address is hashed, never stored."""
    return f"{_KEY_PREFIX}:{scope}:email:{_email_digest(email)}"


def _ip_key(scope: str, ip: str) -> str:
    return f"{_KEY_PREFIX}:{scope}:ip:{ip}"


class SignInThrottle:
    """Redis fixed-window counters for sign-in and password-reset (F5).

    ``check_*`` return ``None`` when the attempt may proceed, or the number of
    seconds to wait (for ``Retry-After``) when it is blocked. With no Redis
    client every method is a no-op that allows the attempt.
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Any] = None,
        signin_ip_max: int = 20,
        signin_ip_window: int = 60,
        signin_email_max_failures: int = 10,
        signin_email_window: int = 900,
        reset_ip_max: int = 10,
        reset_ip_window: int = 900,
        reset_email_max: int = 5,
        reset_email_window: int = 3600,
    ) -> None:
        self._redis = redis_client
        self._signin_ip_max = max(1, int(signin_ip_max))
        self._signin_ip_window = max(1, int(signin_ip_window))
        self._signin_email_max = max(1, int(signin_email_max_failures))
        self._signin_email_window = max(1, int(signin_email_window))
        self._reset_ip_max = max(1, int(reset_ip_max))
        self._reset_ip_window = max(1, int(reset_ip_window))
        self._reset_email_max = max(1, int(reset_email_max))
        self._reset_email_window = max(1, int(reset_email_window))

    @property
    def enabled(self) -> bool:
        """``True`` when a Redis client is wired and limits are enforced."""
        return self._redis is not None

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    async def check_sign_in(self, ip: str, email: Any) -> Optional[int]:
        """Count one sign-in attempt for ``ip`` and check the email's failures.

        The per-IP counter is incremented on every call. The per-email counter
        is only read here (GET/TTL), so a blocked email's window is never
        extended by further attempts.
        """
        if self._redis is None:
            return None
        normalized = normalize_email(email)
        ip_key = _ip_key("signin", ip)
        email_key = _email_key("signin", normalized) if normalized else None
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(ip_key, 0, ex=self._signin_ip_window, nx=True)
                pipe.incr(ip_key)
                pipe.ttl(ip_key)
                if email_key:
                    pipe.get(email_key)
                    pipe.ttl(email_key)
                results = await pipe.execute()
        except Exception as exc:
            self._fail_open("signin", exc)
            return None

        waits = []
        ip_count, ip_ttl = int(results[1]), int(results[2])
        if ip_count > self._signin_ip_max:
            waits.append(self._retry_after(ip_ttl, self._signin_ip_window))
        if email_key:
            failures = int(results[3] or 0)
            if failures >= self._signin_email_max:
                waits.append(self._retry_after(int(results[4]), self._signin_email_window))
        return self._blocked("signin", ip, normalized, waits)

    async def record_sign_in_failure(self, email: Any) -> None:
        """Count one failed sign-in for ``email`` (opens the window if new)."""
        normalized = normalize_email(email)
        if self._redis is None or not normalized:
            return
        key = _email_key("signin", normalized)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(key, 0, ex=self._signin_email_window, nx=True)
                pipe.incr(key)
                await pipe.execute()
        except Exception as exc:
            self._fail_open("signin_failure", exc)

    async def clear_sign_in_failures(self, email: Any) -> None:
        """Forget ``email``'s failures after a correct credential."""
        normalized = normalize_email(email)
        if self._redis is None or not normalized:
            return
        try:
            await self._redis.delete(_email_key("signin", normalized))
        except Exception as exc:
            self._fail_open("signin_clear", exc)

    # ------------------------------------------------------------------
    # Password reset
    # ------------------------------------------------------------------

    async def check_password_reset(self, ip: str, email: Any) -> Optional[int]:
        """Count one reset-token request for ``ip`` and ``email`` (all attempts)."""
        if self._redis is None:
            return None
        normalized = normalize_email(email)
        ip_key = _ip_key("reset", ip)
        email_key = _email_key("reset", normalized) if normalized else None
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(ip_key, 0, ex=self._reset_ip_window, nx=True)
                pipe.incr(ip_key)
                pipe.ttl(ip_key)
                if email_key:
                    pipe.set(email_key, 0, ex=self._reset_email_window, nx=True)
                    pipe.incr(email_key)
                    pipe.ttl(email_key)
                results = await pipe.execute()
        except Exception as exc:
            self._fail_open("reset", exc)
            return None

        waits = []
        if int(results[1]) > self._reset_ip_max:
            waits.append(self._retry_after(int(results[2]), self._reset_ip_window))
        if email_key and int(results[4]) > self._reset_email_max:
            waits.append(self._retry_after(int(results[5]), self._reset_email_window))
        return self._blocked("reset", ip, normalized, waits)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _retry_after(ttl: int, window: int) -> int:
        # TTL is -1/-2 only if the key lost its expiry or vanished between
        # commands; report the full window rather than 0.
        return ttl if ttl >= 1 else window

    @staticmethod
    def _blocked(scope: str, ip: str, email: str, waits: list) -> Optional[int]:
        if not waits:
            return None
        retry = max(waits)
        logger.warning(
            "Auth attempt throttled (F5): scope=%s ip=%s retry_after=%ds",
            scope,
            ip,
            retry,
            extra={"extra_data": {
                "scope": scope,
                "ip": ip,
                "email_hash": _email_digest(email)[:12] if email else None,
                "retry_after_seconds": retry,
            }},
        )
        return retry

    @staticmethod
    def _fail_open(operation: str, exc: Exception) -> None:
        logger.warning(
            "Sign-in throttle Redis call failed; allowing the attempt (fail open): "
            "operation=%s error=%s",
            operation,
            type(exc).__name__,
            extra={"extra_data": {"operation": operation, "error": str(exc)}},
        )


# ----------------------------------------------------------------------
# Process-wide instance
# ----------------------------------------------------------------------

_throttle: SignInThrottle = SignInThrottle()


def configure_signin_throttle(settings: Any, redis_client: Optional[Any] = None) -> SignInThrottle:
    """Build the process-wide throttle from settings (called by ``init_supertokens``).

    ``redis_client`` is for tests. Without one, a dedicated client is built from
    ``settings.redis_url`` with short socket timeouts so an outage fails open
    fast. Disabled (logged once) when the throttle is switched off, when
    ``redis_url`` is unset, or under ``ENVIRONMENT=test``: CI and local runs
    share one long-lived Redis, and real counters would leak 429s between tests
    and runs. Tests inject a fake client instead.
    """
    global _throttle
    limits: Dict[str, int] = dict(
        signin_ip_max=settings.signin_ip_max_attempts,
        signin_ip_window=settings.signin_ip_window_seconds,
        signin_email_max_failures=settings.signin_email_max_failures,
        signin_email_window=settings.signin_email_window_seconds,
        reset_ip_max=settings.password_reset_ip_max_requests,
        reset_ip_window=settings.password_reset_ip_window_seconds,
        reset_email_max=settings.password_reset_email_max_requests,
        reset_email_window=settings.password_reset_email_window_seconds,
    )

    if redis_client is None:
        environment = str(getattr(settings.environment, "value", settings.environment))
        redis_url = (settings.redis_url or "").strip()
        if not settings.auth_signin_throttle_enabled:
            reason = "AUTH_SIGNIN_THROTTLE_ENABLED is false"
        elif environment == "test":
            reason = "ENVIRONMENT=test (tests inject a fake client)"
        elif not redis_url:
            reason = "REDIS_URL is not set"
        else:
            reason = ""
        if reason:
            logger.warning(
                "Sign-in throttle disabled (F5): %s. Sign-in and password-reset "
                "are not rate limited.",
                reason,
            )
            _throttle = SignInThrottle(**limits)
            return _throttle

        import redis.asyncio as aioredis

        # Lazy: no connection is opened until the first request.
        redis_client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )

    _throttle = SignInThrottle(redis_client=redis_client, **limits)
    logger.info(
        "Sign-in throttle enabled (F5): signin %d/%ds per IP, %d failures/%ds "
        "per email; reset %d/%ds per IP, %d/%ds per email",
        limits["signin_ip_max"],
        limits["signin_ip_window"],
        limits["signin_email_max_failures"],
        limits["signin_email_window"],
        limits["reset_ip_max"],
        limits["reset_ip_window"],
        limits["reset_email_max"],
        limits["reset_email_window"],
    )
    return _throttle


def get_signin_throttle() -> SignInThrottle:
    """Return the process-wide throttle (a disabled no-op until configured)."""
    return _throttle


def reset_signin_throttle() -> None:
    """Restore the disabled default. For tests."""
    global _throttle
    _throttle = SignInThrottle()


def throttled_envelope(retry_after: int) -> Dict[str, Any]:
    """The 429 body: the standard error envelope plus ``request_id``."""
    from errors.exceptions import too_many_attempts
    from middleware.request_id import get_request_id

    body = too_many_attempts(retry_after).to_dict()
    body["request_id"] = get_request_id() or str(uuid.uuid4())
    return body


__all__ = [
    "SignInThrottle",
    "configure_signin_throttle",
    "get_signin_throttle",
    "normalize_email",
    "reset_signin_throttle",
    "throttled_envelope",
]
