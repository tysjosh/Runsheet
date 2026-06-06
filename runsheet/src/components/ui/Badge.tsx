/**
 * Badge Component - Standardized badge/pill for status indicators
 *
 * Provides consistent badge styling across all pages.
 */

import type React from "react";

export type BadgeVariant =
  | "default"
  | "success"
  | "error"
  | "warning"
  | "info"
  | "neutral";
export type BadgeSize = "sm" | "md";

export interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  size?: BadgeSize;
  className?: string;
  /** Forwarded test hook so callers can target a specific badge. */
  "data-testid"?: string;
}

const variantStyles: Record<BadgeVariant, string> = {
  default: "bg-gray-100 text-gray-800",
  success: "bg-success-light text-success-dark",
  error: "bg-error-light text-error-dark",
  warning: "bg-warning-light text-warning-dark",
  info: "bg-info-light text-info-dark",
  neutral: "bg-gray-50 text-gray-600",
};

const sizeStyles: Record<BadgeSize, string> = {
  sm: "px-2 py-0.5 text-xs",
  md: "px-3 py-1 text-sm",
};

export const Badge: React.FC<BadgeProps> = ({
  children,
  variant = "default",
  size = "md",
  className = "",
  "data-testid": testId,
}) => {
  return (
    <span
      data-testid={testId}
      className={`inline-flex items-center rounded-lg font-medium ${variantStyles[variant]} ${sizeStyles[size]} ${className}`}
    >
      {children}
    </span>
  );
};
