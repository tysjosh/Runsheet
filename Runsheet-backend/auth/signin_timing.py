"""Fixed minimum response time for failed sign-ins (OI-12).

A wrong password for a real account took about 450 ms longer than an unknown
email (password hashing runs only for a real account), so response time told
an attacker which emails exist. Every failed sign-in, web and driver, is now
padded to ``SIGNIN_FAILURE_FLOOR_MS`` (default 1000 ms, 0 disables).

Only failures are padded: a success reveals nothing the caller doesn't
already know, and a throttled 429 doesn't depend on whether the account
exists.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

_DEFAULT_FLOOR_MS = 1000

#: Indirection so tests can replace the clock and the sleep.
_now: Callable[[], float] = time.monotonic
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def now() -> float:
    """Monotonic start mark for :func:`pad_to_floor`."""
    return _now()


def failure_floor_ms() -> int:
    """``settings.signin_failure_floor_ms``, or the default when unreadable."""
    try:
        from config.settings import get_settings

        value = getattr(get_settings(), "signin_failure_floor_ms", _DEFAULT_FLOOR_MS)
    except Exception:  # noqa: BLE001 — keep the protective default
        return _DEFAULT_FLOOR_MS
    if isinstance(value, bool) or not isinstance(value, int):
        return _DEFAULT_FLOOR_MS
    return value


async def pad_to_floor(
    started_monotonic: float,
    floor_ms: Optional[int] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> float:
    """Sleep until ``floor_ms`` has passed since *started_monotonic*.

    Returns the seconds slept (0 when the floor is disabled or already met).
    """
    floor = failure_floor_ms() if floor_ms is None else floor_ms
    if floor <= 0:
        return 0.0
    remaining = floor / 1000.0 - (_now() - started_monotonic)
    if remaining <= 0:
        return 0.0
    await (sleep or _sleep)(remaining)
    return remaining
