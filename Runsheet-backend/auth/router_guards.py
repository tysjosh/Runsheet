"""Router-level role gates.

:func:`auth.authorization.require_role` is the decision; this is the plumbing that
attaches it to a whole ``APIRouter`` instead of to one handler.

Why router-level rather than per-handler. The defect this exists to prevent is not
a handler with the *wrong* role — it is a handler with *no* role check, which no
test notices because nothing is looking. Both packages that needed fixing
(``commerce/api``, then ``compliance/api``) resolved ``get_tenant_context``
directly in every handler, so a new route was ungated by default and silently so.
Attaching the gate to the router inverts that default: a route added later
inherits it without anyone remembering to.

Usage — each package declares its own policy and keeps this module generic::

    # compliance/api/_authz.py
    COMPLIANCE_OPS_ROLES = ("admin", "dispatcher")
    compliance_ops_dependency = roles_dependency(*COMPLIANCE_OPS_ROLES)

    # compliance/api/ifta_endpoints.py
    router = APIRouter(
        prefix="/api/compliance/ifta",
        dependencies=[Depends(compliance_ops_dependency)],
    )

The mechanism lives here so there is exactly one of it. The Role_Authorizer
docstring records that this codebase already paid for having several
inconsistent per-router role checks; two mechanisms would start that over.

Ordering note: where a package also has a feature-flag guard that answers 404,
apply the role check *after* it, so a tenant without the module stays unable to
tell the difference between "absent" and "forbidden".
"""

from __future__ import annotations

from fastapi import Depends

from auth.authorization import require_role
from ops.middleware.tenant_guard import TenantContext, get_tenant_context


def roles_dependency(*allowed: str):
    """Build a FastAPI dependency requiring any one of ``allowed`` roles.

    Args:
        *allowed: Role names; holding any one grants access. Matched exactly by
            :func:`auth.authorization.require_role` — never by substring, and no
            role implies another.

    Returns:
        An async dependency that resolves the verified ``TenantContext``, applies
        the role check, and returns the context so handlers can still depend on
        it directly.

    Raises:
        AppException: ``insufficient_role`` (HTTP 403) at request time when the
            caller holds none of the required roles. The rejection carries only
            the requirement, never the caller's held roles.
    """
    if not allowed:
        # A gate that allows everything is worse than no gate: it reads as
        # enforcement at the call site while enforcing nothing.
        raise ValueError("roles_dependency() requires at least one role name")

    async def _dependency(
        tenant: TenantContext = Depends(get_tenant_context),
    ) -> TenantContext:
        require_role(tenant, *allowed)
        return tenant

    # Name it for readable dependency-graph output in tests and OpenAPI errors.
    _dependency.__name__ = f"require_roles_{'_or_'.join(allowed)}"
    return _dependency
