"""
``ThreadMessageService`` — the whole job/order thread messaging rule, once.

Both the job-keyed handlers (``POST``/``GET /api/driver/jobs/{job_id}/messages``)
and the order-keyed siblings (``.../orders/{order_id}/messages``) resolve a
:class:`~driver.services.work_ref.WorkRef` and delegate here, so sender-identity
derivation, thread authorization, persistence, delivery, and pagination exist in
exactly one place and the two paths cannot diverge (R7.17, R7.18, R7.19).

Two authorization holes in ``driver/api/message_endpoints.py`` close by
construction once the routers delegate:

* ``_validate_sender_access`` (``:121-131``) compared the **request body's**
  ``sender_id`` to ``job_doc["asset_assigned"]`` and never to the verified
  ``TenantContext`` — a driver could post as any other driver simply by naming
  them in the body. Here the acting identity is a *parameter* the router
  derives from ``TenantContext``; a body value that differs is rejected with
  403 ``SENDER_IDENTITY_MISMATCH`` (R7.5, R7.6), and a body ``sender_role`` is
  ignored outright (R7.7).
* ``list_messages`` (``:260-306``) filtered on ``job_id`` + ``tenant_id`` and
  performed no assignment check at all — any authenticated caller in the tenant
  could read any thread. Here the thread read takes a resolved ``WorkRef``, and
  the resolver has already enforced assignment (R7.8, R7.9).

Collaborators arrive through the constructor from ``configure_message_endpoints``
and its existing module globals: no container, no service locator, no FastAPI
``Depends`` (R7.20).

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Service interfaces.

Validates: Requirements 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 7.17, 15.14
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from driver.models import MessageRequest
from driver.services.driver_es_mappings import JOB_MESSAGES_INDEX
from driver.services.work_ref import WorkRef
from errors.exceptions import forbidden, sender_identity_mismatch

logger = logging.getLogger(__name__)

#: The only roles that may participate in a work thread. A caller holding
#: neither is rejected; the rejection echoes the allowed roles and never the
#: roles the caller actually holds (R15.14).
ALLOWED_SENDER_ROLES = ("driver", "dispatcher")

#: Broadcast event type, unchanged from the job-keyed handler.
MESSAGE_EVENT_TYPE = "job_message"


class ThreadMessageService:
    """Sender identity, thread authorization, persistence, delivery, paging.

    Args:
        es_service: The shared :class:`ElasticsearchService`.
        job_service: The scheduling job service. Retained so the service holds
            the same collaborator set ``configure_message_endpoints`` already
            wires; assignment resolution itself happens in ``WorkRefResolver``.
        order_repository: The fuel-order repository, same rationale.
        driver_ws_manager: ``Driver_WS_Manager`` — delivery to the assigned
            driver (R7.10).
        scheduling_ws_manager: The scheduling channel — delivery to dispatchers
            (R7.10).
        push_notifier: ``Driver_Push_Service``. The R7.11 fallback: a posted
            message becomes a push when the assigned driver holds no open
            realtime connection. The connection probe itself lives in the
            notifier, which holds the same ``Driver_WS_Manager``, so the rule
            exists once rather than on both sides of the call.
    """

    def __init__(
        self,
        *,
        es_service,
        job_service=None,
        order_repository=None,
        driver_ws_manager=None,
        scheduling_ws_manager=None,
        push_notifier=None,
    ) -> None:
        self._es_service = es_service
        self._job_service = job_service
        self._order_repository = order_repository
        self._driver_ws_manager = driver_ws_manager
        self._scheduling_ws_manager = scheduling_ws_manager
        self._push_notifier = push_notifier

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def send(
        self,
        ref: WorkRef,
        body: MessageRequest,
        *,
        sender_id: str,
        sender_role: str,
        request_id: str,
    ) -> dict:
        """Post a message to the thread named by ``ref``.

        ``sender_id`` and ``sender_role`` are derived by the router from the
        verified ``TenantContext``. The body is consulted for ``body`` only: a
        body ``sender_id`` that differs from ``sender_id`` is a rejection, and a
        body ``sender_role`` is ignored (R7.5, R7.6, R7.7).

        Args:
            ref: The resolved unit of work; the resolver has already enforced
                assignment authorization.
            body: The request body.
            sender_id: The acting identity derived from ``TenantContext``.
            sender_role: The acting role derived from ``TenantContext``.
            request_id: The correlation id echoed in the response envelope.

        Returns:
            ``{"data": <persisted message>, "request_id": ...}`` — the same
            envelope the job-keyed handler returns today (R7.15).

        Raises:
            AppException: 403 ``SENDER_IDENTITY_MISMATCH`` when the body names a
                different sender; 403 ``FORBIDDEN`` when the derived role is
                neither ``driver`` nor ``dispatcher``.

        Validates: Requirements 7.5, 7.6, 7.7, 7.10, 7.17
        """
        self._require_supported_role(ref, sender_role)
        self._require_matching_sender(ref, body, sender_id)

        message_doc = self._build_message_doc(
            ref, body, sender_id=sender_id, sender_role=sender_role
        )

        await self._es_service.index_document(
            JOB_MESSAGES_INDEX, message_doc["message_id"], message_doc
        )

        await self._deliver(message_doc, recipient_driver_id=_recipient(ref))

        return {"data": message_doc, "request_id": request_id}

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def list(
        self,
        ref: WorkRef,
        *,
        page: int,
        size: int,
        request_id: str,
    ) -> dict:
        """Return the thread for ``ref``, ``timestamp`` ascending.

        Authorization is the resolution: a caller who could not resolve ``ref``
        never reaches this method, which is what closes the unchecked-read hole
        at ``message_endpoints.py:260-306`` (R7.8, R7.9).

        Args:
            ref: The resolved unit of work.
            page: 1-based page number.
            size: Page size.
            request_id: The correlation id echoed in the response envelope.

        Returns:
            ``{"data": [...], "pagination": {...}, "request_id": ...}`` — the
            same envelope the job-keyed handler returns today (R7.15).

        Validates: Requirements 7.8, 7.9, 7.12, 7.17
        """
        offset = (page - 1) * size
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {_thread_key(ref): ref.work_id}},
                        {"term": {"tenant_id": ref.tenant_id}},
                    ]
                }
            },
            "sort": [{"timestamp": {"order": "asc"}}],
            "from": offset,
            "size": size,
        }

        response = await self._es_service.search_documents(
            JOB_MESSAGES_INDEX, query, size=size
        )
        hits = (response or {}).get("hits", {})
        total = _total_hits(hits)
        messages = [hit["_source"] for hit in hits.get("hits", [])]

        return {
            "data": messages,
            "pagination": {
                "page": page,
                "size": size,
                "total": total,
                "total_pages": max(1, -(-total // size)),  # ceil division
            },
            "request_id": request_id,
        }

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def _require_supported_role(self, ref: WorkRef, sender_role: str) -> None:
        """Reject a role that cannot participate in a work thread."""
        if sender_role in ALLOWED_SENDER_ROLES:
            return
        raise forbidden(
            message="Sender role is not permitted on this thread",
            details={
                **_work_reference(ref),
                "allowed_roles": list(ALLOWED_SENDER_ROLES),
            },
        )

    def _require_matching_sender(
        self, ref: WorkRef, body: MessageRequest, sender_id: str
    ) -> None:
        """Reject a body ``sender_id`` that is not the derived identity.

        An absent or empty body value is not a mismatch — the derived identity
        simply applies (R7.5). Details name the work reference only (R15.14).
        """
        claimed = (getattr(body, "sender_id", None) or "").strip()
        if not claimed or claimed == sender_id:
            return
        raise sender_identity_mismatch(details=_work_reference(ref))

    def _build_message_doc(
        self,
        ref: WorkRef,
        body: MessageRequest,
        *,
        sender_id: str,
        sender_role: str,
    ) -> dict:
        """Compose the ``job_messages`` document.

        The job-keyed document shape is byte-for-byte what the handler writes
        today, so the job-keyed response contract is unchanged (R7.15). The
        order-keyed path substitutes ``order_id`` for ``job_id`` and stamps the
        canonical ``driver_id``; both fields are declared on the
        ``dynamic: strict`` mapping.
        """
        doc: dict[str, Any] = {"message_id": str(uuid.uuid4())}
        if ref.kind == "job":
            doc["job_id"] = ref.work_id
        else:
            doc["order_id"] = ref.work_id
            doc["driver_id"] = ref.driver_id
        doc.update(
            {
                "sender_id": sender_id,
                "sender_role": sender_role,
                "body": body.body,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tenant_id": ref.tenant_id,
            }
        )
        return doc

    # ------------------------------------------------------------------
    # Delivery (R7.10)
    # ------------------------------------------------------------------

    async def _deliver(
        self, message_doc: dict, *, recipient_driver_id: Optional[str]
    ) -> None:
        """Fan the message out to dispatchers and to the assigned driver.

        Delivery is best-effort: a broadcast failure is logged and never fails
        a persisted message.
        """
        if self._scheduling_ws_manager is not None:
            try:
                await self._scheduling_ws_manager.broadcast(
                    MESSAGE_EVENT_TYPE, message_doc
                )
            except Exception as exc:
                logger.warning(
                    "Scheduling WS broadcast failed for %s on thread %s: %s",
                    MESSAGE_EVENT_TYPE,
                    _thread_id(message_doc),
                    exc,
                )

        if self._driver_ws_manager is not None:
            try:
                if recipient_driver_id and hasattr(
                    self._driver_ws_manager, "send_to_driver"
                ):
                    await self._driver_ws_manager.send_to_driver(
                        recipient_driver_id,
                        {"type": MESSAGE_EVENT_TYPE, "data": message_doc},
                    )
                elif hasattr(self._driver_ws_manager, "broadcast"):
                    await self._driver_ws_manager.broadcast(
                        MESSAGE_EVENT_TYPE, message_doc
                    )
            except Exception as exc:
                logger.warning(
                    "Driver WS broadcast failed for %s on thread %s: %s",
                    MESSAGE_EVENT_TYPE,
                    _thread_id(message_doc),
                    exc,
                )

        await self._notify_thread_message(
            message_doc, recipient_driver_id=recipient_driver_id
        )

    async def _notify_thread_message(
        self, message_doc: dict, *, recipient_driver_id: Optional[str]
    ) -> None:
        """Emit the push fallback for a message the driver cannot see (R7.11).

        Called unconditionally: whether the driver holds an open connection is
        decided inside the notifier, which reads the same ``Driver_WS_Manager``
        and is where the R7.11 gate lives. The payload carries identifiers only
        — never the message body, which the app fetches over an authenticated
        request (R9.8).
        """
        if self._push_notifier is None or not recipient_driver_id:
            return
        try:
            await self._push_notifier.notify_thread_message(
                driver_id=recipient_driver_id,
                payload={
                    "tenant_id": message_doc.get("tenant_id"),
                    "order_id": message_doc.get("order_id"),
                    "job_id": message_doc.get("job_id"),
                    "message_id": message_doc.get("message_id"),
                },
            )
        except Exception as exc:
            logger.warning(
                "Thread-message push failed on thread %s: %s",
                _thread_id(message_doc),
                exc,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thread_key(ref: WorkRef) -> str:
    """The ``job_messages`` field that keys this thread."""
    return "job_id" if ref.kind == "job" else "order_id"


def _thread_id(message_doc: dict) -> Optional[str]:
    """The thread identifier for a log line."""
    return message_doc.get("job_id") or message_doc.get("order_id")


def _work_reference(ref: WorkRef) -> dict:
    """Rejection details: the work reference and nothing else.

    Never the caller's held roles, never the identity of the driver a thread is
    assigned to (R15.14).
    """
    return {_thread_key(ref): ref.work_id}


def _recipient(ref: WorkRef) -> Optional[str]:
    """The driver the message is delivered to over ``Driver_WS_Manager``.

    Prefers the canonical ``assigned_driver_id`` that R1.13 began writing, and
    falls back to the resolved caller's ``driver_id``. The pre-migration
    ``asset_assigned`` value is the last resort, since a ``user_id`` cannot
    reach a ``driver_id``-keyed connection.
    """
    doc: dict[str, Any] = ref.order_doc or ref.job_doc or {}
    return doc.get("assigned_driver_id") or ref.driver_id or doc.get("asset_assigned")


def _total_hits(hits: dict) -> int:
    """Read the hit total across both ES response shapes."""
    total = hits.get("total", 0)
    if isinstance(total, dict):
        return int(total.get("value", 0) or 0)
    return int(total or 0)
