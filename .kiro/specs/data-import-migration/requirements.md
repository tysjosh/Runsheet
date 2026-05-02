# Requirements Document

## Introduction

Transform the existing Data Upload page in Runsheet from a demo simulation tool into a production-grade data import/migration tool. Logistics companies onboarding to Runsheet need to migrate historical data from spreadsheets, legacy TMS platforms, and manual records. This feature provides a structured import workflow with data validation, field mapping, progress tracking, and import history — replacing the current demo-oriented UI (time periods, batch IDs, "Reset Demo", temporal settings) while preserving the solid underlying CSV upload and Google Sheets import capabilities.

## Glossary

- **Import_Tool**: The refactored Data Import page component in the Runsheet frontend that replaces the existing DataUpload component
- **Import_Service**: The backend service responsible for receiving, validating, transforming, and indexing imported data into Elasticsearch
- **Field_Mapper**: The UI component that allows users to map columns from their source file to Runsheet's expected schema fields
- **Validation_Engine**: The backend module that inspects uploaded data for schema compliance, required fields, data type correctness, and referential integrity before committing
- **Import_Session**: A tracked unit of work representing a single import operation, from file upload through validation to commit, with associated metadata and status
- **Data_Type**: One of the supported import categories: Fleet, Orders, Riders, Fuel Stations, Inventory, Support Tickets, Jobs/Scheduling
- **Preview_Report**: A summary generated after validation showing record counts, field mapping results, warnings, and errors before the user commits the import
- **Import_History_Log**: A persistent record of all past import sessions, stored in Elasticsearch, queryable by the user
- **Source_File**: The CSV file or Google Sheets document provided by the user containing data to import
- **Schema_Template**: The expected field definitions for each Data_Type, used for validation and field mapping guidance

## Requirements

### Requirement 1: Remove Demo-Specific UI Elements

**User Story:** As a logistics operations manager, I want the data import page to present a professional client-facing interface, so that my team can use it for real data migration without confusion from demo controls.

#### Acceptance Criteria

1. THE Import_Tool SHALL NOT display time period selection controls (afternoon, evening, night)
2. THE Import_Tool SHALL NOT display batch ID or operational time configuration fields
3. THE Import_Tool SHALL NOT display a "Reset Demo" button or demo state indicator
4. THE Import_Tool SHALL NOT display temporal settings panels
5. THE Import_Tool SHALL display a page header titled "Data Import" with a description referencing data migration and onboarding

### Requirement 2: Data Type Selection

**User Story:** As a logistics operations manager, I want to select the type of data I am importing with clear descriptions, so that I understand what each import category does and can choose the correct one.

#### Acceptance Criteria

1. THE Import_Tool SHALL present the following Data_Types for selection: Fleet, Orders, Riders, Fuel Stations, Inventory, Support Tickets, Jobs/Scheduling
2. WHEN a Data_Type is selected, THE Import_Tool SHALL display a description of what records that Data_Type contains and which Elasticsearch index it maps to
3. WHEN a Data_Type is selected, THE Import_Tool SHALL display the required and optional fields for that Data_Type as defined by the Schema_Template
4. THE Import_Tool SHALL require exactly one Data_Type to be selected before allowing an upload to proceed

### Requirement 3: CSV File Upload

**User Story:** As a logistics operations manager, I want to upload CSV files containing my historical data, so that I can migrate records from spreadsheets and legacy exports.

#### Acceptance Criteria

1. THE Import_Tool SHALL accept CSV file uploads via drag-and-drop and file browser selection
2. WHEN a CSV file is uploaded, THE Import_Tool SHALL parse the file header row to extract column names
3. IF a file exceeding 10MB is uploaded, THEN THE Import_Tool SHALL reject the file and display a size limit error message
4. IF a non-CSV file is uploaded, THEN THE Import_Tool SHALL reject the file and display a format error message
5. WHEN a CSV file is successfully parsed, THE Import_Tool SHALL display the detected column names and a sample of the first 5 rows

### Requirement 4: Google Sheets Import

**User Story:** As a logistics operations manager, I want to import data directly from Google Sheets, so that I can pull in records maintained in shared spreadsheets without manual export steps.

#### Acceptance Criteria

1. THE Import_Tool SHALL accept a Google Sheets URL as an import source
2. WHEN a Google Sheets URL is submitted, THE Import_Service SHALL fetch the sheet data and extract column names and rows
3. IF the Google Sheets URL is invalid or the sheet is not accessible, THEN THE Import_Service SHALL return a descriptive error message
4. WHEN Google Sheets data is successfully fetched, THE Import_Tool SHALL display the detected column names and a sample of the first 5 rows

### Requirement 5: Field Mapping

**User Story:** As a logistics operations manager, I want to map columns from my source file to Runsheet's expected fields, so that my data imports correctly even when my column names differ from Runsheet's schema.

#### Acceptance Criteria

1. WHEN source columns are detected, THE Field_Mapper SHALL display each source column alongside a dropdown of target Runsheet fields for the selected Data_Type
2. THE Field_Mapper SHALL auto-suggest mappings when source column names match or closely resemble target field names (case-insensitive, underscore/space normalization)
3. THE Field_Mapper SHALL visually distinguish required target fields from optional target fields
4. IF a required target field has no source column mapped to it, THEN THE Field_Mapper SHALL display a warning indicating the unmapped required field
5. THE Field_Mapper SHALL allow the user to manually override any auto-suggested mapping
6. THE Field_Mapper SHALL prevent mapping two source columns to the same target field

### Requirement 6: Data Validation Preview

**User Story:** As a logistics operations manager, I want to preview validation results before committing an import, so that I can identify and fix errors in my data before it enters the system.

#### Acceptance Criteria

1. WHEN field mapping is confirmed, THE Validation_Engine SHALL validate all rows against the Schema_Template for the selected Data_Type
2. THE Validation_Engine SHALL check each row for: required field presence, data type correctness (string, number, date, enum), and value format compliance
3. WHEN validation completes, THE Import_Tool SHALL display a Preview_Report containing: total row count, valid row count, warning count, and error count
4. WHEN validation errors exist, THE Import_Tool SHALL display each error with the row number, field name, and a description of the violation
5. WHEN validation warnings exist, THE Import_Tool SHALL display each warning with the row number, field name, and a description of the concern
6. THE Import_Tool SHALL allow the user to choose to import only valid rows (skipping error rows) or to cancel and fix the source data

### Requirement 7: Import Execution with Progress Tracking

**User Story:** As a logistics operations manager, I want to see real-time progress during data import, so that I know how the operation is proceeding and how many records have been processed.

#### Acceptance Criteria

1. WHEN the user confirms the import, THE Import_Service SHALL index the validated records into the appropriate Elasticsearch index for the selected Data_Type
2. WHILE an import is in progress, THE Import_Tool SHALL display a progress indicator showing the number of records processed out of the total
3. WHILE an import is in progress, THE Import_Tool SHALL display the current status of the Import_Session (processing, indexing, completing)
4. WHEN the import completes successfully, THE Import_Tool SHALL display a summary with total records imported, records skipped, and the Data_Type
5. IF an error occurs during import execution, THEN THE Import_Service SHALL halt processing, record the error in the Import_Session, and THE Import_Tool SHALL display the error with the count of records successfully imported before the failure

### Requirement 8: Import History Log

**User Story:** As a logistics operations manager, I want to view a history of past imports, so that I can track what data has been loaded, when, and by whom.

#### Acceptance Criteria

1. THE Import_Service SHALL persist an Import_Session record in Elasticsearch for every completed or failed import operation
2. THE Import_Session record SHALL contain: session ID, Data_Type, source type (CSV or Google Sheets), source file name or URL, total records, imported records, skipped records, error count, status (completed, partial, failed), and timestamp
3. THE Import_Tool SHALL display an Import History section listing past Import_Sessions in reverse chronological order
4. THE Import_Tool SHALL allow filtering the Import_History_Log by Data_Type and status
5. WHEN an Import_Session entry is selected, THE Import_Tool SHALL display the full details of that session including any error messages

### Requirement 9: Schema Templates and Download

**User Story:** As a logistics operations manager, I want to download CSV templates for each data type, so that I can prepare my data in the correct format before importing.

#### Acceptance Criteria

1. THE Import_Tool SHALL provide a downloadable CSV template for each supported Data_Type
2. THE CSV template SHALL contain the correct column headers matching the Schema_Template for that Data_Type, with required fields clearly indicated
3. THE CSV template SHALL include 2-3 example rows demonstrating the expected data format and values
4. WHEN the user clicks a template download button, THE Import_Tool SHALL trigger a browser file download of the corresponding CSV template

### Requirement 10: Data Type to Index Mapping

**User Story:** As a logistics operations manager, I want imported data to be correctly routed to the right storage location, so that it appears in the correct sections of the Runsheet platform.

#### Acceptance Criteria

1. THE Import_Service SHALL map the Fleet Data_Type to the "trucks" Elasticsearch index
2. THE Import_Service SHALL map the Orders Data_Type to the "orders" Elasticsearch index
3. THE Import_Service SHALL map the Inventory Data_Type to the "inventory" Elasticsearch index
4. THE Import_Service SHALL map the Support Tickets Data_Type to the "support_tickets" Elasticsearch index
5. THE Import_Service SHALL map the Riders Data_Type to a "riders" Elasticsearch index
6. THE Import_Service SHALL map the Fuel Stations Data_Type to a "fuel_stations" Elasticsearch index
7. THE Import_Service SHALL map the Jobs/Scheduling Data_Type to a "jobs" Elasticsearch index

### Requirement 11: Backend Import API

**User Story:** As a frontend developer, I want well-defined backend API endpoints for the import workflow, so that the Import_Tool can orchestrate validation, preview, and commit operations.

#### Acceptance Criteria

1. THE Import_Service SHALL expose a POST endpoint for uploading a CSV file with a specified Data_Type that returns parsed column names and a row sample
2. THE Import_Service SHALL expose a POST endpoint for fetching Google Sheets data with a specified Data_Type that returns parsed column names and a row sample
3. THE Import_Service SHALL expose a POST endpoint for validating mapped data that accepts the field mapping and source data, and returns a Preview_Report
4. THE Import_Service SHALL expose a POST endpoint for committing a validated import that indexes records into Elasticsearch and returns progress updates
5. THE Import_Service SHALL expose a GET endpoint for retrieving Import_History_Log entries with optional Data_Type and status filters
6. THE Import_Service SHALL expose a GET endpoint for retrieving a single Import_Session by session ID

### Requirement 12: Sidebar Navigation Update

**User Story:** As a Runsheet user, I want the sidebar navigation to reflect the new "Data Import" label, so that the navigation is consistent with the tool's purpose.

#### Acceptance Criteria

1. THE Import_Tool SHALL update the sidebar menu item from "Upload Data" to "Data Import"
2. THE Import_Tool SHALL retain the same navigation route identifier for backward compatibility
