"use client";

/**
 * GlobalSearch: the top bar's cross-entity search (orders, customers, assets
 * via GET /api/search/universal).
 *
 * Implements the WAI-ARIA combobox pattern (R2.1, R12.2; fixes axe
 * `aria-allowed-attr` on the old `type=search` input): the input is
 * `role="combobox"` with `aria-expanded`, `aria-controls` and
 * `aria-activedescendant`; results are grouped options in a listbox.
 * ArrowUp/Down move, Enter opens the active result (or, with none active,
 * jumps to a single match or hands the query to the Orders list), Escape
 * closes then clears. Pressing `/` anywhere outside a text field focuses it.
 */
import { Loader2, Search } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  apiService,
  type UniversalSearchHit,
  type UniversalSearchResults,
} from "../services/api";
import { type EntityType, entityHref, useInShellNav } from "./ui";

const EMPTY: UniversalSearchResults = { orders: [], customers: [], assets: [] };
const GROUPS: { key: keyof UniversalSearchResults; label: string }[] = [
  { key: "orders", label: "Orders" },
  { key: "customers", label: "Customers" },
  { key: "assets", label: "Assets" },
];

interface GlobalSearchProps {
  /** Enter with no result selected: scope the Orders list to the query. */
  onSubmitFallback: (query: string) => void;
}

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    el.isContentEditable
  );
}

export default function GlobalSearch({ onSubmitFallback }: GlobalSearchProps) {
  const nav = useInShellNav();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<UniversalSearchResults>(EMPTY);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();
  const optionId = (i: number) => `${listboxId}-opt-${i}`;

  const flat: UniversalSearchHit[] = GROUPS.flatMap((g) => results[g.key]);
  const total = flat.length;

  // Debounced server search.
  useEffect(() => {
    const trimmed = query.trim();
    setActive(-1);
    if (!trimmed) {
      setResults(EMPTY);
      setLoading(false);
      return;
    }
    setLoading(true);
    const t = setTimeout(async () => {
      try {
        const res = await apiService.universalSearch(trimmed, 5);
        setResults(res);
      } catch {
        setResults(EMPTY);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(t);
  }, [query]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  // "/" focuses the search from anywhere that is not a text field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (isTypingTarget(e.target)) return;
      e.preventDefault();
      inputRef.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const navigate = useCallback(
    (hit: UniversalSearchHit) => {
      const type = hit.type as EntityType;
      setOpen(false);
      setQuery("");
      if (nav?.handles(type)) nav.open(type, hit.id);
      else window.location.assign(entityHref(type, hit.id));
    },
    [nav],
  );

  const submit = () => {
    if (active >= 0 && flat[active]) {
      navigate(flat[active]);
      return;
    }
    if (total === 1) {
      navigate(flat[0]);
      return;
    }
    setOpen(false);
    onSubmitFallback(query.trim());
  };

  const showDropdown = open && query.trim().length > 0;

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setOpen(true);
        if (total > 0) setActive((a) => (a + 1) % total);
        break;
      case "ArrowUp":
        e.preventDefault();
        setOpen(true);
        if (total > 0) setActive((a) => (a <= 0 ? total - 1 : a - 1));
        break;
      case "Enter":
        e.preventDefault();
        submit();
        break;
      case "Escape":
        if (showDropdown) {
          e.preventDefault();
          setOpen(false);
          setActive(-1);
        } else if (query) {
          e.preventDefault();
          setQuery("");
        }
        break;
      default:
    }
  };

  let index = -1;
  return (
    <div ref={containerRef} className="relative min-w-0 max-w-xl flex-1">
      <form
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="relative w-full">
          <Search
            aria-hidden="true"
            className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"
          />
          <input
            ref={inputRef}
            type="text"
            role="combobox"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={onKeyDown}
            placeholder="Search orders, customers, assets…"
            aria-label="Search orders, customers, and assets"
            aria-autocomplete="list"
            aria-expanded={showDropdown}
            aria-controls={listboxId}
            aria-activedescendant={
              showDropdown && active >= 0 ? optionId(active) : undefined
            }
            aria-keyshortcuts="/"
            autoComplete="off"
            className="h-8 w-full rounded-lg border border-slate-200 bg-slate-50 pl-8 pr-9 text-sm text-text placeholder:text-slate-500 focus:border-primary focus:bg-surface focus:outline-none focus:ring-2 focus:ring-focus/30"
          />
          {loading ? (
            <Loader2
              className="absolute right-2.5 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-slate-500"
              aria-hidden="true"
            />
          ) : (
            <span
              aria-hidden="true"
              className="pointer-events-none absolute right-2 top-1/2 hidden -translate-y-1/2 rounded border border-slate-300 px-1 font-mono text-[11px] text-slate-600 sm:block"
            >
              /
            </span>
          )}
        </div>
      </form>
      <div
        id={listboxId}
        role="listbox"
        aria-label="Search results"
        hidden={!showDropdown}
        className="absolute z-50 mt-1 max-h-[70vh] w-full min-w-[20rem] overflow-y-auto rounded-lg border border-slate-200 bg-white py-1 shadow-lg"
      >
        {showDropdown &&
          (total === 0 && !loading ? (
            <div
              role="presentation"
              className="px-3 py-3 text-center text-sm text-text-muted"
            >
              No matches for “{query.trim()}”
            </div>
          ) : (
            GROUPS.map((group) => {
              const hits = results[group.key];
              if (hits.length === 0) return null;
              return (
                <div
                  key={group.key}
                  role="group"
                  aria-labelledby={`${listboxId}-${group.key}`}
                  className="py-1"
                >
                  <div
                    id={`${listboxId}-${group.key}`}
                    role="presentation"
                    className="px-3 py-1 text-xs font-semibold text-text-muted"
                  >
                    {group.label}
                  </div>
                  {hits.map((hit) => {
                    index += 1;
                    const i = index;
                    return (
                      <div
                        key={`${hit.type}-${hit.id}`}
                        id={optionId(i)}
                        role="option"
                        tabIndex={-1}
                        aria-selected={i === active}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => navigate(hit)}
                        onMouseEnter={() => setActive(i)}
                        className={`flex w-full cursor-pointer flex-col items-start gap-0.5 px-3 py-1.5 text-left ${
                          i === active ? "bg-slate-100" : "hover:bg-slate-50"
                        }`}
                      >
                        <span className="truncate text-sm text-text">
                          {hit.label}
                        </span>
                        {hit.sublabel && (
                          <span className="truncate text-xs text-text-muted">
                            {hit.sublabel}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            })
          ))}
      </div>
    </div>
  );
}
