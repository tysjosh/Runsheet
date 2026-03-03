'use client';

import React from 'react';
import type { MetricsBucketEntry } from '../../services/opsApi';

interface FailureBarChartProps {
  /** Metrics data with breakdown by failure reason */
  data: MetricsBucketEntry[];
  /** Currently selected failure reason filter (empty = none) */
  selectedReason: string;
  /** Callback when a failure reason bar is clicked */
  onReasonClick: (reason: string) => void;
}

/**
 * Bar chart showing failure counts grouped by failure reason.
 * Uses CSS/HTML-based bars (no external chart library).
 * Click a bar to filter the failed shipments table.
 *
 * Validates: Requirements 14.1, 14.5
 */
export default function FailureBarChart({
  data,
  selectedReason,
  onReasonClick,
}: FailureBarChartProps) {
  // Aggregate breakdowns across all time buckets into totals per reason
  const reasonTotals = new Map<string, number>();
  for (const bucket of data) {
    if (bucket.breakdown) {
      for (const [reason, count] of Object.entries(bucket.breakdown)) {
        reasonTotals.set(reason, (reasonTotals.get(reason) ?? 0) + count);
      }
    }
  }

  const entries = Array.from(reasonTotals.entries())
    .sort((a, b) => b[1] - a[1]);

  const maxCount = entries.length > 0 ? Math.max(...entries.map(([, c]) => c)) : 0;

  if (entries.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400 text-sm">
        No failure data for the selected time range
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">Failures by Reason</h3>
      <div className="space-y-2">
        {entries.map(([reason, count]) => {
          const pct = maxCount > 0 ? (count / maxCount) * 100 : 0;
          const isSelected = selectedReason === reason;

          return (
            <button
              key={reason}
              type="button"
              onClick={() => onReasonClick(isSelected ? '' : reason)}
              className={`w-full text-left group rounded-lg p-2 transition-colors ${
                isSelected
                  ? 'bg-red-50 ring-1 ring-red-200'
                  : 'hover:bg-gray-50'
              }`}
              aria-label={`${reason}: ${count} failures${isSelected ? ' (selected)' : ''}`}
              aria-pressed={isSelected}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm text-gray-700 truncate max-w-[70%]">
                  {reason}
                </span>
                <span className="text-sm font-medium text-gray-900">{count}</span>
              </div>
              <div className="w-full bg-gray-100 rounded-full h-2">
                <div
                  className={`h-2 rounded-full transition-all ${
                    isSelected ? 'bg-red-500' : 'bg-red-400 group-hover:bg-red-500'
                  }`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
