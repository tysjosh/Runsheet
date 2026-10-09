"use client";

/**
 * DataTable: the one table for the staff app (R4.6, design.md §3).
 *
 * - Header: single line, 36 px, sticky by default, sentence case.
 * - Rows: 40 px (comfortable) or 32 px (compact). Cells marked `truncate`
 *   clip to one line with the full text in a `title` tooltip.
 * - `aria-sort` appears only on `<th>` (axe `aria-allowed-attr`).
 * - Built-in empty, loading (skeleton rows) and error states, optional row
 *   selection, a per-row `⋯` menu, and pagination.
 *
 * `Table` is the same component (the older column shape — `label`/`render` —
 * keeps working), so the 40-odd existing call sites pick up the compact rows
 * without changes. Status-based full-row tinting (`rowClassName`) still works
 * for pages not yet migrated; R5.5 removes it page by page.
 */
import { ArrowDown, ArrowUp, ArrowUpDown, Ellipsis } from "lucide-react";
import type React from "react";
import { Fragment } from "react";
import { Menu, type MenuItem } from "./Menu";
import { Pagination } from "./Pagination";

/** Horizontal alignment for a column's header and cells. */
export type ColumnAlign = "left" | "right" | "center";
export type SortDirection = "asc" | "desc";

export interface Column<T> {
  key: string;
  /** Header content (legacy name; `header` is preferred). */
  label?: React.ReactNode;
  header?: React.ReactNode;
  /** Cell renderer (legacy name; `cell` is preferred). */
  render?: (item: T) => React.ReactNode;
  cell?: (item: T) => React.ReactNode;
  /** Cell + header alignment. Defaults to "left". */
  align?: ColumnAlign;
  /** CSS width, e.g. 120 or "20%". */
  width?: number | string;
  sortable?: boolean;
  /** Clip to one line; the tooltip shows `title(item)` or the text value. */
  truncate?: boolean;
  title?: (item: T) => string | undefined;
  /** Extra classes applied to every body cell in this column. */
  className?: string;
  /** Extra classes applied to this column's header cell. */
  headerClassName?: string;
}

export interface TableSort {
  key: string;
  direction: SortDirection;
}

export interface TablePagination {
  page: number;
  totalPages: number;
  totalItems?: number;
  onPageChange: (page: number) => void;
}

export interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  /** Legacy density name: standard = comfortable, compact = compact. */
  variant?: "standard" | "compact";
  /** `touch`: 48 px rows for phone-sized targets (additive, task 3.11). */
  rowHeight?: "comfortable" | "compact" | "touch";
  stickyHeader?: boolean;
  /**
   * Stick the header at this offset from the page's scroll container (e.g.
   * below a sticky title row). The table then has no `overflow-x-auto`
   * wrapper, which would otherwise capture the sticky header. Trade-off: a
   * table wider than its container then overflows the container instead of
   * scrolling inside it, so use it only for tables that fit (or let the page
   * scroll horizontally). Additive.
   */
  stickyTop?: number | string;
  onRowClick?: (item: T) => void;
  selectedId?: string;
  getRowId?: (item: T) => string;
  keyExtractor?: (item: T) => string;
  emptyState?: React.ReactNode;
  className?: string;
  /** Accessible label forwarded to the underlying <table>. */
  ariaLabel?: string;
  /** When true, renders skeleton rows (or `loadingState`) instead of data. */
  loading?: boolean;
  loadingState?: React.ReactNode;
  /** Shown instead of rows when set. */
  error?: { message: string; onRetry?: () => void } | null;
  /** Optional per-row expansion rendered as a full-width row beneath it. */
  renderExpanded?: (item: T) => React.ReactNode | null;
  /** Optional per-row class override. */
  rowClassName?: (item: T) => string;
  /** Optional per-row data-testid for targeting rows in tests. */
  rowTestId?: (item: T) => string;
  /** Optional footer row content rendered in a <tfoot>. */
  footer?: React.ReactNode;
  sort?: TableSort | null;
  onSortChange?: (sort: TableSort) => void;
  selectable?: boolean;
  selectedIds?: string[];
  onSelectionChange?: (ids: string[]) => void;
  /** Accessible name for a row's checkbox and menu (defaults to its id). */
  rowLabel?: (item: T) => string;
  rowMenu?: (item: T) => MenuItem[];
  pagination?: TablePagination;
}

const ALIGN_CLASS: Record<ColumnAlign, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

function textOf(node: unknown): string | undefined {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  return undefined;
}

export function DataTable<T extends Record<string, any>>({
  columns,
  data,
  variant = "standard",
  rowHeight,
  stickyHeader = true,
  stickyTop,
  onRowClick,
  selectedId,
  getRowId,
  keyExtractor,
  emptyState,
  className = "",
  ariaLabel,
  loading = false,
  loadingState,
  error,
  renderExpanded,
  rowClassName,
  rowTestId,
  footer,
  sort,
  onSortChange,
  selectable = false,
  selectedIds = [],
  onSelectionChange,
  rowLabel,
  rowMenu,
  pagination,
}: TableProps<T>) {
  const density =
    rowHeight ?? (variant === "compact" ? "compact" : "comfortable");
  const rowH =
    density === "compact" ? "h-8" : density === "touch" ? "h-12" : "h-10";
  const cellPad = "px-3 py-1";
  const resolveRowId = getRowId ?? keyExtractor;
  const selected = new Set(selectedIds);
  const ids = data.map((item, i) => resolveRowId?.(item) ?? String(i));
  const allSelected = ids.length > 0 && ids.every((id) => selected.has(id));
  const someSelected = ids.some((id) => selected.has(id));
  const colCount = columns.length + (selectable ? 1 : 0) + (rowMenu ? 1 : 0);

  const toggleRow = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectionChange?.([...next]);
  };
  const toggleAll = () => onSelectionChange?.(allSelected ? [] : ids);

  const fullRow = (content: React.ReactNode, cls = "py-10") => (
    <tr>
      <td colSpan={colCount} className={`text-center ${cls}`}>
        {content}
      </td>
    </tr>
  );

  let body: React.ReactNode;
  if (error) {
    body = fullRow(
      <div
        role="alert"
        className="inline-flex flex-col items-center gap-2 text-sm"
      >
        <span className="font-medium text-red-800">{error.message}</span>
        {error.onRetry && (
          <button
            type="button"
            onClick={error.onRetry}
            className="rounded-lg border border-slate-300 px-3 py-1 text-xs font-medium text-slate-800 hover:bg-slate-50"
          >
            Retry
          </button>
        )}
      </div>,
    );
  } else if (loading) {
    body = loadingState
      ? fullRow(loadingState)
      : Array.from({ length: 5 }, (_, r) => (
          <tr key={`sk-${r}`} className={rowH}>
            {Array.from({ length: colCount }, (_, c) => (
              <td key={c} className={cellPad}>
                <div
                  aria-hidden="true"
                  className="h-3 w-3/4 animate-pulse rounded bg-slate-200"
                />
              </td>
            ))}
          </tr>
        ));
  } else if (data.length === 0) {
    body = fullRow(
      emptyState || (
        <p className="text-sm font-medium text-text-muted">No data found</p>
      ),
    );
  } else {
    body = data.map((item, rowIndex) => {
      const rowId = ids[rowIndex];
      const isSelected = selectedId === rowId || selected.has(rowId);
      const expanded = renderExpanded?.(item);
      const customRowClass = rowClassName?.(item);
      const name = rowLabel?.(item) ?? rowId;
      return (
        <Fragment key={rowId}>
          <tr
            data-testid={rowTestId?.(item)}
            data-row-id={rowId}
            aria-selected={selectable ? selected.has(rowId) : undefined}
            onClick={() => onRowClick?.(item)}
            className={`${rowH} transition-colors ${onRowClick ? "cursor-pointer" : ""} ${
              isSelected
                ? "bg-info-light"
                : customRowClass || "hover:bg-gray-50"
            }`}
          >
            {selectable && (
              <td className="w-9 px-3" onClick={(e) => e.stopPropagation()}>
                <input
                  type="checkbox"
                  aria-label={`Select ${name}`}
                  checked={selected.has(rowId)}
                  onChange={() => toggleRow(rowId)}
                  className="h-4 w-4 rounded border-slate-400 accent-[var(--rs-primary)]"
                />
              </td>
            )}
            {columns.map((column) => {
              const align = ALIGN_CLASS[column.align ?? "left"];
              const renderer = column.cell ?? column.render;
              const content = renderer ? renderer(item) : item[column.key];
              const tip = column.truncate
                ? (column.title?.(item) ?? textOf(content))
                : undefined;
              return (
                <td
                  key={column.key}
                  title={tip}
                  className={`${align} ${cellPad} text-sm ${
                    column.truncate ? "max-w-0 truncate whitespace-nowrap" : ""
                  } ${column.className || ""}`}
                >
                  {content}
                </td>
              );
            })}
            {rowMenu && (
              <td
                className="w-10 px-1 text-right"
                onClick={(e) => e.stopPropagation()}
              >
                <Menu
                  label={`Actions for ${name}`}
                  items={rowMenu(item)}
                  trigger={(p) => (
                    <button
                      {...p}
                      type="button"
                      aria-label={`Actions for ${name}`}
                      className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                    >
                      <Ellipsis aria-hidden="true" className="h-4 w-4" />
                    </button>
                  )}
                />
              </td>
            )}
          </tr>
          {expanded != null && (
            <tr>
              <td colSpan={colCount} className="p-0">
                {expanded}
              </td>
            </tr>
          )}
        </Fragment>
      );
    });
  }

  return (
    <div
      className={`${stickyTop === undefined ? "overflow-x-auto" : ""} ${className}`}
    >
      <table className="w-full border-collapse" aria-label={ariaLabel}>
        <thead
          className={`border-b border-slate-200 bg-slate-50 ${stickyHeader || stickyTop !== undefined ? "sticky top-0 z-10" : ""}`}
          style={stickyTop !== undefined ? { top: stickyTop } : undefined}
        >
          <tr className="h-9">
            {selectable && (
              <th scope="col" className="w-9 px-3 text-left">
                <input
                  type="checkbox"
                  aria-label="Select all rows"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someSelected && !allSelected;
                  }}
                  onChange={toggleAll}
                  className="h-4 w-4 rounded border-slate-400 accent-[var(--rs-primary)]"
                />
              </th>
            )}
            {columns.map((column) => {
              const align = ALIGN_CLASS[column.align ?? "left"];
              const head = column.header ?? column.label;
              const active = sort?.key === column.key ? sort.direction : null;
              const ariaSort = column.sortable
                ? active === "asc"
                  ? "ascending"
                  : active === "desc"
                    ? "descending"
                    : "none"
                : undefined;
              const SortIcon =
                active === "asc"
                  ? ArrowUp
                  : active === "desc"
                    ? ArrowDown
                    : ArrowUpDown;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={ariaSort}
                  style={
                    column.width !== undefined
                      ? { width: column.width }
                      : undefined
                  }
                  className={`${align} whitespace-nowrap px-3 text-xs font-semibold text-slate-600 ${
                    column.headerClassName || ""
                  }`}
                >
                  {column.sortable && onSortChange ? (
                    <button
                      type="button"
                      onClick={() =>
                        onSortChange({
                          key: column.key,
                          direction: active === "asc" ? "desc" : "asc",
                        })
                      }
                      className="inline-flex items-center gap-1 rounded hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                    >
                      {head}
                      <SortIcon aria-hidden="true" className="h-3 w-3" />
                    </button>
                  ) : (
                    head
                  )}
                </th>
              );
            })}
            {rowMenu && (
              <th scope="col" className="w-10 px-1">
                <span className="sr-only">Actions</span>
              </th>
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{body}</tbody>
        {footer && (
          <tfoot className="border-t border-slate-200 bg-slate-50">
            {footer}
          </tfoot>
        )}
      </table>
      {pagination && (
        <Pagination
          currentPage={pagination.page}
          totalPages={pagination.totalPages}
          totalItems={pagination.totalItems}
          onPageChange={pagination.onPageChange}
        />
      )}
    </div>
  );
}

/** `Table` is `DataTable` (kept for the existing call sites). */
export const Table = DataTable;
