/**
 * Polite and assertive live regions, mounted once with the board (design
 * K14.6, R19.1, R19.2). `useAnnouncer` returns the two region texts and an
 * `announce` function; a repeated identical message gets a zero-width space
 * so screen readers read it again.
 */
import { useCallback, useRef, useState } from "react";
import { distinctAnnouncement } from "./state/announce";

export type Politeness = "polite" | "assertive";

export interface Announcer {
  polite: string;
  assertive: string;
  announce: (text: string, politeness?: Politeness) => void;
}

export function useAnnouncer(): Announcer {
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");
  const last = useRef({ polite: "", assertive: "" });
  const announce = useCallback(
    (text: string, politeness: Politeness = "polite") => {
      if (!text) return;
      const next = distinctAnnouncement(last.current[politeness], text);
      last.current[politeness] = next;
      if (politeness === "assertive") setAssertive(next);
      else setPolite(next);
    },
    [],
  );
  return { polite, assertive, announce };
}

export function BoardLiveRegion({
  polite,
  assertive,
}: Pick<Announcer, "polite" | "assertive">) {
  return (
    <div className="sr-only">
      <div
        aria-live="polite"
        aria-atomic="true"
        data-testid="board-live-polite"
      >
        {polite}
      </div>
      <div
        aria-live="assertive"
        aria-atomic="true"
        data-testid="board-live-assertive"
      >
        {assertive}
      </div>
    </div>
  );
}

export default BoardLiveRegion;
