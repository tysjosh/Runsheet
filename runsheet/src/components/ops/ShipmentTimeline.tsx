'use client';

import React from 'react';
import { Clock, MapPin, FileText, Tag } from 'lucide-react';
import type { OpsEvent } from '../../services/opsApi';

/**
 * Color and label mapping for event types.
 */
const EVENT_TYPE_STYLES: Record<string, { color: string; bg: string; label: string }> = {
  shipment_created: { color: 'text-blue-700', bg: 'bg-blue-100', label: 'Created' },
  shipment_updated: { color: 'text-indigo-700', bg: 'bg-indigo-100', label: 'Updated' },
  shipment_delivered: { color: 'text-green-700', bg: 'bg-green-100', label: 'Delivered' },
  shipment_failed: { color: 'text-red-700', bg: 'bg-red-100', label: 'Failed' },
  rider_assigned: { color: 'text-purple-700', bg: 'bg-purple-100', label: 'Rider Assigned' },
  rider_status_changed: { color: 'text-amber-700', bg: 'bg-amber-100', label: 'Rider Status' },
};

const DEFAULT_STYLE = { color: 'text-gray-700', bg: 'bg-gray-100', label: 'Event' };

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function formatLocation(location?: { lat: number; lon: number }): string | null {
  if (!location) return null;
  return `${location.lat.toFixed(4)}, ${location.lon.toFixed(4)}`;
}

interface ShipmentTimelineProps {
  events: OpsEvent[];
}

/**
 * ShipmentTimeline renders a vertical event timeline for a shipment.
 *
 * Each event shows event_type, timestamp, location (if available),
 * event details, and trace_id.
 *
 * Validates: Requirements 15.1, 15.2, 20.5
 */
export default function ShipmentTimeline({ events }: ShipmentTimelineProps) {
  if (events.length === 0) {
    return (
      <div className="text-center py-12 text-gray-500">
        No events recorded for this shipment.
      </div>
    );
  }

  // Sort events by timestamp descending (newest first)
  const sorted = [...events].sort(
    (a, b) => new Date(b.event_timestamp).getTime() - new Date(a.event_timestamp).getTime()
  );

  return (
    <div className="relative">
      {/* Vertical line */}
      <div className="absolute left-5 top-0 bottom-0 w-0.5 bg-gray-200" aria-hidden="true" />

      <ol className="space-y-6" aria-label="Shipment event timeline">
        {sorted.map((event, idx) => {
          const style = EVENT_TYPE_STYLES[event.event_type] ?? DEFAULT_STYLE;
          const loc = formatLocation(event.location);
          const details = event.event_payload
            ? Object.entries(event.event_payload)
            : [];

          return (
            <li key={event.event_id} className="relative pl-12">
              {/* Dot on the timeline */}
              <div
                className={`absolute left-3.5 w-3 h-3 rounded-full border-2 border-white ${
                  idx === 0 ? 'bg-[#232323]' : 'bg-gray-400'
                }`}
                aria-hidden="true"
              />

              <div className="bg-white border border-gray-100 rounded-lg p-4 shadow-sm">
                {/* Header row */}
                <div className="flex items-center gap-2 flex-wrap mb-2">
                  <span
                    className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style.bg} ${style.color}`}
                  >
                    {style.label}
                  </span>

                  <span className="flex items-center gap-1 text-xs text-gray-500">
                    <Clock className="w-3 h-3" />
                    {formatTimestamp(event.event_timestamp)}
                  </span>

                  {loc && (
                    <span className="flex items-center gap-1 text-xs text-gray-500">
                      <MapPin className="w-3 h-3" />
                      {loc}
                    </span>
                  )}
                </div>

                {/* Event details */}
                {details.length > 0 && (
                  <div className="flex items-start gap-1 text-sm text-gray-700 mb-2">
                    <FileText className="w-3.5 h-3.5 mt-0.5 text-gray-400 flex-shrink-0" />
                    <span className="break-all">
                      {details.map(([k, v]) => `${k}: ${String(v)}`).join(', ')}
                    </span>
                  </div>
                )}

                {/* Trace ID */}
                {event.trace_id && (
                  <div className="flex items-center gap-1 text-xs text-gray-400">
                    <Tag className="w-3 h-3" />
                    <span className="font-mono">{event.trace_id}</span>
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
