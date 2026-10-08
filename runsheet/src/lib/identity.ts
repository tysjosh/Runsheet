/**
 * Stable identity colours for trucks, drivers and routes (design.md §2.3).
 *
 * `identityFor(id)` hashes the id (FNV-1a, 32-bit) onto the 8 identity
 * colours, so a truck keeps its colour across sessions and pages.
 * `assignLaneIdentities(ids)` walks lanes in display order and, when a lane's
 * colour would equal its neighbour's or form a pair that merges under
 * deuteranopia (`IDENTITY_ADJACENCY_AVOID`), takes the next free colour. The
 * result is stable for a given lane order.
 */
import { IDENTITY, IDENTITY_ADJACENCY_AVOID } from "../styles/tokens";

export interface IdentityToken {
  index: number;
  token: string;
  hex: string;
}

export function fnv1a(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

const tokenAt = (index: number): IdentityToken => ({
  index,
  token: IDENTITY[index].token,
  hex: IDENTITY[index].hex,
});

function clashes(a: number, b: number | undefined): boolean {
  if (b === undefined) return false;
  if (a === b) return true;
  const ta = IDENTITY[a].token;
  const tb = IDENTITY[b].token;
  return IDENTITY_ADJACENCY_AVOID.some(
    ([x, y]) => (x === ta && y === tb) || (x === tb && y === ta),
  );
}

/**
 * Identity colour for one id. With `neighbours` (ids already shown next to
 * it), a clashing colour moves to the next free one.
 */
export function identityFor(
  id: string,
  neighbours: string[] = [],
): IdentityToken {
  const n = IDENTITY.length;
  const start = fnv1a(id) % n;
  const taken = neighbours.map((nb) => fnv1a(nb) % n);
  for (let step = 0; step < n; step++) {
    const i = (start + step) % n;
    if (!taken.some((t) => clashes(i, t))) return tokenAt(i);
  }
  return tokenAt(start);
}

/** Colours for lanes in display order; adjacent lanes never clash. */
export function assignLaneIdentities(
  laneIds: string[],
): Map<string, IdentityToken> {
  const n = IDENTITY.length;
  const out = new Map<string, IdentityToken>();
  let prev: number | undefined;
  for (const id of laneIds) {
    const start = fnv1a(id) % n;
    let chosen = start;
    for (let step = 0; step < n; step++) {
      const i = (start + step) % n;
      if (!clashes(i, prev)) {
        chosen = i;
        break;
      }
    }
    out.set(id, tokenAt(chosen));
    prev = chosen;
  }
  return out;
}

/** Up to two initials: "Darnell Price" → "DP", "TRK-104" → "T1". */
export function initials(label: string): string {
  const words = label
    .replace(/[^\p{L}\p{N}\s-]/gu, " ")
    .split(/[\s-]+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}
