// Data Import / Migration Types

export type DataType = 'fleet' | 'orders' | 'riders' | 'fuel_stations' | 'inventory' | 'support_tickets' | 'jobs';

export type ImportStatus = 'parsing' | 'mapped' | 'validating' | 'validated' | 'importing' | 'completed' | 'partial' | 'failed';

export interface FieldDef {
  name: string;
  type: 'string' | 'number' | 'date' | 'enum' | 'boolean' | 'geo_point';
  required: boolean;
  description: string;
  enum_values?: string[];
}

export interface SchemaTemplate {
  data_type: string;
  description: string;
  es_index: string;
  fields: FieldDef[];
}

export interface ParseResponse {
  session_id: string;
  columns: string[];
  sample_rows: Record<string, string>[];
  total_rows: number;
  suggested_mapping: Record<string, string>;
}

export interface ValidationIssue {
  row_number: number;
  field_name: string;
  description: string;
  value?: string;
}

export interface ValidationResult {
  session_id: string;
  total_rows: number;
  valid_rows: number;
  error_count: number;
  warning_count: number;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
}

export interface ImportResult {
  session_id: string;
  status: ImportStatus;
  total_records: number;
  imported_records: number;
  skipped_records: number;
  error_count: number;
  errors: string[];
  data_type: string;
  es_index: string;
  duration_seconds: number;
}

export interface ImportSessionRecord {
  session_id: string;
  data_type: string;
  source_type: 'csv' | 'google_sheets';
  source_name: string;
  total_records: number;
  imported_records: number;
  skipped_records: number;
  error_count: number;
  status: ImportStatus;
  errors: string[];
  created_at: string;
  completed_at?: string;
  duration_seconds?: number;
}
