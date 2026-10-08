/**
 * Duty status and Hours of Service on the Work screen (UI revamp D16, R13.3,
 * R13.8). Moved from `app/(tabs)/profile.tsx` with the same queries, the same
 * `transition.mutate` calls and the same server-wins adoption; only where it
 * renders changed.
 *
 * - `DutyStatusChip`: the header chip. Tapping it opens the duty sheet.
 * - `DutyStatusSheet`: the three controls (`lib/duty-api.ts` maps them onto the
 *   server vocabulary, R13.4, R13.5) and the Hours-of-Service card (R17.29),
 *   still a separate section from the duty controls.
 * - `StartShiftCard`: shown while off duty, one tap to go on duty.
 * - `HosLimitBanner`: the at-limit / approaching-limit advisory, kept visible on
 *   Work because it matters while driving.
 *
 * Going on duty with no pre-trip queued today opens the pre-trip form with the
 * assigned unit prefilled (R13.3). Going off duty offers (never forces) the
 * post-trip form, because post-trip is only accepted where the carrier enables
 * it.
 *
 * Requirements: 13.2, 13.4, 13.5, 13.10, 16.20, 17.15, 17.16, 17.29
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRouter } from 'expo-router';
import { ChevronDown, Clock, OctagonAlert } from 'lucide-react-native';
import { useEffect, useState } from 'react';
import { Modal, Pressable, ScrollView, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { ChoiceOption } from '@/components/ChoiceOption';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Text } from '@/components/ui/text';
import {
  adoptServerDutyStatus,
  controlForStatus,
  dutyStatusLabel,
  DUTY_CONTROLS,
  loadDriverIdentity,
  storedDutyStatus,
  submitDutyStatus,
  type DutyControl,
  type DutyStatus,
} from '@/lib/duty-api';
import {
  hosFigureRows,
  hosLimitMessage,
  hosLimitState,
  hosReadingExplanation,
  loadHOSAdvisory,
  readingAgeLabel,
} from '@/lib/hos-api';
import { localCalendarDay } from '@/lib/inspection-api';
import { locationTracker } from '@/lib/location-tracker';
import { pretripQueuedOn } from '@/lib/pretrip-marker';
import { queryKeys } from '@/lib/query-keys';
import { STATUS, type StatusKey } from '@/lib/tokens';

/** Duty status → the status token the chip is drawn with. */
export function dutyStatusTone(status: string | null | undefined): StatusKey {
  switch (status) {
    case 'active':
      return 'ok';
    case 'on_break':
      return 'warning';
    case 'inactive':
      return 'cancelled';
    default:
      return 'draft';
  }
}

/**
 * The duty state, its server adoption and the transition, shared by the chip,
 * the sheet and the start-shift card. Same calls as the old Profile card.
 */
export function useDutyStatus({ onLeave }: { onLeave?: () => void } = {}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [localStatus, setLocalStatus] = useState<DutyStatus | null>(() => storedDutyStatus());
  const [notice, setNotice] = useState<string | null>(null);
  const [offerPostTrip, setOfferPostTrip] = useState(false);

  const me = useQuery({ queryKey: queryKeys.me(), queryFn: loadDriverIdentity });

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
      setNotice(`Duty status updated to ${dutyStatusLabel(adoption.status)} from the server record.`);
    }
  }, [me.data]);

  const transition = useMutation({
    mutationFn: (control: DutyControl) => submitDutyStatus(control),
    onSuccess: async (result, control) => {
      setLocalStatus(result.status);
      // R10.9, R10.10 — going on duty starts sampling; going off duty stops it.
      await locationTracker.applyDutyStatus(result.status);
      setNotice(`You are now ${dutyStatusLabel(result.status)}.`);
      setOfferPostTrip(control === 'off_duty');
      await queryClient.invalidateQueries({ queryKey: queryKeys.me() });
      if (control === 'on_duty' && !pretripQueuedOn(localCalendarDay())) {
        onLeave?.();
        router.push({
          pathname: '/inspection/new',
          params: {
            inspectionType: 'pre_trip',
            ...(me.data?.assigned_truck_id ? { assetId: me.data.assigned_truck_id } : {}),
          },
        });
      }
    },
    onError: (error) => {
      setNotice(error instanceof Error ? error.message : 'The duty status could not be changed.');
    },
  });

  const effectiveStatus = me.data?.duty_status ?? localStatus;

  const openPostTrip = () => {
    setOfferPostTrip(false);
    onLeave?.();
    router.push({
      pathname: '/inspection/new',
      params: {
        inspectionType: 'post_trip',
        ...(me.data?.assigned_truck_id ? { assetId: me.data.assigned_truck_id } : {}),
      },
    });
  };

  return {
    identity: me.data,
    identityLoaded: Boolean(me.data),
    effectiveStatus,
    activeControl: controlForStatus(effectiveStatus),
    transition,
    notice,
    offerPostTrip,
    openPostTrip,
    dismissPostTrip: () => setOfferPostTrip(false),
    refetch: () => me.refetch(),
  };
}

export type DutyState = ReturnType<typeof useDutyStatus>;

export function DutyStatusChip({ duty, onPress }: { duty: DutyState; onPress: () => void }) {
  const tone = STATUS[dutyStatusTone(duty.effectiveStatus)];
  const label = dutyStatusLabel(duty.effectiveStatus);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Duty status: ${label}. Change duty status`}
      onPress={onPress}
      testID="duty-chip"
      className="min-h-11 flex-row items-center gap-2 rounded-full border-2 px-4"
      style={{ backgroundColor: tone.bg, borderColor: tone.border }}
    >
      <View className="h-3 w-3 rounded-full" style={{ backgroundColor: tone.dot }} />
      <Text className="font-semibold" style={{ color: tone.fg }}>
        {label}
      </Text>
      <ChevronDown size={18} color={tone.fg} />
    </Pressable>
  );
}

/** One-tap start of shift while off duty (journey "Start shift", audit §i-e). */
export function StartShiftCard({ duty }: { duty: DutyState }) {
  if (duty.effectiveStatus !== 'off_duty' || !duty.identityLoaded) {
    return null;
  }
  return (
    <Card testID="start-shift">
      <CardHeader className="gap-1 p-4">
        <CardTitle className="text-lg">You are off duty</CardTitle>
        <CardDescription>Go on duty to receive assignments.</CardDescription>
      </CardHeader>
      <CardContent className="p-4 pt-0">
        <Button disabled={duty.transition.isPending} onPress={() => duty.transition.mutate('on_duty')}>
          <Text>{duty.transition.isPending ? 'Going on duty…' : 'Go on duty'}</Text>
        </Button>
      </CardContent>
    </Card>
  );
}

/** R17.15, R17.16 — the limit advisories, kept on Work. Nothing at all when within limits. */
export function HosLimitBanner() {
  const hos = useQuery({ queryKey: queryKeys.hos(), queryFn: loadHOSAdvisory });
  const statement = hos.data?.authoritativeRecordStatement ?? null;
  const advisory = statement ? hos.data?.advisory ?? null : null;
  const state = hosLimitState(advisory);
  const message = hosLimitMessage(advisory);
  if (state === 'at_limit' && message) {
    return (
      <Alert variant="destructive" icon={OctagonAlert} accessibilityLabel="You are at your Hours-of-Service driving limit">
        <AlertTitle>At your driving limit</AlertTitle>
        <AlertDescription>{message}</AlertDescription>
      </Alert>
    );
  }
  if (state === 'approaching_limit' && message) {
    return (
      <Alert icon={Clock} accessibilityLabel="You are approaching your Hours-of-Service driving limit">
        <AlertTitle>Approaching your driving limit</AlertTitle>
        <AlertDescription>{message}</AlertDescription>
      </Alert>
    );
  }
  return null;
}

/**
 * R17.29 — Hours of Service is its own card, sharing nothing with the duty
 * controls. Moved verbatim from the Profile screen.
 */
function HoursOfServiceCard() {
  const hos = useQuery({ queryKey: queryKeys.hos(), queryFn: loadHOSAdvisory });

  // R16.20 — figures only alongside the server's ELD statement.
  const hosStatement = hos.data?.authoritativeRecordStatement ?? null;
  const hosAdvisory = hosStatement ? hos.data?.advisory ?? null : null;
  const hosState = hosLimitState(hosAdvisory);
  const hosMessage = hosLimitMessage(hosAdvisory);
  const hosFigures = hosAdvisory ? hosFigureRows(hosAdvisory) : [];
  const hosExplanation = hosReadingExplanation(hosAdvisory);
  const hosAge = readingAgeLabel(hosAdvisory);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Hours of Service</CardTitle>
        <CardDescription>
          Advisory figures from your carrier&apos;s telematics, separate from the duty status above.
        </CardDescription>
      </CardHeader>
      <CardContent className="gap-3">
        {hos.isLoading && (
          <Text className="text-sm text-muted-foreground">Reading your Hours-of-Service advisory…</Text>
        )}
        {hos.isError && (
          <Text className="text-sm text-muted-foreground">
            Your Hours-of-Service advisory could not be read. Pull down on Work to retry.
          </Text>
        )}
        {hos.data && !hosStatement && (
          <Text className="text-sm text-muted-foreground">
            The advisory arrived without its Hours-of-Service disclosure, so no figures are shown.
          </Text>
        )}

        {hosState === 'at_limit' && hosMessage && (
          <Alert variant="destructive" icon={OctagonAlert} accessibilityLabel="You are at your Hours-of-Service driving limit">
            <AlertTitle>At your driving limit</AlertTitle>
            <AlertDescription>{hosMessage}</AlertDescription>
          </Alert>
        )}

        {hosState === 'approaching_limit' && hosMessage && (
          <Alert icon={Clock} accessibilityLabel="You are approaching your Hours-of-Service driving limit">
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
            {/* R17.13 — no figures is not the same as zero. */}
            {hosState === 'unavailable' && (
              <Text className="text-sm text-muted-foreground">
                Your carrier&apos;s telematics supplies no remaining-hours figures, so this app shows none.
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
            {hosExplanation && <Text className="text-sm text-muted-foreground">{hosExplanation}</Text>}
            <Text className="text-sm text-muted-foreground">
              Truck: {hosAdvisory.truck_id || 'Not assigned'}
              {hosAdvisory.provider_name ? ` · Provider: ${hosAdvisory.provider_name}` : ''}
            </Text>
            {hosAge && <Text className="text-sm text-muted-foreground">{hosAge}</Text>}
          </>
        )}

        {/* R16.20 — verbatim from the server. */}
        {hosStatement && <Text className="text-sm font-semibold text-muted-foreground">{hosStatement}</Text>}
      </CardContent>
    </Card>
  );
}

export function DutyStatusSheet({
  duty,
  visible,
  onClose,
}: {
  duty: DutyState;
  visible: boolean;
  onClose: () => void;
}) {
  const insets = useSafeAreaInsets();
  const { transition, effectiveStatus, activeControl } = duty;
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <View className="flex-1 justify-end bg-black/50">
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Close duty status"
          className="flex-1"
          onPress={onClose}
        />
        <View
          accessibilityViewIsModal
          className="max-h-[88%] rounded-t-3xl bg-background"
          style={{ paddingBottom: insets.bottom + 16 }}
        >
          <ScrollView contentContainerClassName="gap-4 p-5">
            <View className="flex-row items-center justify-between gap-3">
              <Text role="heading" aria-level={2} className="text-2xl font-bold">
                Duty status
              </Text>
              <Button variant="outline" size="sm" onPress={onClose}>
                <Text>Done</Text>
              </Button>
            </View>
            <Text className="text-muted-foreground">
              Currently {dutyStatusLabel(effectiveStatus)}
              {duty.identityLoaded ? '' : ' (reading the server record…)'}
            </Text>

            <View accessibilityRole="radiogroup" accessibilityLabel="Duty status" className="gap-2">
              {DUTY_CONTROLS.map((option) => (
                <ChoiceOption
                  key={option.control}
                  label={option.label}
                  description={option.description}
                  checked={option.control === activeControl}
                  disabled={transition.isPending}
                  onPress={() => transition.mutate(option.control)}
                  testID={`duty-${option.control}`}
                />
              ))}
            </View>
            {effectiveStatus === 'inactive' && (
              <Text className="text-sm text-muted-foreground">
                Dispatch has set your record to inactive. Only the office can change that.
              </Text>
            )}

            {duty.notice && (
              <View accessibilityLiveRegion="polite" className="rounded-xl bg-muted p-4">
                <Text>{duty.notice}</Text>
              </View>
            )}

            {duty.offerPostTrip && (
              <View className="gap-2 rounded-xl border-2 border-input p-4">
                <Text className="font-semibold">End-of-day inspection</Text>
                <Text className="text-sm text-muted-foreground">
                  Record a post-trip walk-around now, where your carrier uses them.
                </Text>
                <View className="flex-row gap-2">
                  <Button
                    size="sm"
                    className="flex-1"
                    onPress={duty.openPostTrip}
                  >
                    <Text>Start post-trip</Text>
                  </Button>
                  <Button size="sm" variant="outline" className="flex-1" onPress={duty.dismissPostTrip}>
                    <Text>Not now</Text>
                  </Button>
                </View>
              </View>
            )}

            <HoursOfServiceCard />
          </ScrollView>
        </View>
      </View>
    </Modal>
  );
}
