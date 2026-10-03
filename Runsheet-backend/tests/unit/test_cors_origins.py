"""
Tests for :mod:`config.cors`, the ``CORS_ORIGINS`` parser shared by the REST
``CORSMiddleware`` and the WebSocket Origin check (staging finding F2).
"""

from __future__ import annotations

import logging
import os

from config.cors import DEFAULT_CORS_ORIGINS, get_cors_origins, parse_cors_origins


def test_unset_gives_default():
    assert parse_cors_origins(None) == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    assert list(DEFAULT_CORS_ORIGINS) == parse_cors_origins(None)


def test_valid_json_list_is_returned_verbatim():
    raw = '["https://app.example.com", "http://localhost:8081"]'
    assert parse_cors_origins(raw) == [
        "https://app.example.com",
        "http://localhost:8081",
    ]


def test_bad_json_gives_default_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="config.cors"):
        assert parse_cors_origins("not json") == list(DEFAULT_CORS_ORIGINS)
    assert "Failed to parse CORS_ORIGINS" in caplog.text


def test_json_non_list_gives_default(caplog):
    with caplog.at_level(logging.WARNING, logger="config.cors"):
        assert parse_cors_origins('"https://app.example.com"') == list(
            DEFAULT_CORS_ORIGINS
        )
    assert "Failed to parse CORS_ORIGINS" in caplog.text


def test_get_reads_env_at_call_time(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://a.example.com"]')
    assert get_cors_origins() == ["https://a.example.com"]
    monkeypatch.delenv("CORS_ORIGINS")
    assert get_cors_origins() == list(DEFAULT_CORS_ORIGINS)


def test_main_cors_middleware_uses_the_shared_parser():
    from fastapi.middleware.cors import CORSMiddleware

    from main import app

    entries = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(entries) == 1
    assert entries[0].kwargs["allow_origins"] == parse_cors_origins(
        os.environ.get("CORS_ORIGINS")
    )
