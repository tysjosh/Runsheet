/**
 * The one place a driver action asks the device where it is.
 *
 * Every geotagged driver request — POD (R5.8), stop check-in (R6.2), exception
 * report (R7.1) — needs a latitude and a longitude, and the driver may have
 * denied precise location. R10.15 is explicit about what that means: a persistent
 * in-app indicator, and status transitions and POD submission keep working. So
 * this module answers with *what it actually knows*, labelled, and never
 * fabricates a position:
 *
 *   `precise`     a fresh fix from the device
 *   `last_known`  the last fix the platform still holds
 *   `null`        nothing is known — the caller decides what to do about it
 *
 * The POD path substitutes the delivery address's own coordinates when the answer
 * is `null`, and says so on screen, which keeps R10.15 and R5.8 both true without
 * inventing a GPS reading. The check-in path does not substitute: a check-in
 * asserts arrival at a stop, and there is nothing honest to assert without a fix.
 *
 * Requirements: 5.8, 6.2, 10.15
 */

import * as Location from 'expo-location';

import type { PermissionDecision } from '@/components/PermissionBanner';

/** Latitude/longitude as every driver-surface contract declares it. */
export interface GeoFix {
  lat: number;
  lng: number;
}

/** Where a fix came from, so the screen can say what it is. */
export type GeotagSource = 'precise' | 'last_known';

export interface GeotagResult {
  fix: (GeoFix & { source: GeotagSource }) | null;
  permission: PermissionDecision;
}

function decisionOf(status: {
  granted: boolean;
  canAskAgain: boolean;
}): PermissionDecision {
  if (status.granted) {
    return 'granted';
  }
  return status.canAskAgain ? 'undetermined' : 'denied';
}

/** The current foreground location decision, without prompting. */
export async function locationPermissionDecision(): Promise<PermissionDecision> {
  try {
    const status = await Location.getForegroundPermissionsAsync();
    return decisionOf(status);
  } catch {
    return 'denied';
  }
}

/**
 * Best position available right now.
 *
 * @param options.prompt request the permission when it has not been decided yet.
 *   Pass `false` on a screen that only wants to render the indicator.
 */
export async function requestGeotag(
  options: { prompt?: boolean } = {},
): Promise<GeotagResult> {
  const prompt = options.prompt ?? true;
  let permission = await locationPermissionDecision();

  if (permission === 'undetermined' && prompt) {
    try {
      permission = decisionOf(await Location.requestForegroundPermissionsAsync());
    } catch {
      permission = 'denied';
    }
  }

  if (permission === 'granted') {
    try {
      const position = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.Balanced,
      });
      return {
        permission,
        fix: {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          source: 'precise',
        },
      };
    } catch {
      // A granted permission with no fix — indoors, or the radio is warming up.
    }
  }

  try {
    const last = await Location.getLastKnownPositionAsync();
    if (last) {
      return {
        permission,
        fix: {
          lat: last.coords.latitude,
          lng: last.coords.longitude,
          source: 'last_known',
        },
      };
    }
  } catch {
    // Nothing is known. The caller decides.
  }

  return { permission, fix: null };
}
