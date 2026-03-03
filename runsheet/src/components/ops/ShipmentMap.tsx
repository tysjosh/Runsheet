'use client';

import React from 'react';
import { MapPin } from 'lucide-react';
import type { OpsEvent, GeoPoint } from '../../services/opsApi';

interface MapMarker {
  label: string;
  location: GeoPoint;
  eventType: string;
  timestamp: string;
}

interface ShipmentMapProps {
  events: OpsEvent[];
}

/**
 * ShipmentMap renders a simple visual representation of event locations.
 *
 * Displays location coordinates as styled markers in a container.
 * This is a placeholder implementation — no external map library is used.
 *
 * Validates: Requirement 15.4
 */
export default function ShipmentMap({ events }: ShipmentMapProps) {
  const markers: MapMarker[] = events
    .filter((e): e is OpsEvent & { location: GeoPoint } => !!e.location)
    .sort(
      (a, b) =>
        new Date(a.event_timestamp).getTime() - new Date(b.event_timestamp).getTime()
    )
    .map((e, idx) => ({
      label: `${idx + 1}`,
      location: e.location,
      eventType: e.event_type,
      timestamp: e.event_timestamp,
    }));

  if (markers.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 bg-gray-50 rounded-lg border border-gray-200 text-gray-400 text-sm">
        No location data available
      </div>
    );
  }

  return (
    <div className="bg-gray-50 rounded-lg border border-gray-200 p-4">
      <h3 className="text-sm font-medium text-gray-700 mb-3 flex items-center gap-1.5">
        <MapPin className="w-4 h-4" />
        Event Locations
      </h3>

      <div className="space-y-2">
        {markers.map((marker, idx) => (
          <div
            key={`${marker.timestamp}-${idx}`}
            className="flex items-center gap-3 bg-white rounded-md px-3 py-2 border border-gray-100"
          >
            {/* Numbered marker */}
            <div className="w-6 h-6 rounded-full bg-[#232323] text-white text-xs font-medium flex items-center justify-center flex-shrink-0">
              {marker.label}
            </div>

            {/* Coordinates */}
            <div className="flex-1 min-w-0">
              <span className="text-sm font-mono text-gray-800">
                {marker.location.lat.toFixed(4)}, {marker.location.lon.toFixed(4)}
              </span>
              <span className="ml-2 text-xs text-gray-400">
                {marker.eventType.replace(/_/g, ' ')}
              </span>
            </div>

            {/* Timestamp */}
            <span className="text-xs text-gray-400 flex-shrink-0">
              {new Date(marker.timestamp).toLocaleTimeString()}
            </span>
          </div>
        ))}
      </div>

      {/* Route summary */}
      {markers.length >= 2 && (
        <div className="mt-3 pt-3 border-t border-gray-200 text-xs text-gray-500">
          {markers.length} location points tracked
        </div>
      )}
    </div>
  );
}
