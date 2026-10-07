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
    <label className="flex items-center gap-2 text-sm text-gray-700">
      <span>Shift</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as ShiftView)}
        className="min-h-9 rounded-md border border-gray-300 bg-white px-2 py-1 text-sm"
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
