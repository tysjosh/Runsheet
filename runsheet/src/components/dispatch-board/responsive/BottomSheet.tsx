/**
 * Bottom sheet for the stacked layout (design K14.8, R20.2): the Modal
 * pattern anchored to the bottom edge. Focus moves in on open, Tab is
 * trapped, Escape and the scrim close it, and focus returns to the opener.
 */
import { X } from "lucide-react";
import { type ReactNode, useId, useRef } from "react";
import { useDialogA11y } from "../../../hooks/useDialogA11y";

export interface BottomSheetProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}

export function BottomSheet({
  isOpen,
  onClose,
  title,
  children,
}: BottomSheetProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useDialogA11y(isOpen, panelRef, onClose);
  if (!isOpen) return null;
  return (
    <div
      className="fixed inset-0 z-40 flex items-end bg-black/30"
      onClick={onClose}
      data-testid="bottom-sheet-scrim"
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="flex max-h-[80vh] w-full flex-col rounded-t-xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-2">
          <h2 id={titleId} className="text-base font-semibold text-gray-900">
            {title}
          </h2>
          <button
            type="button"
            aria-label={`Close ${title.toLowerCase()}`}
            onClick={onClose}
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md text-gray-600 hover:bg-gray-100"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          {children}
        </div>
      </div>
    </div>
  );
}

export default BottomSheet;
