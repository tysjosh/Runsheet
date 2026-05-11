/**
 * Design Tokens - Single source of truth for design values
 * 
 * These tokens define the visual language of the application.
 * All components should reference these tokens instead of hardcoded values.
 */

export const colors = {
  // Primary brand color
  primary: {
    DEFAULT: '#232323',
    hover: '#1a1a1a',
    light: '#404040',
  },
  
  // Semantic colors
  success: {
    DEFAULT: '#10B981',
    light: '#D1FAE5',
    dark: '#065F46',
  },
  error: {
    DEFAULT: '#EF4444',
    light: '#FEE2E2',
    dark: '#991B1B',
  },
  warning: {
    DEFAULT: '#F59E0B',
    light: '#FEF3C7',
    dark: '#92400E',
  },
  info: {
    DEFAULT: '#3B82F6',
    light: '#DBEAFE',
    dark: '#1E40AF',
  },
  
  // Neutral grays
  gray: {
    50: '#F9FAFB',
    100: '#F3F4F6',
    200: '#E5E7EB',
    300: '#D1D5DB',
    400: '#9CA3AF',
    500: '#6B7280',
    600: '#4B5563',
    700: '#374151',
    800: '#1F2937',
    900: '#111827',
  },
} as const;

export const spacing = {
  // Base spacing scale (4px increments)
  0: '0',
  1: '0.25rem',  // 4px
  2: '0.5rem',   // 8px
  3: '0.75rem',  // 12px
  4: '1rem',     // 16px
  5: '1.25rem',  // 20px
  6: '1.5rem',   // 24px
  8: '2rem',     // 32px
  10: '2.5rem',  // 40px
  12: '3rem',    // 48px
  16: '4rem',    // 64px
  20: '5rem',    // 80px
  24: '6rem',    // 96px
} as const;

export const borderRadius = {
  none: '0',
  sm: '0.25rem',   // 4px
  DEFAULT: '0.5rem', // 8px
  md: '0.75rem',   // 12px
  lg: '1rem',      // 16px
  xl: '1.5rem',    // 24px
  full: '9999px',
} as const;

export const fontSize = {
  xs: ['0.75rem', { lineHeight: '1rem' }],      // 12px
  sm: ['0.875rem', { lineHeight: '1.25rem' }],  // 14px
  base: ['1rem', { lineHeight: '1.5rem' }],     // 16px
  lg: ['1.125rem', { lineHeight: '1.75rem' }],  // 18px
  xl: ['1.25rem', { lineHeight: '1.75rem' }],   // 20px
  '2xl': ['1.5rem', { lineHeight: '2rem' }],    // 24px
  '3xl': ['1.875rem', { lineHeight: '2.25rem' }], // 30px
} as const;

export const fontWeight = {
  normal: '400',
  medium: '500',
  semibold: '600',
  bold: '700',
} as const;

export const shadows = {
  sm: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
  DEFAULT: '0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1)',
  md: '0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)',
  lg: '0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)',
  xl: '0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)',
} as const;

export const transitions = {
  fast: '150ms cubic-bezier(0.4, 0, 0.2, 1)',
  DEFAULT: '200ms cubic-bezier(0.4, 0, 0.2, 1)',
  slow: '300ms cubic-bezier(0.4, 0, 0.2, 1)',
} as const;

export const zIndex = {
  dropdown: 1000,
  sticky: 1020,
  fixed: 1030,
  modalBackdrop: 1040,
  modal: 1050,
  popover: 1060,
  tooltip: 1070,
} as const;

// Layout-specific tokens
export const layout = {
  // Page container padding
  page: {
    desktop: { x: spacing[8], y: spacing[6] },
    mobile: { x: spacing[4], y: spacing[4] },
  },
  // Section padding
  section: {
    x: spacing[8],
    y: spacing[4],
  },
  // Table cell padding
  table: {
    header: { x: spacing[6], y: spacing[4] },
    cell: { x: spacing[6], y: spacing[4] },
    compact: { x: spacing[3], y: spacing[2] },
  },
  // Modal padding
  modal: {
    header: { x: spacing[6], y: spacing[4] },
    body: { x: spacing[6], y: spacing[4] },
  },
} as const;
