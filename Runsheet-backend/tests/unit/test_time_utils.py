"""Tests for ``services.time_utils`` and tz-awareness on ES write paths.

Covers:
- ``utcnow()`` returns a tz-aware ``datetime`` whose ``tzinfo`` is
  ``timezone.utc``.
- ``utcnow().isoformat()`` serialises with a ``+00:00`` suffix (no naive
  local-time leakage, no ``"Z"`` / ``+00:00`` double-suffix bugs).
- ``ElasticsearchService.index_document`` stamps every written document
  with tz-aware ``created_at`` / ``updated_at`` values so the ES write
  hot path can never emit a naive timestamp on a non-UTC host.

Validates code-review finding F13 — naive ``datetime.now()`` /
``datetime.utcnow()`` calls must not survive on the ES write hot path.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# Other unit tests in the suite register a ``MagicMock`` under
# ``services.elasticsearch_service`` via ``sys.modules.setdefault`` so
# their transitive imports don't hit a live cluster. That leaves us with
# a mocked class when pytest collects this file after them. Force a
# fresh load of the real module so we can instantiate the real
# ``ElasticsearchService`` for hot-path assertions.
sys.modules.pop("services.elasticsearch_service", None)
_es_module = importlib.import_module("services.elasticsearch_service")
ElasticsearchService = _es_module.ElasticsearchService

from services.time_utils import utcnow  # noqa: E402


def test_utcnow_returns_timezone_aware() -> None:
    """``utcnow()`` must return a tz-aware datetime anchored to UTC."""
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is timezone.utc


def test_utcnow_serialises_to_iso() -> None:
    """``utcnow().isoformat()`` must end with ``+00:00``, not a naive or ``Z`` suffix."""
    iso = utcnow().isoformat()
    assert iso.endswith("+00:00"), iso
    # Must be a proper ISO-8601 string that round-trips via fromisoformat.
    roundtrip = datetime.fromisoformat(iso)
    assert roundtrip.tzinfo is not None











# The four ``index_document`` / ``update_document`` stamping tests that lived here
# moved to ``tests/postgres/test_document_store_timestamps.py``.
#
# They asserted the auto-stamped ``created_at`` / ``updated_at`` by inspecting the
# body handed to a stubbed Elasticsearch client. Phase 6 deleted that client, and the
# stamping had already moved into ``PostgresDocumentStore``, so the assertions had to
# follow the behaviour rather than be deleted with the stub — it was not covered at
# the store layer at all.
#
# One of them got MORE important in the move: Elasticsearch rejected auto-stamped
# fields on the strict event-stream mappings, so a regression was a hard write
# failure. jsonb accepts them quietly, so the ``TIMESTAMP_SKIP_INDICES`` assertion is
# now the only thing that would notice.
