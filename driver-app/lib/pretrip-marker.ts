/**
 * "Has this driver queued a pre-trip inspection today?" — a device-local marker
 * for the Work screen's DVIR prompt (UI revamp R13.3).
 *
 * The backend has no read endpoint for a driver's inspections, so the app can't
 * ask the server. This marker records the calendar day (`localCalendarDay`, the
 * same day the report carries) when a pre-trip is queued on this device. It
 * only decides whether to *offer* the inspection form; it never blocks a duty
 * change, and the server's own pre-trip gate (409 PRETRIP_INSPECTION_REQUIRED on
 * the day's first `in_transit`) is unchanged. Escalation PE-D1 in
 * `phase4-evidence.md` asks for a server read so other devices count too.
 */

import { MMKV } from 'react-native-mmkv';

const STORAGE_ID = 'runsheet-pretrip-marker';
const KEY = 'last_pretrip_day';

interface MarkerStore {
  getString(key: string): string | undefined;
  set(key: string, value: string): void;
  delete(key: string): void;
}

let store: MarkerStore | null = null;

function resolveStore(): MarkerStore {
  if (!store) {
    try {
      store = new MMKV({ id: STORAGE_ID });
    } catch {
      // Jest and the web preview have no native MMKV; memory is enough there.
      const map = new Map<string, string>();
      store = {
        getString: (key) => map.get(key),
        set: (key, value) => {
          map.set(key, value);
        },
        delete: (key) => {
          map.delete(key);
        },
      };
    }
  }
  return store;
}

/** Record that a pre-trip was queued on `day` (`YYYY-MM-DD`). */
export function markPretripQueued(day: string): void {
  resolveStore().set(KEY, day);
}

/** True when a pre-trip was queued on this device on `day`. */
export function pretripQueuedOn(day: string): boolean {
  return resolveStore().getString(KEY) === day;
}

/** Forget the marker (sign-out, tests). */
export function forgetPretripMarker(): void {
  resolveStore().delete(KEY);
}
