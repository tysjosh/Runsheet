/**
 * Board shortcuts (R18.5, R11.4, SC 2.1.4). Registered on the board root's
 * `keydown`. Ignored while focus is in a text input, textarea, select or
 * contenteditable, and while a dialog or menu is open (they handle their own
 * keys). The single-key shortcuts (letters, `/`, `[`, `]`, `?`) can be turned
 * off; Cmd/Ctrl+Z and Escape always work.
 */
import type { KeyboardEvent } from "react";
import type { BoardApi } from "../BoardContext";
import type { BoardItem } from "../intents";

export interface ShortcutActions {
  api: BoardApi | null;
  singleKey: boolean;
  focusSearch: () => void;
  prevDay: () => void;
  nextDay: () => void;
  toggleZoom: () => void;
  openHelp: () => void;
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
}

export function isTextInput(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "textarea" || tag === "select") return true;
  if (tag === "input") {
    const type = (el as HTMLInputElement).type;
    return !["checkbox", "radio", "button", "submit", "reset"].includes(type);
  }
  return el.isContentEditable || el.getAttribute("contenteditable") === "true";
}

/** The selection as one item, if every selected key has the same kind. */
export function selectionItem(api: BoardApi): BoardItem | null {
  const keys = api.state.selection;
  if (keys.length === 0) return null;
  const kind = keys[0].split(":")[0] as BoardItem["kind"];
  if (!keys.every((k) => k.startsWith(`${kind}:`))) return null;
  const ids = keys.map((k) => k.slice(kind.length + 1));
  return {
    kind,
    ids,
    fromTruckId: api.state.placeMode?.fromTruckId ?? null,
  };
}

/** Handles one keydown; returns true when it was a shortcut. */
export function handleShortcut(
  e: KeyboardEvent<HTMLElement>,
  a: ShortcutActions,
): boolean {
  if (e.defaultPrevented || isTextInput(e.target)) return false;
  const target = e.target as HTMLElement;
  if (target.closest?.('[role="dialog"], [role="menu"]')) return false;
  const mod = e.metaKey || e.ctrlKey;
  const key = e.key;

  if (mod && key.toLowerCase() === "z") {
    e.preventDefault();
    if (e.shiftKey) {
      if (a.canRedo) a.redo();
    } else if (a.canUndo) a.undo();
    return true;
  }
  if (key === "Escape") {
    if (a.api?.placeItem || (a.api?.state.selection.length ?? 0) > 0) {
      e.preventDefault();
      a.api?.clearSelection();
      a.api?.announce("Selection cleared.");
      return true;
    }
    return false;
  }
  if (mod || e.altKey || !a.singleKey) return false;

  const api = a.api;
  const sel = api ? selectionItem(api) : null;
  const need = (what: string) => {
    api?.announce(`Select ${what} first.`);
    return true;
  };

  switch (key) {
    case "/":
      e.preventDefault();
      a.focusSearch();
      return true;
    case "?":
      e.preventDefault();
      a.openHelp();
      return true;
    case "[":
      e.preventDefault();
      a.prevDay();
      return true;
    case "]":
      e.preventDefault();
      a.nextDay();
      return true;
    case "t":
    case "T":
      e.preventDefault();
      a.toggleZoom();
      return true;
    case "a":
    case "A":
      e.preventDefault();
      if (!api || !sel || (sel.kind !== "order" && sel.kind !== "stop"))
        return need("an order");
      api.openAssign(sel.kind === "order" ? "assign" : "move", sel);
      return true;
    case "m":
    case "M":
      e.preventDefault();
      if (!api || !sel) return need("a stop or load");
      if (sel.kind === "load") api.openAssign("load", sel);
      else if (sel.kind === "stop") api.openAssign("move", sel);
      else if (sel.kind === "order") api.openAssign("assign", sel);
      else return need("a stop or load");
      return true;
    case "p":
    case "P":
      e.preventDefault();
      if (!api || !sel || sel.kind !== "driver") return need("a driver");
      api.openAssign("pair", sel);
      return true;
    case "u":
    case "U":
      e.preventDefault();
      if (!api || !sel || sel.kind !== "stop") return need("a stop");
      api.perform(sel, { target: "tray" }, "keyboard");
      return true;
    default:
      return false;
  }
}
