"""
Feature flag guard for ops AI tools.

Provides a utility function that ops AI tools call before executing queries.
If the tenant's ops intelligence feature is disabled, returns a structured
disabled response. If the feature flag service is unavailable or raises an
exception, the guard **fails closed** (returns the disabled response) to
prevent unscoped queries from leaking data.

Design principle: fail-closed. If the feature flag service is unavailable or
raises an exception, the tool is blocked rather than allowed to proceed
without proper tenant validation.

Validates: Requirement 27.3 — disabled tenants receive a structured disabled
response from AI tools (no exceptions raised).
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level reference, wired at startup via ``configure_ops_feature_guard``.
_feature_flag_service = None

# Counter for observability — incremented on every flag-check error so
# operators can alert on sustained failures.
_feature_flag_errors_total = 0

DISABLED_RESPONSE = json.dumps(
    {
        "status": "disabled",
        "message": "Ops intelligence is not enabled for this tenant",
    }
)

SERVICE_UNAVAILABLE_RESPONSE = json.dumps(
    {
        "status": "disabled",
        "message": "Feature flag service unavailable — request blocked (fail-closed)",
    }
)


def get_feature_flag_errors_total() -> int:
    """Return the cumulative count of feature-flag check errors."""
    return _feature_flag_errors_total


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
        ``None`` if the tenant is explicitly enabled (tool should proceed).
        A JSON string with ``{"status": "disabled", "message": "..."}``
        if the tenant is disabled OR if the check cannot be performed
        (fail-closed).

    This function **never raises**. On any error (missing service, Redis
    down, etc.) it logs a warning and returns the disabled response
    (fail-closed) so the tool cannot proceed without proper validation.
    """
    global _feature_flag_errors_total

    if tenant_id is None:
        # No tenant context — let the tool handle auth separately.
        return None

    if _feature_flag_service is None:
        _feature_flag_errors_total += 1
        logger.warning(
            "FeatureFlagService not configured for AI tools; "
            "blocking request for tenant_id=%s (fail-closed)",
            tenant_id,
        )
        return SERVICE_UNAVAILABLE_RESPONSE

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
        _feature_flag_errors_total += 1
        logger.warning(
            "Error checking feature flag for tenant_id=%s; "
            "blocking request (fail-closed)",
            tenant_id,
            exc_info=True,
        )
        return SERVICE_UNAVAILABLE_RESPONSE
