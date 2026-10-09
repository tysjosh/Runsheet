"""Per-request "is this portal binding still live" check (design §2.3 step 4, E6).

A signed session outlives a revoke or a customer archive until it expires, so
every portal request re-checks:

* an ``active`` grant exists for the session's user, tenant and customer
  (``auth_users.st_user_id`` joined to ``portal_user_grants`` by email);
* the commerce customer exists and isn't ``archived``.

Verdicts are cached in-process for ``portal_principal_cache_seconds`` keyed
``(tenant, user, customer_id)``, so a re-bind within the TTL can't reuse a
stale ``ok``; :meth:`PortalPrincipalChecker.invalidate_user` drops every key
for a user (revoke). A store failure raises 503 ``PORTAL_UNAVAILABLE`` and is
never cached (fail closed).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Literal, Optional, Protocol, Tuple

from errors.codes import ErrorCode
from errors.exceptions import AppException, portal_unavailable

logger = logging.getLogger(__name__)

Verdict = Literal["ok", "revoked", "customer_archived"]


class PortalGrantStore(Protocol):
    """Read side of ``portal_user_grants`` the checker needs."""

    async def find_active_grant(
        self, *, tenant_id: str, user_id: str, customer_id: str
    ) -> Optional[Dict[str, Any]]:
        """``{"grant_id", "first_seen_at"}`` of the active grant, or ``None``."""
        ...

    async def mark_first_seen(self, *, grant_id: str) -> None:
        """Set ``first_seen_at = now()`` when it is still null."""
        ...


class PostgresPortalGrantStore:
    """:class:`PortalGrantStore` over the ``auth_users`` / grants tables."""

    async def find_active_grant(
        self, *, tenant_id: str, user_id: str, customer_id: str
    ) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        from persistence.database import is_persistence_enabled, session_scope

        if not is_persistence_enabled():
            raise RuntimeError("persistence layer is dormant (database_url unset)")
        query = text(
            "SELECT g.grant_id, g.first_seen_at "
            "FROM auth_users a "
            "JOIN portal_user_grants g "
            "  ON g.email = a.email AND g.tenant_id = a.tenant_id "
            "WHERE a.st_user_id = :user_id "
            "  AND a.tenant_id = :tenant_id "
            "  AND a.customer_id = :customer_id "
            "  AND g.customer_id = :customer_id "
            "  AND g.status = 'active' "
            "LIMIT 1"
        )
        async with session_scope() as db:
            row = (
                await db.execute(
                    query,
                    {
                        "user_id": user_id,
                        "tenant_id": tenant_id,
                        "customer_id": customer_id,
                    },
                )
            ).first()
        if row is None:
            return None
        return {"grant_id": row[0], "first_seen_at": row[1]}

    async def mark_first_seen(self, *, grant_id: str) -> None:
        from sqlalchemy import text

        from persistence.database import session_scope

        async with session_scope() as db:
            await db.execute(
                text(
                    "UPDATE portal_user_grants SET first_seen_at = now() "
                    "WHERE grant_id = :grant_id AND first_seen_at IS NULL"
                ),
                {"grant_id": grant_id},
            )


_CacheKey = Tuple[str, str, str]


class PortalPrincipalChecker:
    """Cached grant + customer check; see the module docstring."""

    def __init__(
        self,
        *,
        customer_service: Any,
        grant_store: Optional[PortalGrantStore] = None,
        cache_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._customers = customer_service
        self._grants: PortalGrantStore = grant_store or PostgresPortalGrantStore()
        self._ttl = float(cache_seconds)
        self._clock = clock
        self._cache: Dict[_CacheKey, Tuple[Verdict, float]] = {}

    def invalidate_user(self, user_id: str) -> None:
        """Drop every cached verdict for ``user_id`` (any tenant, any customer)."""
        for key in [k for k in self._cache if k[1] == user_id]:
            self._cache.pop(key, None)

    async def check(self, tenant_id: str, user_id: str, customer_id: str) -> Verdict:
        key = (tenant_id, user_id, customer_id)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        try:
            verdict = await self._evaluate(tenant_id, user_id, customer_id)
        except AppException as exc:
            if exc.error_code == ErrorCode.PORTAL_UNAVAILABLE:
                raise
            logger.error(
                "Portal principal check failed for tenant=%s user=%s: %s",
                tenant_id,
                user_id,
                exc.error_code,
            )
            raise portal_unavailable() from exc
        except Exception as exc:  # noqa: BLE001 — fail closed, never cache
            logger.error(
                "Portal principal check failed for tenant=%s user=%s: %s",
                tenant_id,
                user_id,
                type(exc).__name__,
            )
            raise portal_unavailable() from exc

        self._cache[key] = (verdict, now + self._ttl)
        return verdict

    async def _evaluate(self, tenant_id: str, user_id: str, customer_id: str) -> Verdict:
        grant = await self._grants.find_active_grant(
            tenant_id=tenant_id, user_id=user_id, customer_id=customer_id
        )
        if grant is None:
            return "revoked"

        try:
            customer = await self._customers.get(tenant_id, customer_id)
        except AppException as exc:
            if exc.error_code != ErrorCode.RESOURCE_NOT_FOUND:
                raise
            # A binding to a customer that no longer exists can't be used.
            return "customer_archived"
        if (customer or {}).get("status") == "archived":
            return "customer_archived"

        if grant.get("first_seen_at") is None:
            await self._grants.mark_first_seen(grant_id=grant["grant_id"])
        return "ok"


# ---------------------------------------------------------------------------
# Module registry (configured from bootstrap/core.py)
# ---------------------------------------------------------------------------

_checker: Optional[PortalPrincipalChecker] = None


def configure_portal_principal(checker: Optional[PortalPrincipalChecker]) -> None:
    """Install the checker; ``None`` leaves the portal failing closed (503)."""
    global _checker
    _checker = checker


def get_principal_checker() -> Optional[PortalPrincipalChecker]:
    return _checker


__all__ = [
    "PortalGrantStore",
    "PortalPrincipalChecker",
    "PostgresPortalGrantStore",
    "configure_portal_principal",
    "get_principal_checker",
]
