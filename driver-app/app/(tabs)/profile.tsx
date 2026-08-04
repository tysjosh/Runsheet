/**
 * Driver profile — duty status, permissions, session.
 *
 * Duty status maps its controls onto the server vocabulary in `lib/duty-api.ts`:
 * the on-duty control is `active`, the off-duty control is `off_duty`, and
 * `on_break` is a third control of its own (R13.4, R13.5). There is no control
 * for `inactive` — that is an administrator-set value and a driver-submitted
 * transition to it is a 403 (R13.2).
 *
 * The server is authoritative. `GET /api/driver/me` is read here and on launch,
 * and its value replaces whatever this device stored (R13.10).
 *
 * The qualification file is a **separate section** from the duty status control
 * (R12.7). The two carry different `DriverStatus` vocabularies —
 * `compliance/models/driver.py:34`'s `active | suspended | expired` against
 * `fuel/order_models.py:63`'s `active | inactive | on_break | off_duty` — so they
 * never share a card, a label helper, or a query key. Every expiry threshold and
 * every banner line comes from `lib/qualification-api.ts`; this screen classifies
 * nothing (R12.3, R12.4, R12.5).
 *
 * The Hours-of-Service advisory is a **third separate section** (R17.29). Three
 * vocabularies, three cards: Runsheet availability (`active | inactive |
 * on_break | off_duty`), compliance qualification (`active | suspended |
 * expired`), and the telematics vendor's own duty-status string carried on an HOS
 * reading (R17.27). The 60-minute threshold and the out-of-hours mapping are
 * decided in `lib/hos-api.ts`, and the statement that the carrier's ELD is the
 * authoritative record is rendered verbatim from the server response (R16.20).
 *
 * Requirements: 12.3, 12.4, 12.5, 12.7, 13.2, 13.4, 13.5, 13.10, 16.20, 17.15,
 * 17.16, 17.29, 9.12, 10.15, 16.13
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { Clock, OctagonAlert, ShieldAlert } from 'lucide-react-native';
import { useEffect, useState, useSyncExternalStore } from 'react';
import { Pressable, RefreshControl, ScrollView, View } from 'react-native';

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
import {
  adoptServerDutyStatus,
  controlForStatus,
  dutyStatusLabel,
  DUTY_CONTROLS,
  forgetDutyStatus,
  loadDriverIdentity,
  storedDutyStatus,
  submitDutyStatus,
  type DutyControl,
} from '@/lib/duty-api';
import { locationPermissionDecision } from '@/lib/geotag';
import {
  hosFigureRows,
  hosLimitMessage,
  hosLimitState,
  hosReadingExplanation,
  loadHOSAdvisory,
  readingAgeLabel,
} from '@/lib/hos-api';
import { locationTracker } from '@/lib/location-tracker';
import { notificationManager } from '@/lib/notification-manager';
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
  const [notice, setNotice] = useState<string | null>(null);
  const [localStatus, setLocalStatus] = useState(() => storedDutyStatus());

  const me = useQuery({
    queryKey: queryKeys.me(),
    queryFn: loadDriverIdentity,
  });

  // R12.1 — the read carries no identifier; the server scopes it to the session.
  const qualifications = useQuery({
    queryKey: queryKeys.dqf(),
    queryFn: loadDriverQualifications,
  });

  // R17.32 — likewise scoped to the session; no `driver_id` is sent.
  const hos = useQuery({
    queryKey: queryKeys.hos(),
    queryFn: loadHOSAdvisory,
  });

  useEffect(() => {
    void locationPermissionDecision().then(setLocationPermission);
  }, []);

  // R13.10 — the server value wins whenever the two differ.
  useEffect(() => {
    if (!me.data) {
      return;
    }
    const adoption = adoptServerDutyStatus(me.data.duty_status);
    setLocalStatus(adoption.status);
    // R10.9, R10.10 — location sampling follows the status now in force.
    void locationTracker.applyDutyStatus(adoption.status).catch(() => undefined);
    if (adoption.adopted) {
      setNotice(
        `Duty status updated to ${dutyStatusLabel(adoption.status)} from the server record.`,
      );
    }
  }, [me.data]);

  const transition = useMutation({
    mutationFn: (control: DutyControl) => submitDutyStatus(control),
    onSuccess: async (result) => {
      setLocalStatus(result.status);
      // R10.9, R10.10 — going on duty starts sampling; going off duty stops it.
      await locationTracker.applyDutyStatus(result.status);
      setNotice(`You are now ${dutyStatusLabel(result.status)}.`);
      await queryClient.invalidateQueries({ queryKey: queryKeys.me() });
    },
    onError: (error) => {
      setNotice(
        error instanceof Error
          ? error.message
          : 'The duty status could not be changed.',
      );
    },
  });

  const exit = async () => {
    setSigningOut(true);
    driverWebSocket.disconnect();
    // R10.10 — a signed-out device is not on duty, so it samples nothing.
    await locationTracker.shutdown();
    await signOut();
    forgetDutyStatus();
    forgetCompartmentAcknowledgements();
    queryClient.clear();
    router.replace('/sign-in');
  };

  const identity = me.data;
  const effectiveStatus = identity?.duty_status ?? localStatus;
  const activeControl = controlForStatus(effectiveStatus);

  const dqf = qualifications.data;
  // Every threshold and every banner line is decided in `lib/qualification-api.ts`.
  const dqfItems = dqf ? qualificationItems(dqf) : [];
  // R12.5 — persistent while ineligible: no dismiss control anywhere below.
  const blockingReasons =
    dqf && !dqf.is_dispatch_eligible ? ineligibilityReasons(dqf) : [];
  const dqfMissing =
    qualifications.error instanceof ApiError &&
    qualifications.error.status === 404;

  // R16.20 — the figures are shown only alongside the server's ELD statement, so
  // a response that omitted it yields no advisory to render rather than an
  // undisclosed one. The sentence itself is never authored here.
  const hosStatement = hos.data?.authoritativeRecordStatement ?? null;
  const hosAdvisory = hosStatement ? hos.data?.advisory ?? null : null;
  // R17.15, R17.16 — the threshold and the out-of-hours mapping are decided in
  // `lib/hos-api.ts`; this screen renders the state it is handed.
  const hosState = hosLimitState(hosAdvisory);
  const hosMessage = hosLimitMessage(hosAdvisory);
  const hosFigures = hosAdvisory ? hosFigureRows(hosAdvisory) : [];
  const hosExplanation = hosReadingExplanation(hosAdvisory);
  const hosAge = readingAgeLabel(hosAdvisory);

  return (
    <ScrollView
      className="flex-1 bg-background"
      contentContainerClassName="gap-5 p-5 pb-28"
      refreshControl={
        <RefreshControl
          refreshing={
            me.isRefetching || qualifications.isRefetching || hos.isRefetching
          }
          onRefresh={() => {
            void me.refetch();
            void qualifications.refetch();
            void hos.refetch();
          }}
        />
      }
    >
      <View className="gap-1">
        <Text className="text-3xl font-bold">Driver profile</Text>
        <Text className="text-muted-foreground">
          Duty status, Hours of Service, qualification file, alerts and session
        </Text>
      </View>

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

      <Card>
        <CardHeader>
          <CardTitle>Duty status</CardTitle>
          <CardDescription>
            Currently {dutyStatusLabel(effectiveStatus)}
            {identity ? '' : ' (reading the server record…)'}
          </CardDescription>
        </CardHeader>
        <CardContent className="gap-2">
          {DUTY_CONTROLS.map((option) => {
            const active = option.control === activeControl;
            return (
              <Pressable
                key={option.control}
                accessibilityRole="radio"
                accessibilityState={{
                  selected: active,
                  disabled: transition.isPending,
                }}
                disabled={transition.isPending}
                onPress={() => transition.mutate(option.control)}
                className={
                  active
                    ? 'rounded-xl border-2 border-primary p-4'
                    : 'rounded-xl border border-input p-4'
                }
              >
                <Text className="font-semibold">{option.label}</Text>
                <Text className="text-sm text-muted-foreground">
                  {option.description}
                </Text>
              </Pressable>
            );
          })}
          {effectiveStatus === 'inactive' && (
            <Text className="text-sm text-muted-foreground">
              Dispatch has set your record to inactive. Only the office can
              change that.
            </Text>
          )}
        </CardContent>
      </Card>

      {notice && (
        <View className="rounded-xl bg-muted p-4">
          <Text>{notice}</Text>
        </View>
      )}

      {/*
        R17.29 — Hours of Service is a third card of its own, sharing nothing
        with the duty status control above it or the qualification file below.
        The duty statuses on this screen are Runsheet availability values; the
        `duty_status` inside the advisory is the telematics vendor's own string
        (R17.27), and neither is ever labelled by the other's helper.
      */}
      <Card>
        <CardHeader>
          <CardTitle>Hours of Service</CardTitle>
          <CardDescription>
            Advisory figures from your carrier&apos;s telematics, separate from the
            duty status control above.
          </CardDescription>
        </CardHeader>
        <CardContent className="gap-3">
          {hos.isLoading && (
            <Text className="text-sm text-muted-foreground">
              Reading your Hours-of-Service advisory…
            </Text>
          )}
          {hos.isError && (
            <Text className="text-sm text-muted-foreground">
              Your Hours-of-Service advisory could not be read. Pull down to
              retry.
            </Text>
          )}
          {hos.data && !hosStatement && (
            <Text className="text-sm text-muted-foreground">
              The advisory arrived without its Hours-of-Service disclosure, so no
              figures are shown.
            </Text>
          )}

          {/* R17.16 — the at-limit state, destructive and icon-distinct. */}
          {hosState === 'at_limit' && hosMessage && (
            <Alert
              variant="destructive"
              icon={OctagonAlert}
              accessibilityLabel="You are at your Hours-of-Service driving limit"
            >
              <AlertTitle>At your driving limit</AlertTitle>
              <AlertDescription>{hosMessage}</AlertDescription>
            </Alert>
          )}

          {/* R17.15 — the approaching-limit advisory, deliberately not destructive. */}
          {hosState === 'approaching_limit' && hosMessage && (
            <Alert
              icon={Clock}
              accessibilityLabel="You are approaching your Hours-of-Service driving limit"
            >
              <AlertTitle>Approaching your driving limit</AlertTitle>
              <AlertDescription>{hosMessage}</AlertDescription>
            </Alert>
          )}

          {hosAdvisory && (
            <>
              {hosState === 'within_limits' && (
                <View className="flex-row items-center justify-between gap-2">
                  <Text className="font-semibold">Drive time</Text>
                  <Badge variant="secondary">
                    <Text>Within limits</Text>
                  </Badge>
                </View>
              )}
              {/*
                R17.13 — a duty-status-only connector supplies no figures. Saying
                so is not the same as showing zero, which would read as being out
                of hours.
              */}
              {hosState === 'unavailable' && (
                <Text className="text-sm text-muted-foreground">
                  Your carrier&apos;s telematics supplies no remaining-hours
                  figures, so this app shows none.
                </Text>
              )}
              {hosFigures.map((row) => (
                <View key={row.key} className="gap-1 rounded-xl border border-input p-3">
                  <Text className="font-semibold">{row.label}</Text>
                  <Text
                    className={
                      row.figure.availability === 'available'
                        ? 'text-sm text-foreground'
                        : 'text-sm text-muted-foreground'
                    }
                  >
                    {row.display}
                  </Text>
                </View>
              ))}
              {hosExplanation && (
                <Text className="text-sm text-muted-foreground">
                  {hosExplanation}
                </Text>
              )}
              <Text className="text-sm text-muted-foreground">
                Truck: {hosAdvisory.truck_id || 'Not assigned'}
                {hosAdvisory.provider_name
                  ? ` · Provider: ${hosAdvisory.provider_name}`
                  : ''}
              </Text>
              {hosAge && (
                <Text className="text-sm text-muted-foreground">{hosAge}</Text>
              )}
            </>
          )}

          {/*
            R16.20 — the statement that accompanies every figure above, taken
            verbatim from the server so the app cannot restate it differently.
          */}
          {hosStatement && (
            <Text className="text-sm font-semibold text-muted-foreground">
              {hosStatement}
            </Text>
          )}
        </CardContent>
      </Card>

      {/*
        R12.7 — the compliance record is its own card, below the duty status
        control and never inside it. The two vocabularies never meet.
      */}
      <Card>
        <CardHeader>
          <CardTitle>Qualification file</CardTitle>
          <CardDescription>
            Your compliance record, which is separate from the duty status above.
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
