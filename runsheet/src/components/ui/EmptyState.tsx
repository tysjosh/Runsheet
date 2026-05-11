/**
 * EmptyState Component - Standardized empty state display
 * 
 * Provides consistent empty state styling across all pages.
 * Replaces multiple empty state implementations.
 */

import React from 'react';
import { Button } from './Button';

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
  className?: string;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  description,
  action,
  className = '',
}) => {
  return (
    <div className={`text-center py-16 text-gray-500 ${className}`}>
      {icon && (
        <div className="flex justify-center mb-4 text-gray-300">
          {React.isValidElement(icon)
            ? React.cloneElement(icon as React.ReactElement, {
                className: 'w-16 h-16',
              })
            : icon}
        </div>
      )}
      <p className="text-lg font-medium text-gray-400">{title}</p>
      {description && (
        <p className="text-sm text-gray-400 mt-1">{description}</p>
      )}
      {action && (
        <div className="mt-6">
          <Button variant="primary" onClick={action.onClick}>
            {action.label}
          </Button>
        </div>
      )}
    </div>
  );
};
