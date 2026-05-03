"""
Rule engine for the Customer Notification Pipeline.

Evaluates notification rules to determine whether a given event type should
trigger notifications for a tenant, and provides CRUD operations for managing
notification rules stored in the ``notification_rules`` Elasticsearch index.

Requirements: 1.7, 1.8, 7.1, 7.2, 7.3, 7.4, 7.5
"""

import logging
import uuid
from datetime import datetime, timezone

from errors.exceptions import resource_not_found, validation_error
from notifications.services.notification_es_mappings import NOTIFICATION_RULES_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# The four supported event types with their default channel configuration.
DEFAULT_EVENT_TYPES = [
    "delivery_confirmation",
    "delay_alert",
    "eta_change",
    "order_status_update",
]

DEFAULT_CHANNELS = ["sms", "email", "whatsapp"]


class RuleEngine:
    """Evaluate and manage notification rules stored in Elasticsearch.

    Each rule maps an ``event_type`` to a tenant-scoped configuration that
    controls whether notifications are generated and which default channels
    are used when a customer has no stored preference.

    Validates: Requirements 1.7, 1.8, 7.1, 7.2, 7.3, 7.4, 7.5
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    # ------------------------------------------------------------------
    # Evaluate
    # ------------------------------------------------------------------

    async def evaluate_rule(
        self, event_type: str, tenant_id: str
    ) -> dict | None:
        """Return the rule config if enabled, ``None`` if disabled or missing.

        Queries the ``notification_rules`` index for a rule matching the
        given *event_type* and *tenant_id*.  Returns the full rule document
        when ``enabled`` is ``True``; returns ``None`` when the rule is
        disabled or does not exist.

        Validates: Requirements 1.7, 1.8, 7.5

        Args:
            event_type: The operational event type (e.g. ``delay_alert``).
            tenant_id: Tenant scope from JWT.

        Returns:
            The rule dict when enabled, or ``None``.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"event_type": event_type}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(
            NOTIFICATION_RULES_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            logger.debug(
                "No notification rule found for event_type=%s tenant_id=%s",
                event_type,
                tenant_id,
            )
            return None

        rule = hits[0]["_source"]

        if not rule.get("enabled", False):
            logger.debug(
                "Notification rule disabled for event_type=%s tenant_id=%s",
                event_type,
                tenant_id,
            )
            return None

        return rule

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_rules(self, tenant_id: str) -> list[dict]:
        """Return all notification rules for a tenant.

        Validates: Requirement 7.1

        Args:
            tenant_id: Tenant scope from JWT.

        Returns:
            List of rule dicts sorted by ``event_type``.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "sort": [{"event_type": {"order": "asc"}}],
            "size": 100,
        }

        response = await self._es.search_documents(
            NOTIFICATION_RULES_INDEX, query, size=100
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_rule(
        self, rule_id: str, tenant_id: str, updates: dict
    ) -> dict:
        """Update specific fields on a notification rule.

        Only ``enabled``, ``default_channels``, and ``template_id`` may be
        updated.  The method first verifies the rule exists and belongs to
        the given tenant before applying the partial update.

        Validates: Requirement 7.2

        Args:
            rule_id: The rule identifier.
            tenant_id: Tenant scope from JWT.
            updates: Dict of fields to update.

        Returns:
            The updated rule dict.

        Raises:
            AppException: 404 if the rule is not found for this tenant.
            AppException: 400 if updates contain disallowed fields.
        """
        # Validate allowed fields
        allowed_fields = {"enabled", "default_channels", "template_id"}
        invalid_fields = set(updates.keys()) - allowed_fields
        if invalid_fields:
            raise validation_error(
                f"Cannot update fields: {', '.join(sorted(invalid_fields))}",
                details={
                    "invalid_fields": sorted(invalid_fields),
                    "allowed_fields": sorted(allowed_fields),
                },
            )

        # Verify rule exists and belongs to tenant
        rule = await self._get_rule(rule_id, tenant_id)

        # Apply update
        now = datetime.now(timezone.utc).isoformat()
        partial_doc = {**updates, "updated_at": now}

        await self._es.update_document(
            NOTIFICATION_RULES_INDEX, rule_id, partial_doc
        )

        # Return the merged result
        rule.update(partial_doc)
        return rule

    # ------------------------------------------------------------------
    # Initialize defaults
    # ------------------------------------------------------------------

    async def initialize_default_rules(self, tenant_id: str) -> None:
        """Create default notification rules for all event types.

        Each rule is created with ``enabled=True`` and all three channels
        (sms, email, whatsapp) as defaults.  Existing rules for the tenant
        are left untouched.

        Validates: Requirement 7.4

        Args:
            tenant_id: Tenant scope.
        """
        existing = await self.list_rules(tenant_id)
        existing_event_types = {r["event_type"] for r in existing}

        now = datetime.now(timezone.utc).isoformat()

        for event_type in DEFAULT_EVENT_TYPES:
            if event_type in existing_event_types:
                logger.info(
                    "Rule already exists for event_type=%s tenant_id=%s — skipping",
                    event_type,
                    tenant_id,
                )
                continue

            rule_id = str(uuid.uuid4())
            doc = {
                "rule_id": rule_id,
                "tenant_id": tenant_id,
                "event_type": event_type,
                "enabled": True,
                "default_channels": list(DEFAULT_CHANNELS),
                "template_id": None,
                "created_at": now,
                "updated_at": now,
            }

            await self._es.index_document(
                NOTIFICATION_RULES_INDEX, rule_id, doc
            )
            logger.info(
                "Created default notification rule: event_type=%s tenant_id=%s rule_id=%s",
                event_type,
                tenant_id,
                rule_id,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_rule(self, rule_id: str, tenant_id: str) -> dict:
        """Fetch a single rule by ID with tenant scoping.

        Args:
            rule_id: The rule identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            The rule document dict.

        Raises:
            AppException: 404 if the rule is not found for this tenant.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"rule_id": rule_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(
            NOTIFICATION_RULES_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Notification rule '{rule_id}' not found",
                details={"rule_id": rule_id},
            )

        return hits[0]["_source"]
