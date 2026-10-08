/**
 * Board toolbar (design K14.1): date navigation (today − 7 … today + 14),
 * shift, zoom, density, search, filters, presence, undo/redo and help,
 * plus Generate plan and agent suggestions (R16.6), the map split view
 * (R17.3) and Publish all ready (R12.1).
 *
 * It is a labelled `role="group"`, not an APG `toolbar`: it holds a date
 * input and a search box, whose arrow keys must keep editing text, so each
 * control stays in the Tab order (Phase 4 review P4-7).
 */
import {
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Filter,
  Map as MapIcon,
  Redo2,
  Search,
  Send,
  Sparkles,
  Undo2,
} from "lucide-react";
import { useState } from "react";
import type { BoardPresenceUser } from "../../hooks/useDispatchBoardSocket";
import type { BoardShift } from "../../services/dispatchBoardApi";
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

export interface BoardToolbarProps {
  view: BoardView;
  serviceDate: string;
  today: string;
  zone: string;
  shifts: BoardShift[];
  products: string[];
  priorities: string[];
  presence: BoardPresenceUser[];
  canUndo: boolean;
  canRedo: boolean;
  onViewChange: (patch: Partial<BoardView>) => void;
  onDateChange: (date: string) => void;
  onUndo: () => void;
  onRedo: () => void;
  onHelp: () => void;
  /** Number of open agent suggestions. */
  suggestionCount: number;
  onSuggestions: () => void;
  /** `null` = enabled; otherwise why Generate plan is unavailable. */
  generateDisabledReason: string | null;
  generating: boolean;
  onGenerate: () => void;
  splitMap: boolean;
  onToggleMap: () => void;
  /** `null` = enabled; otherwise why Publish all ready is unavailable. */
  publishDisabledReason: string | null;
  onPublishAll: () => void;
  /** Stacked layout (< 1024 px): Sequence is forced (design K14.8). */
  zoomLocked?: boolean;
}

const segment = (active: boolean) =>
  `min-h-9 px-3 text-sm font-medium ${
    active ? "bg-primary text-white" : "bg-white text-gray-700 hover:bg-gray-50"
  }`;

const textButton =
  "inline-flex min-h-9 items-center gap-1 rounded-md border border-gray-300 bg-white px-3 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50";

const iconButton =
  "inline-flex min-h-9 min-w-9 items-center justify-center rounded-md border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50";

export function BoardToolbar({
  view,
  serviceDate,
  today,
  zone,
  shifts,
  products,
  priorities,
  presence,
  canUndo,
  canRedo,
  onViewChange,
  onDateChange,
  onUndo,
  onRedo,
  onHelp,
  suggestionCount,
  onSuggestions,
  generateDisabledReason,
  generating,
  onGenerate,
  splitMap,
  onToggleMap,
  publishDisabledReason,
  onPublishAll,
  zoomLocked = false,
}: BoardToolbarProps) {
  const [filtersOpen, setFiltersOpen] = useState(false);
  const min = addDays(today, -DAYS_BACK);
  const max = addDays(today, DAYS_AHEAD);
  const filterCount = activeFilterCount(view.filters);

  return (
    <div
      role="group"
      aria-label="Board"
      className="border-b border-gray-200 bg-white px-4 py-2"
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-1">
          <button
            type="button"
            className={iconButton}
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
            className="min-h-9 rounded-md border border-gray-300 px-2 text-sm"
          />
          <button
            type="button"
            className={iconButton}
            aria-label="Next day"
            disabled={serviceDate >= max}
            onClick={() => onDateChange(addDays(serviceDate, 1))}
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
          {serviceDate !== today && (
            <button
              type="button"
              className="min-h-9 px-2 text-sm font-medium text-primary hover:underline"
              onClick={() => onDateChange(today)}
            >
              Today
            </button>
          )}
          <span
            className="ml-1 text-xs text-gray-500"
            title="Times are in the tenant time zone"
          >
            {zone}
          </span>
        </div>

        <ShiftPicker
          value={view.shift}
          shifts={shifts}
          onChange={(shift) => onViewChange({ shift })}
        />

        <fieldset className="flex overflow-hidden rounded-md border border-gray-300">
          <legend className="sr-only">Zoom</legend>
          <button
            type="button"
            aria-pressed={view.zoom === "timeline"}
            className={`${segment(view.zoom === "timeline")} disabled:cursor-not-allowed disabled:text-gray-400`}
            disabled={zoomLocked}
            aria-describedby={zoomLocked ? "board-zoom-locked" : undefined}
            onClick={() => onViewChange({ zoom: "timeline" })}
          >
            Timeline
          </button>
          <button
            type="button"
            aria-pressed={view.zoom === "sequence"}
            className={segment(view.zoom === "sequence")}
            onClick={() => onViewChange({ zoom: "sequence" })}
          >
            Sequence
          </button>
          {zoomLocked && (
            <span id="board-zoom-locked" className="sr-only">
              Timeline needs a screen at least 1,024 pixels wide.
            </span>
          )}
        </fieldset>

        <fieldset className="flex overflow-hidden rounded-md border border-gray-300">
          <legend className="sr-only">Density</legend>
          <button
            type="button"
            aria-pressed={view.density === "comfortable"}
            className={segment(view.density === "comfortable")}
            onClick={() => onViewChange({ density: "comfortable" })}
          >
            Comfortable
          </button>
          <button
            type="button"
            aria-pressed={view.density === "compact"}
            className={segment(view.density === "compact")}
            onClick={() => onViewChange({ density: "compact" })}
          >
            Compact
          </button>
        </fieldset>

        <div className="relative">
          <Search
            className="pointer-events-none absolute left-2 top-2.5 h-4 w-4 text-gray-400"
            aria-hidden="true"
          />
          <input
            id="board-search"
            type="search"
            aria-label="Search orders, customers, trucks and drivers"
            placeholder="Search"
            value={view.search}
            maxLength={100}
            onChange={(e) => onViewChange({ search: e.target.value })}
            className="min-h-9 w-56 rounded-md border border-gray-300 pl-8 pr-2 text-sm"
          />
        </div>

        <button
          type="button"
          className="inline-flex min-h-9 items-center gap-1 rounded-md border border-gray-300 bg-white px-3 text-sm font-medium text-gray-700 hover:bg-gray-50"
          aria-expanded={filtersOpen}
          aria-controls="board-filters"
          onClick={() => setFiltersOpen((o) => !o)}
        >
          <Filter className="h-4 w-4" aria-hidden="true" />
          Filters{filterCount > 0 ? ` (${filterCount})` : ""}
        </button>

        <button
          type="button"
          className={textButton}
          aria-pressed={splitMap}
          onClick={onToggleMap}
        >
          <MapIcon className="h-4 w-4" aria-hidden="true" />
          Map
        </button>

        <div className="ml-auto flex items-center gap-2">
          <PresenceBar users={presence} />
          <button type="button" className={textButton} onClick={onSuggestions}>
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            Suggestions ({suggestionCount})
          </button>
          <button
            type="button"
            className={textButton}
            disabled={generateDisabledReason !== null || generating}
            title={generateDisabledReason ?? undefined}
            aria-describedby={
              generateDisabledReason ? "board-generate-why" : undefined
            }
            onClick={onGenerate}
          >
            {generating ? "Generating…" : "Generate plan"}
          </button>
          {generateDisabledReason && (
            <span id="board-generate-why" className="sr-only">
              {generateDisabledReason}
            </span>
          )}
          <button
            type="button"
            className="inline-flex min-h-9 items-center gap-1 rounded-md bg-primary px-3 text-sm font-medium text-white hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
            disabled={publishDisabledReason !== null}
            title={publishDisabledReason ?? undefined}
            aria-describedby={
              publishDisabledReason ? "board-publish-why" : undefined
            }
            onClick={onPublishAll}
          >
            <Send className="h-4 w-4" aria-hidden="true" />
            Publish all ready
          </button>
          {publishDisabledReason && (
            <span id="board-publish-why" className="sr-only">
              {publishDisabledReason}
            </span>
          )}
          <button
            type="button"
            className={iconButton}
            aria-label="Undo"
            disabled={!canUndo}
            onClick={onUndo}
          >
            <Undo2 className="h-4 w-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            className={iconButton}
            aria-label="Redo"
            disabled={!canRedo}
            onClick={onRedo}
          >
            <Redo2 className="h-4 w-4" aria-hidden="true" />
          </button>
          <button
            type="button"
            className={iconButton}
            aria-label="Keyboard shortcuts"
            onClick={onHelp}
          >
            <CircleHelp className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      {filtersOpen && (
        <div id="board-filters" className="mt-2">
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

export default BoardToolbar;
