/**
 * Portal lists from 1024 px (D27, R14.9): a table with a 36 px header that
 * sticks under the 56 px top bar, and 48 px rows.
 *
 * Not `DataTable`: the shared table wraps itself in `overflow-x-auto`, which
 * makes its sticky header stick to that box instead of the page, and its rows
 * are 40 px. The column shape matches `DataTable`'s (`header`, `cell`,
 * `align`) so a later switch is mechanical (follow-up: a `stickyTop` offset
 * and a 48 px row height on `DataTable`).
 */
import type { ReactNode } from "react";

export interface PortalColumn<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  className?: string;
}

export default function PortalTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  rowRef,
  first = false,
}: {
  caption: string;
  columns: PortalColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  /** Focus targets (rows get `tabIndex=-1` when set). */
  rowRef?: (key: string, el: HTMLTableRowElement | null) => void;
  first?: boolean;
}) {
  return (
    <table
      data-portal-first={first || undefined}
      className="w-full border-collapse text-left text-sm"
    >
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          {columns.map((c) => (
            <th
              key={c.key}
              scope="col"
              className={`sticky top-14 z-10 h-9 whitespace-nowrap border-b border-border bg-slate-50 px-3 text-xs font-semibold text-slate-600 first:rounded-tl-xl last:rounded-tr-xl ${
                c.align === "right" ? "text-right" : "text-left"
              }`}
            >
              {c.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr
            key={rowKey(row)}
            ref={rowRef ? (el) => rowRef(rowKey(row), el) : undefined}
            tabIndex={rowRef ? -1 : undefined}
            className="h-12 border-b border-slate-100 last:border-b-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus"
          >
            {columns.map((c) => (
              <td
                key={c.key}
                className={`whitespace-nowrap px-3 py-1.5 ${c.align === "right" ? "text-right tabular-nums" : ""} ${c.className ?? ""}`}
              >
                {c.cell(row)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
