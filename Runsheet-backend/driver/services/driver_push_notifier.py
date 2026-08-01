"""
``DriverPushNotifier`` — the four driver push emission points, and the one
business rule that decides whether an emission is delivered.

Four things happen in the field that a driver must learn about even with the app
closed: work is assigned, an assignment is taken away, an exception is escalated,
and a message arrives on a thread the driver is not currently watching. This
module is where each of those becomes a push, and it is the *only* place that
decides whether one is sent.

**It names no provider.** Everything here depends on the channel identifier
``push`` and on the ``ChannelDispatcher`` contract — ``await dispatch(dict) ->
'sent' | 'failed'`` — and on nothing else (R9.15). The dispatcher is resolved by
channel name at emission time, so replacing the provider is a bootstrap change
and no call site moves. Token format validation and provider error interpretation
belong to the dispatcher alone (R9.16); nothing here inspects either.

**The suppression rule lives here, not in the dispatcher.** While a driver's
``drivers_current.status`` is ``off_duty`` or ``inactive``, an *assignment*
notification is dropped and an *escalation* notification for work already
assigned to that driver still goes out (R13.8). That is a business rule about
availability, and the dispatcher has to stay provider-only, so the decision is
made before a dispatcher is ever reached.

**A push never fails the request that triggered it.** Every public method is
total: it returns ``True`` when a device accepted the message and ``False`` for
every other outcome — suppressed, no registered device, no dispatcher wired, a
provider failure, an unreadable duty status. Nothing propagates. A driver whose
handset misses an alert still has a persisted exception, a persisted message, and
a persisted assignment.

**The payload carries identifiers only.** The context handed to the dispatcher is
built from an allow-list (:data:`PUSH_IDENTIFIER_KEYS`), not by filtering a
deny-list, so a caller that hands over a customer name, a phone number, or a
street address cannot get one into a payload (R9.8). The allow-list is the
notifier's own; it deliberately does not import the dispatcher's.

Emission points, and where each is triggered from:

===============================  ==============================================
Trigger                          Source
===============================  ==============================================
Order assigned to a driver       ``OrderService.subscribe("order.dispatched")``
                                 via :meth:`on_order_dispatched`, and the
                                 dispatcher assignment path (R9.5)
Assignment revoked               the assignment-revocation path (R9.6)
Exception escalated              ``ExceptionReportService.report`` on ``high``
                                 or ``critical`` (R9.7)
Thread message, driver offline   ``ThreadMessageService.send``, gated on
                                 ``driver_ws_manager.is_driver_connected``
                                 (R7.11)
===============================  ==============================================

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Push Architecture.

Validates: Requirements 7.11, 9.5, 9.6, 9.7, 9.15, 13.8
- 7.11: a thread message reaches the driver's registered devices when the driver
  holds no open realtime connection
- 9.5: an assignment sends to every registered device for that ``driver_id``
- 9.6: a revocation identifies the revoked ``order_id``
- 9.7: an escalation carries the ``order_id`` and the exception type
- 9.15: every dependency here is the channel identifier ``push`` and the
  ``ChannelDispatcher`` contract — no provider module, endpoint, credential, or
  type is referenced
- 13.8: ``off_duty`` / ``inactive`` suppresses assignment notifications and does
  not suppress escalation notifications
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional, Sequence

from fuel.services.order_es_mappings import DRIVERS_CURRENT_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

#: The channel identifier every emission here depends on. The whole of this
#: module's coupling to push delivery is this string plus the
#: ``ChannelDispatcher`` contract (R9.15).
PUSH_CHANNEL: str = "push"

#: The four registered push ``notification_type`` values. Each one is a
#: ``(event_type, "push")`` template pair, so the wording of an alert is a
#: template edit rather than a code change.
ASSIGNMENT_NOTIFICATION: str = "driver_assignment"
ASSIGNMENT_REVOKED_NOTIFICATION: str = "driver_assignment_revoked"
EXCEPTION_ESCALATION_NOTIFICATION: str = "driver_exception_escalation"
THREAD_MESSAGE_NOTIFICATION: str = "driver_thread_message"

#: The duty statuses that make a driver unavailable for new work (R13.8). Both
#: are ``DriverStatus`` values (``fuel/order_models.py:63``).
UNAVAILABLE_DUTY_STATUSES: tuple[str, ...] = ("off_duty", "inactive")

#: The notification types R13.8 suppresses while the driver is unavailable.
#: Both are assignment-lifecycle alerts about work the driver cannot take on:
#: there is no point telling an off-duty driver that an assignment has arrived,
#: and none in telling them one they were never alerted to has gone away.
SUPPRESSED_WHILE_UNAVAILABLE: tuple[str, ...] = (
    ASSIGNMENT_NOTIFICATION,
    ASSIGNMENT_REVOKED_NOTIFICATION,
)

#: The notification types that are delivered regardless of duty status. An
#: escalation concerns an order *already* assigned to the driver, which is
#: exactly the case R13.8 keeps flowing.
DELIVERED_WHILE_UNAVAILABLE: tuple[str, ...] = (
    EXCEPTION_ESCALATION_NOTIFICATION,
    THREAD_MESSAGE_NOTIFICATION,
)

#: The only keys a push payload may carry (R9.8). An allow-list rather than a
#: deny-list, so a caller that hands over a customer name, a phone number, or a
#: street address cannot get one into a payload — including a key nobody has
#: thought of yet.
PUSH_IDENTIFIER_KEYS: tuple[str, ...] = (
    "tenant_id",
    "order_id",
    "job_id",
    "delivery_window_start",
    "delivery_window_end",
    "exception_id",
    "exception_type",
    "thread_id",
    "message_id",
)


class DriverPushNotifier:
    """Emits the four driver push notifications, suppression rule included.

    Args:
        es_service: The shared ``ElasticsearchService``. Used for the
            ``drivers_current`` duty-status read when no ``driver_repository``
            is available, and nothing else.
        device_registry: The one :class:`~driver.services.device_registry.DeviceRegistry`
            the device router built. Passed in rather than constructed, so one
            instance sits in front of ``driver_devices``.
        notification_service: The ``Notification_Pipeline``. Consulted only to
            resolve the dispatcher registered under the channel name
            :data:`PUSH_CHANNEL`.
        push_dispatcher: A ``ChannelDispatcher`` to use directly, bypassing the
            lookup above. Present for tests and for a deployment that wires the
            channel without the pipeline.
        driver_repository: ``DriverRepository``. Preferred reader of
            ``drivers_current`` because it validates tenant ownership and
            round-trips through the ``Driver`` model.
        driver_ws_manager: ``Driver_WS_Manager``. Read-only here, and only for
            ``is_driver_connected`` — the R7.11 gate. Never written to.
    """

    def __init__(
        self,
        *,
        es_service=None,
        device_registry=None,
        notification_service=None,
        push_dispatcher=None,
        driver_repository=None,
        driver_ws_manager=None,
    ) -> None:
        self._es_service = es_service
        self._device_registry = device_registry
        self._notification_service = notification_service
        self._push_dispatcher = push_dispatcher
        self._driver_repository = driver_repository
        self._driver_ws_manager = driver_ws_manager

    # ------------------------------------------------------------------
    # Emission points
    # ------------------------------------------------------------------

    async def notify_assignment(
        self, *, driver_id: Optional[str], payload: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Alert a driver that an order has been assigned to them (R9.5).

        Suppressed while the driver's duty status is ``off_duty`` or
        ``inactive`` (R13.8).

        Args:
            driver_id: The assigned driver.
            payload: Identifiers for the alert — ``tenant_id``, ``order_id``,
                and the delivery window. Anything outside
                :data:`PUSH_IDENTIFIER_KEYS` is dropped.

        Returns:
            ``True`` when at least one device accepted the notification.

        Validates: Requirements 9.5, 13.8
        """
        return await self._emit(ASSIGNMENT_NOTIFICATION, driver_id, payload)

    async def notify_assignment_revoked(
        self, *, driver_id: Optional[str], payload: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Alert a driver that an assignment has been revoked (R9.6).

        Suppressed while the driver's duty status is ``off_duty`` or
        ``inactive``, on the same grounds as an assignment: it is an
        assignment-lifecycle alert about work the driver is not carrying
        (R13.8).

        Args:
            driver_id: The driver the work was taken from.
            payload: Identifiers for the alert, ``order_id`` among them.

        Returns:
            ``True`` when at least one device accepted the notification.

        Validates: Requirements 9.6, 13.8
        """
        return await self._emit(
            ASSIGNMENT_REVOKED_NOTIFICATION, driver_id, payload
        )

    async def notify_exception_escalation(
        self, *, driver_id: Optional[str], payload: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Alert a driver that an exception on their order was escalated (R9.7).

        Delivered whatever the driver's duty status: the order is already
        assigned to them, which is the case R13.8 explicitly keeps flowing.

        Args:
            driver_id: The driver the escalated order is assigned to.
            payload: Identifiers for the alert — ``order_id`` and
                ``exception_type`` at minimum.

        Returns:
            ``True`` when at least one device accepted the notification.

        Validates: Requirements 9.7, 13.8
        """
        return await self._emit(
            EXCEPTION_ESCALATION_NOTIFICATION, driver_id, payload
        )

    async def notify_thread_message(
        self, *, driver_id: Optional[str], payload: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Alert a driver to a thread message they cannot already see (R7.11).

        The realtime channel is the primary delivery path, so this is the
        fallback and it is gated: a driver holding an open connection has
        already been handed the message and gets no push. An absent
        ``Driver_WS_Manager`` counts as "not connected" — an unproven connection
        must not silently swallow the only alert the driver would get.

        Args:
            driver_id: The thread's assigned driver.
            payload: Identifiers for the alert — ``order_id`` / ``job_id`` and
                the ``message_id``. Never the message body: the app fetches the
                thread over an authenticated request (R9.8).

        Returns:
            ``True`` when at least one device accepted the notification.

        Validates: Requirements 7.11, 9.8
        """
        if self._is_driver_connected(driver_id):
            logger.debug(
                "[PUSH] Thread-message push skipped for driver %s — realtime "
                "connection is open",
                driver_id,
            )
            return False
        return await self._emit(THREAD_MESSAGE_NOTIFICATION, driver_id, payload)

    # ------------------------------------------------------------------
    # Adapters
    # ------------------------------------------------------------------

    async def on_order_dispatched(self, order: Dict[str, Any]) -> None:
        """``order.dispatched`` subscriber: emit the assignment push (R9.5).

        Registered on ``OrderService.subscribe`` from ``bootstrap/driver.py``.
        Dispatch is the point at which an order becomes a driver's work, so it
        is the assignment the driver needs to hear about. An order carrying no
        ``assigned_driver_id`` has nobody to alert and returns quietly.

        The subscriber signature is ``async def handler(order: dict) -> None``;
        ``OrderService`` swallows and logs subscriber exceptions, and this method
        raises none of its own regardless.

        Validates: Requirements 9.5
        """
        if not isinstance(order, dict):
            return

        driver_id = order.get("assigned_driver_id")
        if not driver_id:
            return

        await self.notify_assignment(
            driver_id=driver_id,
            payload={
                "tenant_id": order.get("tenant_id"),
                "order_id": order.get("order_id"),
                "delivery_window_start": order.get("delivery_window_start"),
                "delivery_window_end": order.get("delivery_window_end"),
            },
        )

    # ------------------------------------------------------------------
    # The emission itself
    # ------------------------------------------------------------------

    async def _emit(
        self,
        notification_type: str,
        driver_id: Optional[str],
        payload: Optional[Dict[str, Any]],
    ) -> bool:
        """Suppress, resolve devices, dispatch. Total — never raises.

        The whole body sits under one guard because of what calls it: a POD
        submission, an exception report, a posted message, a status transition.
        None of those may fail because a handset could not be reached.
        """
        try:
            return await self._emit_unguarded(
                notification_type, driver_id, payload or {}
            )
        except Exception as exc:
            # Deliberately broad: an emission failure is a logged event, never
            # the caller's problem.
            logger.warning(
                "[PUSH] Emission of %s for driver %s failed: %s",
                notification_type,
                driver_id,
                exc,
            )
            return False

    async def _emit_unguarded(
        self,
        notification_type: str,
        driver_id: Optional[str],
        payload: Dict[str, Any],
    ) -> bool:
        subject = (driver_id or "").strip()
        if not subject:
            logger.warning(
                "[PUSH] %s not emitted — no driver_id", notification_type
            )
            return False

        tenant_id = str(payload.get("tenant_id") or "").strip()

        if await self._is_suppressed(notification_type, tenant_id, subject):
            return False

        devices = await self._devices(tenant_id, subject)
        if not devices:
            logger.info(
                "[PUSH] %s not emitted for driver %s — no registered device",
                notification_type,
                subject,
            )
            return False

        dispatcher = self._resolve_dispatcher()
        if dispatcher is None:
            logger.warning(
                "[PUSH] %s not emitted for driver %s — no dispatcher "
                "registered for channel '%s'",
                notification_type,
                subject,
                PUSH_CHANNEL,
            )
            return False

        notification = self._build_notification(
            notification_type=notification_type,
            tenant_id=tenant_id,
            driver_id=subject,
            devices=devices,
            payload=payload,
        )

        outcome = await dispatcher.dispatch(notification)
        delivered = outcome == "sent"

        if delivered:
            logger.info(
                "[PUSH] %s delivered to driver %s across %d device(s)",
                notification_type,
                subject,
                len(devices),
            )
        else:
            logger.warning(
                "[PUSH] %s not delivered to driver %s: %s",
                notification_type,
                subject,
                notification.get("failure_reason") or outcome,
            )
        return delivered

    def _build_notification(
        self,
        *,
        notification_type: str,
        tenant_id: str,
        driver_id: str,
        devices: Sequence[Dict[str, Any]],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compose the notification dict the ``push`` dispatcher consumes.

        The shape is the ``ChannelDispatcher`` contract's, not a provider's:
        ``channel`` names the channel, ``notification_type`` selects the
        template, ``devices`` names the destinations, and ``push_data`` carries
        the identifiers. The dispatcher writes ``provider_message_id`` or
        ``failure_reason`` back onto this dict (R9.16).
        """
        identifiers = self._identifiers(payload)
        identifiers["tenant_id"] = tenant_id or identifiers.get("tenant_id", "")

        return {
            "notification_id": str(uuid.uuid4()),
            "channel": PUSH_CHANNEL,
            "notification_type": notification_type,
            "tenant_id": tenant_id,
            "driver_id": driver_id,
            "devices": [
                {
                    "device_id": record.get("device_id"),
                    "push_token": record.get("push_token"),
                }
                for record in devices
            ],
            "push_data": identifiers,
            # Mirrored at the top level too: the dispatcher reads identifier
            # keys from either position, and a dict that carries them in both
            # renders the same alert whichever it consults.
            **identifiers,
        }

    @staticmethod
    def _identifiers(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Reduce a caller's payload to the identifier allow-list (R9.8)."""
        return {
            key: payload[key]
            for key in PUSH_IDENTIFIER_KEYS
            if payload.get(key) not in (None, "")
        }

    # ------------------------------------------------------------------
    # The R13.8 suppression rule
    # ------------------------------------------------------------------

    async def _is_suppressed(
        self, notification_type: str, tenant_id: str, driver_id: str
    ) -> bool:
        """Decide whether R13.8 drops this notification.

        Only an assignment-lifecycle notification is suppressible at all, so an
        escalation never triggers the ``drivers_current`` read. When the read
        cannot answer — no tenant scope, no store, an unreadable record — the
        answer is "not suppressed": the driver is not *known* to be unavailable,
        and dropping an alert on an unproven premise is the worse failure.

        Validates: Requirements 13.8
        """
        if notification_type not in SUPPRESSED_WHILE_UNAVAILABLE:
            return False
        if not tenant_id:
            return False

        status = await self._duty_status(tenant_id, driver_id)
        if status not in UNAVAILABLE_DUTY_STATUSES:
            return False

        logger.info(
            "[PUSH] %s suppressed for driver %s — duty status is %s",
            notification_type,
            driver_id,
            status,
        )
        return True

    async def _duty_status(
        self, tenant_id: str, driver_id: str
    ) -> Optional[str]:
        """Return ``drivers_current.status``, or ``None`` when unreadable.

        ``Duty_Status_Service`` is the sole *writer* of this field (R13.16);
        this is a read of the projection it maintains.
        """
        record = await self._driver_document(tenant_id, driver_id)
        if not isinstance(record, dict):
            return None
        status = record.get("status")
        return status if isinstance(status, str) and status.strip() else None

    async def _driver_document(
        self, tenant_id: str, driver_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read the ``drivers_current`` document, preferring the repository."""
        if self._driver_repository is not None:
            try:
                return _as_document(
                    await self._driver_repository.get(tenant_id, driver_id)
                )
            except Exception as exc:
                logger.warning(
                    "[PUSH] drivers_current read failed for tenant=%s "
                    "driver=%s: %s",
                    tenant_id,
                    driver_id,
                    exc,
                )
                return None

        if self._es_service is None:
            return None

        query = inject_tenant_filter(
            {"query": {"bool": {"filter": [{"term": {"driver_id": driver_id}}]}}},
            tenant_id,
        )
        query["size"] = 1
        try:
            response = await self._es_service.search_documents(
                DRIVERS_CURRENT_INDEX, query, 1
            )
        except Exception as exc:
            logger.warning(
                "[PUSH] drivers_current read failed for tenant=%s driver=%s: %s",
                tenant_id,
                driver_id,
                exc,
            )
            return None

        for hit in ((response or {}).get("hits", {}) or {}).get("hits", []):
            source = hit.get("_source") if isinstance(hit, dict) else None
            if isinstance(source, dict) and source.get("tenant_id") == tenant_id:
                return source
        return None

    # ------------------------------------------------------------------
    # Collaborator resolution
    # ------------------------------------------------------------------

    async def _devices(
        self, tenant_id: str, driver_id: str
    ) -> list[Dict[str, Any]]:
        """Return the driver's registered devices, or an empty list."""
        if self._device_registry is None or not tenant_id:
            return []
        records = await self._device_registry.list_for_driver(
            tenant_id, driver_id
        )
        return [
            record
            for record in (records or [])
            if isinstance(record, dict) and record.get("push_token")
        ]

    def _resolve_dispatcher(self):
        """Return the dispatcher registered under the channel ``push``.

        An explicitly injected dispatcher wins; otherwise the channel name is
        looked up on the notification pipeline, which is the same lookup
        ``RetryPipeline`` performs (``notifications/services/retry_pipeline.py``).
        Either way the only thing named is the channel identifier (R9.15).
        """
        if self._push_dispatcher is not None:
            return self._push_dispatcher

        service = self._notification_service
        if service is None:
            return None

        getter = getattr(service, "get_dispatcher", None)
        if callable(getter):
            try:
                return getter(PUSH_CHANNEL)
            except Exception as exc:
                logger.warning(
                    "[PUSH] Dispatcher lookup for channel '%s' failed: %s",
                    PUSH_CHANNEL,
                    exc,
                )
                return None

        registry = getattr(service, "_dispatchers", None)
        if isinstance(registry, dict):
            return registry.get(PUSH_CHANNEL)
        return None

    def _is_driver_connected(self, driver_id: Optional[str]) -> bool:
        """Whether the driver holds an open realtime connection (R7.11).

        Read-only against ``Driver_WS_Manager``. Anything that cannot answer —
        no manager, no such method, a raising implementation — reports "not
        connected", so the push fallback still fires.
        """
        manager = self._driver_ws_manager
        if manager is None or not driver_id:
            return False
        probe = getattr(manager, "is_driver_connected", None)
        if not callable(probe):
            return False
        try:
            return bool(probe(driver_id))
        except Exception as exc:
            logger.warning(
                "[PUSH] Connection probe failed for driver %s: %s",
                driver_id,
                exc,
            )
            return False


def _as_document(record: Any) -> Optional[Dict[str, Any]]:
    """Normalize a repository return value to a plain dict."""
    if record is None:
        return None
    if isinstance(record, dict):
        return record
    dump = getattr(record, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


__all__ = [
    "DriverPushNotifier",
    "PUSH_CHANNEL",
    "PUSH_IDENTIFIER_KEYS",
    "ASSIGNMENT_NOTIFICATION",
    "ASSIGNMENT_REVOKED_NOTIFICATION",
    "EXCEPTION_ESCALATION_NOTIFICATION",
    "THREAD_MESSAGE_NOTIFICATION",
    "SUPPRESSED_WHILE_UNAVAILABLE",
    "DELIVERED_WHILE_UNAVAILABLE",
    "UNAVAILABLE_DUTY_STATUSES",
]
