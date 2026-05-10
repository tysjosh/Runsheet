"""
Seed default notification templates and rules.

Provides a ``seed_default_data`` helper that initialises the default
templates and notification rules (core + fuel-specific) for a given tenant.

The function delegates to :meth:`RuleEngine.initialize_default_rules` and
:meth:`TemplateRenderer.initialize_default_templates`, which are idempotent —
existing records are left untouched.

Fuel notification rules wire each fuel template to its trigger event:
- low_tank_autofill_alert → Tank level < reorder_point (Email/SMS)
- past_due_invoice → Invoice status → overdue (Email)
- delivery_completed → POD confirmed (Email/SMS)
- e_bol_delivery → Signed BOL generated (Email)

Requirements: 5.6, 7.4, 12.1, 12.2, 12.3, 12.4
"""

import logging
import uuid
from datetime import datetime, timezone

from notifications.services.notification_es_mappings import NOTIFICATION_RULES_INDEX
from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Default tenant used when no specific tenant is provided at bootstrap time.
# Matches the ``TENANT`` constant in ``seed_all_data.py``.
DEFAULT_TENANT_ID = "dev-tenant"

# ---------------------------------------------------------------------------
# Fuel notification rule definitions
# Each rule wires a fuel template_key to its trigger event and channels.
# Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8
# ---------------------------------------------------------------------------

FUEL_NOTIFICATION_RULES: list[dict] = [
    {
        "event_type": "low_tank_autofill_alert",
        "template_key": "low_tank_autofill_alert",
        "trigger_condition": "tank_level_below_reorder_point",
        "default_channels": ["email", "sms"],
        "enabled": True,
        "description": "Fires when TankForecastingAgent predicts tank level < reorder_point",
    },
    {
        "event_type": "past_due_invoice",
        "template_key": "past_due_invoice",
        "trigger_condition": "invoice_status_overdue",
        "default_channels": ["email"],
        "enabled": True,
        "description": "Fires when an invoice transitions to overdue status",
    },
    {
        "event_type": "delivery_completed",
        "template_key": "delivery_completed",
        "trigger_condition": "pod_confirmed",
        "default_channels": ["email", "sms"],
        "enabled": True,
        "description": "Fires when Proof of Delivery is confirmed",
    },
    {
        "event_type": "e_bol_delivery",
        "template_key": "e_bol_delivery",
        "trigger_condition": "signed_bol_generated",
        "default_channels": ["email"],
        "enabled": True,
        "description": "Fires when a signed BOL PDF is generated; attaches the PDF",
    },
]


async def seed_fuel_notification_rules(
    es_service: ElasticsearchService,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    """Seed fuel-specific notification rules wiring templates to trigger events.

    Creates one ``NotificationRule`` per fuel template, mapping the template
    key to its trigger event and default channels. Existing rules for the
    same event_type are left untouched (idempotent).

    Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8

    Args:
        es_service: The shared Elasticsearch service instance.
        tenant_id: Tenant scope to seed data for.
    """
    from notifications.services.rule_engine import RuleEngine

    rule_engine = RuleEngine(es_service)
    existing_rules = await rule_engine.list_rules(tenant_id)
    existing_event_types = {r["event_type"] for r in existing_rules}

    now = datetime.now(timezone.utc).isoformat()

    for rule_def in FUEL_NOTIFICATION_RULES:
        event_type = rule_def["event_type"]
        if event_type in existing_event_types:
            logger.info(
                "Fuel notification rule already exists for event_type=%s "
                "tenant_id=%s — skipping",
                event_type,
                tenant_id,
            )
            continue

        rule_id = str(uuid.uuid4())
        doc = {
            "rule_id": rule_id,
            "tenant_id": tenant_id,
            "event_type": event_type,
            "template_key": rule_def["template_key"],
            "trigger_condition": rule_def["trigger_condition"],
            "enabled": rule_def["enabled"],
            "default_channels": rule_def["default_channels"],
            "description": rule_def["description"],
            "template_id": None,  # Resolved at runtime from template_key
            "created_at": now,
            "updated_at": now,
        }

        await es_service.index_document(
            NOTIFICATION_RULES_INDEX, rule_id, doc
        )
        logger.info(
            "Created fuel notification rule: event_type=%s template_key=%s "
            "channels=%s tenant_id=%s rule_id=%s",
            event_type,
            rule_def["template_key"],
            rule_def["default_channels"],
            tenant_id,
            rule_id,
        )


async def seed_default_data(
    es_service: ElasticsearchService,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    """Seed default notification rules and templates for *tenant_id*.

    Creates:
    - 4 core ``NotificationRule`` records (delivery_confirmation, delay_alert,
      eta_change, order_status_update — all enabled, all channels).
    - 4 fuel ``NotificationRule`` records (low_tank_autofill_alert,
      past_due_invoice, delivery_completed, e_bol_delivery — each wired to
      its trigger event with appropriate channels).
    - All ``NotificationTemplate`` records (core + fuel templates).

    Both :class:`RuleEngine` and :class:`TemplateRenderer` skip records
    that already exist, so this function is safe to call repeatedly.

    Requirements: 5.6, 7.4, 12.1, 12.2, 12.3, 12.4

    Args:
        es_service: The shared Elasticsearch service instance.
        tenant_id: Tenant scope to seed data for.  Defaults to
            ``"dev-tenant"`` for local development.
    """
    from notifications.services.rule_engine import RuleEngine
    from notifications.services.template_renderer import TemplateRenderer

    logger.info(
        "Seeding default notification data for tenant_id=%s …", tenant_id
    )

    # --- Core rules (4 event types) ---
    try:
        rule_engine = RuleEngine(es_service)
        await rule_engine.initialize_default_rules(tenant_id)
        logger.info(
            "Default notification rules seeded for tenant_id=%s", tenant_id
        )
    except Exception as exc:
        logger.warning(
            "Failed to seed default notification rules for tenant_id=%s: %s",
            tenant_id,
            exc,
        )

    # --- Fuel notification rules (4 fuel templates wired to triggers) ---
    try:
        await seed_fuel_notification_rules(es_service, tenant_id)
        logger.info(
            "Fuel notification rules seeded for tenant_id=%s", tenant_id
        )
    except Exception as exc:
        logger.warning(
            "Failed to seed fuel notification rules for tenant_id=%s: %s",
            tenant_id,
            exc,
        )

    # --- Templates (core + fuel) ---
    try:
        template_renderer = TemplateRenderer(es_service)
        await template_renderer.initialize_default_templates(tenant_id)
        logger.info(
            "Default notification templates seeded for tenant_id=%s",
            tenant_id,
        )
    except Exception as exc:
        logger.warning(
            "Failed to seed default notification templates for tenant_id=%s: %s",
            tenant_id,
            exc,
        )

    logger.info(
        "Notification seed complete for tenant_id=%s", tenant_id
    )
