"""
Notifications domain bootstrap module.

Initializes: NotificationService, NotificationWSManager, channel dispatchers
(stub SMS, email, WhatsApp), notification API endpoints, and ES indices.

Requirements: 1.5, 2.1, 5.6, 7.4
"""
import logging

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register notification domain services."""
    from notifications.services.notification_es_mappings import (
        setup_notification_indices,
    )
    from notifications.services.notification_service import NotificationService
    from notifications.services.rule_engine import RuleEngine
    from notifications.services.preference_resolver import PreferenceResolver
    from notifications.services.template_renderer import TemplateRenderer
    from notifications.services.channel_dispatchers import (
        StubSmsDispatcher,
        StubEmailDispatcher,
        StubWhatsAppDispatcher,
    )
    from notifications.ws.notification_ws_manager import NotificationWSManager
    from notifications.api.endpoints import configure_notification_endpoints

    es_service = container.es_service

    # Set up notification ES indices
    try:
        logger.info("Setting up notification indices...")
        setup_notification_indices(es_service)
        logger.info("Notification indices ready")
    except Exception as e:
        logger.warning("Failed to set up notification indices: %s", e)

    # Notification WebSocket manager
    ws_manager = NotificationWSManager()
    container.notification_ws_manager = ws_manager

    # Core services
    notification_service = NotificationService(es_service)
    container.notification_service = notification_service

    # Wire WS manager into the notification service
    notification_service.set_ws_manager(ws_manager)

    # Register stub channel dispatchers (log-only for MVP)
    for dispatcher in (
        StubSmsDispatcher(),
        StubEmailDispatcher(),
        StubWhatsAppDispatcher(),
    ):
        notification_service.register_dispatcher(
            dispatcher.channel_name, dispatcher
        )

    # Wire notification API endpoints
    rule_engine = notification_service._rule_engine
    preference_resolver = notification_service._preference_resolver
    template_renderer = notification_service._template_renderer

    configure_notification_endpoints(
        notification_service=notification_service,
        rule_engine=rule_engine,
        preference_resolver=preference_resolver,
        template_renderer=template_renderer,
    )
    logger.info("Notification API configured")

    # Seed default notification rules and templates for the dev tenant.
    # The seed function is idempotent — existing records are left untouched.
    try:
        from notifications.services.seed_data import seed_default_data

        await seed_default_data(es_service)
        logger.info("Default notification seed data applied")
    except Exception as e:
        logger.warning("Failed to seed default notification data: %s", e)

    # Wire notification_service into job_service (non-blocking hook)
    if container.has("job_service"):
        container.job_service._notification_service = notification_service
        logger.info("NotificationService wired into JobService")


async def shutdown(app, container: ServiceContainer) -> None:
    """Shut down notification WS manager."""
    if container.has("notification_ws_manager"):
        try:
            await container.notification_ws_manager.shutdown()
        except Exception:
            pass

    logger.info("Notifications domain shut down")
