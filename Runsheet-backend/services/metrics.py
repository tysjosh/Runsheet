"""
Prometheus-compatible metrics for the fuel-ops hardening capabilities.

Defines the counter and histogram surface mandated by Task 12.8 of the
``fuel-ops-hardening`` spec (Req 10.3.1). Each metric is defined once
on a dedicated :class:`CollectorRegistry` so scraping the fuel-ops
metrics does not pollute the ops-intelligence registry surfaced at
``/ops/metrics/prometheus`` (which lives in
:mod:`ops.services.ops_metrics`).

Metric catalogue:

    ``fuelops_weather_provider_calls_total`` — one increment per
    :class:`fuel.services.weather_provider.WeatherProvider` invocation.
    Labels ``tenant_id``, ``provider`` (``noaa`` | ``openweather``),
    ``status`` (``success`` | ``timeout`` | ``error``). Drives the
    "weather provider health" alert and lets dispatchers compare NOAA
    vs. OpenWeather success rates per tenant.

    ``fuelops_traffic_provider_calls_total`` — one increment per
    :class:`fuel.services.traffic_provider.TrafficProvider`
    ``get_matrix`` call. Labels ``tenant_id``, ``provider``
    (``mapbox`` | ``here`` | ``google``), ``status`` (``success`` |
    ``timeout`` | ``budget_exceeded`` | ``error``). The ``budget_exceeded``
    status is surfaced by the :class:`RoutePlanningAgent` when a tenant
    hits their monthly ``traffic_budget`` ceiling and we fall back to
    Haversine.

    ``fuelops_ocr_calls_total`` — one increment per
    :class:`services.meter_ticket_ocr_service.MeterTicketOCRService`
    ``extract`` call. Labels ``tenant_id``, ``provider`` (``textract``
    today, future adapters bump the label without breaking the series),
    ``status`` (``success`` | ``requires_manual_review`` | ``timeout`` |
    ``error``). ``requires_manual_review`` specifically tracks
    low-confidence OCR so the ops dashboard can expose a "manual review
    rate" KPI.

    ``fuelops_integration_sync_runs_total`` — one increment per
    :class:`integrations.connector_base.SyncRun` that transitions to a
    terminal status. Labels ``tenant_id``, ``provider`` (``quickbooks_online``
    | ``veeder_root`` | ``geotab`` | ``stripe`` | ``opis``), ``status``
    (``success`` | ``partial`` | ``error``). Increments are emitted by
    :class:`integrations.integration_scheduler.IntegrationScheduler`
    after each completed run so retry-loop behaviour is visible.

    ``fuelops_rack_price_provider_calls_total`` — one increment per
    :class:`integrations.rack_price_provider_base.RackPriceProvider`
    fetch. Labels ``tenant_id``, ``provider`` (``opis`` |
    ``csv_fallback``), ``status`` (``success`` | ``cache_hit`` |
    ``stale_cache`` | ``timeout`` | ``error``). The ``cache_hit`` /
    ``stale_cache`` labels distinguish a Redis hit from a 24-hour
    fallback annotated with ``rack_price_fallback: true``.

    ``fuelops_weather_alert_ingestion_total`` — one increment per
    NOAA/NWS alert the
    :class:`Agents.autonomous.weather_alert_ingester.WeatherAlertIngester`
    ingests. Labels ``tenant_id``, ``provider`` (``noaa`` | ``nws``),
    ``status`` (``new`` | ``updated`` | ``duplicate`` | ``error``).
    ``duplicate`` covers the Task 10.10 idempotence property (same
    ``alert_id`` ingested twice).

    ``fuelops_storm_mode_activations_total`` — one increment per
    :class:`StormModeEvaluator` state transition. Labels ``tenant_id``,
    ``provider`` (stays ``storm_mode_evaluator`` so the standard
    (tenant_id, provider, status) label triple holds across every
    metric in this module), ``status`` (``activated`` | ``cleared`` |
    ``override_activated`` | ``override_cleared``). The override
    statuses capture manual overrides submitted through
    ``POST /api/fuel/storm-mode/override``.

    ``fuelops_terminal_recommendation_latency_ms`` — histogram of
    :class:`fuel.services.sourcing_recommender.SourcingRecommender`
    latency. Labels ``tenant_id``, ``provider`` (``opis`` |
    ``csv_fallback`` reflecting the rack-price provider used during the
    recommendation), ``status`` (``success`` | ``fallback`` | ``error``).
    ``fallback`` is emitted when the recommender returned a recommendation
    built from a cached rack-price fallback (Req 8.2.5) so the dashboard
    can track the impact of upstream degradations on recommendation
    latency.

Every metric is labelled by ``(tenant_id, provider, status)`` so the
Grafana dashboards already published for the ops-intelligence layer can
be cloned with minimal edits. ``tenant_id`` is always the first label
so per-tenant queries stay cheap.

Usage (from any fuel-ops service)::

    from services.metrics import fuelops_weather_provider_calls_total
    fuelops_weather_provider_calls_total.labels(
        tenant_id=tenant_id,
        provider="noaa",
        status="success",
    ).inc()

    from services.metrics import fuelops_terminal_recommendation_latency_ms
    with fuelops_terminal_recommendation_latency_ms.labels(
        tenant_id=tenant_id,
        provider="opis",
        status="success",
    ).time():
        await sourcing_recommender.recommend(...)

Validates: Requirement 10.3.1.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

__all__ = [
    "FUELOPS_REGISTRY",
    "fuelops_weather_provider_calls_total",
    "fuelops_traffic_provider_calls_total",
    "fuelops_ocr_calls_total",
    "fuelops_integration_sync_runs_total",
    "fuelops_rack_price_provider_calls_total",
    "fuelops_weather_alert_ingestion_total",
    "fuelops_storm_mode_activations_total",
    "fuelops_terminal_recommendation_latency_ms",
    "render_fuelops_metrics",
]


#: Dedicated registry for the fuel-ops metric surface. Keeping this
#: registry separate from :mod:`ops.services.ops_metrics` ``REGISTRY``
#: avoids cross-pollution — the ``/ops/metrics/prometheus`` endpoint
#: only scrapes the ops-intelligence metrics, while fuel-ops metrics
#: are exposed through the admin dashboard endpoint (future task).
FUELOPS_REGISTRY = CollectorRegistry()


#: Canonical label triple shared across every metric in this module so
#: dashboards can filter by ``(tenant_id, provider, status)`` without
#: duplicating boilerplate.
_LABELS = ("tenant_id", "provider", "status")


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


fuelops_weather_provider_calls_total = Counter(
    "fuelops_weather_provider_calls_total",
    "Total WeatherProvider.get_hdd calls. ``status`` is one of "
    "success|timeout|error|cache_hit so dashboards can split hot "
    "Redis reads from upstream NOAA/OpenWeather fetches.",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)

fuelops_traffic_provider_calls_total = Counter(
    "fuelops_traffic_provider_calls_total",
    "Total TrafficProvider.get_matrix calls. ``status`` is one of "
    "success|timeout|error|cache_hit|budget_exceeded — the last "
    "surfaces when the tenant exhausted their monthly "
    "traffic_budget counter and the agent fell back to Haversine.",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)

fuelops_ocr_calls_total = Counter(
    "fuelops_ocr_calls_total",
    "Total MeterTicketOCRService.extract calls. ``status`` is one of "
    "success|requires_manual_review|timeout|error. "
    "``requires_manual_review`` fires when confidence < threshold.",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)

fuelops_integration_sync_runs_total = Counter(
    "fuelops_integration_sync_runs_total",
    "Total IntegrationScheduler SyncRun outcomes. ``status`` is one of "
    "success|partial|error and ``provider`` carries the "
    "IntegrationConnector.provider_name (e.g. quickbooks_online).",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)

fuelops_rack_price_provider_calls_total = Counter(
    "fuelops_rack_price_provider_calls_total",
    "Total RackPriceProvider.fetch calls. ``status`` is one of "
    "success|cache_hit|stale_cache|timeout|error. ``stale_cache`` "
    "fires when the recommender annotates the result with "
    "``rack_price_fallback: true``.",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)

fuelops_weather_alert_ingestion_total = Counter(
    "fuelops_weather_alert_ingestion_total",
    "Total WeatherAlertIngester outcomes per alert document processed. "
    "``status`` is one of new|updated|duplicate|error and ``provider`` "
    "is one of noaa|nws.",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)

fuelops_storm_mode_activations_total = Counter(
    "fuelops_storm_mode_activations_total",
    "Total Storm_Mode state transitions. ``status`` is one of "
    "activated|cleared|override_activated|override_cleared and "
    "``provider`` is always ``storm_mode_evaluator`` so the label "
    "shape matches every other metric in this module.",
    _LABELS,
    registry=FUELOPS_REGISTRY,
)


# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------


#: Histogram buckets tuned to terminal-recommendation latency. The
#: Sourcing_Recommender's P99 on a warm Redis + cached OPIS fetch is in
#: the 10–50 ms range; a cold OPIS fetch plus disqualification passes
#: can reach 500 ms. The upper bucket (``5000``) catches genuinely
#: pathological runs so an alert can fire before the recommender times
#: out the request.
_TERMINAL_RECOMMENDATION_BUCKETS: tuple[float, ...] = (
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
)


fuelops_terminal_recommendation_latency_ms = Histogram(
    "fuelops_terminal_recommendation_latency_ms",
    "SourcingRecommender.recommend latency in milliseconds. "
    "``provider`` reflects the rack-price provider used (opis | "
    "csv_fallback) and ``status`` is one of success|fallback|error "
    "so dashboards can distinguish a successful recommendation built "
    "from a stale cache from a fresh OPIS fetch.",
    _LABELS,
    buckets=_TERMINAL_RECOMMENDATION_BUCKETS,
    registry=FUELOPS_REGISTRY,
)


# ---------------------------------------------------------------------------
# Exposition helper
# ---------------------------------------------------------------------------


def render_fuelops_metrics() -> bytes:
    """Return the Prometheus text-format dump of :data:`FUELOPS_REGISTRY`.

    Callers that want to surface the fuel-ops metrics through an HTTP
    endpoint can wire this into a handler like::

        from fastapi import Response

        @app.get("/fuelops/metrics/prometheus")
        async def fuelops_metrics_endpoint() -> Response:
            return Response(
                content=render_fuelops_metrics(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    Keeping the exposition helper here (rather than in every caller)
    means tests can assert the metric names / help strings by parsing
    the text dump without having to stand up an HTTP server.
    """
    return generate_latest(FUELOPS_REGISTRY)
