/**
 * Day and night colours for the parts of the app NativeWind classes don't
 * reach: React Navigation's theme (headers, tab bar, back button) and icon
 * `color` props. Every value comes from the generated `lib/tokens.ts` (UI
 * revamp D15), so the brand green and the night palette match `global.css`.
 */

import { DarkTheme, DefaultTheme, type Theme } from '@react-navigation/native';

import { COLOR, DRIVER, SEMANTIC } from './tokens';

export type Scheme = 'light' | 'dark';

export interface DriverPalette {
  canvas: string;
  surface: string;
  text: string;
  textMuted: string;
  border: string;
  primary: string;
  onPrimary: string;
  /** Tab bar active tint (design.md §8: brand.600 by day, brand.500 at night). */
  tint: string;
  /** Inactive tab icons and secondary icons. */
  icon: string;
  danger: string;
}

const NIGHT = DRIVER.night;

export const PALETTE: Record<Scheme, DriverPalette> = {
  light: {
    canvas: '#ffffff',
    surface: SEMANTIC.surface,
    text: SEMANTIC.text,
    textMuted: SEMANTIC['text-muted'],
    border: SEMANTIC.border,
    primary: SEMANTIC.primary,
    onPrimary: SEMANTIC['on-primary'],
    tint: COLOR.brand['600'],
    icon: COLOR.slate['600'],
    danger: COLOR.red['800'],
  },
  dark: {
    canvas: NIGHT.canvas,
    surface: NIGHT.surface,
    text: NIGHT.text,
    textMuted: NIGHT['text-muted'],
    border: NIGHT.border,
    primary: NIGHT.primary,
    onPrimary: NIGHT['on-primary'],
    tint: COLOR.brand['500'],
    icon: NIGHT['text-muted'],
    danger: COLOR.red['300'],
  },
};

export function paletteFor(scheme: string | null | undefined): DriverPalette {
  return scheme === 'dark' ? PALETTE.dark : PALETTE.light;
}

/** React Navigation theme built from the same tokens as `global.css`. */
export function navigationTheme(scheme: string | null | undefined): Theme {
  const dark = scheme === 'dark';
  const base = dark ? DarkTheme : DefaultTheme;
  const p = paletteFor(scheme);
  return {
    ...base,
    colors: {
      ...base.colors,
      primary: p.tint,
      background: p.canvas,
      card: p.surface,
      text: p.text,
      border: p.border,
      notification: p.danger,
    },
  };
}
