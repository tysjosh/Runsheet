/**
 * Board chrome (design K14.1, UI revamp §7.2, R8.2, R8.3).
 *
 * The old two-to-three-row toolbar is split in two:
 *
 * - `BoardTitleControls` sits in the Dispatch title row (contributed through
 *   `usePageChrome()` by `DispatchBoard`): the date stepper with the time
 *   zone, the shift picker, presence avatars, Generate plan and Publish N
 *   ready (R12.1).
 * - `BoardToolbarRow` is the single 44 px row under it: search, a Filters
 *   popover, one View menu (Timeline/Sequence, Comfortable/Compact), the Map
 *   split toggle, the Suggestions count chip, undo and redo, and `⋯`
 *   (Suggestions list, keyboard shortcuts, truck history). While Place mode
 *   is active its prompt replaces the row's contents (`takeover`), so no extra
 *   row appears.
 *
 * The row is a labelled `role="group"`, not an APG `toolbar`: it holds a
 * search box whose arrow keys must keep editing text, so each control stays
 * in the Tab order (Phase 4 review P4-7). Handlers and state come from
 * `DispatchBoard` / `useBoardController` unchanged; view state still lives in
 * `viewState.ts` (same persisted keys).
 */
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Ellipsis,
  Eye,
  Filter,
  History,
  Map as MapIcon,
  Redo2,
  Search,
  Send,
  Sparkles,
  Undo2,
} from "lucide-react";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import type { BoardPresenceUser } from "../../hooks/useDispatchBoardSocket";
import type { BoardShift } from "../../services/dispatchBoardApi";
import { Menu, type MenuItem } from "../ui";
import { FilterChips } from "./FilterChips";
import { PresenceBar } from "./PresenceBar";
import { ShiftPicker } from "./ShiftPicker";
import {
  activeFilterCount,
  addDays,
  type BoardView,
  DAYS_AHEAD,
  DAYS_BACK,
} from "./viewState";

export const ZOOM_LOCKED_TEXT =
  "Timeline needs a screen at least 1,024 pixels wide.";

const btn =
  "inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-300 bg-surface px-2.5 text-xs font-semibold text-slate-800 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-50";
const iconBtn =
  "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-slate-700 hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:text-slate-400 disabled:hover:bg-transparent";
const chip =
  "inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold focus:outline-none focus-visible:ring-2 focus-visible:ring-focus";

// ── Title row ──────────────────────────────────────────────────────────────

export interface BoardTitleControlsProps {
  serviceDate: string;
  today: string;
  zone: string;
  shift: BoardView["shift"];
  shifts: BoardShift[];
  presence: BoardPresenceUser[];
  onShiftChange: (shift: BoardView["shift"]) => void;
  onDateChange: (date: string) => void;
  /** `null` = enabled; otherwise why Generate plan is unavailable. */
  generateDisabledReason: string | null;
  generating: boolean;
  onGenerate: () => void;
  /** Lanes that look ready to publish (the button's count). */
  readyCount: number;
  /** `null` = enabled; otherwise why Publish is unavailable. */
  publishDisabledReason: string | null;
  onPublishAll: () => void;
}

function PublishText({ readyCount }: { readyCount: number }) {
  return readyCount > 0 ? (
    <span>
      Publish {readyCount}
      <span className="max-xl:sr-only"> ready</span>
    </span>
  ) : (
    <span>
      Publish
      <span className="max-xl:sr-only"> all ready</span>
    </span>
  );
}

/** "Publish 3 ready", or "Publish all ready" when no lane looks ready yet. */
export function publishLabel(readyCount: number): string {
  return readyCount > 0 ? `Publish ${readyCount} ready` : "Publish all ready";
}

/**
 * Standalone title row (no Dispatch host, e.g. the e2e harness): the same
 * controls in their own 44 px row.
 */
export function BoardTitleControls(props: BoardTitleControlsProps) {
  return (
    <div className="flex h-11 shrink-0 items-center gap-3 border-b border-slate-200 bg-surface px-4">
      <BoardDayControls {...props} />
      <div className="ml-auto flex shrink-0 items-center gap-2">
        <BoardPrimaryActions {...props} />
      </div>
    </div>
  );
}

/** Date stepper with the time zone, and the shift picker. */
export function BoardDayControls({
  serviceDate,
  today,
  zone,
  shift,
  shifts,
  onShiftChange,
  onDateChange,
}: BoardTitleControlsProps) {
  const min = addDays(today, -DAYS_BACK);
  const max = addDays(today, DAYS_AHEAD);
  return (
    <>
      <div
        role="group"
        aria-label="Day"
        className="flex shrink-0 items-center gap-1"
      >
        <button
          type="button"
          className={`${btn} w-7 justify-center px-0`}
          aria-label="Previous day"
          disabled={serviceDate <= min}
          onClick={() => onDateChange(addDays(serviceDate, -1))}
        >
          <ChevronLeft className="h-4 w-4" aria-hidden="true" />
        </button>
        <label className="sr-only" htmlFor="board-date">
          Service day
        </label>
        <input
          id="board-date"
          type="date"
          value={serviceDate}
          min={min}
          max={max}
          onChange={(e) => e.target.value && onDateChange(e.target.value)}
          className="h-7 w-[7.75rem] rounded-lg border border-slate-300 bg-surface px-2 text-xs font-semibold text-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        />
        <button
          type="button"
          className={`${btn} w-7 justify-center px-0`}
          aria-label="Next day"
          disabled={serviceDate >= max}
          onClick={() => onDateChange(addDays(serviceDate, 1))}
        >
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        </button>
        {zone && (
          <span
            className="text-xs font-medium text-text-muted"
            title="Times are in the tenant time zone"
          >
            {zone}
          </span>
        )}
        {serviceDate !== today && (
          <button
            type="button"
            className="h-7 rounded-lg px-1.5 text-xs font-semibold text-link hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            onClick={() => onDateChange(today)}
          >
            Today
          </button>
        )}
      </div>
      <ShiftPicker value={shift} shifts={shifts} onChange={onShiftChange} />
    </>
  );
}

/** Presence, Generate plan and Publish N ready (right of the title row). */
export function BoardPrimaryActions({
  presence,
  generateDisabledReason,
  generating,
  onGenerate,
  readyCount,
  publishDisabledReason,
  onPublishAll,
}: BoardTitleControlsProps) {
  const ids = useId();
  const generateWhy = `${ids}-generate-why`;
  const publishWhy = `${ids}-publish-why`;
  return (
    <>
      <PresenceBar users={presence} />
      <button
        type="button"
        className={btn}
        disabled={generateDisabledReason !== null || generating}
        title={generateDisabledReason ?? undefined}
        aria-describedby={generateDisabledReason ? generateWhy : undefined}
        onClick={onGenerate}
      >
        <Sparkles className="h-3.5 w-3.5 text-fuchsia-700" aria-hidden="true" />
        <span className="max-xl:sr-only">
          {generating ? "Generating…" : "Generate plan"}
        </span>
      </button>
      {generateDisabledReason && (
        <span id={generateWhy} className="sr-only">
          {generateDisabledReason}
        </span>
      )}
      <button
        type="button"
        className="inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-primary px-2.5 text-xs font-semibold text-on-primary hover:bg-primary-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={publishDisabledReason !== null}
        title={publishDisabledReason ?? undefined}
        aria-describedby={publishDisabledReason ? publishWhy : undefined}
        onClick={onPublishAll}
      >
        <Send className="h-3.5 w-3.5" aria-hidden="true" />
        {/* Narrow title rows (1024 px) show "Publish" / "Publish 3"; the
              accessible name keeps the full label. */}
        <PublishText readyCount={readyCount} />
      </button>
      {publishDisabledReason && (
        <span id={publishWhy} className="sr-only">
          {publishDisabledReason}
        </span>
      )}
    </>
  );
}

// ── Toolbar row ────────────────────────────────────────────────────────────

export interface BoardToolbarRowProps {
  view: BoardView;
  products: string[];
  priorities: string[];
  canUndo: boolean;
  canRedo: boolean;
  onViewChange: (patch: Partial<BoardView>) => void;
  onUndo: () => void;
  onRedo: () => void;
  onHelp: () => void;
  /** Number of open agent suggestions. */
  suggestionCount: number;
  onSuggestions: () => void;
  splitMap: boolean;
  onToggleMap: () => void;
  /** Opens the History tab of the selected (or first) truck; null hides it. */
  onHistory: (() => void) | null;
  /** Read-only notice shown as a chip ("Preview mode, changes are not saved."). */
  readOnlyText?: string | null;
  /** Stacked layout (< 1024 px): Sequence is forced (design K14.8). */
  zoomLocked?: boolean;
  /** Replaces the row's contents (active Place mode, R8.3). */
  takeover?: ReactNode;
}

export function viewLabel(view: Pick<BoardView, "zoom" | "density">): string {
  return `View: ${view.zoom === "timeline" ? "Timeline" : "Sequence"} · ${
    view.density === "compact" ? "Compact" : "Comfortable"
  }`;
}

export function BoardToolbarRow({
  view,
  products,
  priorities,
  canUndo,
  canRedo,
  onViewChange,
  onUndo,
  onRedo,
  onHelp,
  suggestionCount,
  onSuggestions,
  splitMap,
  onToggleMap,
  onHistory,
  readOnlyText,
  zoomLocked = false,
  takeover,
}: BoardToolbarRowProps) {
  const filterCount = activeFilterCount(view.filters);

  const viewItems: MenuItem[] = [
    {
      id: "timeline",
      group: "Zoom",
      label: zoomLocked ? `Timeline (${ZOOM_LOCKED_TEXT})` : "Timeline",
      checked: view.zoom === "timeline",
      disabled: zoomLocked,
      onSelect: () => onViewChange({ zoom: "timeline" }),
    },
    {
      id: "sequence",
      label: "Sequence",
      checked: view.zoom === "sequence",
      onSelect: () => onViewChange({ zoom: "sequence" }),
    },
    {
      id: "comfortable",
      group: "Density",
      label: "Comfortable",
      checked: view.density === "comfortable",
      onSelect: () => onViewChange({ density: "comfortable" }),
    },
    {
      id: "compact",
      label: "Compact",
      checked: view.density === "compact",
      onSelect: () => onViewChange({ density: "compact" }),
    },
  ];

  const moreItems: MenuItem[] = [
    {
      id: "suggestions",
      label: `Suggestions (${suggestionCount})`,
      icon: <Sparkles className="h-3.5 w-3.5" />,
      onSelect: onSuggestions,
    },
    {
      id: "shortcuts",
      label: "Keyboard shortcuts",
      icon: <CircleHelp className="h-3.5 w-3.5" />,
      onSelect: onHelp,
    },
  ];
  if (onHistory) {
    moreItems.push({
      id: "history",
      label: "Truck history",
      icon: <History className="h-3.5 w-3.5" />,
      onSelect: onHistory,
    });
  }

  return (
    <div
      role="group"
      aria-label="Board"
      data-chrome="toolbar"
      className={`relative flex h-11 shrink-0 items-center gap-2 border-b border-slate-200 px-4 ${
        takeover ? "bg-primary-soft" : "bg-surface"
      }`}
    >
      {takeover ?? (
        <>
          {/* min-w-24, not 32: at 640 px (1280 at 200 % zoom) the row is full,
              and Linux fonts made a 128 px floor push the page 4 px sideways. */}
          <div className="relative w-56 min-w-24 shrink">
            <Search
              className="pointer-events-none absolute left-2 top-1.5 h-4 w-4 text-slate-500"
              aria-hidden="true"
            />
            <input
              id="board-search"
              type="search"
              aria-label="Search orders, customers, trucks and drivers"
              placeholder="Search the board"
              value={view.search}
              maxLength={100}
              onChange={(e) => onViewChange({ search: e.target.value })}
              className="h-7 w-full rounded-lg border border-slate-300 bg-surface pl-8 pr-2 text-xs text-slate-900 placeholder:text-slate-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            />
          </div>
          <FiltersPopover
            count={filterCount}
            view={view}
            products={products}
            priorities={priorities}
            onViewChange={onViewChange}
          />
          <Menu
            label="View"
            align="start"
            items={viewItems}
            trigger={(p) => (
              <button
                {...p}
                type="button"
                className={`${chip} border-slate-300 bg-surface text-slate-800 hover:bg-slate-50`}
              >
                {viewLabel(view)}
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            )}
          />
          <button
            type="button"
            aria-pressed={splitMap}
            onClick={onToggleMap}
            className={`${chip} ${
              splitMap
                ? "border-primary bg-primary-soft text-brand-800"
                : "border-slate-300 bg-surface text-slate-800 hover:bg-slate-50"
            }`}
          >
            <MapIcon className="h-3.5 w-3.5" aria-hidden="true" />
            Map
          </button>
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {readOnlyText && (
              <span
                role="status"
                className="inline-flex h-6 items-center gap-1 rounded-full border border-blue-300 bg-blue-50 px-2 text-xs font-semibold text-blue-800"
              >
                <Eye className="h-3.5 w-3.5" aria-hidden="true" />
                {readOnlyText}
              </span>
            )}
            {suggestionCount > 0 && (
              <button
                type="button"
                onClick={onSuggestions}
                className={`${chip} border-fuchsia-300 bg-fuchsia-50 text-fuchsia-800 hover:bg-fuchsia-100`}
              >
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                {suggestionCount}{" "}
                {suggestionCount === 1 ? "suggestion" : "suggestions"}
              </button>
            )}
            <button
              type="button"
              className={iconBtn}
              aria-label="Undo"
              title="Undo"
              disabled={!canUndo}
              onClick={onUndo}
            >
              <Undo2 className="h-4 w-4" aria-hidden="true" />
            </button>
            <button
              type="button"
              className={iconBtn}
              aria-label="Redo"
              title="Redo"
              disabled={!canRedo}
              onClick={onRedo}
            >
              <Redo2 className="h-4 w-4" aria-hidden="true" />
            </button>
            <Menu
              label="More board actions"
              items={moreItems}
              trigger={(p) => (
                <button
                  {...p}
                  type="button"
                  aria-label="More board actions"
                  title="More board actions"
                  className={`${iconBtn} border border-slate-300`}
                >
                  <Ellipsis className="h-4 w-4" aria-hidden="true" />
                </button>
              )}
            />
          </div>
        </>
      )}
    </div>
  );
}

/** "Filters · 2" with the chip groups in a popover (0 px when closed). */
function FiltersPopover({
  count,
  view,
  products,
  priorities,
  onViewChange,
}: {
  count: number;
  view: BoardView;
  products: string[];
  priorities: string[];
  onViewChange: (patch: Partial<BoardView>) => void;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div
      ref={wrapRef}
      className="relative shrink-0"
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.preventDefault();
          e.stopPropagation();
          setOpen(false);
          buttonRef.current?.focus();
        }
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        className={`${chip} ${
          count > 0
            ? "border-primary bg-primary-soft text-brand-800"
            : "border-slate-300 bg-surface text-slate-800 hover:bg-slate-50"
        }`}
        aria-expanded={open}
        aria-controls="board-filters"
        onClick={() => setOpen((o) => !o)}
      >
        <Filter className="h-3.5 w-3.5" aria-hidden="true" />
        {count > 0 ? `Filters · ${count}` : "Filters"}
      </button>
      {open && (
        <div
          id="board-filters"
          className="absolute left-0 top-full z-40 mt-1 w-[min(40rem,80vw)] rounded-lg border border-slate-200 bg-surface p-3 shadow-lg"
        >
          <FilterChips
            filters={view.filters}
            products={products}
            priorities={priorities}
            onChange={(filters) => onViewChange({ filters })}
          />
        </div>
      )}
    </div>
  );
}
