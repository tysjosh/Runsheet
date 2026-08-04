"""
The one piece of deployment configuration a client needs *before* it can sign in.

``GET /api/auth/public-config`` returns the Runsheet **web app origin** — the
value the backend already holds as ``settings.supertokens_website_domain`` and
feeds to ``InputAppInfo(website_domain=...)`` in ``auth/supertokens_init.py``.
That makes the backend the authoritative source of truth for the origin, because
it is the origin SuperTokens mints password-reset links against. A client that
carries its own copy of the same origin can drift from it, and a driver sent to
the wrong host lands on a page that cannot service the reset token.

Before this endpoint the driver app carried that copy
(``EXPO_PUBLIC_WEB_BASE_URL``). It now reads the origin from here instead, which
removes one of the three independent copies of a single value. The web app's
build-time ``NEXT_PUBLIC_ST_WEBSITE_DOMAIN`` deliberately stays as it is: it is
consumed by ``SuperTokens.init()`` at module load in
``runsheet/src/config/supertokens.ts``, which cannot await a fetch, and it is
co-deployed with the backend so it is far less likely to drift.

Why this route is on the Public_Route_Allowlist
-----------------------------------------------
It serves the sign-in screen, so by construction it must answer callers who have
no session yet. That is only acceptable because of what it returns:

* The website domain is **not a secret** — it is the origin in the user's
  address bar, and it is already embedded in every reset email and in the web
  app's published bundle. Disclosing it to an unauthenticated caller reveals
  nothing they could not read off the browser chrome.
* The response is a fixed, tenant-independent constant. The handler takes no
  request parameters, reads no header, touches no datastore, and derives nothing
  from the caller, so it is byte-identical for everyone. It cannot leak tenant
  data or serve as an oracle for anything about the requester.
* It carries **exactly one field**. Nothing about the SuperTokens core
  connection (``supertokens_connection_uri``, ``supertokens_api_key``), the
  database, or SMTP is exposed, and
  ``tests/unit/test_public_config_endpoint.py`` pins the exact response key set
  so this cannot quietly grow into a config-disclosure surface. If you are here
  to add a field: the endpoint is world-readable, so every field you add is
  published to every unauthenticated caller. Read that test first.

Deliberately excluded: ``api_domain`` and ``api_base_path``. A caller must
already know the API origin to have reached this endpoint at all — the driver app
has it as ``EXPO_PUBLIC_API_BASE_URL`` — so returning them would widen the
surface without conveying anything the caller does not already hold.

Blank-setting behaviour
-----------------------
A blank/whitespace ``supertokens_website_domain`` returns ``website_domain:
null`` with **HTTP 200**, not a 5xx. Two reasons: the client contract for an
unknown origin is already "render no affordance", which ``null`` expresses
directly; and a public endpoint whose status code varies with deployment
configuration hands an unauthenticated caller a probe for distinguishing
deployment states. A constant 200 tells them nothing.

Validates: SuperTokens Auth Migration Req 6.3, 10.1 (single source of truth for
the app_info website origin).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth-public-config"])


class PublicConfigResponse(BaseModel):
    """The unauthenticated client bootstrap payload.

    Exactly one field, by design. See the module docstring for why nothing else
    belongs here; ``tests/unit/test_public_config_endpoint.py`` asserts the key
    set is precisely ``{"website_domain"}``.
    """

    model_config = ConfigDict(extra="forbid")

    website_domain: Optional[str] = Field(
        default=None,
        description=(
            "The Runsheet web app origin (e.g. 'https://app.runsheet.example'), "
            "normalized without a trailing slash. Null when the deployment has "
            "not configured one, in which case a client renders no link out to "
            "the web app."
        ),
    )


@router.get("/public-config", response_model=PublicConfigResponse)
async def get_public_config() -> PublicConfigResponse:
    """Return the web app origin. Unauthenticated, constant for every caller.

    Takes no arguments on purpose: nothing in the response is request-derived,
    so every caller receives the same bytes. The single value is read from a
    **named** settings attribute — there is no path through this function that
    reflects a setting it does not name.
    """
    configured = get_settings().supertokens_website_domain or ""
    # Normalized the way clients normalize an origin (trimmed, no trailing
    # slash) so the value can be concatenated with a path as-is.
    origin = configured.strip().rstrip("/")
    return PublicConfigResponse(website_domain=origin or None)


__all__ = ["router", "PublicConfigResponse"]
