/**
 * EmptyState Component - Standardized empty state display
 *
 * Provides consistent empty state styling across all pages.
 * Replaces multiple empty state implementations.
 */

import React from "react";
import { Button } from "./Button";

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: {
    label: string;
    onClick: () => void;
  };
  className?: string;
  /** `touch`: a 44 px action button for phone-sized targets (R14.19). Additive (3.11). */
  size?: "default" | "touch";
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  description,
  action,
  className = "",
  size = "default",
}) => {
  const renderedIcon = React.isValidElement<{ className?: string }>(icon)
    ? React.cloneElement(icon, {
        className: `w-16 h-16 ${icon.props.className ?? ""}`.trim(),
      })
    : icon;

  return (
    <div className={`text-center py-16 text-gray-500 ${className}`}>
      {icon && (
        <div className="flex justify-center mb-4 text-gray-300">
          {renderedIcon}
        </div>
      )}
      <p className="text-lg font-medium text-gray-500">{title}</p>
      {description && (
        <p className="text-sm text-gray-500 mt-1">{description}</p>
      )}
      {action && (
        <div className="mt-6">
          <Button
            variant="primary"
            onClick={action.onClick}
            className={size === "touch" ? "min-h-11 px-4" : ""}
          >
            {action.label}
          </Button>
        </div>
      )}
    </div>
  );
};
