/**
 * Keyboard shortcut help (R18.5, SC 2.1.4). Lists every shortcut and holds
 * the "Single-key shortcuts" setting (default on), which turns the letter
 * and symbol keys off. Opened by the toolbar button or "?"; the Modal
 * returns focus to whatever opened it.
 */
import { Modal } from "../../ui";

/** R18.5 shortcuts plus the grid keys (K14.5). */
export const SHORTCUTS: [string, string][] = [
  ["/", "Search"],
  ["A", "Assign selected"],
  ["M", "Move selected"],
  ["P", "Pair driver"],
  ["U", "Unassign"],
  ["[ / ]", "Previous / next day"],
  ["T", "Timeline / sequence"],
  ["?", "Show this help"],
  ["Cmd/Ctrl + Z", "Undo"],
  ["Shift + Cmd/Ctrl + Z", "Redo"],
  ["Esc", "Cancel"],
];

export const GRID_KEYS: [string, string][] = [
  ["Up / Down", "Previous / next truck, or card in a tray"],
  ["Left / Right", "Previous / next card on a truck"],
  ["Home / End", "Truck header / last card"],
  ["Enter", "Select a card (Place mode), or place the selection"],
  ["Space", "Add a card to the selection"],
  ["Shift + F10", "Card actions menu"],
];

export const SINGLE_KEY_STORAGE_KEY = "runsheet.dispatchBoard.singleKey.v1";

export function readSingleKeyEnabled(): boolean {
  try {
    return window.localStorage.getItem(SINGLE_KEY_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

export function writeSingleKeyEnabled(on: boolean): void {
  try {
    window.localStorage.setItem(SINGLE_KEY_STORAGE_KEY, on ? "on" : "off");
  } catch {
    // Storage blocked: the setting lasts for this page.
  }
}

export interface ShortcutHelpDialogProps {
  isOpen: boolean;
  onClose: () => void;
  singleKey: boolean;
  onSingleKeyChange: (on: boolean) => void;
}

function KeyList({ rows, label }: { rows: [string, string][]; label: string }) {
  return (
    <dl
      aria-label={label}
      className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm"
    >
      {rows.map(([key, text]) => (
        <div key={key} className="contents">
          <dt>
            <kbd className="rounded border border-gray-300 bg-gray-50 px-1.5 py-0.5 font-mono text-xs">
              {key}
            </kbd>
          </dt>
          <dd className="text-gray-700">{text}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ShortcutHelpDialog({
  isOpen,
  onClose,
  singleKey,
  onSingleKeyChange,
}: ShortcutHelpDialogProps) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Keyboard shortcuts"
      size="sm"
    >
      <div className="space-y-4">
        <label className="flex min-h-9 items-center gap-2 text-sm text-gray-800">
          <input
            type="checkbox"
            checked={singleKey}
            onChange={(e) => onSingleKeyChange(e.target.checked)}
            className="h-4 w-4"
          />
          Single-key shortcuts
        </label>
        <KeyList rows={SHORTCUTS} label="Shortcuts" />
        <h3 className="text-sm font-semibold text-gray-900">On the board</h3>
        <KeyList rows={GRID_KEYS} label="Board keys" />
      </div>
    </Modal>
  );
}

export default ShortcutHelpDialog;
