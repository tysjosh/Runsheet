/**
 * Unit tests for the bulk-create addition to ``ordersApi.ts``.
 *
 * Covers ``POST /api/orders/bulk`` ({@link createOrdersBulk}), including
 * the dry-run path and the fact that this route returns the
 * ``BulkOrderResponse`` payload directly rather than the usual
 * ``{ data, request_id }`` envelope.
 *
 * We mock ``global.fetch`` to verify URL assembly, HTTP method, and JSON
 * body handling without a real HTTP client.
 */

import { ApiError } from "./api";
import {
  type BulkOrderRequest,
  type BulkOrderResponse,
  createOrdersBulk,
} from "./ordersApi";

const API_BASE_URL = "http://localhost:8080/api";

function mockFetchOnce(response: {
  ok: boolean;
  status?: number;
  body?: unknown;
}) {
  const jsonBody = response.body ?? {};
  global.fetch = jest.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status ?? (response.ok ? 200 : 500),
    json: async () => jsonBody,
  }) as unknown as typeof fetch;
}

afterEach(() => {
  jest.restoreAllMocks();
});

function bulkRequestFixture(dryRun = false): BulkOrderRequest {
  return {
    dry_run: dryRun,
    orders: [
      {
        customer_id: "cust-1",
        customer_name: "Acme Fuel",
        ship_to_address: "1 Depot Rd",
        ship_to_lat: 30.1,
        ship_to_lon: -97.7,
        product_code: "DIESEL_2",
        call_type: "will_call",
      },
    ],
  };
}

describe("createOrdersBulk", () => {
  it("POSTs the rows and returns the BulkOrderResponse directly", async () => {
    const response: BulkOrderResponse = {
      total: 1,
      processed: 1,
      duplicates: 0,
      errors: 0,
      dry_run: false,
      results: [
        {
          row_index: 0,
          order_id: "ord-1",
          event_id: "evt-1",
          status: "created",
        },
      ],
    };
    mockFetchOnce({ ok: true, body: response });

    const payload = bulkRequestFixture();
    const result = await createOrdersBulk(payload);

    // No envelope unwrap — the payload is returned as-is.
    expect(result).toEqual(response);
    const [url, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/orders/bulk`);
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual(payload);
  });

  it("propagates dry_run and reports per-row validation status", async () => {
    const response: BulkOrderResponse = {
      total: 1,
      processed: 0,
      duplicates: 0,
      errors: 0,
      dry_run: true,
      results: [{ row_index: 0, status: "valid" }],
    };
    mockFetchOnce({ ok: true, body: response });

    const result = await createOrdersBulk(bulkRequestFixture(true));

    expect(result.dry_run).toBe(true);
    const [, options] = (global.fetch as jest.Mock).mock.calls[0];
    expect(JSON.parse(options.body).dry_run).toBe(true);
  });

  it("raises ApiError when the row cap is exceeded (400)", async () => {
    mockFetchOnce({
      ok: false,
      status: 400,
      body: { detail: "Bulk upload exceeds the maximum of 1000 rows" },
    });

    await expect(createOrdersBulk(bulkRequestFixture())).rejects.toThrow(
      ApiError,
    );
  });
});
