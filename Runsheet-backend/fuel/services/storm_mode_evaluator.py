"""
Storm_Mode_Evaluator — 5-minute activation/deactivation engine for Capability 9.

Task 10.3 of the fuel-ops hardening spec (Requirements 9.1.3, 9.1.4, 9.1.5):

    > THE Platform SHALL define a Storm_Mode activation rule evaluated every
    > 5 minutes for each tenant; Storm_Mode SHALL become ``active`` when any
    > WeatherAlert for a tenant's operational ZIP footprint has severity
    > >= tenant-configured ``storm_mode_activation_severity`` (default
    > ``severe``) and ``expected_start_at`` is within the next 48 hours.
    >
    > WHEN Storm_Mode activates, THE Platform SHALL publish a
    > ``storm_mode_activated`` RiskSignal on the Signal_Bus with tenant_id,
    > triggering WeatherAlert, activation_time, expected_end_at.
    >
    > WHEN Storm_Mode clears (no active qualifying WeatherAlerts remain),
    > THE Platform SHALL publish a ``storm_mode_cleared`` RiskSignal.

Design
------

The evaluator is the downstream consumer of :mod:`Agents.autonomous.weather_alert_ingester`
(Task 10.2). Every 5 minutes it:

1. Discovers the set of tenants to evaluate (via an injected
   :attr:`tenant_discovery` callable — by default an aggregation over
   ``weather_alerts`` plus ``storm_mode_overrides`` so tenants with manual
   overrides are still ticked even when no NWS alerts are live).
2. For each tenant, queries the ``weather_alerts`` ES index for alerts
   whose ``activation_status`` is ``forecast`` or ``active``, whose
   ``severity`` is ``>=`` the tenant-configured activation threshold
   (default ``severe``), and whose ``expected_start_at`` is within the
   next 48 hours. Alerts whose ``expected_end_at`` has already passed are
   excluded.
3. Checks the ``storm_mode_overrides`` ES index for an unexpired manual
   override (``activate``/``deactivate``/``snooze``). Override precedence
   mirrors the design doc: an ``activate`` override forces ``active``, a
   ``deactivate`` or ``snooze`` override forces ``inactive``, and a
   ``clear`` override falls back to the computed state.
4. Computes the desired state (``active``/``inactive``) from the qualifying
   alerts or the override, compares to the last persisted state (Redis
   key ``storm_mode_state:{tenant_id}``), and — when the state changes —
   persists the new state and publishes a ``storm_mode_activated`` or
   ``storm_mode_cleared`` RiskSignal on the injected SignalBus.
5. Idempotence is preserved (Requirement 9.4.5): re-evaluating without
   new alerts is a no-op; duplicate alert ingestion does not re-fire the
   signal because the persisted state matches the desired state.

The evaluator is intentionally a **pure service** — the REST endpoint
(Task 10.4) reads the persisted state via :meth:`get_state`, and the
bootstrap layer (Task 12.1) schedules :meth:`start` / :meth:`stop` on
the application lifecycle so tests can exercise :meth:`evaluate_tenant`
without spinning up the poll loop.

Validates: Requirements 9.1.3, 9.1.4, 9.1.5.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from Agents.overlay.data_contracts import RiskSignal, Severity
from fuel.services.fuel_ops_es_mappings import (
    STORM_MODE_OVERRIDES_INDEX,
    WEATHER_ALERTS_INDEX,
)
from fuel.storm_mode_models import (
    StormModeOverride,
    StormModeOverrideAction,
    WeatherAlert,
    WeatherAlertSeverity,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Default poll interval per Requirement 9.1.3 — 5 minutes.
DEFAULT_POLL_INTERVAL_SECONDS: int = 300

#: Default activation severity threshold per Requirement 9.1.3.
DEFAULT_ACTIVATION_SEVERITY: WeatherAlertSeverity = "severe"

#: Default activation window in hours per Requirement 9.1.3.
DEFAULT_ACTIVATION_WINDOW_HOURS: int = 48

#: Stable source-agent identifier published on every RiskSignal so
#: downstream filters can pin on a single string.
STORM_MODE_EVALUATOR_AGENT_ID: str = "storm_mode_evaluator"

#: RiskSignal entity_type for storm-mode transitions. Mirrors the convention
#: used by :mod:`integrations.integration_scheduler` so the exception
#: commander / ops UI can filter by a stable keyword.
STORM_MODE_ENTITY_TYPE: str = "storm_mode"

#: RiskSignal ``context.signal_type`` values — the wire contract consumed
#: by Delivery_Prioritization_Agent (Task 10.6), Route_Planning_Agent
#: (Task 10.7), and the Customer_Notification pipeline (Task 10.9).
SIGNAL_TYPE_ACTIVATED: str = "storm_mode_activated"
SIGNAL_TYPE_CLEARED: str = "storm_mode_cleared"

#: TTL applied to every published RiskSignal. Set well above the 5-minute
#: poll so subscribers on slower ticks still receive fresh transitions.
DEFAULT_SIGNAL_TTL_SECONDS: int = 3600

#: Confidence stamped on every RiskSignal. The evaluator is deterministic
#: with respect to its inputs, so confidence is 1.0 less a small epsilon
#: that keeps downstream blending math well-behaved.
DEFAULT_SIGNAL_CONFIDENCE: float = 0.99

#: Redis key pattern storing the last computed state per tenant. JSON-
#: encoded ``{"state": ..., "updated_at": ..., "triggering_alert_ids": [...],
#: "expected_end_at": ...}`` document. ``None`` is an acceptable value for
#: ``expected_end_at`` when the triggering alert has no end time.
STATE_KEY_PATTERN: str = "storm_mode_state:{tenant_id}"

#: State is refreshed whenever the evaluator observes a transition. The TTL
#: prevents orphaned tenant state from accumulating indefinitely while still
#: preserving recent storm status for operational dashboards.
STATE_KEY_TTL_SECONDS: int = 30 * 24 * 60 * 60

#: Numeric severity rank so ``severity >= threshold`` comparisons don't
#: depend on string ordering. ``extreme`` outranks ``severe`` which
#: outranks ``moderate`` which outranks ``minor``. Any unrecognised bucket
#: maps to ``minor`` so malformed upstream records never exceed the
#: threshold silently.
_SEVERITY_RANK: Dict[str, int] = {
    "minor": 1,
    "moderate": 2,
    "severe": 3,
    "extreme": 4,
}


#: Logical state stored for each tenant. Intentionally a small literal set
#: so every call site type-checks the same way.
StormModeState = str  # Literal["active", "inactive"]
ACTIVE: StormModeState = "active"
INACTIVE: StormModeState = "inactive"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Callable returning the set of tenants to evaluate this tick. Extracted
#: behind a type alias so unit tests can inject a simple async lambda.
TenantDiscoveryCallable = Callable[[], Awaitable[Iterable[str]]]

#: Callable returning the tenant-configured severity threshold. Defaults
#: to the shipped ``DEFAULT_ACTIVATION_SEVERITY`` when a tenant has not
#: overridden the value.
SeverityThresholdLoader = Callable[[str], Awaitable[WeatherAlertSeverity]]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EvaluationResult:
    """Outcome of a single tenant evaluation.

    Exposed so callers (the REST endpoint in Task 10.4, tests, ops tooling)
    can inspect the decision without having to re-read Redis or the ES
    indices. The evaluator itself only persists the subset that the
    activation rule needs to remain idempotent.
    """

    tenant_id: str
    previous_state: StormModeState
    desired_state: StormModeState
    transitioned: bool
    qualifying_alerts: List[WeatherAlert] = field(default_factory=list)
    override: Optional[StormModeOverride] = None
    computed_state: StormModeState = INACTIVE
    activation_window_hours: int = DEFAULT_ACTIVATION_WINDOW_HOURS
    severity_threshold: WeatherAlertSeverity = DEFAULT_ACTIVATION_SEVERITY
    evaluated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class PersistedState:
    """Serialized form of the last-known storm-mode state per tenant."""

    state: StormModeState
    updated_at: Optional[datetime]
    triggering_alert_ids: List[str]
    expected_end_at: Optional[datetime]

    @classmethod
    def inactive(cls) -> "PersistedState":
        return cls(
            state=INACTIVE,
            updated_at=None,
            triggering_alert_ids=[],
            expected_end_at=None,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "state": self.state,
                "updated_at": self.updated_at.isoformat()
                if self.updated_at
                else None,
                "triggering_alert_ids": list(self.triggering_alert_ids),
                "expected_end_at": self.expected_end_at.isoformat()
                if self.expected_end_at
                else None,
            }
        )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def severity_meets_threshold(
    severity: str, threshold: str
) -> bool:
    """Return ``True`` when ``severity`` is at or above ``threshold``.

    Uses :data:`_SEVERITY_RANK` so comparisons don't rely on string
    ordering. Unknown buckets map to ``minor`` so an upstream record that
    slips through validation cannot silently exceed a ``severe`` threshold.
    """
    severity_rank = _SEVERITY_RANK.get(str(severity).strip().lower(), 1)
    threshold_rank = _SEVERITY_RANK.get(str(threshold).strip().lower(), 3)
    return severity_rank >= threshold_rank


def _coerce_iso(value: Any) -> Optional[datetime]:
    """Parse an ES-roundtripped ISO-8601 string into a tz-aware datetime.

    Returns ``None`` when the input is missing, malformed, or an empty
    string. The ingester writes Z-terminated timestamps; this helper
    normalises both Z and explicit-offset forms.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _override_forces_state(action: StormModeOverrideAction) -> Optional[StormModeState]:
    """Return the state an override forces, or ``None`` when the override
    does not short-circuit the computed state.

    * ``activate`` → ``active``
    * ``deactivate`` / ``snooze`` → ``inactive``
    * ``clear`` → ``None`` (fall back to computed)
    """
    if action == "activate":
        return ACTIVE
    if action in ("deactivate", "snooze"):
        return INACTIVE
    return None


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class StormModeEvaluator:
    """5-minute activation/deactivation engine for Storm_Mode.

    Args:
        es_service: Elasticsearch service exposing ``search_documents``
            (and, for the override scan, nothing else). Required.
        signal_bus: :class:`Agents.overlay.signal_bus.SignalBus`-compatible
            instance used to publish ``storm_mode_activated`` /
            ``storm_mode_cleared`` :class:`RiskSignal`\\ s. Optional so
            the evaluator can be exercised in shadow-only mode during
            backfills and smoke tests.
        redis_client: Redis async client used to persist the last-known
            state per tenant. When omitted the evaluator falls back to an
            in-process state map — still deterministic inside a single
            process, but not shared across workers. The production
            bootstrap always injects a shared Redis client.
        tenant_discovery: Async callable returning an iterable of
            tenant_ids to evaluate on each tick. Defaults to a best-effort
            aggregation over the ``weather_alerts`` and
            ``storm_mode_overrides`` indices, so tenants with manual
            overrides are still evaluated even when no NWS alerts are live.
        severity_threshold_loader: Optional per-tenant severity threshold
            resolver. Defaults to :data:`DEFAULT_ACTIVATION_SEVERITY` for
            every tenant.
        activation_window_hours: Lookahead window for ``expected_start_at``.
            Defaults to :data:`DEFAULT_ACTIVATION_WINDOW_HOURS` (48).
        poll_interval_seconds: Seconds between poll ticks. Defaults to
            :data:`DEFAULT_POLL_INTERVAL_SECONDS` (300 = 5 minutes).
        signal_ttl_seconds: TTL stamped on every published RiskSignal.
        signal_confidence: Confidence stamped on every published RiskSignal.
    """

    def __init__(
        self,
        es_service: Any,
        *,
        signal_bus: Optional[Any] = None,
        redis_client: Optional[Any] = None,
        tenant_discovery: Optional[TenantDiscoveryCallable] = None,
        severity_threshold_loader: Optional[SeverityThresholdLoader] = None,
        activation_window_hours: int = DEFAULT_ACTIVATION_WINDOW_HOURS,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
        signal_ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
        signal_confidence: float = DEFAULT_SIGNAL_CONFIDENCE,
    ) -> None:
        if es_service is None:
            raise ValueError("es_service must not be None")
        if activation_window_hours <= 0:
            raise ValueError("activation_window_hours must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if signal_ttl_seconds <= 0:
            raise ValueError("signal_ttl_seconds must be positive")
        if not 0.0 <= signal_confidence <= 1.0:
            raise ValueError("signal_confidence must be within [0.0, 1.0]")

        self._es = es_service
        self._signal_bus = signal_bus
        self._redis = redis_client
        self._tenant_discovery = tenant_discovery
        self._severity_threshold_loader = severity_threshold_loader
        self._activation_window_hours = activation_window_hours
        self._poll_interval = poll_interval_seconds
        self._signal_ttl = signal_ttl_seconds
        self._signal_confidence = signal_confidence

        #: In-memory fallback when no Redis client is wired.
        self._in_memory_state: Dict[str, PersistedState] = {}

        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the 5-minute poll loop as a background asyncio task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "StormModeEvaluator: started (poll_interval=%ss, window=%sh, threshold=%s)",
            self._poll_interval,
            self._activation_window_hours,
            DEFAULT_ACTIVATION_SEVERITY,
        )

    async def stop(self) -> None:
        """Gracefully stop the poll loop."""
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("StormModeEvaluator: stopped")

    async def _run_loop(self) -> None:
        """Poll forever; never raises out of the loop."""
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "StormModeEvaluator: tick failed: %s", exc, exc_info=True
                )
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def tick(self) -> List[EvaluationResult]:
        """Run one evaluation pass across every discovered tenant.

        Returns a list of :class:`EvaluationResult` — one per tenant —
        so callers (tests, ops tooling, activity-log wrappers) can
        introspect the outcomes. Exceptions raised by a single tenant
        evaluation are caught and logged so a malformed record for one
        tenant cannot starve the rest.
        """
        tenant_ids = await self._safe_discover_tenants()
        results: List[EvaluationResult] = []
        for tenant_id in tenant_ids:
            try:
                result = await self.evaluate_tenant(tenant_id)
            except Exception as exc:
                logger.warning(
                    "StormModeEvaluator: evaluation failed for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                continue
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Per-tenant evaluation
    # ------------------------------------------------------------------

    async def evaluate_tenant(self, tenant_id: str) -> EvaluationResult:
        """Evaluate Storm_Mode state for a single tenant.

        The full decision flow — alert fetch, override lookup, state
        transition, signal publish, state persistence — is exposed as a
        single method so the REST endpoint (Task 10.4), the bootstrap
        poll loop, and the unit tests can all use the same entry point.

        Idempotency: when the desired state already matches the persisted
        state, no signal is published and Redis is not updated. This
        preserves Requirement 9.4.5 (same alert_id ingested twice does
        not toggle state twice).

        Args:
            tenant_id: Identifier of the tenant whose state to evaluate.
                An empty / blank value is accepted but returns a no-op
                ``inactive`` result so upstream callers don't need to
                special-case it.
        """
        now = datetime.now(timezone.utc)
        tenant_id_clean = (tenant_id or "").strip()

        if not tenant_id_clean:
            return EvaluationResult(
                tenant_id=tenant_id_clean,
                previous_state=INACTIVE,
                desired_state=INACTIVE,
                transitioned=False,
                computed_state=INACTIVE,
                evaluated_at=now,
            )

        severity_threshold = await self._resolve_severity_threshold(tenant_id_clean)
        qualifying_alerts = await self._fetch_qualifying_alerts(
            tenant_id_clean, severity_threshold=severity_threshold, now=now
        )
        computed_state = ACTIVE if qualifying_alerts else INACTIVE

        override = await self._fetch_active_override(tenant_id_clean, now=now)
        forced = _override_forces_state(override.action) if override else None
        desired_state: StormModeState = forced if forced is not None else computed_state

        previous = await self._load_persisted_state(tenant_id_clean)
        transitioned = desired_state != previous.state

        result = EvaluationResult(
            tenant_id=tenant_id_clean,
            previous_state=previous.state,
            desired_state=desired_state,
            transitioned=transitioned,
            qualifying_alerts=list(qualifying_alerts),
            override=override,
            computed_state=computed_state,
            activation_window_hours=self._activation_window_hours,
            severity_threshold=severity_threshold,
            evaluated_at=now,
        )

        if not transitioned:
            return result

        expected_end_at = self._longest_expected_end_at(qualifying_alerts)
        next_state = PersistedState(
            state=desired_state,
            updated_at=now,
            triggering_alert_ids=[a.alert_id for a in qualifying_alerts],
            expected_end_at=expected_end_at,
        )
        await self._persist_state(tenant_id_clean, next_state)
        await self._publish_transition(
            tenant_id=tenant_id_clean,
            previous_state=previous.state,
            next_state=desired_state,
            qualifying_alerts=qualifying_alerts,
            override=override,
            activation_time=now,
            expected_end_at=expected_end_at,
        )
        return result

    # ------------------------------------------------------------------
    # State readers
    # ------------------------------------------------------------------

    async def get_state(self, tenant_id: str) -> PersistedState:
        """Return the last-known persisted state for ``tenant_id``.

        Used by the Task 10.4 ``GET /api/fuel/storm-mode/status`` endpoint
        so the REST layer never re-runs the evaluator just to render the
        banner. Falls back to :meth:`PersistedState.inactive` when no
        record exists.
        """
        return await self._load_persisted_state((tenant_id or "").strip())

    # ------------------------------------------------------------------
    # Tenant discovery
    # ------------------------------------------------------------------

    async def _safe_discover_tenants(self) -> List[str]:
        """Return the tenants to evaluate on this tick; never raises."""
        loader = self._tenant_discovery or self._default_tenant_discovery
        try:
            raw = await loader()
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: tenant discovery failed: %s", exc
            )
            return []
        tenant_ids: List[str] = []
        seen: set[str] = set()
        for tenant_id in raw or []:
            if not isinstance(tenant_id, str):
                continue
            stripped = tenant_id.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            tenant_ids.append(stripped)
        return tenant_ids

    async def _default_tenant_discovery(self) -> List[str]:
        """Aggregate tenants from ``weather_alerts`` and ``storm_mode_overrides``.

        A tenant is a candidate for evaluation if either:

        * it has at least one ``weather_alerts`` document with
          ``activation_status`` in ``{forecast, active}`` (so we can
          potentially activate), or
        * it has at least one ``storm_mode_overrides`` document (so a
          manual ``deactivate`` can still be honoured after the triggering
          alerts have cleared).
        """
        tenant_ids: set[str] = set()

        alert_query = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "terms": {
                                "activation_status": ["forecast", "active"]
                            }
                        }
                    ]
                }
            },
            "aggs": {
                "tenants": {"terms": {"field": "tenant_id", "size": 1000}}
            },
        }
        try:
            alert_resp = await self._es.search_documents(
                WEATHER_ALERTS_INDEX, alert_query, 0
            )
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: weather_alerts tenant discovery failed: %s",
                exc,
            )
            alert_resp = None

        for bucket in self._extract_term_buckets(alert_resp, "tenants"):
            key = bucket.get("key")
            if isinstance(key, str) and key.strip():
                tenant_ids.add(key.strip())

        override_query = {
            "size": 0,
            "aggs": {
                "tenants": {"terms": {"field": "tenant_id", "size": 1000}}
            },
        }
        try:
            override_resp = await self._es.search_documents(
                STORM_MODE_OVERRIDES_INDEX, override_query, 0
            )
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: storm_mode_overrides tenant discovery failed: %s",
                exc,
            )
            override_resp = None

        for bucket in self._extract_term_buckets(override_resp, "tenants"):
            key = bucket.get("key")
            if isinstance(key, str) and key.strip():
                tenant_ids.add(key.strip())

        return sorted(tenant_ids)

    @staticmethod
    def _extract_term_buckets(
        response: Any, agg_name: str
    ) -> List[Dict[str, Any]]:
        """Pull the ``buckets`` list out of an ES aggregation response."""
        aggs = (response or {}).get("aggregations") or {}
        agg = aggs.get(agg_name) or {}
        buckets = agg.get("buckets")
        if not isinstance(buckets, list):
            return []
        return [b for b in buckets if isinstance(b, dict)]

    # ------------------------------------------------------------------
    # Alert fetching
    # ------------------------------------------------------------------

    async def _fetch_qualifying_alerts(
        self,
        tenant_id: str,
        *,
        severity_threshold: WeatherAlertSeverity,
        now: datetime,
    ) -> List[WeatherAlert]:
        """Return the set of qualifying alerts for ``tenant_id``.

        Applies Requirement 9.1.3:

        * ``tenant_id`` match.
        * ``activation_status`` in ``{forecast, active}`` — cancelled and
          cleared alerts do not contribute.
        * ``severity >= severity_threshold`` (numeric rank).
        * ``expected_start_at <= now + activation_window_hours``.
        * ``expected_end_at`` is either null or in the future — an alert
          whose window has already closed cannot qualify even if the
          ingester has not yet flipped its status.
        """
        activation_cutoff = now + timedelta(hours=self._activation_window_hours)
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"tenant_id": tenant_id}},
                        {
                            "terms": {
                                "activation_status": ["forecast", "active"]
                            }
                        },
                        {
                            "range": {
                                "expected_start_at": {
                                    "lte": activation_cutoff.isoformat()
                                }
                            }
                        },
                    ]
                }
            },
            "sort": [{"expected_start_at": {"order": "asc"}}],
            "size": 200,
        }
        try:
            resp = await self._es.search_documents(
                WEATHER_ALERTS_INDEX, query, 200
            )
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: failed to fetch weather_alerts "
                "for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return []

        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        qualifying: List[WeatherAlert] = []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                alert = WeatherAlert.model_validate(source)
            except Exception as exc:
                logger.debug(
                    "StormModeEvaluator: skipping malformed alert for "
                    "tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                continue

            # Tenant isolation defense in depth — the ES query already
            # filters by tenant_id but a stray document should never leak.
            if alert.tenant_id != tenant_id:
                continue

            if not severity_meets_threshold(alert.severity, severity_threshold):
                continue

            if alert.expected_end_at is not None and alert.expected_end_at < now:
                continue

            qualifying.append(alert)

        return qualifying

    # ------------------------------------------------------------------
    # Override fetching
    # ------------------------------------------------------------------

    async def _fetch_active_override(
        self, tenant_id: str, *, now: datetime
    ) -> Optional[StormModeOverride]:
        """Return the most-recent non-expired override for ``tenant_id``.

        The override index is queried with:

        * ``tenant_id`` match.
        * ``expires_at`` either null or in the future.

        Results are sorted by ``created_at`` desc so the most recent
        override wins when multiple are live — an ``activate`` issued at
        10:05 supersedes a ``snooze`` issued at 10:00.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [{"term": {"tenant_id": tenant_id}}],
                    "should": [
                        {
                            "bool": {
                                "must_not": [{"exists": {"field": "expires_at"}}]
                            }
                        },
                        {
                            "range": {
                                "expires_at": {"gt": now.isoformat()}
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            },
            "sort": [{"created_at": {"order": "desc"}}],
            "size": 10,
        }
        try:
            resp = await self._es.search_documents(
                STORM_MODE_OVERRIDES_INDEX, query, 10
            )
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: failed to fetch overrides for "
                "tenant=%s: %s",
                tenant_id,
                exc,
            )
            return None

        hits = ((resp or {}).get("hits") or {}).get("hits") or []
        for hit in hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            try:
                override = StormModeOverride.model_validate(source)
            except Exception as exc:
                logger.debug(
                    "StormModeEvaluator: skipping malformed override "
                    "for tenant=%s: %s",
                    tenant_id,
                    exc,
                )
                continue
            if override.tenant_id != tenant_id:
                continue
            if (
                override.expires_at is not None
                and override.expires_at <= now
            ):
                continue
            return override
        return None

    # ------------------------------------------------------------------
    # Severity threshold
    # ------------------------------------------------------------------

    async def _resolve_severity_threshold(
        self, tenant_id: str
    ) -> WeatherAlertSeverity:
        """Return the tenant's activation severity threshold.

        Defaults to :data:`DEFAULT_ACTIVATION_SEVERITY` when no loader is
        wired or the loader raises.
        """
        if self._severity_threshold_loader is None:
            return DEFAULT_ACTIVATION_SEVERITY
        try:
            raw = await self._severity_threshold_loader(tenant_id)
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: severity loader failed for "
                "tenant=%s: %s — falling back to default",
                tenant_id,
                exc,
            )
            return DEFAULT_ACTIVATION_SEVERITY

        if not isinstance(raw, str):
            return DEFAULT_ACTIVATION_SEVERITY
        normalized = raw.strip().lower()
        if normalized not in _SEVERITY_RANK:
            return DEFAULT_ACTIVATION_SEVERITY
        # The Literal type only accepts the four values in _SEVERITY_RANK
        # so the cast is safe.
        return normalized  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    async def _load_persisted_state(self, tenant_id: str) -> PersistedState:
        """Return the last-known state for ``tenant_id`` (or ``inactive``)."""
        if not tenant_id:
            return PersistedState.inactive()

        if self._redis is None:
            return self._in_memory_state.get(
                tenant_id, PersistedState.inactive()
            )

        try:
            raw = await self._redis.get(self._state_key(tenant_id))
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: Redis read failed for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return PersistedState.inactive()

        if raw is None:
            return PersistedState.inactive()

        try:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            logger.warning(
                "StormModeEvaluator: malformed state JSON for tenant=%s: %s",
                tenant_id,
                exc,
            )
            return PersistedState.inactive()

        return self._deserialize_state(data)

    async def _persist_state(
        self, tenant_id: str, state: PersistedState
    ) -> None:
        """Best-effort write; failures are logged but never raised."""
        if not tenant_id:
            return

        if self._redis is None:
            self._in_memory_state[tenant_id] = state
            return

        try:
            await self._redis.set(
                self._state_key(tenant_id),
                state.to_json(),
                ex=STATE_KEY_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "StormModeEvaluator: Redis write failed for tenant=%s: %s",
                tenant_id,
                exc,
            )

    @staticmethod
    def _state_key(tenant_id: str) -> str:
        return STATE_KEY_PATTERN.format(tenant_id=tenant_id)

    @staticmethod
    def _deserialize_state(data: Any) -> PersistedState:
        """Coerce a JSON-decoded payload into a :class:`PersistedState`."""
        if not isinstance(data, dict):
            return PersistedState.inactive()
        raw_state = data.get("state")
        state: StormModeState = (
            ACTIVE if raw_state == ACTIVE else INACTIVE
        )
        updated_at = _coerce_iso(data.get("updated_at"))
        expected_end_at = _coerce_iso(data.get("expected_end_at"))
        raw_alerts = data.get("triggering_alert_ids") or []
        triggering: List[str] = []
        if isinstance(raw_alerts, list):
            for value in raw_alerts:
                if isinstance(value, str) and value.strip():
                    triggering.append(value.strip())
        return PersistedState(
            state=state,
            updated_at=updated_at,
            triggering_alert_ids=triggering,
            expected_end_at=expected_end_at,
        )

    # ------------------------------------------------------------------
    # Signal publication
    # ------------------------------------------------------------------

    async def _publish_transition(
        self,
        *,
        tenant_id: str,
        previous_state: StormModeState,
        next_state: StormModeState,
        qualifying_alerts: Sequence[WeatherAlert],
        override: Optional[StormModeOverride],
        activation_time: datetime,
        expected_end_at: Optional[datetime],
    ) -> bool:
        """Publish the activation/clearance RiskSignal; logs on failure."""
        if self._signal_bus is None:
            logger.info(
                "StormModeEvaluator: skipping publish for tenant=%s "
                "(%s->%s) — no SignalBus wired",
                tenant_id,
                previous_state,
                next_state,
            )
            return False

        signal_type = (
            SIGNAL_TYPE_ACTIVATED if next_state == ACTIVE else SIGNAL_TYPE_CLEARED
        )
        triggering_alert = qualifying_alerts[0] if qualifying_alerts else None

        context: Dict[str, Any] = {
            "signal_type": signal_type,
            "previous_state": previous_state,
            "next_state": next_state,
            "activation_time": activation_time.isoformat(),
            "expected_end_at": expected_end_at.isoformat()
            if expected_end_at
            else None,
            "triggering_alert_ids": [a.alert_id for a in qualifying_alerts],
            "activation_window_hours": self._activation_window_hours,
        }
        if triggering_alert is not None:
            context["triggering_alert"] = {
                "alert_id": triggering_alert.alert_id,
                "alert_type": triggering_alert.alert_type,
                "severity": triggering_alert.severity,
                "headline": triggering_alert.headline,
                "expected_start_at": triggering_alert.expected_start_at.isoformat(),
                "expected_end_at": triggering_alert.expected_end_at.isoformat()
                if triggering_alert.expected_end_at
                else None,
                "source": triggering_alert.source,
                "affected_zip_codes": list(triggering_alert.affected_zip_codes),
            }
        if override is not None:
            context["override"] = {
                "override_id": override.override_id,
                "action": override.action,
                "reason": override.reason,
                "actor_id": override.actor_id,
                "expires_at": override.expires_at.isoformat()
                if override.expires_at
                else None,
            }

        try:
            signal = RiskSignal(
                source_agent=STORM_MODE_EVALUATOR_AGENT_ID,
                entity_id=tenant_id,
                entity_type=STORM_MODE_ENTITY_TYPE,
                severity=Severity.HIGH
                if next_state == ACTIVE
                else Severity.MEDIUM,
                confidence=self._signal_confidence,
                ttl_seconds=self._signal_ttl,
                tenant_id=tenant_id,
                context=context,
                timestamp=activation_time,
            )
            await self._signal_bus.publish(signal)
            return True
        except Exception as exc:
            logger.error(
                "StormModeEvaluator: SignalBus publish failed for tenant=%s "
                "(%s->%s): %s",
                tenant_id,
                previous_state,
                next_state,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _longest_expected_end_at(
        alerts: Sequence[WeatherAlert],
    ) -> Optional[datetime]:
        """Return the latest ``expected_end_at`` across ``alerts``.

        Used as the published ``expected_end_at`` on activation signals
        so downstream consumers have a best-effort "how long will this
        last" signal. Returns ``None`` when no alert supplies an end
        time, which the RiskSignal contract treats as "indefinite".
        """
        latest: Optional[datetime] = None
        for alert in alerts:
            if alert.expected_end_at is None:
                continue
            if latest is None or alert.expected_end_at > latest:
                latest = alert.expected_end_at
        return latest


__all__ = [
    # Constants
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_ACTIVATION_SEVERITY",
    "DEFAULT_ACTIVATION_WINDOW_HOURS",
    "STORM_MODE_EVALUATOR_AGENT_ID",
    "STORM_MODE_ENTITY_TYPE",
    "SIGNAL_TYPE_ACTIVATED",
    "SIGNAL_TYPE_CLEARED",
    "DEFAULT_SIGNAL_TTL_SECONDS",
    "DEFAULT_SIGNAL_CONFIDENCE",
    "STATE_KEY_PATTERN",
    "STATE_KEY_TTL_SECONDS",
    "ACTIVE",
    "INACTIVE",
    # Helpers
    "severity_meets_threshold",
    # Types
    "EvaluationResult",
    "PersistedState",
    "StormModeState",
    "StormModeEvaluator",
    "TenantDiscoveryCallable",
    "SeverityThresholdLoader",
]
