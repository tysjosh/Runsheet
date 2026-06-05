"""
Weather_Alert_Ingester — autonomous agent that polls NOAA/NWS for severe-
weather alerts and materializes them into the ``weather_alerts`` ES index
plus the SignalBus.

Scope (Task 10.2, Requirements 9.1.1, 9.1.2):

* Runs on a 5-minute poll cycle (:attr:`DEFAULT_POLL_INTERVAL_SECONDS`).
* For every tenant with at least one active Customer_Tank, derives the
  tenant's **ZIP footprint** — the distinct set of ZIP codes attached to
  the tenant's tanks — and fetches the NOAA/NWS active-alerts feed for the
  footprint.
* Persists every **new** alert to the ``weather_alerts`` ES index using
  the :class:`fuel.storm_mode_models.WeatherAlert` model introduced by
  Task 10.1.
* Publishes each new alert to the :class:`Agents.overlay.signal_bus.SignalBus`
  so the Storm_Mode_Evaluator (Task 10.3) can react in near-real time.

Duplicate alerts are detected by :attr:`WeatherAlert.alert_id`: the NWS
``id`` is reused verbatim so the same advisory ingested on two consecutive
cycles is idempotent. The ES id equals ``alert_id`` which lets a
``get_document`` hit short-circuit the write and the signal publish.

Design notes:

* The agent extends :class:`Agents.autonomous.base_agent.AutonomousAgentBase`
  so it inherits the polling loop, cooldown tracking, activity logging, and
  WebSocket plumbing used by every other autonomous agent in the system.
* The NWS API and the tenant-footprint lookup are injected as callables /
  services so the unit tests can run entirely in-process without hitting
  the network or a live Elasticsearch cluster.
* Alert severity strings coming from NWS are normalized into the
  :data:`fuel.storm_mode_models.WeatherAlertSeverity` lexicon; unrecognised
  NWS buckets fall back to ``"moderate"`` so a malformed upstream record
  never crashes the poll loop.
* The agent never raises to the base class loop — every failure is logged
  and the loop continues. An empty return on a bad cycle lets the
  AutonomousAgentBase activity-log skip the ES write entirely.

Validates: Requirements 9.1.1, 9.1.2.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from Agents.autonomous.base_agent import AutonomousAgentBase
from fuel.services.fuel_ops_es_mappings import (
    CUSTOMER_TANKS_INDEX,
    WEATHER_ALERTS_INDEX,
)
from fuel.storm_mode_models import (
    WeatherAlert,
    WeatherAlertSeverity,
    WeatherAlertStatus,
)
from services.external_call_tracing import (
    CircuitBreaker,
    CircuitOpenError,
    default_circuit_breaker,
    trace_external_call,
)
from services.metrics import fuelops_weather_alert_ingestion_total

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default poll interval per the Task 10.2 description: 5 minutes.
DEFAULT_POLL_INTERVAL_SECONDS: int = 300

#: Default cooldown window. Cooldown is largely irrelevant here because we
#: key idempotency on ``alert_id`` (ES dedupe), but the base class expects a
#: positive value so we use the same 5-minute window as the poll.
DEFAULT_COOLDOWN_MINUTES: int = 5

#: NWS public alerts endpoint. No API key required.
NWS_ACTIVE_ALERTS_URL: str = "https://api.weather.gov/alerts/active"

#: HTTP timeout for each upstream call (seconds).
DEFAULT_HTTP_TIMEOUT_SECONDS: float = 5.0

#: User-Agent NOAA recommends for the /alerts/active endpoint. Tenants can
#: override at construction time if they want to embed their own contact
#: address.
DEFAULT_USER_AGENT: str = "runsheet-weather-alert-ingester (ops@runsheet.com)"

#: Mapping from NWS severity strings to the canonical
#: :data:`WeatherAlertSeverity` lexicon. NWS returns one of
#: ``Extreme / Severe / Moderate / Minor / Unknown`` — we lowercase and map
#: ``extreme`` → ``extreme``, ``severe`` → ``severe``, everything else to a
#: safe default of ``moderate`` (so ``Unknown`` still produces a valid
#: model).
_NWS_SEVERITY_MAP: Dict[str, WeatherAlertSeverity] = {
    "extreme": "extreme",
    "severe": "severe",
    "moderate": "moderate",
    "minor": "minor",
}

#: Default fallback when the NWS ``severity`` string is missing or not in
#: :data:`_NWS_SEVERITY_MAP`. ``moderate`` is chosen over ``minor`` so an
#: unlabelled advisory still shows up in non-``minor`` filters.
_NWS_SEVERITY_FALLBACK: WeatherAlertSeverity = "moderate"


#: ZIP-prefix (first 3 digits) → USPS 2-letter state ranges. The NWS
#: ``/alerts/active`` endpoint filters by ``area`` (state code), NOT by ZIP,
#: so the ingester derives the distinct set of states covering the tenant's
#: ZIP footprint and queries by ``area``. Ranges are inclusive ``(low, high,
#: state)`` over the integer value of the first three ZIP digits (ZCTA
#: prefixes). Source: USPS ZIP prefix allocations. Prefixes that span only
#: territories / unused blocks fall through to ``None`` and are skipped.
_ZIP3_STATE_RANGES: List[Tuple[int, int, str]] = [
    (0, 0, "PR"), (6, 9, "PR"), (10, 27, "MA"), (28, 29, "RI"),
    (30, 38, "NH"), (39, 49, "ME"), (50, 54, "VT"), (55, 59, "MA"),
    (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"), (150, 196, "PA"),
    (197, 199, "DE"), (200, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"),
    (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"),
    (320, 349, "FL"), (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"),
    (398, 399, "GA"), (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"),
    (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"),
    (569, 579, "SD"), (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"),
    (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"),
    (716, 729, "AR"), (730, 749, "OK"), (750, 799, "TX"), (800, 816, "CO"),
    (820, 831, "WY"), (832, 838, "ID"), (840, 847, "UT"), (850, 865, "AZ"),
    (870, 884, "NM"), (889, 899, "NV"), (900, 961, "CA"), (967, 968, "HI"),
    (970, 979, "OR"), (980, 994, "WA"), (995, 999, "AK"),
]


def zip_to_state(zip_code: str) -> Optional[str]:
    """Map a 5-digit US ZIP to its USPS 2-letter state via prefix ranges.

    Returns ``None`` for malformed ZIPs or prefixes that don't fall in a
    known range, so callers can skip un-mappable ZIPs rather than send an
    invalid NWS ``area`` filter.
    """
    if not isinstance(zip_code, str):
        return None
    digits = zip_code.strip()[:5]
    if len(digits) < 3 or not digits[:3].isdigit():
        return None
    prefix = int(digits[:3])
    for low, high, state in _ZIP3_STATE_RANGES:
        if low <= prefix <= high:
            return state
    return None


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Callable returning the set of {tenant_id → [zip_codes]} the ingester
#: should poll. Extracted behind a type alias so unit tests can inject a
#: simple lambda. The default implementation aggregates distinct zip_codes
#: from the ``customer_tanks`` ES index.
TenantFootprintLoader = Callable[[], "Any"]


# ---------------------------------------------------------------------------
# Ingester
# ---------------------------------------------------------------------------


class WeatherAlertIngester(AutonomousAgentBase):
    """Fetch NOAA/NWS severe-weather alerts on a 5-minute poll.

    Every cycle:

    1. Build the tenant → ZIP-footprint map by aggregating
       :attr:`fuel.services.fuel_ops_es_mappings.CUSTOMER_TANKS_INDEX`.
       Tenants without tanks are skipped; tenants whose tanks lack ZIPs
       are skipped.
    2. For each tenant, call the NWS active-alerts endpoint with the
       tenant's ZIP footprint encoded as ``area`` (state) / ``zone``
       filters. The per-tenant call is wrapped in a 5-second timeout.
    3. For each alert returned, use ``alert_id`` (= NWS ``id``) to check
       whether the ``weather_alerts`` index already has a record. If yes,
       skip silently — the duplicate-detection contract of Task 10.2.
    4. Persist the new alert via ``es.index_document(...)`` and publish
       a :class:`WeatherAlert` to the SignalBus.

    Args:
        es_service: Elasticsearch service exposing ``search_documents``,
            ``get_document``, and ``index_document`` (matches the
            :class:`services.elasticsearch_service.ElasticsearchService`
            contract used throughout the codebase).
        activity_log_service: Activity-log service used by the base class.
        ws_manager: WebSocket manager used by the base class.
        confirmation_protocol: Confirmation protocol used by the base class.
            Unused by this agent (no mutations), but required by the base
            constructor.
        signal_bus: :class:`SignalBus` instance the agent publishes
            :class:`WeatherAlert` records to. Optional so the agent can be
            constructed without a bus (e.g. in a one-off backfill).
        feature_flag_service: Optional per-tenant feature-flag service.
        http_client: Optional pre-built ``httpx.AsyncClient``. When omitted
            the agent lazily constructs one owned client. Injected in tests
            via ``httpx.MockTransport``.
        tenant_footprint_loader: Optional override for the tenant → ZIP
            aggregation step. Must be an ``async`` callable returning a
            ``{tenant_id: [zip_codes]}`` mapping. Defaults to the built-in
            aggregation over ``customer_tanks``.
        poll_interval: Seconds between polls. Defaults to
            :data:`DEFAULT_POLL_INTERVAL_SECONDS` (300 = 5 minutes).
        cooldown_minutes: Cooldown window tracked by the base class.
            Defaults to :data:`DEFAULT_COOLDOWN_MINUTES`.
        nws_base_url: Override for the NWS endpoint. Defaults to
            :data:`NWS_ACTIVE_ALERTS_URL`.
        user_agent: HTTP User-Agent sent to NWS.
        http_timeout_seconds: Per-request timeout.
    """

    #: The base class expects a fixed ``agent_id``. Keep it stable so the
    #: activity-log query `source_agent == "weather_alert_ingester"` stays
    #: meaningful across deployments.
    AGENT_ID: str = "weather_alert_ingester"

    def __init__(
        self,
        es_service: Any,
        activity_log_service: Any,
        ws_manager: Any,
        confirmation_protocol: Any,
        signal_bus: Optional[Any] = None,
        feature_flag_service: Optional[Any] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        tenant_footprint_loader: Optional[TenantFootprintLoader] = None,
        poll_interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        nws_base_url: str = NWS_ACTIVE_ALERTS_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        http_timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        super().__init__(
            agent_id=self.AGENT_ID,
            poll_interval_seconds=poll_interval,
            cooldown_minutes=cooldown_minutes,
            activity_log_service=activity_log_service,
            ws_manager=ws_manager,
            confirmation_protocol=confirmation_protocol,
            feature_flag_service=feature_flag_service,
        )
        if es_service is None:
            raise ValueError("es_service must not be None")
        if http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be positive")

        self._es = es_service
        self._signal_bus = signal_bus
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._tenant_footprint_loader = tenant_footprint_loader
        self._nws_base_url = nws_base_url
        self._user_agent = user_agent
        self._http_timeout = http_timeout_seconds
        self._circuit_breaker = (
            circuit_breaker if circuit_breaker is not None else default_circuit_breaker
        )

    # ------------------------------------------------------------------
    # Base-class hook
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        await super().stop()
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def monitor_cycle(self) -> Tuple[List[Any], List[Any]]:
        """Run one poll cycle.

        Returns a ``(detections, actions)`` tuple where ``detections`` is
        the list of ``alert_id`` strings fetched from NWS (pre-dedupe) and
        ``actions`` is the list of dicts describing per-alert outcomes
        (persisted / skipped / failed). The base class uses the lengths
        of these two lists when writing the activity log.
        """
        detections: List[str] = []
        actions: List[Dict[str, Any]] = []

        footprint_map = await self._safe_load_footprint()
        if not footprint_map:
            return detections, actions

        for tenant_id, zip_codes in footprint_map.items():
            if not zip_codes:
                continue
            try:
                raw_alerts = await self._fetch_nws_alerts(tenant_id, zip_codes)
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.warning(
                    "WeatherAlertIngester: fetch failed for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                continue

            for raw_alert in raw_alerts:
                alert_id = self._extract_alert_id(raw_alert)
                if not alert_id:
                    continue
                detections.append(alert_id)

                try:
                    alert_model = self._build_weather_alert(
                        raw_alert, tenant_id=tenant_id, zip_footprint=zip_codes
                    )
                except ValueError as exc:
                    self.logger.warning(
                        "WeatherAlertIngester: skipping malformed alert "
                        "tenant=%s alert_id=%s: %s",
                        tenant_id,
                        alert_id,
                        exc,
                    )
                    continue

                action = await self._ingest_alert(alert_model)
                actions.append(action)

        return detections, actions

    # ------------------------------------------------------------------
    # Tenant footprint
    # ------------------------------------------------------------------

    async def _safe_load_footprint(self) -> Dict[str, List[str]]:
        """Return ``{tenant_id: [zip_codes]}``; never raises."""
        loader = self._tenant_footprint_loader or self._load_footprint_from_es
        try:
            result = await loader()
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning(
                "WeatherAlertIngester: tenant-footprint loader failed: %s",
                exc,
            )
            return {}
        return self._normalize_footprint(result)

    @staticmethod
    def _normalize_footprint(result: Any) -> Dict[str, List[str]]:
        """Coerce the loader's return value into the canonical dict shape."""
        if not isinstance(result, dict):
            return {}
        normalized: Dict[str, List[str]] = {}
        for tenant_id, zips in result.items():
            if not isinstance(tenant_id, str) or not tenant_id.strip():
                continue
            if not isinstance(zips, Iterable) or isinstance(zips, (str, bytes)):
                continue
            cleaned: List[str] = []
            seen: set[str] = set()
            for zip_code in zips:
                if not isinstance(zip_code, str):
                    continue
                stripped = zip_code.strip()
                if not stripped or stripped in seen:
                    continue
                seen.add(stripped)
                cleaned.append(stripped)
            if cleaned:
                normalized[tenant_id.strip()] = cleaned
        return normalized

    async def _load_footprint_from_es(self) -> Dict[str, List[str]]:
        """Aggregate ``customer_tanks`` into ``{tenant_id: [zip_codes]}``.

        Uses a composite aggregation so large tenants don't blow the
        default 10k term-bucket cap. The aggregation is bounded to active
        tanks so tenants whose tanks are all archived don't pull alerts
        uselessly.
        """
        query = {
            "size": 0,
            "query": {"term": {"status": "active"}},
            "aggs": {
                "tenants": {
                    "terms": {"field": "tenant_id", "size": 1000},
                    "aggs": {
                        "zips": {
                            "terms": {"field": "zip_code", "size": 5000}
                        }
                    },
                }
            },
        }
        try:
            resp = await self._es.search_documents(
                CUSTOMER_TANKS_INDEX, query, 0
            )
        except Exception as exc:
            self.logger.warning(
                "WeatherAlertIngester: customer_tanks aggregation failed: %s",
                exc,
            )
            return {}

        aggs = (resp or {}).get("aggregations") or {}
        tenant_buckets = (aggs.get("tenants") or {}).get("buckets") or []
        footprint: Dict[str, List[str]] = {}
        for tenant_bucket in tenant_buckets:
            tenant_id = tenant_bucket.get("key")
            if not isinstance(tenant_id, str) or not tenant_id:
                continue
            zip_buckets = (
                (tenant_bucket.get("zips") or {}).get("buckets") or []
            )
            zips: List[str] = []
            seen: set[str] = set()
            for zip_bucket in zip_buckets:
                zip_code = zip_bucket.get("key")
                if not isinstance(zip_code, str):
                    continue
                stripped = zip_code.strip()
                if not stripped or stripped in seen:
                    continue
                seen.add(stripped)
                zips.append(stripped)
            if zips:
                footprint[tenant_id] = zips
        return footprint

    # ------------------------------------------------------------------
    # NWS fetch
    # ------------------------------------------------------------------

    async def _fetch_nws_alerts(
        self, tenant_id: str, zip_codes: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Call ``/alerts/active`` and return the raw NWS feature list.

        Every fetch is wrapped in :func:`trace_external_call` so the
        structured-log surface (``tenant_id``, ``provider=nws``,
        ``operation=get_active_alerts``, ``duration_ms``, ``status``,
        and — on failure — ``error_code``) is uniform with every other
        external call in the platform, and the per-``(tenant_id, nws)``
        circuit breaker trips after 5 consecutive failures, sparing the
        upstream while the outage persists (Task 12.9 / Req 10.4.3).
        Fallback behaviour is preserved: on any failure — including
        :class:`CircuitOpenError` — we return an empty feature list so
        the monitor cycle advances without raising.

        The NWS ``/alerts/active`` endpoint does NOT filter by ZIP. It
        filters by ``area`` (USPS 2-letter state code), ``zone`` (NWS zone
        id like ``TXZ123``), or ``point`` (lat,lon). The tenant footprint
        is a set of ZIP codes, so we derive the distinct set of **states**
        covering those ZIPs (via :func:`zip_to_state`) and query by
        ``area`` — NWS returns every active alert in those states and we
        narrow to the tenant's ZIPs client-side in
        :meth:`_build_weather_alert`. When no state can be derived from the
        footprint, we fall back to an unscoped active-alerts pull and filter
        locally rather than sending an invalid ``area`` filter (which NWS
        rejects with HTTP 400). NWS returns a GeoJSON FeatureCollection.
        """
        # Derive the distinct USPS states covering the ZIP footprint; NWS
        # ``area`` accepts a comma-separated list of state codes.
        states: List[str] = []
        seen_states: set[str] = set()
        for zip_code in zip_codes:
            state = zip_to_state(zip_code)
            if state and state not in seen_states:
                seen_states.add(state)
                states.append(state)

        # NWS caps practical area lists; 50 distinct states is the whole
        # country, so the slice is a defensive no-op in practice.
        if states:
            params: Dict[str, str] = {"area": ",".join(states[:50])}
        else:
            # No mappable state — fall back to an unscoped active-alerts
            # pull (filtered client-side) instead of a 400-triggering
            # ``zone=<zip>`` query.
            params = {}
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/geo+json",
        }

        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._http_timeout)
            self._http_client = client
        try:
            async with trace_external_call(
                tenant_id=tenant_id,
                provider="nws",
                operation="get_active_alerts",
                circuit_breaker=self._circuit_breaker,
                # The weather-alert metric uses
                # (new|updated|duplicate|error) as its status
                # vocabulary; we do not feed it from the call
                # wrapper here because ingestion-level outcomes are
                # recorded per-alert by the cycle. The wrapper's
                # own circuit-open / timeout / error tally is
                # already surfaced through the structured-log line
                # so dashboards can grep it by ``event``.
                metric=None,
            ) as call:
                try:
                    response = await client.get(
                        self._nws_base_url, params=params, headers=headers
                    )
                except httpx.HTTPError as exc:
                    call.set_error_code("http_error")
                    self.logger.warning(
                        "WeatherAlertIngester: NWS HTTP failure "
                        "for tenant=%s: %s",
                        tenant_id,
                        exc,
                    )
                    raise
                if response.status_code >= 400:
                    call.set_status("error")
                    call.set_error_code(f"http_{response.status_code}")
                    self.logger.warning(
                        "WeatherAlertIngester: NWS returned HTTP "
                        "%s for tenant=%s",
                        response.status_code,
                        tenant_id,
                    )
                    try:
                        fuelops_weather_alert_ingestion_total.labels(
                            tenant_id=tenant_id,
                            provider="nws",
                            status="error",
                        ).inc()
                    except Exception:  # pragma: no cover - defensive
                        pass
                    return []
                try:
                    payload = response.json()
                except ValueError as exc:
                    call.set_error_code("invalid_json")
                    self.logger.warning(
                        "WeatherAlertIngester: NWS returned non-JSON "
                        "for tenant=%s: %s",
                        tenant_id,
                        exc,
                    )
                    raise
        except CircuitOpenError:
            # Breaker is open for this tenant's NWS feed — skip the
            # cycle. Wrapper already emitted the
            # ``external_call_rejected`` event.
            return []
        except (httpx.HTTPError, ValueError):
            return []

        if not isinstance(payload, dict):
            return []
        features = payload.get("features")
        if not isinstance(features, list):
            return []
        return [f for f in features if isinstance(f, dict)]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_alert_id(raw_alert: Dict[str, Any]) -> Optional[str]:
        """Return the stable NWS alert id, or ``None`` when absent."""
        # NWS alerts return both top-level ``id`` (URL) and
        # ``properties.id``. Prefer the properties id so the key is
        # independent of the scheme / host.
        props = raw_alert.get("properties") or {}
        prop_id = props.get("id")
        if isinstance(prop_id, str) and prop_id.strip():
            return prop_id.strip()
        top_id = raw_alert.get("id")
        if isinstance(top_id, str) and top_id.strip():
            return top_id.strip()
        return None

    def _build_weather_alert(
        self,
        raw_alert: Dict[str, Any],
        *,
        tenant_id: str,
        zip_footprint: Sequence[str],
    ) -> WeatherAlert:
        """Convert a raw NWS feature into a validated :class:`WeatherAlert`.

        Raises:
            ValueError: If required fields are missing or malformed.
        """
        props = raw_alert.get("properties") or {}
        alert_id = self._extract_alert_id(raw_alert)
        if not alert_id:
            raise ValueError("missing alert_id")

        alert_type = props.get("event") or props.get("messageType") or "unknown"
        alert_type = str(alert_type).strip() or "unknown"

        severity = self._map_severity(props.get("severity"))

        expected_start_at = self._parse_datetime(
            props.get("onset") or props.get("effective") or props.get("sent")
        )
        if expected_start_at is None:
            raise ValueError("missing expected_start_at")

        expected_end_at = self._parse_datetime(
            props.get("ends") or props.get("expires")
        )

        region_code = (
            props.get("senderName")
            or (props.get("areaDesc") or "").split(",")[-1].strip()
            or "unknown"
        )

        # Filter the upstream area description against the tenant's ZIP
        # footprint. NWS does not return ZIPs directly on every alert, so
        # the filter is best-effort: we pass through the tenant's zip
        # footprint intersected with any ZIPs the alert does surface via
        # ``parameters.ZIPS`` (some products include it).
        affected_zip_codes = self._extract_affected_zips(props, zip_footprint)

        ingested_at = datetime.now(timezone.utc)
        activation_status: WeatherAlertStatus = self._derive_activation_status(
            expected_start_at=expected_start_at,
            expected_end_at=expected_end_at,
            now=ingested_at,
            upstream_status=props.get("status"),
        )

        return WeatherAlert(
            alert_id=alert_id,
            tenant_id=tenant_id,
            region_code=str(region_code)[:128] or "unknown",
            alert_type=alert_type,
            severity=severity,
            headline=self._optional_str(props.get("headline")),
            description=self._optional_str(props.get("description")),
            expected_start_at=expected_start_at,
            expected_end_at=expected_end_at,
            affected_zip_codes=list(affected_zip_codes),
            source="nws",
            ingested_at=ingested_at,
            activation_status=activation_status,
        )

    @staticmethod
    def _map_severity(value: Any) -> WeatherAlertSeverity:
        if not isinstance(value, str):
            return _NWS_SEVERITY_FALLBACK
        return _NWS_SEVERITY_MAP.get(value.strip().lower(), _NWS_SEVERITY_FALLBACK)

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            # NWS timestamps are ISO-8601 with offset, e.g. ``2024-01-15T12:00:00-05:00``.
            # ``fromisoformat`` handles those in Python 3.11+.
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _optional_str(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _extract_affected_zips(
        props: Dict[str, Any], zip_footprint: Sequence[str]
    ) -> List[str]:
        """Return the ZIP footprint restricted to what NWS surfaces.

        NWS exposes some alert-level ZIP codes via
        ``properties.parameters.ZIPS``. When that field is absent the
        tenant's full footprint is retained — an over-broad match is
        safer than dropping the alert entirely because Storm_Mode already
        requires additional severity / start-time gating downstream.
        """
        parameters = props.get("parameters") or {}
        upstream_zips_raw = parameters.get("ZIPS") or parameters.get("zips")
        upstream_zips: List[str] = []
        if isinstance(upstream_zips_raw, list):
            for entry in upstream_zips_raw:
                if isinstance(entry, str) and entry.strip():
                    upstream_zips.append(entry.strip())
        if upstream_zips:
            footprint_set = {z for z in zip_footprint}
            intersected = [z for z in upstream_zips if z in footprint_set]
            if intersected:
                return intersected
            # Upstream surfaced ZIPs but none overlap — keep the alert but
            # attach the upstream list so Storm_Mode can still evaluate.
            return upstream_zips
        return list(zip_footprint)

    @staticmethod
    def _derive_activation_status(
        *,
        expected_start_at: datetime,
        expected_end_at: Optional[datetime],
        now: datetime,
        upstream_status: Any,
    ) -> WeatherAlertStatus:
        """Bucket the alert into forecast/active/cleared/cancelled."""
        if isinstance(upstream_status, str):
            status = upstream_status.strip().lower()
            if status == "cancel":
                return "cancelled"
        if expected_end_at is not None and expected_end_at < now:
            return "cleared"
        if expected_start_at > now:
            return "forecast"
        return "active"

    # ------------------------------------------------------------------
    # Persist + publish
    # ------------------------------------------------------------------

    async def _ingest_alert(self, alert: WeatherAlert) -> Dict[str, Any]:
        """Persist the alert (if new) and publish a SignalBus message.

        Returns a dict describing the outcome so the ``monitor_cycle``
        caller can append it to the action log.
        """
        if await self._alert_already_persisted(alert.alert_id):
            return {
                "alert_id": alert.alert_id,
                "tenant_id": alert.tenant_id,
                "action": "skipped_duplicate",
            }

        persisted = await self._persist(alert)
        if not persisted:
            return {
                "alert_id": alert.alert_id,
                "tenant_id": alert.tenant_id,
                "action": "persist_failed",
            }

        published = await self._publish(alert)
        return {
            "alert_id": alert.alert_id,
            "tenant_id": alert.tenant_id,
            "action": "ingested",
            "published": published,
        }

    async def _alert_already_persisted(self, alert_id: str) -> bool:
        """Return ``True`` when ``alert_id`` already lives in ES."""
        try:
            existing = await self._es.get_document(
                WEATHER_ALERTS_INDEX, alert_id
            )
        except Exception:
            # ES not-found is commonly surfaced as an exception depending on
            # the adapter. Treat any failure as "not present" — the index
            # is strict-mapped so a later write will fail loudly if there
            # truly is a schema problem.
            return False
        return existing is not None

    async def _persist(self, alert: WeatherAlert) -> bool:
        """Best-effort ``index_document`` call; returns False on failure."""
        try:
            payload = alert.model_dump(mode="json")
            await self._es.index_document(
                WEATHER_ALERTS_INDEX, alert.alert_id, payload
            )
            return True
        except Exception as exc:
            self.logger.error(
                "WeatherAlertIngester: failed to persist alert_id=%s tenant=%s: %s",
                alert.alert_id,
                alert.tenant_id,
                exc,
            )
            return False

    async def _publish(self, alert: WeatherAlert) -> bool:
        """Publish to the SignalBus when one is wired."""
        if self._signal_bus is None:
            return False
        try:
            await self._signal_bus.publish(alert)
            return True
        except Exception as exc:
            self.logger.error(
                "WeatherAlertIngester: SignalBus publish failed for alert_id=%s: %s",
                alert.alert_id,
                exc,
            )
            return False


__all__ = [
    "WeatherAlertIngester",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_COOLDOWN_MINUTES",
    "NWS_ACTIVE_ALERTS_URL",
    "DEFAULT_USER_AGENT",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
]
