/**
 * An open agent suggestion drawn as a ghost load on its truck's lane
 * (R16.1). It is a grid cell button: activating it opens the suggestion
 * review with the why panel and the diff (R16.2).
 */
import { Sparkles } from "lucide-react";
import type { BoardSuggestion } from "../../../services/dispatchBoardApi";
import { useBoard } from "../BoardContext";
import { useRovingItem } from "../keyboard/roving";
import { suggestionLoads } from "./suggestionModel";

export const GHOST_WIDTH = 148;

export function GhostLoad({
  suggestion,
  left,
}: {
  suggestion: BoardSuggestion;
  left: number;
}) {
  const api = useBoard();
  const roving = useRovingItem(`suggestion:${suggestion.suggestion_id}`);
  const stops = suggestionLoads(suggestion).reduce(
    (n, l) => n + l.orderIds.length,
    0,
  );
  return (
    <div
      role="gridcell"
      tabIndex={-1}
      className="absolute inset-y-0"
      style={{ left, width: GHOST_WIDTH }}
    >
      <button
        type="button"
        {...roving}
        data-ghost-load={suggestion.suggestion_id}
        aria-label={`Suggested load from ${suggestion.agent_name}, ${stops} ${stops === 1 ? "stop" : "stops"}. Review`}
        onClick={(e) => {
          e.stopPropagation();
          api.openSuggestions({
            truckId: suggestion.truck_id,
            suggestionId: suggestion.suggestion_id,
          });
        }}
        className="flex h-full w-full flex-col items-start justify-center gap-0.5 rounded-md border-2 border-dashed border-fuchsia-600 bg-white px-2 text-left text-[11px] leading-tight text-fuchsia-800 outline-none hover:bg-fuchsia-50 focus-visible:ring-2 focus-visible:ring-primary"
      >
        <span className="flex items-center gap-1 font-semibold">
          <Sparkles className="h-3 w-3" aria-hidden="true" />
          Suggested load
        </span>
        <span>
          {stops} {stops === 1 ? "stop" : "stops"} · Review
        </span>
      </button>
    </div>
  );
}

export default GhostLoad;
