"use client";

import type { MetricsBucketEntry } from "../../services/opsApi";

interface FailureTrendChartProps {
  /** Time-bucketed failure counts */
  data: MetricsBucketEntry[];
}

/**
 * Trend chart showing failure counts over time using CSS/HTML bars.
 * Renders a simple vertical bar chart with time labels on the x-axis.
 *
 * Validates: Requirement 14.2
 */
export default function FailureTrendChart({ data }: FailureTrendChartProps) {
  if (data.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400 text-sm">
        No trend data for the selected time range
      </div>
    );
  }

  const maxCount = Math.max(...data.map((d) => d.count), 1);

  const formatLabel = (timestamp: string): string => {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    // Show short date/time depending on bucket granularity
    const now = new Date();
    const diffDays = (now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24);
    if (diffDays <= 2) {
      return date.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">Failure Trend</h3>

      {/* Y-axis max label */}
      <div className="flex items-end gap-2 text-xs text-gray-400">
        <span className="w-8 text-right">{maxCount}</span>
        <div className="flex-1 border-b border-dashed border-gray-200" />
      </div>

      {/* Bars */}
      <div
        className="flex items-end gap-1 overflow-x-auto pb-1"
        style={{ minHeight: 120 }}
        role="img"
        aria-label="Failure trend chart"
      >
        {data.map((bucket, i) => {
          const heightPct = maxCount > 0 ? (bucket.count / maxCount) * 100 : 0;
          return (
            <div
              key={`${bucket.timestamp}-${bucket.count}`}
              className="flex flex-col items-center flex-1 min-w-[24px] max-w-[48px]"
            >
              <span className="text-[10px] text-gray-500 mb-1">
                {bucket.count > 0 ? bucket.count : ""}
              </span>
              <div
                className="w-full bg-red-400 rounded-t transition-all hover:bg-red-500"
                style={{
                  height: `${Math.max(heightPct, bucket.count > 0 ? 4 : 0)}px`,
                  maxHeight: 100,
                }}
                title={`${formatLabel(bucket.timestamp)}: ${bucket.count} failures`}
              />
            </div>
          );
        })}
      </div>

      {/* X-axis labels */}
      <div className="flex gap-1 overflow-x-auto">
        {data.map((bucket, i) => (
          <div
            key={`${bucket.timestamp + i}-label`}
            className="flex-1 min-w-[24px] max-w-[48px] text-center"
          >
            <span className="text-[10px] text-gray-400 truncate block">
              {formatLabel(bucket.timestamp)}
            </span>
          </div>
        ))}
      </div>

      {/* Zero line */}
      <div className="flex items-center gap-2 text-xs text-gray-400">
        <span className="w-8 text-right">0</span>
        <div className="flex-1 border-b border-gray-200" />
      </div>
    </div>
  );
}
