"""
Keep credentials out of log output.

Two defences, both installed by ``TelemetryService._setup_logging``:

* :func:`apply_library_log_levels` quiets the HTTP client and LiteLLM loggers.
  httpx logs every request URL at INFO (``HTTP Request: POST <url>``), and the
  Gemini AI Studio path carries the API key as ``?key=...`` in that URL.
  LiteLLM also attaches its own StreamHandler to its loggers, at a level taken
  from ``LITELLM_LOG`` (DEBUG by default in the pinned 1.55.0), so lowering the
  root logger alone would not reach it.

* :func:`install_log_redaction` wraps the log-record factory so every record is
  scrubbed when it is created: the message template, each argument, the
  rendered traceback and the stack info. Doing it in the factory rather than in
  a handler filter covers every handler, including LiteLLM's own and handlers
  added after setup. ``JSONFormatter`` runs :func:`redact_secrets` over its
  final line as a second pass, which catches ``extra_data`` payloads (``extra``
  is applied after the factory runs).

Redaction keeps the name and replaces only the value with ``***REDACTED***``.
It never inserts or removes quotes or backslashes, so a redacted JSON line is
still valid JSON.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"

# Loggers whose INFO/DEBUG output can carry request URLs, headers or payloads.
LIBRARY_LOGGERS = ("httpx", "httpcore", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy")
# LiteLLM attaches handlers directly to these, so their handler levels are capped too.
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")

# Cheap pre-check: text containing none of these cannot match any pattern below.
_TRIGGERS = (
    "key", "token", "secret", "password", "bearer", "basic", "authorization",
    "aiza", "eyj", "appid", "amz-",
)

# Query/form parameters, matched only after ``?`` or ``&`` so prose such as
# ``cache get failed for key=abc`` is left alone. The value stops at a
# backslash so a JSON escape (``\"``, ``\n``) is never half-consumed.
_QUERY_PARAM_RE = re.compile(
    r"([?&](?:key|api_key|apikey|api-key|access_token|token|password|secret"
    r"|client_secret|appid|x-amz-signature|x-amz-security-token"
    r"|x-amz-credential)=)[^&\s\"'#\\]+",
    re.IGNORECASE,
)

# ``name: value`` / ``name=value`` / ``"name": "value"`` pairs whose name is
# exactly one of these. The look-behind stops ``max_tokens`` / ``x-token``
# matching, and the required ``[:=]`` right after the name (and its optional
# closing quote) stops ``token_count`` / ``tokens``. An auth scheme word
# (``Bearer``) is kept and only the credential after it is masked.
_KV_RE = re.compile(
    r"(?<![\w-])"
    r"(?P<prefix>(?:api_key|apikey|api-key|access_token|refresh_token|password"
    r"|secret|client_secret|token|authorization)"
    r"(?P<name_quote>\\?[\"'])?\s*[:=]\s*(?P<value_quote>\\?[\"'])?)"
    r"(?P<scheme>(?:bearer|basic|token|digest)\s+)?"
    # A value opened by a plain quote runs to its closing quote (so
    # ``"password": "a b"`` is masked whole); anything else stops at the
    # first quote, space, separator or backslash.
    r"(?P<value>(?<=[^\\]\")(?:[^\"\\]|\\.)+(?=\")"
    r"|(?<=[^\\]')(?:[^'\\]|\\.)+(?=')"
    r"|[^\"'\s,;&}\]\\]+)",
    re.IGNORECASE,
)
# An unquoted JSON literal after a quoted name (``"token": 5``) is left alone:
# replacing it with bare text would make the line invalid JSON.
_JSON_LITERAL_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")

# Bare ``Bearer <token>``. The token alphabet excludes ``*`` so an already
# redacted value is not matched again.
_BEARER_RE = re.compile(r"(\b[Bb]earer\s+)[A-Za-z0-9._~+/=-]+")

# Bare ``Basic <base64>``. Requires a digit or ``+/=`` in the token so English
# ("Basic validation failed") is untouched.
_BASIC_RE = re.compile(
    r"(\bBasic\s+)(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{8,}={0,2}"
)

# Google API keys (Gemini AI Studio, Maps).
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")

# JWT-shaped tokens (header.payload.signature, base64url).
_JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


def _kv_sub(match: "re.Match[str]") -> str:
    if (
        match.group("name_quote")
        and not match.group("value_quote")
        and not match.group("scheme")
        and _JSON_LITERAL_RE.fullmatch(match.group("value"))
    ):
        return match.group(0)
    return f"{match.group('prefix')}{match.group('scheme') or ''}{REDACTED}"


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential values replaced by ``***REDACTED***``."""
    if not isinstance(text, str) or not text:
        return text
    lowered = text.lower()
    if not any(trigger in lowered for trigger in _TRIGGERS):
        return text
    text = _GOOGLE_KEY_RE.sub(REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _QUERY_PARAM_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _KV_RE.sub(_kv_sub, text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BASIC_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return text


# --------------------------------------------------------------------------
# Record factory
# --------------------------------------------------------------------------

_FACTORY_MARK = "_runsheet_log_redaction"
_TRACEBACK_FORMATTER = logging.Formatter()
_PLAIN_TYPES = (int, float, bool, type(None))


def _redact_value(value: Any) -> Any:
    """Redact one log argument.

    Strings are redacted in place. Any other object (an exception, an
    ``httpx.URL``) is replaced by its redacted ``str`` only when that string
    held a secret, so ``%r`` and ``%d`` behave as before for everything else.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, _PLAIN_TYPES):
        return value
    try:
        rendered = str(value)
    except Exception:  # noqa: BLE001 — a broken __str__ must not break logging
        return value
    redacted = redact_secrets(rendered)
    return value if redacted == rendered else redacted


def _redact_record(record: logging.LogRecord) -> None:
    args = record.args
    if isinstance(args, tuple):
        args = tuple(_redact_value(a) for a in args)
    elif isinstance(args, Mapping):
        args = {k: _redact_value(v) for k, v in args.items()}
    record.args = args

    msg = record.msg
    if isinstance(msg, str):
        redacted_msg = redact_secrets(msg)
        if redacted_msg != msg and args:
            # Redaction may have swallowed a ``%s`` placeholder (``key=%s``).
            # If the template no longer fits its arguments, render with the
            # original template and redact the result instead.
            try:
                redacted_msg % args
            except (TypeError, ValueError, KeyError):
                try:
                    redacted_msg = redact_secrets(msg % args)
                    record.args = ()
                except Exception:  # noqa: BLE001 — leave the bad call as it was
                    redacted_msg = msg
        record.msg = redacted_msg
    elif not isinstance(msg, _PLAIN_TYPES):
        record.msg = _redact_value(msg)

    if record.exc_info and not record.exc_text:
        record.exc_text = redact_secrets(
            _TRACEBACK_FORMATTER.formatException(record.exc_info)
        )
    if record.stack_info:
        record.stack_info = redact_secrets(record.stack_info)


def install_log_redaction() -> None:
    """Wrap the log-record factory with secret redaction. Idempotent."""
    current = logging.getLogRecordFactory()
    if getattr(current, _FACTORY_MARK, False):
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        try:
            _redact_record(record)
        except Exception:  # noqa: BLE001 — never let redaction drop a record
            pass
        return record

    setattr(redacting_factory, _FACTORY_MARK, True)
    logging.setLogRecordFactory(redacting_factory)


def apply_library_log_levels(level: Any = "WARNING") -> None:
    """Set the HTTP-client and LiteLLM loggers to ``level`` (default WARNING).

    Also raises any handler attached directly to a LiteLLM logger to at least
    ``level``, because LiteLLM sets its own handler's level from
    ``LITELLM_LOG``. An unknown or non-string level falls back to WARNING.
    """
    numeric = logging.getLevelName(level.strip().upper()) if isinstance(level, str) else None
    if not isinstance(numeric, int):
        numeric = logging.WARNING
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(numeric)
    for name in _LITELLM_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            if handler.level < numeric:
                handler.setLevel(numeric)


__all__ = [
    "LIBRARY_LOGGERS",
    "REDACTED",
    "apply_library_log_levels",
    "install_log_redaction",
    "redact_secrets",
]
