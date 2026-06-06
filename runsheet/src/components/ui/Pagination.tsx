/**
 * Pagination Component - Standardized pagination controls
 *
 * Replaces duplicated pagination code across multiple components.
 */

import type React from "react";
import { Button } from "./Button";

export interface PaginationProps {
  currentPage: number;
  totalPages: number;
  totalItems?: number;
  onPageChange: (page: number) => void;
  className?: string;
}

export const Pagination: React.FC<PaginationProps> = ({
  currentPage,
  totalPages,
  totalItems,
  onPageChange,
  className = "",
}) => {
  if (totalPages <= 1) return null;

  return (
    <div
      className={`flex items-center justify-between px-8 py-3 border-t border-gray-100 ${className}`}
    >
      <span className="text-xs text-gray-500">
        Page {currentPage} of {totalPages}
        {totalItems !== undefined && ` (${totalItems} items)`}
      </span>
      <div className="flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => onPageChange(Math.max(1, currentPage - 1))}
          disabled={currentPage <= 1}
          aria-label="Previous page"
        >
          Prev
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => onPageChange(Math.min(totalPages, currentPage + 1))}
          disabled={currentPage >= totalPages}
          aria-label="Next page"
        >
          Next
        </Button>
      </div>
    </div>
  );
};
