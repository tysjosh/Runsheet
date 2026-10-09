/**
 * Button: the one button for the staff app (design.md §3).
 *
 * Variants: primary (brand green, white text 5.1:1), secondary (outlined),
 * ghost (text button), danger (red.600, white 4.8:1) and link. The old
 * `success` and `warning` variants failed AA with white text and were removed;
 * their callers use `primary` or `secondary`.
 *
 * Sizes are fixed heights: sm 28 px, md 32 px, lg 40 px.
 */
import React from "react";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "danger"
  | "ghost"
  | "link";
export type ButtonSize = "sm" | "md" | "lg";
export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
  icon?: React.ReactNode;
  loading?: boolean;
}

const variantStyles: Record<ButtonVariant, string> = {
  primary:
    "bg-primary text-on-primary hover:bg-primary-hover disabled:opacity-50 border border-transparent",
  secondary:
    "bg-surface text-slate-800 border border-slate-300 hover:bg-slate-50 disabled:opacity-50",
  danger:
    "bg-danger text-white hover:bg-danger-hover disabled:opacity-50 border border-transparent",
  ghost:
    "bg-transparent text-slate-700 hover:bg-slate-100 disabled:opacity-50 border border-transparent",
  link: "bg-transparent text-link underline-offset-2 hover:underline disabled:opacity-50 !px-0 border border-transparent",
};

const sizeStyles: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs",
  md: "h-8 px-3 text-sm",
  lg: "h-10 px-4 text-sm",
};

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      className={`animate-spin ${className}`}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = "primary",
      size = "md",
      fullWidth = false,
      icon,
      loading = false,
      className = "",
      children,
      disabled,
      ...props
    },
    ref,
  ) => {
    const baseStyles =
      "inline-flex items-center justify-center gap-1.5 whitespace-nowrap font-medium rounded-lg transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-focus";
    const widthStyles = fullWidth ? "w-full" : "";
    return (
      <button
        ref={ref}
        className={`${baseStyles} ${variantStyles[variant]} ${sizeStyles[size]} ${widthStyles} ${className}`}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        {...props}
      >
        {loading ? <Spinner /> : icon}
        {children}
      </button>
    );
  },
);
Button.displayName = "Button";
