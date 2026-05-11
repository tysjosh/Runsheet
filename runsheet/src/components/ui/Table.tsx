/**
 * Table Component - Standardized table with variants
 * 
 * Provides two table variants:
 * - standard: Spacious layout for detailed views
 * - compact: Dense layout for data-heavy views
 */

import React from 'react';

export interface Column<T> {
  key: string;
  label: string;
  render?: (item: T) => React.ReactNode;
  className?: string;
}

export interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  variant?: 'standard' | 'compact';
  onRowClick?: (item: T) => void;
  selectedId?: string;
  getRowId?: (item: T) => string;
  emptyState?: React.ReactNode;
  className?: string;
}

export function Table<T extends Record<string, any>>({
  columns,
  data,
  variant = 'standard',
  onRowClick,
  selectedId,
  getRowId,
  emptyState,
  className = '',
}: TableProps<T>) {
  const isCompact = variant === 'compact';
  
  const headerPadding = isCompact ? 'px-3 py-1.5' : 'px-6 py-4';
  const cellPadding = isCompact ? 'px-3 py-2' : 'px-6 py-4';
  const headerText = isCompact
    ? 'text-xs font-medium text-gray-600 uppercase'
    : 'text-xs font-medium text-gray-600 uppercase tracking-wider';

  return (
    <div className={`overflow-x-auto ${className}`}>
      <table className="w-full">
        <thead className="bg-gray-50 sticky top-0 border-b border-gray-200 z-10">
          <tr>
            {columns.map((column, index) => (
              <th
                key={column.key}
                className={`text-left ${headerPadding} ${headerText} ${
                  index === 0 && !isCompact ? 'px-8' : ''
                } ${column.className || ''}`}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {data.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="text-center py-12">
                {emptyState || (
                  <div className="text-gray-500">
                    <p className="text-lg font-medium text-gray-400">No data found</p>
                  </div>
                )}
              </td>
            </tr>
          ) : (
            data.map((item, rowIndex) => {
              const rowId = getRowId ? getRowId(item) : String(rowIndex);
              const isSelected = selectedId === rowId;
              const isClickable = !!onRowClick;

              return (
                <tr
                  key={rowId}
                  onClick={() => onRowClick?.(item)}
                  className={`transition-colors ${
                    isClickable ? 'cursor-pointer' : ''
                  } ${
                    isSelected ? 'bg-blue-50' : 'hover:bg-gray-50'
                  }`}
                >
                  {columns.map((column, colIndex) => (
                    <td
                      key={column.key}
                      className={`${cellPadding} ${
                        colIndex === 0 && !isCompact ? 'px-8' : ''
                      } ${column.className || ''}`}
                    >
                      {column.render
                        ? column.render(item)
                        : item[column.key]}
                    </td>
                  ))}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
