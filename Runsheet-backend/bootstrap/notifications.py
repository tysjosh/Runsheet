"""
Notifications domain bootstrap module.

Initializes: NotificationService, NotificationWSManager, channel dispatchers
(real Twilio/SendGrid when credentials are present, stub otherwise),
notification API endpoints, and ES indices.

Requirements: 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 5.6, 7.4
"""
import logging
import os

from bootstrap.container import ServiceContainer

logger = logging.getLogger(__name__)


def _create_dispatchers():
    """Build the list of channel dispatchers.

    For each channel, attempt to instantiate the real provider-backed
    dispatcher.  If the required environment variables are missing (the
    real dispatcher raises ``ValueError``) or the provider SDK is not
    installed (``ImportError``), fall back to the log-only stub.
    """
    from notifications.services.channel_dispatchers import (
        StubSmsDispatcher,
        StubEmailDispatcher,
        StubWhatsAppDispatcher,
    )

    dispatchers = []

    # --- SMS (Twilio) ---
    _twilio_sms_vars = all([
        os.environ.get("TWILIO_ACCOUNT_SID"),
        os.environ.get("TWILIO_AUTH_TOKEN"),
        os.environ.get("TWILIO_FROM_NUMBER"),
    ])
    if _twilio_sms_vars:
        try:
            from notifications.services.twilio_sms_dispatcher import (
                TwilioSmsDispatcher,
            )
            dispatchers.append(TwilioSmsDispatcher())
            logger.info("Registered REAL Twilio SMS dispatcher")
        except (ValueError, ImportError) as exc:
            logger.warning("Twilio SMS dispatcher unavailable (%s), using stub", exc)
            dispatchers.append(StubSmsDispatcher())
    else:
        logger.info("Twilio SMS env vars not set — using stub SMS dispatcher")
        dispatchers.append(StubSmsDispatcher())

    # --- WhatsApp (Twilio) ---
    _twilio_wa_vars = all([
        os.environ.get("TWILIO_ACCOUNT_SID"),
        os.environ.get("TWILIO_AUTH_TOKEN"),
        os.environ.get("TWILIO_WHATSAPP_FROM_NUMBER"),
    ])
    if _twilio_wa_vars:
        try:
            from notifications.services.twilio_whatsapp_dispatcher import (
                TwilioWhatsAppDispatcher,
            )
            dispatchers.append(TwilioWhatsAppDispatcher())
            logger.info("Registered REAL Twilio WhatsApp dispatcher")
        except (ValueError, ImportError) as exc:
            logger.warning("Twilio WhatsApp dispatcher unavailable (%s), using stub", exc)
            dispatchers.append(StubWhatsAppDispatcher())
    else:
        logger.info("Twilio WhatsApp env vars not set — using stub WhatsApp dispatcher")
        dispatchers.append(StubWhatsAppDispatcher())

    # --- Email (SendGrid) ---
    _sendgrid_vars = all([
        os.environ.get("SENDGRID_API_KEY"),
        os.environ.get("SENDGRID_FROM_EMAIL"),
    ])
    if _sendgrid_vars:
        try:
            from notifications.services.sendgrid_email_dispatcher import (
                SendGridEmailDispatcher,
            )
            dispatchers.append(SendGridEmailDispatcher())
            logger.info("Registered REAL SendGrid email dispatcher")
        except (ValueError, ImportError) as exc:
            logger.warning("SendGrid email dispatcher unavailable (%s), using stub", exc)
            dispatchers.append(StubEmailDispatcher())
    else:
        logger.info("SendGrid env vars not set — using stub email dispatcher")
        dispatchers.append(StubEmailDispatcher())

    return dispatchers


async def initialize(app, container: ServiceContainer) -> None:
    """Create and register notification domain services."""
    from notifications.services.notification_es_mappings import (
        setup_notification_indices,
    )
    from notifications.services.audit_es_mappings import setup_audit_indices
    from notifications.services.notification_service import NotificationService
    from notifications.services.audit_timeline_service import AuditTimelineService
    from notifications.services.communication_metrics_service import CommunicationMetricsService
    from notifications.services.retry_pipeline import RetryPipeline
    from notifications.services.rule_engine import RuleEngine
    from notifications.services.preference_resolver import PreferenceResolver
    from notifications.services.template_renderer import TemplateRenderer
    from notifications.ws.notification_ws_manager import NotificationWSManager
    from notifications.api.endpoints import configure_notification_endpoints
    from notifications.api.metrics_endpoints import configure_metrics_endpoints

    es_service = container.es_service

    # Set up notification ES indices
    try:
        logger.info("Setting up notification indices...")
        setup_notification_indices(es_service)
        logger.info("Notification indices ready")
    except Exception as e:
        logger.warning("Failed to set up notification indices: %s", e)

    # Set up audit timeline indices
    try:
        logger.info("Setting up audit timeline indices...")
        setup_audit_indices(es_service)
        logger.info("Audit timeline indices ready")
    except Exception as e:
        logger.warning("Failed to set up audit timeline indices: %s", e)

    # Notification WebSocket manager
    ws_manager = NotificationWSManager()
    container.notification_ws_manager = ws_manager

    # Core services
    notification_service = NotificationService(es_service)
    container.notification_service = notification_service

    # Audit timeline service (Req 12.1–12.4)
    audit_timeline_service = AuditTimelineService(es_service)
    container.audit_timeline_service = audit_timeline_service

    # Communication SLA metrics service (Req 13.1–13.5)
    communication_metrics_service = CommunicationMetricsService(es_service)
    container.communication_metrics_service = communication_metrics_service

    # Wire WS manager into the notification service
    notification_service.set_ws_manager(ws_manager)

    # Register channel dispatchers — real when credentials are present,
    # stub (log-only) otherwise.
    for dispatcher in _create_dispatchers():
        notification_service.register_dispatcher(
            dispatcher.channel_name, dispatcher
        )

    # Retry pipeline with exponential backoff and DLQ
    retry_pipeline = RetryPipeline(
        notification_service=notification_service,
        es_service=es_service,
    )
    notification_service.set_retry_pipeline(retry_pipeline)
    container.retry_pipeline = retry_pipeline

    # Start the retry pipeline background task
    retry_pipeline.start()
    logger.info("Retry pipeline started")

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

    # Wire communication metrics API endpoints (Req 13.5)
    configure_metrics_endpoints(
        metrics_service=communication_metrics_service,
    )
    logger.info("Communication metrics API configured")

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
        container.job_service._audit_timeline_service = audit_timeline_service
        logger.info("NotificationService and AuditTimelineService wired into JobService")


async def shutdown(app, container: ServiceContainer) -> None:
    """Shut down notification WS manager and retry pipeline."""
    if container.has("retry_pipeline"):
        try:
            container.retry_pipeline.stop()
        except Exception:
            pass

    if container.has("notification_ws_manager"):
        try:
            await container.notification_ws_manager.shutdown()
        except Exception:
            pass

    logger.info("Notifications domain shut down")
