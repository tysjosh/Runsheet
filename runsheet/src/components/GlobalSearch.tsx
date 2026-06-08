"use client";

/**
 * GlobalSearch — the header's cross-entity search.
 *
 * Replaces the orders-only header box with a universal search over orders,
 * customers, and assets (GET /api/search/universal). Typing debounces a
 * server query and shows grouped results in a dropdown; selecting a result
 * navigates to that entity (in-shell when the shell can host the type, via a
 * canonical route otherwise). Pressing Enter with no selection falls back to
 * the orders board scoped to the query (the previous behavior).
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
  /** Enter with no result selected: scope the orders board to the query. */
  onSubmitFallback: (query: string) => void;
}

export default function GlobalSearch({ onSubmitFallback }: GlobalSearchProps) {
  const nav = useInShellNav();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<UniversalSearchResults>(EMPTY);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  // Flatten the grouped results into a single ordered list for keyboard nav
  // and to detect emptiness.
  const flat: UniversalSearchHit[] = GROUPS.flatMap((g) => results[g.key]);
  const total = flat.length;

  // Debounced server search.
  useEffect(() => {
    const trimmed = query.trim();
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
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const navigate = useCallback(
    (hit: UniversalSearchHit) => {
      const type = hit.type as EntityType;
      setOpen(false);
      setQuery("");
      if (nav?.handles(type)) {
        nav.open(type, hit.id);
      } else {
        window.location.assign(entityHref(type, hit.id));
      }
    },
    [nav],
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = query.trim();
    // If there's exactly one obvious match, jump straight to it; otherwise
    // hand the query to the orders board.
    if (total === 1) {
      navigate(flat[0]);
      return;
    }
    setOpen(false);
    onSubmitFallback(trimmed);
  };

  const showDropdown = open && query.trim().length > 0;

  return (
    <div ref={containerRef} className="relative ml-2 max-w-xl flex-1">
      <form onSubmit={handleSubmit} role="search">
        <div className="relative w-full">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2"
            style={{ color: "var(--color-gray-400)" }}
          />
          <input
            type="search"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            placeholder="Search orders, customers, assets…"
            aria-label="Search orders, customers, and assets"
            aria-expanded={showDropdown}
            aria-controls={listboxId}
            className="w-full rounded-lg border py-2 pl-9 pr-9 text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--color-primary)]"
            style={{
              borderColor:
                "color-mix(in srgb, var(--color-primary) 12%, transparent)",
              backgroundColor: "var(--color-surface-muted)",
              color: "var(--color-primary)",
            }}
          />
          {loading && (
            <Loader2
              className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-gray-400"
              aria-hidden="true"
            />
          )}
        </div>
      </form>

      {showDropdown && (
        <div
          id={listboxId}
          role="listbox"
          className="absolute z-50 mt-1 max-h-[70vh] w-full min-w-[20rem] overflow-y-auto rounded-lg border border-gray-200 bg-white py-1 shadow-lg"
        >
          {total === 0 && !loading ? (
            <div className="px-3 py-3 text-center text-sm text-gray-500">
              No matches for “{query.trim()}”
            </div>
          ) : (
            GROUPS.map((group) => {
              const hits = results[group.key];
              if (hits.length === 0) return null;
              return (
                <div key={group.key} className="py-1">
                  <div className="px-3 py-1 text-xs font-semibold uppercase tracking-wide text-gray-400">
                    {group.label}
                  </div>
                  {hits.map((hit) => (
                    <button
                      key={`${hit.type}-${hit.id}`}
                      type="button"
                      role="option"
                      aria-selected={false}
                      onClick={() => navigate(hit)}
                      className="flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left transition-colors hover:bg-gray-100"
                    >
                      <span className="truncate text-sm text-gray-900">
                        {hit.label}
                      </span>
                      {hit.sublabel && (
                        <span className="truncate text-xs text-gray-500">
                          {hit.sublabel}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
