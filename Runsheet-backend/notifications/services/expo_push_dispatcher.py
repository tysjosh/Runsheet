"""
Real push dispatcher for the driver mobile app.

Extends ``ChannelDispatcher`` to deliver push notifications to a driver's
registered devices.  Credentials are read from environment variables at
init time; a ``ValueError`` is raised when the access token is absent.
The HTTP client is built lazily so the module can be imported in an
environment that has no outbound network access.

This module is the ONLY place in the backend that names the push
provider (driver-mobile-app Requirement 9.15).  Every caller depends on
the ``ChannelDispatcher`` abstraction and the channel identifier
``push`` and nothing else.

Requirements: 9.4, 9.8, 9.9, 9.10, 9.13, 9.14, 9.16, 9.17
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from driver.services.driver_es_mappings import (
    DRIVER_DEVICES_INDEX,
    DRIVER_PUSH_ATTEMPTS_INDEX,
)
from notifications.services.channel_dispatchers import ChannelDispatcher
from notifications.services.template_renderer import (
    DRIVER_PUSH_TEMPLATES,
    render_template,
)

logger = logging.getLogger(__name__)


# The only identifiers a push payload may carry (Requirement 9.8): no
# customer name, no customer phone number, no street address.  The
# allow-list — not a deny-list — is what makes the exclusion hold for
# keys a future caller has not thought of yet.
PUSH_PAYLOAD_IDENTIFIER_KEYS: tuple[str, ...] = (
    "tenant_id",
    "order_id",
    "job_id",
    "notification_id",
    "delivery_window_start",
    "delivery_window_end",
    "exception_id",
    "exception_type",
    "thread_id",
    "message_id",
)

# ``title`` / ``body`` come from a DEFAULT_TEMPLATES entry, keyed by
# (event_type, "push"), so wording changes are a template edit rather
# than a code change.
_PUSH_TEMPLATES_BY_TYPE: dict[str, dict] = {
    entry["event_type"]: entry for entry in DRIVER_PUSH_TEMPLATES
}


class ExpoPushDispatcher(ChannelDispatcher):
    """Push dispatcher backed by the Expo Push send API.

    Shape matches ``SendGridEmailDispatcher``
    (``notifications/services/sendgrid_email_dispatcher.py``):
    credentials from ``os.environ`` in ``__init__`` with a raise when
    absent (R9.17), the HTTP client built lazily, ``channel_name``
    returning the channel identifier (R9.13), and ``dispatch`` writing
    ``provider_message_id`` on success and ``failure_reason`` on failure
    onto the notification dict before returning ``'sent'`` or
    ``'failed'`` (R9.16).

    Validates: Requirements 9.4, 9.8, 9.9, 9.10, 9.13, 9.14, 9.16, 9.17
    """

    # Provider error names the dispatcher retries (R9.9).
    _RETRYABLE = frozenset(
        {"TooManyRequests", "InternalServerError", "ServiceUnavailable"}
    )
    # Provider error names that mean the stored token is dead (R9.4).
    _INVALID_TOKEN = frozenset({"DeviceNotRegistered", "InvalidCredentials"})

    # R9.9: up to 3 retries with exponentially increasing delays starting
    # at 1 second, so at most 4 attempts per device.  This ladder is
    # deliberately separate from ``RetryPipeline``'s 60 s / 120 s / 240 s
    # → DLQ flow: the fast one absorbs a rate-limit blip inside the
    # request, the slow one absorbs a provider outage across requests.
    # ``RetryPipeline`` sees only the ``'failed'`` return value and needs
    # no push-specific branch.
    _RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

    _DEFAULT_ENDPOINT = "https://exp.host/--/api/v2/push/send"

    # Transport-level status codes carry no provider error name, so they
    # are mapped onto one here — the one place allowed to interpret them.
    _STATUS_ERROR_NAMES = {
        429: "TooManyRequests",
        500: "InternalServerError",
        502: "InternalServerError",
        503: "ServiceUnavailable",
        504: "ServiceUnavailable",
    }

    _MAX_TOKEN_LENGTH = 512

    def __init__(self, es_service=None) -> None:
        self._access_token = os.environ.get("EXPO_ACCESS_TOKEN")

        if not self._access_token:
            raise ValueError(
                "Missing Expo push credentials in environment. "
                "Required: EXPO_ACCESS_TOKEN"
            )

        self._endpoint = os.environ.get(
            "EXPO_PUSH_ENDPOINT", self._DEFAULT_ENDPOINT
        )
        # Elasticsearch is optional so the dispatcher stays constructible
        # from the notifications bootstrap before the driver surface is
        # wired; without it the attempt audit degrades to a log line.
        self._es = es_service
        # Built lazily on first send — see ``_get_client``.
        self._client = None

    @property
    def channel_name(self) -> str:
        return "push"

    async def dispatch(self, notification: dict) -> str:
        """Deliver to every registered device for the notification's driver.

        Retries a retryable provider error up to 3 times with delays of
        1 s, 2 s, 4 s (R9.9).  Deletes the ``driver_devices`` record for
        any token the provider reports unregistered or invalid (R9.4).
        Writes one ``driver_push_attempts`` document per attempt (R9.10).

        Returns ``'sent'`` when at least one device accepted the message,
        else ``'failed'``.
        """
        tenant_id = notification.get("tenant_id") or ""
        driver_id = notification.get("driver_id") or ""
        notification_type = notification.get("notification_type") or "unknown"

        devices = self._resolve_devices(notification)
        if not devices:
            reason = "No registered device for driver"
            notification["failure_reason"] = reason
            logger.warning(
                "[PUSH] %s — driver %s type %s",
                reason,
                driver_id,
                notification_type,
            )
            return "failed"

        title, body = self._render(notification_type, notification)
        data = self._payload_data(notification_type, notification)

        message_ids: list[str] = []
        failures: list[str] = []

        for device_id, push_token in devices:
            message_id, reason = await self._deliver_to_device(
                tenant_id=tenant_id,
                driver_id=driver_id,
                device_id=device_id,
                push_token=push_token,
                notification_type=notification_type,
                title=title,
                body=body,
                data=data,
            )
            if message_id is not None:
                message_ids.append(message_id)
            elif reason is None:
                # Accepted but the provider returned no ticket id.
                message_ids.append("")
            else:
                failures.append(f"{device_id}: {reason}")

        if message_ids:
            provider_message_id = ",".join(mid for mid in message_ids if mid)
            if provider_message_id:
                notification["provider_message_id"] = provider_message_id
            logger.info(
                "[PUSH] Sent type %s to driver %s — %d/%d device(s)",
                notification_type,
                driver_id,
                len(message_ids),
                len(devices),
            )
            return "sent"

        notification["failure_reason"] = "; ".join(failures)
        logger.warning(
            "[PUSH] Failed type %s for driver %s: %s",
            notification_type,
            driver_id,
            notification["failure_reason"],
        )
        return "failed"

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _deliver_to_device(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        device_id: str,
        push_token: str,
        notification_type: str,
        title: str,
        body: str,
        data: dict,
    ) -> tuple[Optional[str], Optional[str]]:
        """Deliver to one device, retrying retryable provider errors.

        Returns ``(provider_message_id, None)`` on success and
        ``(None, failure_reason)`` on failure.
        """
        max_attempts = len(self._RETRY_DELAYS) + 1

        for attempt_number in range(1, max_attempts + 1):
            if not self._is_valid_token(push_token):
                reason = "Invalid push token format"
                await self._record_attempt(
                    tenant_id=tenant_id,
                    driver_id=driver_id,
                    device_id=device_id,
                    notification_type=notification_type,
                    outcome="failed",
                    provider_message_id=None,
                    failure_reason=reason,
                    attempt_number=attempt_number,
                )
                return None, reason

            message_id, error_name, reason = await self._send_once(
                push_token=push_token,
                title=title,
                body=body,
                data=data,
            )

            outcome = "sent" if reason is None else "failed"
            await self._record_attempt(
                tenant_id=tenant_id,
                driver_id=driver_id,
                device_id=device_id,
                notification_type=notification_type,
                outcome=outcome,
                provider_message_id=message_id,
                failure_reason=reason,
                attempt_number=attempt_number,
            )

            if outcome == "sent":
                return (message_id or ""), None

            if error_name in self._INVALID_TOKEN:
                # R9.4 — the registry record holding this token is dead.
                await self._prune_device(tenant_id, driver_id, device_id)
                return None, reason

            if (
                error_name in self._RETRYABLE
                and attempt_number <= len(self._RETRY_DELAYS)
            ):
                await asyncio.sleep(self._RETRY_DELAYS[attempt_number - 1])
                continue

            return None, reason

        return None, "Retries exhausted"

    async def _send_once(
        self, *, push_token: str, title: str, body: str, data: dict
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Post one message.

        Returns ``(provider_message_id, error_name, failure_reason)``
        where ``failure_reason`` is ``None`` on success.
        """
        import httpx

        payload = {
            "to": push_token,
            "title": title,
            "body": body,
            "data": data,
            "priority": "high",
            "channelId": "dispatch",
        }

        try:
            client = self._get_client()
            response = await client.post(
                self._endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            # A transport error is transient; treat it as retryable.
            return None, "ServiceUnavailable", f"Transport error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            return None, None, str(exc)

        return self._interpret_response(response)

    def _interpret_response(
        self, response
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Interpret a provider response.

        Provider error interpretation is confined to this module (R9.16);
        callers see only the return value plus ``provider_message_id`` /
        ``failure_reason`` on the notification dict.
        """
        status_code = getattr(response, "status_code", None)

        try:
            payload = response.json()
        except Exception:
            payload = None

        if not isinstance(payload, dict):
            payload = {}

        top_level_errors = payload.get("errors")
        if isinstance(top_level_errors, list) and top_level_errors:
            first = top_level_errors[0] if isinstance(top_level_errors[0], dict) else {}
            error_name = self._normalize_error_name(
                first.get("code")
            ) or self._STATUS_ERROR_NAMES.get(status_code)
            reason = first.get("message") or f"Push provider error {error_name}"
            return None, error_name, reason

        if status_code not in (200, 201, 202):
            error_name = self._STATUS_ERROR_NAMES.get(status_code)
            reason = f"Push provider returned status {status_code}"
            return None, error_name, reason

        ticket = self._first_ticket(payload)
        if ticket is None:
            return None, None, "Push provider returned no delivery ticket"

        if ticket.get("status") == "ok":
            return ticket.get("id"), None, None

        details = ticket.get("details")
        raw_error = details.get("error") if isinstance(details, dict) else None
        error_name = self._normalize_error_name(raw_error)
        reason = ticket.get("message") or f"Push provider error {error_name}"
        return None, error_name, reason

    @staticmethod
    def _first_ticket(payload: dict) -> Optional[dict]:
        tickets = payload.get("data")
        if isinstance(tickets, dict):
            return tickets
        if isinstance(tickets, list) and tickets:
            first = tickets[0]
            return first if isinstance(first, dict) else None
        return None

    @classmethod
    def _normalize_error_name(cls, raw) -> Optional[str]:
        """Normalize a provider error name to its CamelCase spelling."""
        if not isinstance(raw, str) or not raw:
            return None
        known = cls._RETRYABLE | cls._INVALID_TOKEN
        if raw in known:
            return raw
        camel = "".join(part.capitalize() for part in raw.split("_"))
        return camel if camel in known else raw

    def _is_valid_token(self, push_token) -> bool:
        """Validate token shape. Format validation lives here only (R9.16)."""
        return (
            isinstance(push_token, str)
            and bool(push_token.strip())
            and len(push_token) <= self._MAX_TOKEN_LENGTH
        )

    def _get_client(self):
        """Build the HTTP client on first use."""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        """Close the lazily-built HTTP client, if one was built."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Payload
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_devices(notification: dict) -> list[tuple[str, str]]:
        """Return ``(device_id, push_token)`` pairs for this notification.

        Accepts either a ``devices`` list of registry records or a single
        device given as ``device_id`` / ``push_token``, falling back to
        ``recipient_reference`` for the token so the dict shape stays
        compatible with the other channels.
        """
        records = notification.get("devices")
        if not isinstance(records, list):
            records = [
                {
                    "device_id": notification.get("device_id"),
                    "push_token": (
                        notification.get("push_token")
                        or notification.get("recipient_reference")
                    ),
                }
            ]

        devices: list[tuple[str, str]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            token = record.get("push_token") or record.get("recipient_reference")
            if not token:
                continue
            devices.append((record.get("device_id") or "unknown", token))
        return devices

    @staticmethod
    def _render(notification_type: str, notification: dict) -> tuple[str, str]:
        """Render ``title`` / ``body`` from the DEFAULT_TEMPLATES entry."""
        context = ExpoPushDispatcher._identifier_context(notification)
        template = _PUSH_TEMPLATES_BY_TYPE.get(notification_type)

        if template is None:
            # No push template for this event type — fall back to
            # identifiers only, never to a customer-facing body.
            order_id = context.get("order_id")
            title = "Runsheet"
            body = f"Order {order_id}" if order_id else "Open Runsheet for details"
            return title, body

        title = render_template(template.get("subject_template") or "", context)
        body = render_template(template.get("body_template") or "", context)
        return title, body

    @staticmethod
    def _payload_data(notification_type: str, notification: dict) -> dict:
        """Build the ``data`` block — identifiers only (R9.8)."""
        data = ExpoPushDispatcher._identifier_context(notification)
        data["type"] = notification_type
        return data

    @staticmethod
    def _identifier_context(notification: dict) -> dict:
        extra = notification.get("push_data")
        source = dict(notification)
        if isinstance(extra, dict):
            source.update(extra)

        return {
            key: source[key]
            for key in PUSH_PAYLOAD_IDENTIFIER_KEYS
            if source.get(key) not in (None, "")
        }

    # ------------------------------------------------------------------
    # Audit and registry pruning
    # ------------------------------------------------------------------

    async def _record_attempt(
        self,
        *,
        tenant_id: str,
        driver_id: str,
        device_id: str,
        notification_type: str,
        outcome: str,
        provider_message_id: Optional[str],
        failure_reason: Optional[str],
        attempt_number: int,
    ) -> None:
        """Write one ``driver_push_attempts`` document per attempt (R9.10).

        Carries no message body and no customer identifier, for the same
        reason the push payload carries none (R9.8).  An audit failure
        never changes the delivery outcome.
        """
        attempt_id = str(uuid.uuid4())
        document = {
            "attempt_id": attempt_id,
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "device_id": device_id,
            "notification_type": notification_type,
            "outcome": outcome,
            "provider_message_id": provider_message_id,
            "failure_reason": failure_reason,
            "attempt_number": attempt_number,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._es is None:
            logger.info(
                "[PUSH] Attempt %d for driver %s device %s: %s (no ES service; "
                "audit not persisted)",
                attempt_number,
                driver_id,
                device_id,
                outcome,
            )
            return

        try:
            await self._es.index_document(
                DRIVER_PUSH_ATTEMPTS_INDEX, attempt_id, document
            )
        except Exception as exc:
            logger.warning(
                "[PUSH] Failed to record push attempt for driver %s device %s: %s",
                driver_id,
                device_id,
                exc,
            )

    async def _prune_device(
        self, tenant_id: str, driver_id: str, device_id: str
    ) -> None:
        """Delete the registry record holding a dead token (R9.4)."""
        if self._es is None:
            logger.warning(
                "[PUSH] Provider reported device %s for driver %s invalid but no "
                "ES service is wired; registry record not pruned",
                device_id,
                driver_id,
            )
            return

        doc_id = f"{tenant_id}:{driver_id}:{device_id}"
        try:
            await self._es.delete_document(DRIVER_DEVICES_INDEX, doc_id)
            logger.info(
                "[PUSH] Pruned device registration %s for driver %s",
                device_id,
                driver_id,
            )
        except Exception as exc:
            logger.warning(
                "[PUSH] Failed to prune device registration %s for driver %s: %s",
                device_id,
                driver_id,
                exc,
            )
