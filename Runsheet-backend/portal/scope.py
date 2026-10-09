"""Central default-deny for customer-portal sessions (design §2.2).

A session holding the ``customer`` role may reach only ``/api/portal/*`` and a
short allowlist of account routes. Every other authenticated HTTP route, and
every WebSocket, refuses it before any handler runs. The decision lives here
once and is applied by:

* ``middleware.auth_enforcement.AuthEnforcementMiddleware`` (E1/E2),
* ``ops.middleware.tenant_guard.get_tenant_context`` (E1b, defense in depth),
* ``bootstrap.websockets._resolve_ws_claims`` (E3).

This module must stay import-light: the tenant guard and the auth middleware
import it, so it may not import anything that imports them.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

#: The exclusive portal role (mirrors ``auth.supertokens_init.CUSTOMER_PORTAL_ROLE``).
PORTAL_ROLE = "customer"

#: Every portal API route starts with this prefix.
PORTAL_PATH_PREFIX = "/api/portal/"

#: Non-portal routes a customer session may call (PD4). These are the real
#: account routes in ``auth/api/account_endpoints.py``.
CUSTOMER_ALLOWED_EXACT = frozenset(
    {
        "/api/auth/account/me",
        "/api/auth/account/change-password",
    }
)

#: Refusal codes (``errors.codes.ErrorCode`` values).
PORTAL_IDENTITY_INVALID = "PORTAL_IDENTITY_INVALID"
PORTAL_ROUTE_FORBIDDEN = "PORTAL_ROUTE_FORBIDDEN"


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_customer_claims(claims: Mapping[str, Any]) -> bool:
    """True when the claims hold the ``customer`` role (well-formed or not)."""
    roles = claims.get("roles") or []
    return isinstance(roles, (list, tuple)) and PORTAL_ROLE in roles


def customer_session_verdict(claims: Mapping[str, Any], path: str) -> Optional[str]:
    """Decide whether a verified session may reach ``path``.

    Returns ``None`` to allow, otherwise the ``ErrorCode`` value to refuse with:

    * a session without ``customer`` is staff: always ``None`` (unchanged);
    * ``customer`` plus any other role, or without a non-empty
      ``customer_id``: ``PORTAL_IDENTITY_INVALID`` on every route;
    * a well-formed customer: ``None`` on the portal prefix and the
      allowlist, else ``PORTAL_ROUTE_FORBIDDEN``.
    """
    roles = [r for r in (claims.get("roles") or []) if isinstance(r, str)]
    if PORTAL_ROLE not in roles:
        return None
    if roles != [PORTAL_ROLE] or not _nonempty_str(claims.get("customer_id")):
        return PORTAL_IDENTITY_INVALID
    if path.startswith(PORTAL_PATH_PREFIX) or path in CUSTOMER_ALLOWED_EXACT:
        return None
    return PORTAL_ROUTE_FORBIDDEN


def portal_enabled(settings: Any) -> bool:
    """The portal serves traffic only with its flag AND the commerce backbone.

    The portal needs ``CustomerService``, which exists only when
    ``commerce_backbone_enabled`` is on.
    """
    return bool(
        getattr(settings, "customer_portal_enabled", False)
        and getattr(settings, "commerce_backbone_enabled", False)
    )


_WORD_SEGMENT = re.compile(r"^[A-Za-z_.-]+$")


def collapse_path(path: str) -> str:
    """Collapse id-like path segments to ``{id}`` for audit lines.

    Used where no route template is resolved yet (middleware before routing).
    Any segment that isn't purely letters, ``_``, ``.`` or ``-`` (so every
    number, UUID, email or free text) becomes ``{id}``, so raw ids never
    reach the log.
    """
    segments = (path or "").split("/")
    return "/".join(
        s if (not s or _WORD_SEGMENT.match(s)) else "{id}" for s in segments
    )


__all__ = [
    "CUSTOMER_ALLOWED_EXACT",
    "PORTAL_IDENTITY_INVALID",
    "PORTAL_PATH_PREFIX",
    "PORTAL_ROLE",
    "PORTAL_ROUTE_FORBIDDEN",
    "collapse_path",
    "customer_session_verdict",
    "is_customer_claims",
    "portal_enabled",
]
