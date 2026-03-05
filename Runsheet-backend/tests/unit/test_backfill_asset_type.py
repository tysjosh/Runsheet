"""
Unit tests for the backfill_asset_type migration script.
"""

import pytest
from unittest.mock import MagicMock, patch

from scripts.backfill_asset_type import (
    _find_trucks_without_asset_type,
    _build_bulk_actions,
    run_backfill,
)


# ---------------------------------------------------------------------------
# _find_trucks_without_asset_type
# ---------------------------------------------------------------------------

class TestFindTrucksWithoutAssetType:
    def test_returns_docs_missing_asset_type(self):
        client = MagicMock()
        hits = [
            {"_id": "1", "_source": {"truck_id": "T1", "plate_number": "ABC-123"}},
            {"_id": "2", "_source": {"truck_id": "T2", "plate_number": "XYZ-789"}},
        ]
        # First search returns hits, second scroll returns empty (end)
        client.search.return_value = {
            "_scroll_id": "scroll1",
            "hits": {"hits": hits},
        }
        client.scroll.return_value = {
            "_scroll_id": "scroll1",
            "hits": {"hits": []},
        }
        client.clear_scroll.return_value = None

        result = _find_trucks_without_asset_type(client, "trucks")

        assert len(result) == 2
        assert result[0]["_id"] == "1"
        assert result[0]["plate_number"] == "ABC-123"
        assert result[1]["_id"] == "2"
        assert result[1]["plate_number"] == "XYZ-789"

    def test_returns_empty_when_all_have_asset_type(self):
        client = MagicMock()
        client.search.return_value = {
            "_scroll_id": "scroll1",
            "hits": {"hits": []},
        }
        client.clear_scroll.return_value = None

        result = _find_trucks_without_asset_type(client, "trucks")
        assert result == []

    def test_handles_missing_plate_number(self):
        client = MagicMock()
        client.search.return_value = {
            "_scroll_id": "scroll1",
            "hits": {"hits": [
                {"_id": "3", "_source": {"truck_id": "T3"}},
            ]},
        }
        client.scroll.return_value = {"_scroll_id": "scroll1", "hits": {"hits": []}}
        client.clear_scroll.return_value = None

        result = _find_trucks_without_asset_type(client, "trucks")
        assert result[0]["plate_number"] == ""


# ---------------------------------------------------------------------------
# _build_bulk_actions
# ---------------------------------------------------------------------------

class TestBuildBulkActions:
    def test_builds_correct_update_actions(self):
        docs = [
            {"_id": "1", "plate_number": "ABC-123"},
            {"_id": "2", "plate_number": "XYZ-789"},
        ]
        actions = _build_bulk_actions("trucks", docs)

        assert len(actions) == 2
        for action in actions:
            assert action["_op_type"] == "update"
            assert action["_index"] == "trucks"
            assert action["doc"]["asset_type"] == "vehicle"
            assert action["doc"]["asset_subtype"] == "truck"

        assert actions[0]["doc"]["asset_name"] == "ABC-123"
        assert actions[1]["doc"]["asset_name"] == "XYZ-789"

    def test_falls_back_to_id_when_plate_number_empty(self):
        docs = [{"_id": "doc-99", "plate_number": ""}]
        actions = _build_bulk_actions("trucks", docs)

        assert actions[0]["doc"]["asset_name"] == "doc-99"

    def test_empty_docs_returns_empty_actions(self):
        assert _build_bulk_actions("trucks", []) == []


# ---------------------------------------------------------------------------
# run_backfill (integration-style with mocks)
# ---------------------------------------------------------------------------

class TestRunBackfill:
    @patch("scripts.backfill_asset_type.bulk")
    @patch("scripts.backfill_asset_type._connect")
    @patch("scripts.backfill_asset_type.get_settings")
    def test_updates_documents_and_returns_count(
        self, mock_settings, mock_connect, mock_bulk
    ):
        client = MagicMock()
        mock_connect.return_value = client
        client.search.return_value = {
            "_scroll_id": "s1",
            "hits": {"hits": [
                {"_id": "1", "_source": {"plate_number": "P1"}},
            ]},
        }
        client.scroll.return_value = {"_scroll_id": "s1", "hits": {"hits": []}}
        client.clear_scroll.return_value = None
        mock_bulk.return_value = (1, [])

        count = run_backfill()

        assert count == 1
        mock_bulk.assert_called_once()

    @patch("scripts.backfill_asset_type._connect")
    @patch("scripts.backfill_asset_type.get_settings")
    def test_returns_zero_when_nothing_to_update(self, mock_settings, mock_connect):
        client = MagicMock()
        mock_connect.return_value = client
        client.search.return_value = {
            "_scroll_id": "s1",
            "hits": {"hits": []},
        }
        client.clear_scroll.return_value = None

        count = run_backfill()
        assert count == 0
