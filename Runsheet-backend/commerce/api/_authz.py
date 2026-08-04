"""Who may reach the commerce surface.

Before this module the nine routers under ``commerce/api/`` performed **no**
role check at all: every handler resolved a ``TenantContext`` and went straight
to its service, so any authenticated member of a tenant — including a
``driver`` — could read the tenant's receivables aging, account roster, price
books, and payment records. Tenant isolation held; role isolation did not exist.

The policy lives here rather than being repeated per router so that "who may see
pricing" has one answer, and so a router added later has an obvious thing to
depend on. It deliberately mirrors the tiers in
``runsheet/src/config/modules.ts``: a UI that hides a control while the API
serves it to anyone is the worse of the two failure modes, because it looks
enforced.

Two audiences:

* :data:`COMMERCE_OPS_ROLES` — customers and invoices. A dispatcher works these
  during a shift; they are capabilities 6 and 7 of the delivery pipeline.
* :data:`COMMERCE_STAFF_ROLES` — accounts, AR aging, price books, pricing rules,
  price-protection contracts, payments. The customer's ERP is the authoritative
  price and invoice, so a tenant's own ``admin`` gets no second editable copy
  here. Runsheet staff retain access to diagnose and to run a tenant that has no
  ERP.

``platform_admin`` implies nothing, exactly as in
:func:`auth.authorization.require_role`, so a staff account must hold an
operations role alongside it to reach a hub that renders these as tabs. See
:data:`auth.supertokens_init.PLATFORM_STAFF_ROLES`.

Ordering note: every caller applies the feature-flag check *before* the role
check, so a tenant without the commerce backbone still gets 404 rather than 403.
A disabled feature should stay invisible instead of advertising that it exists
and is merely forbidden.
"""

from __future__ import annotations

from auth.authorization import require_role
from auth.router_guards import roles_dependency
from ops.middleware.tenant_guard import TenantContext

#: Operations roles: the commerce surfaces a dispatcher uses to run deliveries.
COMMERCE_OPS_ROLES: tuple[str, ...] = ("admin", "dispatcher")

#: Runsheet-staff role for the pricing and billing surfaces the ERP owns.
COMMERCE_STAFF_ROLES: tuple[str, ...] = ("platform_admin",)


def require_commerce_ops(tenant: TenantContext) -> None:
    """Require an operations role for a customer/invoice surface.

    Raises:
        AppException: ``insufficient_role`` (HTTP 403) when the caller holds
            neither ``admin`` nor ``dispatcher``.
    """
    require_role(tenant, *COMMERCE_OPS_ROLES)


def require_commerce_staff(tenant: TenantContext) -> None:
    """Require ``platform_admin`` for a pricing/billing surface.

    A tenant ``admin`` is refused here on purpose: the ERP holds the
    authoritative price and invoice.

    Raises:
        AppException: ``insufficient_role`` (HTTP 403) when the caller does not
            hold ``platform_admin``.
    """
    require_role(tenant, *COMMERCE_STAFF_ROLES)


# ---------------------------------------------------------------------------
# Router-level dependency
# ---------------------------------------------------------------------------


#: Router-level staff gate for routers with no shared feature-flag guard.
#:
#: ``pricing_endpoints`` and ``price_protection_endpoints`` resolve
#: ``get_tenant_context`` per handler rather than sharing a ``require_*_enabled``
#: dependency, so there is no single function to add the role check to. Attaching
#: this to the ``APIRouter`` covers every route the router declares, including ones
#: added later — the property that matters, since the failure mode being fixed here
#: is precisely a handler that quietly has no gate.
#:
#: Built by the shared factory in :mod:`auth.router_guards` so this package and
#: ``compliance/api`` use one mechanism rather than two.
commerce_staff_dependency = roles_dependency(*COMMERCE_STAFF_ROLES)
