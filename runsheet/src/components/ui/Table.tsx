/**
 * Table Component - Standardized table with variants
 *
 * The single source of truth for tabular layouts across the platform.
 * Provides two density variants:
 * - standard: Spacious layout for detailed views (px-6/py-4, px-8 first col)
 * - compact: Dense layout for data-heavy views (px-3/py-2)
 *
 * Columns describe how each cell renders; the component owns the chrome
 * (header styling, zebra dividers, hover, sticky header, empty/loading
 * states, optional footer, and optional per-row expansion) so individual
 * pages never hand-roll <table> markup.
 */

import type React from "react";

/** Horizontal alignment for a column's header and cells. */
export type ColumnAlign = "left" | "right" | "center";

export interface Column<T> {
  key: string;
  label: React.ReactNode;
  render?: (item: T) => React.ReactNode;
  /** Cell + header alignment. Defaults to "left". */
  align?: ColumnAlign;
  /** Extra classes applied to every body cell in this column. */
  className?: string;
  /** Extra classes applied to this column's header cell. */
  headerClassName?: string;
}

export interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  variant?: "standard" | "compact";
  onRowClick?: (item: T) => void;
  selectedId?: string;
  getRowId?: (item: T) => string;
  keyExtractor?: (item: T) => string;
  emptyState?: React.ReactNode;
  className?: string;
  /** Accessible label forwarded to the underlying <table>. */
  ariaLabel?: string;
  /** When true, renders a loading row instead of data/empty. */
  loading?: boolean;
  /** Loading row content (defaults to a spinner + "Loading…"). */
  loadingState?: React.ReactNode;
  /**
   * Optional per-row expansion. When provided and it returns non-null for a
   * row, an extra full-width <tr> is rendered beneath that row. The host
   * controls open/closed state (e.g. via a column button + local state).
   */
  renderExpanded?: (item: T) => React.ReactNode | null;
  /** Optional per-row class override (e.g. status-based row coloring). */
  rowClassName?: (item: T) => string;
  /** Optional per-row data-testid for targeting rows in tests. */
  rowTestId?: (item: T) => string;
  /** Optional footer row content rendered in a <tfoot>. */
  footer?: React.ReactNode;
}

const ALIGN_CLASS: Record<ColumnAlign, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

export function Table<T extends Record<string, any>>({
  columns,
  data,
  variant = "standard",
  onRowClick,
  selectedId,
  getRowId,
  keyExtractor,
  emptyState,
  className = "",
  ariaLabel,
  loading = false,
  loadingState,
  renderExpanded,
  rowClassName,
  rowTestId,
  footer,
}: TableProps<T>) {
  const isCompact = variant === "compact";

  const headerPadding = isCompact ? "px-3 py-1.5" : "px-6 py-4";
  const cellPadding = isCompact ? "px-3 py-2" : "px-6 py-4";
  const headerText = isCompact
    ? "text-xs font-medium text-gray-600 uppercase"
    : "text-xs font-medium text-gray-600 uppercase tracking-wider";

  const resolveRowId = getRowId ?? keyExtractor;

  return (
    <div className={`overflow-x-auto ${className}`}>
      <table className="w-full" aria-label={ariaLabel}>
        <thead className="bg-gray-50 sticky top-0 border-b border-gray-200 z-10">
          <tr>
            {columns.map((column, index) => {
              const align = ALIGN_CLASS[column.align ?? "left"];
              return (
                <th
                  key={column.key}
                  className={`${align} ${headerPadding} ${headerText} ${
                    index === 0 && !isCompact ? "px-8" : ""
                  } ${column.headerClassName || ""}`}
                >
                  {column.label}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {loading ? (
            <tr>
              <td colSpan={columns.length} className="text-center py-12">
                {loadingState || (
                  <div className="inline-flex items-center gap-2 text-gray-500 text-sm">
                    <span className="w-4 h-4 border-2 border-gray-300 border-t-primary rounded-full animate-spin" />
                    Loading…
                  </div>
                )}
              </td>
            </tr>
          ) : data.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="text-center py-12">
                {emptyState || (
                  <div className="text-gray-500">
                    <p className="text-lg font-medium text-gray-500">
                      No data found
                    </p>
                  </div>
                )}
              </td>
            </tr>
          ) : (
            data.map((item, rowIndex) => {
              const rowId = resolveRowId?.(item) ?? String(rowIndex);
              const isSelected = selectedId === rowId;
              const isClickable = !!onRowClick;
              const expanded = renderExpanded?.(item);
              const customRowClass = rowClassName?.(item);

              return (
                <FragmentRow key={rowId}>
                  <tr
                    data-testid={rowTestId?.(item)}
                    onClick={() => onRowClick?.(item)}
                    className={`transition-colors ${
                      isClickable ? "cursor-pointer" : ""
                    } ${
                      isSelected
                        ? "bg-info-light"
                        : customRowClass || "hover:bg-gray-50"
                    }`}
                  >
                    {columns.map((column, colIndex) => {
                      const align = ALIGN_CLASS[column.align ?? "left"];
                      return (
                        <td
                          key={column.key}
                          className={`${align} ${cellPadding} ${
                            colIndex === 0 && !isCompact ? "px-8" : ""
                          } ${column.className || ""}`}
                        >
                          {column.render
                            ? column.render(item)
                            : item[column.key]}
                        </td>
                      );
                    })}
                  </tr>
                  {expanded != null && (
                    <tr>
                      <td colSpan={columns.length} className="p-0">
                        {expanded}
                      </td>
                    </tr>
                  )}
                </FragmentRow>
              );
            })
          )}
        </tbody>
        {footer && (
          <tfoot className="border-t border-gray-200 bg-gray-50">
            {footer}
          </tfoot>
        )}
      </table>
    </div>
  );
}

/**
 * Wrapper that lets a data row and its optional expansion row share a single
 * React key without introducing extra DOM. Kept local to avoid importing
 * React.Fragment shorthand (which cannot take a key) at every call site.
 */
function FragmentRow({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
