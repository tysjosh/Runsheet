"""
Allowed browser origins, parsed from the ``CORS_ORIGINS`` environment variable.

One parser shared by the REST ``CORSMiddleware`` (``main.py``) and the
WebSocket handshake Origin check (``bootstrap/websockets.py``, staging finding
F2), so both surfaces trust exactly the same origins.

``CORS_ORIGINS`` is a JSON list, e.g. ``["https://app.example.com"]``. Unset
means :data:`DEFAULT_CORS_ORIGINS`. Invalid JSON, or JSON that is not a list,
logs a warning and falls back to the default.

This deliberately does not use ``Settings.cors_origins``: that field has a
different default (``["http://localhost:3000"]``), and ``main.py`` loads
``.env.<environment>`` into ``os.environ`` at import, so the environment is the
source the middleware has always read.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: Origins trusted when ``CORS_ORIGINS`` is unset or unparseable.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def parse_cors_origins(raw: Optional[str]) -> list[str]:
    """Parse a ``CORS_ORIGINS`` value into a list of origins."""
    if raw is None:
        return list(DEFAULT_CORS_ORIGINS)
    try:
        origins = json.loads(raw)
        if not isinstance(origins, list):
            raise ValueError(f"expected a JSON list, got {type(origins).__name__}")
    except Exception as exc:  # noqa: BLE001 — any bad value falls back
        logger.warning("Failed to parse CORS_ORIGINS: %s, using defaults", exc)
        return list(DEFAULT_CORS_ORIGINS)
    return origins


def get_cors_origins() -> list[str]:
    """Return the allowed origins from ``CORS_ORIGINS``, read at call time."""
    return parse_cors_origins(os.environ.get("CORS_ORIGINS"))


__all__ = ["DEFAULT_CORS_ORIGINS", "get_cors_origins", "parse_cors_origins"]
