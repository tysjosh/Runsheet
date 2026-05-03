"""
Template renderer for the Customer Notification Pipeline.

Renders notification templates by replacing placeholders with event data
using Python's ``str.format_map()`` with a ``SafeDict`` that returns
``[missing]`` for missing keys. Provides CRUD operations for managing
notification templates stored in the ``notification_templates`` Elasticsearch
index.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
"""

import logging
import uuid
from datetime import datetime, timezone

from errors.exceptions import resource_not_found, validation_error
from notifications.services.notification_es_mappings import NOTIFICATION_TEMPLATES_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SafeDict — returns "[missing]" for absent keys
# ---------------------------------------------------------------------------


class SafeDict(dict):
    """A dict subclass that returns ``[missing]`` for absent keys.

    Used with ``str.format_map()`` so that templates render gracefully
    even when some placeholder values are not provided.

    Validates: Requirement 5.5
    """

    def __missing__(self, key: str) -> str:
        logger.warning(f"Template placeholder missing: {key}")
        return "[missing]"


def render_template(template_str: str, data: dict) -> str:
    """Render a single template string by replacing ``{placeholder}`` tokens.

    Uses ``str.format_map()`` with :class:`SafeDict` so that missing keys
    produce ``[missing]`` instead of raising ``KeyError``.

    Validates: Requirements 5.4, 5.7

    Args:
        template_str: The template string with ``{placeholder}`` syntax.
        data: A dict mapping placeholder names to their values.

    Returns:
        The rendered string with all placeholders replaced.
    """
    return template_str.format_map(SafeDict(data))


# ---------------------------------------------------------------------------
# Default template definitions
# ---------------------------------------------------------------------------

# 4 event types × 3 channels = 12 default templates
DEFAULT_TEMPLATES: list[dict] = [
    # --- delivery_confirmation ---
    {
        "event_type": "delivery_confirmation",
        "channel": "sms",
        "subject_template": "Delivery Confirmed — Order {order_id}",
        "body_template": "Your order {order_id} has been delivered. Thank you for choosing our service!",
        "placeholders": ["order_id"],
    },
    {
        "event_type": "delivery_confirmation",
        "channel": "email",
        "subject_template": "Delivery Confirmed — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your order {order_id} has been delivered successfully.\n\n"
            "Thank you for choosing our service!"
        ),
        "placeholders": ["order_id", "customer_name"],
    },
    {
        "event_type": "delivery_confirmation",
        "channel": "whatsapp",
        "subject_template": "Delivery Confirmed — Order {order_id}",
        "body_template": "Your order {order_id} has been delivered. Thank you for choosing our service!",
        "placeholders": ["order_id"],
    },
    # --- delay_alert ---
    {
        "event_type": "delay_alert",
        "channel": "sms",
        "subject_template": "Delivery Delayed — Order {order_id}",
        "body_template": "Your delivery {order_id} is delayed by {delay_minutes} minutes. New ETA: {new_eta}",
        "placeholders": ["order_id", "delay_minutes", "new_eta"],
    },
    {
        "event_type": "delay_alert",
        "channel": "email",
        "subject_template": "Delivery Delayed — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your delivery for order {order_id} is delayed by {delay_minutes} minutes.\n"
            "New estimated arrival: {new_eta}\n\n"
            "We apologize for the inconvenience."
        ),
        "placeholders": ["order_id", "delay_minutes", "new_eta", "customer_name"],
    },
    {
        "event_type": "delay_alert",
        "channel": "whatsapp",
        "subject_template": "Delivery Delayed — Order {order_id}",
        "body_template": "Your delivery {order_id} is delayed by {delay_minutes} minutes. New ETA: {new_eta}",
        "placeholders": ["order_id", "delay_minutes", "new_eta"],
    },
    # --- eta_change ---
    {
        "event_type": "eta_change",
        "channel": "sms",
        "subject_template": "ETA Updated — Order {order_id}",
        "body_template": "ETA updated for order {order_id}: {new_eta} (was {previous_eta})",
        "placeholders": ["order_id", "new_eta", "previous_eta"],
    },
    {
        "event_type": "eta_change",
        "channel": "email",
        "subject_template": "ETA Updated — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "The estimated arrival for your order {order_id} has been updated.\n"
            "Previous ETA: {previous_eta}\n"
            "New ETA: {new_eta}\n\n"
            "Thank you for your patience."
        ),
        "placeholders": ["order_id", "new_eta", "previous_eta", "customer_name"],
    },
    {
        "event_type": "eta_change",
        "channel": "whatsapp",
        "subject_template": "ETA Updated — Order {order_id}",
        "body_template": "ETA updated for order {order_id}: {new_eta} (was {previous_eta})",
        "placeholders": ["order_id", "new_eta", "previous_eta"],
    },
    # --- order_status_update ---
    {
        "event_type": "order_status_update",
        "channel": "sms",
        "subject_template": "Order Update — {order_id}",
        "body_template": "Order {order_id} status changed from {previous_status} to {new_status}",
        "placeholders": ["order_id", "previous_status", "new_status"],
    },
    {
        "event_type": "order_status_update",
        "channel": "email",
        "subject_template": "Order Update — {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your order {order_id} status has changed.\n"
            "Previous status: {previous_status}\n"
            "New status: {new_status}\n\n"
            "Thank you for your patience."
        ),
        "placeholders": ["order_id", "previous_status", "new_status", "customer_name"],
    },
    {
        "event_type": "order_status_update",
        "channel": "whatsapp",
        "subject_template": "Order Update — {order_id}",
        "body_template": "Order {order_id} status changed from {previous_status} to {new_status}",
        "placeholders": ["order_id", "previous_status", "new_status"],
    },
]


class TemplateRenderer:
    """Render and manage notification templates stored in Elasticsearch.

    Each template maps an ``event_type`` + ``channel`` combination to a
    subject and body template containing ``{placeholder}`` tokens that are
    replaced with event data at render time.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
    """

    def __init__(self, es_service: ElasticsearchService):
        self._es = es_service

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    async def render(
        self, template_id: str, event_data: dict, tenant_id: str
    ) -> dict:
        """Render a template by replacing placeholders with *event_data* values.

        Fetches the template from Elasticsearch by *template_id* +
        *tenant_id*, then renders both ``subject_template`` and
        ``body_template`` using :func:`render_template`.

        Validates: Requirements 5.4, 5.5, 5.7, 5.8

        Args:
            template_id: The template identifier.
            event_data: Dict of placeholder values from the operational event.
            tenant_id: Tenant scope from JWT.

        Returns:
            Dict with ``subject`` and ``body`` keys containing the rendered
            strings.

        Raises:
            AppException: 404 if the template is not found for this tenant.
        """
        template = await self._get_template(template_id, tenant_id)

        subject_template = template.get("subject_template") or ""
        body_template = template.get("body_template") or ""

        rendered_subject = render_template(subject_template, event_data) if subject_template else ""
        rendered_body = render_template(body_template, event_data)

        return {
            "subject": rendered_subject,
            "body": rendered_body,
        }

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    async def list_templates(
        self,
        tenant_id: str,
        event_type: str | None = None,
        channel: str | None = None,
    ) -> list[dict]:
        """Return all notification templates for a tenant.

        Optionally filtered by *event_type* and/or *channel*.

        Validates: Requirement 5.2

        Args:
            tenant_id: Tenant scope from JWT.
            event_type: Optional event type filter.
            channel: Optional channel filter.

        Returns:
            List of template dicts sorted by ``event_type`` then ``channel``.
        """
        must_clauses: list[dict] = [
            {"term": {"tenant_id": tenant_id}},
        ]

        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})

        if channel:
            must_clauses.append({"term": {"channel": channel}})

        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                }
            },
            "sort": [
                {"event_type": {"order": "asc"}},
                {"channel": {"order": "asc"}},
            ],
            "size": 100,
        }

        response = await self._es.search_documents(
            NOTIFICATION_TEMPLATES_INDEX, query, size=100
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_template(
        self, template_id: str, tenant_id: str, updates: dict
    ) -> dict:
        """Update specific fields on a notification template.

        Only ``subject_template`` and ``body_template`` may be updated.
        The method first verifies the template exists and belongs to the
        given tenant before applying the partial update.

        Validates: Requirement 5.3

        Args:
            template_id: The template identifier.
            tenant_id: Tenant scope from JWT.
            updates: Dict of fields to update.

        Returns:
            The updated template dict.

        Raises:
            AppException: 404 if the template is not found for this tenant.
            AppException: 400 if updates contain disallowed fields.
        """
        allowed_fields = {"subject_template", "body_template"}
        invalid_fields = set(updates.keys()) - allowed_fields
        if invalid_fields:
            raise validation_error(
                f"Cannot update fields: {', '.join(sorted(invalid_fields))}",
                details={
                    "invalid_fields": sorted(invalid_fields),
                    "allowed_fields": sorted(allowed_fields),
                },
            )

        template = await self._get_template(template_id, tenant_id)

        now = datetime.now(timezone.utc).isoformat()
        partial_doc = {**updates, "updated_at": now}

        await self._es.update_document(
            NOTIFICATION_TEMPLATES_INDEX, template_id, partial_doc
        )

        template.update(partial_doc)
        return template

    # ------------------------------------------------------------------
    # Initialize defaults
    # ------------------------------------------------------------------

    async def initialize_default_templates(self, tenant_id: str) -> None:
        """Create default notification templates for all event type × channel combos.

        Creates 12 templates (4 event types × 3 channels). Existing
        templates for the tenant are left untouched.

        Validates: Requirement 5.6

        Args:
            tenant_id: Tenant scope.
        """
        existing = await self.list_templates(tenant_id)
        existing_keys: set[tuple[str, str]] = {
            (t["event_type"], t["channel"]) for t in existing
        }

        now = datetime.now(timezone.utc).isoformat()

        for tmpl_def in DEFAULT_TEMPLATES:
            key = (tmpl_def["event_type"], tmpl_def["channel"])
            if key in existing_keys:
                logger.info(
                    "Template already exists for event_type=%s channel=%s tenant_id=%s — skipping",
                    tmpl_def["event_type"],
                    tmpl_def["channel"],
                    tenant_id,
                )
                continue

            template_id = str(uuid.uuid4())
            doc = {
                "template_id": template_id,
                "tenant_id": tenant_id,
                "event_type": tmpl_def["event_type"],
                "channel": tmpl_def["channel"],
                "subject_template": tmpl_def["subject_template"],
                "body_template": tmpl_def["body_template"],
                "placeholders": tmpl_def["placeholders"],
                "created_at": now,
                "updated_at": now,
            }

            await self._es.index_document(
                NOTIFICATION_TEMPLATES_INDEX, template_id, doc
            )
            logger.info(
                "Created default notification template: event_type=%s channel=%s tenant_id=%s template_id=%s",
                tmpl_def["event_type"],
                tmpl_def["channel"],
                tenant_id,
                template_id,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_template(self, template_id: str, tenant_id: str) -> dict:
        """Fetch a single template by ID with tenant scoping.

        Args:
            template_id: The template identifier.
            tenant_id: Tenant scope from JWT.

        Returns:
            The template document dict.

        Raises:
            AppException: 404 if the template is not found for this tenant.
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"template_id": template_id}},
                        {"term": {"tenant_id": tenant_id}},
                    ]
                }
            },
            "size": 1,
        }

        response = await self._es.search_documents(
            NOTIFICATION_TEMPLATES_INDEX, query, size=1
        )
        hits = response["hits"]["hits"]

        if not hits:
            raise resource_not_found(
                f"Notification template '{template_id}' not found",
                details={"template_id": template_id},
            )

        return hits[0]["_source"]
