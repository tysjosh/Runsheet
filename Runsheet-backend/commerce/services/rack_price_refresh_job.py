"""Scheduled job to refresh OPIS rack prices with 90-day retention.

Runs daily at 06:00 local (see ``RACK_PRICE_REFRESH_INTERVAL_SECONDS``)
and fetches the latest rack prices from the OPIS data feed. The actual
OPIS API integration is **out of scope** for this spec — it is an
external data feed that requires a commercial subscription and
credentials provisioning. This module provides the placeholder
infrastructure so the cron scheduling, retention policy, and bootstrap
wiring are in place for when the OPIS connector is implemented.

Registered via the existing scheduler infrastructure (asyncio
background task pattern used throughout ``bootstrap/``). Wired from
``bootstrap/compliance.py`` alongside the price-protection expiry job.

Validates: Requirement 11.6
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Interval between rack-price refresh cycles (seconds). Daily per
# Req 11.6 — the OPIS feed publishes once per day (typically 06:00
# local) so more frequent polling adds no value. Kept as a module
# constant so the bootstrap cron can import it alongside the cycle
# function.
RACK_PRICE_REFRESH_INTERVAL_SECONDS: int = 86_400  # 24 hours

# Retention period for historical rack prices (days). Per Req 11.6,
# 90 days of price history are retained for audit and dispute
# resolution.
RACK_PRICE_RETENTION_DAYS: int = 90


async def refresh_rack_prices() -> int:
    """Refresh OPIS rack prices from the external data feed.

    NOTE: The actual OPIS API integration is out of scope for the
    fuel-compliance-backbone spec. OPIS is a commercial data feed
    (Oil Price Information Service) that requires:
    - A paid subscription agreement
    - API credentials provisioning
    - Terminal-specific product mapping configuration

    This placeholder logs a message and returns 0 (no prices
    refreshed). When the OPIS connector is implemented, this function
    will:
    1. Fetch the latest rack prices for all configured terminals
    2. Index them into the ``rack_prices`` ES index with timestamps
    3. Purge records older than ``RACK_PRICE_RETENTION_DAYS``
    4. Return the count of prices refreshed

    Returns:
        The number of rack prices refreshed (0 until OPIS integration
        is implemented).

    Validates: Requirement 11.6
    """
    logger.info(
        "OPIS rack price refresh not yet connected — "
        "actual OPIS API integration is out of scope for this spec. "
        "Retention policy: %d days.",
        RACK_PRICE_RETENTION_DAYS,
    )
    return 0
