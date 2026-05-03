"""
Seed default notification templates and rules.

Provides a ``seed_default_data`` helper that initialises the 12 default
templates (4 event types × 3 channels) and 4 default notification rules
(one per event type, all enabled, all channels) for a given tenant.

The function delegates to :meth:`RuleEngine.initialize_default_rules` and
:meth:`TemplateRenderer.initialize_default_templates`, which are idempotent —
existing records are left untouched.

Requirements: 5.6, 7.4
"""

import logging

from services.elasticsearch_service import ElasticsearchService

logger = logging.getLogger(__name__)

# Default tenant used when no specific tenant is provided at bootstrap time.
# Matches the ``TENANT`` constant in ``seed_all_data.py``.
DEFAULT_TENANT_ID = "dev-tenant"


async def seed_default_data(
    es_service: ElasticsearchService,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    """Seed default notification rules and templates for *tenant_id*.

    Creates:
    - 4 ``NotificationRule`` records (one per event type, all enabled,
      default channels = ``["sms", "email", "whatsapp"]``).
    - 12 ``NotificationTemplate`` records (4 event types × 3 channels).

    Both :class:`RuleEngine` and :class:`TemplateRenderer` skip records
    that already exist, so this function is safe to call repeatedly.

    Requirements: 5.6, 7.4

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

    # --- Rules (4 event types) ---
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

    # --- Templates (4 event types × 3 channels = 12) ---
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
