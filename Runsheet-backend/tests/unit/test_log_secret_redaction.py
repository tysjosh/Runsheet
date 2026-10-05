"""
N1: API keys and tokens must not reach the logs.

httpx logs every request URL at INFO, and the Gemini AI Studio path carries the
API key as ``?key=...``. On staging that line went to CloudWatch. These tests
drive ``TelemetryService`` and the stdlib ``logging`` API only, so on the old
code they fail on their assertions rather than on an import.

Every secret here is fake and built in the test.
"""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from telemetry.service import JSONFormatter, TelemetryService

FAKE_GOOGLE_KEY = "AIza" + "A" * 35
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-2.5-flash:streamGenerateContent?key={FAKE_GOOGLE_KEY}&alt=sse"
)
FAKE_JWT = "eyJ" + "h" * 20 + "." + "p" * 30 + "." + "s" * 25

_WATCHED_LOGGERS = (
    "httpx",
    "httpcore",
    "LiteLLM",
    "LiteLLM Router",
    "LiteLLM Proxy",
    "uvicorn.access",
    "n1.test",
)


@pytest.fixture
def restore_logging():
    """Save and restore root handlers, logger levels and the record factory."""
    root = logging.getLogger()
    saved_root_handlers = [
        h for h in root.handlers if not type(h).__module__.startswith("_pytest")
    ]
    saved_root_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in _WATCHED_LOGGERS}
    saved_handlers = {
        name: list(logging.getLogger(name).handlers) for name in _WATCHED_LOGGERS
    }
    saved_factory = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(saved_factory)
    for handler in root.handlers[:]:
        if isinstance(handler.formatter, JSONFormatter):
            root.removeHandler(handler)
    for handler in saved_root_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_root_level)
    for name in _WATCHED_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(saved_levels[name])
        lg.handlers[:] = saved_handlers[name]


class _ListHandler(logging.Handler):
    """Collects records and their formatted text."""

    def __init__(self, formatter: logging.Formatter | None = None) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []
        self.setFormatter(formatter or logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


def _telemetry(capsys, **overrides) -> TelemetryService:
    """Build TelemetryService against capsys's stdout and drop its boot line."""
    settings = SimpleNamespace(log_level="INFO", otel_endpoint=None, **overrides)
    service = TelemetryService(settings)
    capsys.readouterr()
    return service


def _json_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# (1) Default: httpx INFO lines are not written at all ----------------------


def test_httpx_request_line_is_not_logged_by_default(capsys, restore_logging):
    for name in ("httpx", "httpcore", "LiteLLM"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    _telemetry(capsys)
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"',
        "POST", httpx.URL(GEMINI_URL), "HTTP/1.1", 200, "OK",
    )

    assert capsys.readouterr().out == ""
    for name in ("httpx", "httpcore", "LiteLLM"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


# (2) Opt-in INFO: the line is written with the key masked ------------------


def test_httpx_request_line_at_info_has_the_key_redacted(capsys, restore_logging):
    _telemetry(capsys, http_client_log_level="INFO")
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"',
        "POST", httpx.URL(GEMINI_URL), "HTTP/1.1", 200, "OK",
    )

    out = capsys.readouterr().out
    assert FAKE_GOOGLE_KEY not in out
    (line,) = _json_lines(out)
    assert "key=***REDACTED***" in line["message"]
    assert "alt=sse" in line["message"]


# (3) Exceptions: message argument and traceback ----------------------------


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", GEMINI_URL)
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError(
        f"Client error '429 Too Many Requests' for url '{GEMINI_URL}'",
        request=request,
        response=response,
    )


def test_exception_argument_is_redacted(capsys, restore_logging):
    _telemetry(capsys)
    logging.getLogger("n1.test").warning("Gemini call failed: %s", _status_error())

    out = capsys.readouterr().out
    assert FAKE_GOOGLE_KEY not in out
    (line,) = _json_lines(out)
    assert "Gemini call failed: Client error '429" in line["message"]
    assert "key=***REDACTED***" in line["message"]


def test_exception_traceback_is_redacted(capsys, restore_logging):
    _telemetry(capsys)
    try:
        raise _status_error()
    except httpx.HTTPStatusError:
        logging.getLogger("n1.test").exception("Gemini call failed")

    out = capsys.readouterr().out
    assert FAKE_GOOGLE_KEY not in out
    (line,) = _json_lines(out)
    assert "HTTPStatusError" in line["exception"]
    assert "key=***REDACTED***" in line["exception"]


def test_exception_logged_as_the_message_is_redacted(capsys, restore_logging):
    _telemetry(capsys)
    logging.getLogger("n1.test").error(_status_error())

    out = capsys.readouterr().out
    assert FAKE_GOOGLE_KEY not in out
    (line,) = _json_lines(out)
    assert "key=***REDACTED***" in line["message"]


def test_secret_passed_into_a_key_placeholder_is_redacted(capsys, restore_logging):
    """``?key=%s`` loses its placeholder to redaction; the line must still render."""
    _telemetry(capsys)
    logging.getLogger("n1.test").warning(
        "GET %s?key=%s took %dms", "https://maps.example/api", "rawSECRET42", 12
    )

    out = capsys.readouterr().out
    assert "rawSECRET42" not in out
    (line,) = _json_lines(out)
    assert line["message"] == "GET https://maps.example/api?key=***REDACTED*** took 12ms"


# (4) extra_data payloads ---------------------------------------------------


def test_authorization_header_in_extra_data_is_redacted(capsys, restore_logging):
    _telemetry(capsys)
    logging.getLogger("n1.test").info(
        "outbound call",
        extra={"extra_data": {"headers": {"Authorization": "Bearer abc.def"}}},
    )

    out = capsys.readouterr().out
    assert "abc.def" not in out
    (line,) = _json_lines(out)  # still valid JSON
    assert line["headers"]["Authorization"] == "Bearer ***REDACTED***"


# (5) A handler attached directly to LiteLLM's logger -----------------------


def test_handler_on_litellm_logger_receives_redacted_records(capsys, restore_logging):
    _telemetry(capsys)
    own_handler = _ListHandler()
    logging.getLogger("LiteLLM").addHandler(own_handler)

    logging.getLogger("LiteLLM").warning(
        "POST Request Sent from LiteLLM: curl -X POST %s -H 'x-goog-api-key: %s'",
        GEMINI_URL,
        FAKE_GOOGLE_KEY,
    )

    assert own_handler.lines, "the LiteLLM handler received nothing"
    assert all(FAKE_GOOGLE_KEY not in text for text in own_handler.lines)
    assert "key=***REDACTED***" in own_handler.lines[0]


# (6) uvicorn access records keep their 5-tuple -----------------------------


def test_uvicorn_access_record_keeps_its_args_shape(capsys, restore_logging):
    from uvicorn.logging import AccessFormatter

    _telemetry(capsys)
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)
    handler = _ListHandler(
        AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s')
    )
    access.addHandler(handler)

    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000", "GET", "/x?token=SECRET", "1.1", 200,
    )

    (record,) = handler.records
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert "SECRET" not in record.args[2]
    assert "SECRET" not in handler.lines[0]
    assert "/x?token=***REDACTED***" in handler.lines[0]


# (7) Pattern coverage through the logging pipeline -------------------------


@pytest.mark.parametrize(
    "text, secret",
    [
        ("GET https://router.hereapi.com/v8/routes?apiKey=hereSECRET123&origin=1,2", "hereSECRET123"),
        ("GET https://maps.googleapis.com/maps/api/distancematrix/json?origins=a&key=gSECRET456", "gSECRET456"),
        ("GET https://api.mapbox.com/directions-matrix/v1/x?access_token=pk.mbSECRET789", "pk.mbSECRET789"),
        ("GET https://api.openweathermap.org/data/3.0/onecall?lat=1&appid=owSECRET000", "owSECRET000"),
        (f"session cookie {FAKE_JWT}", FAKE_JWT),
        ('payload {"password": "hunter2pass"}', "hunter2pass"),
        ("payload {'client_secret': 'two words'}", "words"),
        (f"raw key {FAKE_GOOGLE_KEY} in text", FAKE_GOOGLE_KEY),
    ],
)
def test_secret_patterns_are_redacted(capsys, restore_logging, text, secret):
    _telemetry(capsys)
    logging.getLogger("n1.test").warning("%s", text)

    out = capsys.readouterr().out
    assert secret not in out
    (line,) = _json_lines(out)
    assert "***REDACTED***" in line["message"]


@pytest.mark.parametrize(
    "text",
    [
        "cache get failed for key=cache_1",
        "llm call max_tokens=50",
        "usage token_count=3",
    ],
)
def test_lookalikes_are_left_unchanged(capsys, restore_logging, text):
    _telemetry(capsys)
    logging.getLogger("n1.test").warning(text)

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["message"] == text


# (8) Settings ---------------------------------------------------------------


_SETTINGS_ENV = {
    "GOOGLE_CLOUD_PROJECT": "test-project-id",
    "ENVIRONMENT": "development",
}


def test_http_client_log_level_defaults_to_warning_and_normalizes():
    from config.settings import Settings

    with patch.dict(os.environ, _SETTINGS_ENV, clear=True):
        assert Settings().http_client_log_level == "WARNING"
    with patch.dict(os.environ, {**_SETTINGS_ENV, "HTTP_CLIENT_LOG_LEVEL": "debug"}, clear=True):
        assert Settings().http_client_log_level == "DEBUG"


def test_http_client_log_level_rejects_unknown_values():
    from config.settings import Settings

    with patch.dict(os.environ, {**_SETTINGS_ENV, "HTTP_CLIENT_LOG_LEVEL": "nonsense"}, clear=True):
        with pytest.raises(ValidationError, match="http_client_log_level"):
            Settings()
