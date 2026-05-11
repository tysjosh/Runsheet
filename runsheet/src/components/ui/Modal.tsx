/**
 * Modal Component - Standardized modal dialog
 * 
 * Provides consistent modal styling across all pages.
 * Replaces multiple modal implementations.
 */

import React, { useEffect } from 'react';
import { X } from 'lucide-react';
import { Button } from './Button';

export interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: 'sm' | 'md' | 'lg' | 'xl';
  className?: string;
}

const sizeStyles = {
  sm: 'max-w-md',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
};

export const Modal: React.FC<ModalProps> = ({
  isOpen,
  onClose,
  title,
  children,
  footer,
  size = 'md',
  className = '',
}) => {
  // Close on escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen, onClose]);

  // Prevent body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = '';
    }
    return () => {
      document.body.style.overflow = '';
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onClose}
    >
      <div
        className={`bg-white rounded-xl shadow-xl w-full ${sizeStyles[size]} mx-4 ${className}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-[#232323]">{title}</h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded transition-colors"
            aria-label="Close modal"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="px-6 py-4 max-h-[calc(100vh-200px)] overflow-y-auto">
          {children}
        </div>

        {/* Footer */}
        {footer && (
          <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-100">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
};

export interface ModalFooterProps {
  onCancel?: () => void;
  onConfirm?: () => void;
  cancelText?: string;
  confirmText?: string;
  confirmVariant?: 'primary' | 'danger' | 'success';
  loading?: boolean;
}

export const ModalFooter: React.FC<ModalFooterProps> = ({
  onCancel,
  onConfirm,
  cancelText = 'Cancel',
  confirmText = 'Confirm',
  confirmVariant = 'primary',
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
