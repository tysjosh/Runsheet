import React from 'react';

interface LoadingSpinnerProps {
  /** Size of the spinner: 'sm' (16px), 'md' (32px), 'lg' (48px) */
  size?: 'sm' | 'md' | 'lg';
  /** Loading message to display below the spinner */
  message?: string;
  /** Whether to show the spinner in a full-height container */
  fullHeight?: boolean;
  /** Custom class name for additional styling */
  className?: string;
}

/**
 * LoadingSpinner component for displaying loading indicators during API calls.
 * Requirement 9.3: Loading indicators must be displayed during API calls.
 */
export default function LoadingSpinner({
  size = 'md',
  message,
  fullHeight = true,
  className = '',
}: LoadingSpinnerProps) {
  const sizeClasses = {
    sm: 'h-4 w-4',
    md: 'h-8 w-8',
    lg: 'h-12 w-12',
  };

  const containerClasses = fullHeight
    ? 'h-full flex items-center justify-center'
    : 'flex items-center justify-center py-8';

  return (
    <div className={`${containerClasses} ${className}`}>
      <div className="text-center">
        <div
          className={`animate-spin rounded-full border-b-2 border-[#232323] mx-auto ${sizeClasses[size]}`}
          role="status"
          aria-label="Loading"
        />
        {message && (
          <p className="mt-2 text-gray-600 text-sm">{message}</p>
        )}
      </div>
    </div>
  );
}

/**
 * Inline loading spinner for use within buttons or inline elements
 */
export function InlineSpinner({ className = '' }: { className?: string }) {
  return (
    <div
      className={`animate-spin rounded-full h-4 w-4 border-2 border-current border-t-transparent ${className}`}
      role="status"
      aria-label="Loading"
    />
  );
}
