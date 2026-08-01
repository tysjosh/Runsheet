/**
 * PermissionBanner — NEW. The persistent in-app indicators for a denied device
 * permission: notifications (R9.12) and precise location (R10.15).
 *
 * Both banners are persistent by design — there is no dismiss control. A denied
 * notification permission means dispatch alerts never arrive, and a denied
 * location permission means the breadcrumb track has a hole in it; neither is
 * something the driver should be able to hide. The location banner also states
 * what still works, because R10.15 requires status transitions and POD
 * submission to keep going while location sharing is off.
 *
 * The banner takes a permission state as a prop rather than reading
 * `lib/notification-manager.ts` or `lib/location-tracker.ts`, so the display and
 * the permission state machines stay independent. {@link PermissionDecision} is
 * the three-way answer both `expo-notifications` and `expo-location` give.
 *
 * Nothing here is copied or adapted from the donor (R16.21).
 *
 * Requirements: 9.12, 10.15, 16.21
 */

import React from 'react';
import { Linking, View } from 'react-native';
import { BellOff, MapPinOff } from 'lucide-react-native';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Text } from '@/components/ui/text';
import { cn } from '@/lib/utils';

/**
 * A device permission answer. `undetermined` raises no banner — the app can
 * still ask — so only `denied` produces a notice.
 */
export type PermissionDecision = 'granted' | 'denied' | 'undetermined';

/** The two permissions the driver surface depends on. */
export interface DriverPermissionState {
  notifications: PermissionDecision;
  location: PermissionDecision;
}

export type PermissionNoticeId = 'notifications' | 'location';

export interface PermissionNotice {
  id: PermissionNoticeId;
  title: string;
  description: string;
  actionLabel: string;
}

const NOTICES: Record<PermissionNoticeId, Omit<PermissionNotice, 'id'>> = {
  notifications: {
    title: 'Dispatch alerts are disabled',
    description:
      'Notifications are turned off for this device, so new assignments, revocations, and escalations will not alert you. Open your notification settings to turn them back on.',
    actionLabel: 'Open notification settings',
  },
  location: {
    title: 'Location sharing is disabled',
    description:
      'Precise location is turned off for this device, so dispatch cannot see your position or give customers an arrival estimate. You can still change order status and submit proof of delivery.',
    actionLabel: 'Open location settings',
  },
};

/**
 * The notices a permission state calls for, notifications first.
 *
 * A partial state is accepted so a caller can render the banner before both
 * permission checks have resolved; an absent decision raises nothing.
 */
export function deniedPermissionNotices(
  state: Partial<DriverPermissionState>,
): PermissionNotice[] {
  const order: PermissionNoticeId[] = ['notifications', 'location'];
  return order
    .filter((id) => state[id] === 'denied')
    .map((id) => ({ id, ...NOTICES[id] }));
}

export interface PermissionBannerProps {
  permissions: Partial<DriverPermissionState>;
  /**
   * Opens the device settings page for the denied permission. Defaults to
   * `Linking.openSettings()`, which lands on this app's settings on both iOS
   * and Android.
   */
  onOpenSettings?: (id: PermissionNoticeId) => void;
  className?: string;
}

const ICONS: Record<PermissionNoticeId, typeof BellOff> = {
  notifications: BellOff,
  location: MapPinOff,
};

export function PermissionBanner({
  permissions,
  onOpenSettings,
  className,
}: PermissionBannerProps) {
  const notices = deniedPermissionNotices(permissions);

  const handlePress = React.useCallback(
    (id: PermissionNoticeId) => {
      if (onOpenSettings) {
        onOpenSettings(id);
        return;
      }
      void Linking.openSettings();
    },
    [onOpenSettings],
  );

  if (notices.length === 0) {
    return null;
  }

  return (
    <View className={cn('gap-3', className)}>
      {notices.map((notice) => (
        <Alert key={notice.id} variant="destructive" icon={ICONS[notice.id]}>
          <AlertTitle>{notice.title}</AlertTitle>
          <AlertDescription>{notice.description}</AlertDescription>
          <View className="mt-3 pl-7">
            <Button
              variant="outline"
              size="sm"
              onPress={() => handlePress(notice.id)}
              accessibilityLabel={notice.actionLabel}
            >
              <Text>{notice.actionLabel}</Text>
            </Button>
          </View>
        </Alert>
      ))}
    </View>
  );
}
