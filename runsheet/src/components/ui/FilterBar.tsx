/**
 * FilterBar Component - Standardized search and filter controls
 *
 * Provides consistent filter layout across all pages.
 * Replaces multiple filter implementations.
 */

import { Search } from "lucide-react";
import type React from "react";

export interface FilterBarProps {
  searchValue?: string;
  onSearchChange?: (value: string) => void;
  searchPlaceholder?: string;
  filters?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}

export const FilterBar: React.FC<FilterBarProps> = ({
  searchValue,
  onSearchChange,
  searchPlaceholder = "Search...",
  filters,
  actions,
  className = "",
}) => {
  return (
    <div className={`flex gap-4 ${className}`}>
      {onSearchChange && (
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-gray-500" />
          <input
            type="text"
            aria-label="Search"
            placeholder={searchPlaceholder}
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            className="w-full pl-10 pr-4 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none"
          />
        </div>
      )}
      {filters && <div className="flex gap-4">{filters}</div>}
      {actions && <div className="flex gap-3">{actions}</div>}
    </div>
  );
};

export interface FilterSelectProps
  extends React.SelectHTMLAttributes<HTMLSelectElement> {
  icon?: React.ReactNode;
  label?: string;
}

export const FilterSelect: React.FC<FilterSelectProps> = ({
  icon,
  label,
  className = "",
  children,
  ...props
}) => {
  return (
    <div className="relative">
      {icon && (
        <div className="absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-500">
          {icon}
        </div>
      )}
      <select
        className={`${icon ? "pl-10" : "pl-4"} pr-8 py-3 text-sm border border-gray-200 rounded-xl focus:ring-2 focus:ring-gray-200 focus:border-gray-300 focus:outline-none bg-white min-w-[140px] ${className}`}
        aria-label={label}
        {...props}
      >
        {children}
      </select>
    </div>
  );
};
