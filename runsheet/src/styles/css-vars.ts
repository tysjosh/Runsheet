/**
 * CSS Variable References
 *
 * Use these to reference CSS variables defined in globals.css
 * This provides type safety and autocomplete for CSS variables.
 */

export const cssVars = {
  // Colors
  color: {
    primary: "var(--color-primary)",
    primaryHover: "var(--color-primary-hover)",
    primaryLight: "var(--color-primary-light)",
    primarySoft: "var(--color-primary-soft)",

    ink: "var(--color-ink)",
    brandAccent: "var(--color-brand-accent)",
    brandAccentSoft: "var(--color-brand-accent-soft)",
    brandSecondary: "var(--color-brand-secondary)",
    brandSecondarySoft: "var(--color-brand-secondary-soft)",

    success: "var(--color-success)",
    successLight: "var(--color-success-light)",
    successDark: "var(--color-success-dark)",

    error: "var(--color-error)",
    errorLight: "var(--color-error-light)",
    errorDark: "var(--color-error-dark)",

    warning: "var(--color-warning)",
    warningLight: "var(--color-warning-light)",
    warningDark: "var(--color-warning-dark)",

    info: "var(--color-info)",
    infoLight: "var(--color-info-light)",
    infoDark: "var(--color-info-dark)",

    gray: {
      50: "var(--color-gray-50)",
      100: "var(--color-gray-100)",
      200: "var(--color-gray-200)",
      300: "var(--color-gray-300)",
      400: "var(--color-gray-400)",
      500: "var(--color-gray-500)",
      600: "var(--color-gray-600)",
      700: "var(--color-gray-700)",
      800: "var(--color-gray-800)",
      900: "var(--color-gray-900)",
    },

    surface: {
      default: "var(--color-surface)",
      muted: "var(--color-surface-muted)",
      subtle: "var(--color-surface-subtle)",
    },
  },

  // Border Radius
  radius: {
    sm: "var(--radius-sm)",
    default: "var(--radius)",
    md: "var(--radius-md)",
    lg: "var(--radius-lg)",
    xl: "var(--radius-xl)",
  },

  // Shadows
  shadow: {
    sm: "var(--shadow-sm)",
    default: "var(--shadow)",
    md: "var(--shadow-md)",
    lg: "var(--shadow-lg)",
    xl: "var(--shadow-xl)",
  },

  // Transitions
  transition: {
    fast: "var(--transition-fast)",
    default: "var(--transition)",
    slow: "var(--transition-slow)",
  },
} as const;
