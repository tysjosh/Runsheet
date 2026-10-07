"""Staff portal-user administration (design §1.7, D3; R1).

Routes live under ``/api/commerce/customers/{customer_id}/portal-users``, so
every one of them is a staff route outside the customer allowlist by
construction: a ``customer`` session is refused by the central deny before
any of this runs.

``require_portal_admin`` runs, in order: the portal flag (404
``PORTAL_DISABLED``), ``get_tenant_context``, the exact ``admin`` role (403
``INSUFFICIENT_ROLE``), then ``CustomerService.get`` (404, nothing written).
The ``portal_audit`` line comes from ``PortalAuditMiddleware``; the handler
names are the audit actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import APIRouter, Depends, Request, Response

from auth.authorization import require_role
from config.settings import get_settings
from errors.exceptions import portal_disabled, portal_unavailable
from ops.middleware.tenant_guard import TenantContext, get_tenant_context
from portal.models import (
    PortalUserInviteRequest,
    PortalUserInviteResponse,
    PortalUserLinkResponse,
    PortalUserListResponse,
    PortalUserRevokeResponse,
)
from portal.scope import portal_enabled
from portal.services.portal_access_service import (
    PortalAccessService,
    get_portal_access_service,
)

router = APIRouter(
    prefix="/api/commerce/customers/{customer_id}/portal-users",
    tags=["portal-admin"],
)


@dataclass(frozen=True)
class PortalAdminTarget:
    """The admin caller, the resolved customer, and the service."""

    tenant: TenantContext
    customer_id: str
    customer: Mapping[str, Any]
    service: PortalAccessService


async def require_portal_admin(
    customer_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> PortalAdminTarget:
    """Flag -> admin role -> customer exists in the caller's tenant."""
    if not portal_enabled(get_settings()):
        raise portal_disabled()
    require_role(tenant, "admin")
    service = get_portal_access_service()
    if service is None:
        raise portal_unavailable()
    customer = await service.get_customer(tenant.tenant_id, customer_id)
    return PortalAdminTarget(
        tenant=tenant, customer_id=customer_id, customer=customer, service=service
    )


@router.get("", response_model=PortalUserListResponse)
async def portal_user_list(
    target: PortalAdminTarget = Depends(require_portal_admin),
) -> PortalUserListResponse:
    """Invited, active and revoked portal users of the customer, newest first."""
    return await target.service.list(
        target.tenant, target.customer_id, customer=target.customer
    )


@router.post("", response_model=PortalUserInviteResponse, status_code=201)
async def portal_user_invite(
    request: Request,
    response: Response,
    body: PortalUserInviteRequest,
    target: PortalAdminTarget = Depends(require_portal_admin),
) -> PortalUserInviteResponse:
    """Invite a portal user; 200 with ``already_invited`` when already active."""
    result = await target.service.invite(
        target.tenant, target.customer_id, body.email, customer=target.customer
    )
    if result.already_invited:
        response.status_code = 200
    # The grant id isn't a path param here; hand it to the audit line.
    request.state.portal_audit_target_ids = {"grant_id": result.grant_id}
    return result


@router.post("/{grant_id}/resend", response_model=PortalUserLinkResponse)
async def portal_user_resend(
    grant_id: str,
    target: PortalAdminTarget = Depends(require_portal_admin),
) -> PortalUserLinkResponse:
    """Mint a new password-set link and re-send the invite email."""
    return await target.service.resend(
        target.tenant, target.customer_id, grant_id, customer=target.customer
    )


@router.delete("/{grant_id}", response_model=PortalUserRevokeResponse)
async def portal_user_revoke(
    grant_id: str,
    target: PortalAdminTarget = Depends(require_portal_admin),
) -> PortalUserRevokeResponse:
    """Revoke the portal user: the identity is deleted, the grant kept (D10)."""
    return await target.service.revoke(
        target.tenant, target.customer_id, grant_id, customer=target.customer
    )


__all__ = [
    "PortalAdminTarget",
    "portal_user_invite",
    "portal_user_list",
    "portal_user_resend",
    "portal_user_revoke",
    "require_portal_admin",
    "router",
]
