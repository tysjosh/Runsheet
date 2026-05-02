# Implementation Plan: Data Import / Migration Tool

## Overview

Replace the demo-oriented DataUpload component with a production-grade data import/migration tool. The implementation proceeds backend-first (schema templates → validation → field mapping → import service → API endpoints → ES index), then frontend (types → API service → wizard component → sub-components → routing updates), followed by property-based tests and integration tests.

## Tasks

- [x] 1. Backend schema templates and data models
  - [x] 1.1 Create `Runsheet-backend/services/schema_templates.py` with `FieldDef`, `SchemaTemplate`, and `SchemaTemplates` class
    - Define `FieldDef` Pydantic model with fields: name, type (FieldType enum), required, description, enum_values, date_format
    - Define `SchemaTemplate` Pydantic model with fields: data_type, description, es_index, fields
    - Define `SchemaTemplates` class with `TEMPLATES` dict containing all 7 data types (fleet, orders, riders, fuel_stations, inventory, support_tickets, jobs)
    - Define `DATA_TYPE_INDEX_MAP` dict mapping each data type to its ES index (fleet→trucks, orders→orders, inventory→inventory, support_tickets→support_tickets, riders→riders, fuel_stations→fuel_stations, jobs→jobs)
    - Implement `get_template()`, `get_index()`, `get_required_fields()`, `get_optional_fields()`, `generate_csv_template()` methods
    - _Requirements: 2.1, 2.2, 2.3, 9.1, 9.2, 9.3, 10.1–10.7_

  - [x] 1.2 Create `Runsheet-backend/services/import_models.py` with all Pydantic models for the import workflow
    - Define enums: `DataTypeEnum`, `FieldType`, `ImportStatus`
    - Define models: `ParseResult`, `ValidationIssue`, `ValidationResult`, `ImportResult`, `ImportSessionRecord`
    - _Requirements: 6.3, 6.4, 7.4, 8.2_

- [ ] 2. Backend validation engine and field mapper
  - [x] 2.1 Create `Runsheet-backend/services/validation_engine.py` with `ValidationEngine` class
    - Implement `validate_rows(rows, data_type, field_mapping)` method
    - Apply field mapping to transform source column names to target field names
    - Check required field presence for each row
    - Check data type correctness: string (any), number (parseable float/int), date (ISO8601 or common formats), enum (value in allowed list), boolean, geo_point
    - Return `ValidationResult` with per-row errors and warnings, counts, and row-level detail
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 2.2 Write property test for validation type and presence detection (Property 7)
    - **Property 7: Validation detects all type and presence violations**
    - **Validates: Requirements 6.1, 6.2**
    - File: `Runsheet-backend/tests/property/test_import_validation_property.py`

  - [ ]* 2.3 Write property test for preview report count consistency (Property 8)
    - **Property 8: Preview report count consistency**
    - **Validates: Requirements 6.3**
    - File: `Runsheet-backend/tests/property/test_import_validation_property.py`

  - [ ]* 2.4 Write property test for validation issue display completeness (Property 9)
    - **Property 9: Validation issue display completeness**
    - **Validates: Requirements 6.4, 6.5**
    - File: `Runsheet-backend/tests/property/test_import_validation_property.py`

  - [x] 2.5 Create `Runsheet-backend/services/field_mapper.py` with `FieldMapper` class
    - Implement `auto_map(source_columns, data_type)` method
    - Normalization algorithm: lowercase, replace spaces/hyphens with underscores, strip whitespace
    - Exact match after normalization → map
    - Substring containment (target in source or source in target) → map
    - Unmapped columns get no suggestion
    - Implement `validate_mapping(field_mapping, data_type)` to check for duplicate target mappings and unmapped required fields
    - _Requirements: 5.2, 5.4, 5.6_

  - [ ]* 2.6 Write property test for auto-suggest field mapping normalization (Property 4)
    - **Property 4: Auto-suggest field mapping normalization**
    - **Validates: Requirements 5.2**
    - File: `Runsheet-backend/tests/property/test_import_field_mapper_property.py`

  - [ ]* 2.7 Write property test for unmapped required field detection (Property 5)
    - **Property 5: Unmapped required field detection**
    - **Validates: Requirements 5.4**
    - File: `Runsheet-backend/tests/property/test_import_field_mapper_property.py`

  - [ ]* 2.8 Write property test for duplicate target mapping prevention (Property 6)
    - **Property 6: Duplicate target mapping prevention**
    - **Validates: Requirements 5.6**
    - File: `Runsheet-backend/tests/property/test_import_field_mapper_property.py`

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Backend ImportService and CSV/Sheets parsing
  - [x] 4.1 Create `Runsheet-backend/services/import_service.py` with `ImportService` class
    - Constructor takes `ElasticsearchService`, initializes `SchemaTemplates`, `ValidationEngine`, `FieldMapper`, and `_active_sessions` dict
    - Implement `parse_csv(file_content, data_type)`: parse CSV bytes, extract headers, first 5 sample rows, total row count, call `FieldMapper.auto_map()` for suggested mapping, create session in `_active_sessions`, return `ParseResult`
    - Implement `parse_sheets(url, data_type)`: fetch Google Sheets data via public CSV export URL, parse like CSV, create session, return `ParseResult`
    - Implement `validate(session_id, field_mapping)`: retrieve session, run `ValidationEngine.validate_rows()`, store result in session, return `ValidationResult`
    - Implement `commit(session_id, skip_errors)`: retrieve session, filter rows based on skip_errors flag, bulk index via `es_service.bulk_index_documents()`, persist `ImportSessionRecord` to `import_sessions` index, return `ImportResult`
    - Implement `get_history(data_type, status)`: query `import_sessions` ES index with optional filters, return sorted by `created_at` descending
    - Implement `get_session(session_id)`: fetch single session from ES
    - Implement `generate_template(data_type)`: delegate to `SchemaTemplates.generate_csv_template()`
    - _Requirements: 3.2, 3.5, 4.2, 4.4, 5.2, 6.1, 7.1, 7.4, 7.5, 8.1, 8.2, 8.3, 9.1_

  - [ ]* 4.2 Write property test for CSV header parsing (Property 2)
    - **Property 2: CSV header parsing preserves column names**
    - **Validates: Requirements 3.2**
    - File: `Runsheet-backend/tests/property/test_import_csv_parsing_property.py`

  - [ ]* 4.3 Write property test for source data preview correctness (Property 3)
    - **Property 3: Source data preview correctness**
    - **Validates: Requirements 3.5, 4.4**
    - File: `Runsheet-backend/tests/property/test_import_csv_parsing_property.py`

- [x] 5. Backend import_sessions ES index setup
  - [x] 5.1 Add `import_sessions` index mapping to `Runsheet-backend/services/elasticsearch_service.py`
    - Add the `import_sessions` mapping to the index creation logic (following the existing pattern for other indices)
    - Fields: session_id (keyword), data_type (keyword), source_type (keyword), source_name (text + keyword), total_records (integer), imported_records (integer), skipped_records (integer), error_count (integer), status (keyword), errors (text), field_mapping (object, enabled: false), created_at (date), completed_at (date), duration_seconds (float)
    - _Requirements: 8.1, 8.2_

- [x] 6. Backend API endpoints router
  - [x] 6.1 Create `Runsheet-backend/import_endpoints.py` with FastAPI router mounted at `/api/import`
    - `POST /api/import/upload/csv` — Accept `UploadFile` + `data_type` form field, validate file size (≤10MB) and extension (.csv), call `ImportService.parse_csv()`, return `ParseResult`
    - `POST /api/import/upload/sheets` — Accept JSON body with `url` and `data_type`, call `ImportService.parse_sheets()`, return `ParseResult`
    - `POST /api/import/validate` — Accept JSON body with `session_id` and `field_mapping`, call `ImportService.validate()`, return `ValidationResult`
    - `POST /api/import/commit` — Accept JSON body with `session_id` and `skip_errors`, call `ImportService.commit()`, return `ImportResult`
    - `GET /api/import/history` — Accept optional query params `data_type` and `status`, call `ImportService.get_history()`, return list of `ImportSessionRecord`
    - `GET /api/import/history/{session_id}` — Call `ImportService.get_session()`, return `ImportSessionRecord`
    - `GET /api/import/templates/{data_type}` — Call `ImportService.generate_template()`, return CSV as `StreamingResponse` with content-disposition header
    - `GET /api/import/schemas/{data_type}` — Call `SchemaTemplates.get_template()`, return `SchemaTemplate` JSON
    - _Requirements: 3.3, 3.4, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [x] 6.2 Register the import router in `Runsheet-backend/main.py`
    - Import `from import_endpoints import router as import_router`
    - Add `app.include_router(import_router)` alongside existing routers
    - _Requirements: 11.1_

- [x] 7. Checkpoint — Ensure all backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Backend schema template property tests
  - [ ]* 8.1 Write property test for data type metadata completeness (Property 1)
    - **Property 1: Data type metadata completeness**
    - **Validates: Requirements 2.2, 2.3**
    - File: `Runsheet-backend/tests/property/test_import_schema_templates_property.py`

  - [ ]* 8.2 Write property test for schema template CSV generation round-trip (Property 12)
    - **Property 12: Schema template CSV generation round-trip**
    - **Validates: Requirements 9.1, 9.2, 9.3**
    - File: `Runsheet-backend/tests/property/test_import_schema_templates_property.py`

- [ ] 9. Backend import session property tests
  - [ ]* 9.1 Write property test for import session record completeness (Property 10)
    - **Property 10: Import session record completeness**
    - **Validates: Requirements 8.2**
    - File: `Runsheet-backend/tests/property/test_import_session_property.py`

  - [ ]* 9.2 Write property test for import history chronological ordering (Property 11)
    - **Property 11: Import history chronological ordering**
    - **Validates: Requirements 8.3**
    - File: `Runsheet-backend/tests/property/test_import_session_property.py`

- [x] 10. Frontend TypeScript types
  - [x] 10.1 Create `runsheet/src/types/import.ts` with all TypeScript types
    - Define types: `DataType`, `ImportStatus`, `FieldDef`, `SchemaTemplate`, `ParseResponse`, `ValidationIssue`, `ValidationResult`, `ImportResult`, `ImportSessionRecord`
    - Match the backend Pydantic models exactly
    - _Requirements: 2.1, 6.3, 6.4, 7.4, 8.2_

- [x] 11. Frontend importApi service
  - [x] 11.1 Create `runsheet/src/services/importApi.ts` with API service module
    - Follow the existing `apiService` pattern in `runsheet/src/services/api.ts`
    - Implement: `uploadCSV(file, dataType)`, `uploadSheets(url, dataType)`, `validate(sessionId, fieldMapping)`, `commit(sessionId, skipErrors)`, `getHistory(filters?)`, `getSession(sessionId)`, `getSchema(dataType)`, `downloadTemplate(dataType)`
    - Use `API_BASE_URL` from environment, handle errors consistently
    - _Requirements: 11.1–11.6, 9.4_

- [x] 12. Frontend DataImport wizard component
  - [x] 12.1 Create `runsheet/src/components/DataImport.tsx` — main multi-step wizard component
    - Manage `ImportWorkflowState` with steps: select-type → upload → map-fields → validate → commit → complete
    - Render step indicator/breadcrumb showing current position in the workflow
    - Render the active sub-component for the current step
    - Include an "Import History" tab/button accessible from any step
    - Remove all demo-specific UI: no time period selection, no batch ID, no "Reset Demo", no temporal settings
    - Page header: "Data Import" with description referencing data migration and onboarding
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 12.2 Create `runsheet/src/components/import/DataTypeSelector.tsx` sub-component
    - Card grid layout for 7 data types (fleet, orders, riders, fuel_stations, inventory, support_tickets, jobs)
    - Each card shows: type name, description, ES index name, required field count, optional field count
    - Fetch schema via `importApi.getSchema()` on mount
    - Include "Download Template" button per card calling `importApi.downloadTemplate()`
    - Require exactly one selection before proceeding
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 9.1, 9.4_

  - [x] 12.3 Create `runsheet/src/components/import/SourceUploader.tsx` sub-component
    - Tabbed interface: CSV upload tab and Google Sheets tab
    - CSV: drag-and-drop zone + file browser, client-side 10MB size check, .csv extension check
    - Google Sheets: URL input field with submit button
    - On successful parse: display detected column names and 5-row sample preview table
    - Error display for parse failures with "Try Again" action
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 4.4_

  - [x] 12.4 Create `runsheet/src/components/import/FieldMapper.tsx` sub-component
    - Two-column layout: source columns on left, target field dropdowns on right
    - Auto-populate with `suggested_mapping` from parse response
    - Visually distinguish required vs optional target fields (e.g., asterisk, color)
    - Show warning badges for unmapped required fields
    - Allow manual override of any mapping
    - Prevent duplicate target field selection (disable already-mapped targets in dropdowns)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 12.5 Create `runsheet/src/components/import/ValidationPreview.tsx` sub-component
    - Display Preview_Report: total rows, valid rows, warning count, error count
    - Scrollable table of errors: row number, field name, description, value
    - Scrollable table of warnings: row number, field name, description
    - Action buttons: "Import Valid Rows" (enabled when valid_rows > 0), "Cancel" (go back to fix source)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 12.6 Create `runsheet/src/components/import/ImportProgress.tsx` sub-component
    - Progress bar showing records processed / total records
    - Status label cycling through: processing → indexing → completing
    - Animated spinner during active import
    - Error display if import fails mid-execution, showing count of records imported before failure
    - _Requirements: 7.1, 7.2, 7.3, 7.5_

  - [x] 12.7 Create `runsheet/src/components/import/ImportComplete.tsx` sub-component
    - Summary card: total imported, skipped, data type, duration
    - "Start New Import" button to reset wizard to step 1
    - _Requirements: 7.4_

  - [x] 12.8 Create `runsheet/src/components/import/ImportHistory.tsx` sub-component
    - Table listing past import sessions in reverse chronological order
    - Columns: date, data type, source type, source name, total records, imported, status
    - Filter controls: data type dropdown, status dropdown
    - Expandable row detail showing full session info including errors
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [x] 13. Sidebar and dashboard routing updates
  - [x] 13.1 Update `runsheet/src/components/Sidebar.tsx`
    - Change the menu item with `id: "upload-data"` label from "Upload Data" to "Data Import"
    - Change the icon from `Upload` to `FileInput` (import from lucide-react)
    - Keep `id: "upload-data"` unchanged for backward compatibility
    - _Requirements: 12.1, 12.2_

  - [x] 13.2 Update `runsheet/src/app/dashboard/page.tsx`
    - Change the lazy import from `DataUpload` to `DataImport`: `const DataImport = lazy(() => import("../../components/DataImport"));`
    - Update the `case "upload-data"` block to render `<DataImport />` instead of `<DataUpload />`
    - _Requirements: 1.1, 1.5, 12.2_

- [x] 14. Checkpoint — Ensure all tests pass and full workflow works end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Integration tests
  - [x]* 15.1 Write integration tests for import API endpoints
    - Test `POST /api/import/upload/csv` with valid CSV, oversized file, non-CSV file
    - Test `POST /api/import/upload/sheets` with valid and invalid URLs
    - Test `POST /api/import/validate` with valid and invalid session/mapping
    - Test `POST /api/import/commit` with valid session, skip_errors flag
    - Test `GET /api/import/history` with and without filters
    - Test `GET /api/import/history/{session_id}` with valid and invalid IDs
    - Test `GET /api/import/templates/{data_type}` for each data type
    - Test `GET /api/import/schemas/{data_type}` for each data type
    - File: `Runsheet-backend/tests/integration/test_import_endpoints.py`
    - _Requirements: 11.1–11.6, 3.3, 3.4, 10.1–10.7_

- [x] 16. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (12 properties total)
- Unit tests validate specific examples and edge cases
- Backend is implemented first so the frontend can integrate against real endpoints
- The existing `DataUpload.tsx` is not deleted — it is replaced by `DataImport.tsx` in the routing
