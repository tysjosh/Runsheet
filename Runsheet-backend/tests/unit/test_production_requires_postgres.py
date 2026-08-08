"""Production must not run with a dormant persistence layer.

``database_url`` is ``Optional`` because ES-only was the pre-migration posture,
and the layer degrades to it *silently*: ``is_persistence_enabled()`` returns
False and every caller takes the legacy path without a warning. Three
correctness guarantees exist only in Postgres, and all three fail quietly:

* **invoice numbering** — ``allocate_invoice_number`` returns ``None`` when
  dormant and the invoice is finalized with no ``invoice_number``. An open
  invoice no accounting system can reference, returned as 200.
* **idempotency keys** — ``IdempotencyKeyORM``'s own docstring: the ES index
  "could not actually prevent two concurrent requests with the same key from
  both being processed".
* **credit limits** — the ``SELECT ... FOR UPDATE`` behind the credit check has
  no ES equivalent, so concurrent orders can exceed a limit.

The real ``.env.production`` in this workspace has no ``DATABASE_URL``, so this
was not hypothetical — it was the configuration a deploy would have used.

Having the URL is necessary but not sufficient: numbering is gated on
``database_url`` AND ``commerce_dual_write_postgres``
(``commerce_persistence_bridge._enabled``), so commerce-enabled-but-dual-write-off
is refused too.

Requirements: 1.2 (environment-specific configuration validation).
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

_DB = "postgresql+psycopg://u:p@db.internal:5432/runsheet"


def _base(environment: str) -> dict:
    """A config that is otherwise complete for a non-development environment."""
    env = {
        "ENVIRONMENT": environment,
        "ELASTIC_ENDPOINT": "https://es.example.com:9243",
        "ELASTIC_API_KEY": "test-api-key",
        "GEMINI_API_KEY": "test-gemini-key",
        "SESSION_STORE_TYPE": "redis",
        "REDIS_URL": "redis://redis.internal:6379",
        "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
        "SUPERTOKENS_API_KEY": "st-managed-core-api-key",
    }
    if environment == "production":
        env["CORS_ORIGINS"] = '["https://app.runsheet.com"]'
    return env


def _load(env: dict):
    from config.settings import Settings

    with patch.dict(os.environ, env, clear=True):
        return Settings()


class TestDatabaseUrlIsRequiredOutsideDevelopment:
    @pytest.mark.parametrize("environment", ["production", "staging"])
    def test_missing_database_url_refuses_to_start(self, environment):
        with pytest.raises(Exception) as excinfo:
            _load(_base(environment))
        message = str(excinfo.value)
        assert "database_url is required" in message
        # The error has to say WHY, or the next engineer just sets it to ""
        # to make the message go away.
        assert "invoice_number" in message
        assert environment in message

    @pytest.mark.parametrize("environment", ["production", "staging"])
    def test_supplying_it_passes(self, environment):
        settings = _load({**_base(environment), "DATABASE_URL": _DB})
        assert settings.database_url == _DB

    def test_development_still_allows_a_dormant_layer(self):
        """The counterweight — this must stay a laptop-friendly default.

        A developer without Postgres running has to be able to boot; that is the
        whole reason the field is Optional. Requiring it everywhere would be a
        different kind of wrong.
        """
        settings = _load(
            {
                "ENVIRONMENT": "development",
                "ELASTIC_ENDPOINT": "http://localhost:9200",
                "ELASTIC_API_KEY": "dev",
                "GEMINI_API_KEY": "dev",
            }
        )
        assert settings.database_url is None

    def test_test_environment_still_allows_a_dormant_layer(self):
        """Same for ENVIRONMENT=test: the suite must not need a live database.

        ``REDIS_URL`` is supplied because the redis validator excludes only
        ``development`` — not ``test`` — so a bare test config trips that
        unrelated check first. The new guard follows the SuperTokens validator
        instead and exempts both, which is what lets the suite run without
        Postgres.
        """
        settings = _load(
            {
                "ENVIRONMENT": "test",
                "ELASTIC_ENDPOINT": "http://localhost:9200",
                "ELASTIC_API_KEY": "t",
                "GEMINI_API_KEY": "t",
                "REDIS_URL": "redis://localhost:6379",
            }
        )
        assert settings.database_url is None


class TestCommerceRequiresDualWrite:
    """A database URL alone does not switch numbering on."""

    def test_commerce_enabled_without_dual_write_refuses_to_start(self):
        with pytest.raises(Exception) as excinfo:
            _load(
                {
                    **_base("production"),
                    "DATABASE_URL": _DB,
                    "COMMERCE_BACKBONE_ENABLED": "true",
                    "COMMERCE_DUAL_WRITE_POSTGRES": "false",
                }
            )
        message = str(excinfo.value)
        assert "commerce_dual_write_postgres must be True" in message
        assert "invoice numbering" in message

    def test_commerce_enabled_with_dual_write_passes(self):
        settings = _load(
            {
                **_base("production"),
                "DATABASE_URL": _DB,
                "COMMERCE_BACKBONE_ENABLED": "true",
                "COMMERCE_DUAL_WRITE_POSTGRES": "true",
            }
        )
        assert settings.commerce_backbone_enabled is True
        assert settings.commerce_dual_write_postgres is True

    def test_commerce_disabled_does_not_require_dual_write(self):
        """Counterweight: the guard is about commerce, not about Postgres.

        A deployment that does not run the commerce backbone has no invoices to
        number, so demanding dual-write there would be cargo-culting.
        """
        settings = _load(
            {
                **_base("production"),
                "DATABASE_URL": _DB,
                "COMMERCE_BACKBONE_ENABLED": "false",
                "COMMERCE_DUAL_WRITE_POSTGRES": "false",
            }
        )
        assert settings.commerce_backbone_enabled is False
