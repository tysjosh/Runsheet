"use client";

/**
 * In-shell navigation context.
 *
 * Cross-module reference links (rendered by {@link EntityLink} and a few inline
 * links) normally point at a canonical owning-module route. When the app is
 * running inside the dashboard shell, we'd rather open the target *in-shell*
 * (no full-page navigation, sidebar preserved) for the entity types the shell
 * can host. The shell provides this context; link components consume it and
 * fall back to a real route `<Link>` when there is no provider (e.g. on the
 * standalone routes) or for entity types the shell can't host yet.
 */

import { createContext, useContext } from "react";
import type { EntityType } from "./EntityLink";

export interface InShellNav {
  /** Whether the shell can open this entity type in-shell. */
  handles: (type: EntityType) => boolean;
  /** Open the entity in-shell. Only called for types `handles` returned true. */
  open: (type: EntityType, id: string) => void;
}

const InShellNavContext = createContext<InShellNav | null>(null);

export function InShellNavProvider({
  value,
  children,
}: {
  value: InShellNav;
  children: React.ReactNode;
}) {
  return (
    <InShellNavContext.Provider value={value}>
      {children}
    </InShellNavContext.Provider>
  );
}

/** Returns the in-shell navigator, or null when rendered outside the shell. */
export function useInShellNav(): InShellNav | null {
  return useContext(InShellNavContext);
}
