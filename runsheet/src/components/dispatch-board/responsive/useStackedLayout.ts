/**
 * Responsive layout switch (design K14.8, R20.2). Below 1024 px the board is
 * stacked: lanes as a vertical list in Sequence layout, the trays in a bottom
 * sheet, the detail drawer full screen and the Place mode banner pinned to
 * the bottom.
 */
import { useCallback, useSyncExternalStore } from "react";

/** Matches viewports narrower than 1024 CSS px. */
export const STACKED_QUERY = "(max-width: 1023.98px)";

function mediaList(query: string): MediaQueryList | null {
  if (typeof window === "undefined" || !window.matchMedia) return null;
  return window.matchMedia(query);
}

/** `true` while `query` matches; `false` on the server and without `matchMedia`. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = mediaList(query);
      if (!list?.addEventListener) return () => {};
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => mediaList(query)?.matches ?? false,
    () => false,
  );
}

export function useStackedLayout(): boolean {
  return useMediaQuery(STACKED_QUERY);
}
