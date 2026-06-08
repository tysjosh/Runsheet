"""
Fuel-type pluggable ``Consumption_Model`` strategies.

Capability 1 / Requirement 1.5 of the fuel-ops hardening spec asks for a
common interface so the Tank_Forecasting_Agent can pick the right algorithm
per fuel type:

* Retail stations use the existing rolling 7-day average.
* Propane uses a tank-specific K-factor multiplied by HDD plus a base load.
* Heating oil uses a linear regression on HDD with a K-factor fallback when
  the regression is a poor fit.
* Diesel uses a 28-day rolling average with a light day-of-week seasonality
  adjustment.
* Generator fuel uses gallons-per-runtime-hour multiplied by projected
  runtime hours.

Every strategy is a pure function: it takes the customer tank, the relevant
history, the weather window, and an optional customer-type multiplier and
returns a :class:`ConsumptionPrediction` with three fields the forecaster
persists for traceability (Requirement 1.5.6):

    * ``gallons_per_day`` — non-negative double (Req 1.5.7).
    * ``confidence`` — a float in ``[0.0, 1.0]`` (Req 1.5.7).
    * ``model_name`` — short identifier stamped on every forecast.
    * ``features_used`` — structured inputs that produced the prediction.
    * ``anomaly_flags`` — qualitative reasons surfaced to the agent.
    * ``as_of`` — evaluation timestamp.

The module is deliberately dependency-free beyond stdlib + Pydantic so the
same code can run inside the agent's 300-second decision cycle **and**
inside property-based test suites without any external fixtures.

The base class provides helpers that every concrete model uses:

* ``_coerce_history`` — normalize heterogeneous ``FuelEvent``-like records
  (raw dicts from ES, Pydantic models, or parsed POD records) into a single
  ``list[dict]`` shape the math can rely on. The forecasting agent persists
  historical deliveries as ES ``_source`` dicts today so a dict-oriented
  normalizer is the lowest-friction contract.
* ``_coerce_weather`` — normalize ``DailyWeather`` rows (the Pydantic model
  from :mod:`Agents.support.weather_provider`) or dicts into an ordered
  ``list[DailyWeather]`` sorted by date.
* ``_now`` — single injectable clock source so tests can freeze ``as_of``.

Validates: Requirements 1.5.1, 1.5.2, 1.5.4, 1.5.5, 1.5.6.
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuel.customer_tank_models import CustomerTank
from fuel.services.weather_provider import DailyWeather, compute_hdd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants — tuned to the defaults the design document describes.
# ---------------------------------------------------------------------------

#: Residential-default K-factor used by :class:`PropaneKFactorModel` when
#: there is insufficient history to learn a tank-specific K (Requirement
#: 1.5.5). The design text specifies 0.7 for the universal default; the
#: forecaster can override per customer-type via the Redis config.
DEFAULT_RESIDENTIAL_K_FACTOR: float = 0.7

#: Commercial-default K-factor used when the tank's customer_type is
#: ``commercial`` / ``keep_full`` / ``auto_fill`` and no history exists.
DEFAULT_COMMERCIAL_K_FACTOR: float = 0.9

#: Low-confidence score returned when any model falls back to a default
#: because a tank-specific fit was unreachable (Req 1.5.5).
DEFAULT_FALLBACK_CONFIDENCE: float = 0.3

#: Minimum number of complete delivery intervals required before the
#: propane K-factor model estimates a tank-specific coefficient (Req
#: 1.5.4). Below this the model falls back to the customer-type default.
MIN_PROPANE_INTERVALS: int = 3

#: R² threshold below which the heating-oil regression model falls back
#: to a pure-HDD slope estimate (Req 1.5.2 + design §"fall back to pure
#: HDD slope if R² < 0.5").
HEATING_OIL_R_SQUARED_THRESHOLD: float = 0.5

#: Rolling window (days) used by :class:`DieselRollingModel` (design
#: §"28-day rolling average with simple day-of-week seasonality factor").
DIESEL_ROLLING_DAYS: int = 28

#: Baseline propane "pilot-light" load in gallons/day. Applied on top of
#: K × HDD so a tank still consumes a small amount when HDD is zero.
DEFAULT_PROPANE_BASE_LOAD_GPD: float = 0.1

#: Default projected generator runtime when the tank has no runtime
#: history — matches a "backup generator averaging 1 hour/day" heuristic
#: in the design doc.
DEFAULT_GENERATOR_RUNTIME_HOURS_PER_DAY: float = 1.0

#: Default generator fuel burn rate in gallons/hour when the customer
#: profile does not specify one (Req 1.5.2). Conservative mid-range for a
#: 20–30 kW standby unit.
DEFAULT_GENERATOR_GALLONS_PER_HOUR: float = 2.0

#: Forecast horizon in days for the weather-HDD integration. The
#: forecaster queries a 14-day trailing + 7-day forward window; this is
#: the forward half that feeds the prediction.
DEFAULT_WEATHER_HORIZON_DAYS: int = 7


# ---------------------------------------------------------------------------
# Prediction model
# ---------------------------------------------------------------------------


class ConsumptionPrediction(BaseModel):
    """Consumption forecast for a single customer tank.

    All fields are shaped so the forecasting agent can persist them
    alongside the rest of the ``mvp_tank_forecasts`` document without
    extra transformations. The ``features_used`` map is the richest piece
    of debugging context available to operators — it carries the K-factor,
    regression coefficients, rolling averages, or runtime inputs that
    produced the prediction.
    """

    model_config = ConfigDict(extra="forbid")

    gallons_per_day: float = Field(
        ...,
        ge=0.0,
        description=(
            "Projected daily consumption in US gallons. Non-negative by "
            "contract (Req 1.5.7). Models that would otherwise return a "
            "negative number clamp at zero."
        ),
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Forecast confidence in ``[0, 1]`` (Req 1.5.7). Models derive "
            "this from data volume, regression fit, or fallback status."
        ),
    )
    model_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Short identifier (``retail_station_rolling_7d``, "
            "``propane_k_factor``, etc.) stamped on every forecast for "
            "auditability (Req 1.5.6)."
        ),
    )
    features_used: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured inputs (K-factor, R², rolling averages, "
            "runtime hours, etc.) that produced the prediction. The "
            "forecaster embeds this map verbatim in the forecast doc."
        ),
    )
    anomaly_flags: List[str] = Field(
        default_factory=list,
        description=(
            "Qualitative flags the model surfaced while computing the "
            "prediction (``insufficient_history``, ``regression_poor_fit``, "
            "``weather_fallback``, etc.)."
        ),
    )
    as_of: datetime = Field(
        ...,
        description="Wall-clock time at which the prediction was evaluated.",
    )

    @field_validator("model_name")
    @classmethod
    def _strip_model_name(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("model_name must be a non-empty string")
        return value.strip()


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


HistoryEntry = Mapping[str, Any]
WeatherEntry = Union[DailyWeather, Mapping[str, Any]]


class ConsumptionModel(ABC):
    """Abstract ``Consumption_Model`` strategy.

    Subclasses implement :meth:`predict` with the algorithm specific to
    their fuel family. The base class provides shared helpers plus default
    argument coercion so call sites can pass raw ES dicts without needing
    to pre-build :class:`DailyWeather` objects every time.

    The abstract method is ``async`` to match the design document's
    interface (``async predict(...) -> ConsumptionPrediction``). None of
    the bundled concrete models currently ``await`` anything internally —
    they run pure-Python math — but the async signature keeps the door
    open for future models that query external services (e.g. a hosted
    regression service).
    """

    #: Short identifier stamped on predictions. Concrete subclasses MUST
    #: override. Used both in logs and in forecast documents.
    model_name: ClassVar[str] = "abstract"

    def __init__(self, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        # ``clock`` is injectable for deterministic tests. Default to a
        # timezone-aware UTC stamp so persisted timestamps are consistent.
        self._clock = clock or _default_clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @abstractmethod
    async def predict(
        self,
        tank_id: str,
        historical_events: Sequence[HistoryEntry],
        weather: Sequence[WeatherEntry],
        *,
        customer_type: Optional[str] = None,
        horizon_days: int = DEFAULT_WEATHER_HORIZON_DAYS,
        calibrated_k_factor: Optional[float] = None,
    ) -> ConsumptionPrediction:
        """Return a :class:`ConsumptionPrediction` for the tank.

        Args:
            tank_id: Opaque tank identifier. Models use it only for
                logging / anomaly-flag context; they never query the
                tank catalog here.
            historical_events: Iterable of delivery / consumption records
                shaped like the ``fuel_events`` index (``timestamp``,
                ``quantity_gallons``, etc.). Either a dict or an object
                with the same keys works — the base class coerces both.
            weather: Iterable of :class:`DailyWeather` rows (or dicts
                shaped like one) covering the forecast window. Empty is
                allowed; models that need weather annotate
                ``weather_fallback``.
            customer_type: Optional customer-segment hint used when the
                tank has no history and the model needs a default K / base
                load. Matches the ``customer_type`` enum on CustomerTank.
            horizon_days: Forecast horizon in days. Defaults to 7; the
                propane and heating-oil models average the forward HDD
                window over this many days before applying their slope.
            calibrated_k_factor: Optional operator-approved K-factor from
                the K-Factor Calibration workflow (stored on
                ``CustomerTank.k_factor``). When present and positive, the
                propane model treats it as authoritative and uses it
                instead of the learned/default K (Req 9.5). Other models
                that do not model a single K ignore it.

        Returns:
            A non-negative :class:`ConsumptionPrediction`.
        """

    # ------------------------------------------------------------------
    # Helpers (protected)
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_history(
        historical_events: Sequence[HistoryEntry],
    ) -> List[Dict[str, Any]]:
        """Return a normalized list of history dicts sorted by timestamp."""

        if not historical_events:
            return []
        out: List[Dict[str, Any]] = []
        for entry in historical_events:
            if isinstance(entry, Mapping):
                record = dict(entry)
            else:
                # Pydantic model or object with attributes — try the usual
                # dump path first, fall back to ``vars`` otherwise.
                dump = getattr(entry, "model_dump", None)
                if callable(dump):
                    record = dump(mode="python")
                else:
                    try:
                        record = dict(vars(entry))
                    except TypeError:
                        logger.debug(
                            "ConsumptionModel: skipping non-mappable "
                            "history entry %r",
                            entry,
                        )
                        continue
            out.append(record)

        # Sort by whichever timestamp key is present. Pick the first one
        # that yields a parseable value; otherwise leave in input order.
        def _sort_key(record: Dict[str, Any]) -> datetime:
            for key in ("timestamp", "delivered_at", "event_time"):
                value = record.get(key)
                if value is not None:
                    parsed = _parse_timestamp(value)
                    if parsed is not None:
                        return parsed
            # Records without a parseable timestamp sort to the epoch so
            # they do not destabilize ordering.
            return datetime.min.replace(tzinfo=timezone.utc)

        out.sort(key=_sort_key)
        return out

    @staticmethod
    def _coerce_weather(weather: Sequence[WeatherEntry]) -> List[DailyWeather]:
        """Return a normalized ``list[DailyWeather]`` sorted by date.

        Entries that fail validation (e.g. NaN ``avg_temp_f``) are logged
        at debug level and dropped; the forecaster prefers degraded
        predictions over an exception thrown from the forecast cycle.
        """

        out: List[DailyWeather] = []
        if not weather:
            return out
        for entry in weather:
            if isinstance(entry, DailyWeather):
                if _weather_is_finite(entry):
                    out.append(entry)
                continue
            if isinstance(entry, Mapping):
                try:
                    row = DailyWeather(**entry)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "ConsumptionModel: dropping unparseable weather "
                        "entry %r: %s",
                        entry,
                        exc,
                    )
                    continue
                if _weather_is_finite(row):
                    out.append(row)
                continue
            logger.debug(
                "ConsumptionModel: skipping non-mapping weather entry %r",
                entry,
            )
        out.sort(key=lambda r: r.date)
        return out

    def _now(self) -> datetime:
        """Return the model's notion of "now" (injectable for tests)."""

        return self._clock()


# ---------------------------------------------------------------------------
# Strategy: retail station rolling 7-day average
# ---------------------------------------------------------------------------


class RetailStationModel(ConsumptionModel):
    """Rolling 7-day average consumption for retail stations.

    The existing :mod:`Agents.autonomous.fuel_calculations` module exposes
    pure refill-math helpers but does not itself compute a rolling
    gallons/day figure — that math lives inline in the overlay
    ``TankForecastingAgent`` and operates in **liters/hour**. Rather than
    tangling this strategy with the legacy agent, we replicate the same
    rolling window here in **gallons/day**:

        gallons/day = (sum delivered gallons in last 7 days) / (days covered)

    The model is gallons-first because every new US-market Consumption
    Model operates in gallons; agents that still carry liters elsewhere
    can convert with :func:`services.unit_conversion.to_canonical_volume`.

    Anomaly flags:
        * ``no_history``       — zero events in the 7-day window.
        * ``single_event``     — only one event (confidence capped).
    """

    model_name: ClassVar[str] = "retail_station_rolling_7d"

    #: Rolling window in days. Kept as a class attribute so tests can
    #: override without sub-classing.
    window_days: ClassVar[int] = 7

    async def predict(
        self,
        tank_id: str,
        historical_events: Sequence[HistoryEntry],
        weather: Sequence[WeatherEntry],
        *,
        customer_type: Optional[str] = None,
        horizon_days: int = DEFAULT_WEATHER_HORIZON_DAYS,
    ) -> ConsumptionPrediction:
        now = self._now()
        history = self._coerce_history(historical_events)
        cutoff = now - timedelta(days=self.window_days)

        gallons_in_window = 0.0
        events_in_window: List[Dict[str, Any]] = []
        for record in history:
            ts = _parse_timestamp(record.get("timestamp") or record.get("delivered_at"))
            if ts is None or ts < cutoff:
                continue
            qty = _as_non_negative_float(
                record.get("quantity_gallons")
                or record.get("gallons")
                or record.get("quantity_liters_gallons")
            )
            if qty is None:
                continue
            gallons_in_window += qty
            events_in_window.append(record)

        anomalies: List[str] = []
        if not events_in_window:
            anomalies.append("no_history")
            return ConsumptionPrediction(
                gallons_per_day=0.0,
                confidence=0.1,
                model_name=self.model_name,
                features_used={
                    "tank_id": tank_id,
                    "window_days": self.window_days,
                    "events_in_window": 0,
                },
                anomaly_flags=anomalies,
                as_of=now,
            )

        gallons_per_day = gallons_in_window / float(self.window_days)
        # Confidence scales with the number of events observed. Cap at 0.9
        # so the agent can still reduce confidence downstream for anomaly
        # flags (sensor drift, station outage, etc.).
        confidence = min(0.9, 0.2 + 0.1 * len(events_in_window))
        if len(events_in_window) == 1:
            anomalies.append("single_event")
            confidence = min(confidence, 0.3)

        return ConsumptionPrediction(
            gallons_per_day=max(0.0, gallons_per_day),
            confidence=confidence,
            model_name=self.model_name,
            features_used={
                "tank_id": tank_id,
                "window_days": self.window_days,
                "events_in_window": len(events_in_window),
                "gallons_in_window": round(gallons_in_window, 3),
            },
            anomaly_flags=anomalies,
            as_of=now,
        )


# ---------------------------------------------------------------------------
# Strategy: propane K-factor × HDD + base load
# ---------------------------------------------------------------------------


class PropaneKFactorModel(ConsumptionModel):
    """Propane consumption model driven by a K-factor and HDD.

    K-factor learning (Requirement 1.5.4):

        For each pair of consecutive deliveries, compute the HDD sum in
        the interval between them and solve for the per-interval K:

            gallons_delivered ≈ K * Σ HDD + base_load * days

        Rearranging:

            K_i = (gallons_delivered − base_load * days) / Σ HDD_i

        Average across all valid intervals to get the tank-specific K.

    Prediction:

        gallons/day = K * average(forecast_HDD) + base_load

    Fallback (Requirement 1.5.5):

        Fewer than 3 complete intervals → use customer-type default K,
        annotate ``insufficient_history``, clamp confidence to ``0.3``.

    Weather fallback:

        Zero finite HDD samples in the forecast window → use base_load
        only, annotate ``weather_fallback``, clamp confidence to ``0.2``.
    """

    model_name: ClassVar[str] = "propane_k_factor"

    def __init__(
        self,
        *,
        base_load_gpd: float = DEFAULT_PROPANE_BASE_LOAD_GPD,
        residential_k: float = DEFAULT_RESIDENTIAL_K_FACTOR,
        commercial_k: float = DEFAULT_COMMERCIAL_K_FACTOR,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(clock=clock)
        if base_load_gpd < 0:
            raise ValueError("base_load_gpd must be non-negative")
        self._base_load = float(base_load_gpd)
        self._residential_k = float(residential_k)
        self._commercial_k = float(commercial_k)

    async def predict(
        self,
        tank_id: str,
        historical_events: Sequence[HistoryEntry],
        weather: Sequence[WeatherEntry],
        *,
        customer_type: Optional[str] = None,
        horizon_days: int = DEFAULT_WEATHER_HORIZON_DAYS,
        calibrated_k_factor: Optional[float] = None,
    ) -> ConsumptionPrediction:
        now = self._now()
        history = self._coerce_history(historical_events)
        weather_rows = self._coerce_weather(weather)

        # K-factor precedence (Req 9.5):
        #   1. Operator-approved calibrated K (weather-normalized, human
        #      reviewed) — authoritative when present and positive.
        #   2. Learned K from delivery history (>= MIN_PROPANE_INTERVALS).
        #   3. Customer-type default constant.
        k_result = _learn_propane_k_factor(
            history=history,
            weather_rows=weather_rows,
            base_load_gpd=self._base_load,
            min_intervals=MIN_PROPANE_INTERVALS,
        )
        anomalies: List[str] = list(k_result["anomaly_flags"])
        learned_k = k_result["learned_k"]

        k_factor: float
        k_factor_source: str
        if calibrated_k_factor is not None and calibrated_k_factor > 0:
            k_factor = float(calibrated_k_factor)
            k_factor_source = "calibrated"
        elif learned_k is not None:
            k_factor = float(learned_k)
            k_factor_source = "learned"
        else:
            k_factor = _default_k_for_customer_type(
                customer_type,
                residential=self._residential_k,
                commercial=self._commercial_k,
            )
            k_factor_source = "default"
            anomalies.append("insufficient_history")

        # A calibrated K is human-approved, so a thin history no longer
        # implies low confidence — only the weather-fallback path does.
        used_default_k = k_factor_source == "default"

        # Average HDD across the forward horizon.
        forecast_hdd_samples = _select_forecast_hdd(weather_rows, now, horizon_days)
        if forecast_hdd_samples:
            avg_hdd = sum(forecast_hdd_samples) / len(forecast_hdd_samples)
            gpd = k_factor * avg_hdd + self._base_load
            weather_fallback = False
        else:
            avg_hdd = 0.0
            gpd = self._base_load
            weather_fallback = True
            anomalies.append("weather_fallback")

        # Confidence: strong when we have a learned K *and* weather.
        if used_default_k or weather_fallback:
            confidence = DEFAULT_FALLBACK_CONFIDENCE
        else:
            # Scale with interval count, capped at 0.85.
            confidence = min(0.85, 0.4 + 0.1 * k_result["intervals_used"])

        features_used: Dict[str, Any] = {
            "tank_id": tank_id,
            "k_factor": round(k_factor, 4),
            "k_factor_source": k_factor_source,
            "base_load_gpd": self._base_load,
            "forecast_hdd_avg": round(avg_hdd, 3),
            "forecast_hdd_days": len(forecast_hdd_samples),
            "intervals_used": k_result["intervals_used"],
            "customer_type": customer_type,
        }

        return ConsumptionPrediction(
            gallons_per_day=max(0.0, gpd),
            confidence=confidence,
            model_name=self.model_name,
            features_used=features_used,
            anomaly_flags=anomalies,
            as_of=now,
        )


# ---------------------------------------------------------------------------
# Strategy: heating-oil linear regression
# ---------------------------------------------------------------------------


class HeatingOilHDDRegressionModel(ConsumptionModel):
    """Linear regression: ``gallons/day = a * HDD + b``.

    The model fits an ordinary-least-squares line over pairs of
    ``(HDD, gallons/day)`` inferred from consecutive deliveries in the
    history. When R² falls below :data:`HEATING_OIL_R_SQUARED_THRESHOLD`
    it falls back to a pure-HDD slope (K × HDD, no intercept) and clamps
    confidence to ``DEFAULT_FALLBACK_CONFIDENCE`` (Req 1.5.2 + design).

    Fallbacks:

        * ``< 2 complete intervals``     → propane-style default K (0.7)
          with ``insufficient_history``.
        * ``2 ≤ n < 3`` intervals        → use slope-only K estimate.
        * ``R² < 0.5``                   → slope-only K estimate,
          ``regression_poor_fit``.
        * No weather                     → base_load only,
          ``weather_fallback``.

    Confidence is the R² on success, clamped into
    ``[DEFAULT_FALLBACK_CONFIDENCE, 0.95]``. Fallbacks use
    :data:`DEFAULT_FALLBACK_CONFIDENCE`.
    """

    model_name: ClassVar[str] = "heating_oil_hdd_regression"

    def __init__(
        self,
        *,
        base_load_gpd: float = 0.0,
        default_k_factor: float = DEFAULT_RESIDENTIAL_K_FACTOR,
        r_squared_threshold: float = HEATING_OIL_R_SQUARED_THRESHOLD,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(clock=clock)
        if base_load_gpd < 0:
            raise ValueError("base_load_gpd must be non-negative")
        if not 0.0 <= r_squared_threshold <= 1.0:
            raise ValueError("r_squared_threshold must be in [0, 1]")
        self._base_load = float(base_load_gpd)
        self._default_k = float(default_k_factor)
        self._r2_threshold = float(r_squared_threshold)

    async def predict(
        self,
        tank_id: str,
        historical_events: Sequence[HistoryEntry],
        weather: Sequence[WeatherEntry],
        *,
        customer_type: Optional[str] = None,
        horizon_days: int = DEFAULT_WEATHER_HORIZON_DAYS,
    ) -> ConsumptionPrediction:
        now = self._now()
        history = self._coerce_history(historical_events)
        weather_rows = self._coerce_weather(weather)

        # Build (avg_HDD, gallons_per_day) samples from consecutive deliveries.
        samples = _build_heating_oil_samples(
            history=history, weather_rows=weather_rows
        )

        anomalies: List[str] = []
        forecast_hdd_samples = _select_forecast_hdd(weather_rows, now, horizon_days)
        avg_forecast_hdd = (
            sum(forecast_hdd_samples) / len(forecast_hdd_samples)
            if forecast_hdd_samples
            else 0.0
        )
        weather_fallback = not forecast_hdd_samples

        # Dispatch on sample count and fit quality.
        slope: float
        intercept: float
        r_squared: float
        fit_source: str
        confidence: float

        if len(samples) < 2:
            # Not enough data to fit anything — fall back to a default K
            # and flag insufficient history (Req 1.5.5 style).
            slope = self._default_k
            intercept = self._base_load
            r_squared = 0.0
            fit_source = "default_k"
            confidence = DEFAULT_FALLBACK_CONFIDENCE
            anomalies.append("insufficient_history")
        else:
            slope, intercept, r_squared = _ols_fit(samples)
            if r_squared < self._r2_threshold or not math.isfinite(slope):
                # Poor fit — fall back to a slope-only estimate using the
                # naive ratio of total gallons to total HDD (same shape as
                # the propane K-factor).
                k_est = _slope_only_k(samples)
                slope = k_est if math.isfinite(k_est) else self._default_k
                intercept = self._base_load
                fit_source = "slope_only"
                confidence = DEFAULT_FALLBACK_CONFIDENCE
                anomalies.append("regression_poor_fit")
            else:
                fit_source = "ols"
                confidence = max(
                    DEFAULT_FALLBACK_CONFIDENCE,
                    min(0.95, r_squared),
                )

        if weather_fallback:
            gpd = intercept if fit_source == "ols" else self._base_load
            anomalies.append("weather_fallback")
            confidence = min(confidence, DEFAULT_FALLBACK_CONFIDENCE)
        else:
            gpd = slope * avg_forecast_hdd + intercept

        features_used: Dict[str, Any] = {
            "tank_id": tank_id,
            "fit_source": fit_source,
            "slope": round(slope, 4),
            "intercept": round(intercept, 4),
            "r_squared": round(r_squared, 4),
            "samples": len(samples),
            "forecast_hdd_avg": round(avg_forecast_hdd, 3),
            "forecast_hdd_days": len(forecast_hdd_samples),
            "customer_type": customer_type,
        }

        return ConsumptionPrediction(
            gallons_per_day=max(0.0, gpd),
            confidence=confidence,
            model_name=self.model_name,
            features_used=features_used,
            anomaly_flags=anomalies,
            as_of=now,
        )


# ---------------------------------------------------------------------------
# Strategy: diesel 28-day rolling average with weekday seasonality
# ---------------------------------------------------------------------------


class DieselRollingModel(ConsumptionModel):
    """Diesel consumption: 28-day rolling average with weekday seasonality.

    Algorithm:

        1. Aggregate daily gallons from every delivery / consumption event
           in the trailing 28-day window (bucketed by ``date``).
        2. Compute the overall mean as the baseline gallons/day.
        3. Compute the weekday-specific mean for the day the forecast
           covers — today if ``horizon_days == 1``, else the weekday
           centroid of the forward window.
        4. Seasonality factor = weekday_mean / overall_mean. Clamped to
           ``[0.5, 1.5]`` so a sparsely-sampled weekday does not produce
           a wildly skewed forecast.
        5. gallons/day = baseline * seasonality_factor.

    Anomaly flags:

        * ``no_history``           — no events in the rolling window.
        * ``single_weekday``       — only one weekday observed (factor
          forced to 1.0 for numerical stability).
    """

    model_name: ClassVar[str] = "diesel_rolling_28d"

    #: Bounds applied to the weekday/overall seasonality factor. Chosen
    #: so a thin data set never produces a >=2× prediction.
    min_season_factor: ClassVar[float] = 0.5
    max_season_factor: ClassVar[float] = 1.5

    def __init__(
        self,
        *,
        window_days: int = DIESEL_ROLLING_DAYS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(clock=clock)
        if window_days <= 0:
            raise ValueError("window_days must be positive")
        self._window_days = int(window_days)

    async def predict(
        self,
        tank_id: str,
        historical_events: Sequence[HistoryEntry],
        weather: Sequence[WeatherEntry],
        *,
        customer_type: Optional[str] = None,
        horizon_days: int = DEFAULT_WEATHER_HORIZON_DAYS,
    ) -> ConsumptionPrediction:
        now = self._now()
        history = self._coerce_history(historical_events)
        cutoff = now - timedelta(days=self._window_days)

        daily: Dict[date, float] = {}
        for record in history:
            ts = _parse_timestamp(
                record.get("timestamp") or record.get("delivered_at")
            )
            if ts is None or ts < cutoff:
                continue
            qty = _as_non_negative_float(
                record.get("quantity_gallons")
                or record.get("gallons")
                or record.get("quantity_liters_gallons")
            )
            if qty is None:
                continue
            day = ts.date()
            daily[day] = daily.get(day, 0.0) + qty

        anomalies: List[str] = []
        if not daily:
            anomalies.append("no_history")
            return ConsumptionPrediction(
                gallons_per_day=0.0,
                confidence=0.1,
                model_name=self.model_name,
                features_used={
                    "tank_id": tank_id,
                    "window_days": self._window_days,
                    "days_observed": 0,
                },
                anomaly_flags=anomalies,
                as_of=now,
            )

        days_observed = len(daily)
        total_gallons = sum(daily.values())
        # Average over the full window, not just days with events, so a
        # 28-day window with 4 sparse delivery days still yields a realistic
        # "per-day" rate.
        baseline = total_gallons / float(self._window_days)

        # Weekday seasonality — group the daily totals by weekday, then
        # pick the target weekday (tomorrow by default, or the centroid
        # of the forward window if horizon_days > 1).
        by_weekday: Dict[int, List[float]] = {}
        for day, total in daily.items():
            by_weekday.setdefault(day.weekday(), []).append(total)

        target_weekday = _target_weekday(now, horizon_days)
        overall_mean = total_gallons / float(days_observed)
        weekday_series = by_weekday.get(target_weekday)
        if not weekday_series or len(by_weekday) == 1:
            anomalies.append("single_weekday")
            season_factor = 1.0
        else:
            weekday_mean = sum(weekday_series) / len(weekday_series)
            raw_factor = weekday_mean / overall_mean if overall_mean > 0 else 1.0
            season_factor = max(
                self.min_season_factor, min(self.max_season_factor, raw_factor)
            )

        gpd = max(0.0, baseline * season_factor)
        # Confidence scales with coverage — more distinct days → higher.
        confidence = min(0.9, 0.3 + 0.02 * days_observed)

        features_used: Dict[str, Any] = {
            "tank_id": tank_id,
            "window_days": self._window_days,
            "days_observed": days_observed,
            "baseline_gpd": round(baseline, 3),
            "season_factor": round(season_factor, 4),
            "target_weekday": target_weekday,
            "gallons_in_window": round(total_gallons, 3),
        }
        return ConsumptionPrediction(
            gallons_per_day=gpd,
            confidence=confidence,
            model_name=self.model_name,
            features_used=features_used,
            anomaly_flags=anomalies,
            as_of=now,
        )


# ---------------------------------------------------------------------------
# Strategy: generator runtime × gallons/hour
# ---------------------------------------------------------------------------


class GeneratorRuntimeModel(ConsumptionModel):
    """gallons/day = gallons_per_hour × projected_runtime_hours_per_day.

    Inputs resolution:

        * ``gallons_per_hour`` — first of:
            1. tank history entry with a ``gallons_per_hour`` /
               ``fuel_burn_gph`` field (most recent one wins);
            2. constructor argument (``profile_gallons_per_hour``);
            3. :data:`DEFAULT_GENERATOR_GALLONS_PER_HOUR`.
        * ``projected_runtime_hours_per_day`` — first of:
            1. average of ``runtime_hours`` across the last 7 recorded
               runtime events (if present);
            2. constructor argument (``profile_runtime_hours_per_day``);
            3. :data:`DEFAULT_GENERATOR_RUNTIME_HOURS_PER_DAY`.

    Confidence scales with the number of runtime events observed; when
    the prediction falls back entirely to defaults the confidence is
    :data:`DEFAULT_FALLBACK_CONFIDENCE`.
    """

    model_name: ClassVar[str] = "generator_runtime"

    def __init__(
        self,
        *,
        profile_gallons_per_hour: Optional[float] = None,
        profile_runtime_hours_per_day: Optional[float] = None,
        lookback_events: int = 7,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        super().__init__(clock=clock)
        if profile_gallons_per_hour is not None and profile_gallons_per_hour < 0:
            raise ValueError("profile_gallons_per_hour must be non-negative")
        if (
            profile_runtime_hours_per_day is not None
            and profile_runtime_hours_per_day < 0
        ):
            raise ValueError("profile_runtime_hours_per_day must be non-negative")
        if lookback_events <= 0:
            raise ValueError("lookback_events must be positive")
        self._profile_gph = profile_gallons_per_hour
        self._profile_runtime = profile_runtime_hours_per_day
        self._lookback = int(lookback_events)

    async def predict(
        self,
        tank_id: str,
        historical_events: Sequence[HistoryEntry],
        weather: Sequence[WeatherEntry],
        *,
        customer_type: Optional[str] = None,
        horizon_days: int = DEFAULT_WEATHER_HORIZON_DAYS,
    ) -> ConsumptionPrediction:
        now = self._now()
        history = self._coerce_history(historical_events)

        gph, gph_source = self._resolve_gallons_per_hour(history)
        runtime, runtime_source, n_runtime_events = self._resolve_runtime_hours(history)

        gpd = max(0.0, gph * runtime)

        anomalies: List[str] = []
        if gph_source == "default":
            anomalies.append("default_gallons_per_hour")
        if runtime_source == "default":
            anomalies.append("default_runtime_hours")

        if gph_source == "default" and runtime_source == "default":
            confidence = DEFAULT_FALLBACK_CONFIDENCE
        elif gph_source == "default" or runtime_source == "default":
            # One side defaulted — keep confidence modest.
            confidence = min(0.6, 0.35 + 0.05 * n_runtime_events)
        else:
            confidence = min(0.9, 0.5 + 0.05 * n_runtime_events)

        features_used: Dict[str, Any] = {
            "tank_id": tank_id,
            "gallons_per_hour": round(gph, 4),
            "gallons_per_hour_source": gph_source,
            "projected_runtime_hours_per_day": round(runtime, 4),
            "projected_runtime_source": runtime_source,
            "runtime_events": n_runtime_events,
            "customer_type": customer_type,
        }
        return ConsumptionPrediction(
            gallons_per_day=gpd,
            confidence=confidence,
            model_name=self.model_name,
            features_used=features_used,
            anomaly_flags=anomalies,
            as_of=now,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_gallons_per_hour(
        self, history: List[Dict[str, Any]]
    ) -> tuple[float, str]:
        for record in reversed(history):  # most recent first
            for key in ("gallons_per_hour", "fuel_burn_gph"):
                val = _as_non_negative_float(record.get(key))
                if val is not None and val > 0:
                    return val, "history"
        if self._profile_gph is not None and self._profile_gph > 0:
            return float(self._profile_gph), "profile"
        return DEFAULT_GENERATOR_GALLONS_PER_HOUR, "default"

    def _resolve_runtime_hours(
        self, history: List[Dict[str, Any]]
    ) -> tuple[float, str, int]:
        runtimes: List[float] = []
        for record in reversed(history):
            val = _as_non_negative_float(record.get("runtime_hours"))
            if val is None:
                continue
            runtimes.append(val)
            if len(runtimes) >= self._lookback:
                break
        if runtimes:
            return sum(runtimes) / len(runtimes), "history", len(runtimes)
        if self._profile_runtime is not None and self._profile_runtime > 0:
            return float(self._profile_runtime), "profile", 0
        return DEFAULT_GENERATOR_RUNTIME_HOURS_PER_DAY, "default", 0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


#: Short-name registry used by :func:`build_consumption_model`. Agents
#: select a strategy by fuel_type today — the registry keeps the option
#: of a tenant-level override (``consumption_model_config:{tenant_id}``)
#: open without requiring every caller to hand-roll an import map.
CONSUMPTION_MODEL_REGISTRY: Dict[str, type] = {
    "retail_station_rolling_7d": RetailStationModel,
    "retail": RetailStationModel,
    "propane_k_factor": PropaneKFactorModel,
    "propane": PropaneKFactorModel,
    "heating_oil_hdd_regression": HeatingOilHDDRegressionModel,
    "heating_oil": HeatingOilHDDRegressionModel,
    "diesel_rolling_28d": DieselRollingModel,
    "diesel": DieselRollingModel,
    "generator_runtime": GeneratorRuntimeModel,
    "generator_fuel": GeneratorRuntimeModel,
    "generator": GeneratorRuntimeModel,
}


def build_consumption_model(name: str, **kwargs: Any) -> ConsumptionModel:
    """Return a concrete :class:`ConsumptionModel` by short name.

    ``name`` matching is case-insensitive. Unknown names raise
    :class:`ValueError` so callers surface a useful error rather than a
    silent default.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("consumption model name must be a non-empty string")
    key = name.strip().lower()
    cls = CONSUMPTION_MODEL_REGISTRY.get(key)
    if cls is None:
        raise ValueError(
            f"unknown consumption model {name!r}; "
            f"valid names: {sorted(set(CONSUMPTION_MODEL_REGISTRY))}"
        )
    return cls(**kwargs)


def select_consumption_model_for_tank(
    tank: CustomerTank,
    **kwargs: Any,
) -> ConsumptionModel:
    """Return the default :class:`ConsumptionModel` for a customer tank.

    Dispatches on ``tank.fuel_type``. Unknown fuel types default to
    :class:`DieselRollingModel` because a rolling average is the safest
    generic fallback (it works on any gallons/date stream).
    """

    mapping = {
        "propane": PropaneKFactorModel,
        "heating_oil": HeatingOilHDDRegressionModel,
        "diesel": DieselRollingModel,
        "farm_fuel": DieselRollingModel,
        "gasoline": RetailStationModel,
        "generator_fuel": GeneratorRuntimeModel,
    }
    cls = mapping.get(tank.fuel_type, DieselRollingModel)
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Return a timezone-aware UTC now. Injected at construction time."""

    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Return a timezone-aware ``datetime`` or ``None``.

    Accepts ISO-8601 strings (with or without ``Z``) and native
    ``datetime`` instances. Naive datetimes are assumed UTC.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _as_non_negative_float(value: Any) -> Optional[float]:
    """Return a non-negative finite ``float`` or ``None``."""

    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    if out < 0:
        return None
    return out


def _weather_is_finite(row: DailyWeather) -> bool:
    """Reject weather rows whose HDD or temperature is NaN / infinite."""

    return math.isfinite(row.avg_temp_f) and math.isfinite(row.hdd)


def _select_forecast_hdd(
    weather_rows: Sequence[DailyWeather],
    now: datetime,
    horizon_days: int,
) -> List[float]:
    """Return HDD values for the forecast window.

    We prefer rows strictly in the future (date > today). If none are
    available (common in tests that only supply historical weather) fall
    back to the most recent ``horizon_days`` days.
    """

    if not weather_rows or horizon_days <= 0:
        return []
    today = now.date()
    forward = [row.hdd for row in weather_rows if row.date > today]
    if forward:
        return forward[:horizon_days]
    tail = [row.hdd for row in weather_rows[-horizon_days:]]
    return tail


def _learn_propane_k_factor(
    *,
    history: List[Dict[str, Any]],
    weather_rows: List[DailyWeather],
    base_load_gpd: float,
    min_intervals: int,
) -> Dict[str, Any]:
    """Estimate a per-tank K-factor from consecutive deliveries.

    Returns a dict with:
        * ``learned_k``: float or ``None`` when < ``min_intervals``.
        * ``intervals_used``: number of valid intervals averaged.
        * ``anomaly_flags``: list of diagnostic flags.
    """

    hdd_by_date: Dict[date, float] = {row.date: row.hdd for row in weather_rows}
    intervals: List[float] = []
    anomalies: List[str] = []

    for prev, curr in zip(history, history[1:]):
        prev_ts = _parse_timestamp(prev.get("timestamp") or prev.get("delivered_at"))
        curr_ts = _parse_timestamp(curr.get("timestamp") or curr.get("delivered_at"))
        if prev_ts is None or curr_ts is None or curr_ts <= prev_ts:
            continue
        delivered = _as_non_negative_float(
            curr.get("quantity_gallons")
            or curr.get("gallons")
            or curr.get("delivered_gallons")
        )
        if delivered is None or delivered == 0:
            continue
        # Sum HDD across [prev_date, curr_date).
        total_hdd = 0.0
        days_in_interval = 0
        cursor = prev_ts.date()
        end = curr_ts.date()
        while cursor < end:
            total_hdd += hdd_by_date.get(cursor, 0.0)
            cursor = cursor + timedelta(days=1)
            days_in_interval += 1
        if total_hdd <= 0 or days_in_interval == 0:
            continue
        # K = (delivered - base_load * days) / Σ HDD. Guard against
        # negatives when base_load dominates (summer shoulder).
        numerator = delivered - base_load_gpd * days_in_interval
        if numerator <= 0:
            continue
        k = numerator / total_hdd
        if math.isfinite(k) and k > 0:
            intervals.append(k)

    if len(intervals) < min_intervals:
        return {
            "learned_k": None,
            "intervals_used": len(intervals),
            "anomaly_flags": anomalies,
        }
    return {
        "learned_k": sum(intervals) / len(intervals),
        "intervals_used": len(intervals),
        "anomaly_flags": anomalies,
    }


def _build_heating_oil_samples(
    *,
    history: List[Dict[str, Any]],
    weather_rows: List[DailyWeather],
) -> List[tuple[float, float]]:
    """Return ``(avg_HDD, gallons_per_day)`` samples across delivery intervals."""

    hdd_by_date: Dict[date, float] = {row.date: row.hdd for row in weather_rows}
    samples: List[tuple[float, float]] = []
    for prev, curr in zip(history, history[1:]):
        prev_ts = _parse_timestamp(prev.get("timestamp") or prev.get("delivered_at"))
        curr_ts = _parse_timestamp(curr.get("timestamp") or curr.get("delivered_at"))
        if prev_ts is None or curr_ts is None or curr_ts <= prev_ts:
            continue
        delivered = _as_non_negative_float(
            curr.get("quantity_gallons")
            or curr.get("gallons")
            or curr.get("delivered_gallons")
        )
        if delivered is None or delivered <= 0:
            continue
        cursor = prev_ts.date()
        end = curr_ts.date()
        total_hdd = 0.0
        days = 0
        while cursor < end:
            total_hdd += hdd_by_date.get(cursor, 0.0)
            cursor = cursor + timedelta(days=1)
            days += 1
        if days == 0:
            continue
        gallons_per_day = delivered / days
        avg_hdd = total_hdd / days
        if math.isfinite(gallons_per_day) and math.isfinite(avg_hdd):
            samples.append((avg_hdd, gallons_per_day))
    return samples


def _ols_fit(samples: List[tuple[float, float]]) -> tuple[float, float, float]:
    """Fit ``y = slope * x + intercept`` using ordinary least squares.

    Returns ``(slope, intercept, r_squared)``. When the variance of ``x``
    is zero (all HDD equal), returns the mean of ``y`` as the intercept,
    zero slope, and R² = 0.0.
    """

    n = len(samples)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean_x = sum(x for x, _ in samples) / n
    mean_y = sum(y for _, y in samples) / n

    var_x = sum((x - mean_x) ** 2 for x, _ in samples)
    if var_x == 0:
        return 0.0, mean_y, 0.0

    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in samples)
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    # R² = 1 - SS_res / SS_tot.
    ss_tot = sum((y - mean_y) ** 2 for _, y in samples)
    if ss_tot == 0:
        r_squared = 1.0 if cov_xy == 0 else 0.0
    else:
        ss_res = sum(
            (y - (slope * x + intercept)) ** 2 for x, y in samples
        )
        r_squared = max(0.0, min(1.0, 1.0 - ss_res / ss_tot))
    return slope, intercept, r_squared


def _slope_only_k(samples: List[tuple[float, float]]) -> float:
    """Return the K that minimizes ``Σ (y_i - K x_i)²`` (no intercept)."""

    num = sum(x * y for x, y in samples)
    den = sum(x * x for x, _ in samples)
    if den == 0:
        return float("nan")
    return num / den


def _default_k_for_customer_type(
    customer_type: Optional[str],
    *,
    residential: float,
    commercial: float,
) -> float:
    """Return the default K for a customer_type, matching Req 1.5.5.

    Residential → residential default. Commercial / keep_full /
    auto_fill → commercial default. Everything else (including
    ``None``) → residential.
    """

    if customer_type in ("commercial", "keep_full", "auto_fill"):
        return commercial
    return residential


def _target_weekday(now: datetime, horizon_days: int) -> int:
    """Return the weekday (0=Mon…6=Sun) the forecast is primarily for.

    For a 1-day horizon we forecast "tomorrow". For multi-day horizons we
    pick the centroid weekday — this matches the design doc's "simple
    day-of-week seasonality factor" spirit while keeping the result
    deterministic.
    """

    offset = max(1, horizon_days // 2 if horizon_days > 1 else 1)
    target = (now + timedelta(days=offset)).weekday()
    return target


__all__ = [
    # Constants
    "DEFAULT_RESIDENTIAL_K_FACTOR",
    "DEFAULT_COMMERCIAL_K_FACTOR",
    "DEFAULT_FALLBACK_CONFIDENCE",
    "MIN_PROPANE_INTERVALS",
    "HEATING_OIL_R_SQUARED_THRESHOLD",
    "DIESEL_ROLLING_DAYS",
    "DEFAULT_PROPANE_BASE_LOAD_GPD",
    "DEFAULT_GENERATOR_RUNTIME_HOURS_PER_DAY",
    "DEFAULT_GENERATOR_GALLONS_PER_HOUR",
    "DEFAULT_WEATHER_HORIZON_DAYS",
    # Models
    "ConsumptionPrediction",
    "ConsumptionModel",
    "RetailStationModel",
    "PropaneKFactorModel",
    "HeatingOilHDDRegressionModel",
    "DieselRollingModel",
    "GeneratorRuntimeModel",
    # Factory
    "CONSUMPTION_MODEL_REGISTRY",
    "build_consumption_model",
    "select_consumption_model_for_tank",
]
