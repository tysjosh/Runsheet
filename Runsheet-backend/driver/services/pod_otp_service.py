"""
``PODOTPService`` — the proof-of-delivery one-time code, generated at dispatch.

This module binds the Glossary name ``POD_OTP_Service``. It owns three things
and nothing else:

1. **Generation at dispatch.** :meth:`PODOTPService.on_order_dispatched` is
   registered on ``OrderService.subscribe("order.dispatched", ...)`` in
   ``bootstrap/fuel.py`` — the same public subscription helper the invoice and
   K-factor subscribers use. It returns immediately when the tenant policy
   value ``otp_required`` is false, which is the default in every tenant
   (R5.31).
2. **Persistence on the order document** under ``pod_otp`` — the first key
   :func:`driver.services.pod_service.extract_expected_otp` reads — together
   with ``pod_otp_generated_at`` (R5.25).
3. **One delivery to the customer** through ``Notification_Pipeline`` with
   event type ``pod_otp``, rendered from the ``pod_otp`` ``DEFAULT_TEMPLATES``
   entries and dispatched on ``sms`` and ``email`` (R5.27). There is no second
   delivery mechanism.

The validity window (R5.28–R5.30) is *evaluated at submission*, not at
generation, so the rule lives here as two pure helpers —
:func:`otp_validity_end` and :func:`assert_otp_window_open` — which
``PODSubmissionService`` calls once it verifies the code. Keeping the rule in
this module means the generator and the validator can never disagree about
what "valid" means.

R5.26 is honoured by omission: neither the code nor any value derived from it
is ever logged here, and this service writes to no response body. The
``/api/driver`` surface strips ``pod_otp`` in
:class:`driver.services.work_service.DriverWorkService` before serialization,
and this service deliberately does **not** mutate the order dict it is handed,
so the transition response the dispatcher receives cannot carry the code
either.

Design: see ``.kiro/specs/driver-mobile-app/design.md``
§"POD OTP generation at `dispatch`".

Validates: Requirements 5.25, 5.26, 5.27, 5.28, 5.29, 5.30, 5.31
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from errors.exceptions import otp_window_expired
from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX
from scheduling.services.scheduling_es_mappings import TENANT_JOB_POLICIES_INDEX
from services.time_utils import utcnow

logger = logging.getLogger(__name__)

#: Number of decimal digits in a generated code. Six digits is what the
#: customer is asked to read back to the driver.
POD_OTP_DIGITS: int = 6

#: Validity of a generated code when the order carries no
#: ``delivery_window_end`` (R5.30).
POD_OTP_DEFAULT_VALIDITY_HOURS: int = 24

#: The order-document field the code is persisted under. It is the first key
#: ``extract_expected_otp`` reads, so no reader change is needed.
POD_OTP_FIELD: str = "pod_otp"

#: The order-document field carrying the generation instant the validity
#: window is measured from.
POD_OTP_GENERATED_AT_FIELD: str = "pod_otp_generated_at"

#: The ``Notification_Pipeline`` event type. A **new** dispatch-time entry, not
#: a reuse of the completion-time ``delivery_confirmation`` body.
POD_OTP_EVENT_TYPE: str = "pod_otp"


# ---------------------------------------------------------------------------
# Validity window — pure helpers, evaluated at submission (R5.28-R5.30)
# ---------------------------------------------------------------------------


def _parse_instant(value: Any) -> Optional[datetime]:
    """Coerce an ES date value to a timezone-aware UTC ``datetime``.

    Accepts a ``datetime`` (naive values are read as UTC) or an ISO-8601
    string, including the trailing-``Z`` form Elasticsearch returns. Anything
    unparseable is ``None`` so callers can degrade rather than raise.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def otp_validity_end(work_doc: Optional[dict]) -> Optional[datetime]:
    """Return the instant the provisioned code stops being valid.

    The window runs from ``pod_otp_generated_at`` through the order's
    ``delivery_window_end`` (R5.28), or 24 hours from generation when the order
    carries no window (R5.30). A code the customer received hours in advance is
    therefore still usable when the driver arrives.

    Returns ``None`` when neither bound can be established — no
    ``delivery_window_end`` and no parseable ``pod_otp_generated_at``. There is
    then no window to enforce, and the fail-closed
    ``OTP_NOT_PROVISIONED`` / ``OTP_VERIFICATION_FAILED`` checks in
    ``PODSubmissionService`` remain the control.
    """
    if not work_doc:
        return None

    window_end = _parse_instant(work_doc.get("delivery_window_end"))
    if window_end is not None:
        return window_end

    generated_at = _parse_instant(work_doc.get(POD_OTP_GENERATED_AT_FIELD))
    if generated_at is None:
        return None
    return generated_at + timedelta(hours=POD_OTP_DEFAULT_VALIDITY_HOURS)


def assert_otp_window_open(
    work_doc: Optional[dict], *, now: Optional[datetime] = None
) -> None:
    """Raise ``OTP_WINDOW_EXPIRED`` when the provisioned code has lapsed.

    HTTP 409 with the order's ``delivery_window_end`` in ``details`` (R5.29),
    so the offline queue can classify the submission from the status code and
    Driver_App can tell the driver why. Neither the submitted nor the expected
    code appears in the exception or in any log line.
    """
    validity_end = otp_validity_end(work_doc)
    if validity_end is None:
        return

    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    if moment <= validity_end:
        return

    raw_window_end = (work_doc or {}).get("delivery_window_end")
    details: dict[str, Any] = {
        "delivery_window_end": (
            raw_window_end
            if isinstance(raw_window_end, str) or raw_window_end is None
            else _iso(raw_window_end)
        ),
        "otp_valid_until": _iso(validity_end),
    }
    raise otp_window_expired(details=details)


def _iso(value: Any) -> Optional[str]:
    """Render a datetime as ISO-8601, passing through non-datetimes."""
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class PODOTPService:
    """Generate, persist, and deliver the POD one-time code at dispatch.

    Collaborators arrive through the constructor, matching the wiring pattern
    the rest of the driver surface uses. ``notification_service`` is the one
    exception: ``Notification_Pipeline`` is registered by
    ``bootstrap/notifications.py``, which runs *after* ``fuel`` in
    ``_BOOT_ORDER``, so it is injected later by
    :meth:`set_notification_service` from ``bootstrap/driver.py``.

    Every failure degrades rather than propagating: ``OrderService`` swallows
    subscriber exceptions, so raising here would only hide the cause. A
    persistence failure suppresses the notification as well — a customer
    holding a code the server cannot verify is worse than no code, because the
    fail-closed posture in ``PODSubmissionService`` rejects the submission
    either way.
    """

    def __init__(
        self,
        *,
        es_service: Any,
        notification_service: Any = None,
        clock: Any = None,
    ) -> None:
        self._es = es_service
        self._notification_service = notification_service
        self._clock = clock or utcnow

    # -- wiring ---------------------------------------------------------

    def set_notification_service(self, notification_service: Any) -> None:
        """Inject ``Notification_Pipeline`` after construction.

        Called from ``bootstrap/driver.py``, the first module that runs after
        ``bootstrap/notifications.py`` has put the service on the container.
        """
        self._notification_service = notification_service

    # -- the hook -------------------------------------------------------

    async def on_order_dispatched(self, order: dict) -> None:
        """Handle ``order.dispatched``: provision the code when required.

        Registered by ``bootstrap/fuel.py`` on
        ``OrderService.subscribe("order.dispatched", ...)``. Called after the
        transition is persisted and broadcast.

        Validates: Requirements 5.25, 5.27, 5.31
        """
        if not isinstance(order, dict):
            return

        tenant_id = order.get("tenant_id") or ""
        order_id = order.get("order_id") or ""
        if not tenant_id or not order_id:
            logger.warning(
                "PODOTPService: order.dispatched without tenant_id/order_id "
                "— no code provisioned"
            )
            return

        policies = await self._get_tenant_policies(tenant_id)
        if not policies.get("otp_required", False):
            # The default in every tenant (R5.31). Nothing is generated,
            # nothing is persisted, and no notification is submitted.
            return

        existing = order.get(POD_OTP_FIELD)
        if existing is not None and str(existing).strip():
            # A re-dispatch must not invalidate the code the customer already
            # holds, so an order that already carries one keeps it.
            logger.info(
                "PODOTPService: order=%s tenant=%s already carries a "
                "provisioned code — leaving it in place",
                order_id,
                tenant_id,
            )
            return

        code = self._generate_code()
        generated_at = self._now()

        persisted = await self._persist(
            tenant_id=tenant_id,
            order_id=order_id,
            code=code,
            generated_at=generated_at,
        )
        if not persisted:
            # No notification: a code the server cannot verify is worse than
            # no code at all.
            return

        await self._notify(
            order=order,
            tenant_id=tenant_id,
            order_id=order_id,
            code=code,
            generated_at=generated_at,
        )

    # -- generation -----------------------------------------------------

    def _generate_code(self) -> str:
        """Return a zero-padded 6-digit code from the ``secrets`` CSPRNG."""
        upper_bound = 10 ** POD_OTP_DIGITS
        return str(secrets.randbelow(upper_bound)).zfill(POD_OTP_DIGITS)

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    # -- persistence ----------------------------------------------------

    async def _persist(
        self,
        *,
        tenant_id: str,
        order_id: str,
        code: str,
        generated_at: datetime,
    ) -> bool:
        """Write the code onto the order document. ``True`` when it landed.

        A partial ES update rather than a repository round-trip: the scripted
        upsert in ``FuelOrderRepository.upsert_with_last_event_timestamp``
        no-ops on an equal ``last_event_timestamp``, and the transition that
        triggered this hook has just written that exact value — so the upsert
        path would silently discard the code. The Postgres source-of-truth is
        kept in step through ``mirror_current_state_fields``, which is the
        partial-update mirror for precisely this case: reads served from
        Postgres after cutover must see the code the submission verifies
        against.
        """
        if self._es is None:
            logger.error(
                "PODOTPService: no es_service — cannot provision a code for "
                "order=%s tenant=%s",
                order_id,
                tenant_id,
            )
            return False

        fields = {
            POD_OTP_FIELD: code,
            POD_OTP_GENERATED_AT_FIELD: generated_at.isoformat(),
        }

        try:
            await self._es.update_document(
                FUEL_ORDERS_CURRENT_INDEX, order_id, fields
            )
        except Exception as exc:
            logger.error(
                "PODOTPService: failed to persist the code for order=%s "
                "tenant=%s: %s",
                order_id,
                tenant_id,
                exc,
            )
            return False

        try:
            from commerce.services.commerce_persistence_bridge import (
                mirror_current_state_fields,
            )

            await mirror_current_state_fields(
                "fuel_order", tenant_id, order_id, fields
            )
        except Exception as exc:  # noqa: BLE001 - mirror is best-effort
            logger.warning(
                "PODOTPService: Postgres mirror of the provisioned code "
                "failed for order=%s tenant=%s: %s",
                order_id,
                tenant_id,
                exc,
            )

        logger.info(
            "PODOTPService: provisioned a delivery code for order=%s "
            "tenant=%s (code not logged)",
            order_id,
            tenant_id,
        )
        return True

    # -- delivery -------------------------------------------------------

    async def _notify(
        self,
        *,
        order: dict,
        tenant_id: str,
        order_id: str,
        code: str,
        generated_at: datetime,
    ) -> None:
        """Submit exactly one ``pod_otp`` notification (R5.27)."""
        if self._notification_service is None:
            logger.warning(
                "PODOTPService: no notification_service — the code for "
                "order=%s tenant=%s was persisted but not delivered",
                order_id,
                tenant_id,
            )
            return

        customer_id = order.get("customer_id") or ""
        if not customer_id:
            logger.warning(
                "PODOTPService: order=%s tenant=%s carries no customer_id — "
                "the code cannot be delivered",
                order_id,
                tenant_id,
            )
            return

        valid_until = otp_validity_end(
            {
                "delivery_window_end": order.get("delivery_window_end"),
                POD_OTP_GENERATED_AT_FIELD: generated_at,
            }
        )

        event_data = {
            "customer_id": customer_id,
            "customer_name": order.get("customer_name") or "Valued Customer",
            "order_id": order_id,
            "otp_code": code,
            "valid_until": _iso(valid_until) or "",
        }

        try:
            await self._notification_service.notify_event(
                event_type=POD_OTP_EVENT_TYPE,
                event_data=event_data,
                tenant_id=tenant_id,
            )
            logger.info(
                "PODOTPService: submitted the pod_otp notification for "
                "order=%s tenant=%s",
                order_id,
                tenant_id,
            )
        except Exception as exc:
            logger.error(
                "PODOTPService: pod_otp notification failed for order=%s "
                "tenant=%s: %s",
                order_id,
                tenant_id,
                exc,
            )

    # -- policies -------------------------------------------------------

    async def _get_tenant_policies(self, tenant_id: str) -> dict:
        """Read ``tenant_job_policies``, defaulting ``otp_required`` to false.

        Same read and same default as
        ``PODSubmissionService._get_tenant_policies`` (R5.31), so generation
        and verification agree on whether a tenant requires a code.
        """
        defaults = {"otp_required": False}
        if self._es is None:
            return defaults

        try:
            response = await self._es.search_documents(
                TENANT_JOB_POLICIES_INDEX,
                {"query": {"term": {"tenant_id": tenant_id}}, "size": 1},
                size=1,
            )
            hits = (response or {}).get("hits", {}).get("hits", [])
            if hits:
                source = hits[0].get("_source", {}) or {}
                return {
                    "otp_required": source.get(
                        "otp_required", defaults["otp_required"]
                    )
                }
        except Exception as exc:
            logger.warning(
                "PODOTPService: failed to read tenant policies for %s, "
                "using defaults: %s",
                tenant_id,
                exc,
            )
        return defaults
