"""An agent stack with no LLM credential must fail at startup, not per request.

Three call sites — ``bootstrap/agents.py``, ``Agents/mainagent.py`` and
``Agents/orchestrator.py`` — each hardcoded the model id ``gemini/gemini-2.5-flash``
and read ``os.environ.get("GEMINI_API_KEY", "")``. An unset variable therefore
became an **empty key** rather than an error: the model constructed, boot
succeeded, and every agent call failed on authentication one request at a time.

The trap was that the deployment looked configured. ``GOOGLE_CLOUD_PROJECT`` was
set in the production env file, which reads like a Gemini credential and is not
one — LiteLLM routes on the model-id prefix, and ``gemini/`` is Google AI Studio
(API key), not Vertex (ADC):

    litellm.get_llm_provider("gemini/gemini-2.5-flash")    -> "gemini"
    litellm.get_llm_provider("vertex_ai/gemini-2.5-flash") -> "vertex_ai"

So the provider is explicit now, and each provider is checked against the
setting it actually needs.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from Agents.model_provider import (
    AgentModelUnconfigured,
    resolve_agent_model_spec,
)


class _FakeSettings:
    """Minimal stand-in; the resolver only reads these four attributes."""

    def __init__(
        self,
        *,
        provider="gemini",
        model="gemini-2.5-flash",
        api_key="",
        project="",
        location="us-central1",
    ):
        self.agent_llm_provider = provider
        self.agent_llm_model = model
        self.gemini_api_key = api_key
        self.google_cloud_project = project
        self.google_cloud_location = location


class TestModelIdCarriesTheRightProviderPrefix:
    """The prefix decides which credential is used, so it cannot be guessed."""

    def test_gemini_provider_uses_the_api_key(self):
        model_id, kwargs = resolve_agent_model_spec(
            _FakeSettings(api_key="sk-test")
        )
        assert model_id == "gemini/gemini-2.5-flash"
        assert kwargs == {"api_key": "sk-test"}

    def test_vertex_provider_passes_project_and_location_not_an_api_key(self):
        model_id, kwargs = resolve_agent_model_spec(
            _FakeSettings(
                provider="vertex_ai", project="proj-1", location="europe-west4"
            )
        )
        assert model_id == "vertex_ai/gemini-2.5-flash"
        assert kwargs == {
            "vertex_project": "proj-1",
            "vertex_location": "europe-west4",
        }
        assert "api_key" not in kwargs, (
            "Vertex authenticates with ADC; sending an api_key hides which "
            "credential is actually in use"
        )

    def test_the_prefix_is_what_litellm_routes_on(self):
        """Guards the assumption the whole design rests on.

        If this ever stops holding, the credential checks below are checking
        the wrong thing.
        """
        litellm = pytest.importorskip("litellm")
        assert litellm.get_llm_provider("gemini/gemini-2.5-flash")[1] == "gemini"
        assert (
            litellm.get_llm_provider("vertex_ai/gemini-2.5-flash")[1]
            == "vertex_ai"
        )


class TestAnEmptyCredentialRaisesInsteadOfBuilding:
    def test_gemini_without_a_key_raises(self):
        with pytest.raises(AgentModelUnconfigured) as exc:
            resolve_agent_model_spec(_FakeSettings(api_key=""))
        assert "GEMINI_API_KEY" in str(exc.value)

    def test_a_gcp_project_does_not_satisfy_the_gemini_provider(self):
        """The exact production misconfiguration.

        GOOGLE_CLOUD_PROJECT was set and GEMINI_API_KEY was not, which looked
        configured and 401s on every call.
        """
        with pytest.raises(AgentModelUnconfigured):
            resolve_agent_model_spec(
                _FakeSettings(api_key="", project="runsheet-prod")
            )

    def test_vertex_without_a_project_raises(self):
        with pytest.raises(AgentModelUnconfigured):
            resolve_agent_model_spec(
                _FakeSettings(provider="vertex_ai", project="")
            )

    def test_an_unknown_provider_raises_rather_than_building_a_bad_prefix(self):
        with pytest.raises(AgentModelUnconfigured):
            resolve_agent_model_spec(_FakeSettings(provider="openai"))

    def test_the_optional_variant_degrades_instead_of_raising(self):
        """Intent classification has a keyword fallback; it must not raise."""
        from Agents.model_provider import try_resolve_agent_model_spec

        model_id, kwargs = try_resolve_agent_model_spec(
            _FakeSettings(api_key="")
        )
        assert model_id is None and kwargs == {}


class TestStartupRefusesAMissingCredential:
    """Settings-level guard: the failure belongs at boot, where it is visible."""

    @staticmethod
    def _env(**overrides):
        env = {
            "ENVIRONMENT": "production",
            "ELASTIC_ENDPOINT": "https://es.example.com",
            "ELASTIC_API_KEY": "es-key",
            "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/runsheet",
            "REDIS_URL": "redis://redis.internal:6379",
            "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
            "SUPERTOKENS_API_KEY": "st-key",
            "CORS_ORIGINS": '["https://app.example.com"]',
            "GOOGLE_CLOUD_PROJECT": "runsheet-prod",
        }
        env.update(overrides)
        return env

    def test_production_refuses_to_start_without_a_gemini_key(self):
        from config.settings import Settings

        with patch.dict(os.environ, self._env(), clear=True):
            with pytest.raises(Exception) as exc:
                Settings()
        message = str(exc.value).lower()
        assert "gemini_api_key" in message, message

    def test_production_starts_with_a_gemini_key(self):
        from config.settings import Settings

        with patch.dict(
            os.environ, self._env(GEMINI_API_KEY="sk-prod"), clear=True
        ):
            settings = Settings()
        assert settings.agent_llm_provider == "gemini"
        assert settings.gemini_api_key == "sk-prod"

    def test_production_starts_on_vertex_without_a_gemini_key(self):
        """Vertex is the documented way out, so it must actually be usable."""
        from config.settings import Settings

        with patch.dict(
            os.environ, self._env(AGENT_LLM_PROVIDER="vertex_ai"), clear=True
        ):
            settings = Settings()
        assert settings.agent_llm_provider == "vertex_ai"

    def test_development_still_starts_without_any_llm_credential(self):
        """A laptop must not need a paid credential to boot the app."""
        from config.settings import Settings

        env = self._env(ENVIRONMENT="development", CORS_ORIGINS='["http://localhost:3000"]')
        env.pop("DATABASE_URL")
        with patch.dict(os.environ, env, clear=True):
            settings = Settings()
        assert settings.gemini_api_key == ""

    def test_a_model_name_carrying_a_prefix_is_rejected(self):
        """``gemini/gemini-2.5-flash`` here would compose to a doubled prefix."""
        from config.settings import Settings

        with patch.dict(
            os.environ,
            self._env(
                GEMINI_API_KEY="sk-prod", AGENT_LLM_MODEL="gemini/gemini-2.5-flash"
            ),
            clear=True,
        ):
            with pytest.raises(Exception) as exc:
                Settings()
        assert "prefix" in str(exc.value).lower()
