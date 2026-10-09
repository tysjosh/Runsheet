"use client";

/**
 * IconButton: an icon-only button whose required `label` becomes both the
 * accessible name and the tooltip, so no icon button ships unnamed (axe
 * `button-name`).
 */
import React from "react";
import { Tooltip } from "./Tooltip";

export interface IconButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> {
  label: string;
  icon: React.ReactNode;
  size?: "sm" | "md" | "lg";
  variant?: "ghost" | "secondary" | "primary";
  /** Hide the tooltip (e.g. inside a menu that already shows the label). */
  noTooltip?: boolean;
}

const SIZE = { sm: "h-7 w-7", md: "h-8 w-8", lg: "h-10 w-10" };
const VARIANT = {
  ghost: "text-slate-700 hover:bg-slate-100",
  secondary:
    "border border-slate-300 bg-surface text-slate-800 hover:bg-slate-50",
  primary: "bg-primary text-on-primary hover:bg-primary-hover",
};

export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
  (
    {
      label,
      icon,
      size = "md",
      variant = "ghost",
      noTooltip = false,
      className = "",
      type = "button",
      ...props
    },
    ref,
  ) => {
    const button = (
      <button
        ref={ref}
        type={type}
        aria-label={label}
        className={`inline-flex shrink-0 items-center justify-center rounded-lg transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 disabled:opacity-50 ${SIZE[size]} ${VARIANT[variant]} ${className}`}
        {...props}
      >
        <span aria-hidden="true" className="inline-flex">
          {icon}
        </span>
      </button>
    );
    return noTooltip ? (
      button
    ) : (
      <Tooltip content={label} describe={false}>
        {button}
      </Tooltip>
    );
  },
);
IconButton.displayName = "IconButton";
