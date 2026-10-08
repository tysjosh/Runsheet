/**
 * Hand-offs to the platform's maps and phone apps (UI revamp R13.4).
 *
 * Navigate opens the stop address with `maps:` on iOS, `geo:` on Android and a
 * Google Maps directions URL on web. The address is what the driver reads on
 * screen, so it is what the maps app searches; coordinates are the fallback
 * when a stop has no address text.
 */

import { Linking, Platform } from 'react-native';

export interface Destination {
  address?: string | null;
  lat?: number | null;
  lon?: number | null;
}

function query(dest: Destination): string | null {
  const address = dest.address?.trim();
  if (address) return address;
  if (Number.isFinite(dest.lat) && Number.isFinite(dest.lon)) {
    return `${dest.lat},${dest.lon}`;
  }
  return null;
}

/** The maps URL for `dest` on `os`, or null when there is nothing to search. */
export function mapsUrl(dest: Destination, os: string = Platform.OS): string | null {
  const q = query(dest);
  if (!q) return null;
  const encoded = encodeURIComponent(q);
  switch (os) {
    case 'ios':
      return `maps:?daddr=${encoded}`;
    case 'android':
      return `geo:0,0?q=${encoded}`;
    default:
      return `https://www.google.com/maps/dir/?api=1&destination=${encoded}`;
  }
}

/** `tel:` URL keeping a leading `+` and the digits, or null when none remain. */
export function telUrl(phone: string | null | undefined): string | null {
  if (!phone) return null;
  const trimmed = phone.trim();
  const digits = trimmed.replace(/\D/g, '');
  if (!digits) return null;
  return `tel:${trimmed.startsWith('+') ? '+' : ''}${digits}`;
}

/** Open a hand-off URL; resolves false when the platform refuses it. */
export async function openHandoff(url: string | null): Promise<boolean> {
  if (!url) return false;
  try {
    await Linking.openURL(url);
    return true;
  } catch {
    return false;
  }
}
