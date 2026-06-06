import type {
  ImportResult,
  ImportSessionRecord,
  ParseResponse,
  SchemaTemplate,
  ValidationResult,
} from "../types/import";
import { ApiError, ApiTimeoutError, fetchWithSession } from "./api";
// `importApi` previously declared a narrower `buildQueryString`
// (`Record<string, string | undefined>`); the shared signature is a superset,
// so it adopts the shared helper with no behavior change (Req 2.5/4.3).
import { buildQueryString, fetchWithTimeout } from "./utils";

// ─── Configuration ───────────────────────────────────────────────────────────

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// ─── HTTP Helpers ────────────────────────────────────────────────────────────

async function importRequest<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options?.headers as Record<string, string> | undefined),
    };

    // Session cookie + anti-CSRF token are attached by the SuperTokens SDK;
    // an auth failure triggers a refresh-then-retry, else a redirect to
    // sign-in (Req 8.4, 8.5).
    const response = await fetchWithSession(fetchWithTimeout, url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(
        body.detail || body.message || `HTTP error! status: ${response.status}`,
        response.status,
      );
    }

    return await response.json();
  } catch (error) {
    if (error instanceof ApiTimeoutError || error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Unknown error",
      0,
    );
  }
}

// ─── Import API Service ──────────────────────────────────────────────────────

export const importApi = {
  /**
   * Upload a CSV file for parsing.
   * POST /import/upload/csv — multipart form data
   */
  async uploadCSV(file: File, dataType: string): Promise<ParseResponse> {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("data_type", dataType);

    const url = `${API_BASE_URL}/import/upload/csv`;
    try {
      const response = await fetchWithSession(fetchWithTimeout, url, {
        method: "POST",
        body: formData,
        // Do not set Content-Type — the browser sets it with the boundary for multipart
      });

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new ApiError(
          body.detail ||
            body.message ||
            `HTTP error! status: ${response.status}`,
          response.status,
        );
      }

      return await response.json();
    } catch (error) {
      if (error instanceof ApiTimeoutError || error instanceof ApiError) {
        throw error;
      }
      throw new ApiError(
        error instanceof Error ? error.message : "Unknown error",
        0,
      );
    }
  },

  /**
   * Import data from a Google Sheets URL.
   * POST /import/upload/sheets — JSON body
   */
  async uploadSheets(url: string, dataType: string): Promise<ParseResponse> {
    return importRequest<ParseResponse>("/import/upload/sheets", {
      method: "POST",
      body: JSON.stringify({ url, data_type: dataType }),
    });
  },

  /**
   * Validate mapped data against the schema.
   * POST /import/validate — JSON body
   */
  async validate(
    sessionId: string,
    fieldMapping: Record<string, string>,
  ): Promise<ValidationResult> {
    return importRequest<ValidationResult>("/import/validate", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        field_mapping: fieldMapping,
      }),
    });
  },

  /**
   * Commit validated records to Elasticsearch.
   * POST /import/commit — JSON body
   */
  async commit(sessionId: string, skipErrors: boolean): Promise<ImportResult> {
    return importRequest<ImportResult>("/import/commit", {
      method: "POST",
      body: JSON.stringify({
        session_id: sessionId,
        skip_errors: skipErrors,
      }),
    });
  },

  /**
   * Retrieve import history with optional filters.
   * GET /import/history
   */
  async getHistory(filters?: {
    dataType?: string;
    status?: string;
  }): Promise<ImportSessionRecord[]> {
    const qs = buildQueryString({
      data_type: filters?.dataType,
      status: filters?.status,
    });
    return importRequest<ImportSessionRecord[]>(`/import/history${qs}`);
  },

  /**
   * Retrieve a single import session by ID.
   * GET /import/history/:sessionId
   */
  async getSession(sessionId: string): Promise<ImportSessionRecord> {
    return importRequest<ImportSessionRecord>(
      `/import/history/${encodeURIComponent(sessionId)}`,
    );
  },

  /**
   * Get the schema template for a data type.
   * GET /import/schemas/:dataType
   */
  async getSchema(dataType: string): Promise<SchemaTemplate> {
    return importRequest<SchemaTemplate>(
      `/import/schemas/${encodeURIComponent(dataType)}`,
    );
  },

  /**
   * Trigger a browser download of the CSV template for a data type.
   * Opens the template endpoint URL directly so the browser handles the download.
   */
  downloadTemplate(dataType: string): void {
    const url = `${API_BASE_URL}/import/templates/${encodeURIComponent(dataType)}`;
    window.open(url, "_blank");
  },
};
