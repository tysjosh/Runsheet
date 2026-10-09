"""Finished provider adapters must actually be constructed at boot.

Two real integrations shipped complete and were never wired, so the agents ran
on no external data at all while looking healthy:

* ``NOAAWeatherProvider`` / ``OpenWeatherProvider`` — nothing called
  ``build_weather_provider`` outside its own module and nothing called
  ``TankForecastingAgent.set_weather_provider``, so ``_weather_provider`` was
  always ``None``. Degree-days are the dominant term in the propane and
  heating-oil consumption models, so every one of those forecasts ran without
  its main input and said so only via ``weather_fallback: true``.
* ``OPISRackPriceProvider`` — bootstrap hardcoded the CSV fallback with a loader
  that raised ``FileNotFoundError`` unconditionally, so terminal sourcing had no
  price data for any tenant.

These tests pin the *selection* logic, which is where both regressions lived.
Whether a given credential is valid is not checkable here; that it is read and
routed to the right adapter is.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from fuel.services.weather_provider import (
    NOAA_TOKEN_ENV,
    OPENWEATHER_KEY_ENV,
    NOAAWeatherProvider,
    OpenWeatherProvider,
    build_weather_provider,
)
from integrations.rack_price_provider_base import (
    OPIS_API_KEY_ENV,
    CSVFallbackRackPriceProvider,
    OPISRackPriceProvider,
    build_rack_price_provider,
)


def _select_weather_provider_name(env: dict) -> str:
    """Mirror of the bootstrap selection rule (see bootstrap/agents.py).

    Kept as a small pure function so the rule is testable without standing up
    the whole agent bootstrap, which needs ES, Redis and a scheduler.
    """
    name = (env.get("FUEL_OPS_WEATHER_PROVIDER") or "").strip().lower()
    if name:
        return name
    if env.get(OPENWEATHER_KEY_ENV):
        return "openweather"
    if env.get(NOAA_TOKEN_ENV):
        return "noaa"
    return ""


def _select_rack_provider_name(env: dict) -> str:
    name = (env.get("FUEL_OPS_RACK_PRICE_PROVIDER") or "").strip().lower()
    if name:
        return name
    return "opis" if env.get(OPIS_API_KEY_ENV) else "csv_fallback"


class TestWeatherProviderSelection:
    def test_an_openweather_key_selects_openweather(self):
        assert (
            _select_weather_provider_name({OPENWEATHER_KEY_ENV: "k"})
            == "openweather"
        )

    def test_a_noaa_token_selects_noaa(self):
        assert _select_weather_provider_name({NOAA_TOKEN_ENV: "t"}) == "noaa"

    def test_an_explicit_name_wins_over_autodetection(self):
        assert (
            _select_weather_provider_name(
                {"FUEL_OPS_WEATHER_PROVIDER": "noaa", OPENWEATHER_KEY_ENV: "k"}
            )
            == "noaa"
        )

    def test_no_credential_selects_nothing(self):
        """Building an adapter with no token would be worse than not building one.

        Both adapters return ``[]`` with a warning when their credential is
        absent, so wiring one unconditionally trades a visible "not registered"
        for a provider that silently fails on every call.
        """
        assert _select_weather_provider_name({}) == ""

    @pytest.mark.parametrize(
        "name,expected",
        [("noaa", NOAAWeatherProvider), ("openweather", OpenWeatherProvider)],
    )
    def test_each_selected_name_builds_its_adapter(self, name, expected):
        provider = build_weather_provider(
            name, es_service=MagicMock(), redis_client=MagicMock()
        )
        assert isinstance(provider, expected)

    def test_the_provider_persists_to_the_weather_observations_index(self):
        """Bootstrap passes ``es_service`` for a reason worth pinning.

        Persisted observations are what give the compliance K-factor service
        real degree-days instead of its empty-index fallback.
        """
        provider = build_weather_provider("noaa", es_service=MagicMock())
        assert provider.index_name == "weather_observations"


class TestTankForecastingAgentReceivesTheProvider:
    def test_the_agent_accepts_a_weather_provider_at_construction(self):
        """The regression was a constructor argument nobody passed."""
        import inspect

        from Agents.overlay.tank_forecasting_agent import TankForecastingAgent

        params = inspect.signature(TankForecastingAgent.__init__).parameters
        assert "weather_provider" in params

    def test_bootstrap_passes_weather_provider_to_the_agent(self):
        """Cheap structural check on the wiring line itself.

        Importing bootstrap/agents.py needs ES, Redis and a live scheduler, so
        this asserts on the source rather than executing it. It fails if the
        keyword is dropped again, which is precisely how this broke.
        """
        import pathlib

        source = pathlib.Path("bootstrap/agents.py").read_text()
        assert "weather_provider=weather_provider" in source, (
            "TankForecastingAgent is no longer receiving the weather provider; "
            "propane and heating-oil forecasts are running weather-blind again"
        )


class TestRackPriceProviderSelection:
    def test_an_opis_key_selects_opis(self):
        assert _select_rack_provider_name({OPIS_API_KEY_ENV: "k"}) == "opis"

    def test_no_opis_key_falls_back_to_csv(self):
        assert _select_rack_provider_name({}) == "csv_fallback"

    def test_an_explicit_name_wins_over_autodetection(self):
        assert (
            _select_rack_provider_name(
                {"FUEL_OPS_RACK_PRICE_PROVIDER": "csv_fallback", OPIS_API_KEY_ENV: "k"}
            )
            == "csv_fallback"
        )

    def test_opis_builds_without_a_csv_loader(self):
        """OPIS takes no loader; passing one would raise.

        The old code could only ever build the CSV adapter, so this path was
        never exercised.
        """
        with patch.dict(os.environ, {OPIS_API_KEY_ENV: "k"}, clear=False):
            provider = build_rack_price_provider("opis", redis_client=MagicMock())
        assert isinstance(provider, OPISRackPriceProvider)

    def test_csv_fallback_still_requires_a_loader(self):
        provider = build_rack_price_provider(
            "csv_fallback",
            csv_loader=lambda tenant_id: b"",
            redis_client=MagicMock(),
        )
        assert isinstance(provider, CSVFallbackRackPriceProvider)

    def test_bootstrap_no_longer_hardcodes_the_csv_adapter(self):
        """The bug was an unconditional adapter choice, not a bad adapter."""
        import pathlib

        source = pathlib.Path("bootstrap/agents.py").read_text()
        assert "build_rack_price_provider(" in source
        assert "sourcing_rack_provider = CSVFallbackRackPriceProvider(" not in source, (
            "rack-price provider is hardcoded to the CSV fallback again; OPIS "
            "can never be selected and terminal sourcing has no price data"
        )
