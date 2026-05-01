"""
Signal Bus — in-process pub/sub for inter-agent communication.

Supports typed publish/subscribe for RiskSignal, InterventionProposal,
OutcomeRecord, and PolicyChangeProposal. Persists all signals to
Elasticsearch for audit and replay.

Validates: Requirements 2.1–2.8
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type

from Agents.overlay.data_contracts import (
    InterventionProposal,
    OutcomeRecord,
    PolicyChangeProposal,
    RiskSignal,
)

logger = logging.getLogger(__name__)

# Type alias for subscriber callbacks
SubscriberCallback = Callable[[Any], Coroutine[Any, Any, None]]

# ES index for signal persistence
AGENT_SIGNALS_INDEX = "agent_signals"


@dataclass
class Subscription:
    """A single subscription with optional filters."""

    subscriber_id: str
    message_type: Type
    callback: SubscriberCallback
    filters: Dict[str, Any] = field(default_factory=dict)
    # Filters support: source_agent, entity_type, severity, tenant_id


class SignalBus:
    """In-process pub/sub for inter-agent communication.

    Provides:
    - Typed publish/subscribe for all data contract types
    - Topic-based filtering by source_agent, entity_type, severity, tenant_id
    - TTL-based signal expiration for RiskSignals
    - ES persistence to agent_signals index
    - Delivery metrics (published, delivered, expired, active subscriptions)

    Args:
        es_service: ElasticsearchService for signal persistence.
    """

    def __init__(self, es_service) -> None:
        self._es = es_service
        self._subscriptions: Dict[Type, List[Subscription]] = defaultdict(list)
        self._lock = asyncio.Lock()

        # Metrics
        self._metrics = {
            "signals_published_total": defaultdict(int),  # by type name
            "signals_delivered_total": defaultdict(int),  # by subscriber_id
            "signals_expired_total": 0,
            "delivery_errors_total": 0,
        }

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        subscriber_id: str,
        message_type: Type,
        callback: SubscriberCallback,
        filters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a subscription for a message type with optional filters.

        Args:
            subscriber_id: Unique identifier for the subscriber.
            message_type: The data contract type to subscribe to.
            callback: Async callback invoked on matching messages.
            filters: Optional dict of field filters (source_agent,
                entity_type, severity, tenant_id).
        """
        sub = Subscription(
            subscriber_id=subscriber_id,
            message_type=message_type,
            callback=callback,
            filters=filters or {},
        )
        async with self._lock:
            self._subscriptions[message_type].append(sub)

    async def unsubscribe(self, subscriber_id: str) -> None:
        """Remove all subscriptions for a subscriber."""
        async with self._lock:
            for msg_type in self._subscriptions:
                self._subscriptions[msg_type] = [
                    s
                    for s in self._subscriptions[msg_type]
                    if s.subscriber_id != subscriber_id
                ]

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, message) -> int:
        """Publish a message to all matching subscribers.

        Validates the message, persists to ES, checks TTL for
        RiskSignals, and delivers to matching subscribers.

        Returns:
            Number of subscribers that received the message.
        """
        msg_type = type(message)
        type_name = msg_type.__name__

        # Validate (Pydantic model_validate triggers on construction)
        # Persist to ES
        await self._persist(message)
        self._metrics["signals_published_total"][type_name] += 1

        # TTL check for RiskSignals
        if isinstance(message, RiskSignal):
            age_seconds = (
                datetime.now(timezone.utc) - message.timestamp
            ).total_seconds()
            if age_seconds > message.ttl_seconds:
                self._metrics["signals_expired_total"] += 1
                return 0

        # Deliver to matching subscribers
        async with self._lock:
            subs = list(self._subscriptions.get(msg_type, []))

        delivered = 0
        for sub in subs:
            if not self._matches_filters(message, sub.filters):
                continue
            try:
                await sub.callback(message)
                delivered += 1
                self._metrics["signals_delivered_total"][sub.subscriber_id] += 1
            except Exception as e:
                self._metrics["delivery_errors_total"] += 1
                logger.error(
                    "SignalBus delivery error for subscriber %s: %s",
                    sub.subscriber_id,
                    e,
                )
                # Skip failing subscriber, continue delivering (Req 2.7)

        return delivered

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _matches_filters(self, message, filters: Dict[str, Any]) -> bool:
        """Check if a message matches subscription filters."""
        for field_name, expected_value in filters.items():
            actual = getattr(message, field_name, None)
            if actual is None:
                continue
            if isinstance(expected_value, (list, set, tuple)):
                if actual not in expected_value:
                    return False
            elif actual != expected_value:
                return False
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _persist(self, message) -> None:
        """Persist a signal to the agent_signals ES index."""
        try:
            doc = message.model_dump(mode="json")
            doc["_signal_type"] = type(message).__name__
            doc_id = (
                getattr(message, "signal_id", None)
                or getattr(message, "proposal_id", None)
                or getattr(message, "outcome_id", None)
                or getattr(message, "forecast_id", None)
                or getattr(message, "priority_list_id", None)
                or getattr(message, "event_id", None)
            )
            await self._es.index_document(AGENT_SIGNALS_INDEX, doc_id, doc)
        except Exception as e:
            logger.error("SignalBus persistence error: %s", e)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    @property
    def active_subscriptions(self) -> int:
        """Count of all active subscriptions across all message types."""
        return sum(len(subs) for subs in self._subscriptions.values())

    def get_metrics(self) -> Dict[str, Any]:
        """Return current metrics snapshot.

        Returns:
            Dict with signals_published_total, signals_delivered_total,
            signals_expired_total, delivery_errors_total, and
            active_subscriptions.
        """
        return {
            "signals_published_total": dict(self._metrics["signals_published_total"]),
            "signals_delivered_total": dict(self._metrics["signals_delivered_total"]),
            "signals_expired_total": self._metrics["signals_expired_total"],
            "delivery_errors_total": self._metrics["delivery_errors_total"],
            "active_subscriptions": self.active_subscriptions,
        }
