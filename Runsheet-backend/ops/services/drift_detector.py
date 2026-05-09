"""
Drift Detector for the Ops Intelligence Layer.

Compares Dinee source state against Runsheet read-model state (Elasticsearch)
to detect data divergence. Supports shipment count/status comparison and rider
status comparison for a given tenant and time range.

Additionally supports per-channel order drift detection (design §11):
- A ``DriftSourceRegistry`` maps ``channel_type → DriftSourceAdapter``
- Channels without a registered adapter emit ``drift_api_unavailable``
- Compares via ``_compare_orders(source, es, channel_id)`` and emits
  ``DivergentRecord(entity_type="order", ...)``

Logs divergent records with entity_id, expected state, and actual state.
Emits WARN alert when drift exceeds a configurable threshold (default 1%).

Validates: Requirements 25.1-25.6, 7.1.1-7.1.5
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

import httpx
from pydantic import BaseModel

from config.settings import Settings
from fuel.services.order_metrics import orders_drift_alert_total
from ops.middleware.tenant_guard import inject_tenant_filter
from ops.services.ops_es_service import OpsElasticsearchService
from ops.services.ops_metrics import ops_drift_percentage, REGISTRY

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DriftSourceAdapter protocol + registry
# ---------------------------------------------------------------------------

class DriftSourceAdapter(Protocol):
    """Protocol for upstream drift source adapters.

    Each adapter knows how to fetch upstream orders for a specific
    channel type within a time range.
    """

    async def fetch_upstream_orders(
        self,
        channel: Any,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        """Return upstream orders for the given channel and time range.

        Each returned dict MUST contain at minimum:
        - ``order_id``: str
        - ``status``: str
        """
        ...


class DriftSourceRegistry:
    """Maps channel_type → DriftSourceAdapter.

    Channels without a registered adapter report ``drift_api_unavailable``
    and the detector continues with other channels.
    """

    def __init__(self) -> None:
        self._adapters: Dict[str, DriftSourceAdapter] = {}

    def register(self, channel_type: str, adapter: DriftSourceAdapter) -> None:
        """Register a drift source adapter for a channel type."""
        self._adapters[channel_type] = adapter

    def get_or_none(self, channel_type: str) -> Optional[DriftSourceAdapter]:
        """Return the adapter for the channel type, or None if unavailable."""
        return self._adapters.get(channel_type)

    @property
    def registered_types(self) -> List[str]:
        """Return all registered channel types."""
        return list(self._adapters.keys())


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class DivergentRecord(BaseModel):
    """A single record where Dinee and Runsheet state disagree."""
    entity_id: str
    entity_type: str  # "shipment" or "rider"
    field: str
    expected: Optional[str] = None  # Dinee value
    actual: Optional[str] = None    # Runsheet value


class DriftResult(BaseModel):
    """Result of a drift detection run."""
    tenant_id: str
    checked_at: datetime
    shipment_count_dinee: int = 0
    shipment_count_runsheet: int = 0
    rider_count_dinee: int = 0
    rider_count_runsheet: int = 0
    divergent_shipments: List[Dict[str, Any]] = []
    divergent_riders: List[Dict[str, Any]] = []
    # Per-channel order drift (design §11)
    channel_statuses: Dict[str, str] = {}
    divergent_orders: List[Dict[str, Any]] = []
    drift_percentage: float = 0.0
    alert_triggered: bool = False


# ---------------------------------------------------------------------------
# DriftDetector service
# ---------------------------------------------------------------------------

class DriftDetector:
    """
    Compares Dinee source state against Runsheet Elasticsearch read-model.

    Validates:
    - Req 25.1: Compare shipment counts/statuses between Dinee API and
      shipments_current for a tenant + time range.
    - Req 25.2: Compare rider statuses between Dinee API and riders_current
      for a tenant.
    - Req 25.3: Log divergent records with entity_id, expected state, actual state.
    - Req 25.5: Support scheduled runs at configurable interval (default 6h).
    - Req 25.6: Emit WARN when drift exceeds configurable threshold (default 1%).
    - Req 7.1.1-7.1.5: Per-channel order drift detection with
      DriftSourceRegistry, _compare_orders, and entity_type buckets.
    """

    DEFAULT_THRESHOLD_PCT: float = 1.0
    DEFAULT_SCHEDULE_INTERVAL_HOURS: int = 6

    def __init__(
        self,
        ops_es: OpsElasticsearchService,
        settings: Settings,
        *,
        threshold_pct: Optional[float] = None,
        schedule_interval_hours: Optional[int] = None,
        intake_channel_repo: Optional[Any] = None,
        drift_source_registry: Optional[DriftSourceRegistry] = None,
    ):
        self._ops_es = ops_es
        self._settings = settings
        self.threshold_pct = (
            threshold_pct
            if threshold_pct is not None
            else self.DEFAULT_THRESHOLD_PCT
        )
        self.schedule_interval_hours = (
            schedule_interval_hours
            if schedule_interval_hours is not None
            else self.DEFAULT_SCHEDULE_INTERVAL_HOURS
        )
        self._intake_channel_repo = intake_channel_repo
        self._drift_source_registry = (
            drift_source_registry
            if drift_source_registry is not None
            else DriftSourceRegistry()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_detection(
        self,
        tenant_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> DriftResult:
        """
        Execute a full drift detection run for *tenant_id*.

        Validates: Req 25.1-25.6, 7.1.1-7.1.5
        """
        now = datetime.now(timezone.utc)
        result = DriftResult(tenant_id=tenant_id, checked_at=now)

        # --- Shipment drift (Req 25.1) — preserved during deprecation ---
        dinee_shipments = await self._fetch_dinee_shipments(
            tenant_id, start_time, end_time,
        )
        es_shipments = await self._fetch_es_shipments(
            tenant_id, start_time, end_time,
        )

        result.shipment_count_dinee = len(dinee_shipments)
        result.shipment_count_runsheet = len(es_shipments)
        result.divergent_shipments = self._compare_shipments(
            dinee_shipments, es_shipments,
        )

        # --- Rider drift (Req 25.2) — preserved during deprecation ---
        dinee_riders = await self._fetch_dinee_riders(tenant_id)
        es_riders = await self._fetch_es_riders(tenant_id)

        result.rider_count_dinee = len(dinee_riders)
        result.rider_count_runsheet = len(es_riders)
        result.divergent_riders = self._compare_riders(
            dinee_riders, es_riders,
        )

        # --- Per-channel order drift (Req 7.1.1-7.1.5, design §11) ---
        await self._run_order_drift(tenant_id, start_time, end_time, result)

        # --- Calculate drift percentage ---
        # Include all entity types: shipments (legacy), riders (legacy/driver),
        # and orders (new)
        total_entities = max(
            result.shipment_count_dinee + result.rider_count_dinee, 1,
        )
        total_divergent = (
            len(result.divergent_shipments) + len(result.divergent_riders)
        )
        result.drift_percentage = round(
            (total_divergent / total_entities) * 100, 2,
        )

        # Update Prometheus gauge (Req 23.6)
        ops_drift_percentage.labels(tenant_id=tenant_id).set(result.drift_percentage)

        # --- Log divergent records (Req 25.3) ---
        for rec in result.divergent_shipments:
            logger.info(
                "Drift detected — shipment divergence: entity_id=%s "
                "field=%s expected=%s actual=%s tenant=%s",
                rec.get("entity_id"),
                rec.get("field"),
                rec.get("expected"),
                rec.get("actual"),
                tenant_id,
            )
        for rec in result.divergent_riders:
            logger.info(
                "Drift detected — rider divergence: entity_id=%s "
                "field=%s expected=%s actual=%s tenant=%s",
                rec.get("entity_id"),
                rec.get("field"),
                rec.get("expected"),
                rec.get("actual"),
                tenant_id,
            )
        for rec in result.divergent_orders:
            logger.info(
                "Drift detected — order divergence: entity_id=%s "
                "field=%s expected=%s actual=%s tenant=%s channel_id=%s",
                rec.get("entity_id"),
                rec.get("field"),
                rec.get("expected"),
                rec.get("actual"),
                tenant_id,
                rec.get("channel_id"),
            )

        # --- Emit WARN alert if threshold exceeded (Req 25.6) ---
        if result.drift_percentage > self.threshold_pct:
            result.alert_triggered = True
            logger.warning(
                "DRIFT THRESHOLD EXCEEDED: tenant=%s drift=%.2f%% "
                "threshold=%.2f%% divergent_shipments=%d divergent_riders=%d",
                tenant_id,
                result.drift_percentage,
                self.threshold_pct,
                len(result.divergent_shipments),
                len(result.divergent_riders),
            )

        return result

    # ------------------------------------------------------------------
    # Per-channel order drift (design §11)
    # ------------------------------------------------------------------

    async def _run_order_drift(
        self,
        tenant_id: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        result: DriftResult,
    ) -> None:
        """Execute per-channel order drift detection.

        For each enabled intake channel:
        - If no DriftSourceAdapter is registered for the channel_type,
          emit ``drift_api_unavailable`` on channel_statuses and continue.
        - Otherwise, fetch upstream orders and ES orders, compare, and
          emit DivergentRecord entries with entity_type="order".
        - When drift_percentage for a channel exceeds the configured
          threshold, emit the ``orders_drift_alert_total`` metric and
          log a WARN.

        Validates: Req 7.1.1, 7.1.2, 7.1.3, 7.1.4, 7.1.5
        """
        if self._intake_channel_repo is None:
            return

        try:
            channels = await self._intake_channel_repo.list_for_tenant(tenant_id)
        except Exception as exc:
            logger.warning(
                "Order drift: failed to list channels for tenant=%s: %s",
                tenant_id, exc,
            )
            return

        for channel in channels:
            channel_id = channel.channel_id if hasattr(channel, "channel_id") else channel.get("channel_id", "")
            channel_type = channel.channel_type if hasattr(channel, "channel_type") else channel.get("channel_type", "")

            adapter = self._drift_source_registry.get_or_none(channel_type)
            if adapter is None:
                result.channel_statuses[channel_id] = "drift_api_unavailable"
                continue

            try:
                source_orders = await adapter.fetch_upstream_orders(
                    channel, start_time, end_time,
                )
            except Exception as exc:
                logger.warning(
                    "Order drift: adapter fetch failed for channel=%s "
                    "tenant=%s: %s",
                    channel_id, tenant_id, exc,
                )
                result.channel_statuses[channel_id] = "drift_api_unavailable"
                continue

            es_orders = await self._fetch_es_orders(
                tenant_id, channel_id, start_time, end_time,
            )

            divergences = self._compare_orders(
                source_orders, es_orders, channel_id,
            )
            result.divergent_orders.extend(divergences)

            # Per-channel drift percentage check (Req 7.1.4)
            total_channel = max(len(source_orders), 1)
            channel_drift_pct = (len(divergences) / total_channel) * 100

            if channel_drift_pct > self.threshold_pct:
                orders_drift_alert_total.labels(
                    tenant_id=tenant_id, channel_id=channel_id,
                ).inc()
                logger.warning(
                    "ORDER DRIFT THRESHOLD EXCEEDED: tenant=%s channel=%s "
                    "drift=%.2f%% threshold=%.2f%% divergent_orders=%d",
                    tenant_id,
                    channel_id,
                    channel_drift_pct,
                    self.threshold_pct,
                    len(divergences),
                )
                result.channel_statuses[channel_id] = "drift_alert"
            else:
                result.channel_statuses[channel_id] = "ok"

    # ------------------------------------------------------------------
    # Order comparison (Req 7.1.2, 7.1.3)
    # ------------------------------------------------------------------

    def _compare_orders(
        self,
        source_orders: List[Dict[str, Any]],
        es_orders: List[Dict[str, Any]],
        channel_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Compare order records between upstream source and Runsheet ES.

        Catches three divergence shapes:
        - missing-upstream: order exists in ES but not in the upstream source
        - missing-runsheet: order exists upstream but not in ES
        - status-mismatch: order exists in both but status differs

        Validates: Req 7.1.2, 7.1.3
        """
        divergent: List[Dict[str, Any]] = []

        source_by_id: Dict[str, Dict[str, Any]] = {
            o.get("order_id", ""): o for o in source_orders
        }
        es_by_id: Dict[str, Dict[str, Any]] = {
            o.get("order_id", o.get("_id", "")): o for o in es_orders
        }

        # Orders in upstream source but missing in Runsheet (missing-runsheet)
        for oid, source_rec in source_by_id.items():
            if oid not in es_by_id:
                divergent.append(
                    DivergentRecord(
                        entity_id=oid,
                        entity_type="order",
                        field="presence",
                        expected="exists",
                        actual="missing",
                    ).model_dump()
                )
                continue

            # Status mismatch
            es_rec = es_by_id[oid]
            source_status = source_rec.get("status")
            es_status = es_rec.get("status")
            if source_status and es_status and source_status != es_status:
                divergent.append(
                    DivergentRecord(
                        entity_id=oid,
                        entity_type="order",
                        field="status",
                        expected=str(source_status),
                        actual=str(es_status),
                    ).model_dump()
                )

        # Orders in Runsheet but missing in upstream (missing-upstream)
        for oid in es_by_id:
            if oid not in source_by_id:
                divergent.append(
                    DivergentRecord(
                        entity_id=oid,
                        entity_type="order",
                        field="presence",
                        expected="missing",
                        actual="exists",
                    ).model_dump()
                )

        return divergent

    # ------------------------------------------------------------------
    # ES fetcher for orders (per-channel)
    # ------------------------------------------------------------------

    async def _fetch_es_orders(
        self,
        tenant_id: str,
        channel_id: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        """Fetch orders from fuel_orders_current for a specific channel."""
        from fuel.services.order_es_mappings import FUEL_ORDERS_CURRENT_INDEX

        query: Dict[str, Any] = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"intake_channel_id": channel_id}},
                    ]
                }
            }
        }

        # Apply tenant filter
        query = inject_tenant_filter(query, tenant_id)

        # Add optional time-range filter
        if start_time or end_time:
            range_filter: Dict[str, Any] = {}
            if start_time:
                range_filter["gte"] = start_time.isoformat()
            if end_time:
                range_filter["lte"] = end_time.isoformat()
            query["query"]["bool"]["filter"].append(
                {"range": {"updated_at": range_filter}}
            )

        return await self._scroll_es_index(FUEL_ORDERS_CURRENT_INDEX, query)

    # ------------------------------------------------------------------
    # Dinee API fetchers
    # ------------------------------------------------------------------

    async def _fetch_dinee_shipments(
        self,
        tenant_id: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        """Fetch shipments from the Dinee REST API for *tenant_id*."""
        base_url = self._settings.dinee_api_base_url
        api_key = self._settings.dinee_api_key
        if not base_url or not api_key:
            logger.warning(
                "Dinee API not configured (missing base_url or api_key). "
                "Skipping Dinee shipment fetch for tenant=%s",
                tenant_id,
            )
            return []

        params: Dict[str, str] = {"tenant_id": tenant_id}
        if start_time:
            params["start_time"] = start_time.isoformat()
        if end_time:
            params["end_time"] = end_time.isoformat()

        url = f"{base_url.rstrip('/')}/shipments"
        return await self._paginated_get(url, api_key, params)

    async def _fetch_dinee_riders(
        self,
        tenant_id: str,
    ) -> List[Dict[str, Any]]:
        """Fetch riders from the Dinee REST API for *tenant_id*."""
        base_url = self._settings.dinee_api_base_url
        api_key = self._settings.dinee_api_key
        if not base_url or not api_key:
            logger.warning(
                "Dinee API not configured (missing base_url or api_key). "
                "Skipping Dinee rider fetch for tenant=%s",
                tenant_id,
            )
            return []

        params: Dict[str, str] = {"tenant_id": tenant_id}
        url = f"{base_url.rstrip('/')}/riders"
        return await self._paginated_get(url, api_key, params)

    async def _paginated_get(
        self,
        url: str,
        api_key: str,
        params: Dict[str, str],
        *,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all pages from a Dinee REST endpoint.

        Expects the Dinee API to return ``{"data": [...], "has_more": bool}``.
        """
        all_records: List[Dict[str, Any]] = []
        page = 1

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                request_params = {
                    **params,
                    "page": str(page),
                    "page_size": str(page_size),
                }
                try:
                    resp = await client.get(
                        url,
                        params=request_params,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Accept": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "Dinee API HTTP error: url=%s status=%s tenant=%s",
                        url,
                        exc.response.status_code,
                        params.get("tenant_id"),
                    )
                    break
                except httpx.RequestError as exc:
                    logger.error(
                        "Dinee API request error: url=%s error=%s tenant=%s",
                        url,
                        str(exc),
                        params.get("tenant_id"),
                    )
                    break

                data = body.get("data", [])
                all_records.extend(data)

                if not body.get("has_more", False):
                    break
                page += 1

        return all_records


    # ------------------------------------------------------------------
    # Elasticsearch fetchers
    # ------------------------------------------------------------------

    async def _fetch_es_shipments(
        self,
        tenant_id: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        """Fetch shipments from the local shipments_current ES index."""
        query: Dict[str, Any] = {"query": {"match_all": {}}}

        # Apply tenant filter (Req 9.2)
        query = inject_tenant_filter(query, tenant_id)

        # Add optional time-range filter
        if start_time or end_time:
            range_filter: Dict[str, Any] = {}
            if start_time:
                range_filter["gte"] = start_time.isoformat()
            if end_time:
                range_filter["lte"] = end_time.isoformat()
            query["query"]["bool"]["filter"].append(
                {"range": {"updated_at": range_filter}}
            )

        return await self._scroll_es_index(
            OpsElasticsearchService.SHIPMENTS_CURRENT, query,
        )

    async def _fetch_es_riders(
        self,
        tenant_id: str,
    ) -> List[Dict[str, Any]]:
        """Fetch riders from the local riders_current ES index."""
        query: Dict[str, Any] = {"query": {"match_all": {}}}
        query = inject_tenant_filter(query, tenant_id)

        return await self._scroll_es_index(
            OpsElasticsearchService.RIDERS_CURRENT, query,
        )

    async def _scroll_es_index(
        self,
        index: str,
        query: Dict[str, Any],
        *,
        page_size: int = 500,
    ) -> List[Dict[str, Any]]:
        """Scroll through an ES index and return all matching documents."""
        records: List[Dict[str, Any]] = []
        es = self._ops_es.client

        try:
            body = {**query, "size": page_size}
            resp = es.search(index=index, body=body, scroll="2m")
            scroll_id = resp.get("_scroll_id")
            hits = resp.get("hits", {}).get("hits", [])

            while hits:
                for hit in hits:
                    doc = hit.get("_source", {})
                    doc["_id"] = hit.get("_id")
                    records.append(doc)

                if not scroll_id:
                    break
                resp = es.scroll(scroll_id=scroll_id, scroll="2m")
                hits = resp.get("hits", {}).get("hits", [])

            # Clean up scroll context
            if scroll_id:
                try:
                    es.clear_scroll(scroll_id=scroll_id)
                except Exception:
                    pass  # best-effort cleanup

        except Exception as exc:
            logger.error(
                "ES scroll error: index=%s error=%s", index, str(exc),
            )

        return records

    # ------------------------------------------------------------------
    # Comparison logic
    # ------------------------------------------------------------------

    def _compare_shipments(
        self,
        dinee_shipments: List[Dict[str, Any]],
        es_shipments: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Compare shipment records between Dinee and Runsheet.

        Checks:
        - Shipments present in Dinee but missing in Runsheet
        - Status mismatches for shipments present in both
        Validates: Req 25.1, 25.3
        """
        divergent: List[Dict[str, Any]] = []

        es_by_id: Dict[str, Dict[str, Any]] = {
            s.get("shipment_id", s.get("_id", "")): s for s in es_shipments
        }
        dinee_by_id: Dict[str, Dict[str, Any]] = {
            s.get("shipment_id", ""): s for s in dinee_shipments
        }

        # Shipments in Dinee but missing in Runsheet
        for sid, dinee_rec in dinee_by_id.items():
            if sid not in es_by_id:
                divergent.append(
                    DivergentRecord(
                        entity_id=sid,
                        entity_type="shipment",
                        field="presence",
                        expected="exists",
                        actual="missing",
                    ).model_dump()
                )
                continue

            # Status mismatch
            es_rec = es_by_id[sid]
            dinee_status = dinee_rec.get("status")
            es_status = es_rec.get("status")
            if dinee_status and es_status and dinee_status != es_status:
                divergent.append(
                    DivergentRecord(
                        entity_id=sid,
                        entity_type="shipment",
                        field="status",
                        expected=str(dinee_status),
                        actual=str(es_status),
                    ).model_dump()
                )

        # Shipments in Runsheet but missing in Dinee
        for sid in es_by_id:
            if sid not in dinee_by_id:
                divergent.append(
                    DivergentRecord(
                        entity_id=sid,
                        entity_type="shipment",
                        field="presence",
                        expected="missing",
                        actual="exists",
                    ).model_dump()
                )

        return divergent

    def _compare_riders(
        self,
        dinee_riders: List[Dict[str, Any]],
        es_riders: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Compare rider records between Dinee and Runsheet.

        Checks:
        - Riders present in Dinee but missing in Runsheet
        - Status mismatches for riders present in both
        Validates: Req 25.2, 25.3
        """
        divergent: List[Dict[str, Any]] = []

        es_by_id: Dict[str, Dict[str, Any]] = {
            r.get("rider_id", r.get("_id", "")): r for r in es_riders
        }
        dinee_by_id: Dict[str, Dict[str, Any]] = {
            r.get("rider_id", ""): r for r in dinee_riders
        }

        # Riders in Dinee but missing in Runsheet
        for rid, dinee_rec in dinee_by_id.items():
            if rid not in es_by_id:
                divergent.append(
                    DivergentRecord(
                        entity_id=rid,
                        entity_type="rider",
                        field="presence",
                        expected="exists",
                        actual="missing",
                    ).model_dump()
                )
                continue

            # Status mismatch
            es_rec = es_by_id[rid]
            dinee_status = dinee_rec.get("status")
            es_status = es_rec.get("status")
            if dinee_status and es_status and dinee_status != es_status:
                divergent.append(
                    DivergentRecord(
                        entity_id=rid,
                        entity_type="rider",
                        field="status",
                        expected=str(dinee_status),
                        actual=str(es_status),
                    ).model_dump()
                )

        # Riders in Runsheet but missing in Dinee
        for rid in es_by_id:
            if rid not in dinee_by_id:
                divergent.append(
                    DivergentRecord(
                        entity_id=rid,
                        entity_type="rider",
                        field="presence",
                        expected="missing",
                        actual="exists",
                    ).model_dump()
                )

        return divergent


# ---------------------------------------------------------------------------
# Module-level reference & configure function
# ---------------------------------------------------------------------------

_drift_detector: Optional[DriftDetector] = None


def configure_drift_detector(
    *,
    ops_es: OpsElasticsearchService,
    settings: Settings,
    threshold_pct: Optional[float] = None,
    schedule_interval_hours: Optional[int] = None,
    intake_channel_repo: Optional[Any] = None,
    drift_source_registry: Optional[DriftSourceRegistry] = None,
) -> DriftDetector:
    """
    Wire the module-level DriftDetector instance.

    Called during application startup so that endpoint handlers and
    scheduled jobs can access the shared detector without circular imports.
    """
    global _drift_detector
    _drift_detector = DriftDetector(
        ops_es=ops_es,
        settings=settings,
        threshold_pct=threshold_pct,
        schedule_interval_hours=schedule_interval_hours,
        intake_channel_repo=intake_channel_repo,
        drift_source_registry=drift_source_registry,
    )
    return _drift_detector


def get_drift_detector() -> DriftDetector:
    """Return the configured DriftDetector or raise."""
    if _drift_detector is None:
        raise RuntimeError(
            "DriftDetector not configured. Call configure_drift_detector() during startup."
        )
    return _drift_detector
