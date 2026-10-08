"use client";

/**
 * Tabs: hub tabs or views, URL-synced (R2.4).
 *
 * `useUrlTab` reads `?tab=` (or another param), restores it on load, maps
 * legacy aliases (e.g. `scheduling` → `jobs`), falls back to the first
 * visible tab for unknown values, and writes changes with `router.replace`
 * (no history entry per click). Other query params are preserved.
 *
 * `<Tabs>` renders the WAI-ARIA tablist: automatic activation, Left/Right,
 * Home/End. It is controlled by `value`/`onChange`; without them it binds to
 * the URL itself.
 */
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type ReactNode, useCallback, useId, useMemo, useRef } from "react";

export interface TabItem {
  id: string;
  label: string;
  icon?: ReactNode;
  /** A count or badge after the label. */
  badge?: ReactNode;
  hidden?: boolean;
}

export interface UrlTabOptions {
  param?: string;
  aliases?: Record<string, string>;
  /** Value used when the URL has none (defaults to the first id). */
  fallback?: string;
}

/** Resolve a raw URL value against the visible ids. Exported for tests. */
export function resolveTab(
  raw: string | null | undefined,
  ids: string[],
  { aliases = {}, fallback }: Omit<UrlTabOptions, "param"> = {},
): string {
  const first = fallback && ids.includes(fallback) ? fallback : ids[0];
  if (!raw) return first ?? "";
  const v = aliases[raw] ?? raw;
  return ids.includes(v) ? v : (first ?? "");
}

export function useUrlTab(
  ids: string[],
  { param = "tab", aliases, fallback }: UrlTabOptions = {},
): [string, (id: string) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const raw = searchParams?.get(param) ?? null;
  const key = ids.join("|");
  const active = useMemo(
    () => resolveTab(raw, ids, { aliases, fallback }),
    [raw, key, aliases, fallback],
  );
  const setActive = useCallback(
    (id: string) => {
      const params = new URLSearchParams(searchParams?.toString() ?? "");
      params.set(param, id);
      router.replace(`${pathname ?? ""}?${params.toString()}`, {
        scroll: false,
      });
    },
    [router, pathname, searchParams, param],
  );
  return [active, setActive];
}

export interface TabsProps {
  tabs: TabItem[];
  value?: string;
  onChange?: (id: string) => void;
  param?: string;
  aliases?: Record<string, string>;
  variant?: "segmented" | "underline";
  label?: string;
  /** Id prefix for `aria-controls` → `${idBase}-panel`. */
  idBase?: string;
  className?: string;
}

function UrlTabs(props: TabsProps) {
  const visible = props.tabs.filter((t) => !t.hidden).map((t) => t.id);
  const [value, setValue] = useUrlTab(visible, {
    param: props.param,
    aliases: props.aliases,
  });
  return (
    <TabList
      {...props}
      value={value}
      onChange={(id) => {
        setValue(id);
        props.onChange?.(id);
      }}
    />
  );
}

export function Tabs(props: TabsProps) {
  if (props.value === undefined) return <UrlTabs {...props} />;
  return <TabList {...props} value={props.value} />;
}

function TabList({
  tabs,
  value,
  onChange,
  variant = "segmented",
  label = "Views",
  idBase,
  className = "",
}: TabsProps & { value: string }) {
  const auto = useId();
  const base = idBase ?? `tabs-${auto}`;
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const visible = tabs.filter((t) => !t.hidden);

  const focusAt = (i: number) => {
    const t = visible[(i + visible.length) % visible.length];
    if (!t) return;
    refs.current[t.id]?.focus();
    onChange?.(t.id);
  };

  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      focusAt(i + 1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      focusAt(i - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      focusAt(0);
    } else if (e.key === "End") {
      e.preventDefault();
      focusAt(visible.length - 1);
    }
  };

  const segmented = variant === "segmented";
  return (
    <div
      role="tablist"
      aria-label={label}
      className={
        segmented
          ? `inline-flex h-7 shrink-0 items-stretch overflow-hidden rounded-lg border border-slate-200 bg-surface ${className}`
          : `flex h-full shrink-0 items-stretch gap-1 ${className}`
      }
    >
      {visible.map((t, i) => {
        const on = t.id === value;
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`${base}-tab-${t.id}`}
            aria-selected={on}
            aria-controls={idBase ? `${idBase}-panel` : undefined}
            tabIndex={on ? 0 : -1}
            data-tab={t.id}
            onClick={() => onChange?.(t.id)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={
              segmented
                ? `inline-flex items-center gap-1.5 whitespace-nowrap px-2.5 text-xs font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus ${
                    i > 0 ? "border-l border-slate-200" : ""
                  } ${
                    on
                      ? "bg-primary-soft text-brand-800 shadow-[inset_0_-2px_0_var(--rs-primary)]"
                      : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                  }`
                : `inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-2 text-sm font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                    on
                      ? "border-primary text-brand-800"
                      : "border-transparent text-slate-600 hover:text-slate-900"
                  }`
            }
          >
            {t.icon && (
              <span
                aria-hidden="true"
                className="inline-flex [&>svg]:h-3.5 [&>svg]:w-3.5"
              >
                {t.icon}
              </span>
            )}
            {t.label}
            {t.badge}
          </button>
        );
      })}
    </div>
  );
}

/**
 * The panel for a `Tabs` with `idBase`: `role="tabpanel"`, labelled by the
 * active tab. Scrollable panels are focusable (axe scrollable-region-focusable).
 */
export function TabPanel({
  idBase,
  value,
  children,
  className = "",
}: {
  idBase: string;
  value: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role="tabpanel"
      id={`${idBase}-panel`}
      aria-labelledby={`${idBase}-tab-${value}`}
      className={className}
    >
      {children}
    </div>
  );
}
