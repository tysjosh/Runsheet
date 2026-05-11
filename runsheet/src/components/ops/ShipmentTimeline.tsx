"use client";

import { CheckCircle, Clock, MapPin } from "lucide-react";
import type { OpsEvent } from "../../services/opsApi";

interface ShipmentTimelineProps {
  events: OpsEvent[];
}

function formatDateTime(value: string): string {
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function formatEventType(value: string): string {
  return value.replace(/_/g, " ");
}

export default function ShipmentTimeline({ events }: ShipmentTimelineProps) {
  const sortedEvents = [...events].sort(
    (a, b) =>
      new Date(a.event_timestamp).getTime() -
      new Date(b.event_timestamp).getTime(),
  );

  if (sortedEvents.length === 0) {
    return (
      <div className="rounded-xl border border-gray-100 bg-gray-50 px-4 py-8 text-center">
        <Clock className="mx-auto h-6 w-6 text-gray-400" aria-hidden="true" />
        <p className="mt-2 text-sm font-medium text-primary">
          No shipment events yet
        </p>
        <p className="mt-1 text-xs text-gray-500">
          Events will appear here as the shipment progresses.
        </p>
      </div>
    );
  }

  return (
    <ol className="space-y-0">
      {sortedEvents.map((event, index) => {
        const isLast = index === sortedEvents.length - 1;

        return (
          <li key={event.event_id} className="relative flex gap-3 pb-5">
            {!isLast && (
              <span
                className="absolute left-4 top-8 h-[calc(100%-2rem)] w-px bg-gray-200"
                aria-hidden="true"
              />
            )}
            <span className="relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary-soft text-primary">
              <CheckCircle className="h-4 w-4" aria-hidden="true" />
            </span>

            <div className="min-w-0 flex-1 rounded-xl border border-gray-100 bg-white px-4 py-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold capitalize text-primary">
                    {formatEventType(event.event_type)}
                  </p>
                  <p className="mt-0.5 text-xs text-gray-500">
                    {formatDateTime(event.event_timestamp)}
                  </p>
                </div>
                {event.trace_id && (
                  <span className="rounded bg-gray-100 px-2 py-0.5 font-mono text-[10px] text-gray-500">
                    {event.trace_id}
                  </span>
                )}
              </div>

              {event.location && (
                <p className="mt-3 flex items-center gap-1.5 text-xs text-gray-600">
                  <MapPin
                    className="h-3.5 w-3.5 text-info"
                    aria-hidden="true"
                  />
                  {event.location.lat.toFixed(4)},{" "}
                  {event.location.lon.toFixed(4)}
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
