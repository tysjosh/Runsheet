"""
ES-backed accumulated-HDD provider for K-factor variance.

The K-Factor Calibration Service needs accumulated Heating Degree Days
(HDD) between two deliveries to compute predicted-vs-actual variance. The
production weather adapters (NOAA / OpenWeather) fetch from external APIs
and *persist* every daily observation to the ``weather_observations`` ES
index. When no external adapter is configured (e.g. local/dev, or any
deployment without weather API credentials) the calibration feature would
otherwise be dead because ``weather_provider`` is ``None``.

This provider closes that gap: it reads the already-persisted
``weather_observations`` rows and sums their ``hdd`` over the requested
window. It exposes the ``get_accumulated_hdd(zip, from, to, *, tenant_id)``
interface the calibration service prefers, so it slots in without any
change to the service.

It is intentionally read-only and side-effect free — a safe default that
reuses whatever observations the real adapters (or a seed) have written.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fuel.services.fuel_ops_es_mappings import WEATHER_OBSERVATIONS_INDEX
from ops.middleware.tenant_guard import inject_tenant_filter

logger = logging.getLogger(__name__)

# Generous per-window cap; a delivery interval spans at most a few months.
_MAX_OBSERVATION_ROWS = 400


class EsHddProvider:
    """Sums ``weather_observations.hdd`` over a date window for a ZIP.

    Args:
        es_service: Elasticsearch handle exposing ``search_documents``.
    """

    def __init__(self, es_service: Any) -> None:
        self._es = es_service

    async def get_accumulated_hdd(
        self,
        zip_code: str,
        from_date: date,
        to_date: date,
        *,
        tenant_id: str,
    ) -> float:
        """Return summed HDD for ``zip_code`` over ``[from_date, to_date)``.

        Tenant-scoped via ``inject_tenant_filter``. Returns ``0.0`` when no
        observations are found (the caller treats non-positive HDD as
        "cannot score this delivery" and skips it).
        """
        query = {
            "query": {
                "bool": {
                    "must": [{"term": {"zip_code": zip_code}}],
                    "filter": [
                        {
                            "range": {
                                "date": {
                                    "gte": from_date.isoformat(),
                                    "lt": to_date.isoformat(),
                                }
                            }
                        }
                    ],
                }
            },
            "size": _MAX_OBSERVATION_ROWS,
        }
        query = inject_tenant_filter(query, tenant_id)

        try:
            resp = await self._es.search_documents(
                WEATHER_OBSERVATIONS_INDEX, query, _MAX_OBSERVATION_ROWS
            )
        except Exception as exc:
            logger.warning(
                "EsHddProvider: weather_observations query failed for "
                "zip=%s tenant=%s: %s",
                zip_code,
                tenant_id,
                exc,
            )
            return 0.0

        hits = (resp or {}).get("hits", {}).get("hits", [])
        total = 0.0
        for hit in hits:
            source = hit.get("_source", {}) or {}
            if source.get("tenant_id") != tenant_id:
                continue
            hdd = source.get("hdd")
            if isinstance(hdd, (int, float)):
                total += float(hdd)
        return total


__all__ = ["EsHddProvider"]
