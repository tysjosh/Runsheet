import type { OpsShipment } from "../../../services/opsApi";

/**
 * Escape a single CSV field value.
 * If the value contains a comma, double-quote, or newline, wrap it in
 * double-quotes and escape any internal double-quotes by doubling them.
 */
function escapeCSVField(value: string): string {
  if (
    value.includes(",") ||
    value.includes('"') ||
    value.includes("\n") ||
    value.includes("\r")
  ) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

/**
 * Generate a CSV string from an array of failure shipment objects.
 *
 * Columns: shipment_id, failure_reason, rider_id, origin, destination, timestamp
 *
 * Validates: Requirements 9.4, 9.5
 */
export function generateFailureCSV(shipments: OpsShipment[]): string {
  const headers = [
    "shipment_id",
    "failure_reason",
    "rider_id",
    "origin",
    "destination",
    "timestamp",
  ];

  const rows = shipments.map((s) =>
    [
      escapeCSVField(s.shipment_id ?? ""),
      escapeCSVField(s.failure_reason ?? ""),
      escapeCSVField(s.rider_id ?? ""),
      escapeCSVField(s.origin ?? ""),
      escapeCSVField(s.destination ?? ""),
      escapeCSVField(s.updated_at ?? ""),
    ].join(","),
  );

  return [headers.join(","), ...rows].join("\n");
}
