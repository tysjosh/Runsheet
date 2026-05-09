"""
Structured logging helper for the Order Intake Pipeline.

Ensures every log line emitted by the intake pipeline includes the
required context fields: ``tenant_id``, ``intake_channel_id``,
``order_id`` (when known), ``event_id``, ``trace_id``, ``request_id``.

These fields are injected via the existing ``extra_data`` dict pattern
used by :class:`telemetry.service.JSONFormatter`.

Usage::

    from telemetry.intake_logging import IntakeLogContext, intake_logger

    ctx = IntakeLogContext(
        tenant_id="t-123",
        intake_channel_id="voice-ai-prod",
        trace_id="abc-def",
        request_id="req-001",
    )
    log = intake_logger(__name__, ctx)
    log.info("Order received")
    # => JSON includes tenant_id, intake_channel_id, trace_id, request_id

    ctx.order_id = "ord_abc123"
    ctx.event_id = "evt_xyz789"
    log.info("Order persisted")
    # => JSON now also includes order_id and event_id

Validates: Requirement 9.2.4.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class IntakeLogContext:
    """Holds the structured context fields for intake pipeline logging.

    All fields are optional at construction time because some (like
    ``order_id``) are only known after certain pipeline stages complete.
    """

    tenant_id: str = ""
    intake_channel_id: str = ""
    order_id: Optional[str] = None
    event_id: Optional[str] = None
    trace_id: str = ""
    request_id: str = ""

    def as_extra_data(self) -> Dict[str, Any]:
        """Return the context fields as a dict suitable for ``extra_data``."""
        data: Dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "intake_channel_id": self.intake_channel_id,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
        }
        if self.order_id is not None:
            data["order_id"] = self.order_id
        if self.event_id is not None:
            data["event_id"] = self.event_id
        return data


class IntakeLogger:
    """Wraps a standard :class:`logging.Logger` to inject intake context.

    Every log call automatically merges the :class:`IntakeLogContext`
    fields into the ``extra_data`` dict that
    :class:`telemetry.service.JSONFormatter` reads.
    """

    def __init__(self, logger: logging.Logger, context: IntakeLogContext) -> None:
        self._logger = logger
        self._context = context

    @property
    def context(self) -> IntakeLogContext:
        return self._context

    def _make_extra(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Merge context fields with any caller-supplied extra data."""
        merged = self._context.as_extra_data()
        if extra:
            merged.update(extra)
        return {"extra_data": merged}

    def debug(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._logger.debug(msg, extra=self._make_extra(extra), **kwargs)

    def info(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._logger.info(msg, extra=self._make_extra(extra), **kwargs)

    def warning(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._logger.warning(msg, extra=self._make_extra(extra), **kwargs)

    def error(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._logger.error(msg, extra=self._make_extra(extra), **kwargs)

    def exception(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._logger.exception(msg, extra=self._make_extra(extra), **kwargs)


def intake_logger(name: str, context: IntakeLogContext) -> IntakeLogger:
    """Create an :class:`IntakeLogger` bound to the given context.

    Args:
        name: Logger name (typically ``__name__``).
        context: The intake context carrying tenant/channel/order fields.

    Returns:
        An IntakeLogger that injects context into every log line.
    """
    return IntakeLogger(logging.getLogger(name), context)
