"""
REST endpoints for the Stripe Connector (Task 9.8, Req 5.5.*).

Exposes two route surfaces:

* ``GET /api/integrations/stripe/public-config`` — tenant-scoped,
  authenticated via the JWT tenant guard. Returns the tenant's
  ``publishable_key`` so the frontend can construct a Stripe.js
  client-side checkout flow. The secret_key and webhook_secret are
  NEVER returned here (Req 5.5.2, 5.1.8).

* ``POST /webhooks/stripe/{tenant_id}`` — unauthenticated (no JWT).
  Stripe signs every webhook payload, and the connector's
  :meth:`verify_webhook_signature` authenticates the request against
  the per-tenant webhook_secret (Req 5.5.4). The tenant_id comes from
  the URL path rather than a JWT claim because webhooks don't carry
  one. Returns HTTP 400 on an invalid signature, 200 on success, and
  404 when no Stripe integration instance is configured for the
  ``tenant_id``.

Both routes are configured through :func:`configure_stripe_endpoints`
at bootstrap time (mirroring the pattern in
:mod:`integrations.api.integrations_endpoints`) so this module never
touches module-global Elasticsearch or AWS state at import time.

Two separate :class:`APIRouter` instances are exported:

    * :data:`router` — mounted at prefix ``/api/integrations/stripe``
      and protected by the tenant guard. Hosts the public-config
      endpoint.
    * :data:`webhook_router` — mounted at the app root (no prefix)
      and unauthenticated. Hosts the webhook endpoint. The path
      ``/webhooks/stripe/{tenant_id}`` is deliberate: Stripe dashboard
      wiring prescribes the full URL per tenant, so the path includes
      the tenant id rather than deriving it from an authorization
      header.

Validates: Requirements 5.5.1, 5.5.2, 5.5.4, 5.5.7.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict

from integrations.stripe_connector import (
    StripeConnector,
    StripeSignatureVerificationError,
)
from ops.middleware.tenant_guard import TenantContext, get_tenant_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

#: Tenant-scoped router mounted at ``/api/integrations/stripe``. Hosts
#: the ``public-config`` endpoint which requires a verified JWT.
router = APIRouter(prefix="/api/integrations/stripe", tags=["stripe"])

#: Unauthenticated router hosting the ``/webhooks/stripe/{tenant_id}``
#: endpoint. Mounted at the app root so the full webhook URL matches
#: the value the tenant configures in their Stripe dashboard.
webhook_router = APIRouter(tags=["stripe"])

# Auth policy marker — consumed by the bootstrap linter to confirm
# this router is NOT behind the tenant guard. Stripe webhooks carry
# a signed ``Stripe-Signature`` header and the connector authenticates
# the request on its own (Req 5.5.4).
WEBHOOK_ROUTER_AUTH_POLICY = "webhook_signature"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


#: A callable that returns a :class:`StripeConnector` configured for
#: the supplied ``tenant_id`` (or ``None`` when the tenant has not
#: connected Stripe). Bootstrap wires this factory so the endpoints
#: never hard-code the construction path. Tests pass an in-memory stub.
ConnectorFactory = Callable[[str], Awaitable[Optional[StripeConnector]]]


# ---------------------------------------------------------------------------
# Module-level wiring (same pattern as integrations_endpoints.py)
# ---------------------------------------------------------------------------

_connector_factory: Optional[ConnectorFactory] = None


def configure_stripe_endpoints(
    *,
    connector_factory: ConnectorFactory,
) -> None:
    """Wire the Stripe connector factory into the REST routers.

    Called once during application startup. Tests inject a factory
    that returns a :class:`StripeConnector` backed by in-memory fakes
    so the routers can be exercised without ES / KMS / Stripe SDK.

    Args:
        connector_factory: ``async def (tenant_id) -> Optional[
            StripeConnector]``. Should return ``None`` when the tenant
            has not yet completed the Stripe connect flow; the
            handlers then return HTTP 404 so clients uniformly see
            "this tenant has no Stripe integration" rather than a 500.
    """

    global _connector_factory
    if connector_factory is None:
        raise ValueError("connector_factory must not be None")
    _connector_factory = connector_factory


def _get_connector_factory() -> ConnectorFactory:
    if _connector_factory is None:
        raise RuntimeError(
            "Stripe endpoints not configured. Call "
            "configure_stripe_endpoints() during startup."
        )
    return _connector_factory


async def _resolve_connector_or_404(tenant_id: str) -> StripeConnector:
    factory = _get_connector_factory()
    connector: Optional[StripeConnector]
    try:
        connector = await factory(tenant_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "StripeConnector factory failed tenant=%s: %s", tenant_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "stripe_connector_unavailable",
                "message": "Stripe connector factory raised an error.",
            },
        )
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "stripe_integration_not_configured",
                "message": (
                    "No active Stripe integration for this tenant. "
                    "Connect Stripe from the Integration Marketplace first."
                ),
            },
        )
    return connector


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class StripePublicConfigResponse(BaseModel):
    """Response body for ``GET /api/integrations/stripe/public-config``.

    Carries ONLY the ``publishable_key`` — the secret_key and
    webhook_secret never appear here (Req 5.5.1, 5.1.8). The
    ``extra="forbid"`` constraint is a belt-and-braces check so a
    future refactor that tries to add a secret field here trips a
    :class:`pydantic.ValidationError` at test time rather than at
    customer-disclosure time.
    """

    model_config = ConfigDict(extra="forbid")

    publishable_key: str


class StripeWebhookResponse(BaseModel):
    """Minimal 200-response for ``POST /webhooks/stripe/{tenant_id}``.

    Stripe does not inspect the body — a 200 status is enough for it
    to stop retrying. We return a small JSON summary so the test
    suite can assert the connector's dispatch decision without
    reading through logs.
    """

    model_config = ConfigDict(extra="forbid")

    received: bool = True
    handled: bool = False
    event_type: Optional[str] = None
    reason: Optional[str] = None


class StripePaymentItem(BaseModel):
    """Single redacted PaymentIntent row returned by ``/payments``.

    ``extra="forbid"`` is a belt-and-braces safety net: a future
    refactor that adds a raw Stripe field here would trip a
    :class:`pydantic.ValidationError` at test time rather than
    leaking PII to the UI (Req 5.1.8).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None
    created: Optional[int] = None
    customer: Optional[str] = None
    description: Optional[str] = None
    metadata: Dict[str, str] = {}


class StripePaymentsListResponse(BaseModel):
    """Response body for ``GET /api/integrations/stripe/payments``."""

    model_config = ConfigDict(extra="forbid")

    items: List[StripePaymentItem]
    has_more: bool
    next_starting_after: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /api/integrations/stripe/public-config (Req 5.5.2)
# ---------------------------------------------------------------------------


@router.get("/public-config", response_model=StripePublicConfigResponse)
async def get_public_config(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> StripePublicConfigResponse:
    """Return the tenant's Stripe ``publishable_key``.

    Tenant-scoped via the tenant guard. Returns HTTP 404 when no
    Stripe integration has been configured for the tenant, HTTP 500
    when the stored envelope is missing a publishable_key (indicating
    a broken record — the operator should disconnect + reconnect).

    Validates: Requirement 5.5.2, 5.1.8.
    """

    connector = await _resolve_connector_or_404(tenant.tenant_id)
    try:
        publishable_key = await connector.get_publishable_key()
    except RuntimeError as exc:
        logger.error(
            "Stripe public-config: envelope is missing publishable_key "
            "tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "stripe_envelope_corrupt",
                "message": (
                    "Stripe integration envelope is missing the "
                    "publishable_key. Disconnect and reconnect the "
                    "integration."
                ),
            },
        )
    return StripePublicConfigResponse(publishable_key=publishable_key)


# ---------------------------------------------------------------------------
# GET /api/integrations/stripe/payments (Req 5.5.6)
# ---------------------------------------------------------------------------


def _parse_iso8601_timestamp(raw: Optional[str], *, field_name: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp query parameter.

    Accepts the "Z" suffix for UTC (common from JS ``Date.toISOString()``)
    and bare offset forms. Naive datetimes are treated as UTC to keep the
    Stripe ``created`` epoch conversion deterministic across deployments.
    Raises :class:`HTTPException` 400 on invalid input.
    """

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_timestamp",
                "message": (
                    f"{field_name} must be an ISO-8601 timestamp "
                    f"(got {raw!r}): {exc}"
                ),
            },
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("/payments", response_model=StripePaymentsListResponse)
async def list_payments(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    limit: int = Query(
        10,
        ge=1,
        description=(
            "Maximum number of PaymentIntents to return. Values above "
            "100 are clamped to 100 to match Stripe's own upper bound."
        ),
    ),
    starting_after: Optional[str] = Query(
        default=None,
        description=(
            "Stripe pagination cursor — the id of the PaymentIntent "
            "immediately before the page you want. Pass the "
            "``next_starting_after`` value from a prior response."
        ),
    ),
    created_gte: Optional[str] = Query(
        default=None,
        alias="created.gte",
        description="Lower bound on PaymentIntent.created (ISO-8601).",
    ),
    created_lte: Optional[str] = Query(
        default=None,
        alias="created.lte",
        description="Upper bound on PaymentIntent.created (ISO-8601).",
    ),
) -> StripePaymentsListResponse:
    """Return a paginated, redacted page of recent Stripe PaymentIntents.

    Tenant-scoped via :func:`get_tenant_context` — the connector
    factory is called with ``tenant.tenant_id`` so every request
    reads from the issuer tenant's own Stripe credentials. Returns
    HTTP 404 when no Stripe integration is configured, 400 on an
    unparseable timestamp, and 503 when the Stripe SDK call fails
    (so the Integration_Marketplace UI can surface a retry hint
    rather than a generic 500).

    The endpoint never returns raw PaymentIntent fields: the
    connector's :meth:`StripeConnector.list_payments` reduces each
    item to the operator-safe subset via ``_redact_payment_intent``.
    Card numbers, ``payment_method_data``, receipt emails, and
    client secrets MUST NOT leak through (Req 5.1.8).

    Limit ``>100`` is clamped to 100 server-side; requesting
    ``limit=1000`` is allowed (it simply caps at 100) rather than
    rejected so a curious operator can't trigger a 422 by hitting
    the URL in a browser.

    Validates: Requirement 5.5.6, 5.1.8.
    """

    # Query("limit", ge=1) enforces the lower bound; cap the upper
    # bound server-side so the frontend can't bypass it.
    capped_limit = min(int(limit), 100)

    gte_dt = _parse_iso8601_timestamp(created_gte, field_name="created.gte")
    lte_dt = _parse_iso8601_timestamp(created_lte, field_name="created.lte")

    connector = await _resolve_connector_or_404(tenant.tenant_id)

    try:
        page = await connector.list_payments(
            limit=capped_limit,
            starting_after=starting_after,
            created_gte=gte_dt,
            created_lte=lte_dt,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Stripe payments list: connector call failed tenant=%s: %s",
            tenant.tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "stripe_list_payments_failed",
                "message": (
                    "Unable to list Stripe PaymentIntents for this tenant. "
                    "Retry or verify the Stripe credentials."
                ),
            },
        )

    return StripePaymentsListResponse(
        items=[StripePaymentItem(**item) for item in page.get("items", [])],
        has_more=bool(page.get("has_more", False)),
        next_starting_after=page.get("next_starting_after"),
    )


# ---------------------------------------------------------------------------
# POST /webhooks/stripe/{tenant_id} (Req 5.5.4)
# ---------------------------------------------------------------------------


@webhook_router.post(
    "/webhooks/stripe/{tenant_id}",
    response_model=StripeWebhookResponse,
)
async def receive_stripe_webhook(
    request: Request,
    tenant_id: str = Path(..., min_length=1),
) -> StripeWebhookResponse:
    """Receive a Stripe webhook and update the matching Reconciliation_Record.

    Steps:
        1. Read the raw request body. Stripe signs the payload bytes
           verbatim, so we MUST use the raw bytes — not a re-serialized
           JSON object — for signature verification.
        2. Read the ``Stripe-Signature`` header.
        3. Resolve the :class:`StripeConnector` for ``tenant_id`` (404
           when none). The connector's
           :meth:`verify_webhook_signature` loads the tenant's
           webhook_secret from the vault and delegates to
           ``stripe.Webhook.construct_event``.
        4. Dispatch the verified event to the connector's
           :meth:`handle_webhook_event` so the matching
           Reconciliation_Record is updated (Req 5.5.4).

    Returns:
        200 with a :class:`StripeWebhookResponse` on success.
        400 when the signature header is missing or verification
        fails — the response body carries the error reason so the
        operator can diagnose misconfigured secrets.
        404 when no Stripe integration instance exists for
        ``tenant_id``.

    Validates: Requirement 5.5.4.
    """

    signature_header = request.headers.get("stripe-signature")
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "missing_stripe_signature",
                "message": "Stripe-Signature header is required.",
            },
        )

    try:
        payload_bytes = await request.body()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Stripe webhook: could not read body tenant=%s: %s",
            tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_request_body",
                "message": "Failed to read the webhook request body.",
            },
        )

    connector = await _resolve_connector_or_404(tenant_id)

    try:
        event = await connector.verify_webhook_signature(
            payload_bytes, signature_header
        )
    except StripeSignatureVerificationError as exc:
        logger.warning(
            "Stripe webhook: signature verification failed tenant=%s: %s",
            tenant_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_signature",
                "message": str(exc),
            },
        )

    summary: Dict[str, Any] = {}
    try:
        summary = await connector.handle_webhook_event(event)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Stripe webhook: handler raised tenant=%s event_type=%s: %s",
            tenant_id,
            event.get("type") if isinstance(event, dict) else None,
            exc,
        )
        # Still return 200 to Stripe so it stops retrying — the
        # internal failure is logged, and a follow-up event or a manual
        # sync_pull will converge the state.
        return StripeWebhookResponse(
            received=True,
            handled=False,
            event_type=(
                str(event.get("type"))
                if isinstance(event, dict) and event.get("type") is not None
                else None
            ),
            reason=f"handler_error: {exc}",
        )

    return StripeWebhookResponse(
        received=True,
        handled=bool(summary.get("handled")),
        event_type=summary.get("event_type"),
        reason=summary.get("reason"),
    )


__all__ = [
    "ConnectorFactory",
    "StripePaymentItem",
    "StripePaymentsListResponse",
    "StripePublicConfigResponse",
    "StripeWebhookResponse",
    "WEBHOOK_ROUTER_AUTH_POLICY",
    "configure_stripe_endpoints",
    "router",
    "webhook_router",
]
