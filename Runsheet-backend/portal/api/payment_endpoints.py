"""Portal ACH payment routes (design §3, §6.2; R6).

* ``POST /api/portal/invoices/{invoice_id}/payments`` — start (or replay) a
  payment (payment limit). Header ``Idempotency-Key`` required.
* ``GET /api/portal/payment-attempts/{payment_attempt_id}`` — attempt status
  for polling after confirm (read limit). ``include_client_secret=true``
  returns the secret to the attempt's own user while it is ``created``.

Both depend on :func:`~portal.api.invoice_endpoints.require_portal_invoicing`
(404 ``INVOICING_DISABLED``). Data comes only through ``portal.services``;
this module imports no store.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from middleware.rate_limiter import limiter
from portal.api._authz import (
    PORTAL_PAYMENT_LIMIT,
    PORTAL_READ_LIMIT,
    PORTAL_READ_SCOPE,
    PortalScope,
    portal_rate_key,
    reject_scope_params,
)
from portal.api.invoice_endpoints import require_portal_invoicing
from portal.models import (
    PortalPaymentAttempt,
    PortalPaymentAttemptEnvelope,
    PortalPaymentCreated,
    PortalPaymentCreatedEnvelope,
    PortalPaymentRequest,
    is_path_id,
)
from portal.services.portal_payment_service import (
    get_portal_payment_service,
    payment_attempt_not_found,
    validate_idempotency_key,
)
from portal.services.projection import payment_attempt_label
from portal.services.scoped_readers import invoice_not_found

router = APIRouter(
    prefix="/api/portal",
    tags=["portal"],
    dependencies=[Depends(reject_scope_params)],
)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=PortalPaymentCreatedEnvelope,
    status_code=201,
)
@limiter.limit(PORTAL_PAYMENT_LIMIT, key_func=portal_rate_key)
async def create_portal_payment(
    request: Request,
    invoice_id: str,
    body: Optional[PortalPaymentRequest] = None,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    scope: PortalScope = Depends(require_portal_invoicing),
) -> JSONResponse:
    """201 with the new attempt and its client secret; 200 for a replay of
    the same ``Idempotency-Key`` (R6.1-R6.6)."""
    key = validate_idempotency_key(idempotency_key)
    if not is_path_id(invoice_id):
        raise invoice_not_found(invoice_id)
    result = await get_portal_payment_service().create(
        scope,
        invoice_id,
        idempotency_key=key,
        amount_cents=body.amount_cents if body is not None else None,
    )
    attempt = result.attempt
    request.state.portal_audit_target_ids = {"payment_attempt_id": attempt["payment_attempt_id"]}
    envelope = PortalPaymentCreatedEnvelope(
        data=PortalPaymentCreated(
            payment_attempt_id=attempt["payment_attempt_id"],
            status_code=attempt["status"],
            amount_cents=int(attempt["amount_cents"]),
            client_secret=result.client_secret,
            publishable_key=result.publishable_key,
        ),
        request_id=_request_id(request),
    )
    headers = {}
    if result.retry_after is not None:
        headers["Retry-After"] = str(result.retry_after)
    return JSONResponse(
        status_code=result.status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


@router.get(
    "/payment-attempts/{payment_attempt_id}",
    response_model=PortalPaymentAttemptEnvelope,
)
@limiter.shared_limit(PORTAL_READ_LIMIT, scope=PORTAL_READ_SCOPE, key_func=portal_rate_key)
async def get_portal_payment_attempt(
    request: Request,
    payment_attempt_id: str,
    include_client_secret: Optional[str] = Query(default=None, pattern="^(true|false)$"),
    scope: PortalScope = Depends(require_portal_invoicing),
) -> PortalPaymentAttemptEnvelope:
    """The attempt's status. Polling never calls Stripe; only
    ``include_client_secret=true`` does, for the attempt's own user (§6.2)."""
    if not is_path_id(payment_attempt_id):
        raise payment_attempt_not_found(payment_attempt_id)
    view = await get_portal_payment_service().get_attempt(
        scope, payment_attempt_id, include_client_secret=include_client_secret == "true"
    )
    row = view.attempt
    return PortalPaymentAttemptEnvelope(
        data=PortalPaymentAttempt(
            payment_attempt_id=row["payment_attempt_id"],
            invoice_id=row["invoice_id"],
            status_code=row["status"],
            status_label=payment_attempt_label(row["status"]),
            amount_cents=int(row["amount_cents"]),
            created_at=row.get("created_at"),
            client_secret=view.client_secret,
            publishable_key=view.publishable_key,
        ),
        request_id=_request_id(request),
    )


__all__ = ["router"]
