"""
``ExceptionReportService`` — the whole driver field-exception rule, once.

A driver reports a field exception against one unit of work, which the caller
may name either by ``job_id`` or by ``order_id``. Both router handlers resolve
that path parameter into a :class:`~driver.services.work_ref.WorkRef` and then
call :meth:`ExceptionReportService.report`, so persistence, ``RiskSignal``
publication, and the escalation broadcast exist in exactly one place and the
two paths cannot diverge on a rule or on an error code (R7.16, R7.19).

The rule, in order:

1. persist the exception into ``driver_exceptions`` with the acting
   ``driver_id``, the type, the severity, the note, the geotag, and any
   supplied media ``file_ref`` values (R7.1)
2. append an ``exception_reported`` event to the job timeline, on the
   job-keyed path, best-effort (existing behaviour)
3. publish a ``RiskSignal`` on the existing SignalBus path (R7.2)
4. broadcast ``exception_escalation`` when severity is ``high`` or
   ``critical`` (R7.3), and emit the escalation push for that driver (R9.7)

Authorization is not repeated here: it belongs to the resolver that produced
the ``WorkRef`` — dual acceptance on the job-keyed path (R1.12), and
``assigned_driver_id`` equality on the order-keyed path (R7.4, R7.21).

Collaborators arrive through the constructor from the module-level globals
``configure_exception_endpoints`` already holds. No dependency-injection
container, no service locator, no FastAPI ``Depends`` for collaborators
(R7.20).

Steps 2 through 4 are best-effort by design: a persisted exception is the
record of the driver's report, and a downstream bus or socket failure must not
turn a successful report into an error the driver has to retry from the field.

Design: see ``.kiro/specs/driver-mobile-app/design.md`` §Service interfaces.

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.13, 7.16, 9.7
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from Agents.overlay.data_contracts import RiskSignal, Severity
from driver.models import ExceptionRequest
from driver.services.driver_es_mappings import DRIVER_EXCEPTIONS_INDEX
from driver.services.work_ref import WorkRef

logger = logging.getLogger(__name__)

#: Severities that raise an escalation to dispatch (R7.3).
ESCALATION_SEVERITIES = (Severity.HIGH, Severity.CRITICAL)

#: Source agent name on every published RiskSignal. Unchanged from the
#: in-handler implementation so SignalBus subscribers keep matching.
RISK_SIGNAL_SOURCE_AGENT = "driver_exception_reporter"


class ExceptionReportService:
    """Persist → publish ``RiskSignal`` → broadcast escalation, from a WorkRef.

    Args:
        es_service: Anything exposing
            ``index_document(index, doc_id, document)``. Required.
        job_service: Anything exposing ``_append_event(...)``. Optional: the
            job timeline event is skipped when it is absent.
        order_repository: Held for symmetry with the other extracted driver
            services; the exception rule reads the order document off the
            ``WorkRef`` the resolver already fetched, so nothing here calls it.
        signal_bus: Anything exposing ``publish(signal)``. Optional.
        driver_ws_manager: Driver-facing socket manager. Optional.
        scheduling_ws_manager: Dispatcher-facing socket manager. Optional.
        push_notifier: Anything exposing
            ``notify_exception_escalation(driver_id=..., payload=...)``.
            Optional — the escalation push emission point R9.7 names lives
            here, and stays inert until the notifier is wired.
    """

    def __init__(
        self,
        *,
        es_service,
        job_service=None,
        order_repository=None,
        signal_bus=None,
        driver_ws_manager=None,
        scheduling_ws_manager=None,
        push_notifier=None,
    ) -> None:
        if es_service is None:
            raise ValueError("ExceptionReportService requires an es_service")
        self._es_service = es_service
        self._job_service = job_service
        self._order_repository = order_repository
        self._signal_bus = signal_bus
        self._driver_ws_manager = driver_ws_manager
        self._scheduling_ws_manager = scheduling_ws_manager
        self._push_notifier = push_notifier

    # -- the rule -------------------------------------------------------

    async def report(
        self,
        ref: WorkRef,
        body: ExceptionRequest,
        *,
        request_id: str,
    ) -> dict:
        """Report a field exception against the resolved unit of work.

        Args:
            ref: The resolved work reference, carrying the tenant scope and
                the acting ``driver_id``.
            body: The validated request body. ``exception_type`` and
                ``severity`` are already enum-checked by Pydantic (R7.3).
            request_id: The correlation identifier echoed in the response.

        Returns:
            ``{"data": <persisted exception document>, "request_id": ...}`` —
            the same envelope the job-keyed handler has always returned.

        Validates: Requirements 7.1, 7.2, 7.3, 7.13, 7.16, 9.7
        """
        now = datetime.now(timezone.utc).isoformat()
        exception_id = str(uuid.uuid4())

        exception_doc = self._build_document(
            exception_id=exception_id,
            ref=ref,
            body=body,
            now=now,
        )

        # 1. Persist (R7.1). A failure here is the caller's failure: there is
        #    no report without a stored record, so this one propagates.
        await self._es_service.index_document(
            DRIVER_EXCEPTIONS_INDEX, exception_id, exception_doc
        )

        # 2. Job timeline event — job-keyed path only.
        await self._append_timeline_event(ref, body, exception_id, now)

        # 3. RiskSignal on the existing SignalBus path (R7.2).
        await self._publish_risk_signal(ref, body, exception_id)

        # 4. Escalation on high/critical (R7.3) plus the push (R9.7).
        if body.severity in ESCALATION_SEVERITIES:
            await self._escalate(ref, body, exception_id, now)

        return {"data": exception_doc, "request_id": request_id}

    # -- persistence ----------------------------------------------------

    def _build_document(
        self,
        *,
        exception_id: str,
        ref: WorkRef,
        body: ExceptionRequest,
        now: str,
    ) -> dict:
        """Build the ``driver_exceptions`` document.

        Carries the acting ``driver_id`` from the verified session claim on
        the ``WorkRef`` rather than from the request body, and both work keys
        so an exception resolved from either namespace names its order
        (R7.1, R7.13). ``driver_exceptions`` is ``dynamic: strict`` and
        declares every key written here.

        Validates: Requirements 7.1, 7.13
        """
        return {
            "exception_id": exception_id,
            "job_id": ref.job_id,
            "order_id": ref.order_id,
            "driver_id": ref.driver_id,
            "exception_type": body.exception_type.value,
            "severity": body.severity.value,
            "note": body.note,
            "location": body.location.model_dump() if body.location else None,
            "media_refs": body.media_refs or [],
            "tenant_id": ref.tenant_id,
            "timestamp": now,
        }

    async def _append_timeline_event(
        self,
        ref: WorkRef,
        body: ExceptionRequest,
        exception_id: str,
        now: str,
    ) -> None:
        """Append ``exception_reported`` to the job timeline, best-effort.

        Only the job-keyed path has a job timeline to append to; the
        order-keyed path carries no ``job_id``.
        """
        if self._job_service is None or ref.job_id is None:
            return
        try:
            await self._job_service._append_event(
                job_id=ref.job_id,
                event_type="exception_reported",
                tenant_id=ref.tenant_id,
                actor_id=ref.driver_id,
                payload={
                    "exception_id": exception_id,
                    "exception_type": body.exception_type.value,
                    "severity": body.severity.value,
                    "note": body.note,
                    "timestamp": now,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to append exception_reported event for job %s: %s",
                ref.job_id,
                exc,
            )

    # -- RiskSignal -----------------------------------------------------

    def build_risk_signal(
        self,
        ref: WorkRef,
        body: ExceptionRequest,
        exception_id: str,
    ) -> RiskSignal:
        """Convert an exception report into a ``RiskSignal``.

        ``entity_id`` is the work key the caller named, ``entity_type`` is the
        exception type, and the severity maps straight across. Consumed by the
        exception_commander and exception_replanning agents.

        Validates: Requirement 7.2
        """
        return RiskSignal(
            source_agent=RISK_SIGNAL_SOURCE_AGENT,
            entity_id=ref.work_id,
            entity_type=body.exception_type.value,
            severity=body.severity,
            confidence=0.9,
            ttl_seconds=3600,
            tenant_id=ref.tenant_id,
            context={
                "exception_id": exception_id,
                "note": body.note,
                "location": body.location.model_dump() if body.location else None,
                "media_refs": body.media_refs or [],
            },
        )

    async def _publish_risk_signal(
        self,
        ref: WorkRef,
        body: ExceptionRequest,
        exception_id: str,
    ) -> None:
        """Publish the ``RiskSignal`` on the existing SignalBus path (R7.2)."""
        if self._signal_bus is None:
            return
        try:
            await self._signal_bus.publish(
                self.build_risk_signal(ref, body, exception_id)
            )
        except Exception as exc:
            logger.warning(
                "Failed to publish RiskSignal for exception %s: %s",
                exception_id,
                exc,
            )

    # -- escalation -----------------------------------------------------

    async def _escalate(
        self,
        ref: WorkRef,
        body: ExceptionRequest,
        exception_id: str,
        now: str,
    ) -> None:
        """Broadcast ``exception_escalation`` and emit the escalation push.

        Validates: Requirements 7.3, 9.7
        """
        escalation_data = {
            "job_id": ref.job_id,
            "order_id": ref.order_id,
            "exception_id": exception_id,
            "exception_type": body.exception_type.value,
            "severity": body.severity.value,
            "note": body.note,
            "timestamp": now,
            "tenant_id": ref.tenant_id,
        }
        await self._broadcast(
            "exception_escalation", escalation_data, driver_id=ref.driver_id
        )
        await self._notify_escalation(ref, body, exception_id)

    async def _broadcast(
        self,
        event_type: str,
        event_data: dict,
        *,
        driver_id: Optional[str] = None,
    ) -> None:
        """Fan an escalation event out over both socket managers (R7.3)."""
        if self._scheduling_ws_manager is not None:
            try:
                await self._scheduling_ws_manager.broadcast(event_type, event_data)
            except Exception as exc:
                logger.warning(
                    "Scheduling WS broadcast failed for %s on work %s: %s",
                    event_type,
                    event_data.get("job_id") or event_data.get("order_id"),
                    exc,
                )

        if self._driver_ws_manager is not None:
            try:
                if driver_id and hasattr(self._driver_ws_manager, "send_to_driver"):
                    await self._driver_ws_manager.send_to_driver(
                        driver_id,
                        {"type": event_type, "data": event_data},
                    )
                elif hasattr(self._driver_ws_manager, "broadcast"):
                    await self._driver_ws_manager.broadcast(event_type, event_data)
            except Exception as exc:
                logger.warning(
                    "Driver WS broadcast failed for %s on work %s: %s",
                    event_type,
                    event_data.get("job_id") or event_data.get("order_id"),
                    exc,
                )

    async def _notify_escalation(
        self,
        ref: WorkRef,
        body: ExceptionRequest,
        exception_id: str,
    ) -> None:
        """Emit the escalation push for the acting driver (R9.7).

        The payload carries identifiers and the exception type only — no
        customer name, phone number, or street address (R9.8).
        """
        if self._push_notifier is None:
            return
        try:
            await self._push_notifier.notify_exception_escalation(
                driver_id=ref.driver_id,
                payload={
                    "tenant_id": ref.tenant_id,
                    "order_id": ref.order_id,
                    "job_id": ref.job_id,
                    "exception_id": exception_id,
                    "exception_type": body.exception_type.value,
                    "severity": body.severity.value,
                },
            )
        except Exception as exc:
            logger.warning(
                "Escalation push failed for exception %s: %s", exception_id, exc
            )
