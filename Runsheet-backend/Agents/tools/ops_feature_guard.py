"""
Feature flag guard for ops AI tools.

Provides a utility function that ops AI tools call before executing queries.
If the tenant's ops intelligence feature is disabled, returns a structured
disabled response. If enabled (or on any error), returns None to allow the
tool to proceed.

Design principle: fail-open. If the feature flag service is unavailable or
raises an exception, the tool is allowed to proceed rather than blocking
the user.

Validates: Requirement 27.3 — disabled tenants receive a structured disabled
response from AI tools (no exceptions raised).
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level reference, wired at startup via ``configure_ops_feature_guard``.
_feature_flag_service = None

DISABLED_RESPONSE = json.dumps(
    {
        "status": "disabled",
        "message": "Ops intelligence is not enabled for this tenant",
    }
)


def configure_ops_feature_guard(feature_flag_service) -> None:
    """
    Wire the FeatureFlagService into this module.

    Called once during application startup (lifespan) so that
    ``check_ops_feature_flag`` can look up tenant state.
    """
    global _feature_flag_service
    _feature_flag_service = feature_flag_service
    logger.info("Ops AI tools feature guard configured")


async def check_ops_feature_flag(tenant_id: Optional[str]) -> Optional[str]:
    """
    Check whether the ops intelligence layer is enabled for *tenant_id*.

    Returns:
        ``None`` if the tenant is enabled (tool should proceed).
        A JSON string with ``{"status": "disabled", "message": "..."}``
        if the tenant is disabled.

    This function **never raises**. On any error (missing service, Redis
    down, etc.) it logs a warning and returns ``None`` (fail-open) so the
    tool can still attempt to serve the user.
    """
    if tenant_id is None:
        # No tenant context — let the tool handle auth separately.
        return None

    if _feature_flag_service is None:
        logger.warning(
            "FeatureFlagService not configured for AI tools; "
            "allowing request for tenant_id=%s (fail-open)",
            tenant_id,
        )
        return None

    try:
        enabled = await _feature_flag_service.is_enabled(tenant_id)
        if not enabled:
            logger.info(
                "Ops intelligence disabled for tenant_id=%s; "
                "returning disabled response from AI tool",
                tenant_id,
            )
            return DISABLED_RESPONSE
        return None
    except Exception:
        logger.warning(
            "Error checking feature flag for tenant_id=%s; "
            "allowing request (fail-open)",
            tenant_id,
            exc_info=True,
        )
        return None
