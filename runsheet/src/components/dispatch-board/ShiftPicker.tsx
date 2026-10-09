/**
 * Shift selector: "All day" plus the tenant's shifts (R2.4, Q2).
 */
import type { BoardShift } from "../../services/dispatchBoardApi";
import type { ShiftView } from "./viewState";

const LABELS: Record<string, string> = { day: "Day", night: "Night" };

export interface ShiftPickerProps {
  value: ShiftView;
  shifts: BoardShift[];
  onChange: (shift: ShiftView) => void;
}

export function ShiftPicker({ value, shifts, onChange }: ShiftPickerProps) {
  return (
    <label className="flex shrink-0 items-center gap-1.5 text-xs font-medium text-text-muted">
      <span className="max-xl:sr-only">Shift</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as ShiftView)}
        className="h-7 max-w-28 rounded-lg border border-slate-300 bg-surface px-1.5 text-xs font-semibold text-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <option value="all">All day</option>
        {shifts.map((s) => (
          <option key={s.id} value={s.id}>
            {LABELS[s.id] ?? s.id} {s.start}–{s.end}
          </option>
        ))}
      </select>
    </label>
  );
}

export default ShiftPicker;
