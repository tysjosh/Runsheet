"""Single place that decides which LLM the agents talk to, and how it authenticates.

Before this module the model id ``"gemini/gemini-2.5-flash"`` was hardcoded in
three places (``bootstrap/agents.py``, ``Agents/mainagent.py``,
``Agents/orchestrator.py``) and each one passed
``os.environ.get("GEMINI_API_KEY", "")`` — an **empty string** when the variable
is absent. Nothing failed at startup; every agent call failed on authentication
instead, one request at a time.

The prefix is not cosmetic. LiteLLM routes on it::

    litellm.get_llm_provider("gemini/gemini-2.5-flash")    -> provider "gemini"
    litellm.get_llm_provider("vertex_ai/gemini-2.5-flash") -> provider "vertex_ai"

``gemini`` is Google AI Studio and authenticates with an API key. ``vertex_ai``
is Google Cloud and authenticates with Application Default Credentials. Setting
``GOOGLE_CLOUD_PROJECT`` therefore does nothing for a ``gemini/`` model id — it
was possible to have a fully populated GCP configuration and still get 401s,
which is exactly the state the production env file was in.

So the provider is now explicit (``AGENT_LLM_PROVIDER``), the model name is
configuration rather than a literal (``AGENT_LLM_MODEL``), and
:mod:`config.settings` refuses to start staging/production when the selected
provider has no credential.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Providers this application knows how to authenticate. Kept deliberately
#: short: each entry needs a credential story that is checkable at startup.
GEMINI_PROVIDER = "gemini"
VERTEX_PROVIDER = "vertex_ai"
SUPPORTED_PROVIDERS = (GEMINI_PROVIDER, VERTEX_PROVIDER)


class AgentModelUnconfigured(RuntimeError):
    """Raised when no usable LLM credential is configured.

    Raised rather than returning a model with an empty key, because an empty
    key produces an authentication error per request with nothing pointing at
    the cause.
    """


def resolve_agent_model_spec(settings=None) -> Tuple[str, Dict[str, Any]]:
    """Return ``(model_id, client_args)`` for the configured provider.

    Args:
        settings: Optional settings object. Loaded via ``get_settings()`` when
            omitted so callers that already hold one avoid a second lookup.

    Returns:
        ``model_id`` carries the LiteLLM provider prefix; ``client_args`` is
        ready to hand to ``LiteLLMModel(client_args=...)`` or to splat into
        ``litellm.acompletion``.

    Raises:
        AgentModelUnconfigured: when the selected provider has no credential.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()

    provider = (getattr(settings, "agent_llm_provider", GEMINI_PROVIDER) or "").strip().lower()
    model = (getattr(settings, "agent_llm_model", "") or "").strip()
    if not model:
        raise AgentModelUnconfigured("agent_llm_model is empty")

    if provider == GEMINI_PROVIDER:
        api_key = (getattr(settings, "gemini_api_key", "") or "").strip()
        if not api_key:
            raise AgentModelUnconfigured(
                "AGENT_LLM_PROVIDER=gemini requires GEMINI_API_KEY. "
                "GOOGLE_CLOUD_PROJECT does not substitute for it: a 'gemini/' "
                "model id routes to Google AI Studio, which authenticates by "
                "API key. To use Google Cloud instead, set "
                "AGENT_LLM_PROVIDER=vertex_ai."
            )
        return f"{GEMINI_PROVIDER}/{model}", {"api_key": api_key}

    if provider == VERTEX_PROVIDER:
        project = (getattr(settings, "google_cloud_project", "") or "").strip()
        if not project:
            raise AgentModelUnconfigured(
                "AGENT_LLM_PROVIDER=vertex_ai requires GOOGLE_CLOUD_PROJECT."
            )
        location = (getattr(settings, "google_cloud_location", "") or "us-central1").strip()
        # Vertex authenticates with Application Default Credentials, which may
        # come from GOOGLE_APPLICATION_CREDENTIALS or from the platform (GCE /
        # Cloud Run / GKE metadata). Selecting this provider is the operator
        # asserting ADC resolves; there is no way to verify that from here
        # without making a billable call.
        return (
            f"{VERTEX_PROVIDER}/{model}",
            {"vertex_project": project, "vertex_location": location},
        )

    raise AgentModelUnconfigured(
        f"unknown AGENT_LLM_PROVIDER={provider!r}; expected one of "
        f"{', '.join(SUPPORTED_PROVIDERS)}"
    )


def build_agent_model(
    settings=None,
    *,
    max_tokens: int = 8000,
    temperature: float = 0.7,
):
    """Construct the Strands ``LiteLLMModel`` the agents run on.

    Raises:
        AgentModelUnconfigured: propagated from :func:`resolve_agent_model_spec`
            so a misconfigured deployment surfaces at wiring time.
    """
    from strands.models.litellm import LiteLLMModel

    model_id, client_args = resolve_agent_model_spec(settings)
    logger.info("Agent LLM resolved to %s", model_id)
    return LiteLLMModel(
        model_id=model_id,
        client_args=client_args,
        params={"max_tokens": max_tokens, "temperature": temperature},
    )


def try_resolve_agent_model_spec(
    settings=None,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Best-effort variant for optional call sites (returns ``(None, {})``).

    Used by the orchestrator's LLM intent classification, which already has a
    keyword-matching fallback: a missing credential should degrade routing, not
    raise into a user request.
    """
    try:
        return resolve_agent_model_spec(settings)
    except Exception as exc:  # noqa: BLE001 — optional path, never propagate
        logger.warning("Agent LLM unavailable for this call: %s", exc)
        return None, {}


__all__ = [
    "AgentModelUnconfigured",
    "GEMINI_PROVIDER",
    "SUPPORTED_PROVIDERS",
    "VERTEX_PROVIDER",
    "build_agent_model",
    "resolve_agent_model_spec",
    "try_resolve_agent_model_spec",
]
