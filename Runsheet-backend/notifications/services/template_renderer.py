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
from notifications.templates.fuel_templates import FUEL_NOTIFICATION_TEMPLATES
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

# 4 event types × 3 channels = 12 default templates, plus 4 × 3 = 12
# severe-weather (``_storm`` suffix) variants used when Storm_Mode is
# active for keep-full or generator customers (Task 10.9, Req 9.2.6).
DEFAULT_TEMPLATES: list[dict] = [
    # --- delivery_confirmation ---
    {
        "event_type": "delivery_confirmation",
        "channel": "sms",
        "subject_template": "Delivery Confirmed — Order {order_id}",
        "body_template": (
            "Your order {order_id} has been delivered: {delivered_gallons} gal "
            "at {price}. BOL: {bol_link}"
        ),
        "placeholders": ["order_id", "delivered_gallons", "price", "bol_link"],
    },
    {
        "event_type": "delivery_confirmation",
        "channel": "email",
        "subject_template": "Delivery Confirmed — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your order {order_id} has been delivered successfully.\n\n"
            "Delivered gallons: {delivered_gallons}\n"
            "Price: {price}\n"
            "Bill of Lading: {bol_link}\n\n"
            "Thank you for choosing our service!"
        ),
        "placeholders": [
            "order_id",
            "customer_name",
            "delivered_gallons",
            "price",
            "bol_link",
        ],
    },
    {
        "event_type": "delivery_confirmation",
        "channel": "whatsapp",
        "subject_template": "Delivery Confirmed — Order {order_id}",
        "body_template": (
            "Your order {order_id} has been delivered: {delivered_gallons} gal "
            "at {price}. BOL: {bol_link}"
        ),
        "placeholders": ["order_id", "delivered_gallons", "price", "bol_link"],
    },
    # --- delay_alert ---
    {
        "event_type": "delay_alert",
        "channel": "sms",
        "subject_template": "Delivery Delayed — Order {order_id}",
        "body_template": (
            "Your delivery {order_id} is delayed by {delay_minutes} minutes. "
            "New ETA: {new_eta}. Planned gallons: {delivered_gallons}. "
            "Price: {price}. BOL: {bol_link}"
        ),
        "placeholders": [
            "order_id",
            "delay_minutes",
            "new_eta",
            "delivered_gallons",
            "price",
            "bol_link",
        ],
    },
    {
        "event_type": "delay_alert",
        "channel": "email",
        "subject_template": "Delivery Delayed — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your delivery for order {order_id} is delayed by {delay_minutes} minutes.\n"
            "New estimated arrival: {new_eta}\n"
            "Planned gallons: {delivered_gallons}\n"
            "Price: {price}\n"
            "Bill of Lading: {bol_link}\n\n"
            "We apologize for the inconvenience."
        ),
        "placeholders": [
            "order_id",
            "delay_minutes",
            "new_eta",
            "customer_name",
            "delivered_gallons",
            "price",
            "bol_link",
        ],
    },
    {
        "event_type": "delay_alert",
        "channel": "whatsapp",
        "subject_template": "Delivery Delayed — Order {order_id}",
        "body_template": (
            "Your delivery {order_id} is delayed by {delay_minutes} minutes. "
            "New ETA: {new_eta}. Planned gallons: {delivered_gallons}. "
            "Price: {price}. BOL: {bol_link}"
        ),
        "placeholders": [
            "order_id",
            "delay_minutes",
            "new_eta",
            "delivered_gallons",
            "price",
            "bol_link",
        ],
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
    # ------------------------------------------------------------------
    # Severe-weather variants (Task 10.9, Req 9.2.6)
    #
    # The Customer_Notification pipeline swaps to these templates when
    # Storm_Mode is active for the tenant and the recipient is a
    # keep-full or generator-fuel customer. Each variant surfaces the
    # triggering ``{weather_alert_headline}`` / ``{weather_alert_type}``
    # so customers understand why service is being re-prioritized. The
    # ``event_type`` carries a ``_storm`` suffix so the existing render
    # lookup keeps the non-storm templates untouched.
    # ------------------------------------------------------------------
    # --- delivery_confirmation_storm ---
    {
        "event_type": "delivery_confirmation_storm",
        "channel": "sms",
        "subject_template": "Storm Delivery Completed — Order {order_id}",
        "body_template": (
            "Severe weather ({weather_alert_type}) is active in your area. "
            "Your priority delivery {order_id} has been completed. Stay safe."
        ),
        "placeholders": [
            "order_id",
            "weather_alert_type",
            "weather_alert_headline",
        ],
    },
    {
        "event_type": "delivery_confirmation_storm",
        "channel": "email",
        "subject_template": "Storm Delivery Completed — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "We successfully completed your priority delivery for order "
            "{order_id} while severe weather is active in your area.\n\n"
            "Active alert: {weather_alert_headline} ({weather_alert_type}, "
            "severity {weather_alert_severity}).\n"
            "Expected through: {weather_alert_expected_end_at}.\n\n"
            "If you need a follow-up visit or conservation guidance, "
            "reply to this message or call dispatch.\n\n"
            "Stay safe."
        ),
        "placeholders": [
            "order_id",
            "customer_name",
            "weather_alert_type",
            "weather_alert_headline",
            "weather_alert_severity",
            "weather_alert_expected_end_at",
        ],
    },
    {
        "event_type": "delivery_confirmation_storm",
        "channel": "whatsapp",
        "subject_template": "Storm Delivery Completed — Order {order_id}",
        "body_template": (
            "Severe weather ({weather_alert_type}) is active. Your priority "
            "delivery {order_id} is complete. Stay safe."
        ),
        "placeholders": [
            "order_id",
            "weather_alert_type",
        ],
    },
    # --- delay_alert_storm ---
    {
        "event_type": "delay_alert_storm",
        "channel": "sms",
        "subject_template": "Storm Delay — Order {order_id}",
        "body_template": (
            "{weather_alert_type} in effect. Your delivery {order_id} is "
            "delayed by {delay_minutes} min. New ETA: {new_eta}. "
            "We are prioritizing critical customers."
        ),
        "placeholders": [
            "order_id",
            "delay_minutes",
            "new_eta",
            "weather_alert_type",
        ],
    },
    {
        "event_type": "delay_alert_storm",
        "channel": "email",
        "subject_template": "Storm Delay — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "A {weather_alert_type} is active for your service area "
            "({weather_alert_headline}). Your delivery for order "
            "{order_id} is delayed by {delay_minutes} minutes.\n"
            "New estimated arrival: {new_eta}.\n\n"
            "During severe weather we prioritize keep-full and backup-"
            "generator customers; we are keeping your service in that "
            "priority queue. If your situation becomes urgent please "
            "reply or call dispatch.\n\n"
            "Alert expected through: {weather_alert_expected_end_at}."
        ),
        "placeholders": [
            "order_id",
            "delay_minutes",
            "new_eta",
            "customer_name",
            "weather_alert_type",
            "weather_alert_headline",
            "weather_alert_expected_end_at",
        ],
    },
    {
        "event_type": "delay_alert_storm",
        "channel": "whatsapp",
        "subject_template": "Storm Delay — Order {order_id}",
        "body_template": (
            "{weather_alert_type} active. Delivery {order_id} delayed "
            "{delay_minutes} min. New ETA: {new_eta}."
        ),
        "placeholders": [
            "order_id",
            "delay_minutes",
            "new_eta",
            "weather_alert_type",
        ],
    },
    # --- eta_change_storm ---
    {
        "event_type": "eta_change_storm",
        "channel": "sms",
        "subject_template": "Storm ETA Update — Order {order_id}",
        "body_template": (
            "{weather_alert_type} active. ETA for {order_id} updated: "
            "{new_eta} (was {previous_eta})."
        ),
        "placeholders": [
            "order_id",
            "new_eta",
            "previous_eta",
            "weather_alert_type",
        ],
    },
    {
        "event_type": "eta_change_storm",
        "channel": "email",
        "subject_template": "Storm ETA Update — Order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Severe weather ({weather_alert_headline}) is active in your "
            "service area. The estimated arrival for your order "
            "{order_id} has been updated.\n"
            "Previous ETA: {previous_eta}\n"
            "New ETA: {new_eta}\n\n"
            "Alert expected through: {weather_alert_expected_end_at}.\n\n"
            "Thank you for your patience while we prioritize critical "
            "storm deliveries."
        ),
        "placeholders": [
            "order_id",
            "new_eta",
            "previous_eta",
            "customer_name",
            "weather_alert_type",
            "weather_alert_headline",
            "weather_alert_expected_end_at",
        ],
    },
    {
        "event_type": "eta_change_storm",
        "channel": "whatsapp",
        "subject_template": "Storm ETA Update — Order {order_id}",
        "body_template": (
            "{weather_alert_type} active. New ETA for {order_id}: "
            "{new_eta} (was {previous_eta})."
        ),
        "placeholders": [
            "order_id",
            "new_eta",
            "previous_eta",
            "weather_alert_type",
        ],
    },
    # --- order_status_update_storm ---
    {
        "event_type": "order_status_update_storm",
        "channel": "sms",
        "subject_template": "Storm Order Update — {order_id}",
        "body_template": (
            "{weather_alert_type} active. Order {order_id} status: "
            "{previous_status} → {new_status}."
        ),
        "placeholders": [
            "order_id",
            "previous_status",
            "new_status",
            "weather_alert_type",
        ],
    },
    {
        "event_type": "order_status_update_storm",
        "channel": "email",
        "subject_template": "Storm Order Update — {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Severe weather ({weather_alert_headline}) is active for your "
            "service area. Your order {order_id} status has changed.\n"
            "Previous status: {previous_status}\n"
            "New status: {new_status}\n\n"
            "Alert expected through: {weather_alert_expected_end_at}.\n\n"
            "We will continue to prioritize your service throughout this "
            "event."
        ),
        "placeholders": [
            "order_id",
            "previous_status",
            "new_status",
            "customer_name",
            "weather_alert_type",
            "weather_alert_headline",
            "weather_alert_expected_end_at",
        ],
    },
    {
        "event_type": "order_status_update_storm",
        "channel": "whatsapp",
        "subject_template": "Storm Order Update — {order_id}",
        "body_template": (
            "{weather_alert_type} active. {order_id}: {previous_status} "
            "→ {new_status}."
        ),
        "placeholders": [
            "order_id",
            "previous_status",
            "new_status",
            "weather_alert_type",
        ],
    },
]

# ---------------------------------------------------------------------------
# Extend DEFAULT_TEMPLATES with fuel-specific notification templates
# (low_tank_autofill_alert, past_due_invoice, delivery_completed, e_bol_delivery)
# Validates: Requirements 12.1, 12.2, 12.3, 12.4
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES.extend(FUEL_NOTIFICATION_TEMPLATES)

# ---------------------------------------------------------------------------
# pod_otp — the dispatch-time proof-of-delivery code
# (driver-mobile-app R5.27)
#
# ``sms`` and ``email`` only, matching the two dispatchers the ``pod_otp``
# notification rule enables. This is a NEW dispatch-time entry, not a reuse of
# the completion-time ``delivery_confirmation`` body: it is sent when the order
# leaves the terminal so the customer already holds the code when the driver
# arrives, and it never carries delivered gallons, price, or a BOL link,
# because none of those exist yet.
# ---------------------------------------------------------------------------
POD_OTP_TEMPLATES: list[dict] = [
    {
        "event_type": "pod_otp",
        "channel": "sms",
        "subject_template": "Your delivery code for order {order_id}",
        "body_template": (
            "Your delivery code for order {order_id} is {otp_code}. "
            "Give it to the driver at delivery. Valid until {valid_until}."
        ),
        "placeholders": ["order_id", "otp_code", "valid_until"],
    },
    {
        "event_type": "pod_otp",
        "channel": "email",
        "subject_template": "Your delivery code for order {order_id}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your fuel delivery for order {order_id} is on its way.\n\n"
            "Delivery code: {otp_code}\n"
            "Valid until: {valid_until}\n\n"
            "Give this code to the driver at delivery to confirm receipt. "
            "Do not share it with anyone else."
        ),
        "placeholders": [
            "order_id",
            "customer_name",
            "otp_code",
            "valid_until",
        ],
    },
]

DEFAULT_TEMPLATES.extend(POD_OTP_TEMPLATES)

# ---------------------------------------------------------------------------
# Driver push notifications (driver-mobile-app R9.5, R9.6, R9.7, R9.8, R7.11)
#
# ``push`` channel only. ``subject_template`` is the notification title and
# ``body_template`` is the notification body, so the wording of a dispatch
# alert is a template edit rather than a code change.
#
# Every placeholder here is an entity identifier. R9.8 excludes customer
# names, customer phone numbers, and street addresses from every push
# payload, so no template below may reference one — the app fetches the
# destination detail over an authenticated request when the driver taps.
# ---------------------------------------------------------------------------
DRIVER_PUSH_TEMPLATES: list[dict] = [
    {
        "event_type": "driver_assignment",
        "channel": "push",
        "subject_template": "New assignment",
        "body_template": (
            "Order {order_id} · window {delivery_window_start}"
            "–{delivery_window_end}"
        ),
        "placeholders": [
            "order_id",
            "delivery_window_start",
            "delivery_window_end",
        ],
    },
    {
        "event_type": "driver_assignment_revoked",
        "channel": "push",
        "subject_template": "Assignment revoked",
        "body_template": "Order {order_id} is no longer assigned to you.",
        "placeholders": ["order_id"],
    },
    {
        "event_type": "driver_exception_escalation",
        "channel": "push",
        "subject_template": "Exception escalated",
        "body_template": "Order {order_id} · {exception_type}",
        "placeholders": ["order_id", "exception_type"],
    },
    {
        "event_type": "driver_thread_message",
        "channel": "push",
        "subject_template": "New message",
        "body_template": "Order {order_id} · open the app to read it.",
        "placeholders": ["order_id"],
    },
]

DEFAULT_TEMPLATES.extend(DRIVER_PUSH_TEMPLATES)


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
        must_clauses: list[dict] = []

        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})

        if channel:
            must_clauses.append({"term": {"channel": channel}})

        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": [{"term": {"tenant_id": tenant_id}}],
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
                    ],
                    "filter": [{"term": {"tenant_id": tenant_id}}],
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
