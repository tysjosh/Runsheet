"""
Unit tests for :mod:`Agents.support.consumption_models`.

Covers Capability 1 / Requirements 1.5.1, 1.5.2, 1.5.4, 1.5.5, 1.5.6 of the
fuel-ops hardening spec:

* :class:`ConsumptionPrediction` Pydantic contract — gallons_per_day ≥ 0,
  confidence in [0, 1], required ``model_name`` / ``as_of`` fields, and
  optional ``features_used`` / ``anomaly_flags``.
* :class:`ConsumptionModel` base plumbing — history and weather coercion
  from heterogeneous dict / Pydantic inputs, injectable clock for
  deterministic ``as_of`` stamping.
* :class:`RetailStationModel` — rolling 7-day gallons/day average, zero-
  history fallback, window boundary handling.
* :class:`PropaneKFactorModel` — K-factor learning from ≥3 delivery
  intervals, customer-type default fallback, weather-only fallback.
* :class:`HeatingOilHDDRegressionModel` — OLS fit, R² < 0.5 slope-only
  fallback, default-K fallback with insufficient samples, weather
  fallback.
* :class:`DieselRollingModel` — 28-day rolling baseline with weekday
  seasonality clamp.
* :class:`GeneratorRuntimeModel` — gallons_per_hour × runtime with
  history / profile / default resolution cascade.
* :func:`build_consumption_model` / :func:`select_consumption_model_for_tank`
  registry lookup.

Validates: Requirements 1.5.1, 1.5.2, 1.5.4, 1.5.5, 1.5.6.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from pydantic import ValidationError

from fuel.services.consumption_models import (
    CONSUMPTION_MODEL_REGISTRY,
    DEFAULT_COMMERCIAL_K_FACTOR,
    DEFAULT_FALLBACK_CONFIDENCE,
    DEFAULT_RESIDENTIAL_K_FACTOR,
    ConsumptionModel,
    ConsumptionPrediction,
    DieselRollingModel,
    GeneratorRuntimeModel,
    HeatingOilHDDRegressionModel,
    PropaneKFactorModel,
    RetailStationModel,
    build_consumption_model,
    select_consumption_model_for_tank,
)
from fuel.customer_tank_models import CustomerTank
from fuel.services.weather_provider import DailyWeather, compute_hdd


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


FIXED_NOW = datetime(2024, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock(now: datetime = FIXED_NOW):
    def _now() -> datetime:
        return now
    return _now


def _event(
    day_offset: int,
    gallons: float,
    *,
    now: datetime = FIXED_NOW,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a history-dict for ``day_offset`` days before ``now``."""

    ts = now - timedelta(days=day_offset)
    out = {"timestamp": ts.isoformat(), "quantity_gallons": gallons}
    if extra:
        out.update(extra)
    return out


def _weather_row(
    day_offset: int,
    avg_temp_f: float,
    *,
    now: datetime = FIXED_NOW,
    tenant: str = "t-1",
    zip_code: str = "06001",
    provider: str = "stub",
) -> DailyWeather:
    """Return a :class:`DailyWeather` row anchored on ``now``."""

    d = (now + timedelta(days=day_offset)).date()
    return DailyWeather(
        date=d,
        zip_code=zip_code,
        tenant_id=tenant,
        avg_temp_f=avg_temp_f,
        hdd=compute_hdd(avg_temp_f),
        provider=provider,
        retrieved_at=now,
    )


def _customer_tank(fuel_type: str = "propane", **overrides: Any) -> CustomerTank:
    base = {
        "customer_tank_id": "tank_test",
        "tenant_id": "tenant-A",
        "customer_id": "CUST-1",
        "customer_type": "residential",
        "fuel_type": fuel_type,
        "fuel_product_code": {
            "propane": "PROPANE",
            "heating_oil": "HEATING_OIL",
            "diesel": "DIESEL_2",
            "generator_fuel": "DIESEL_2",
            "farm_fuel": "DIESEL_2",
            "gasoline": "GASOLINE_REG",
        }[fuel_type],
        "capacity_gallons": 500.0,
        "current_level_gallons": 250.0,
        "location_lat": 41.5,
        "location_lon": -72.5,
        "zip_code": "06001",
        "status": "active",
    }
    base.update(overrides)
    return CustomerTank(**base)


# ---------------------------------------------------------------------------
# ConsumptionPrediction
# ---------------------------------------------------------------------------


class TestConsumptionPrediction:
    def test_valid_payload_round_trips(self) -> None:
        pred = ConsumptionPrediction(
            gallons_per_day=5.0,
            confidence=0.8,
            model_name="propane_k_factor",
            features_used={"k": 0.7},
            anomaly_flags=["weather_fallback"],
            as_of=FIXED_NOW,
        )
        dumped = pred.model_dump(mode="json")
        assert dumped["model_name"] == "propane_k_factor"
        assert dumped["gallons_per_day"] == 5.0
        assert dumped["anomaly_flags"] == ["weather_fallback"]

    def test_rejects_negative_gallons(self) -> None:
        with pytest.raises(ValidationError):
            ConsumptionPrediction(
                gallons_per_day=-1.0,
                confidence=0.5,
                model_name="x",
                as_of=FIXED_NOW,
            )

    def test_rejects_confidence_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            ConsumptionPrediction(
                gallons_per_day=1.0,
                confidence=1.5,
                model_name="x",
                as_of=FIXED_NOW,
            )
        with pytest.raises(ValidationError):
            ConsumptionPrediction(
                gallons_per_day=1.0,
                confidence=-0.01,
                model_name="x",
                as_of=FIXED_NOW,
            )

    def test_rejects_blank_model_name(self) -> None:
        with pytest.raises(ValidationError):
            ConsumptionPrediction(
                gallons_per_day=1.0,
                confidence=0.5,
                model_name="   ",
                as_of=FIXED_NOW,
            )

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ConsumptionPrediction(
                gallons_per_day=1.0,
                confidence=0.5,
                model_name="x",
                as_of=FIXED_NOW,
                not_a_field="nope",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Base class helpers
# ---------------------------------------------------------------------------


class _ProbeModel(ConsumptionModel):
    """Minimal ConsumptionModel subclass for exercising the helpers."""

    model_name = "probe"

    async def predict(
        self,
        tank_id: str,
        historical_events,
        weather,
        *,
        customer_type=None,
        horizon_days=7,
    ) -> ConsumptionPrediction:
        return ConsumptionPrediction(
            gallons_per_day=0.0,
            confidence=0.5,
            model_name=self.model_name,
            as_of=self._now(),
        )


class TestBaseHelpers:
    def test_coerce_history_sorts_by_timestamp(self) -> None:
        history = [
            _event(1, 50.0),
            _event(5, 100.0),
            _event(3, 75.0),
        ]
        out = _ProbeModel._coerce_history(history)
        timestamps = [r["timestamp"] for r in out]
        assert timestamps == sorted(timestamps)  # chronological

    def test_coerce_history_handles_pydantic_and_missing_timestamps(self) -> None:
        pydantic_row = DailyWeather(
            date=date(2024, 1, 1),
            zip_code="06001",
            tenant_id="t-1",
            avg_temp_f=30.0,
            hdd=35.0,
            provider="stub",
            retrieved_at=FIXED_NOW,
        )
        dict_row_no_ts = {"quantity_gallons": 10.0}
        dict_row = _event(2, 20.0)
        out = _ProbeModel._coerce_history([pydantic_row, dict_row_no_ts, dict_row])
        assert len(out) == 3
        # The dict without timestamp sorts to the epoch so ordering is stable.
        assert any("quantity_gallons" in r and r.get("quantity_gallons") == 20.0 for r in out)

    def test_coerce_weather_drops_non_finite(self) -> None:
        bad = DailyWeather(
            date=date(2024, 1, 1),
            zip_code="06001",
            tenant_id="t-1",
            avg_temp_f=float("nan"),
            hdd=0.0,  # hdd bound by mapping; we exercise the avg_temp NaN path
            provider="stub",
            retrieved_at=FIXED_NOW,
        )
        good = _weather_row(-1, 20.0)
        out = _ProbeModel._coerce_weather([bad, good])
        assert [r.date for r in out] == [good.date]

    def test_coerce_weather_accepts_dict(self) -> None:
        payload = {
            "date": "2024-02-01",
            "zip_code": "06001",
            "tenant_id": "t-1",
            "avg_temp_f": 25.0,
            "hdd": 40.0,
            "provider": "stub",
            "retrieved_at": FIXED_NOW.isoformat(),
        }
        out = _ProbeModel._coerce_weather([payload])
        assert len(out) == 1 and out[0].hdd == 40.0

    async def test_clock_is_injectable(self) -> None:
        custom = datetime(1999, 12, 31, 23, 59, tzinfo=timezone.utc)
        model = _ProbeModel(clock=_fixed_clock(custom))
        out = await model.predict("t", [], [])
        assert out.as_of == custom


# ---------------------------------------------------------------------------
# RetailStationModel
# ---------------------------------------------------------------------------


class TestRetailStationModel:
    @pytest.fixture
    def model(self) -> RetailStationModel:
        return RetailStationModel(clock=_fixed_clock())

    async def test_no_history_returns_zero_with_low_confidence(
        self, model: RetailStationModel
    ) -> None:
        out = await model.predict("tank_1", [], [])
        assert out.gallons_per_day == 0.0
        assert 0.0 < out.confidence <= 0.15
        assert "no_history" in out.anomaly_flags
        assert out.model_name == "retail_station_rolling_7d"
        assert out.features_used["events_in_window"] == 0

    async def test_rolling_7_day_average(self, model: RetailStationModel) -> None:
        # 700 gallons over 7 days → 100 gpd.
        history = [_event(i, 100.0) for i in range(1, 8)]
        out = await model.predict("tank_1", history, [])
        assert out.gallons_per_day == pytest.approx(100.0)
        assert out.features_used["events_in_window"] == 7
        assert out.features_used["gallons_in_window"] == pytest.approx(700.0)
        assert 0.0 <= out.confidence <= 1.0

    async def test_excludes_events_older_than_window(
        self, model: RetailStationModel
    ) -> None:
        # 100g in window (day 3), 500g outside (day 30).
        history = [_event(3, 100.0), _event(30, 500.0)]
        out = await model.predict("tank_1", history, [])
        assert out.gallons_per_day == pytest.approx(100.0 / 7.0)
        assert out.features_used["events_in_window"] == 1

    async def test_single_event_flagged_with_capped_confidence(
        self, model: RetailStationModel
    ) -> None:
        history = [_event(2, 50.0)]
        out = await model.predict("tank_1", history, [])
        assert "single_event" in out.anomaly_flags
        assert out.confidence <= 0.3

    async def test_ignores_invalid_quantities(
        self, model: RetailStationModel
    ) -> None:
        history = [
            _event(1, 100.0),
            {"timestamp": FIXED_NOW.isoformat(), "quantity_gallons": -5.0},
            {"timestamp": FIXED_NOW.isoformat(), "quantity_gallons": float("inf")},
            {"timestamp": "not-a-timestamp", "quantity_gallons": 50.0},
        ]
        out = await model.predict("tank_1", history, [])
        assert out.features_used["events_in_window"] == 1
        assert out.gallons_per_day == pytest.approx(100.0 / 7.0)


# ---------------------------------------------------------------------------
# PropaneKFactorModel
# ---------------------------------------------------------------------------


class TestPropaneKFactorModel:
    @pytest.fixture
    def model(self) -> PropaneKFactorModel:
        return PropaneKFactorModel(
            base_load_gpd=0.0, clock=_fixed_clock()
        )

    def _make_propane_history(self, *, gallons_per_delivery: float = 100.0) -> List[Dict[str, Any]]:
        """Four deliveries spaced 10 days apart (3 intervals)."""

        return [
            _event(40, 0.0),  # initial fill (seed; deliveries 2-4 count)
            _event(30, gallons_per_delivery),
            _event(20, gallons_per_delivery),
            _event(10, gallons_per_delivery),
        ]

    def _make_constant_hdd_weather(
        self, *, hdd_value: float, historical_days: int = 45, forward_days: int = 7
    ) -> List[DailyWeather]:
        """Return a flat HDD record covering history + forward horizon."""

        # pick avg_temp such that compute_hdd yields the target value.
        avg_temp = 65.0 - hdd_value
        rows: List[DailyWeather] = []
        for offset in range(-historical_days, forward_days + 1):
            rows.append(_weather_row(offset, avg_temp))
        return rows

    async def test_insufficient_history_uses_residential_default(
        self, model: PropaneKFactorModel
    ) -> None:
        # Only one delivery → cannot build intervals → fall back.
        history = [_event(10, 100.0)]
        weather = self._make_constant_hdd_weather(hdd_value=10.0)
        out = await model.predict("tank_1", history, weather, customer_type="residential")
        assert "insufficient_history" in out.anomaly_flags
        assert out.confidence == DEFAULT_FALLBACK_CONFIDENCE
        assert out.features_used["k_factor"] == pytest.approx(DEFAULT_RESIDENTIAL_K_FACTOR, rel=1e-3)
        # gpd = default_K * 10 HDD = 7.0 (commercial would be 9.0)
        assert out.gallons_per_day == pytest.approx(
            DEFAULT_RESIDENTIAL_K_FACTOR * 10.0
        )

    async def test_insufficient_history_uses_commercial_default(
        self, model: PropaneKFactorModel
    ) -> None:
        history = [_event(10, 100.0)]
        weather = self._make_constant_hdd_weather(hdd_value=10.0)
        out = await model.predict(
            "tank_1", history, weather, customer_type="commercial"
        )
        assert out.features_used["k_factor"] == pytest.approx(
            DEFAULT_COMMERCIAL_K_FACTOR, rel=1e-3
        )

    async def test_learned_k_factor_with_sufficient_history(
        self, model: PropaneKFactorModel
    ) -> None:
        # 100 gallons / 10 days / 10 HDD/day = K of 1.0 exactly.
        history = self._make_propane_history(gallons_per_delivery=100.0)
        weather = self._make_constant_hdd_weather(hdd_value=10.0)
        out = await model.predict("tank_1", history, weather)
        assert "insufficient_history" not in out.anomaly_flags
        assert out.features_used["k_factor_source"] == "learned"
        assert out.features_used["k_factor"] == pytest.approx(1.0, rel=1e-3)
        # Prediction = K * avg_forward_HDD = 1.0 * 10.0 = 10.0
        assert out.gallons_per_day == pytest.approx(10.0, rel=1e-3)
        assert out.features_used["intervals_used"] == 3

    async def test_weather_fallback_annotates_and_uses_base_load(self) -> None:
        model = PropaneKFactorModel(base_load_gpd=0.5, clock=_fixed_clock())
        history = self._make_propane_history(gallons_per_delivery=100.0)
        out = await model.predict("tank_1", history, [])
        assert "weather_fallback" in out.anomaly_flags
        assert out.gallons_per_day == pytest.approx(0.5)
        assert out.confidence == DEFAULT_FALLBACK_CONFIDENCE

    async def test_zero_hdd_intervals_skipped(
        self, model: PropaneKFactorModel
    ) -> None:
        # All summer: HDD == 0, so no interval contributes to K. History
        # still has >=3 events but the K-learning produces no intervals,
        # so the model falls back to the customer-type default K. The
        # weather record itself is well-formed (just warm) so no
        # ``weather_fallback`` flag is set — the prediction evaluates to
        # 0.0 gpd naturally because K × 0 HDD + 0 base_load = 0.
        history = self._make_propane_history(gallons_per_delivery=100.0)
        weather = self._make_constant_hdd_weather(hdd_value=0.0)
        out = await model.predict("tank_1", history, weather, customer_type="residential")
        assert "insufficient_history" in out.anomaly_flags
        # Zero-HDD is a valid summer signal, not a fallback.
        assert "weather_fallback" not in out.anomaly_flags
        # Base load = 0 → result is 0.0.
        assert out.gallons_per_day == 0.0
        assert out.features_used["k_factor_source"] == "default"

    async def test_gallons_per_day_always_non_negative(
        self, model: PropaneKFactorModel
    ) -> None:
        history = self._make_propane_history(gallons_per_delivery=1.0)
        weather = self._make_constant_hdd_weather(hdd_value=5.0)
        out = await model.predict("tank_1", history, weather)
        assert out.gallons_per_day >= 0.0
        assert 0.0 <= out.confidence <= 1.0

    def test_constructor_rejects_negative_base_load(self) -> None:
        with pytest.raises(ValueError):
            PropaneKFactorModel(base_load_gpd=-1.0)


# ---------------------------------------------------------------------------
# HeatingOilHDDRegressionModel
# ---------------------------------------------------------------------------


class TestHeatingOilHDDRegressionModel:
    @pytest.fixture
    def model(self) -> HeatingOilHDDRegressionModel:
        return HeatingOilHDDRegressionModel(clock=_fixed_clock())

    async def test_insufficient_samples_uses_default_k(
        self, model: HeatingOilHDDRegressionModel
    ) -> None:
        # Only one delivery → zero intervals → zero samples.
        history = [_event(10, 100.0)]
        weather = [_weather_row(i, 40.0) for i in range(-30, 8)]
        out = await model.predict("tank_1", history, weather)
        assert "insufficient_history" in out.anomaly_flags
        assert out.features_used["fit_source"] == "default_k"
        assert out.confidence == DEFAULT_FALLBACK_CONFIDENCE
        assert out.gallons_per_day >= 0.0

    async def test_perfect_linear_fit_high_r_squared(self) -> None:
        """Deliveries scale cleanly with HDD → OLS finds a near-perfect fit."""

        model = HeatingOilHDDRegressionModel(clock=_fixed_clock())
        # 3 intervals of 10 days each. HDDs and gallons chosen so the
        # relationship is exactly gpd = 2*HDD + 5 → OLS should recover it.
        # interval HDDs (10 days each): avg = 5, 10, 15
        # gpd = 2*avg + 5 → 15, 25, 35  → delivered = gpd*10 = 150, 250, 350
        history: List[Dict[str, Any]] = []
        history.append(_event(40, 0.0))
        history.append(_event(30, 150.0))
        history.append(_event(20, 250.0))
        history.append(_event(10, 350.0))

        # Weather: build a record where interval avg_HDDs are 5, 10, 15.
        weather: List[DailyWeather] = []
        # Days -40..-30 (interval 1): HDD=5
        # Days -30..-20 (interval 2): HDD=10
        # Days -20..-10 (interval 3): HDD=15
        # Days -10..+7 (forecast):    HDD=15 (use same slope for prediction)
        for day_offset in range(-40, -30):
            weather.append(_weather_row(day_offset, 60.0))  # hdd=5
        for day_offset in range(-30, -20):
            weather.append(_weather_row(day_offset, 55.0))  # hdd=10
        for day_offset in range(-20, -10):
            weather.append(_weather_row(day_offset, 50.0))  # hdd=15
        for day_offset in range(-10, 8):
            weather.append(_weather_row(day_offset, 50.0))  # hdd=15

        out = await model.predict("tank_1", history, weather)
        assert out.features_used["fit_source"] == "ols"
        assert out.features_used["r_squared"] >= 0.99
        assert out.features_used["slope"] == pytest.approx(2.0, rel=1e-2)
        assert out.features_used["intercept"] == pytest.approx(5.0, rel=1e-2)
        # gpd = 2 * 15 + 5 = 35.
        assert out.gallons_per_day == pytest.approx(35.0, rel=1e-2)
        assert out.confidence >= 0.95 - 1e-9

    async def test_low_r_squared_falls_back_to_slope_only(self) -> None:
        model = HeatingOilHDDRegressionModel(clock=_fixed_clock())
        # Build history with deliveries that do NOT scale with HDD — the
        # OLS fit will produce a poor R², driving the slope-only fallback.
        history: List[Dict[str, Any]] = []
        history.append(_event(40, 0.0))
        history.append(_event(30, 300.0))  # large
        history.append(_event(20, 50.0))   # small
        history.append(_event(10, 500.0))  # huge

        weather: List[DailyWeather] = []
        # All three intervals have the same HDD average, so variance in
        # x is zero → OLS returns slope=0, intercept=mean(y), R²=0.
        for day_offset in range(-40, 8):
            weather.append(_weather_row(day_offset, 55.0))  # hdd=10

        out = await model.predict("tank_1", history, weather)
        assert "regression_poor_fit" in out.anomaly_flags
        assert out.features_used["fit_source"] == "slope_only"
        assert out.confidence == DEFAULT_FALLBACK_CONFIDENCE

    async def test_no_weather_triggers_weather_fallback(self) -> None:
        model = HeatingOilHDDRegressionModel(clock=_fixed_clock())
        history = [
            _event(40, 0.0),
            _event(30, 150.0),
            _event(20, 250.0),
            _event(10, 350.0),
        ]
        out = await model.predict("tank_1", history, [])
        assert "weather_fallback" in out.anomaly_flags
        assert out.gallons_per_day >= 0.0
        assert 0.0 <= out.confidence <= 1.0

    def test_constructor_rejects_invalid_r_squared_threshold(self) -> None:
        with pytest.raises(ValueError):
            HeatingOilHDDRegressionModel(r_squared_threshold=1.5)

    def test_constructor_rejects_negative_base_load(self) -> None:
        with pytest.raises(ValueError):
            HeatingOilHDDRegressionModel(base_load_gpd=-1.0)


# ---------------------------------------------------------------------------
# DieselRollingModel
# ---------------------------------------------------------------------------


class TestDieselRollingModel:
    @pytest.fixture
    def model(self) -> DieselRollingModel:
        return DieselRollingModel(clock=_fixed_clock())

    async def test_no_history_returns_zero(self, model: DieselRollingModel) -> None:
        out = await model.predict("tank_1", [], [])
        assert out.gallons_per_day == 0.0
        assert "no_history" in out.anomaly_flags
        assert out.confidence > 0.0

    async def test_rolling_average_over_28_days(
        self, model: DieselRollingModel
    ) -> None:
        # 28 days of 30 gallons/day → baseline 30.
        history = [_event(i, 30.0) for i in range(1, 29)]
        out = await model.predict("tank_1", history, [])
        assert out.features_used["days_observed"] == 28
        # single_weekday flag absent because events span all weekdays.
        assert "single_weekday" not in out.anomaly_flags
        assert out.gallons_per_day > 0.0
        # baseline 30 × season factor (within [0.5, 1.5]) → in [15, 45].
        assert 15.0 <= out.gallons_per_day <= 45.0

    async def test_seasonality_factor_is_clamped(self) -> None:
        """A sparsely-observed weekday must not produce a >2× prediction."""

        model = DieselRollingModel(clock=_fixed_clock())
        # One huge event on a Monday; smaller events on other weekdays.
        history: List[Dict[str, Any]] = []
        for i in range(1, 15):
            history.append(_event(i, 10.0))
        # Add a huge Friday outlier.
        # Find offset that lands on a Friday.
        for offset in range(1, 29):
            candidate = FIXED_NOW - timedelta(days=offset)
            if candidate.weekday() == 4:  # Friday
                history.append(_event(offset, 500.0))
                break
        out = await model.predict("tank_1", history, [])
        assert (
            DieselRollingModel.min_season_factor
            <= out.features_used["season_factor"]
            <= DieselRollingModel.max_season_factor
        )

    async def test_single_weekday_flagged(self) -> None:
        model = DieselRollingModel(clock=_fixed_clock())
        # All events on the same weekday (7 days apart).
        history = [_event(i, 50.0) for i in (7, 14, 21)]
        out = await model.predict("tank_1", history, [])
        assert "single_weekday" in out.anomaly_flags
        # Factor forced to 1.0.
        assert out.features_used["season_factor"] == pytest.approx(1.0)

    async def test_output_always_non_negative(self) -> None:
        model = DieselRollingModel(clock=_fixed_clock())
        history = [_event(i, 0.0) for i in range(1, 10)]
        out = await model.predict("tank_1", history, [])
        assert out.gallons_per_day >= 0.0

    def test_constructor_rejects_invalid_window(self) -> None:
        with pytest.raises(ValueError):
            DieselRollingModel(window_days=0)


# ---------------------------------------------------------------------------
# GeneratorRuntimeModel
# ---------------------------------------------------------------------------


class TestGeneratorRuntimeModel:
    async def test_all_defaults_low_confidence(self) -> None:
        model = GeneratorRuntimeModel(clock=_fixed_clock())
        out = await model.predict("tank_1", [], [])
        # default 2 gph × 1 hr = 2 gpd.
        assert out.gallons_per_day == pytest.approx(2.0)
        assert "default_gallons_per_hour" in out.anomaly_flags
        assert "default_runtime_hours" in out.anomaly_flags
        assert out.confidence == DEFAULT_FALLBACK_CONFIDENCE

    async def test_profile_values_used_when_no_history(self) -> None:
        model = GeneratorRuntimeModel(
            profile_gallons_per_hour=3.0,
            profile_runtime_hours_per_day=2.0,
            clock=_fixed_clock(),
        )
        out = await model.predict("tank_1", [], [])
        assert out.gallons_per_day == pytest.approx(6.0)
        assert out.features_used["gallons_per_hour_source"] == "profile"
        assert out.features_used["projected_runtime_source"] == "profile"
        assert "default_gallons_per_hour" not in out.anomaly_flags
        assert "default_runtime_hours" not in out.anomaly_flags

    async def test_history_overrides_profile(self) -> None:
        model = GeneratorRuntimeModel(
            profile_gallons_per_hour=3.0,
            profile_runtime_hours_per_day=2.0,
            clock=_fixed_clock(),
        )
        # Two runtime events: average 4 hours/day.
        history = [
            _event(5, 0.0, extra={"runtime_hours": 3.0, "gallons_per_hour": 2.5}),
            _event(1, 0.0, extra={"runtime_hours": 5.0}),
        ]
        out = await model.predict("tank_1", history, [])
        # Most recent gph from history is 2.5 (last event has no gph so
        # we walk back to the prior one).
        assert out.features_used["gallons_per_hour_source"] == "history"
        assert out.features_used["projected_runtime_source"] == "history"
        assert out.features_used["runtime_events"] == 2
        # 2.5 × 4.0 = 10.0
        assert out.gallons_per_day == pytest.approx(10.0, rel=1e-6)
        assert out.confidence > DEFAULT_FALLBACK_CONFIDENCE

    async def test_ignores_negative_runtime(self) -> None:
        model = GeneratorRuntimeModel(clock=_fixed_clock())
        history = [_event(1, 0.0, extra={"runtime_hours": -2.0})]
        out = await model.predict("tank_1", history, [])
        # Falls back to defaults since negative runtime is rejected.
        assert out.features_used["projected_runtime_source"] == "default"

    def test_constructor_rejects_negative_profiles(self) -> None:
        with pytest.raises(ValueError):
            GeneratorRuntimeModel(profile_gallons_per_hour=-1.0)
        with pytest.raises(ValueError):
            GeneratorRuntimeModel(profile_runtime_hours_per_day=-1.0)
        with pytest.raises(ValueError):
            GeneratorRuntimeModel(lookback_events=0)


# ---------------------------------------------------------------------------
# Factory / registry
# ---------------------------------------------------------------------------


class TestFactory:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("retail_station_rolling_7d", RetailStationModel),
            ("retail", RetailStationModel),
            ("propane_k_factor", PropaneKFactorModel),
            ("PROPANE", PropaneKFactorModel),  # case-insensitive
            ("heating_oil_hdd_regression", HeatingOilHDDRegressionModel),
            ("heating_oil", HeatingOilHDDRegressionModel),
            ("diesel_rolling_28d", DieselRollingModel),
            ("diesel", DieselRollingModel),
            ("generator_runtime", GeneratorRuntimeModel),
            ("generator_fuel", GeneratorRuntimeModel),
            ("generator", GeneratorRuntimeModel),
        ],
    )
    def test_build_resolves_by_name(self, name: str, expected: type) -> None:
        out = build_consumption_model(name)
        assert isinstance(out, expected)

    def test_build_rejects_unknown_name(self) -> None:
        with pytest.raises(ValueError):
            build_consumption_model("no_such_model")

    def test_build_rejects_blank_name(self) -> None:
        with pytest.raises(ValueError):
            build_consumption_model("   ")

    def test_registry_covers_every_concrete_model(self) -> None:
        # Every concrete model defined in the module should be reachable
        # through at least one registry entry.
        assert {
            RetailStationModel,
            PropaneKFactorModel,
            HeatingOilHDDRegressionModel,
            DieselRollingModel,
            GeneratorRuntimeModel,
        } <= set(CONSUMPTION_MODEL_REGISTRY.values())

    @pytest.mark.parametrize(
        "fuel_type,expected",
        [
            ("propane", PropaneKFactorModel),
            ("heating_oil", HeatingOilHDDRegressionModel),
            ("diesel", DieselRollingModel),
            ("farm_fuel", DieselRollingModel),
            ("gasoline", RetailStationModel),
            ("generator_fuel", GeneratorRuntimeModel),
        ],
    )
    def test_select_consumption_model_for_tank_by_fuel_type(
        self, fuel_type: str, expected: type
    ) -> None:
        tank = _customer_tank(fuel_type=fuel_type)
        model = select_consumption_model_for_tank(tank)
        assert isinstance(model, expected)
