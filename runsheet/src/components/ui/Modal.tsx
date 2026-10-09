/**
 * Modal Component - Standardized modal dialog
 *
 * Provides consistent modal styling across all pages.
 * Replaces multiple modal implementations.
 */

import { X } from "lucide-react";
import type React from "react";
import { useEffect, useId, useRef } from "react";
import { useDialogA11y } from "../../hooks/useDialogA11y";
import { Button } from "./Button";

export interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  /** `custom`: no max-width class; pass one in `className`. */
  size?: "sm" | "md" | "lg" | "xl" | "custom";
  className?: string;
  /** Id of the element that describes the dialog (FormDialog's help text). */
  describedById?: string;
  /** Accessible name of the × button (default "Close modal"). */
  closeLabel?: string;
  /** Replaces the body's default padding/max-height classes. */
  bodyClassName?: string;
  /** Rendered between the header and the body (e.g. a stepper). */
  subheader?: React.ReactNode;
  /** Rendered over the panel (e.g. FormDialog's discard prompt). */
  overlay?: React.ReactNode;
  /**
   * `sheet`: below 640 px the panel is a bottom sheet (full width, rounded
   * top corners, no side margin); from 640 px it is the centred dialog.
   * Additive (task 3.11, for the portal's Request delivery dialog).
   */
  mobile?: "sheet";
}

const sizeStyles = {
  sm: "max-w-md",
  md: "max-w-lg",
  lg: "max-w-2xl",
  xl: "max-w-4xl",
  custom: "",
};

export const Modal: React.FC<ModalProps> = ({
  isOpen,
  onClose,
  title,
  children,
  footer,
  size = "md",
  className = "",
  describedById,
  closeLabel = "Close modal",
  bodyClassName = "px-6 py-4 max-h-[calc(100vh-200px)] overflow-y-auto",
  subheader,
  overlay,
  mobile,
}) => {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  // Focus trap, initial focus, Escape-to-close, and focus restoration.
  useDialogA11y(isOpen, panelRef, onClose);

  // Prevent body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      className={`fixed inset-0 z-50 flex justify-center bg-black/30 ${
        mobile === "sheet" ? "items-end sm:items-center" : "items-center"
      }`}
      data-mobile={mobile}
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={describedById}
        tabIndex={-1}
        className={`relative flex flex-col bg-white shadow-xl w-full ${sizeStyles[size]} ${
          mobile === "sheet"
            ? "mx-0 rounded-t-xl sm:mx-4 sm:rounded-xl"
            : "mx-4 rounded-xl"
        } ${className}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 id={titleId} className="text-lg font-semibold text-primary">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-gray-600 rounded transition-colors"
            aria-label={closeLabel}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {subheader}

        {/* Body */}
        <div className={bodyClassName}>{children}</div>

        {/* Footer */}
        {footer && (
          <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-100">
            {footer}
          </div>
        )}
        {overlay}
      </div>
    </div>
  );
};

export interface ModalFooterProps {
  onCancel?: () => void;
  onConfirm?: () => void;
  cancelText?: string;
  confirmText?: string;
  confirmVariant?: "primary" | "danger";
  loading?: boolean;
}

export const ModalFooter: React.FC<ModalFooterProps> = ({
  onCancel,
  onConfirm,
  cancelText = "Cancel",
  confirmText = "Confirm",
  confirmVariant = "primary",
  loading = false,
}) => {
  return (
    <>
      {onCancel && (
        <Button variant="ghost" onClick={onCancel} disabled={loading}>
          {cancelText}
        </Button>
      )}
      {onConfirm && (
        <Button variant={confirmVariant} onClick={onConfirm} loading={loading}>
          {confirmText}
        </Button>
      )}
    </>
  );
};
