'use client';

import React from 'react';
import { Filter } from 'lucide-react';
import type { ShipmentStatus } from '../../services/opsApi';

export interface OpsFilterValues {
  status: ShipmentStatus | '';
  rider_id: string;
  start_date: string;
  end_date: string;
}

interface OpsFiltersProps {
  filters: OpsFilterValues;
  onChange: (filters: OpsFilterValues) => void;
}

const STATUS_OPTIONS: { value: ShipmentStatus | ''; label: string }[] = [
  { value: '', label: 'All Statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'in_transit', label: 'In Transit' },
  { value: 'delivered', label: 'Delivered' },
  { value: 'failed', label: 'Failed' },
  { value: 'returned', label: 'Returned' },
];

/**
 * Filter controls for the shipment status board.
 * Supports filtering by status, rider, and date range.
 *
 * Validates: Requirement 12.3
 */
export default function OpsFilters({ filters, onChange }: OpsFiltersProps) {
  const update = (patch: Partial<OpsFilterValues>) => {
    onChange({ ...filters, ...patch });
  };

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Filter className="w-4 h-4 text-gray-400" aria-hidden="true" />

      <select
        value={filters.status}
        onChange={(e) => update({ status: e.target.value as ShipmentStatus | '' })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 bg-white"
        aria-label="Filter by status"
      >
        {STATUS_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>

      <input
        type="text"
        placeholder="Rider ID"
        value={filters.rider_id}
        onChange={(e) => update({ rider_id: e.target.value })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300 w-36"
        aria-label="Filter by rider ID"
      />

      <input
        type="date"
        value={filters.start_date}
        onChange={(e) => update({ start_date: e.target.value })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
        aria-label="Start date"
      />

      <span className="text-gray-400 text-sm">to</span>

      <input
        type="date"
        value={filters.end_date}
        onChange={(e) => update({ end_date: e.target.value })}
        className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:ring-2 focus:ring-gray-200 focus:border-gray-300"
        aria-label="End date"
      />
    </div>
  );
}
