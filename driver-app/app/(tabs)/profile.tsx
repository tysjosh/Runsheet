/**
 * Driver profile — qualification file, permissions, session.
 *
 * Duty status and the Hours-of-Service advisory moved to the Work screen
 * (UI revamp D16, R13.8: `components/DutyStatusControl.tsx`), with the same
 * queries and mutations. What stays here is what a driver checks rarely.
 *
 * The qualification file carries the compliance vocabulary
 * (`compliance/models/driver.py:34`: `active | suspended | expired`), which never
 * shares a card, a label helper, or a query key with duty status (R12.7). Every
 * expiry threshold and every banner line comes from `lib/qualification-api.ts`;
 * this screen classifies nothing (R12.3, R12.4, R12.5).
 *
 * Requirements: 12.3, 12.4, 12.5, 12.7, 9.12, 10.15, 16.13
 */

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { ShieldAlert } from 'lucide-react-native';
import { useEffect, useState, useSyncExternalStore } from 'react';
import { RefreshControl, ScrollView, View } from 'react-native';

import {
  PermissionBanner,
  type PermissionDecision,
} from '@/components/PermissionBanner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Text } from '@/components/ui/text';
import { ApiError } from '@/lib/api-client';
import { forgetDutyStatus, loadDriverIdentity } from '@/lib/duty-api';
import { locationPermissionDecision } from '@/lib/geotag';
import { locationTracker } from '@/lib/location-tracker';
import { notificationManager } from '@/lib/notification-manager';
import { forgetPretripMarker } from '@/lib/pretrip-marker';
import {
  formatQualificationDate,
  ineligibilityReasons,
  loadDriverQualifications,
  qualificationItems,
  qualificationStatusLabel,
  type QualificationItem,
} from '@/lib/qualification-api';
import { queryKeys } from '@/lib/query-keys';
import { forgetCompartmentAcknowledgements } from '@/lib/route-api';
import { signOut } from '@/lib/session';
import { driverWebSocket } from '@/lib/websocket';

/**
 * One qualification item, rendered with the indicator its tier calls for.
 *
 * The advisory tier (R12.3) and the urgent tier (R12.4) both show the days
 * remaining, differing in emphasis; an item further out than 60 days shows its
 * date and no indicator; an expired one shows that it has expired rather than a
 * countdown. The tier and the wording arrive already decided on `item` — this
 * component compares no dates.
 */
function QualificationRow({ item }: { item: QualificationItem }) {
  const severe = item.urgency === 'urgent' || item.urgency === 'expired';
  return (
    <View className="gap-1 rounded-xl border border-input p-3">
      <View className="flex-row items-start justify-between gap-2">
        <Text className="flex-1 font-semibold">{item.label}</Text>
        {item.urgency === 'expired' && (
          <Badge variant="destructive">
            <Text>Expired</Text>
          </Badge>
        )}
        {item.indicatorLabel && (
          <Badge variant={severe ? 'destructive' : 'secondary'}>
            <Text>{item.daysRemaining}d</Text>
          </Badge>
        )}
      </View>
      <Text className="text-sm text-muted-foreground">
        {item.expiryDate
          ? `Expires ${formatQualificationDate(item.expiryDate)}`
          : 'Not on file'}
      </Text>
      {item.indicatorLabel && (
        <Text
          className={
            severe
              ? 'text-sm font-semibold text-destructive'
              : 'text-sm text-foreground'
          }
        >
          {item.indicatorLabel}
        </Text>
      )}
    </View>
  );
}

export default function ProfileScreen() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const notifications = useSyncExternalStore(
    (listener) => notificationManager.subscribe(listener),
    () => notificationManager.getSnapshot(),
  );
  const [locationPermission, setLocationPermission] =
    useState<PermissionDecision>('undetermined');
  const [signingOut, setSigningOut] = useState(false);
  const me = useQuery({
    queryKey: queryKeys.me(),
    queryFn: loadDriverIdentity,
  });

  // R12.1 — the read carries no identifier; the server scopes it to the session.
  const qualifications = useQuery({
    queryKey: queryKeys.dqf(),
    queryFn: loadDriverQualifications,
  });

  useEffect(() => {
    void locationPermissionDecision().then(setLocationPermission);
  }, []);

  const exit = async () => {
    setSigningOut(true);
    driverWebSocket.disconnect();
    // R10.10 — a signed-out device is not on duty, so it samples nothing.
    await locationTracker.shutdown();
    await signOut();
    forgetDutyStatus();
    forgetPretripMarker();
    forgetCompartmentAcknowledgements();
    queryClient.clear();
    router.replace('/sign-in');
  };

  const identity = me.data;

  const dqf = qualifications.data;
  // Every threshold and every banner line is decided in `lib/qualification-api.ts`.
  const dqfItems = dqf ? qualificationItems(dqf) : [];
  // R12.5 — persistent while ineligible: no dismiss control anywhere below.
  const blockingReasons =
    dqf && !dqf.is_dispatch_eligible ? ineligibilityReasons(dqf) : [];
  const dqfMissing =
    qualifications.error instanceof ApiError &&
    qualifications.error.status === 404;

  return (
    <ScrollView
      className="flex-1 bg-background"
      contentContainerClassName="gap-5 p-5 pb-28"
      refreshControl={
        <RefreshControl
          refreshing={me.isRefetching || qualifications.isRefetching}
          onRefresh={() => {
            void me.refetch();
            void qualifications.refetch();
          }}
        />
      }
    >
      <PermissionBanner
        permissions={{
          notifications: notifications.alertsDisabled
            ? 'denied'
            : notifications.permission,
          location: locationPermission,
        }}
      />

      {blockingReasons.length > 0 && (
        <Alert
          variant="destructive"
          icon={ShieldAlert}
          accessibilityLabel="You are not eligible for dispatch"
        >
          <AlertTitle>You are not eligible for dispatch</AlertTitle>
          <AlertDescription>
            Dispatch is blocked until the office clears the following:
          </AlertDescription>
          <View className="mt-2 gap-1 pl-7">
            {blockingReasons.map((reason, index) => (
              <Text key={`${index}-${reason}`} className="text-sm text-foreground">
                • {reason}
              </Text>
            ))}
          </View>
        </Alert>
      )}

      {/*
        R12.7 — the compliance record is its own card; duty status lives on
        the Work screen. The two vocabularies never meet.
      */}
      <Card>
        <CardHeader>
          <CardTitle>Qualification file</CardTitle>
          <CardDescription>
            Your compliance record, which is separate from your duty status on Work.
            {dqf
              ? ` Currently ${qualificationStatusLabel(dqf.qualification_status)}.`
              : ''}
          </CardDescription>
        </CardHeader>
        <CardContent className="gap-3">
          {qualifications.isLoading && (
            <Text className="text-sm text-muted-foreground">
              Reading your qualification file…
            </Text>
          )}
          {dqfMissing && (
            <Text className="text-sm text-muted-foreground">
              No qualification file is on record for you. The office maintains it.
            </Text>
          )}
          {qualifications.isError && !dqfMissing && (
            <Text className="text-sm text-muted-foreground">
              Your qualification file could not be read. Pull down to retry.
            </Text>
          )}
          {dqf && (
            <>
              <View className="flex-row items-center justify-between gap-2">
                <Text className="font-semibold">Dispatch eligibility</Text>
                <Badge
                  variant={dqf.is_dispatch_eligible ? 'secondary' : 'destructive'}
                >
                  <Text>
                    {dqf.is_dispatch_eligible ? 'Eligible' : 'Not eligible'}
                  </Text>
                </Badge>
              </View>
              <Text className="text-sm text-muted-foreground">
                CDL class: {dqf.cdl_class || 'Not on file'}
              </Text>
              {dqfItems.map((item) => (
                <QualificationRow key={item.key} item={item} />
              ))}
              <Text className="text-sm text-muted-foreground">
                Last drug test: {formatQualificationDate(dqf.last_drug_test_date)}
              </Text>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{identity?.driver_name || 'Driver'}</CardTitle>
          <CardDescription>Session and assignment</CardDescription>
        </CardHeader>
        <CardContent className="gap-2">
          <Text>Driver ID: {identity?.driver_id || 'Loading…'}</Text>
          <Text>Truck: {identity?.assigned_truck_id || 'Not assigned'}</Text>
        </CardContent>
      </Card>

      <Button variant="outline" disabled={signingOut} onPress={() => void exit()}>
        <Text>{signingOut ? 'Signing out…' : 'Sign out'}</Text>
      </Button>
    </ScrollView>
  );
}
