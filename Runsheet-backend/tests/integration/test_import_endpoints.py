"""
Integration tests for import API endpoints.

Tests the full HTTP request/response cycle for all import endpoints
using httpx.AsyncClient with ASGITransport against a minimal FastAPI app.

Validates:
- Requirements 11.1–11.6: Backend Import API endpoints
- Requirements 3.3, 3.4: CSV file upload validation (size, format)
- Requirements 10.1–10.7: Data type to index mapping
"""

import csv
import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from middleware.rate_limiter import limiter
from services.import_models import ImportStatus
from services.import_service import ImportService
from services.schema_templates import SchemaTemplates

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUPPORTED_DATA_TYPES = [
    "fleet", "orders", "riders", "fuel_stations",
    "inventory", "support_tickets", "jobs",
]

DATA_TYPE_INDEX_MAP = {
    "fleet": "trucks",
    "orders": "orders",
    "riders": "riders",
    "fuel_stations": "fuel_stations",
    "inventory": "inventory",
    "support_tickets": "support_tickets",
    "jobs": "jobs",
}

_schema_templates = SchemaTemplates()


def _csv_bytes_for(data_type: str, num_rows: int = 3) -> bytes:
    """Generate valid CSV bytes for a given data type using schema templates."""
    template = _schema_templates.get_template(data_type)
    field_names = [f.name for f in template.fields]
    example_rows = SchemaTemplates._EXAMPLE_DATA.get(data_type, [])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(field_names)
    for row_dict in example_rows[:num_rows]:
        writer.writerow([row_dict.get(f, "") for f in field_names])
    return buf.getvalue().encode("utf-8")


def _mock_es_service() -> MagicMock:
    """Create a mock ElasticsearchService with sensible defaults."""
    mock = MagicMock()
    mock.bulk_index_documents = AsyncMock(
        return_value={"successful": 3, "failed": 0, "errors": []}
    )
    mock.search_documents = AsyncMock(
        return_value={"hits": {"hits": [], "total": {"value": 0}}}
    )
    mock.index_document = AsyncMock(return_value={"result": "created"})
    return mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_es():
    """Provide a fresh mock ElasticsearchService."""
    return _mock_es_service()


@pytest.fixture()
def test_app(mock_es):
    """Create a minimal FastAPI app with the import router and mocked ES."""
    app = FastAPI()

    # Set up rate limiter on the test app
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Patch the module-level import_service with one backed by our mock ES
    test_import_service = ImportService(mock_es)

    with patch("import_endpoints.import_service", test_import_service):
        from import_endpoints import router
        app.include_router(router)
        # Store references for test access
        app.state._test_import_service = test_import_service
        app.state._test_mock_es = mock_es
        yield app


@pytest.fixture()
async def client(test_app):
    """Provide an async HTTP client bound to the test app."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ===========================================================================
# POST /api/import/upload/csv
# ===========================================================================

class TestUploadCSV:
    """Tests for CSV file upload endpoint.

    Validates: Requirements 11.1, 3.3, 3.4
    """

    async def test_upload_valid_csv_fleet(self, client):
        """Upload a valid fleet CSV and verify parse result."""
        csv_data = _csv_bytes_for("fleet")
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("fleet.csv", csv_data, "text/csv")},
            data={"data_type": "fleet"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "session_id" in body
        assert "columns" in body
        assert "truck_id" in body["columns"]
        assert body["total_rows"] == 3
        assert len(body["sample_rows"]) <= 5
        assert "suggested_mapping" in body

    async def test_upload_valid_csv_orders(self, client):
        """Upload a valid orders CSV."""
        csv_data = _csv_bytes_for("orders")
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("orders.csv", csv_data, "text/csv")},
            data={"data_type": "orders"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "order_id" in body["columns"]

    async def test_upload_oversized_file(self, client):
        """Reject a file exceeding 10 MB. Validates: Requirement 3.3"""
        big_data = b"a" * (10 * 1024 * 1024 + 1)
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("big.csv", big_data, "text/csv")},
            data={"data_type": "fleet"},
        )
        assert resp.status_code == 400
        assert "10MB" in resp.json()["detail"] or "size limit" in resp.json()["detail"].lower()

    async def test_upload_non_csv_file(self, client):
        """Reject a non-CSV file. Validates: Requirement 3.4"""
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("data.txt", b"hello world", "text/plain")},
            data={"data_type": "fleet"},
        )
        assert resp.status_code == 400
        assert "csv" in resp.json()["detail"].lower()

    async def test_upload_invalid_data_type(self, client):
        """Reject an unsupported data type."""
        csv_data = b"col1,col2\nval1,val2\n"
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("data.csv", csv_data, "text/csv")},
            data={"data_type": "nonexistent"},
        )
        assert resp.status_code == 422
        assert "unsupported" in resp.json()["detail"].lower() or "Unsupported" in resp.json()["detail"]

    async def test_upload_csv_empty_file(self, client):
        """Reject an empty CSV (no header row)."""
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("empty.csv", b"", "text/csv")},
            data={"data_type": "fleet"},
        )
        assert resp.status_code == 422


# ===========================================================================
# POST /api/import/upload/sheets
# ===========================================================================

class TestUploadSheets:
    """Tests for Google Sheets upload endpoint.

    Validates: Requirements 11.2
    """

    async def test_upload_sheets_invalid_url(self, client):
        """Reject an invalid Google Sheets URL."""
        resp = await client.post(
            "/api/import/upload/sheets",
            json={"url": "https://example.com/not-a-sheet", "data_type": "fleet"},
        )
        assert resp.status_code == 422
        assert "google" in resp.json()["detail"].lower() or "sheet" in resp.json()["detail"].lower()

    async def test_upload_sheets_invalid_data_type(self, client):
        """Reject an unsupported data type for sheets."""
        resp = await client.post(
            "/api/import/upload/sheets",
            json={
                "url": "https://docs.google.com/spreadsheets/d/abc123/edit",
                "data_type": "nonexistent",
            },
        )
        assert resp.status_code == 422

    async def test_upload_sheets_valid_url_network_error(self, client):
        """A valid-looking URL that can't be fetched returns 422."""
        resp = await client.post(
            "/api/import/upload/sheets",
            json={
                "url": "https://docs.google.com/spreadsheets/d/fake_id_12345/edit",
                "data_type": "fleet",
            },
        )
        # The endpoint should return 422 because the sheet can't be fetched
        assert resp.status_code == 422


# ===========================================================================
# POST /api/import/validate
# ===========================================================================

class TestValidate:
    """Tests for the validation endpoint.

    Validates: Requirements 11.3
    """

    async def _create_session(self, client, data_type: str = "fleet") -> str:
        """Helper: upload a CSV and return the session_id."""
        csv_data = _csv_bytes_for(data_type)
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": (f"{data_type}.csv", csv_data, "text/csv")},
            data={"data_type": data_type},
        )
        assert resp.status_code == 200
        return resp.json()["session_id"]

    async def test_validate_valid_session(self, client):
        """Validate with a valid session and correct mapping."""
        session_id = await self._create_session(client, "fleet")
        mapping = {
            "truck_id": "truck_id",
            "plate_number": "plate_number",
            "status": "status",
        }
        resp = await client.post(
            "/api/import/validate",
            json={"session_id": session_id, "field_mapping": mapping},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == session_id
        assert "total_rows" in body
        assert "valid_rows" in body
        assert "error_count" in body
        assert "errors" in body
        assert "warnings" in body

    async def test_validate_invalid_session(self, client):
        """Validate with a non-existent session returns 404."""
        resp = await client.post(
            "/api/import/validate",
            json={
                "session_id": str(uuid.uuid4()),
                "field_mapping": {"col": "field"},
            },
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_validate_with_full_mapping(self, client):
        """Validate with all fields mapped produces valid rows."""
        session_id = await self._create_session(client, "inventory")
        # Map all required fields for inventory: item_id, name, quantity
        mapping = {
            "item_id": "item_id",
            "name": "name",
            "quantity": "quantity",
            "category": "category",
            "unit": "unit",
            "location": "location",
            "status": "status",
            "last_updated": "last_updated",
        }
        resp = await client.post(
            "/api/import/validate",
            json={"session_id": session_id, "field_mapping": mapping},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid_rows"] > 0


# ===========================================================================
# POST /api/import/commit
# ===========================================================================

class TestCommit:
    """Tests for the commit endpoint.

    Validates: Requirements 11.4
    """

    async def _create_and_validate_session(
        self, client, data_type: str = "fleet"
    ) -> str:
        """Helper: upload CSV, validate, return session_id."""
        csv_data = _csv_bytes_for(data_type)
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": (f"{data_type}.csv", csv_data, "text/csv")},
            data={"data_type": data_type},
        )
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        # Build a mapping from the suggested mapping
        suggested = resp.json().get("suggested_mapping", {})
        if not suggested:
            template = _schema_templates.get_template(data_type)
            suggested = {f.name: f.name for f in template.fields}

        resp = await client.post(
            "/api/import/validate",
            json={"session_id": session_id, "field_mapping": suggested},
        )
        assert resp.status_code == 200
        return session_id

    async def test_commit_valid_session(self, client, mock_es):
        """Commit a validated session successfully."""
        mock_es.bulk_index_documents = AsyncMock(
            return_value={"successful": 3, "failed": 0, "errors": []}
        )
        session_id = await self._create_and_validate_session(client, "fleet")
        resp = await client.post(
            "/api/import/commit",
            json={"session_id": session_id, "skip_errors": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == session_id
        assert body["status"] in [s.value for s in ImportStatus]
        assert body["data_type"] == "fleet"
        assert body["es_index"] == "trucks"
        assert "imported_records" in body
        assert "duration_seconds" in body

    async def test_commit_with_skip_errors(self, client, mock_es):
        """Commit with skip_errors=True."""
        mock_es.bulk_index_documents = AsyncMock(
            return_value={"successful": 2, "failed": 0, "errors": []}
        )
        session_id = await self._create_and_validate_session(client, "orders")
        resp = await client.post(
            "/api/import/commit",
            json={"session_id": session_id, "skip_errors": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_type"] == "orders"
        assert body["es_index"] == "orders"

    async def test_commit_invalid_session(self, client):
        """Commit with a non-existent session returns 404."""
        resp = await client.post(
            "/api/import/commit",
            json={"session_id": str(uuid.uuid4()), "skip_errors": False},
        )
        assert resp.status_code == 404

    async def test_commit_unvalidated_session(self, client):
        """Commit a session that hasn't been validated returns 409."""
        csv_data = _csv_bytes_for("fleet")
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("fleet.csv", csv_data, "text/csv")},
            data={"data_type": "fleet"},
        )
        session_id = resp.json()["session_id"]
        resp = await client.post(
            "/api/import/commit",
            json={"session_id": session_id, "skip_errors": False},
        )
        assert resp.status_code == 409
        assert "not been validated" in resp.json()["detail"].lower()


# ===========================================================================
# GET /api/import/history
# ===========================================================================

class TestHistory:
    """Tests for the import history endpoint.

    Validates: Requirements 11.5
    """

    async def test_get_history_empty(self, client):
        """Get history when no imports have been done."""
        resp = await client.get("/api/import/history")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_history_with_data_type_filter(self, client):
        """Get history filtered by data_type."""
        resp = await client.get("/api/import/history", params={"data_type": "fleet"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_history_with_status_filter(self, client):
        """Get history filtered by status."""
        resp = await client.get("/api/import/history", params={"status": "completed"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_history_with_both_filters(self, client):
        """Get history filtered by both data_type and status."""
        resp = await client.get(
            "/api/import/history",
            params={"data_type": "orders", "status": "failed"},
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_history_returns_records_after_commit(self, client, mock_es):
        """After a commit, history should include the session record."""
        # Set up mock to return a session record from ES
        session_record = {
            "session_id": "test-session-123",
            "data_type": "fleet",
            "source_type": "csv",
            "source_name": "fleet.csv",
            "total_records": 3,
            "imported_records": 3,
            "skipped_records": 0,
            "error_count": 0,
            "status": "completed",
            "errors": [],
            "field_mapping": {"truck_id": "truck_id"},
            "created_at": "2024-01-15T10:00:00Z",
            "completed_at": "2024-01-15T10:00:05Z",
            "duration_seconds": 5.0,
        }
        mock_es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": session_record}],
                    "total": {"value": 1},
                }
            }
        )
        resp = await client.get("/api/import/history")
        assert resp.status_code == 200
        records = resp.json()
        assert len(records) == 1
        assert records[0]["session_id"] == "test-session-123"
        assert records[0]["data_type"] == "fleet"


# ===========================================================================
# GET /api/import/history/{session_id}
# ===========================================================================

class TestHistorySession:
    """Tests for the single session detail endpoint.

    Validates: Requirements 11.6
    """

    async def test_get_session_valid_id(self, client, mock_es):
        """Retrieve a session by valid ID."""
        session_record = {
            "session_id": "sess-abc-123",
            "data_type": "orders",
            "source_type": "csv",
            "source_name": "orders.csv",
            "total_records": 10,
            "imported_records": 8,
            "skipped_records": 2,
            "error_count": 2,
            "status": "partial",
            "errors": ["Row 3: missing order_id"],
            "field_mapping": {"order_id": "order_id"},
            "created_at": "2024-02-01T08:00:00Z",
            "completed_at": "2024-02-01T08:00:10Z",
            "duration_seconds": 10.0,
        }
        mock_es.search_documents = AsyncMock(
            return_value={
                "hits": {
                    "hits": [{"_source": session_record}],
                    "total": {"value": 1},
                }
            }
        )
        resp = await client.get("/api/import/history/sess-abc-123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "sess-abc-123"
        assert body["data_type"] == "orders"
        assert body["status"] == "partial"

    async def test_get_session_invalid_id(self, client, mock_es):
        """Retrieve a session with a non-existent ID returns 404."""
        mock_es.search_documents = AsyncMock(
            return_value={"hits": {"hits": [], "total": {"value": 0}}}
        )
        resp = await client.get(f"/api/import/history/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ===========================================================================
# GET /api/import/templates/{data_type}
# ===========================================================================

class TestTemplates:
    """Tests for the CSV template download endpoint.

    Validates: Requirements 9.1, 9.2, 9.3, 10.1–10.7
    """

    @pytest.mark.parametrize("data_type", SUPPORTED_DATA_TYPES)
    async def test_download_template_for_each_type(self, client, data_type):
        """Download CSV template for each supported data type."""
        resp = await client.get(f"/api/import/templates/{data_type}")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")

        # Parse the CSV content
        content = resp.text
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        # Header row + 2-3 example rows
        assert len(rows) >= 3  # header + at least 2 data rows
        assert len(rows) <= 4  # header + at most 3 data rows

        # Verify header matches schema template fields
        template = _schema_templates.get_template(data_type)
        expected_fields = [f.name for f in template.fields]
        assert rows[0] == expected_fields

    async def test_download_template_invalid_type(self, client):
        """Request a template for an unsupported data type returns 400."""
        resp = await client.get("/api/import/templates/nonexistent")
        assert resp.status_code == 400


# ===========================================================================
# GET /api/import/schemas/{data_type}
# ===========================================================================

class TestSchemas:
    """Tests for the schema endpoint.

    Validates: Requirements 2.2, 2.3
    """

    @pytest.mark.parametrize("data_type", SUPPORTED_DATA_TYPES)
    async def test_get_schema_for_each_type(self, client, data_type):
        """Get schema for each supported data type."""
        resp = await client.get(f"/api/import/schemas/{data_type}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_type"] == data_type
        assert "description" in body
        assert len(body["description"]) > 0
        assert "es_index" in body
        assert "fields" in body
        assert len(body["fields"]) > 0

        # Verify each field has required attributes
        for field in body["fields"]:
            assert "name" in field
            assert "type" in field
            assert "required" in field
            assert "description" in field

    async def test_get_schema_invalid_type(self, client):
        """Request schema for an unsupported data type returns 400."""
        resp = await client.get("/api/import/schemas/nonexistent")
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "data_type,expected_index",
        list(DATA_TYPE_INDEX_MAP.items()),
    )
    async def test_schema_es_index_mapping(self, client, data_type, expected_index):
        """Verify each data type maps to the correct ES index.

        Validates: Requirements 10.1–10.7
        """
        resp = await client.get(f"/api/import/schemas/{data_type}")
        assert resp.status_code == 200
        assert resp.json()["es_index"] == expected_index


# ===========================================================================
# End-to-end workflow test
# ===========================================================================

class TestImportWorkflow:
    """End-to-end test covering upload → validate → commit."""

    async def test_full_csv_import_workflow(self, client, mock_es):
        """Run the complete import workflow for fleet data."""
        mock_es.bulk_index_documents = AsyncMock(
            return_value={"successful": 3, "failed": 0, "errors": []}
        )

        # Step 1: Upload CSV
        csv_data = _csv_bytes_for("fleet")
        resp = await client.post(
            "/api/import/upload/csv",
            files={"file": ("fleet.csv", csv_data, "text/csv")},
            data={"data_type": "fleet"},
        )
        assert resp.status_code == 200
        parse_result = resp.json()
        session_id = parse_result["session_id"]
        suggested = parse_result["suggested_mapping"]

        # Step 2: Validate
        resp = await client.post(
            "/api/import/validate",
            json={"session_id": session_id, "field_mapping": suggested},
        )
        assert resp.status_code == 200
        validation = resp.json()
        assert validation["total_rows"] == 3

        # Step 3: Commit
        resp = await client.post(
            "/api/import/commit",
            json={"session_id": session_id, "skip_errors": False},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["status"] in ["completed", "partial"]
        assert result["data_type"] == "fleet"
        assert result["es_index"] == "trucks"
        assert result["imported_records"] == 3
